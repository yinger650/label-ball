#!/usr/bin/env python3
"""稀疏标注插值：未标帧补上、不可见不插、空洞太大留空。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from client.sparse import (  # noqa: E402
    all_explicit_invisible,
    fill_sparse_labels,
    seed_submit_error,
    validate_sparse_anchors,
)


ROI = [100, 50, 800, 400]
PANO_W = 7680
FPS = 30.0


def test_fill_middle_frames():
    targets = [10, 13, 16, 19, 22]
    labels = {
        "10": {"frame_idx": 10, "visible": True, "pano_x": 200.0, "pano_y": 80.0},
        "22": {"frame_idx": 22, "visible": True, "pano_x": 320.0, "pano_y": 140.0},
    }
    err = validate_sparse_anchors(labels, targets)
    assert err is None, err
    out, filled = fill_sparse_labels(labels, targets, ROI, PANO_W, FPS, max_interp_gap=30)
    assert filled == [13, 16, 19], filled
    for fi in filled:
        rec = out[str(fi)]
        assert rec["interp"] is True
        assert rec["visible"] is True
        assert rec["pano_x"] is not None
    # 锚点不动
    assert out["10"]["pano_x"] == 200.0
    assert out["22"]["pano_x"] == 320.0


def test_invisible_not_filled():
    targets = [10, 13, 16, 19]
    labels = {
        "10": {"frame_idx": 10, "visible": True, "pano_x": 200.0, "pano_y": 80.0},
        "13": {"frame_idx": 13, "visible": False},
        "19": {"frame_idx": 19, "visible": True, "pano_x": 280.0, "pano_y": 100.0},
    }
    out, filled = fill_sparse_labels(labels, targets, ROI, PANO_W, FPS)
    assert 13 not in filled
    assert out["13"]["visible"] is False
    assert 16 in filled


def test_need_anchors():
    targets = [10, 13, 16]
    labels = {"10": {"frame_idx": 10, "visible": True, "pano_x": 1, "pano_y": 2}}
    err = validate_sparse_anchors(labels, targets)
    assert err and "2 个" in err
    labels["16"] = {"frame_idx": 16, "visible": True, "pano_x": 3, "pano_y": 4}
    assert validate_sparse_anchors(labels, targets) is None


def test_all_invisible_ok():
    targets = [10, 13, 16]
    labels = {str(t): {"frame_idx": t, "visible": False} for t in targets}
    assert all_explicit_invisible(labels, targets)
    assert validate_sparse_anchors(labels, targets) is None
    assert seed_submit_error(labels, targets) is None
    labels["10"]["visible"] = True
    labels["10"]["pano_x"] = 1
    labels["10"]["pano_y"] = 2
    err = seed_submit_error(labels, targets)
    assert err and "不可见" in err
    labels["16"] = {"frame_idx": 16, "visible": True, "pano_x": 3, "pano_y": 4}
    assert seed_submit_error(labels, targets) is None


def test_gap_too_wide():
    targets = list(range(0, 40))
    labels = {
        "0": {"frame_idx": 0, "visible": True, "pano_x": 200.0, "pano_y": 80.0},
        "39": {"frame_idx": 39, "visible": True, "pano_x": 400.0, "pano_y": 120.0},
    }
    out, filled = fill_sparse_labels(labels, targets, ROI, PANO_W, FPS, max_interp_gap=8)
    assert filled == [], filled
    missing = [t for t in targets if str(t) not in out]
    assert len(missing) == 38


def test_anchors_outside_want():
    """时间轴头尾可以不是待修目标帧（常见：尾帧不是 step 对齐）。"""
    want = [10, 13, 16, 19, 22]
    labels = {
        "8": {"frame_idx": 8, "visible": True, "pano_x": 200.0, "pano_y": 80.0},
        "24": {"frame_idx": 24, "visible": True, "pano_x": 320.0, "pano_y": 140.0},
    }
    assert validate_sparse_anchors(labels, want) is None
    out, filled = fill_sparse_labels(labels, want, ROI, PANO_W, FPS)
    assert filled == want, filled
    assert out["13"]["interp"] is True


def test_long_packet_two_ends():
    targets = list(range(0, 300, 3))
    labels = {
        str(targets[0]): {"frame_idx": targets[0], "visible": True, "pano_x": 200.0, "pano_y": 80.0},
        str(targets[-1]): {"frame_idx": targets[-1], "visible": True, "pano_x": 200.0, "pano_y": 80.0},
    }
    assert validate_sparse_anchors(labels, targets) is None
    _out, filled = fill_sparse_labels(labels, targets, ROI, PANO_W, FPS)
    assert len(filled) == len(targets) - 2, len(filled)


def test_single_want_frame():
    labels = {"7": {"frame_idx": 7, "visible": True, "pano_x": 1, "pano_y": 2}}
    assert validate_sparse_anchors(labels, [7]) is None


def test_outside_error():
    labels = {
        "20": {"frame_idx": 20, "visible": True, "pano_x": 1, "pano_y": 2},
        "30": {"frame_idx": 30, "visible": True, "pano_x": 3, "pano_y": 4},
    }
    err = validate_sparse_anchors(labels, [10, 20, 30, 40])
    assert err and "外侧" in err


if __name__ == "__main__":
    test_fill_middle_frames()
    test_invisible_not_filled()
    test_need_anchors()
    test_all_invisible_ok()
    test_gap_too_wide()
    test_anchors_outside_want()
    test_long_packet_two_ends()
    test_single_want_frame()
    test_outside_error()
    print("ok sparse")
