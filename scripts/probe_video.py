#!/usr/bin/env python3
"""MiniMax-M3 视频输入阶梯实验：V0 分辨率体检 → V1 帧率即索引 → V2 分块 → V3 形态。

全景视频很可能看不见 17 px 的球（压到长边 1008 只剩 4 px）。V0 先问最粗的方向题；
连方向都错就转 V2 分块 1:1。M3 的 fps 上限是 5，所以把目标帧重编成 1 fps，
「第 t 秒」= 第 t 个目标帧。

    ./venv/bin/python scripts/probe_video.py --stage v0
    ./venv/bin/python scripts/probe_video.py --stage v0 v1 v2 v3 --packet 260122__002__1703_1713
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import data_roots, views as V  # noqa: E402
from client.prep import load_prepared, pano_to_roi  # noqa: E402
from client.vlm import VlmPool, extract_json  # noqa: E402
from producer.derive import encode_indexed_clip  # noqa: E402

THUMB = "https://ballgt.yinger650.com/thumb/{pid}/clip.mp4"
OUT = Path("/mnt/afs/wangmin/data/_ai_label_work/probe_video")
HIT_PX = 20.0


def load_gt(gt_dir: Path, packet_id: str, prep) -> Dict[int, Tuple[float, float]]:
    out: Dict[int, Tuple[float, float]] = {}
    for f in sorted(gt_dir.glob(f"{packet_id}__*.json")):
        d = json.loads(f.read_text())
        if d.get("annotator") == "smoketest":
            continue
        for r in d.get("labels", []):
            if r.get("visible") and r.get("pano_x") is not None:
                rx, ry = pano_to_roi(r["pano_x"], r["pano_y"], prep.roi, prep.pano_w)
                if 0 <= rx < prep.size[0] and 0 <= ry < prep.size[1]:
                    out[int(r["frame_idx"])] = (rx, ry)
    return out


def gt_band(gt: Dict[int, Tuple[float, float]], width: float) -> Dict[str, Any]:
    if not gt:
        return {}
    xs = [p[0] for p in gt.values()]
    ys = [p[1] for p in gt.values()]
    mx, my = float(np.median(xs)), float(np.median(ys))
    span = float(np.hypot(max(xs) - min(xs), max(ys) - min(ys)))
    third = width / 3.0
    if mx < third:
        band = "left"
    elif mx < 2 * third:
        band = "mid"
    else:
        band = "right"
    dx = xs[-1] - xs[0] if len(xs) > 1 else 0.0
    if abs(dx) < 20:
        direction = "still"
    else:
        direction = "right" if dx > 0 else "left"
    return {
        "band": band, "direction": direction, "span_px": round(span, 1),
        "median": [round(mx, 1), round(my, 1)], "n": len(gt),
    }


def video_spec(url: str, fps: float = 1.0, side: int = 1008, detail: str = "high") -> dict:
    return {"url": url, "fps": fps, "max_long_side_pixel": side, "detail": detail}


def file_url(path: Path) -> str:
    raw = path.read_bytes()
    return f"data:video/mp4;base64,{__import__('base64').b64encode(raw).decode()}"


def ask_video(pool: VlmPool, prompt: str, videos: Sequence[dict], *,
              thinking: Optional[str] = None, max_tokens: int = 512,
              use_cache: bool = True) -> Tuple[Optional[dict], Any]:
    extra = None
    if thinking == "off":
        extra = {"thinking": {"type": "disabled"}}
    elif thinking == "on":
        # M3 只认 adaptive / disabled，enabled 会 400
        extra = {"thinking": {"type": "adaptive"}}
    p = pool.provider("minimax")
    p.cfg.timeout = max(p.cfg.timeout, 180.0)
    p.cfg.max_tokens = max_tokens
    obj, res = pool.ask_json(
        "primary", prompt, system="只输出一个 JSON 对象，不要 markdown。",
        videos=list(videos), provider="minimax", use_cache=use_cache,
        extra_body=extra, max_tokens=max_tokens,
    )
    return obj, res


def rec(stage: str, **kw) -> dict:
    kw.setdefault("stage", stage)
    kw.setdefault("when", time.strftime("%Y-%m-%d %H:%M:%S"))
    return kw


# ---------------------------------------------------------------- V0


V0_PROMPT = """这段是足球场全景（可能被压得很扁）。请只回答最粗的问题：

1. 画面里有没有一颗正在使用的足球（在飞、在滚、或停在球员脚下都算）？
2. 它大致在画面的左 / 中 / 右哪一段？
3. 从哪往哪走（left / right / still）？

{"has_ball": true, "band": "left|mid|right", "direction": "left|right|still", "conf": 0.5, "why": "一句话"}
"""


def run_v0(pool: VlmPool, pid: str, prep, truth: dict, out_dir: Path,
           use_cache: bool) -> List[dict]:
    rows = []
    thumb = THUMB.format(pid=pid)
    variants = [("thumb_url", thumb, None)]
    local = None
    for root in data_roots.prepared_roots():
        p = root / pid / "clip.mp4"
        if p.exists():
            local = p
            break
    # 本地全分辨率动辄十几 MB base64，默认跳过；要对照时加 --local
    if local and getattr(run_v0, "use_local", False):
        if local.stat().st_size <= 45 * 1024 * 1024:
            variants.append(("local_b64", file_url(local), local))
        else:
            try:
                mm = pool.provider("minimax").upload_file(local)
                variants.append(("local_files", mm, local))
            except Exception as e:  # noqa: BLE001
                rows.append(rec("v0", variant="local_files", ok=False, error=str(e)))

    for name, url, _src in variants:
        t0 = time.monotonic()
        obj, res = ask_video(pool, V0_PROMPT, [video_spec(url)], use_cache=use_cache)
        row = rec(
            "v0", variant=name, ok=bool(obj), obj=obj,
            latency=round(getattr(res, "latency", time.monotonic() - t0), 2),
            tokens=getattr(res, "usage", None) or {},
            error=getattr(res, "error", "") or "",
            raw=(getattr(res, "text", "") or "")[:400],
            truth=truth,
        )
        if obj and truth:
            row["band_ok"] = str(obj.get("band") or "") == truth.get("band")
            row["dir_ok"] = str(obj.get("direction") or "") == truth.get("direction")
            # 静止包方向说 still 或任意都算松通过；band 必须对
            row["pass"] = bool(row["band_ok"]) and (
                truth.get("direction") == "still" or row["dir_ok"]
            )
        rows.append(row)
        print(f"  V0 {name}: obj={obj}  band_ok={row.get('band_ok')} dir_ok={row.get('dir_ok')} "
              f"{row['latency']}s", flush=True)
    return rows


# ---------------------------------------------------------------- V1


V1_PROMPT = """这是把 {n} 个目标帧按时间顺序重编成的 {fps} fps 视频：第 t 秒 = 第 t 个目标帧（从 0 起）。

请数清楚一共大约几秒，并指出球（或你认为的球）第一次能看清的秒数、最后一次能看清的秒数。
不确定就填 null。不要猜像素坐标。

{{"n_seconds": 0, "first_t": 0, "last_t": 0, "conf": 0.5, "why": "一句话"}}
"""


def run_v1(pool: VlmPool, pid: str, prep, gt: dict, out_dir: Path,
           use_cache: bool) -> List[dict]:
    rows = []
    clip = None
    for root in data_roots.prepared_roots():
        p = root / pid / "clip.mp4"
        if p.exists():
            clip = p
            break
    if clip is None:
        return [rec("v1", ok=False, error="没有本地 clip.mp4")]
    targets = list(prep.targets)
    ids = []
    for fi in targets:
        try:
            ids.append(int(prep.meta.clip_index(fi)))
        except Exception:
            continue
    if not ids:
        return [rec("v1", ok=False, error="clip_index 失败")]
    for fps in (1.0, 5.0):
        dest = out_dir / f"{pid}_indexed_{int(fps)}fps.mp4"
        encode_indexed_clip(clip, dest, ids, fps=fps)
        prompt = V1_PROMPT.format(n=len(ids), fps=int(fps))
        url = file_url(dest) if dest.stat().st_size <= 45 * 1024 * 1024 else (
            pool.provider("minimax").upload_file(dest)
        )
        obj, res = ask_video(pool, prompt, [video_spec(url, fps=fps)], use_cache=use_cache)
        n = len(ids)
        first_ok = last_ok = None
        if obj and gt:
            vis = [i for i, fi in enumerate(targets) if fi in gt]
            if vis and obj.get("first_t") is not None:
                first_ok = abs(int(obj["first_t"]) - vis[0]) <= 3
            if vis and obj.get("last_t") is not None:
                last_ok = abs(int(obj["last_t"]) - vis[-1]) <= 3
        row = rec(
            "v1", fps=fps, n_frames=n, ok=bool(obj), obj=obj,
            first_ok=first_ok, last_ok=last_ok,
            n_seconds_ok=(abs(int(obj.get("n_seconds") or -1) - n) <= 2) if obj else None,
            latency=round(getattr(res, "latency", 0), 2),
            tokens=getattr(res, "usage", None) or {},
            error=getattr(res, "error", "") or "",
            raw=(getattr(res, "text", "") or "")[:400],
            path=str(dest),
        )
        rows.append(row)
        print(f"  V1 {fps}fps: n={n} obj={obj} n_ok={row['n_seconds_ok']} "
              f"first_ok={first_ok} {row['latency']}s")
    return rows


# ---------------------------------------------------------------- V2


def tiles_for(size: Tuple[int, int], side: int = 1008) -> List[Tuple[int, int, int, int]]:
    w, h = int(size[0]), int(size[1])
    out = []
    ny = max(1, int(np.ceil(h / side)))
    nx = max(1, int(np.ceil(w / side)))
    tw, th = int(np.ceil(w / nx)), int(np.ceil(h / ny))
    for iy in range(ny):
        for ix in range(nx):
            x0, y0 = ix * tw, iy * th
            ww, hh = min(tw, w - x0), min(th, h - y0)
            if ww >= 64 and hh >= 64:
                out.append((x0, y0, ww, hh))
    return out


def burn_tile_video(prep, ids_fi: Sequence[int], crop: Tuple[int, int, int, int],
                    dest: Path, cols: int = 6, rows: int = 6, fps: float = 1.0) -> V.GridView:
    """把每帧裁切+网格烧进视频，返回最后一帧的 GridView 做格子映射。"""
    x0, y0, ww, hh = crop
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix("")
    tmp.mkdir(parents=True, exist_ok=True)
    master = None
    for i, fi in enumerate(ids_fi):
        view = V.make_crop_view(
            prep, int(fi), (x0 + ww / 2.0, y0 + hh / 2.0),
            size=float(max(ww, hh)), cols=cols, rows=rows, panes=("raw",),
        )
        master = view
        img = view.image.copy()
        cv2.putText(img, f"t={i} f{fi}", (12, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (0, 255, 255), 2, cv2.LINE_AA)
        h, w = img.shape[:2]
        pad_w, pad_h = w % 2, h % 2
        if pad_w or pad_h:
            img = cv2.copyMakeBorder(img, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, value=(0, 0, 0))
        cv2.imwrite(str(tmp / f"{i:04d}.png"), img)
    import subprocess
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-framerate", str(fps), "-i", str(tmp / "%04d.png"),
            "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-an", str(dest),
        ],
        check=True,
    )
    return master


V2_PROMPT = """这是球场的一块 {w}×{h} 裁切，按 1 fps 编成视频：第 t 秒 = 第 t 个目标帧。
每帧烧了格子（{cols}×{rows}）和 t=帧号。

请输出你看见球的格子序列。看不见就填 null。球可以是静止的。

{{"points": [{{"t": 0, "cell": "C3"}}], "conf": 0.5, "why": "一句话"}}
"""


def run_v2(pool: VlmPool, pid: str, prep, gt: dict, out_dir: Path,
           use_cache: bool, layers: str = "raw") -> List[dict]:
    rows = []
    tiles = tiles_for(prep.size, 1008 if prep.size[1] > 800 else 720)
    # 若有 GT，只跑覆盖真球的 1–2 块
    hot = []
    if gt:
        for crop in tiles:
            x0, y0, ww, hh = crop
            if any(x0 <= p[0] < x0 + ww and y0 <= p[1] < y0 + hh for p in gt.values()):
                hot.append(crop)
    pick = hot or tiles[:2]
    targets = list(prep.targets)
    for i, crop in enumerate(pick):
        dest = out_dir / f"{pid}_tile{i}_{crop[2]}x{crop[3]}.mp4"
        view = burn_tile_video(prep, targets, crop, dest)
        url = file_url(dest) if dest.stat().st_size <= 45 * 1024 * 1024 else (
            pool.provider("minimax").upload_file(dest)
        )
        prompt = V2_PROMPT.format(w=crop[2], h=crop[3], cols=view.cols, rows=view.rows)
        obj, res = ask_video(pool, prompt, [video_spec(url, fps=1.0, side=1008)],
                             use_cache=use_cache, max_tokens=1024)
        hits = nulls = n = 0
        errs = []
        if obj and isinstance(obj.get("points"), list):
            for pt in obj["points"]:
                if not isinstance(pt, dict):
                    continue
                t = pt.get("t")
                cell = pt.get("cell")
                if t is None or int(t) < 0 or int(t) >= len(targets):
                    continue
                fi = targets[int(t)]
                if fi not in gt:
                    continue
                n += 1
                if not cell:
                    nulls += 1
                    continue
                xy = view.cell_center(str(cell))
                if xy is None:
                    nulls += 1
                    continue
                err = float(np.hypot(xy[0] - gt[fi][0], xy[1] - gt[fi][1]))
                errs.append(err)
                hits += int(err <= HIT_PX)
        row = rec(
            "v2", tile=i, crop=list(crop), ok=bool(obj), obj=obj,
            n_scored=n, hits=hits, nulls=nulls,
            hit_rate=round(hits / n, 3) if n else None,
            median_err=round(float(np.median(errs)), 1) if errs else None,
            latency=round(getattr(res, "latency", 0), 2),
            tokens=getattr(res, "usage", None) or {},
            error=getattr(res, "error", "") or "",
            raw=(getattr(res, "text", "") or "")[:400],
            path=str(dest),
        )
        rows.append(row)
        print(f"  V2 tile{i} {crop}: hit={hits}/{n} med={row['median_err']} {row['latency']}s")
    return rows


# ---------------------------------------------------------------- V3


V3_PROMPTS = {
    "traj": (
        "用一句话描述这段 1 fps 视频里球的轨迹（方向、有没有踢球/落地），不要坐标。\n"
        '{"kind": "traj", "text": "...", "events": ["kick"], "conf": 0.5}'
    ),
    "cells": V2_PROMPT,
    "keyframes": (
        "只标关键帧（踢球、落地、方向突变）的格子，其他帧不要填。\n"
        "第 t 秒 = 第 t 个目标帧。"
        '{"points": [{"t": 0, "cell": "C3", "event": "kick"}], "conf": 0.5}'
    ),
}


def run_v3(pool: VlmPool, pid: str, prep, gt: dict, out_dir: Path,
           use_cache: bool) -> List[dict]:
    rows = []
    tiles = tiles_for(prep.size, 1008 if prep.size[1] > 800 else 720)
    crop = tiles[0]
    if gt:
        for c in tiles:
            x0, y0, ww, hh = c
            if any(x0 <= p[0] < x0 + ww and y0 <= p[1] < y0 + hh for p in gt.values()):
                crop = c
                break
    dest = out_dir / f"{pid}_v3_tile.mp4"
    view = burn_tile_video(prep, list(prep.targets), crop, dest)
    url = file_url(dest) if dest.stat().st_size <= 45 * 1024 * 1024 else (
        pool.provider("minimax").upload_file(dest)
    )
    for form, prompt in V3_PROMPTS.items():
        p = (
            prompt.replace("{w}", str(crop[2])).replace("{h}", str(crop[3]))
            .replace("{cols}", str(view.cols)).replace("{rows}", str(view.rows))
        )
        for think in ("off", "on"):
            obj, res = ask_video(
                pool, p, [video_spec(url, fps=1.0, side=1008)],
                thinking=think, use_cache=use_cache, max_tokens=1024,
            )
            row = rec(
                "v3", form=form, thinking=think, ok=bool(obj), obj=obj,
                latency=round(getattr(res, "latency", 0), 2),
                tokens=getattr(res, "usage", None) or {},
                reasoning_len=int((getattr(res, "usage", None) or {}).get("reasoning_content_len") or 0),
                error=getattr(res, "error", "") or "",
                raw=(getattr(res, "text", "") or "")[:400],
            )
            rows.append(row)
            print(f"  V3 {form}/{think}: ok={row['ok']} tok={row['tokens']} "
                  f"think={row['reasoning_len']} {row['latency']}s")
    return rows


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="MiniMax-M3 视频输入阶梯实验")
    ap.add_argument("--stage", nargs="+", default=["v0"], choices=["v0", "v1", "v2", "v3"])
    ap.add_argument("--packet", default="260122__002__1703_1713")
    ap.add_argument("--work", type=Path, default=None)
    ap.add_argument("--gt", type=Path, default=data_roots.GT)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--local", action="store_true", help="V0 额外打本地全分辨率（base64 / Files API）")
    ap.add_argument("--max-frames", type=int, default=0, help="V1/V2/V3 最多用多少目标帧，0=全部")
    a = ap.parse_args()

    pool = VlmPool(use_cache=not a.no_cache)
    if not pool.cfgs.get("minimax") or not pool.cfgs["minimax"].has_key:
        print("没有 MINIMAX_KEY，跳过真实调用。脚本本身已就位。")
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "skipped.json").write_text(json.dumps({"skip": "no MINIMAX_KEY", "stages": a.stage}))
        return 0

    work = a.work or data_roots.find_prepared(a.packet)
    prep = load_prepared(work, a.packet)
    if a.max_frames:
        prep.meta.targets = list(prep.meta.targets)[: int(a.max_frames)]
    gt = load_gt(a.gt, a.packet, prep)
    truth = gt_band(gt, float(prep.size[0]))
    out_dir = a.out / a.packet
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"包 {a.packet} ROI {prep.size} GT {len(gt)} 真值区段 {truth}", flush=True)
    print(f"阶段 {a.stage}\n", flush=True)

    report: Dict[str, Any] = {
        "packet": a.packet, "roi": list(prep.size), "gt_n": len(gt),
        "truth": truth, "stages": a.stage, "rows": [],
    }
    dest = out_dir / ("+".join(a.stage) + ".json")

    def save() -> None:
        dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if "v0" in a.stage:
        run_v0.use_local = bool(a.local)
        report["rows"] += run_v0(pool, a.packet, prep, truth, out_dir, not a.no_cache)
        save()
    if "v1" in a.stage:
        report["rows"] += run_v1(pool, a.packet, prep, gt, out_dir, not a.no_cache)
        save()
    if "v2" in a.stage:
        report["rows"] += run_v2(pool, a.packet, prep, gt, out_dir, not a.no_cache)
        save()
    if "v3" in a.stage:
        report["rows"] += run_v3(pool, a.packet, prep, gt, out_dir, not a.no_cache)
        save()

    print(f"\n写到 {dest}")
    v0 = [r for r in report["rows"] if r.get("stage") == "v0"]
    if v0:
        seen = any(r.get("pass") for r in v0)
        print("V0 结论：全景视频" + ("能看出大致区段" if seen else "看不清球，应转 V2 分块"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
