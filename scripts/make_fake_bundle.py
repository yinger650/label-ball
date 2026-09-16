#!/usr/bin/env python3
"""造一个符合规格的**假审核包**，让服务端能脱离 AI 客户端自测。

审核包由 `client/bundle.py` 产出、由 `server/templates/review.html` 播放，两边是并行
开发的。没有这个脚本，服务端这一侧要等客户端跑通真实 VLM 流程才能验证提交、入库、
审核那条路——那太晚了。所以这里按规格造一份形状完全一样的数据，顺带把规格钉死：
下面这份 manifest 的字段就是两边的契约。

## 审核包规格

    follow.webp       N 个目标帧的稳定裁切（原图轨道），sprite 雪碧图，每格 192 px
    follow_enh.webp   同上的滤波（DoG）轨道
    region.webp       可选：所有帧裁同一个固定区域（轨迹包围盒留边后 < 640 px 才出）
    overview.webp     一张全局静图：DoG 时间最大投影叠标注折线
    manifest.json     逐帧元数据

sprite 是**存储格式而不是显示格式**：一次 HTTP 请求拿到全部帧，浏览器按格子 blit 到
canvas 就是零解码延迟的逐帧播放，比 mp4 更适合需要精确单帧步进的审核。第 `i` 帧的格
子在 `col = i % cols`、`row = i // cols`，源矩形 `(col*cell, row*cell, cell, cell)`。

manifest：

    {
      "version": "1.0", "kind": "ball_review_bundle",
      "packet_id": ..., "fps": 30.0, "step": 3, "cell": 192, "n_frames": 98,
      "roi": [x, y, w, h], "pano_size": [w, h],
      "tracks": {
        "follow":     {"file": ..., "cols": 14, "rows": 7, "cell": 192, "pane": "raw", "mode": "follow"},
        "follow_enh": {... "pane": "enh", "mode": "follow"},
        "region":     {... "mode": "region", "rect": [x,y,w,h], "scale": 0.34}
      },
      "overview": {"file": ..., "width": 1200, "height": 567, "roi": [...], "scale": 0.333},
      "frames": [{
        "i": 0, "frame_idx": 300,
        "pano_x": ..., "pano_y": ...,     # 标注点（全景像素）
        "cx": ..., "cy": ...,             # 裁切窗中心（全景像素，来自**平滑后**的轨迹）
        "dx": ..., "dy": ...,             # 标注点相对裁切中心的偏移（源像素）
        "rx": ..., "ry": ...,             # 固定区域轨道里标注点的格内像素坐标
        "ox": ..., "oy": ...,             # 标注点在 overview 图上的像素坐标
        "conf": 0.82, "interp": false,
        "visible": true, "occluded": false, "blurred": false,
        "stages": ["coarse", "refine1", "refine2"], "motion_px": 12.4
      }]
    }

裁切窗中心用**平滑后**的轨迹而不是原始标注点，是这个格式唯一重要的设计决定：镜头稳住
之后，球该稳稳停在画面中央、标注标记该牢牢粘在球上，两个错误信号同时可见——球跳出中
心说明平滑轨迹和标注不一致，标记脱开球说明这一帧标错了。以原始标注点为中心的话，镜头
会跟着错误一起晃，反而把错误藏起来。所以 `dx/dy`（而不是 `pano_x/pano_y`）才是审核页
画标记的依据；`dx/dy` 由产包方算好并**已处理全景横向接缝取模**，审核页直接用。

## 用法

    ./venv/bin/python scripts/make_fake_bundle.py --out /tmp/fake_pkt

产出 `bundle.zip`（传给 `/api/v2/submit` 的 `bundle` 字段）、`result.json`（`result`
字段）、`meta.json`（`/api/v2/register` 的 `meta`）以及解开的各个文件，方便肉眼看。
"""

from __future__ import annotations

import argparse
import json
import math
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

CELL = 192
FIELD_GREEN = (46, 92, 52)


# ---------------------------------------------------------------- 合成图像

def _disc(cell: int, cx: float, cy: float, r: float) -> np.ndarray:
    """一个软边圆盘的 mask。软边而不是硬边：真实的 8–30 px 小球在 ERP 上边缘是糊的，
    硬边会让审核页上的球看起来比实际好认，自测就失去意义了。"""
    yy, xx = np.mgrid[0:cell, 0:cell]
    d = np.hypot(xx - cx, yy - cy)
    return np.clip((r + 1.0 - d) / 2.0, 0.0, 1.0)


def _cell_raw(rng: np.random.Generator, ball: Optional[Tuple[float, float]], r: float,
              cell: int, line_phase: float) -> np.ndarray:
    """原图轨道的一格：草皮 + 噪声 + 一条球场白线 + 球。"""
    img = np.array(FIELD_GREEN, np.float32)[None, None, :] + rng.normal(0, 6.0, (cell, cell, 3))
    # 白线给一个可辨认的静态参照物：跟随模式下球不动、线在动，播放起来才看得出镜头在跑
    y0 = int((0.5 + 0.45 * math.sin(line_phase)) * cell)
    img[max(0, y0 - 1):y0 + 2, :, :] += 70.0
    if ball is not None:
        m = _disc(cell, ball[0], ball[1], r)[..., None]
        img = img * (1 - m) + np.array([238, 240, 235], np.float32)[None, None, :] * m
    return np.clip(img, 0, 255).astype(np.uint8)


def _cell_enh(rng: np.random.Generator, ball: Optional[Tuple[float, float]], r: float,
              cell: int, blob: Optional[Tuple[float, float, float]]) -> np.ndarray:
    """滤波（DoG）轨道的一格：近黑底 + 高频残留 + 球的白斑 + 偶尔一个大白人形。

    大白人形是故意加的：真实滤波图上球员也是白的，且比球大得多。审核页如果把「最亮的
    东西」当成球来判断，在这种图上就会露馅。
    """
    img = np.clip(rng.normal(10, 5.0, (cell, cell)), 0, 255)
    if blob is not None:
        img = np.maximum(img, 150.0 * _disc(cell, blob[0], blob[1], blob[2]))
    if ball is not None:
        img = np.maximum(img, 245.0 * _disc(cell, ball[0], ball[1], r))
    return np.repeat(np.clip(img, 0, 255).astype(np.uint8)[..., None], 3, axis=2)


# ---------------------------------------------------------------- 轨迹


def synth_track(n: int, roi: List[int], seed: int) -> Dict[str, list]:
    """造一条像样的球轨迹，并故意埋进几类错误。

    埋错误是重点：全对的假数据只能验证接口通不通，验证不了审核页到底能不能让人**看见**
    错误。所以这里保证有标偏的帧（标记脱开球）、插值帧、低置信帧和不可见帧。
    """
    rng = np.random.default_rng(seed)
    w, h = roi[2], roi[3]
    # 一段抛物线飞行 + 落地后滚动，比纯随机游走更像真实的球
    t = np.linspace(0, 1, n)
    true_x = 0.12 * w + 0.72 * w * t
    true_y = 0.72 * h - 0.55 * h * np.sin(math.pi * t) * (1 - 0.35 * t)
    true_x += rng.normal(0, 1.2, n)
    true_y += rng.normal(0, 1.2, n)

    # 标注 = 真值 + 亚像素抖动。再挑几帧标偏，偏到肉眼能看出「标记脱开球」
    lab_x, lab_y = true_x + rng.normal(0, 0.8, n), true_y + rng.normal(0, 0.8, n)
    bad = sorted(rng.choice(np.arange(3, n - 3), size=max(2, n // 20), replace=False).tolist())
    for i in bad:
        ang = rng.uniform(0, 2 * math.pi)
        off = rng.uniform(28, 70)
        lab_x[i] += off * math.cos(ang)
        lab_y[i] += off * math.sin(ang)

    interp = sorted(rng.choice(np.arange(2, n - 2), size=max(1, n // 14), replace=False).tolist())
    invisible = sorted(rng.choice(np.arange(2, n - 2), size=max(1, n // 25), replace=False).tolist())
    invisible = [i for i in invisible if i not in interp]

    conf = np.clip(rng.normal(0.78, 0.12, n), 0.05, 0.99)
    for i in interp:
        conf[i] = float(rng.uniform(0.12, 0.35))
    for i in bad:
        conf[i] = float(rng.uniform(0.30, 0.65))

    # 裁切窗中心 = 平滑后的轨迹。滑动平均就够：这里要的是「镜头稳」，不是精确重建
    k = 5
    pad = np.r_[[lab_x[0]] * (k // 2), lab_x, [lab_x[-1]] * (k // 2)]
    sm_x = np.convolve(pad, np.ones(k) / k, mode="valid")
    pad = np.r_[[lab_y[0]] * (k // 2), lab_y, [lab_y[-1]] * (k // 2)]
    sm_y = np.convolve(pad, np.ones(k) / k, mode="valid")

    return {
        "true_x": true_x.tolist(), "true_y": true_y.tolist(),
        "lab_x": lab_x.tolist(), "lab_y": lab_y.tolist(),
        "sm_x": sm_x.tolist(), "sm_y": sm_y.tolist(),
        "conf": conf.tolist(), "interp": interp, "invisible": invisible, "bad": bad,
    }


# ---------------------------------------------------------------- 组包


def build(out: Path, packet_id: str, n: int, cols: int, step: int, fps: float,
          frame_start: int, roi: List[int], pano: List[int], seed: int,
          cell: int = CELL, ball_r: float = 5.0) -> Dict[str, object]:
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed + 7)
    tr = synth_track(n, roi, seed)
    rows = math.ceil(n / cols)
    pano_w = pano[0]

    sheet_raw = np.zeros((rows * cell, cols * cell, 3), np.uint8)
    sheet_enh = np.zeros((rows * cell, cols * cell, 3), np.uint8)

    # 固定区域轨道：轨迹包围盒留边后 < 640 px 才出，否则球在格子里只有几个像素，不如看 overview
    xs, ys = tr["lab_x"], tr["lab_y"]
    bx0, bx1 = min(xs) - 40, max(xs) + 40
    by0, by1 = min(ys) - 40, max(ys) + 40
    side = max(bx1 - bx0, by1 - by0)
    want_region = side < 640
    rect = None
    sheet_region = None
    if want_region:
        rcx, rcy = (bx0 + bx1) / 2, (by0 + by1) / 2
        rect = [rcx - side / 2, rcy - side / 2, side, side]
        sheet_region = np.zeros((rows * cell, cols * cell, 3), np.uint8)
    r_scale = (cell / side) if want_region else None

    frames: List[dict] = []
    labels: List[dict] = []
    ov_w = 1200
    ov_scale = ov_w / roi[2]
    ov_h = max(1, int(round(roi[3] * ov_scale)))
    ov = np.clip(np.random.default_rng(seed + 3).normal(8, 4.0, (ov_h, ov_w)), 0, 255)

    for i in range(n):
        fi = frame_start + i * step
        vis = i not in tr["invisible"]
        cx_l, cy_l = tr["sm_x"][i], tr["sm_y"][i]
        tx, ty = tr["true_x"][i], tr["true_y"][i]
        lx, ly = tr["lab_x"][i], tr["lab_y"][i]

        # 球画在它相对裁切中心的真实偏移处；标记画在标注偏移处。两者不同 = 这一帧标错了
        ball = ((tx - cx_l) + cell / 2, (ty - cy_l) + cell / 2) if vis else None
        if ball is not None and not (-cell <= ball[0] <= 2 * cell and -cell <= ball[1] <= 2 * cell):
            ball = None
        blob = None
        if rng.random() < 0.25:  # 四分之一的格子里放个「球员」
            blob = (rng.uniform(0, cell), rng.uniform(0, cell), rng.uniform(14, 26))

        r_, c_ = divmod(i, cols)
        y0, x0 = r_ * cell, c_ * cell
        sheet_raw[y0:y0 + cell, x0:x0 + cell] = _cell_raw(rng, ball, ball_r, cell, i * 0.21)
        sheet_enh[y0:y0 + cell, x0:x0 + cell] = _cell_enh(rng, ball, ball_r, cell, blob)

        rx = ry = None
        if want_region:
            bxy = (((tx - rect[0]) * r_scale), ((ty - rect[1]) * r_scale)) if vis else None
            sheet_region[y0:y0 + cell, x0:x0 + cell] = _cell_raw(
                rng, bxy, max(2.0, ball_r * r_scale), cell, i * 0.21)
            if vis:
                rx = round((lx - rect[0]) * r_scale, 2)
                ry = round((ly - rect[1]) * r_scale, 2)

        ox = oy = None
        if vis:
            ox, oy = lx * ov_scale, ly * ov_scale
            # overview 是 DoG 时间最大投影：整条飞行轨迹显成一条亮带。带子要比球在
            # 这个缩放下的直径宽一些——真实的最大投影会把相邻帧的球连成条，太细的话
            # 「折线是否严丝合缝压在亮带上」这个信号就看不出来了
            yy, xx = np.mgrid[0:ov_h, 0:ov_w]
            band = np.hypot(xx - tx * ov_scale, yy - ty * ov_scale) < max(3.0, 2 * ball_r * ov_scale)
            ov[band] = 230

        motion = round(math.hypot(lx - tr["lab_x"][i - 1], ly - tr["lab_y"][i - 1]), 1) if i else 0.0
        pano_x = ((roi[0] + lx) % pano_w) if vis else None
        pano_y = (roi[1] + ly) if vis else None
        stages = (["coarse", "refine1", "refine2"] if i == 0 else ["predict", "refine2"])
        if i in tr["interp"]:
            stages = ["interp"]
        elif tr["conf"][i] < 0.45:
            stages = stages + ["streak"]

        frames.append({
            "i": i, "frame_idx": fi,
            "pano_x": round(pano_x, 2) if pano_x is not None else None,
            "pano_y": round(pano_y, 2) if pano_y is not None else None,
            "cx": round((roi[0] + cx_l) % pano_w, 2), "cy": round(roi[1] + cy_l, 2),
            "dx": round(lx - cx_l, 2) if vis else None,
            "dy": round(ly - cy_l, 2) if vis else None,
            "rx": rx, "ry": ry,
            "ox": round(ox, 2) if ox is not None else None,
            "oy": round(oy, 2) if oy is not None else None,
            "conf": round(tr["conf"][i], 3),
            "interp": i in tr["interp"],
            "visible": vis, "occluded": False,
            "blurred": bool(motion > 30),
            "stages": stages, "motion_px": motion,
        })
        labels.append({
            "frame_idx": fi,
            "pano_x": round(pano_x, 2) if pano_x is not None else None,
            "pano_y": round(pano_y, 2) if pano_y is not None else None,
            "visible": vis, "occluded": False, "blurred": bool(motion > 30),
            "conf": round(tr["conf"][i], 3), "interp": i in tr["interp"],
            "stages": stages,
        })

    # 标注折线叠在亮带上。压不上就是标错了，一眼定生死
    ov_img = Image.fromarray(np.repeat(np.clip(ov, 0, 255).astype(np.uint8)[..., None], 3, axis=2))
    from PIL import ImageDraw

    dr = ImageDraw.Draw(ov_img)
    pts = [(f["ox"], f["oy"]) for f in frames if f["ox"] is not None]
    for a, b in zip(pts, pts[1:]):
        dr.line([a, b], fill=(96, 165, 250), width=2)
    for p in pts:
        dr.ellipse([p[0] - 2, p[1] - 2, p[0] + 2, p[1] + 2], outline=(74, 222, 128))

    tracks: Dict[str, dict] = {}
    for name, sheet, pane in (("follow", sheet_raw, "raw"), ("follow_enh", sheet_enh, "enh")):
        f = f"{name}.webp"
        Image.fromarray(sheet).save(out / f, "WEBP", quality=88, method=4)
        tracks[name] = {"file": f, "cols": cols, "rows": rows, "cell": cell,
                        "pane": pane, "mode": "follow"}
    if want_region:
        Image.fromarray(sheet_region).save(out / "region.webp", "WEBP", quality=88, method=4)
        tracks["region"] = {
            "file": "region.webp", "cols": cols, "rows": rows, "cell": cell,
            "pane": "raw", "mode": "region",
            "rect": [round(v, 1) for v in rect], "scale": round(r_scale, 6),
        }
    ov_img.save(out / "overview.webp", "WEBP", quality=86, method=4)

    manifest = {
        "version": "1.0", "kind": "ball_review_bundle",
        "packet_id": packet_id, "fps": fps, "step": step, "cell": cell,
        "n_frames": n, "roi": roi, "pano_size": pano,
        "tracks": tracks,
        "overview": {"file": "overview.webp", "width": ov_w, "height": ov_h,
                     "roi": roi, "scale": round(ov_scale, 6)},
        "frames": frames,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))

    zpath = out / "bundle.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
        for name in ["manifest.json", "overview.webp"] + [t["file"] for t in tracks.values()]:
            zf.write(out / name, name)

    meta = {
        "packet_id": packet_id, "group": packet_id.split("__")[0],
        "stem": packet_id.split("__")[1] if "__" in packet_id else "stem",
        "tag": packet_id.rsplit("__", 1)[-1],
        "start_sec": float(frame_start / fps), "end_sec": float((frame_start + n * step) / fps),
        "fps": fps, "pano_size": pano, "roi": roi,
        "frame_start": frame_start, "frame_end": frame_start + n * step,
        "clip_frames": n * step, "step": step,
        "targets": [f["frame_idx"] for f in frames],
        "sphere_transform": None, "sampling": "random",
        "produced_at": "2026-09-10T00:00:00", "version": "1.0", "kind": "ball_gt_packet",
        "decim": 1, "clip_fps": 30.0,
        "notes": {"fake": True},
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))

    vis_rows = [r for r in labels if r["visible"]]
    result = {
        "version": "1.0", "kind": "ball_gt", "packet_id": packet_id,
        "group": meta["group"], "stem": meta["stem"], "tag": meta["tag"],
        "annotator": "fake-ai-client", "source": "ai",
        "workflow": "erp_ball_v1", "model_used": "glm-4.6v-flash",
        "fps": fps, "pano_size": pano, "roi": roi,
        "frame_start": meta["frame_start"], "frame_end": meta["frame_end"], "step": step,
        "n_targets": n, "n_labeled": len(labels), "n_visible": len(vis_rows),
        "n_occluded": 0, "n_blurred": sum(1 for r in labels if r["blurred"]),
        "n_interp": len(tr["interp"]),
        "elapsed_sec": round(12.0 * n / 100 * 60, 1), "n_vlm_calls": int(1.8 * n),
        "labels": labels,
    }
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False))
    thumbs = write_thumbs(out)

    return {
        "out": out, "packet_id": packet_id, "manifest": manifest, "meta": meta,
        "result": result, "bundle_zip": zpath,
        "bundle_bytes": zpath.stat().st_size,
        "thumbs": thumbs,
        "planted_bad_frames": [frames[i]["frame_idx"] for i in tr["bad"]],
        "has_region": want_region,
    }


def write_thumbs(out: Path) -> Path:
    """冒烟用的缩略件占位：两张小 JPEG + 两个假 mp4 头。"""
    td = Path(out) / "thumbs"
    td.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 8), (24, 24, 24)).save(td / "scan_dog.jpg", "JPEG", quality=80)
    Image.new("RGB", (16, 8), (40, 80, 180)).save(td / "scan_time.jpg", "JPEG", quality=80)
    stub = b"\x00\x00\x00\x18ftypmp42"
    (td / "clip.mp4").write_bytes(stub)
    (td / "dog.mp4").write_bytes(stub)
    return td


def parse_args():
    p = argparse.ArgumentParser(description="造一个符合规格的假审核包（服务端自测用）")
    p.add_argument("--out", default="/tmp/fake_bundle", help="输出目录")
    p.add_argument("--packet-id", default="fakegrp__fakestem__0010_0020")
    p.add_argument("--n-frames", type=int, default=60, help="目标帧数（真实包约 100）")
    p.add_argument("--cols", type=int, default=14, help="sprite 每行几格")
    p.add_argument("--cell", type=int, default=CELL)
    p.add_argument("--step", type=int, default=3)
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--frame-start", type=int, default=300)
    p.add_argument("--roi", default="1200,900,3600,1700", help="x,y,w,h")
    p.add_argument("--pano", default="7680,3840", help="w,h")
    p.add_argument("--ball-r", type=float, default=5.0, help="球半径（真实 8–30 px 直径）")
    p.add_argument("--seed", type=int, default=0)
    return p.parse_args()


def run(a) -> Dict[str, object]:
    info = build(
        Path(a.out), a.packet_id, a.n_frames, a.cols, a.step, a.fps, a.frame_start,
        [int(v) for v in a.roi.split(",")], [int(v) for v in a.pano.split(",")],
        a.seed, cell=a.cell, ball_r=a.ball_r,
    )
    m = info["manifest"]
    print(f"[out]   {info['out']}", flush=True)
    print(f"[pkt]   {info['packet_id']}  {a.n_frames} 帧  "
          f"sprite {m['tracks']['follow']['cols']}×{m['tracks']['follow']['rows']} × {a.cell}px", flush=True)
    print(f"[track] {', '.join(sorted(m['tracks']))}"
          + ("" if info["has_region"] else "（轨迹包围盒过大，未出 region 轨道）"), flush=True)
    print(f"[zip]   {info['bundle_zip']}  {info['bundle_bytes'] / 1e6:.2f} MB", flush=True)
    print(f"[bad]   故意标偏的帧：{info['planted_bad_frames']}", flush=True)
    print(f"[hint]  register 用 meta.json，submit 用 result.json + bundle.zip", flush=True)
    return info


if __name__ == "__main__":
    run(parse_args())
