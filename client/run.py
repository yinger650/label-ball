#!/usr/bin/env python3
"""AI 客户端主循环：lease → 下载 artifacts → prep → workflow(seed) → track → submit。

冷启动没有种子就**不硬跑**（L0 montage 实测 0% 命中）：release 掉并打日志，
让人工站先标头两帧（或 `--seed-gt` / 服务端已有 GT），下一轮再续跑。

    venv/bin/python3 -m client.run --server http://HOST:8780 --worker ai-1
    venv/bin/python3 -m client.run --once --seed-gt /path/to/gt.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client.api import Lease, ServerClient  # noqa: E402
from client.prep import pano_to_roi, prepare_packet, workspace  # noqa: E402

log = logging.getLogger("client.run")

DEFAULT_WORK = Path(os.environ.get("BALL_AI_WORK") or (Path.home() / ".cache" / "ball_ai" / "work"))
MIN_SEED = 2
# 一包里至少要有这个比例的目标帧真的定出了坐标，否则整包退回去让人标两帧。
#
# 剩下那点观测撑不起一条轨迹：`track.interpolate` 只能补短空洞，长空洞会被它原样
# 留空或者用一条离真实轨迹很远的直线连过去。提交这样一包的代价不只是数据没用 ——
# 审核的人得逐帧改一遍，比从头标还慢。而 `no_seed` release 之后人只需要标两帧。
#
# 0.45 是量出来的，不是拍的。它同时是**包级的精度闸门** —— 答出的帧少，说明本地
# 核验一直不同意，那这一包剩下答出来的点通常也不对。标定 v2_final（3 包 54 帧，
# `verify.gate: 0.6`）：
#
#   包            答出比例   precision@20px
#   260116          61%          100%
#   260108          39%           57%     ← 0.35 会放它过去
#   260122          33%           33%
#
# 卡 0.35 时提交出去的数据 precision 是 15/18 = 83%；卡 0.45 只提交第一包，
# 100%。代价是第二、三包退回人工 —— 这正是「宁缺勿错」该付的代价。
MIN_FOUND_RATIO = 0.45


# ---------------------------------------------------------------- 种子


def iter_label_rows(doc: Any) -> List[dict]:
    """容忍三种形态：result 文档 / {frame: rec} / 裸 list。"""
    if doc is None:
        return []
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)]
    if not isinstance(doc, dict):
        return []
    if isinstance(doc.get("labels"), list):
        return [r for r in doc["labels"] if isinstance(r, dict)]
    if isinstance(doc.get("labels"), dict):
        return [r for r in doc["labels"].values() if isinstance(r, dict)]
    rows = []
    for k, v in doc.items():
        if isinstance(v, dict) and (v.get("frame_idx") is not None or str(k).lstrip("-").isdigit()):
            rec = dict(v)
            rec.setdefault("frame_idx", int(k) if str(k).lstrip("-").isdigit() else v.get("frame_idx"))
            rows.append(rec)
    return rows


def load_label_doc(path: Path) -> Any:
    """读一份 GT / labels JSON。目录或缺失返回 None，不要把目录当文件读。"""
    if not path:
        return None
    p = Path(path)
    if not p.exists() or p.is_dir():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def visible_seed_points(
    rows: Sequence[dict],
    roi: Sequence[int],
    pano_w: int,
) -> List[Tuple[int, float, float]]:
    """可见且有坐标的点 → `(frame_idx, roi_x, roi_y)`，按帧号排序去重。"""
    by_f: Dict[int, Tuple[int, float, float]] = {}
    for r in rows:
        if r.get("visible") is False:
            continue
        fi = r.get("frame_idx")
        if fi is None:
            continue
        try:
            fi = int(fi)
        except (TypeError, ValueError):
            continue
        if r.get("pano_x") is not None and r.get("pano_y") is not None:
            x, y = pano_to_roi(float(r["pano_x"]), float(r["pano_y"]), list(roi), int(pano_w))
        elif r.get("x") is not None and r.get("y") is not None:
            x, y = float(r["x"]), float(r["y"])
        else:
            continue
        by_f[fi] = (fi, x, y)
    return [by_f[k] for k in sorted(by_f)]


def local_labels_path(work_root: Path, packet_id: str) -> Path:
    return workspace(work_root, packet_id) / "labels.json"


def resolve_seed(
    *,
    work_root: Path,
    packet_id: str,
    lease: Lease,
    api: Optional[ServerClient] = None,
    seed_gt: Optional[Path] = None,
    roi: Optional[Sequence[int]] = None,
    pano_w: Optional[int] = None,
) -> Tuple[List[Tuple[int, float, float]], str]:
    """按约定顺序找 ≥2 个可见点。返回 (seed, source)。找不到则 seed 为空。

    1. `--seed-gt`（调试覆盖）
    2. 本机工作区 `labels.json`
    3. lease 带回的 prior / gt
    4. `GET /api/v2/gt/<packet_id>`
    """
    meta = lease.meta or {}
    roi = list(roi if roi is not None else (meta.get("roi") or [0, 0, 1, 1]))
    pano_w = int(pano_w if pano_w is not None else (meta.get("pano_size") or [1])[0])

    def take(rows: Sequence[dict], why: str) -> Optional[Tuple[List[Tuple[int, float, float]], str]]:
        pts = visible_seed_points(rows, roi, pano_w)
        if len(pts) >= MIN_SEED:
            return pts, why
        return None

    if seed_gt:
        p = Path(seed_gt)
        if p.is_dir():
            hits = sorted(p.glob(f"{packet_id}__*.json")) + sorted(p.glob(f"{packet_id}.json"))
            if not hits:
                log.info("%s --seed-gt 目录里没有匹配文件，改走其它种子来源", packet_id)
                p = None
            else:
                p = hits[0]
        if p is not None:
            doc = load_label_doc(p)
            got = take(iter_label_rows(doc), f"seed-gt:{p}")
            if got:
                return got

    local = load_label_doc(local_labels_path(work_root, packet_id))
    got = take(iter_label_rows(local), "local-labels")
    if got:
        return got

    got = take(lease.prior_labels, "lease-prior")
    if got:
        return got

    if api is not None:
        got = take(api.get_gt(packet_id), "server-gt")
        if got:
            return got

    return [], ""


# ---------------------------------------------------------------- 结果


def points_to_labels(prep, points) -> List[dict]:
    rows = []
    for p in points:
        px = py = None
        if p.x is not None and p.y is not None:
            px, py = prep.to_pano(p.x, p.y)
        vis = bool(p.visible) and px is not None
        rows.append({
            "frame_idx": int(p.frame_idx),
            "visible": vis,
            "pano_x": px,
            "pano_y": py,
            "occluded": bool(p.occluded),
            "blurred": bool(p.blurred),
            "conf": float(p.conf or 0.0),
            "interp": bool(p.interp),
            "outlier": bool(p.outlier),
            "stages": list(p.stages or []),
        })
    return rows


def build_ai_result(prep, labels: List[dict], *, worker: str, workflow: str,
                    model_used: str, elapsed: float, n_vlm_calls: int,
                    seed_source: str, summary: dict) -> dict:
    vis = [r for r in labels if r.get("visible")]
    return {
        "version": "1.0",
        "kind": "ball_gt",
        "packet_id": prep.packet_id,
        "group": prep.meta.group,
        "stem": prep.meta.stem,
        "tag": prep.meta.tag,
        "annotator": worker,
        "source": "ai",
        "workflow": workflow,
        "model_used": model_used,
        "fps": prep.meta.fps,
        "pano_size": list(prep.meta.pano_size),
        "roi": list(prep.roi),
        "frame_start": prep.meta.frame_start,
        "frame_end": prep.meta.frame_end,
        "step": prep.meta.step,
        "n_targets": prep.meta.n_targets,
        "n_labeled": len(labels),
        "n_visible": len(vis),
        "labels": labels,
        "elapsed_sec": round(elapsed, 2),
        "n_vlm_calls": n_vlm_calls,
        "seed_source": seed_source,
        "summary": summary,
    }


# ---------------------------------------------------------------- 编排


FetchFn = Callable[..., Path]
WorkflowFn = Callable[..., list]
PrepareFn = Callable[..., Any]


@dataclass
class RunConfig:
    api: ServerClient
    work_root: Path = field(default_factory=lambda: DEFAULT_WORK)
    worker: str = "ai"
    kind: str = "ai"
    workflow: str = "erp_ball_v2"
    seed_gt: Optional[Path] = None
    allow_cold_start: bool = False
    keep_work: bool = False
    hb_interval: float = 60.0
    fetch_fn: Optional[FetchFn] = None
    store: Any = None
    prepare_fn: Optional[PrepareFn] = None
    workflow_fn: Optional[WorkflowFn] = None
    track_fn: Optional[Callable] = None
    bundle_fn: Optional[Callable] = None  # 已废弃，忽略
    cleanup: bool = True


def _default_workflow(prep, seed, frames=None, **kw):
    from client.vlm import VlmPool
    from client.workflow import Workflow

    pool = kw.get("pool") or VlmPool()
    wf = Workflow.load(kw.get("workflow") or "erp_ball_v2", pool)
    return wf.run_packet(prep, frames=frames, seed=seed), pool, wf.name


def process_lease(cfg: RunConfig, lease: Lease) -> str:
    """处理一个已领到的包。返回 ok / no_seed / error。失败由调用方 release。"""
    work_root = Path(cfg.work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    pid = lease.packet_id
    hb = cfg.api.start_heartbeat(lease.lease_id, cfg.hb_interval)
    t0 = time.time()
    try:
        seed, seed_src = resolve_seed(
            work_root=work_root, packet_id=pid, lease=lease,
            api=cfg.api, seed_gt=cfg.seed_gt,
        )
        if len(seed) < MIN_SEED:
            if not cfg.allow_cold_start:
                reason = (
                    f"no_seed: 冷启动没有 ≥{MIN_SEED} 个可见点 "
                    f"(local labels / lease prior / server gt / --seed-gt 都没有)。"
                    f"已 release，请用人工站标头两帧后再让 AI 续跑。"
                )
                log.warning("%s %s", pid, reason)
                cfg.api.release(lease.lease_id, "no_seed")
                return "no_seed"
            log.warning(
                "%s 无种子，--allow-cold-start 继续（走 propose/refine1，精度会差）",
                pid,
            )
            seed_src = "cold-start"

        log.info("%s 种子 %d 个点，来源 %s", pid, len(seed), seed_src)
        hb.set_stage("download")
        dest_dir = workspace(work_root, pid)
        dest_dir.mkdir(parents=True, exist_ok=True)
        fetch = cfg.fetch_fn
        if fetch is None:
            from client.fetch import ensure_packet_files, open_store
            from packet import ALL_ARTIFACT_FILES, STAGE1_FILES, STAGE2_FILES
            store = cfg.store or open_store()
            fetch = lambda **kw: ensure_packet_files(  # noqa: E731
                store, kw["aoss_key"], dest_dir, pid, which=ALL_ARTIFACT_FILES,
            )
            fetch(aoss_key=lease.aoss_key, dest=dest_dir)
        else:
            fetch(aoss_key=lease.aoss_key, dest=dest_dir / "packet.zip")

        hb.set_stage("prep")
        prep_fn = cfg.prepare_fn or prepare_packet
        src = dest_dir if (dest_dir / "clip.mp4").exists() or (dest_dir / "meta.json").exists() else dest_dir / "packet.zip"
        prep = prep_fn(src, work_root, packet_id=pid)

        hb.set_stage("workflow")
        frames = lease.target_frames or prep.targets
        if cfg.workflow_fn is not None:
            runs = cfg.workflow_fn(prep, seed=seed, frames=frames)
            pool = None
            wf_name = cfg.workflow
        else:
            runs, pool, wf_name = _default_workflow(
                prep, seed, frames=frames, workflow=cfg.workflow,
            )

        from client.workflow import summarize, to_obs
        from client import track as T

        n_found = sum(1 for r in runs if getattr(r, "x", None) is not None)
        if runs and n_found < MIN_FOUND_RATIO * len(runs):
            dropped = sum(1 for r in runs if getattr(r, "drop_reason", ""))
            reason = (f"no_seed: 只在 {n_found}/{len(runs)} 帧上定出坐标"
                      f"（低于 {MIN_FOUND_RATIO:.0%}，其中 {dropped} 帧被本地核验弃掉）。"
                      f"已 release，请用人工站标头两帧后再让 AI 续跑。")
            log.warning("%s %s", pid, reason)
            cfg.api.release(lease.lease_id, "no_seed")
            return "no_seed"

        hb.set_stage("track")
        track_fn = cfg.track_fn or T.process
        fps = float(prep.meta.fps) / max(int(prep.meta.step or 1), 1)
        points, stats = track_fn(to_obs(runs), fps=fps)

        model_used = ""
        if pool is not None:
            avail = pool.available()
            model_used = avail[0] if avail else ""
        from dataclasses import asdict

        labels = points_to_labels(prep, points)
        n_calls = sum(getattr(r, "n_calls", 0) for r in runs)
        result = build_ai_result(
            prep, labels, worker=cfg.worker, workflow=wf_name,
            model_used=model_used, elapsed=time.time() - t0,
            n_vlm_calls=n_calls, seed_source=seed_src,
            summary={**summarize(runs), "track": asdict(stats) if hasattr(stats, "__dataclass_fields__") else dict(stats.__dict__)},
        )
        # 工作副本留给人工站 / 下一轮续跑对照
        local_labels_path(work_root, pid).write_text(
            json.dumps({str(r["frame_idx"]): r for r in labels}, ensure_ascii=False),
            encoding="utf-8",
        )

        hb.set_stage("submit")
        resp = cfg.api.submit(
            result, None,
            source="ai", lease_id=lease.lease_id, worker=cfg.worker,
            workflow=wf_name, model_used=model_used,
        )
        log.info("%s 已提交 submission_id=%s n_labeled=%s",
                 pid, (resp or {}).get("submission_id"), len(labels))

        if cfg.cleanup and not cfg.keep_work:
            # 解码帧很大，提交成功就清；labels 已经进服务端
            shutil.rmtree(workspace(work_root, pid), ignore_errors=True)
        return "ok"
    finally:
        hb.stop()


def run_loop(cfg: RunConfig, *, once: bool = False, max_packets: int = 0,
             idle_sec: float = 15.0) -> int:
    """主循环。返回处理成功的包数。失败自动 release 并换包。"""
    n_ok = 0
    n_seen = 0
    while True:
        if max_packets and n_seen >= max_packets:
            break
        try:
            lease = cfg.api.lease(cfg.worker, cfg.kind)
        except Exception as e:  # noqa: BLE001
            log.error("lease 失败: %s", e)
            if once:
                break
            time.sleep(idle_sec)
            continue
        if lease is None:
            log.info("池子暂时没有适合的包")
            if once:
                break
            time.sleep(idle_sec)
            continue
        n_seen += 1
        log.info("领到 %s kind=%s lease=%s", lease.packet_id, lease.kind, lease.lease_id)
        try:
            status = process_lease(cfg, lease)
        except Exception as e:  # noqa: BLE001
            log.exception("处理 %s 失败，release 换包: %s", lease.packet_id, e)
            cfg.api.release(lease.lease_id, f"error: {type(e).__name__}: {e}")
            status = "error"
        if status == "ok":
            n_ok += 1
        if once:
            break
        # no_seed / 处理失败后必须歇一下：否则会立刻再领到同一个最老的 available 包，
        # 把服务端租约表打爆（曾经到过每秒十几次）。
        if status in ("no_seed", "error"):
            log.info("%s 状态 %s，%ss 后再领，避免空转", lease.packet_id, status, idle_sec)
            time.sleep(idle_sec)
    return n_ok


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AI 标注客户端主循环")
    p.add_argument("--server", required=True, help="服务端根 URL，例如 http://host:8080")
    p.add_argument("--worker", default=os.environ.get("BALL_GT_ANNOTATOR") or "ai")
    p.add_argument("--kind", default="ai", choices=("ai", "human", "repair"))
    p.add_argument("--once", action="store_true", help="只领一个包（或空池）就退出")
    p.add_argument("--max-packets", type=int, default=0, help="最多处理几个包，0=不限")
    p.add_argument("--seed-gt", type=Path, default=None,
                   help="调试：用这份 GT/result JSON 当种子（文件或目录）")
    p.add_argument(
        "--allow-cold-start",
        action="store_true",
        help="没有 ≥2 个可见点种子时仍继续（走本地 propose）。默认会 release。",
    )
    p.add_argument("--work", type=Path, default=DEFAULT_WORK)
    p.add_argument("--workflow", default="erp_ball_v2")
    p.add_argument("--token", default="", help="BALL_GT_API_TOKEN，默认读环境 / .env")
    p.add_argument("--idle", type=float, default=15.0, help="池空时等待秒数")
    p.add_argument("--keep-work", action="store_true", help="提交后保留本地工作区")
    p.add_argument("--hb-interval", type=float, default=60.0)
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        from client.vlm import load_dotenv
        load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    a = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    api = ServerClient(a.server, token=a.token, worker=a.worker)
    cfg = RunConfig(
        api=api,
        work_root=a.work,
        worker=a.worker,
        kind=a.kind,
        workflow=a.workflow,
        seed_gt=a.seed_gt,
        allow_cold_start=a.allow_cold_start,
        keep_work=a.keep_work,
        hb_interval=a.hb_interval,
    )
    n = run_loop(cfg, once=a.once, max_packets=a.max_packets, idle_sec=a.idle)
    log.info("结束，成功 %d 包", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
