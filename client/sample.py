#!/usr/bin/env python3
"""事件驱动的目标帧采样：调用预算花在踢球 / 落地，不均摊到每一帧。

人工标注的实测（锚点留一交叉）：
- 间隔 2–3 个目标帧线性插值中位 3–4 px，90%/82% ≤20 px
- 间隔 ≥6 崩掉（样条尤其会过冲）
- 锚点密度随球速差 50 倍：静止包 2 个锚点，有事件的包 93 个

自适应判据是加速度。锚点处加速度中位 7–10 px、事件段 p90 27–70 px。
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

import numpy as np

# 间隔 2–3 是安全工作点；4 开始劣化
SPARSE_STRIDE = 2
# 事件邻域：加速度超阈值的锚点前后各扩这么多目标帧，密标（间隔 1）
EVENT_PAD = 2
# px / 目标帧。实测事件 p90 约 27–70，25 能抓住踢球/落地，放过匀速弹道
ACC_EVENT = 25.0


def _accel(pts: Sequence[Tuple[int, float, float]]) -> List[float]:
    """相邻三点的加速度幅值，长度 = len(pts)；两端用 0。"""
    n = len(pts)
    out = [0.0] * n
    if n < 3:
        return out
    for i in range(1, n - 1):
        f0, x0, y0 = pts[i - 1]
        f1, x1, y1 = pts[i]
        f2, x2, y2 = pts[i + 1]
        dt01 = max(float(f1 - f0), 1.0)
        dt12 = max(float(f2 - f1), 1.0)
        vx1, vy1 = (x1 - x0) / dt01, (y1 - y0) / dt01
        vx2, vy2 = (x2 - x1) / dt12, (y2 - y1) / dt12
        # 再除以中点两侧的平均时间，得到「每目标帧」的加速度
        dt = 0.5 * (dt01 + dt12)
        out[i] = float(np.hypot(vx2 - vx1, vy2 - vy1) / max(dt, 1.0) * dt)
    return out


def event_frames(
    anchors: Sequence[Tuple[int, float, float]],
    *,
    acc_thr: float = ACC_EVENT,
    pad: int = EVENT_PAD,
    step: int = 3,
) -> List[int]:
    """加速度超阈值的锚点，连同前后 `pad` 个目标帧，按帧号去重排序。"""
    if len(anchors) < 3:
        return []
    pts = sorted((int(f), float(x), float(y)) for f, x, y in anchors)
    acc = _accel(pts)
    st = max(1, int(step))
    hot: set = set()
    for (fi, _x, _y), a in zip(pts, acc):
        if a < acc_thr:
            continue
        for k in range(-int(pad), int(pad) + 1):
            hot.add(fi + k * st)
    return sorted(hot)


def schedule(
    targets: Sequence[int],
    anchors: Sequence[Tuple[int, float, float]] = (),
    *,
    stride: int = SPARSE_STRIDE,
    acc_thr: float = ACC_EVENT,
    pad: int = EVENT_PAD,
    step: int = 3,
) -> List[int]:
    """决定这一包该问模型的目标帧。

    - 锚点 < 3：冷启动，每个目标帧都问（调用方自己会用种子跳过已标的）
    - 否则：稀疏网格（每 `stride` 个目标帧）∪ 事件邻域
    """
    ts = [int(f) for f in targets]
    if len(anchors) < 3:
        return list(ts)
    stride = max(1, int(stride))
    sparse = set(ts[::stride])
    if ts:
        sparse.add(ts[0])
        sparse.add(ts[-1])
    hot = set(event_frames(anchors, acc_thr=acc_thr, pad=pad, step=step))
    want = sparse | (hot & set(ts))
    return [f for f in ts if f in want]


def should_query(
    frame_idx: int,
    targets: Sequence[int],
    anchors: Sequence[Tuple[int, float, float]],
    **kw,
) -> bool:
    return int(frame_idx) in set(schedule(targets, anchors, **kw))


def skippable(
    frame_idx: int,
    targets: Sequence[int],
    anchors: Iterable[Tuple[int, float, float]],
    **kw,
) -> bool:
    """True = 这一帧可以留给线性插值，不必花模型调用。"""
    return not should_query(frame_idx, targets, list(anchors), **kw)
