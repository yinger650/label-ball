#!/usr/bin/env python3
"""client/sample.py：事件驱动采样。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from client import sample as S  # noqa: E402


def test_cold_start_asks_all() -> None:
    ts = list(range(1000, 1100, 3))
    assert S.schedule(ts, []) == ts
    assert S.schedule(ts, [(1000, 1.0, 1.0), (1003, 2.0, 1.0)]) == ts


def test_sparse_plus_event() -> None:
    ts = list(range(0, 30, 3))  # 10 个目标帧
    # 前半静止，中间一脚踢出去（位移从 0 跳到 200 px）
    anchors = [
        (0, 10.0, 10.0),
        (3, 10.0, 10.0),
        (6, 10.0, 10.0),
        (9, 210.0, 10.0),
        (12, 400.0, 10.0),
        (15, 410.0, 10.0),
        (18, 412.0, 10.0),
        (21, 412.0, 10.0),
    ]
    want = S.schedule(ts, anchors, stride=2, acc_thr=25.0, step=3)
    hot = S.event_frames(anchors, acc_thr=25.0, step=3)
    assert ts[0] in want and ts[-1] in want
    assert hot, hot
    assert any(f in want for f in (6, 9, 12)), (want, hot)
    assert len(want) < len(ts)


def test_skippable_straight() -> None:
    ts = list(range(0, 30, 3))
    anchors = [(i, float(i) * 2, 0.0) for i in ts[:6]]  # 匀速直线
    skipped = [f for f in ts if S.skippable(f, ts, anchors, stride=2, acc_thr=25.0, step=3)]
    assert skipped, "匀速段该能跳过若干帧"


def main() -> int:
    test_cold_start_asks_all()
    test_sparse_plus_event()
    test_skippable_straight()
    print("sample 3 组通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
