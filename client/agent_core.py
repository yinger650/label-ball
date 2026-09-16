#!/usr/bin/env python3
"""Agent 标注内核：把 `client/manual.py` 里跑通的那套流程从 Flask 里抽出来。

`manual.py` 已经把「领包 → 后台下载解码 → 心跳续租 → 写 labels.json → 提交
`source=human` 免审入库」跑了很多包了，这里一个字都不重新设计，只是把它从
Flask 的 request/jsonify 里剥出来变成纯 Python，好让 `client/mcp_server.py`
（stdio MCP）和测试直接调用。分法照 `client/lab_core.py` + `client/lab.py`：
core 只收 Python 参数、只返回 dict，外壳负责协议。

两条硬约束，改动时别绕过：

- **坐标只有一条通道**：`views.GridView.cell_center(cell)` → ROI 像素 →
  `prep.PreparedPacket.to_pano`。ROI 常常跨过全景 0° 接缝，取模在
  `prep.roi_to_pano` 里做过了，任何地方再写一遍减法都会在接缝上错一整个 pano 宽。
- **labels.json 的字段是 `blurred`**（不是 `motion_blur`），和 `annotate.js`、
  `packet.build_result`、服务端 GT schema 对齐。

返回值约定：每个方法都返回 dict；出错时带 `error` 键（外壳据此置 MCP 的
`isError`），不抛异常——agent 拿到一句中文比拿到 traceback 有用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import track as T  # noqa: E402
from client import views as V  # noqa: E402
from client.api import ServerClient  # noqa: E402
from client.prep import (  # noqa: E402
    PrepareStatus,
    is_raw_ready,
    is_ready,
    load_prepared,
    prepare_packet,
    workspace,
)
from client.run import (  # noqa: E402
    DEFAULT_WORK,
    iter_label_rows,
    load_label_doc,
    local_labels_path,
)
from packet import build_result  # noqa: E402

log = logging.getLogger("client.agent_core")

MODES = ("global", "crop", "time_grid")

# `pane` 参数的取值。agent 标注路线和 AI 工作流吃同样的图，判据才能是同一套。
VIEW_PANE_SETS = {
    "both": ("raw", "enh"),
    "raw": ("raw",),
    "enh": ("enh",),
    # 彩色时间扫描图：R=当前帧、G=之后、B=之前。静止的灯光和边线三色齐亮成灰白。
    "scan": ("scan_rgb", "raw"),
    "trail": ("enh_maxproj", "enh"),
}

# time_grid 的默认几何：160 px 的窗够装下 8 帧的球位移（实测球逐帧挪 12 px 中位、
# 48 px p90），300 px 一格出图后每格约 2× 放大。
DEFAULT_TIME_GRID_SIZE = V.TIME_GRID_SIZE
DEFAULT_TIME_GRID_TILES = 9

# 逐级放大的默认几何。数字不是拍的，来自 client/workflows/erp_ball_v1.yaml 顶部
# 那张实测表：480 px 窗 6×6 只有 38% 命中，80 px 窗 8×8 到 88%，45 px 窗 9×9
# 100%。所以 crop 默认给最保守的 480——没有先验时它是唯一能覆盖足够大范围的一级，
# agent 该自己往下钻。
DEFAULT_CROP_SIZE = 480.0
DEFAULT_CROP_COLS = 8
DEFAULT_CROP_ROWS = 8
DEFAULT_GLOBAL_COLS = 9  # 三段横切，每段 3 列，字母连续
DEFAULT_GLOBAL_ROWS = 3
GLOBAL_STRIPS = 3

# 每个 pane 出图的目标边长。`make_crop_view` 自己还会按 max_side 收一次，
# 这里只是把「放大到能看见像素块」这件事从 agent 手里拿走。
CROP_TARGET_PX = 800
MAX_UPSCALE = 24.0

PANES_CROP = ("raw", "enh")
PANES_GLOBAL = ("raw", "enh")

# 视图登记只存几何（几百字节），但也别无限长。淘汰掉的仍能从磁盘的 <view_id>.json
# 重建，所以淘汰是安全的。
VIEW_REGISTRY_CAP = 512


# ---------------------------------------------------------------- 视图登记


@dataclass
class ViewRef:
    """一张出好的图。`view` 是把格子号翻回 ROI 像素的唯一权威。"""

    view_id: str
    packet_id: str
    frame_idx: int
    mode: str
    view: V.GridView
    paths: List[Path] = field(default_factory=list)
    center: Optional[Tuple[float, float]] = None
    size: float = 0.0
    center_source: str = ""
    panes: List[str] = field(default_factory=list)
    strips: List[V.GridView] = field(default_factory=list)


def _geom_of(view: V.GridView) -> dict:
    return {
        "cols": int(view.cols),
        "rows": int(view.rows),
        "origin": [float(view.origin[0]), float(view.origin[1])],
        "cell_w": float(view.cell_w),
        "cell_h": float(view.cell_h),
        "col_offset": int(view.col_offset),
        "kind": view.kind,
        "frame_idx": int(view.frame_idx),
        "extent": [float(v) for v in view.extent],
    }


def _view_from_geom(g: dict) -> V.GridView:
    """从落盘的几何重建一个只用来算坐标的 GridView（图是 1×1 占位）。

    进程重启后 agent 手里的 view_id 还能兑现成坐标，靠的就是这条路。
    """
    return V.GridView(
        image=np.zeros((1, 1, 3), np.uint8),
        cols=int(g["cols"]),
        rows=int(g["rows"]),
        origin=(float(g["origin"][0]), float(g["origin"][1])),
        cell_w=float(g["cell_w"]),
        cell_h=float(g["cell_h"]),
        kind=str(g.get("kind") or "crop"),
        frame_idx=int(g.get("frame_idx") or -1),
        extent=tuple(float(v) for v in (g.get("extent") or (0, 0, 0, 0))),  # type: ignore[arg-type]
        col_offset=int(g.get("col_offset") or 0),
    )


def _auto_upscale(size: float) -> float:
    return max(1.0, min(MAX_UPSCALE, CROP_TARGET_PX / max(float(size), 1.0)))


def _xy(val: Any) -> Optional[Tuple[float, float]]:
    if val is None:
        return None
    if isinstance(val, dict):
        if val.get("x") is None or val.get("y") is None:
            return None
        return float(val["x"]), float(val["y"])
    if isinstance(val, (list, tuple)) and len(val) >= 2:
        if val[0] is None or val[1] is None:
            return None
        return float(val[0]), float(val[1])
    return None


# ---------------------------------------------------------------- 会话


FetchFn = Callable[..., Any]
PrepareFn = Callable[..., Any]


class AgentSession:
    """一个 agent 标注员的全部状态：租约、工作区、出过的图。

    线程安全的部分只有 `_jobs`（后台 prepare 线程会写）和视图登记；labels.json
    是「读整份 → 改一条 → 写整份」，同一个 agent 串行调用工具，不加锁也不会打架。
    """

    def __init__(
        self,
        api: ServerClient,
        work_root: Path = DEFAULT_WORK,
        worker: str = "agent",
        hb_interval: float = 120.0,
        fetch_fn: Optional[FetchFn] = None,
        store: Any = None,
        prepare_fn: Optional[PrepareFn] = None,
    ) -> None:
        self.api = api
        self.work_root = Path(work_root)
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.worker = worker or "agent"
        self.hb_interval = float(hb_interval)
        self.fetch_fn = fetch_fn
        self.store = store
        self.prepare_fn = prepare_fn
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._job_lock = threading.Lock()
        self._views: Dict[str, ViewRef] = {}
        self._view_lock = threading.Lock()
        self._preps: Dict[str, Any] = {}
        self._hb: set = set()

    # ------------------------------------------------------------ 落盘

    @staticmethod
    def _read_json(p: Path) -> dict:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def session_path(self, pid: str) -> Path:
        return workspace(self.work_root, pid) / "session.json"

    def load_session(self, pid: str) -> dict:
        return self._read_json(self.session_path(pid))

    def save_session(self, pid: str, data: dict) -> None:
        p = self.session_path(pid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def labels_dict(self, pid: str) -> Dict[str, dict]:
        """读 labels.json。三种历史形态都认（照搬 manual.py）。"""
        doc = load_label_doc(local_labels_path(self.work_root, pid)) or {}
        out: Dict[str, dict] = {}
        if isinstance(doc, dict) and not isinstance(doc.get("labels"), (list, dict)):
            for k, v in doc.items():
                if isinstance(v, dict) and v.get("frame_idx") is not None:
                    out[str(int(v["frame_idx"]))] = v
            if out:
                return out
        for r in iter_label_rows(doc):
            if r.get("frame_idx") is None:
                continue
            out[str(int(r["frame_idx"]))] = r
        return out

    def write_labels(self, pid: str, labels: Dict[str, dict]) -> None:
        p = local_labels_path(self.work_root, pid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")

    def _merge_prior(self, pid: str, prior: Sequence[dict], mark_ai: bool = True) -> Dict[str, dict]:
        labels = self.labels_dict(pid)
        for r in prior or []:
            if not isinstance(r, dict) or r.get("frame_idx") is None:
                continue
            key = str(int(r["frame_idx"]))
            if key in labels:
                continue
            rec = dict(r)
            rec["frame_idx"] = int(r["frame_idx"])
            if mark_ai:
                rec["from_ai"] = True
            labels[key] = rec
        self.write_labels(pid, labels)
        return labels

    # ------------------------------------------------------------ 心跳 / 准备

    def _heartbeat_loop(self, lease_id: str) -> None:
        """照搬 manual.py：靠扫 session.json 判断该不该继续续租。

        不持引用、不留 stop 事件——agent 可能在任何一步被掐掉，用磁盘状态当真相
        比用内存标志靠得住；会话标 done 或文件没了，线程自己退。
        """
        interval = max(5.0, self.hb_interval)
        while True:
            time.sleep(interval)
            alive = False
            for p in self.work_root.glob("*/session.json"):
                d = self._read_json(p)
                if d.get("lease_id") == lease_id and not d.get("done"):
                    alive = True
                    break
            if not alive:
                self._hb.discard(lease_id)
                return
            try:
                self.api.heartbeat(lease_id, "annotate")
            except Exception as e:  # noqa: BLE001
                log.warning("心跳失败 lease=%s: %s", lease_id, e)

    def _ensure_heartbeat(self, lease_id: str) -> None:
        if not lease_id or lease_id in self._hb:
            return
        self._hb.add(lease_id)
        threading.Thread(
            target=self._heartbeat_loop, args=(lease_id,),
            name=f"hb-{lease_id[:8]}", daemon=True,
        ).start()

    def start_prepare(self, pid: str, aoss_key: str) -> None:
        """后台下载 + 解码。一包 1–3 分钟，agent 用 ball_status 轮询。"""
        st = PrepareStatus()
        with self._job_lock:
            self._jobs[pid] = {"state": "pending", "msg": "排队下载", "status": st}

        def run() -> None:
            try:
                dest_dir = workspace(self.work_root, pid)
                dest_dir.mkdir(parents=True, exist_ok=True)
                if not is_ready(self.work_root, pid):
                    st.set(state="pending", msg="正在从 AOSS 下载…")
                    if self.fetch_fn is None:
                        from client.fetch import ensure_packet_files, open_store
                        from packet import ALL_ARTIFACT_FILES
                        store = self.store or open_store()
                        ensure_packet_files(store, aoss_key, dest_dir, pid, which=ALL_ARTIFACT_FILES)
                    else:
                        self.fetch_fn(aoss_key=aoss_key, dest=dest_dir / "packet.zip")
                    st.set(state="decoding", msg="正在解码…")
                    prep_fn = self.prepare_fn or prepare_packet
                    src = dest_dir if (dest_dir / "clip.mp4").exists() else dest_dir / "packet.zip"
                    prep_fn(src, self.work_root, packet_id=pid, status=st)
                else:
                    st.set(state="ready", msg="已就绪")
                with self._job_lock:
                    self._jobs[pid] = {"state": "ready", "msg": "就绪", "status": st}
            except Exception as e:  # noqa: BLE001
                log.exception("准备 %s 失败", pid)
                st.set(state="error", msg=f"{type(e).__name__}: {e}")
                with self._job_lock:
                    self._jobs[pid] = {"state": "error", "msg": str(e), "status": st}

        threading.Thread(target=run, name=f"prep-{pid}", daemon=True).start()

    def job_state(self, pid: str) -> dict:
        with self._job_lock:
            job = dict(self._jobs.get(pid) or {})
        st: Optional[PrepareStatus] = job.get("status")
        snap = st.snapshot() if st else {}
        ready = is_ready(self.work_root, pid)
        state = job.get("state") or snap.get("state") or ("ready" if ready else "pending")
        if ready:
            state = "ready"
        total = int(snap.get("total") or 0)
        done = int(snap.get("done") or 0)
        return {
            "state": state,
            "msg": job.get("msg") or snap.get("msg") or "",
            "done": done,
            "total": total,
            "progress": round(done / total, 3) if total else (1.0 if state == "ready" else 0.0),
            "ready": state == "ready",
        }

    # ------------------------------------------------------------ 包信息

    def prep(self, pid: str):
        p = self._preps.get(pid)
        if p is None:
            p = load_prepared(self.work_root, pid)
            # 默认 LRU 缓 96 帧，是按「工作流一帧要看好几次」调的。agent 是一帧标完
            # 就往下走，缓那么多只是把几百 MB 的解码帧钉在内存里（ROI 一张 raw 就
            # 7 MB），缩到十几帧足够覆盖同一帧的多级裁切。
            p._cap = 12
            self._preps[pid] = p
        return p

    def _meta_dict(self, pid: str, sess: dict) -> dict:
        """就绪后用解包出来的 meta.json，没就绪就用 lease 带回的 meta 顶一下。"""
        if is_ready(self.work_root, pid):
            m = self.prep(pid).meta
            return {
                "group": m.group, "stem": m.stem, "tag": m.tag,
                "fps": m.fps, "pano_size": list(m.pano_size), "roi": list(m.roi),
                "step": int(m.step or 1), "targets": [int(x) for x in m.targets],
                "frame_start": m.frame_start, "frame_end": m.frame_end,
            }
        m = dict(sess.get("meta") or {})
        return {
            "group": m.get("group"), "stem": m.get("stem"), "tag": m.get("tag"),
            "fps": m.get("fps"), "pano_size": list(m.get("pano_size") or []),
            "roi": list(m.get("roi") or []), "step": int(m.get("step") or 1),
            "targets": [int(x) for x in (m.get("targets") or [])],
            "frame_start": m.get("frame_start"), "frame_end": m.get("frame_end"),
        }

    @staticmethod
    def _is_repair(sess: dict) -> bool:
        return sess.get("mode") == "repair" or sess.get("kind") == "repair"

    def targets_of(self, pid: str, sess: dict, meta: dict) -> List[int]:
        """要标满的帧。修补模式只标被打回的那几帧。"""
        if self._is_repair(sess):
            tf = [int(x) for x in (sess.get("target_frames") or [])]
            if tf:
                return tf
        return list(meta.get("targets") or [])

    def _progress(self, pid: str, sess: dict, meta: dict) -> dict:
        labels = self.labels_dict(pid)
        targets = self.targets_of(pid, sess, meta)
        missing = [t for t in targets if str(t) not in labels]
        vis = [r for r in labels.values() if r.get("visible") and r.get("pano_x") is not None]
        return {
            "n_targets": len(targets),
            "n_labeled": sum(1 for t in targets if str(t) in labels),
            "n_visible": len(vis),
            "n_missing": len(missing),
            "next_frame": missing[0] if missing else None,
            "missing": missing,
            "labels": labels,
            "targets": targets,
        }

    # ------------------------------------------------------------ 领包

    def claim(self, worker: str = "", kind: str = "human") -> dict:
        """领一个包（或接上自己没做完的那个）。后台起下载解码 + 心跳。"""
        who = worker or self.worker or "agent"
        kind = str(kind or "human")
        if kind not in ("human", "repair"):
            return {"error": "kind 只能是 human / repair"}

        for sess_file in sorted(self.work_root.glob("*/session.json")):
            sess = self._read_json(sess_file)
            if sess.get("worker") != who or sess.get("done"):
                continue
            pid = sess.get("packet_id") or sess_file.parent.name
            with self._job_lock:
                job = dict(self._jobs.get(pid) or {})
            if not (is_ready(self.work_root, pid) or job.get("state") == "ready"):
                if job.get("state") == "error":
                    continue
                if not job:
                    self.start_prepare(pid, sess.get("aoss_key") or "")
            self._ensure_heartbeat(str(sess.get("lease_id") or ""))
            return self._claim_payload(pid, sess, resume=True)

        try:
            lease = self.api.lease(who, kind)
        except Exception as e:  # noqa: BLE001
            return {"error": f"lease 失败: {type(e).__name__}: {e}"}
        if lease is None:
            return {
                "packet_id": None,
                "leased": False,
                "message": "池子里暂时没有适合的包（repair 模式要有 need_human 的包）",
            }

        pid = lease.packet_id
        sess = {
            "packet_id": pid,
            "lease_id": lease.lease_id,
            "kind": kind,
            "mode": "repair" if kind == "repair" else "full",
            "worker": who,
            "aoss_key": lease.aoss_key,
            "target_frames": lease.target_frames,
            "meta": lease.meta,
            "started_at": time.time(),
            "done": False,
        }
        self.save_session(pid, sess)
        self._merge_prior(pid, lease.prior_labels, mark_ai=(kind == "repair"))
        self._ensure_heartbeat(lease.lease_id)
        self.start_prepare(pid, lease.aoss_key)
        return self._claim_payload(pid, sess, resume=False)

    def _claim_payload(self, pid: str, sess: dict, resume: bool) -> dict:
        meta = self._meta_dict(pid, sess)
        prog = self._progress(pid, sess, meta)
        job = self.job_state(pid)
        return {
            "packet_id": pid,
            "leased": True,
            "resume": bool(resume),
            "lease_id": sess.get("lease_id"),
            "kind": sess.get("kind") or "human",
            "mode": sess.get("mode") or "full",
            "worker": sess.get("worker") or self.worker,
            "roi": meta["roi"],
            "pano_size": meta["pano_size"],
            "step": meta["step"],
            "fps": meta["fps"],
            "n_targets": prog["n_targets"],
            "n_labeled": prog["n_labeled"],
            "target_frames": prog["targets"],
            "prepare": job,
            "message": (
                f"{'接上未完成的包' if resume else '已领到包'} {pid}，"
                f"{prog['n_targets']} 个目标帧要标；准备状态 {job['state']}，"
                f"就绪前先用 ball_status 轮询（下载+解码约 1–3 分钟）"
            ),
        }

    # ------------------------------------------------------------ 状态 / 帧

    def prepare_state(self, packet_id: str) -> dict:
        sess = self.load_session(packet_id)
        if not sess and not is_ready(self.work_root, packet_id):
            return {"error": f"没有 {packet_id} 的会话，先用 ball_lease 领包"}
        meta = self._meta_dict(packet_id, sess)
        prog = self._progress(packet_id, sess, meta)
        job = self.job_state(packet_id)
        started = float(sess.get("started_at") or 0.0)
        return {
            "packet_id": packet_id,
            "state": job["state"],
            "ready": job["ready"],
            "msg": job["msg"],
            "done": job["done"],
            "total": job["total"],
            "progress": job["progress"],
            "kind": sess.get("kind") or "human",
            "mode": sess.get("mode") or "full",
            "submitted": bool(sess.get("done")),
            "n_targets": prog["n_targets"],
            "n_labeled": prog["n_labeled"],
            "n_visible": prog["n_visible"],
            "n_missing": prog["n_missing"],
            "next_frame": prog["next_frame"],
            "elapsed_sec": round(time.time() - started, 1) if started else None,
        }

    def frames(self, packet_id: str) -> dict:
        sess = self.load_session(packet_id)
        if not is_ready(self.work_root, packet_id):
            job = self.job_state(packet_id)
            return {
                "error": f"{packet_id} 还没准备好（{job['state']}: {job['msg']}），"
                         f"用 ball_status 等到 ready 再来",
                "packet_id": packet_id,
                "prepare": job,
            }
        prep = self.prep(packet_id)
        meta = self._meta_dict(packet_id, sess)
        prog = self._progress(packet_id, sess, meta)
        rows: Dict[str, dict] = {}
        pts: List[Tuple[int, float, float]] = []
        for key in sorted(prog["labels"], key=lambda s: int(s)):
            r = dict(prog["labels"][key])
            if r.get("pano_x") is not None and r.get("pano_y") is not None:
                rx, ry = prep.to_roi(float(r["pano_x"]), float(r["pano_y"]))
                r["roi_x"], r["roi_y"] = round(rx, 2), round(ry, 2)
                if r.get("visible"):
                    pts.append((int(r["frame_idx"]), rx, ry))
            rows[key] = r
        nxt = prog["next_frame"]
        pred = None
        if nxt is not None and pts:
            got = T.predict_next([p for p in pts if p[0] < nxt], nxt, step=meta["step"])
            if got:
                pred = {"center": [round(got[0], 1), round(got[1], 1)], "radius_px": round(got[2], 1)}
        return {
            "packet_id": packet_id,
            "ready": True,
            "roi": meta["roi"],
            "pano_size": meta["pano_size"],
            "step": meta["step"],
            "fps": meta["fps"],
            "mode": sess.get("mode") or "full",
            "targets": prog["targets"],
            "n_targets": prog["n_targets"],
            "n_labeled": prog["n_labeled"],
            "n_visible": prog["n_visible"],
            "missing": prog["missing"],
            "next_frame": nxt,
            "labels": rows,
            "recent_visible": [[fi, round(x, 2), round(y, 2)] for fi, x, y in pts[-6:]],
            "predicted": pred,
            "hint": (
                "已标可见点 ≥2 个时，predicted.center 就是下一帧的外推中心，直接喂给 "
                "ball_view(mode='crop', center=...)；一个都没有就先 mode='global' 定位。"
            ),
        }

    # ------------------------------------------------------------ 出图

    def _views_dir(self, pid: str) -> Path:
        d = workspace(self.work_root, pid) / "views"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _resolve_center(
        self, prep, packet_id: str, sess: dict, meta: dict, frame_idx: int,
        center: Any,
    ) -> Tuple[Optional[Tuple[float, float]], str]:
        """crop 的中心：显式给的 > 按已标点外推 > 最近一个可见点 > ROI 中心。"""
        got = _xy(center)
        if got is not None:
            return got, "given"
        pts: List[Tuple[int, float, float]] = []
        for r in self.labels_dict(packet_id).values():
            if not r.get("visible") or r.get("pano_x") is None or r.get("frame_idx") is None:
                continue
            fi = int(r["frame_idx"])
            if fi >= frame_idx:
                continue
            rx, ry = prep.to_roi(float(r["pano_x"]), float(r["pano_y"]))
            pts.append((fi, rx, ry))
        pts.sort()
        if pts:
            pr = T.predict_next(pts[-6:], frame_idx, step=int(meta.get("step") or 1))
            if pr:
                return (pr[0], pr[1]), "predict"
            return (pts[-1][1], pts[-1][2]), "last_visible"
        w, h = prep.size
        return (w / 2.0, h / 2.0), "roi_center"

    def render_scan(self, packet_id: str, kind: str = "time", max_side: int = 1400) -> dict:
        """产包端 Scan 图：时间彩色或灰度最大投影，带粗网格，用来挑锚点帧。"""
        if not is_ready(self.work_root, packet_id) and not is_raw_ready(self.work_root, packet_id):
            job = self.job_state(packet_id)
            return {"error": f"{packet_id} 还没准备好（{job['state']}: {job['msg']}）"}
        kind = "dog" if str(kind).lower() in ("dog", "gray", "scan_dog") else "time"
        prep = self.prep(packet_id) if is_ready(self.work_root, packet_id) else load_prepared(self.work_root, packet_id)
        src = prep.root / ("scan_dog.jpg" if kind == "dog" else "scan_time.jpg")
        if not src.exists():
            return {"error": "还没有 scan 图（等 ball_status ready，或这是旧 zip 包）"}
        import cv2
        img = cv2.imread(str(src), cv2.IMREAD_COLOR if kind == "time" else cv2.IMREAD_GRAYSCALE)
        if img is None:
            return {"error": f"读不了 {src.name}"}
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        h, w = img.shape[:2]
        if max(w, h) > max_side:
            sc = max_side / max(w, h)
            img = cv2.resize(img, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
            h, w = img.shape[:2]
        cols, rows = 12, 6
        cw, rh = w / cols, h / rows
        for i in range(1, cols):
            x = int(round(i * cw))
            cv2.line(img, (x, 0), (x, h), (80, 80, 80), 1)
        for j in range(1, rows):
            y = int(round(j * rh))
            cv2.line(img, (0, y), (w, y), (80, 80, 80), 1)
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for j in range(rows):
            for i in range(cols):
                cv2.putText(img, f"{letters[i]}{j+1}", (int(i * cw) + 4, int(j * rh) + 16),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (180, 180, 180), 1, cv2.LINE_AA)
        view_id = self._view_id("scan", 0, [kind, max_side])
        path = self._write_png(packet_id, view_id, 0, img)
        roi = list(prep.roi)
        return {
            "ok": True,
            "packet_id": packet_id,
            "kind": kind,
            "view_id": view_id,
            "image_paths": [str(path)],
            "size": [w, h],
            "roi": roi,
            "cols": cols,
            "rows": rows,
            "note": (
                "时间彩色：早=蓝、晚=红，白线是球轨迹。先读走向再 ball_frames 挑 2 个锚点帧，"
                "进 crop 480→80→45。格子只帮你描述位置，不能 ball_label。"
            ),
        }

    def render_view(
        self,
        packet_id: str,
        frame_idx: int,
        mode: str = "crop",
        center: Any = None,
        size: Optional[float] = None,
        cols: Optional[int] = None,
        rows: Optional[int] = None,
        pane: Optional[str] = None,
    ) -> dict:
        """出一张（或三张）带网格的图，写到 `{work}/{pid}/views/`。

        `mode="global"`：ROI 横切三段、每段 1:1 不缩小、raw+enh 竖叠，三段共用一套
        连续字母。整幅拼版找球实测 0%，所以全局只用来定个大概，别指望在这一级点准。

        `mode="crop"`：正方形窗放大，共用一套格子。`pane` 选看哪几张图。

        `mode="time_grid"`：同一小块画面的连续 9 帧拼 3×3，**没有格子**。用来确认
        「这里到底有没有球」：球逐格平移，灯光一动不动。单帧图上这两样分不开。
        """
        mode = str(mode or "crop").lower()
        if mode not in MODES:
            return {"error": f"mode 只能是 {' / '.join(MODES)}，收到 {mode!r}"}
        if not is_ready(self.work_root, packet_id):
            job = self.job_state(packet_id)
            return {"error": f"{packet_id} 还没准备好（{job['state']}: {job['msg']}）"}
        prep = self.prep(packet_id)
        try:
            frame_idx = int(frame_idx)
        except (TypeError, ValueError):
            return {"error": f"frame_idx 不是整数: {frame_idx!r}"}
        if not prep.has(frame_idx):
            return {"error": f"没有帧 {frame_idx}；帧号看 ball_frames 的 targets"}
        sess = self.load_session(packet_id)
        meta = self._meta_dict(packet_id, sess)

        if mode == "global":
            return self._render_global(prep, packet_id, frame_idx, cols, rows)
        panes = PANES_CROP
        if pane is not None:
            key = str(pane).lower()
            if key not in VIEW_PANE_SETS:
                return {"error": f"pane 只能是 {' / '.join(VIEW_PANE_SETS)}，收到 {pane!r}"}
            panes = VIEW_PANE_SETS[key]
        if mode == "time_grid":
            return self._render_time_grid(prep, packet_id, sess, meta, frame_idx, center, size,
                                          panes[0])
        return self._render_crop(prep, packet_id, sess, meta, frame_idx, center, size, cols, rows,
                                 panes)

    def _register(self, ref: ViewRef) -> None:
        """登记视图并**扔掉图本身**。

        图已经落盘，登记只需要几何。一包 100 帧 × 每帧 3–4 张图，每张 raw+enh
        拼起来几 MB，留在内存里就是几百 MB 的泄漏；`ball_view_image` 反正是从
        文件读的。
        """
        payload = {
            "view_id": ref.view_id,
            "packet_id": ref.packet_id,
            "frame_idx": ref.frame_idx,
            "mode": ref.mode,
            "paths": [str(p) for p in ref.paths],
            "geom": _geom_of(ref.view),
        }
        (self._views_dir(ref.packet_id) / f"{ref.view_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        ref.view = _view_from_geom(payload["geom"])
        ref.strips = []
        with self._view_lock:
            self._views[ref.view_id] = ref
            while len(self._views) > VIEW_REGISTRY_CAP:
                self._views.pop(next(iter(self._views)))

    def _view_id(self, mode: str, frame_idx: int, parts: Sequence[Any]) -> str:
        h = hashlib.md5(json.dumps(list(parts), ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:8]
        return f"{mode[0]}{frame_idx}-{h}"

    def _write_png(self, pid: str, view_id: str, index: int, img: np.ndarray) -> Path:
        p = self._views_dir(pid) / f"{view_id}_{index}.png"
        p.write_bytes(V.encode(img, "png"))
        return p

    def _render_global(self, prep, pid: str, frame_idx: int,
                       cols: Optional[int], rows: Optional[int]) -> dict:
        total_cols = int(cols or DEFAULT_GLOBAL_COLS)
        per = max(1, int(round(total_cols / GLOBAL_STRIPS)))
        n_rows = max(1, int(rows or DEFAULT_GLOBAL_ROWS))
        view_id = self._view_id("global", frame_idx, ["global", per, n_rows, list(PANES_GLOBAL)])
        master, strips = V.make_global_strips(
            prep, frame_idx, n_strips=GLOBAL_STRIPS, cols_per_strip=per,
            rows=n_rows, panes=PANES_GLOBAL,
        )
        paths = [self._write_png(pid, view_id, i, sv.image) for i, sv in enumerate(strips)]
        ref = ViewRef(
            view_id=view_id, packet_id=pid, frame_idx=frame_idx, mode="global",
            view=master, paths=paths, center=None, size=float(master.extent[2]),
            center_source="", panes=list(PANES_GLOBAL), strips=list(strips),
        )
        self._register(ref)
        names = ("左", "中", "右")
        images = []
        for i, sv in enumerate(strips):
            lets = sv.letters()
            images.append({
                "index": i,
                "path": str(paths[i]),
                "title": f"图{i + 1} = {names[i] if i < 3 else f'段{i + 1}'}段，列 {lets[0]}–{lets[-1]}",
                "letters": lets,
                "extent": [round(v, 1) for v in sv.extent],
            })
        roi_w, roi_h = prep.size
        return {
            "view_id": view_id,
            "packet_id": pid,
            "frame_idx": frame_idx,
            "mode": "global",
            "cols": master.cols,
            "rows": master.rows,
            "letters": master.letters(),
            "cell_w_px": round(master.cell_w, 1),
            "cell_h_px": round(master.cell_h, 1),
            "n_cells": master.cols * master.rows,
            "extent": [0.0, 0.0, float(roi_w), float(roi_h)],
            "center": None,
            "size": None,
            "center_source": "",
            "upscale": 1.0,
            "panes": list(PANES_GLOBAL),
            "image_paths": [str(p) for p in paths],
            "images": images,
            "hint": (
                f"三张图是同一帧 ROI 的左/中/右三段，1:1 未缩小，共用一套 {master.cols}×"
                f"{master.rows} 的格子（列 {master.letters()}）。上半是原图、下半是滤波图；"
                "飞行的球在滤波图里是孤立小白斑，球员是大块白人形。每格约 "
                f"{round(master.cell_w)}×{round(master.cell_h)} px，只能定个大概，"
                "拿到格子号后立刻用 mode='crop' 往下钻，别在这一级 ball_label。"
            ),
        }

    def _render_time_grid(self, prep, pid: str, sess: dict, meta: dict, frame_idx: int,
                          center: Any, size: Optional[float], pane: str) -> dict:
        """连续 9 帧的 3×3 时间拼版。**故意不带格子**，只回答「有没有球在动」。

        给了格线模型就会顺手报一个格子，而这张图的每一格是不同的**时刻**，
        在上面选格子没有任何坐标含义 —— 所以这个 mode 不能用来 ball_label。
        """
        try:
            win = float(size) if size is not None else DEFAULT_TIME_GRID_SIZE
        except (TypeError, ValueError):
            return {"error": f"size 不是数字: {size!r}"}
        if win <= 0:
            return {"error": "size 必须 > 0"}
        ctr, src = self._resolve_center(prep, pid, sess, meta, frame_idx, center)
        if ctr is None:
            return {"error": "算不出裁切中心，请显式给 center=[x, y]（ROI 像素）"}
        img, frames, labels = V.make_time_grid_view(
            prep, frame_idx, ctr, size=win, n=DEFAULT_TIME_GRID_TILES, pane=pane)
        view_id = self._view_id("time_grid", frame_idx,
                                ["tg", round(ctr[0], 1), round(ctr[1], 1), round(win, 1), pane])
        path = self._write_png(pid, view_id, 0, img)
        # 登记一个退化视图，只为了 ball_view_image 能按 view_id 把图取回来；
        # cols=rows=1 也在明示「这张图上没有可选的格子」。
        ref = ViewRef(
            view_id=view_id, packet_id=pid, frame_idx=frame_idx, mode="time_grid",
            view=V.GridView(image=img, cols=1, rows=1, origin=(ctr[0] - win / 2, ctr[1] - win / 2),
                            cell_w=win, cell_h=win, kind="time_grid", frame_idx=frame_idx,
                            panes=[pane],
                            extent=(ctr[0] - win / 2, ctr[1] - win / 2, win, win)),
            paths=[path], center=ctr, size=win, center_source=src, panes=[pane],
        )
        self._register(ref)
        return {
            "view_id": view_id,
            "packet_id": pid,
            "frame_idx": frame_idx,
            "mode": "time_grid",
            "selectable": False,
            "frames": [int(f) for f in frames],
            "tile_labels": labels,
            "center": [round(ctr[0], 1), round(ctr[1], 1)],
            "size": round(win, 1),
            "center_source": src,
            "pane": pane,
            "image_paths": [str(path)],
            "hint": (
                "连续 9 帧按时间排 3×3：左上最早，右下 t0 是当前帧（黄框）。"
                "球 = 小圆点在 9 格里逐格平移；灯光 / 边线交点 / 广告牌 = 9 格位置一动不动；"
                "球员的脚 = 大块竖长且和人体连着。**这张图没有格子，不能用来 ball_label**，"
                "确认有球之后回 mode='crop' 逐级放大点格子。"
            ),
        }

    def _render_crop(self, prep, pid: str, sess: dict, meta: dict, frame_idx: int,
                     center: Any, size: Optional[float],
                     cols: Optional[int], rows: Optional[int],
                     panes: Sequence[str] = PANES_CROP) -> dict:
        try:
            win = float(size) if size is not None else DEFAULT_CROP_SIZE
        except (TypeError, ValueError):
            return {"error": f"size 不是数字: {size!r}"}
        if win <= 0:
            return {"error": "size 必须 > 0"}
        n_cols = max(1, int(cols or DEFAULT_CROP_COLS))
        n_rows = max(1, int(rows or DEFAULT_CROP_ROWS))
        ctr, src = self._resolve_center(prep, pid, sess, meta, frame_idx, center)
        if ctr is None:
            return {"error": "算不出裁切中心，请显式给 center=[x, y]（ROI 像素）"}
        up = _auto_upscale(win)
        view_id = self._view_id(
            "crop", frame_idx,
            ["crop", round(ctr[0], 1), round(ctr[1], 1), round(win, 1), n_cols, n_rows, list(panes)],
        )
        view = V.make_crop_view(
            prep, frame_idx, ctr, win, n_cols, n_rows,
            upscale=up, resample="nearest", panes=panes,
        )
        path = self._write_png(pid, view_id, 0, view.image)
        ref = ViewRef(
            view_id=view_id, packet_id=pid, frame_idx=frame_idx, mode="crop",
            view=view, paths=[path], center=ctr, size=win,
            center_source=src, panes=list(panes),
        )
        self._register(ref)
        roi_w, roi_h = prep.size
        x0, y0, w, h = view.extent
        clipped = x0 < 0 or y0 < 0 or x0 + w > roi_w or y0 + h > roi_h
        return {
            "view_id": view_id,
            "packet_id": pid,
            "frame_idx": frame_idx,
            "mode": "crop",
            "cols": view.cols,
            "rows": view.rows,
            "letters": view.letters(),
            "cell_w_px": round(view.cell_w, 2),
            "cell_h_px": round(view.cell_h, 2),
            "n_cells": view.cols * view.rows,
            "extent": [round(v, 1) for v in view.extent],
            "center": [round(ctr[0], 1), round(ctr[1], 1)],
            "size": round(win, 1),
            "center_source": src,
            "upscale": round(view.upscale, 2),
            "panes": list(panes),
            "image_paths": [str(path)],
            "images": [{
                "index": 0,
                "path": str(path),
                "title": f"从左到右 {' | '.join(panes)}，同一套 {view.cols}×{view.rows} 格子",
                "letters": view.letters(),
                "extent": [round(v, 1) for v in view.extent],
            }],
            "out_of_roi": bool(clipped),
            "hint": (
                f"左右两个 pane 是同一块 {round(win)}px 窗的原图和滤波图，共用一套 "
                f"{view.cols}×{view.rows} 格子（列 {view.letters()}，行 1–{view.rows}），"
                f"每格 {round(view.cell_w, 1)} px。看得见球就回一个格子号（如 C3）："
                "窗还大就用这个格心当 center 再收一级（480→80→45），"
                "到 45px 那级再 ball_label。看不见就别猜——猜错比空缺更有害。"
                + ("（注意：窗超出 ROI，黑边部分不是画面）" if clipped else "")
            ),
        }

    # ------------------------------------------------------------ 坐标

    def get_view(self, view_id: str) -> Optional[ViewRef]:
        with self._view_lock:
            ref = self._views.get(view_id)
        if ref is not None:
            return ref
        # 进程重启后内存登记没了，从磁盘几何重建一个只能算坐标的
        for p in self.work_root.glob(f"*/views/{view_id}.json"):
            d = self._read_json(p)
            if not d.get("geom"):
                continue
            ref = ViewRef(
                view_id=view_id,
                packet_id=str(d.get("packet_id") or p.parents[1].name),
                frame_idx=int(d.get("frame_idx") or -1),
                mode=str(d.get("mode") or "crop"),
                view=_view_from_geom(d["geom"]),
                paths=[Path(x) for x in (d.get("paths") or [])],
            )
            with self._view_lock:
                self._views[view_id] = ref
            return ref
        return None

    def resolve_point(self, view_id: str, cell: str) -> dict:
        """格子号 → ROI 像素 → 全景像素。agent 的答案落地只走这一个入口。"""
        ref = self.get_view(view_id)
        if ref is None:
            return {"error": f"不认识 view_id={view_id!r}，先调 ball_view"}
        xy = ref.view.cell_center(str(cell))
        if xy is None:
            return {
                "error": f"格子名不合法或超出范围: {cell!r}；"
                         f"这张图是列 {ref.view.letters()} × 行 1–{ref.view.rows}"
            }
        if not is_ready(self.work_root, ref.packet_id):
            return {"error": f"{ref.packet_id} 还没准备好，算不了全景坐标"}
        prep = self.prep(ref.packet_id)
        roi_w, roi_h = prep.size
        if not (0 <= xy[0] < roi_w and 0 <= xy[1] < roi_h):
            return {
                "error": f"格子 {str(cell).upper()} 落在 ROI 之外（ROI 像素 "
                         f"{round(xy[0], 1)},{round(xy[1], 1)}，ROI 尺寸 {roi_w}×{roi_h}）。"
                         "那一块是补的黑边，不是画面，换个格子或挪一下 center。"
            }
        px, py = prep.to_pano(xy[0], xy[1])
        return {
            "view_id": view_id,
            "packet_id": ref.packet_id,
            "frame_idx": ref.frame_idx,
            "cell": str(cell).strip().upper(),
            "roi_xy": [round(xy[0], 2), round(xy[1], 2)],
            "pano_xy": [round(px, 2), round(py, 2)],
            "cell_px": [round(ref.view.cell_w, 2), round(ref.view.cell_h, 2)],
        }

    def view_image(self, view_id: str, index: int = 0) -> dict:
        """同一张图的 PNG 字节，给读不了本地文件的 agent 内联回去。"""
        ref = self.get_view(view_id)
        if ref is None:
            return {"error": f"不认识 view_id={view_id!r}，先调 ball_view"}
        try:
            index = int(index or 0)
        except (TypeError, ValueError):
            return {"error": f"index 不是整数: {index!r}"}
        if not ref.paths:
            return {"error": f"{view_id} 没有落盘的图（进程重启后只剩几何）"}
        if not (0 <= index < len(ref.paths)):
            return {"error": f"index 越界：这张视图有 {len(ref.paths)} 张图，index 只能 0–{len(ref.paths) - 1}"}
        p = Path(ref.paths[index])
        if not p.exists():
            return {"error": f"图文件不见了: {p}"}
        return {
            "view_id": view_id,
            "packet_id": ref.packet_id,
            "frame_idx": ref.frame_idx,
            "index": index,
            "n_images": len(ref.paths),
            "path": str(p),
            "mime": "image/png",
            "data": p.read_bytes(),
        }

    # ------------------------------------------------------------ 写标注

    def clear(self, packet_id: str, frame_idx: int) -> dict:
        labels = self.labels_dict(packet_id)
        key = str(int(frame_idx))
        had = labels.pop(key, None)
        self.write_labels(packet_id, labels)
        sess = self.load_session(packet_id)
        meta = self._meta_dict(packet_id, sess)
        prog = self._progress(packet_id, sess, meta)
        return {
            "ok": True,
            "packet_id": packet_id,
            "frame_idx": int(frame_idx),
            "cleared": had is not None,
            "record": None,
            "n_labeled": prog["n_labeled"],
            "n_targets": prog["n_targets"],
            "next_frame": prog["next_frame"],
            "message": f"帧 {frame_idx} 的标注已清除" if had else f"帧 {frame_idx} 本来就没标",
        }

    def label(
        self,
        packet_id: str,
        frame_idx: int,
        view_id: Optional[str] = None,
        cell: Optional[str] = None,
        roi_xy: Any = None,
        pano_xy: Any = None,
        visible: Optional[bool] = None,
        occluded: bool = False,
        blurred: bool = False,
        clear: bool = False,
    ) -> dict:
        """写一帧标注到 labels.json。坐标三种给法，优先级 cell > roi_xy > pano_xy。"""
        try:
            fi = int(frame_idx)
        except (TypeError, ValueError):
            return {"error": f"frame_idx 不是整数: {frame_idx!r}"}
        if clear:
            return self.clear(packet_id, fi)
        sess = self.load_session(packet_id)
        if sess.get("done"):
            return {"error": f"{packet_id} 这一包已经提交/释放过了，不能再改"}
        meta = self._meta_dict(packet_id, sess)

        cell_name: Optional[str] = None
        roi_pt: Optional[Tuple[float, float]] = None
        pano_pt: Optional[Tuple[float, float]] = None
        cell_in = None if cell is None else str(cell).strip()
        if cell_in and cell_in.lower() not in ("null", "none"):
            if not view_id:
                return {"error": "给了 cell 就必须给 view_id（格子号只在那张图的坐标系里有意义）"}
            got = self.resolve_point(view_id, cell_in)
            if got.get("error"):
                return got
            ref = self.get_view(view_id)
            if ref is not None and ref.frame_idx != fi:
                return {
                    "error": f"view_id 是帧 {ref.frame_idx} 的图，但要标的是帧 {fi}；"
                             "别把上一帧的格子号写到这一帧"
                }
            cell_name = got["cell"]
            roi_pt = (got["roi_xy"][0], got["roi_xy"][1])
            pano_pt = (got["pano_xy"][0], got["pano_xy"][1])
        elif _xy(roi_xy) is not None:
            if not is_ready(self.work_root, packet_id):
                return {"error": f"{packet_id} 还没准备好，roi_xy 换不成全景坐标"}
            roi_pt = _xy(roi_xy)
            prep = self.prep(packet_id)
            roi_w, roi_h = prep.size
            if not (0 <= roi_pt[0] < roi_w and 0 <= roi_pt[1] < roi_h):
                return {"error": f"roi_xy 超出 ROI（{roi_w}×{roi_h}）: {roi_pt}"}
            pano_pt = prep.to_pano(roi_pt[0], roi_pt[1])
        elif _xy(pano_xy) is not None:
            pano_pt = _xy(pano_xy)
            if is_ready(self.work_root, packet_id):
                roi_pt = self.prep(packet_id).to_roi(pano_pt[0], pano_pt[1])

        if pano_pt is None and visible is not False:
            return {
                "error": "要么给坐标（cell+view_id / roi_xy / pano_xy），"
                         "要么明确 visible=false 表示这一帧看不见球。不许猜坐标。"
            }
        vis = bool(pano_pt is not None) if visible is None else bool(visible)
        if vis and pano_pt is None:
            return {"error": "visible=true 就必须给坐标"}
        if not vis:
            pano_pt = None
            roi_pt = None
            cell_name = None

        rec = {
            "frame_idx": fi,
            "visible": vis,
            "pano_x": None if pano_pt is None else round(float(pano_pt[0]), 2),
            "pano_y": None if pano_pt is None else round(float(pano_pt[1]), 2),
            "occluded": bool(occluded),
            "blurred": bool(blurred),
            "source": "agent",
        }
        if cell_name:
            rec["cell"] = cell_name
        labels = self.labels_dict(packet_id)
        labels[str(fi)] = rec
        self.write_labels(packet_id, labels)
        prog = self._progress(packet_id, sess, meta)
        return {
            "ok": True,
            "packet_id": packet_id,
            "frame_idx": fi,
            "record": rec,
            "roi_xy": None if roi_pt is None else [round(roi_pt[0], 2), round(roi_pt[1], 2)],
            "pano_xy": None if pano_pt is None else [rec["pano_x"], rec["pano_y"]],
            "cell": cell_name,
            "n_labeled": prog["n_labeled"],
            "n_targets": prog["n_targets"],
            "n_missing": prog["n_missing"],
            "next_frame": prog["next_frame"],
            "message": (
                f"帧 {fi} 已记{'（可见 ' + str(rec['pano_x']) + ',' + str(rec['pano_y']) + '）' if vis else '（看不见）'}"
                f"，还剩 {prog['n_missing']} 个目标帧"
            ),
        }

    # ------------------------------------------------------------ 收口

    def submit(self, packet_id: str, force: bool = False) -> dict:
        """`build_result` + 审核包 + `submit(source="human")`。缺目标帧默认拒绝。

        仍直接写 `gt`（免等审核员），但审核页可以再打开打回。审核包拼失败不挡提交。
        """
        sess = self.load_session(packet_id)
        if not sess:
            return {"error": f"没有 {packet_id} 的会话，先用 ball_lease 领包"}
        if sess.get("done"):
            return {"error": f"{packet_id} 已经提交或释放过了"}
        if not is_ready(self.work_root, packet_id):
            job = self.job_state(packet_id)
            return {"error": f"{packet_id} 还没准备好（{job['state']}: {job['msg']}）"}
        prep = self.prep(packet_id)
        meta = self._meta_dict(packet_id, sess)
        prog = self._progress(packet_id, sess, meta)
        labels = prog["labels"]
        missing = prog["missing"]
        if missing and not force:
            return {
                "error": f"还有 {len(missing)} 个目标帧没标，拒绝提交（真要交残包传 force=true）",
                "packet_id": packet_id,
                "n_missing": len(missing),
                "missing": missing[:20],
                "next_frame": missing[0],
            }
        n_vis = int(prog.get("n_visible") or 0)
        if n_vis == 0 and not force:
            return {
                "error": (
                    "整包 0 帧可见，拒绝免审入库（真没有球就 ball_release，"
                    "不要交一包全不可见的 GT）"
                ),
                "packet_id": packet_id,
                "n_visible": 0,
                "n_labeled": len(labels),
            }
        worker = sess.get("worker") or self.worker or "agent"
        started = float(sess.get("started_at") or 0.0)
        elapsed = round(time.time() - started, 2) if started else None
        result = build_result(prep.meta, worker, labels, {"elapsed_sec": elapsed})
        result["source"] = "human"
        try:
            resp = self.api.submit(
                result, None,
                source="human",
                lease_id=sess.get("lease_id"),
                worker=worker,
            )
        except Exception as e:  # noqa: BLE001
            return {"error": f"提交失败: {type(e).__name__}: {e}"}
        sess["done"] = True
        sess["submitted_at"] = time.time()
        self.save_session(packet_id, sess)
        return {
            "ok": True,
            "packet_id": packet_id,
            "n_labeled": len(labels),
            "n_visible": prog["n_visible"],
            "n_missing": len(missing),
            "forced": bool(missing and force),
            "elapsed_sec": elapsed,
            "submission_id": (resp or {}).get("submission_id"),
            "has_bundle": False,
            "message": f"已提交（{len(labels)} 帧，source=human 入库）",
        }

    def release(self, packet_id: str, reason: str = "released") -> dict:
        """放弃这一包换下一个。labels.json 留着，下次领到同一包能接着用。"""
        sess = self.load_session(packet_id)
        if not sess:
            return {"error": f"没有 {packet_id} 的会话"}
        lease_id = sess.get("lease_id")
        resp: dict = {}
        if lease_id and not sess.get("done"):
            resp = self.api.release(lease_id, str(reason or "released")) or {}
        sess["done"] = True
        sess["released_reason"] = str(reason or "released")
        self.save_session(packet_id, sess)
        return {
            "ok": True,
            "packet_id": packet_id,
            "lease_id": lease_id,
            "reason": str(reason or "released"),
            "server": resp,
            "message": f"{packet_id} 已释放（{reason}），可以再 ball_lease 领新包",
        }
