#!/usr/bin/env python3
"""人工稀疏标注：未标的目标帧在提交时用 track.interpolate 补坐标。

明确标成不可见的帧保持不可见、不插值。首尾外侧不外推。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from client.prep import pano_to_roi, roi_to_pano
from client.track import interpolate

# 人工头尾两帧即可插完整段（按时间轴下标计空洞，覆盖整段解码帧）。
HUMAN_INTERP_GAP = 2000


def is_anchor(rec: Optional[dict]) -> bool:
    return bool(rec and rec.get("visible") and rec.get("pano_x") is not None and rec.get("pano_y") is not None)


def visible_anchors(labels: Dict[str, dict]) -> List[int]:
    out: List[int] = []
    for key, rec in labels.items():
        if not is_anchor(rec):
            continue
        fi = rec.get("frame_idx")
        if fi is None:
            try:
                fi = int(key)
            except (TypeError, ValueError):
                continue
        out.append(int(fi))
    return sorted(set(out))


def all_explicit_invisible(labels: Dict[str, dict], targets: Sequence[int]) -> bool:
    """每一帧都被人标成不可见（整段没球）。"""
    want = [int(t) for t in targets]
    if not want:
        return False
    return all(
        (labels.get(str(t)) or {}).get("visible") is False
        for t in want
    )


def seed_submit_error(
    labels: Dict[str, dict],
    targets: Sequence[int],
    min_visible: int = 2,
) -> Optional[str]:
    """预标能不能交。2 个可见点，或整段明确不可见，都可以。"""
    n = len(visible_anchors(labels))
    if n >= int(min_visible):
        return None
    if all_explicit_invisible(labels, targets):
        return None
    return (
        f"预标至少要 {int(min_visible)} 个可见点，或把整段标成不可见后再提交。"
        f"现在可见 {n}"
    )


def validate_sparse_anchors(labels: Dict[str, dict], targets: Sequence[int]) -> Optional[str]:
    """任意两帧可见点即可当锚点（不必落在待修列表里）。整段不可见算通过。"""
    want = [int(t) for t in targets]
    if not want:
        return "没有目标帧"
    if all_explicit_invisible(labels, want):
        return None
    anchors = visible_anchors(labels)
    labeled_want = [t for t in want if is_anchor(labels.get(str(t)))]
    if len(want) == 1:
        if labeled_want or (labels.get(str(want[0])) or {}).get("visible") is False:
            return None
        return "这一帧还没标"
    if len(anchors) < 2:
        return (
            f"至少要 2 个可见点（在画面上点头尾球心即可插值，"
            f"[ ] 只圈范围不记点），现在 {len(anchors)}"
        )
    lo, hi = anchors[0], anchors[-1]
    outside = [
        t for t in want
        if t < lo or t > hi
        if not is_anchor(labels.get(str(t)))
        if (labels.get(str(t)) or {}).get("visible") is not False
    ]
    if outside:
        return (
            f"还有 {len(outside)} 帧在头尾锚点外侧，无法插值。"
            "请把两个可见点标在待修段两端（或时间轴更外侧）"
        )
    return None


def fill_sparse_labels(
    labels: Dict[str, dict],
    targets: Sequence[int],
    roi: Sequence[int],
    pano_w: int,
    fps: float,
    *,
    max_interp_gap: int = HUMAN_INTERP_GAP,
) -> Tuple[Dict[str, dict], List[int]]:
    """对未标目标帧插值补坐标，返回 (新 labels, 被插值的 frame_idx 列表)。

    锚点可以是任意已标可见帧（含时间轴头尾、不在待修列表里的帧）。
    已被人标成不可见的帧保持不可见。已有可见点的帧不动。
    """
    want = [int(t) for t in targets]
    want_set = set(want)
    timeline = sorted(set(want) | set(visible_anchors(labels)))
    if not timeline:
        return dict(labels), []
    roi_l = [int(x) for x in roi]
    xs: List[Optional[float]] = []
    ys: List[Optional[float]] = []
    explicit_invis = set()
    for t in timeline:
        rec = labels.get(str(t))
        if rec and rec.get("visible") is False:
            explicit_invis.add(t)
            xs.append(None)
            ys.append(None)
        elif is_anchor(rec):
            rx, ry = pano_to_roi(float(rec["pano_x"]), float(rec["pano_y"]), roi_l, int(pano_w))
            xs.append(rx)
            ys.append(ry)
        else:
            xs.append(None)
            ys.append(None)

    xo, yo, filled = interpolate(
        timeline, xs, ys, fps=float(fps or 30.0), max_interp_gap=int(max_interp_gap),
    )
    out = dict(labels)
    filled_ids: List[int] = []
    for i, t in enumerate(timeline):
        if t not in want_set:
            continue
        if t in explicit_invis:
            continue
        if is_anchor(out.get(str(t))):
            continue
        if not filled[i] or xo[i] is None or yo[i] is None:
            continue
        px, py = roi_to_pano(float(xo[i]), float(yo[i]), roi_l, int(pano_w))
        out[str(t)] = {
            "frame_idx": t,
            "visible": True,
            "pano_x": px,
            "pano_y": py,
            "interp": True,
            "occluded": False,
            "blurred": False,
        }
        filled_ids.append(t)
    return out, filled_ids
