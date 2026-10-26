#!/usr/bin/env python3
"""国产模型投票标注：持续领包，本地 blob 粗定位，四家模型读点，≥2 家在 10px 内才落点。

每进程一个 worker（并行由 scripts/run_vote_label.sh 起多个进程）。
点不够、轨迹接不上就 hard:cannot_locate 释放，不把猜的点交上去。

    PYTHONPATH=. venv/bin/python scripts/vote_label_loop.py --worker vote-0
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client.api import Lease, ServerClient  # noqa: E402
from client.fetch import ensure_packet_files, open_store  # noqa: E402
from client.prep import prepare_packet, workspace  # noqa: E402
from client.propose import activity_map, find_blobs  # noqa: E402
from client.run import build_ai_result  # noqa: E402
from client.vlm import Provider, extract_json, load_dotenv, load_providers  # noqa: E402
from packet import ALL_ARTIFACT_FILES  # noqa: E402

log = logging.getLogger("vote")

SYSTEM = "你是足球比赛视频的标注员，只标比赛中正在使用的那一个球。只回 JSON，不要解释。"
UNITS = {
    "px": "坐标单位是像素：左上角 (0,0)，右下角 ({w},{h})",
    "n1000": "坐标用 0–1000 归一化：左上角 (0,0)，右下角 (1000,1000)，与图的实际像素无关",
}
PROMPT = (
    "这是 8K 全景足球视频的一块局部原图，宽 {w} px、高 {h} px。{unit}。"
    "球可能在滚动、在空中，也可能静止在地上或球员脚下；场边的备用球不算。"
    '能看见比赛用球就回 {{"visible": true, "x": 球心横坐标, "y": 球心纵坐标}}；'
    '看不见或不确定回 {{"visible": false}}，不要猜。'
)
# (provider, unit, crop_px, nearest-neighbor scale) — 各家读点对比里的最佳单帧
PRIMARY = (("cmp_glm53_flash", "px", 320, 2), ("cmp_deepseek", "n1000", 480, 1))
BACKUP = (("cmp_minimax_m3", "px", 480, 1), ("cmp_mimo_pro", "n1000", 480, 1))
AGREE_R = 10.0
MAX_SPEED = 450.0  # 相邻目标帧（step=3）快球大约 390px
MIN_SUBMIT = 20
METHOD = {
    "harness": "vote-vlm",
    "harness_version": "vote-domestic-v1",
    "model": "glm-5.3-flash+deepseek-flash+MiniMax-M3+mimo-v2.6-pro",
    "model_version": "2026-10-23",
}


def _png(img) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("png encode failed")
    return buf.tobytes()


def _crop(img, cx: float, cy: float, size: int, scale: int):
    x0, y0 = math.floor(cx - size / 2), math.floor(cy - size / 2)
    h, w = img.shape[:2]
    x1, y1 = x0 + size, y0 + size
    pad = [max(0, -y0), max(0, y1 - h), max(0, -x0), max(0, x1 - w)]
    patch = img[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
    if any(pad):
        patch = cv2.copyMakeBorder(patch, pad[0], pad[1], pad[2], pad[3], cv2.BORDER_CONSTANT, value=0)
    if scale != 1:
        patch = cv2.resize(patch, (size * scale, size * scale), interpolation=cv2.INTER_NEAREST)
    return patch, x0, y0


def _to_px(v: float, unit: str, extent: int) -> float:
    return v * extent / 1000.0 if unit == "n1000" else v


def _vote(preds: Sequence[Optional[Tuple[float, float]]], k: int = 2, radius: float = AGREE_R):
    pts = [p for p in preds if p is not None]
    best: List[Tuple[float, float]] = []
    for p in pts:
        sup = [q for q in pts if math.hypot(p[0] - q[0], p[1] - q[1]) <= radius]
        if len(sup) > len(best):
            best = sup
    if len(best) < k:
        return None
    return (statistics.median(q[0] for q in best), statistics.median(q[1] for q in best))


class Readers:
    def __init__(self, providers_path: Path):
        cfgs = load_providers(providers_path)
        self.prov = {name: Provider(cfgs[name], cache=None) for name, *_ in PRIMARY + BACKUP}
        self.calls = 0

    def _one(self, spec, prep, fi: int, center: Tuple[float, float]):
        name, unit, size, scale = spec
        img, x0, y0 = _crop(prep.raw(fi), center[0], center[1], size, scale)
        w = h = size * scale
        r = self.prov[name].call(
            PROMPT.format(w=w, h=h, unit=UNITS[unit].format(w=w, h=h)),
            images=[_png(img)], system=SYSTEM, use_cache=False, retries=4,
        )
        self.calls += 1
        if not r.ok:
            log.info("f%s %s 失败 %s", fi, name, (r.error or "")[:160])
            return None
        js = extract_json(r.text or "")
        if not isinstance(js, dict) or not js.get("visible") or js.get("x") is None:
            return None
        try:
            ax, ay = float(js["x"]), float(js["y"])
        except (TypeError, ValueError):
            return None
        return (x0 + _to_px(ax, unit, w) / scale, y0 + _to_px(ay, unit, h) / scale)

    def ask(self, prep, fi: int, center: Tuple[float, float], specs) -> Optional[Tuple[float, float]]:
        with ThreadPoolExecutor(max_workers=len(specs)) as pool:
            preds = list(pool.map(lambda s: self._one(s, prep, fi, center), specs))
        return _vote(preds)


def _speed_ok(a, b, step: int) -> bool:
    gap = abs(int(b[0]) - int(a[0])) / max(step, 1)
    if gap <= 0:
        return False
    return math.hypot(a[1] - b[1], a[2] - b[2]) <= MAX_SPEED * gap


def longest_chain(pts: List[Tuple[int, float, float]], step: int) -> List[Tuple[int, float, float]]:
    pts = sorted(pts, key=lambda p: p[0])
    n = len(pts)
    dp = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if _speed_ok(pts[j], pts[i], step) and dp[j] + 1 > dp[i]:
                dp[i] = dp[j] + 1
                prev[i] = j
    if not n:
        return []
    i = max(range(n), key=lambda k: dp[k])
    out = []
    while i >= 0:
        out.append(pts[i])
        i = prev[i]
    return list(reversed(out))


def _near(hit, center, limit) -> bool:
    return hit is not None and math.hypot(hit[0] - center[0], hit[1] - center[1]) <= limit


def search_at(readers: Readers, prep, fi: int, pred: Tuple[float, float], activity) -> Optional[Tuple[float, float]]:
    """先问 glm+deepseek。对不上再在预测点附近的 blob 上问一次，仍不行才叫另外两家。"""
    hit = readers.ask(prep, fi, pred, PRIMARY)
    if _near(hit, pred, 180):
        return hit
    blobs = find_blobs(prep.enh(fi), window=(pred[0], pred[1], 320), top_k=2, activity=activity)
    for b in blobs:
        if math.hypot(b.x - pred[0], b.y - pred[1]) > 400:
            continue
        hit = readers.ask(prep, fi, (b.x, b.y), PRIMARY)
        if _near(hit, (b.x, b.y), 30) and _near(hit, pred, MAX_SPEED):
            return hit
    hit = readers.ask(prep, fi, pred, PRIMARY + BACKUP)
    if _near(hit, pred, MAX_SPEED):
        return hit
    return None


def bootstrap(readers: Readers, prep, targets: List[int], step: int, activity):
    n = len(targets)
    idxs = sorted({0, n // 5, 2 * n // 5, 3 * n // 5, 4 * n // 5, n - 1})
    hits: List[Tuple[int, float, float]] = []
    for i in idxs:
        fi = targets[i]
        blobs = find_blobs(prep.enh(fi), top_k=4, activity=activity)
        for b in blobs:
            hit = readers.ask(prep, fi, (b.x, b.y), PRIMARY)
            if _near(hit, (b.x, b.y), 30):
                hits.append((fi, hit[0], hit[1]))
                break
    chain = longest_chain(hits, step)
    if len(chain) >= 2:
        return chain
    for i in idxs:
        fi = targets[i]
        if any(h[0] == fi for h in hits):
            continue
        blobs = find_blobs(prep.enh(fi), top_k=3, activity=activity)
        for b in blobs:
            hit = readers.ask(prep, fi, (b.x, b.y), PRIMARY + BACKUP)
            if _near(hit, (b.x, b.y), 30):
                hits.append((fi, hit[0], hit[1]))
                break
    return longest_chain(hits, step)


def propagate(readers, prep, targets, step, known: Dict[int, Tuple[float, float]], activity, forward: bool):
    order = targets if forward else list(reversed(targets))
    last: Optional[Tuple[int, float, float]] = None
    vel = (0.0, 0.0)
    miss = 0
    for fi in order:
        if fi in known:
            if last is not None:
                dt = (fi - last[0]) / max(step, 1)
                if dt != 0:
                    vel = ((known[fi][0] - last[1]) / dt, (known[fi][1] - last[2]) / dt)
            last = (fi, known[fi][0], known[fi][1])
            miss = 0
            continue
        if last is None or miss >= 8:
            continue
        dt = (fi - last[0]) / max(step, 1)
        pred = (last[1] + vel[0] * dt, last[2] + vel[1] * dt)
        rw, rh = prep.size
        pred = (min(max(pred[0], 0), rw - 1), min(max(pred[1], 0), rh - 1))
        hit = search_at(readers, prep, fi, pred, activity)
        if hit is None:
            miss += 1
            continue
        known[fi] = hit
        last = (fi, hit[0], hit[1])
        miss = 0
        if dt != 0 and last is not None:
            vel = ((hit[0] - (pred[0] - vel[0] * dt)) / dt, (hit[1] - (pred[1] - vel[1] * dt)) / dt)


def label_packet(readers: Readers, prep) -> List[dict]:
    targets = [int(t) for t in (prep.targets or prep.frames)]
    step = int(prep.meta.step or 1)
    activity = activity_map(prep)
    chain = bootstrap(readers, prep, targets, step, activity)
    log.info("%s 冷启动链 %d 点", prep.packet_id, len(chain))
    if len(chain) < 2:
        return []
    known = {fi: (x, y) for fi, x, y in chain}
    propagate(readers, prep, targets, step, known, activity, forward=True)
    propagate(readers, prep, targets, step, known, activity, forward=False)
    pts = longest_chain([(fi, xy[0], xy[1]) for fi, xy in known.items()], step)
    rows = []
    for fi, x, y in pts:
        px, py = prep.to_pano(x, y)
        rows.append({
            "frame_idx": int(fi), "visible": True, "pano_x": round(px, 2), "pano_y": round(py, 2),
            "occluded": False, "blurred": False, "conf": 0.85, "interp": False, "outlier": False,
            "stages": ["vote"],
        })
    return rows


def lease_packet(api: ServerClient, worker: str) -> Optional[Lease]:
    """AFS 上的 client.api 还没有 method 参数；直接把方法字段放进请求体。"""
    payload = api.post("/api/v2/lease", {"worker": worker, "kind": "ai", **METHOD})
    got = Lease.from_response(payload if isinstance(payload, dict) else {})
    if got is None:
        reason = (payload or {}).get("reason") if isinstance(payload, dict) else "池子空"
        log.info("lease 空: %s", reason)
    return got


def submit_packet(api: ServerClient, result: dict, lease_id: str, worker: str) -> dict:
    result = {**result, **METHOD}
    fields = {
        "source": "ai", "packet_id": str(result.get("packet_id") or ""),
        "lease_id": lease_id, "worker": worker,
        "workflow": METHOD["harness_version"], "model_used": METHOD["model"],
        **METHOD,
    }
    raw = json.dumps(result, ensure_ascii=False).encode()
    return api.post_multipart("/api/v2/submit", fields, {
        "result": ("result.json", raw, "application/json"),
    })


def process_one(api: ServerClient, store, work: Path, worker: str, providers: Path) -> str:
    lease = lease_packet(api, worker)
    if lease is None:
        return "empty"
    pid = lease.packet_id
    hb = api.start_heartbeat(lease.lease_id, 45.0)
    t0 = time.time()
    readers = Readers(providers)
    try:
        hb.set_stage("download")
        dest = workspace(work, pid)
        dest.mkdir(parents=True, exist_ok=True)
        ensure_packet_files(store, lease.aoss_key, dest, pid, which=ALL_ARTIFACT_FILES)
        hb.set_stage("prep")
        src = dest if (dest / "clip.mp4").exists() or (dest / "meta.json").exists() else dest / "packet.zip"
        prep = prepare_packet(src, work, packet_id=pid)
        hb.set_stage("vote")
        rows = label_packet(readers, prep)
        log.info("%s 投票点 %d / %d，调用 %d，用时 %.0fs",
                 pid, len(rows), len(prep.targets or []), readers.calls, time.time() - t0)
        if len(rows) < MIN_SUBMIT:
            api.release(lease.lease_id, f"hard:cannot_locate: 投票只确认 {len(rows)} 帧，不够 {MIN_SUBMIT}")
            return "released"
        hb.set_stage("submit")
        result = build_ai_result(
            prep, rows, worker=worker, workflow=METHOD["harness_version"],
            model_used=METHOD["model"], elapsed=time.time() - t0,
            n_vlm_calls=readers.calls, seed_source="blob+vote",
            summary={"n_voted": len(rows), "n_targets": len(prep.targets or []), "n_calls": readers.calls},
        )
        resp = submit_packet(api, result, lease.lease_id, worker)
        log.info("%s 已提交 %s", pid, (resp or {}).get("submission_id"))
        return "submitted"
    except Exception as e:  # noqa: BLE001
        log.exception("%s 失败", pid)
        api.release(lease.lease_id, f"error:{type(e).__name__}: {e}")
        return "error"
    finally:
        hb.stop()
        shutil.rmtree(workspace(work, pid), ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server", default=os.environ.get("BALL_GT_SERVER") or "https://ballgt.yinger650.com")
    ap.add_argument("--worker", default="vote-0")
    ap.add_argument("--work", type=Path, default=Path("/mnt/afs/wangmin/data/_ai_label_work/vote"))
    ap.add_argument("--providers", type=Path, default=Path("deploy/vote/providers.yaml"))
    ap.add_argument("--idle", type=float, default=20.0)
    ap.add_argument("--max-packets", type=int, default=0)
    ap.add_argument("--log-jsonl", type=Path, default=None)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    work = a.work / a.worker
    work.mkdir(parents=True, exist_ok=True)
    api = ServerClient(a.server, worker=a.worker)
    store = open_store()
    seen = 0
    while True:
        if a.max_packets and seen >= a.max_packets:
            break
        status = process_one(api, store, work, a.worker, a.providers)
        rec = {"worker": a.worker, "status": status, "t": time.strftime("%H:%M:%S")}
        if a.log_jsonl:
            a.log_jsonl.parent.mkdir(parents=True, exist_ok=True)
            with a.log_jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if status == "empty":
            log.info("池子空，%ss 后再领", a.idle)
            time.sleep(a.idle)
            continue
        seen += 1
        if status != "submitted":
            time.sleep(a.idle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
