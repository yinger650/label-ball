#!/usr/bin/env python3
"""生产端：持续随机抽取 Studio 里的视频，切出 ERP 球场 ROI 片段，打包上传到标注任务池。

设计要点：

**帧号必须能对回原视频**。GT 的 `frame_idx` 用的是源视频帧号，所以切片不能靠「大概
seek 到那个时间」。这里用 `ffmpeg -copyts -ss T`，再从输出的 `stream.start_time`
反推源帧号——实测（12345 / 27000 / 41111 三个起点）pts 反推值、请求值、与源帧逐像素
最佳匹配三者完全一致。

**「活跃」窗怎么挑**：直接用已有的 broadcast 相机轨迹（`*trajectory_cache*.json` 里的
`trajectory[].yaw`）。导播相机本来就在追球，偏航角速率大 = 球在大范围移动，这是零成本
且相当准的代理。剩下一半窗口纯随机抽，用来覆盖静止球、界外球、定位球——用户明确要求
运动和静止数据都要有。

**不重复**：已产出的包（对象存储上的 packets/）和已有 GT 窗口都算「用过」，同一路视频的
新窗口还必须与用过的窗口不重叠。

用法（本仓库根目录）:
  export BALL_GT_DATA_ROOT=/mnt/afs/wangmin/data
  export BALL_GT_CREDENTIALS=./credentials.json
  python producer/make_packets.py --n-packets 20

集群提交见 producer/submit.sh 与 docs/sensecore.md。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aoss_store import Store, load_credentials  # noqa: E402
from packet import (  # noqa: E402
    CLIP_FPS,
    PacketMeta,
    choose_decim,
    make_packet_id,
    packet_prefix,
    parse_packet_id,
)
from producer.derive import decimate_clip, derive_from_clip  # noqa: E402
from producer.server_api import ServerAPI, resolve_job_id, resolve_node  # noqa: E402


DATA_ROOT = Path(os.environ.get("BALL_GT_DATA_ROOT", "/mnt/afs/wangmin/data"))
ACTIVITY_CACHE = Path(
    os.environ.get(
        "BALL_GT_ACTIVITY_CACHE",
        str(DATA_ROOT / "_research_ball_det" / "ball_gt" / "activity"),
    )
)


def _even(v: int) -> int:
    return v - (v % 2)


def field_arc_x(xs: List[float], pano_w: int) -> Tuple[float, float]:
    """球场在 ERP 横向上占据的**最小圆弧**，返回 (起点 x, 弧长)。

    不能直接取 `max(x) - min(x)`：实测 100 路视频里有 46 路的球场跨过全景接缝
    （x=0/pano_w），此时包围盒宽达全景的 79%（约 6100 px）而真实弧长只有 48%
    （约 3700 px），中间那一大段是球场背面的场外街景。更糟的是球穿过接缝时会从
    ROI 的一边跳到另一边，视野跟随会彻底失效。

    最小圆弧 = 最大圆周间隙的补集。
    """
    s = sorted(xs)
    best_gap, best_i = -1.0, 0
    for i in range(len(s)):
        gap = (s[(i + 1) % len(s)] - s[i]) % pano_w if i == len(s) - 1 else s[i + 1] - s[i]
        if gap > best_gap:
            best_gap, best_i = gap, i
    return s[(best_i + 1) % len(s)], pano_w - best_gap


def compute_roi_wrapped(
    field_json: dict, pano_w: int, pano_h: int, margin_x: int, margin_top: int, margin_bottom: int
) -> Tuple[int, int, int, int]:
    """球场 ROI，允许横向跨接缝。

    返回 `(x0, y0, w, h)`，其中 **x0 + w 可能超过 pano_w**，表示裁切窗绕过接缝；
    ROI 内的局部 x 与全景 x 的关系统一是 `pano_x = (x0 + local_x) % pano_w`。

    旧版若不处理接缝，跨 x=0 的球场会把场外街景整片圈进 ROI；新数据一律走本函数。
    """
    corners = field_json.get("field_corners_pano") or []
    assert corners, "field_corners_pano missing"
    xs = [float(p[0]) for p in corners]
    ys = [float(p[1]) for p in corners]

    start, arc = field_arc_x(xs, pano_w)
    x0 = _even(int(round(start - margin_x)) % pano_w)
    w = min(_even(pano_w), _even(int(round(arc + 2 * margin_x))))
    y0 = _even(max(0, int(min(ys)) - margin_top))
    y1 = min(pano_h, int(max(ys)) + margin_bottom)
    # 球场角点在 ERP 里往往只占几十像素高（远端相机俯视），但飞球会远高于角点、
    # 近端球又远低于角点。仅靠角点+小 margin 会把这两类球裁掉。
    h = _even(y1 - y0)
    # 角点跨度往往只有几十像素；飞球可远高于角点、近端球远低于角点。
    # 至少覆盖约 42% 全景高，避免「只看见球场腰线」导致天上/近端球标不到。
    min_h = _even(max(1600, int(pano_h * 0.42)))
    if h < min_h:
        extra = min_h - h
        # 略偏向上扩展（飞球更常出画）
        up = (extra * 3) // 5
        y0 = _even(max(0, y0 - up))
        y1 = min(pano_h, y0 + min_h)
        if y1 - y0 < min_h:  # 贴底时向上补
            y0 = _even(max(0, y1 - min_h))
    return x0, y0, w, _even(y1 - y0)


# ---------------------------------------------------------------- 视频发现


def is_archived(group_dir: Path) -> bool:
    """归档后本地 ERP mp4 会被删除，但 config/ 还在，所以必须看状态而不是看目录存在。"""
    p = group_dir / ".pipeline_state.json"
    if not p.exists():
        return False
    try:
        st = json.loads(p.read_text())
    except Exception:
        return False
    arc = st.get("archive") or {}
    return str(arc.get("state") or "").upper() == "ARCHIVED"


def discover_videos(data_root: Path) -> List[dict]:
    """扫出所有「本地还能解码、且已标定」的 ERP 视频。"""
    out: List[dict] = []
    for group_dir in sorted(p for p in data_root.iterdir() if p.is_dir()):
        group = group_dir.name
        if group.startswith("_") or group.startswith(".") or group.startswith("test_"):
            continue
        if is_archived(group_dir):
            continue
        out_dir, cfg_dir = group_dir / "output", group_dir / "config"
        if not out_dir.is_dir():
            continue
        for mp4 in sorted(out_dir.glob("*.mp4")):
            if "_demo" in mp4.name or "_broadcast" in mp4.name:
                continue
            stem = mp4.stem
            field = cfg_dir / f"{stem}.mp4_field.json"
            if not field.exists() or mp4.stat().st_size < 1024:
                continue
            try:
                ann = json.loads(field.read_text())
            except Exception:
                continue
            if not ann.get("sphere_transform") or not ann.get("field_corners_pano"):
                continue
            out.append({"group": group, "stem": stem, "mp4": mp4, "field": field, "ann": ann})
    return out


def probe(video: Path) -> Tuple[float, float]:
    """返回 (fps, duration_sec)。"""
    r = subprocess.run(
        ["ffprobe", "-v", "0", "-select_streams", "v",
         "-show_entries", "stream=r_frame_rate:format=duration", "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    )
    d = json.loads(r.stdout)
    rate = d["streams"][0]["r_frame_rate"]
    num, den = rate.split("/")
    fps = float(num) / float(den)
    dur = float(d["format"]["duration"])
    return fps, dur


# ---------------------------------------------------------------- 活跃度


def activity_profile(group: str, stem: str, out_dir: Path, fps: float) -> Optional[List[float]]:
    """每秒的相机偏航角速率（度/秒），作为「球在大范围移动」的代理。

    结果缓存成小文件：trajectory_cache 有 80–90 MB，解析一次要十几秒，
    而同一路视频可能被抽中多次。

    缓存的读写失败一律吞掉：它只是优化，集群容器的 uid 不一定有共享盘的写权限，
    不该因为写不了缓存就让整个生产任务挂掉。
    """
    cache = ACTIVITY_CACHE / f"{group}__{stem}.json"
    try:
        ACTIVITY_CACHE.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            return json.loads(cache.read_text())["yaw_rate_per_sec"]
    except Exception:
        pass

    cands = sorted(out_dir.glob(f"{stem}*trajectory_cache*.json"))
    if not cands:
        return None
    try:
        traj = json.loads(cands[0].read_text()).get("trajectory") or []
    except Exception:
        return None
    if len(traj) < int(fps * 5):
        return None

    yaws = [float(t.get("yaw", 0.0)) for t in traj]
    per_sec: List[float] = []
    n = int(round(fps))
    for s in range(0, len(yaws) // n):
        seg = yaws[s * n : (s + 1) * n + 1]
        if len(seg) < 2:
            break
        # 解缠绕：导播机位跨 ±180° 时原始差值会出现假跳变
        d = 0.0
        for a, b in zip(seg, seg[1:]):
            step = (b - a + 180.0) % 360.0 - 180.0
            d += abs(step)
        per_sec.append(d)
    try:
        cache.write_text(json.dumps({"yaw_rate_per_sec": per_sec, "fps": fps}, ensure_ascii=False))
    except OSError as e:
        print(f"[warn] 活跃度缓存写入失败（不影响生产）：{e}", flush=True)
    return per_sec


def pick_window(
    dur: float,
    clip_sec: float,
    used: List[Tuple[float, float]],
    profile: Optional[List[float]],
    want_active: bool,
    rng: random.Random,
    edge_margin: float = 20.0,
) -> Optional[Tuple[float, str]]:
    """挑一个不与 used 重叠的起始秒。返回 (start_sec, 'active'|'random')。"""
    lo, hi = edge_margin, dur - clip_sec - edge_margin
    if hi <= lo:
        return None

    def free(s: float) -> bool:
        return all(s + clip_sec <= a or s >= b for a, b in used)

    if want_active and profile:
        # 按 clip 长度聚合活跃度，取高分位里的窗口随机选一个（不取最高，避免总落在同一处）
        w = int(round(clip_sec))
        agg = [(sum(profile[i : i + w]), i) for i in range(0, max(0, len(profile) - w))]
        agg = [(v, i) for v, i in agg if lo <= i <= hi]
        if agg:
            agg.sort(reverse=True)
            top = agg[: max(1, len(agg) // 5)]
            rng.shuffle(top)
            for _, i in top:
                if free(float(i)):
                    return float(i), "active"

    for _ in range(200):
        s = round(rng.uniform(lo, hi), 1)
        if free(s):
            return s, "random"
    return None


# ---------------------------------------------------------------- 切片


def crop_filter(roi: Tuple[int, int, int, int], pano_w: int) -> List[str]:
    """裁切滤镜。ROI 跨接缝时拆成两段再横向拼回去，等价于「环绕裁切」。"""
    rx, ry, rw, rh = roi
    if rx + rw <= pano_w:
        return ["-filter:v", f"crop={rw}:{rh}:{rx}:{ry}"]
    wa = pano_w - rx
    wb = rw - wa
    return [
        "-filter_complex",
        f"[0:v]crop={wa}:{rh}:{rx}:{ry}[a];[0:v]crop={wb}:{rh}:0:{ry}[b];[a][b]hstack[v]",
        "-map", "[v]",
    ]


def extract_clip(
    video: Path,
    start_sec: float,
    n_frames: int,
    roi: Tuple[int, int, int, int],
    out: Path,
    crf: int,
    fps: float,
    pano_w: int,
) -> int:
    """切出 ROI 片段，返回从输出 pts 反推的源起始帧号。

    `-copyts` 让输出保留源时间轴，这样 `stream.start_time` 就是可信的源帧号来源；
    没有它，输出时间戳会被重置到 0，帧号信息就永久丢失了。
    """
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-copyts", "-ss", f"{start_sec:.6f}", "-i", str(video),
         "-frames:v", str(n_frames), *crop_filter(roi, pano_w),
         "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-an", str(out)],
        check=True,
    )
    r = subprocess.run(
        ["ffprobe", "-v", "0", "-select_streams", "v",
         "-show_entries", "stream=start_time,nb_frames", "-of", "json", str(out)],
        capture_output=True, text=True, check=True,
    )
    st = json.loads(r.stdout)["streams"][0]
    return int(round(float(st["start_time"]) * fps))


# ---------------------------------------------------------------- 服务端挂钩


class Reporter:
    """作业内心跳 + 产出上报。服务端关掉或不可达时全部变成 no-op。"""

    def __init__(self, api: ServerAPI, job_id: str, args) -> None:
        self.api = api
        self.job_id = job_id
        self.node = resolve_node()
        self.poll_sec = int(args.poll_sec)
        self.keep_available = int(args.keep_available)
        self.batch = int(args.batch)
        self.loop_iter = 0
        self.stage = "读池"
        self.n_produced = 0
        self.n_round = 0
        self.last_error = ""
        self._flush_error = False
        self._offline_warned = False

    def set_stage(self, stage: str, heartbeat: bool = True) -> None:
        self.stage = stage
        if heartbeat:
            self.heartbeat()

    def note_error(self, msg: str) -> None:
        self.last_error = str(msg)
        self._flush_error = True
        self.api.event(self.job_id, "error", str(msg))

    def heartbeat(self) -> Optional[dict]:
        err = self.last_error if self._flush_error else None
        self._flush_error = False
        return self.api.heartbeat(
            self.job_id,
            loop_iter=self.loop_iter,
            stage=self.stage,
            n_produced=self.n_produced,
            last_error=err,
            keep_available=self.keep_available,
            batch=self.batch,
            poll_sec=self.poll_sec,
            node=self.node,
        )

    def produced(self, pid: str, meta: PacketMeta, size_bytes: int, sampling: str,
                 thumb_dir: Optional[Path] = None) -> None:
        self.n_produced += 1
        self.n_round += 1
        payload = {
            "packet_id": pid,
            "aoss_key": packet_prefix(pid),
            "meta": json.loads(meta.to_json()),
            "n_targets": len(meta.targets),
            "size_bytes": size_bytes,
            "producer_job_id": self.job_id,
            "producer_node": self.node,
        }
        r = self.api.register_packet(payload, thumb_dir=thumb_dir)
        if r is None and self.api.enabled:
            print(f"[warn] 注册包失败 {pid}：{self.api.last_error}", flush=True)
        ev = self.api.event(
            self.job_id,
            "produced",
            pid,
            extra={"sampling": sampling, "size_bytes": size_bytes, "n_targets": len(meta.targets)},
        )
        if ev is None and self.api.enabled:
            print(f"[warn] 上报 produced 失败 {pid}：{self.api.last_error}", flush=True)


def decide_need(reporter: Reporter, args) -> int:
    """本轮该补几个。优先心跳里的 want_more，其次 GET /pool，都拿不到就退回固定目标。"""
    hb = reporter.heartbeat()
    if hb is not None and hb.get("want_more") is not None:
        need = max(0, int(hb["want_more"]))
        pool = hb.get("pool") or {}
        avail = pool.get("n_available")
        extra = f"，available={avail}" if avail is not None else ""
        print(f"[loop] 服务端 want_more={need}{extra}，目标 {args.keep_available}", flush=True)
        return need
    # 心跳已经超时/连不上就别再打 /pool，少等一个 timeout
    pool = None
    if hb is not None or not reporter.api.enabled:
        pool = reporter.api.pool(keep_available=args.keep_available, batch=args.batch)
    if pool is not None and pool.get("want_more") is not None:
        need = max(0, int(pool["want_more"]))
        print(
            f"[loop] GET /pool want_more={need}，available={pool.get('n_available')}，"
            f"目标 {args.keep_available}",
            flush=True,
        )
        return need
    need = min(int(args.keep_available), int(args.batch))
    if not reporter.api.enabled:
        if not reporter._offline_warned:
            print(
                f"[warn] 未配置 BALL_GT_SERVER，退回按 --keep-available={args.keep_available} "
                f"生产（本轮最多 {need} 个，离线）",
                flush=True,
            )
            reporter._offline_warned = True
    else:
        print(
            f"[warn] 服务端不可达（{reporter.api.last_error}），"
            f"退回按 --keep-available={args.keep_available} 生产（本轮最多 {need} 个）",
            flush=True,
        )
    return need


# ---------------------------------------------------------------- 主流程


def load_used(store: Store, gt_dir: Path) -> Dict[str, List[Tuple[float, float]]]:
    """已用过的时间窗，按 "group__stem" 归组。

    两个来源：对象存储上已产出的包，以及本地已有的 GT（早期手工标的两个窗）。
    """
    used: Dict[str, List[Tuple[float, float]]] = {}

    def add(pid: str) -> None:
        try:
            group, stem, a, b = parse_packet_id(pid)
        except ValueError:
            return
        used.setdefault(f"{group}__{stem}", []).append((a, b))

    for pid in store.list_packets():
        add(pid)
    if gt_dir.is_dir():
        for p in gt_dir.glob("*.json"):
            add(p.stem)
    return used


def apply_blocklist(
    videos: List[dict],
    used: Dict[str, List[Tuple[float, float]]],
    bl: Optional[dict],
) -> None:
    """按服务端避让清单原地剔除停用视频，并把已移除包的时间窗记成已用。

    移除的包通常还留在对象存储上、`load_used` 已经避开了它的窗口；这里再按服务端补一遍，
    对象被清掉之后也不会被重新切出来。服务端不可达（bl=None）就只靠 `load_used`。
    """
    if not bl:
        return
    banned = set(bl.get("video_keys") or [])
    if banned:
        before = len(videos)
        videos[:] = [v for v in videos if f"{v['group']}__{v['stem']}" not in banned]
        print(f"[blocklist] 停用视频 {len(banned)} 路，本机剔除 {before - len(videos)} 路", flush=True)
    for pid in bl.get("packets") or []:
        try:
            group, stem, a, b = parse_packet_id(pid)
        except ValueError:
            continue
        used.setdefault(f"{group}__{stem}", []).append((a, b))


def run(args) -> None:
    cred = load_credentials(args.credentials)
    store = Store(cred)
    print(f"[store] {json.dumps(cred.redacted, ensure_ascii=False)}", flush=True)

    api = ServerAPI(timeout=float(getattr(args, "server_timeout", 15.0)))
    job_id = resolve_job_id()
    reporter = Reporter(api, job_id, args)
    print(
        f"[producer] job_id={job_id} node={reporter.node} "
        f"server={'off' if not api.enabled else api.base}",
        flush=True,
    )

    if not args.loop:
        produce(args, store, args.n_packets, reporter)
        return

    # 持续模式：问服务端还缺几个（心跳带回 want_more），标注员消耗多少就补多少。
    # 不再数 AOSS 上的 available——那边不再写 claims/，那个数会永远等于全部已产包。
    t0 = time.time()
    while True:
        if args.max_hours and (time.time() - t0) / 3600.0 > args.max_hours:
            print("[stop] 达到时间上限", flush=True)
            return
        reporter.loop_iter += 1
        reporter.n_round = 0
        reporter.set_stage("读池", heartbeat=False)
        try:
            need = decide_need(reporter, args)
        except Exception as e:  # noqa: BLE001
            reporter.note_error(str(e))
            print(f"[warn] 询问补池失败：{e}，{args.poll_sec}s 后重试", flush=True)
            time.sleep(args.poll_sec)
            continue
        if need > 0:
            print(f"[loop] 本轮补 {need} 个", flush=True)
            produce(args, store, min(need, args.batch), reporter)
        else:
            print("[loop] 缓冲池已满，休息", flush=True)
            time.sleep(args.poll_sec)


def produce(args, store: Store, n_want: int, reporter: Optional[Reporter] = None) -> int:
    """产出至多 n_want 个包，返回实际产出数。"""
    rng = random.Random(args.seed)
    videos = discover_videos(Path(args.data_root))
    print(f"[scan] 可用视频 {len(videos)} 路（已排除归档场次与未标定视频）", flush=True)
    if not videos:
        raise SystemExit("没有可用视频")

    used = load_used(store, Path(args.gt_dir))
    print(f"[pool] 已产出/已标注窗口涉及 {len(used)} 路视频", flush=True)
    apply_blocklist(videos, used, reporter.api.blocklist() if reporter else None)
    if not videos:
        print("[stop] 可用视频都被停用了", flush=True)
        return 0
    args = argparse.Namespace(**{**vars(args), "n_packets": n_want})

    # 优先抽窗口最少的视频，让覆盖面铺开而不是在同一路上反复取样
    made, t_start = 0, time.time()
    tmpdir = Path(tempfile.mkdtemp(prefix="ballgt_"))
    fails = 0
    while made < args.n_packets:
        if fails > 3 * len(videos) + 20:
            print("[stop] 连续多次找不到可用窗口，可能是池子已铺满", flush=True)
            break

        if reporter:
            reporter.set_stage("抽窗")
        videos.sort(key=lambda v: (len(used.get(f"{v['group']}__{v['stem']}", [])), rng.random()))
        pool = videos[: max(1, len(videos) // 3)]
        v = rng.choice(pool)
        key = f"{v['group']}__{v['stem']}"

        try:
            fps, dur = probe(v["mp4"])
        except Exception as e:
            print(f"[skip] {key} ffprobe 失败: {e}", flush=True)
            if reporter:
                reporter.note_error(f"ffprobe {key}: {e}")
            videos.remove(v)
            continue

        prof = activity_profile(v["group"], v["stem"], v["mp4"].parent, fps)
        want_active = rng.random() < args.active_ratio
        picked = pick_window(dur, args.clip_sec, used.get(key, []), prof, want_active, rng)
        if picked is None:
            fails += 1
            used.setdefault(key, []).append((0.0, dur))  # 这一路已铺满，别再抽
            continue
        start_sec, how = picked
        end_sec = start_sec + args.clip_sec

        ann = v["ann"]
        pano = ann.get("pano_size") or {}
        pano_w, pano_h = int(pano.get("width", 7680)), int(pano.get("height", 3840))
        roi = compute_roi_wrapped(
            ann, pano_w, pano_h, args.margin_x, args.margin_top, args.margin_bottom
        )
        n_src = int(round(args.clip_sec * fps))
        decim = choose_decim(fps)
        pid = make_packet_id(v["group"], v["stem"], start_sec, end_sec)
        prefix = packet_prefix(pid)

        if store.exists(f"{prefix}meta.json") or store.exists(f"packets/{pid}.zip"):
            used.setdefault(key, []).append((start_sec, end_sec))
            continue

        print(f"[make] {pid}  ({how}, {v['mp4'].name}, roi={roi}, decim={decim})", flush=True)
        work = tmpdir / pid
        work.mkdir(exist_ok=True)
        raw_clip = work / "clip_src.mp4"
        clip = work / "clip.mp4"
        t0 = time.time()
        if reporter:
            reporter.set_stage("ffmpeg 切片")
        try:
            frame_start = extract_clip(
                v["mp4"], start_sec, n_src, roi, raw_clip, args.crf, fps, pano_w
            )
        except subprocess.CalledProcessError as e:
            print(f"[fail] 切片失败 {pid}: {e}", flush=True)
            if reporter:
                reporter.note_error(f"切片失败 {pid}: {e}")
            fails += 1
            continue

        expect = int(round(start_sec * fps))
        if abs(frame_start - expect) > 1:
            print(f"[fail] 帧对齐异常：请求 {expect} 实得 {frame_start}，丢弃该包", flush=True)
            fails += 1
            continue

        if decim > 1:
            if reporter:
                reporter.set_stage("抽帧")
            try:
                decimate_clip(raw_clip, clip, decim)
            except subprocess.CalledProcessError as e:
                print(f"[fail] 抽帧失败 {pid}: {e}", flush=True)
                if reporter:
                    reporter.note_error(f"抽帧失败 {pid}: {e}")
                fails += 1
                continue
            raw_clip.unlink(missing_ok=True)
        else:
            raw_clip.replace(clip)

        clip_frames = n_src if decim == 1 else (n_src + decim - 1) // decim
        frame_end = frame_start + n_src - 1
        targets = list(range(frame_start, frame_end + 1, decim * args.step))

        if reporter:
            reporter.set_stage("DoG / scan")
        try:
            derive_from_clip(clip, work, fps=CLIP_FPS)
        except Exception as e:  # noqa: BLE001
            print(f"[fail] 派生失败 {pid}: {e}", flush=True)
            if reporter:
                reporter.note_error(f"派生失败 {pid}: {e}")
            fails += 1
            continue

        artifacts = {}
        for rel in (
            "clip.mp4", "dog.mp4", "scan_dog.jpg", "scan_time.jpg",
            "thumb/clip.mp4", "thumb/dog.mp4", "thumb/scan_dog.jpg", "thumb/scan_time.jpg",
        ):
            p = work / rel
            if p.exists():
                artifacts[rel] = {"bytes": p.stat().st_size}

        meta = PacketMeta(
            packet_id=pid,
            group=v["group"],
            stem=v["stem"],
            tag=f"{start_sec:04.0f}_{end_sec:04.0f}",
            start_sec=start_sec,
            end_sec=end_sec,
            fps=fps,
            pano_size=[pano_w, pano_h],
            roi=list(roi),
            frame_start=frame_start,
            frame_end=frame_end,
            clip_frames=clip_frames,
            step=args.step,
            targets=targets,
            sphere_transform=ann.get("sphere_transform"),
            field_size=ann.get("field_size"),
            field_corners_pano=ann.get("field_corners_pano"),
            sampling=how,
            produced_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            notes={"source_video": str(v["mp4"]), "crf": args.crf, "decim": decim},
            decim=decim,
            clip_fps=CLIP_FPS,
            artifacts=artifacts,
        )
        (work / "meta.json").write_text(meta.to_json(), encoding="utf-8")
        size_bytes = sum(int(v.get("bytes") or 0) for v in artifacts.values())
        size_mb = size_bytes / 1e6

        if args.dry_run:
            print(f"[dry-run] 本会上传 {pid} ({size_mb:.1f} MB, {len(targets)} 个标注点)", flush=True)
        else:
            if reporter:
                reporter.set_stage("上传")
            store.upload_file(f"{prefix}meta.json", work / "meta.json")
            for rel in artifacts:
                store.upload_file(f"{prefix}{rel}", work / rel)
            store.put_json(f"packets/{pid}.meta.json", json.loads(meta.to_json()))
            store.append_line(
                "produced.jsonl",
                {"packet_id": pid, "produced_at": meta.produced_at, "sampling": how,
                 "size_bytes": size_bytes, "n_targets": len(targets), "prefix": prefix},
            )
            if reporter:
                reporter.produced(
                    pid, meta, size_bytes=size_bytes, sampling=how,
                    thumb_dir=work / "thumb",
                )

        used.setdefault(key, []).append((start_sec, end_sec))
        made += 1
        fails = 0
        print(
            f"[ok] {pid}  {size_mb:.1f} MB  {len(targets)} 点  "
            f"用时 {time.time() - t0:.0f}s  ({made}/{args.n_packets})",
            flush=True,
        )

    print(f"[done] 新增 {made} 个包，耗时 {(time.time() - t_start) / 60:.1f} 分钟", flush=True)
    return made


def parse_args():
    p = argparse.ArgumentParser(description="生产足球 GT 标注包并上传到任务池")
    p.add_argument("--data-root", default=str(DATA_ROOT))
    p.add_argument(
        "--gt-dir",
        default=os.environ.get(
            "BALL_GT_EXISTING_GT",
            str(DATA_ROOT / "_research_ball_det" / "tbd" / "ball_gt"),
        ),
        help="已有 GT 目录，其窗口视为已用过",
    )
    p.add_argument("--n-packets", type=int, default=20, help="单次模式下产出多少个包")
    p.add_argument("--loop", action="store_true",
                   help="持续模式：维持待领取缓冲池，标注员消耗多少就补多少")
    p.add_argument("--keep-available", type=int, default=40,
                   help="持续模式下维持多少个待领取的包")
    p.add_argument("--batch", type=int, default=10, help="持续模式下每轮最多补几个")
    p.add_argument("--poll-sec", type=int, default=300, help="缓冲池已满时的检查间隔")
    p.add_argument("--max-hours", type=float, default=0.0, help="0 = 不限时")
    p.add_argument("--clip-sec", type=float, default=10.0)
    p.add_argument("--step", type=int, default=3, help="每隔多少帧需要标注一次")
    p.add_argument("--active-ratio", type=float, default=0.5,
                   help="偏向球运动活跃时段的比例，其余纯随机（保证静止球也有量）")
    p.add_argument("--crf", type=int, default=16, help="切片编码质量，越小越好越大")
    p.add_argument("--margin-x", type=int, default=150)
    p.add_argument("--margin-top", type=int, default=900,
                   help="相对球场角点向上扩展（飞球 / 天空方向）")
    p.add_argument("--margin-bottom", type=int, default=1100,
                   help="相对球场角点向下扩展（近相机端的球）")
    p.add_argument("--credentials", default=None)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--server-timeout",
        type=float,
        default=float(os.environ.get("BALL_GT_SERVER_TIMEOUT") or 15),
        help="问服务端心跳/补池的超时（秒）",
    )
    return p.parse_args()


if __name__ == "__main__":
    os.umask(0o002)  # 共享盘约定：新文件对组可写
    run(parse_args())
