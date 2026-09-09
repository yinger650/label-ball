#!/usr/bin/env python3
"""标注用的画面增强：把飞行中的球变成一眼可见的白斑。

只服务于**显示**，不是检测器。检测侧的权威实现在
`Research/ball_tbd/cand_erp_roi.py`；这里刻意独立实现，是为了让 `Research/ball_gt/`
能作为单个目录分发给标注员，不必拖上整个仓库。

为什么需要它：球在原生 ERP 只有 8–30 px，在**静态帧**上人眼几乎找不到——这也正是
WASB / FootAndBall 这类现成检测器失手的原因。而球飞行时有动态模糊，在多间隔残差图上
是一块孤立白斑，人眼一眼就能锁定。

两步：

1. **多间隔残差** `max(|I_t − B|, |I_t − I_{t−1}|, |I_t − I_{t−3}|)`。
   单用 t−1 不够：球帧间位移中位约 24 px 而球径 8–30 px，相邻帧可能完全不重叠，
   快球会碎成两个亮斑；t−3 给出更长基线。B 是稀疏中值背景，负责让**静止**的球
   也留下残差（纯帧差在静止球上恒为零）。
2. **多尺度 DoG**。全局阈值不可用：阈值高则球的残差低于球员造成的高阈值而消失，
   阈值低则球的亮斑与球员腿部亮斑连成一片。DoG 按尺度响应，与全局强度无关。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import cv2
import numpy as np

DEFAULT_SIGMAS = (1.5, 2.5, 4.0)
DEFAULT_LAGS = (1, 3)


def to_gray(bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def build_background(grays: Sequence[np.ndarray]) -> np.ndarray:
    """稀疏采样的逐像素中值背景。中值而非均值：球员穿过时不会在背景里留下拖影。"""
    stack = np.stack(list(grays), axis=0)
    return np.median(stack, axis=0).astype(np.uint8)


def gain_compensate(cur: np.ndarray, bg: np.ndarray) -> np.ndarray:
    """按中值比例对齐曝光。相机自动曝光会让整帧亮度漂移，不补偿则背景差分整片发亮。"""
    mc, mb = float(np.median(cur)), float(np.median(bg))
    if mc < 1.0:
        return bg
    return np.clip(bg.astype(np.float32) * (mc / max(mb, 1.0)), 0, 255).astype(np.uint8)


def residual(
    cur_gray: np.ndarray,
    bg: Optional[np.ndarray],
    history: Dict[int, np.ndarray],
    frame_idx: int,
    lags: Sequence[int] = DEFAULT_LAGS,
) -> np.ndarray:
    acc = np.zeros_like(cur_gray)
    if bg is not None:
        acc = cv2.absdiff(cur_gray, gain_compensate(cur_gray, bg))
    for lag in lags:
        prev = history.get(frame_idx - lag)
        if prev is not None:
            acc = np.maximum(acc, cv2.absdiff(cur_gray, prev))
    return acc


def dog_response(r: np.ndarray, sigmas: Sequence[float] = DEFAULT_SIGMAS) -> np.ndarray:
    """多尺度 DoG 响应的逐像素最大值（尺度归一化后可跨 sigma 比较）。"""
    r32 = r.astype(np.float32)
    best: Optional[np.ndarray] = None
    for s in sigmas:
        d = (cv2.GaussianBlur(r32, (0, 0), s) - cv2.GaussianBlur(r32, (0, 0), s * 1.6)) * s
        best = d if best is None else np.maximum(best, d)
    return best if best is not None else r32


def render_enhanced(
    cur_bgr: np.ndarray,
    bg: Optional[np.ndarray],
    history: Dict[int, np.ndarray],
    frame_idx: int,
    lags: Sequence[int] = DEFAULT_LAGS,
    sigmas: Sequence[float] = DEFAULT_SIGMAS,
    gain: float = 6.0,
) -> np.ndarray:
    """返回可直接显示的灰度增强图（球 = 白斑）。

    `gain` 作用在 DoG 响应上而不是残差上：DoG 已经把大面积的球员躯干残差压低
    （它们的尺度远大于 sigma），此时放大能提升球的对比度而不会把画面整片打白。
    """
    g = to_gray(cur_bgr)
    r = residual(g, bg, history, frame_idx, lags)
    d = dog_response(r, sigmas)
    return np.clip(d * gain, 0, 255).astype(np.uint8)


def sample_indices(n: int, k: int) -> List[int]:
    """在 [0, n) 上均匀取 k 个下标，用于稀疏背景估计。"""
    if n <= k:
        return list(range(n))
    step = n / float(k)
    return [min(n - 1, int(round(i * step))) for i in range(k)]
