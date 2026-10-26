#!/usr/bin/env python3
"""从 clip.mp4 派生 dog / scan / 缩略件。产包端算一次，标注端只解码。

DoG 编码参数由实验定档（`/tmp/dog_encode_eval`，100 个 GT 可见点）：
CRF 16 + deblock=-3,-3 + tune stillimage 的 top-8 blob 召回与 JPEG q95 持平
（0.420→0.420），白斑峰值保留 99.7%。CRF 12 体积翻倍没有召回收益。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enhance import DEFAULT_LAGS, build_background, render_enhanced, sample_indices, to_gray  # noqa: E402
from packet import (  # noqa: E402
    CLIP_FPS,
    DOG_NAME,
    SCAN_DOG_NAME,
    SCAN_TIME_NAME,
    THUMB_DIR,
    THUMB_SIDE,
)

BG_SAMPLES = 24
SCAN_JPEG_Q = 90
DOG_CRF = 16
THUMB_CLIP_CRF = 30

DOG_X264 = [
    "-c:v", "libx264", "-crf", str(DOG_CRF), "-preset", "veryfast",
    "-tune", "stillimage", "-g", "30",
    "-x264-params", "deblock=-3,-3",
    "-pix_fmt", "yuv420p", "-an",
]


def maxpool_resize(gray: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    """缩小滤波图只能 max-pool，面积平均会吃掉 8 px 白斑。"""
    h, w = gray.shape[:2]
    if out_w >= w and out_h >= h:
        return cv2.resize(gray, (out_w, out_h), interpolation=cv2.INTER_NEAREST)
    f = max(w / max(out_w, 1), h / max(out_h, 1))
    k = 2 * int(np.ceil(f / 2)) + 1
    img = cv2.dilate(gray, np.ones((k, k), np.uint8)) if k > 1 else gray
    return cv2.resize(img, (out_w, out_h), interpolation=cv2.INTER_NEAREST)


def thumb_size(w: int, h: int, side: int = THUMB_SIDE) -> Tuple[int, int]:
    scale = side / max(w, h, 1)
    tw = max(2, int(round(w * scale)) // 2 * 2)
    th = max(2, int(round(h * scale)) // 2 * 2)
    return tw, th


def _open_gray_encoder(path: Path, w: int, h: int, fps: float) -> subprocess.Popen:
    path.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "rawvideo", "-pix_fmt", "gray",
            "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
            *DOG_X264, str(path),
        ],
        stdin=subprocess.PIPE,
    )


def _even(n: int) -> int:
    return n if n % 2 == 0 else n + 1


def hue_bgr(gray: np.ndarray, t: float) -> np.ndarray:
    """t∈[0,1]：早=蓝(H=120) → 晚=红(H=0)。V=灰度。"""
    hue = int(round(120.0 * (1.0 - max(0.0, min(1.0, t)))))
    hsv = np.empty((*gray.shape, 3), np.uint8)
    hsv[..., 0] = hue
    hsv[..., 1] = 255
    hsv[..., 2] = gray
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def encode_thumb_clip(clip: Path, dest: Path, side: int = THUMB_SIDE, crf: int = THUMB_CLIP_CRF) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale='if(gte(iw,ih),{side},-2)':'if(gte(iw,ih),-2,{side})',"
        "setsar=1"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(clip),
            "-vf", vf, "-c:v", "libx264", "-crf", str(crf),
            "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", str(dest),
        ],
        check=True,
    )


def derive_from_clip(
    clip: Path,
    out_dir: Path,
    *,
    fps: float = CLIP_FPS,
    thumb_side: int = THUMB_SIDE,
    status=None,
) -> Dict[str, Path]:
    """稀疏趟算背景，流式趟写出 dog / thumb/dog / 两张 scan。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir = out_dir / THUMB_DIR
    thumb_dir.mkdir(exist_ok=True)

    cap = cv2.VideoCapture(str(clip))
    if not cap.isOpened():
        raise RuntimeError(f"打不开 clip: {clip}")
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if n <= 0 or w <= 0 or h <= 0:
        ok, fr0 = cap.read()
        if not ok:
            cap.release()
            raise RuntimeError("clip 解码不出任何帧")
        h, w = fr0.shape[:2]
        n = 1
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    bg_ids = set(sample_indices(n, BG_SAMPLES))
    samples: List[np.ndarray] = []
    i = 0
    if status:
        status.set(state="bg", total=n, done=0, msg="采样背景…")
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i in bg_ids:
            samples.append(to_gray(fr))
        i += 1
        if status:
            status.bump()
    cap.release()
    if not samples:
        raise RuntimeError("背景采样为空")
    bg = build_background(samples)
    n = max(n, i)

    tw, th = thumb_size(w, h, thumb_side)
    ew, eh = _even(w), _even(h)
    tew, teh = _even(tw), _even(th)

    dog_path = out_dir / DOG_NAME
    thumb_dog_path = thumb_dir / DOG_NAME
    enc = _open_gray_encoder(dog_path, ew, eh, fps)
    enc_t = _open_gray_encoder(thumb_dog_path, tew, teh, fps)
    assert enc.stdin and enc_t.stdin

    scan_dog = np.zeros((h, w), np.uint8)
    scan_time = np.zeros((h, w, 3), np.uint8)
    hist: Dict[int, np.ndarray] = {}

    cap = cv2.VideoCapture(str(clip))
    i = 0
    if status:
        status.set(state="dog", total=n, done=0, msg="生成 DoG…")
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            g = to_gray(fr)
            hist_now = {i - lag: hist[i - lag] for lag in DEFAULT_LAGS if (i - lag) in hist}
            dog = render_enhanced(fr, bg, hist_now, i)
            hist[i] = g
            drop = i - max(DEFAULT_LAGS) - 1
            if drop in hist:
                del hist[drop]

            scan_dog = np.maximum(scan_dog, dog)
            t = i / max(n - 1, 1)
            scan_time = np.maximum(scan_time, hue_bgr(dog, t))

            if dog.shape[1] != ew or dog.shape[0] != eh:
                pad = np.zeros((eh, ew), np.uint8)
                pad[: dog.shape[0], : dog.shape[1]] = dog
                payload = pad
            else:
                payload = dog
            enc.stdin.write(payload.tobytes())

            small = maxpool_resize(dog, tw, th)
            if small.shape[1] != tew or small.shape[0] != teh:
                pad = np.zeros((teh, tew), np.uint8)
                pad[: small.shape[0], : small.shape[1]] = small
                small = pad
            enc_t.stdin.write(small.tobytes())
            i += 1
            if status:
                status.bump()
    finally:
        cap.release()
        enc.stdin.close()
        enc_t.stdin.close()
        rc1, rc2 = enc.wait(), enc_t.wait()
        if rc1 != 0 or rc2 != 0:
            raise RuntimeError(f"ffmpeg 编 DoG 失败 rc={rc1}/{rc2}")

    jpg = [int(cv2.IMWRITE_JPEG_QUALITY), SCAN_JPEG_Q]
    scan_dog_path = out_dir / SCAN_DOG_NAME
    scan_time_path = out_dir / SCAN_TIME_NAME
    cv2.imwrite(str(scan_dog_path), scan_dog, jpg)
    cv2.imwrite(str(scan_time_path), scan_time, jpg)
    cv2.imwrite(str(thumb_dir / SCAN_DOG_NAME), maxpool_resize(scan_dog, tw, th), jpg)
    cv2.imwrite(str(thumb_dir / SCAN_TIME_NAME), _maxpool_bgr(scan_time, tw, th), jpg)

    thumb_clip = thumb_dir / "clip.mp4"
    encode_thumb_clip(clip, thumb_clip, thumb_side)
    return {
        "dog": dog_path,
        "scan_dog": scan_dog_path,
        "scan_time": scan_time_path,
        "thumb_clip": thumb_clip,
        "thumb_dog": thumb_dog_path,
        "thumb_scan_dog": thumb_dir / SCAN_DOG_NAME,
        "thumb_scan_time": thumb_dir / SCAN_TIME_NAME,
    }


def _maxpool_bgr(bgr: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    chans = cv2.split(bgr)
    return cv2.merge([maxpool_resize(c, out_w, out_h) for c in chans])


def decimate_clip(src: Path, dest: Path, decim: int) -> None:
    """精确按帧抽取：`select not(mod(n, decim))`，再重置时间轴。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # FRAME_RATE is the source rate (e.g. 59.94), not the packet rate.
    # Retiming to it after dropping frames made 300-frame clips last 5s while
    # metadata and DoG videos used 30fps/10s. Keep every selected frame at 30fps.
    vf = f"select='not(mod(n\\,{decim}))',setpts=N/({CLIP_FPS}*TB)"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(src),
            "-vf", vf, "-r", str(CLIP_FPS), "-c:v", "libx264", "-crf", "16",
            "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", str(dest),
        ],
        check=True,
    )


def encode_indexed_clip(
    src: Path,
    dest: Path,
    frame_indices: Sequence[int],
    *,
    fps: float = 1.0,
    crf: int = 18,
    crop: Optional[Tuple[int, int, int, int]] = None,
) -> None:
    """把指定源帧（clip 内 0-based）按给定 fps 重编。

    M3 的 fps 上限是 5，所以把 100 个目标帧编成 1 fps / 100 秒，
    模型说「第 t 秒」= 第 t 个目标帧。`crop=(x,y,w,h)` 先裁再编，给分块保分辨率用。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    ids = [int(i) for i in frame_indices]
    if not ids:
        raise ValueError("frame_indices 为空")
    expr = "+".join(f"eq(n\\,{i})" for i in ids)
    vf = f"select='{expr}'"
    if crop is not None:
        x, y, w, h = (int(v) for v in crop)
        vf += f",crop={w}:{h}:{x}:{y}"
    vf += f",setpts=N/{float(fps)}/TB"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(src),
            "-vf", vf, "-r", str(fps),
            "-c:v", "libx264", "-crf", str(crf),
            "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", str(dest),
        ],
        check=True,
    )
