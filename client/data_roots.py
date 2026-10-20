#!/usr/bin/env python3
"""本机已 prepare 的包根目录。

旧脚本默认只看 `_ai_label_work/work`，那里只有 3 包。6 包人工 GT 全在
`acp/` 下已经解码过（见 2026-09-16 日更）。优先 acp，没有再退回 work。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

AI_WORK = Path("/mnt/afs/wangmin/data/_ai_label_work")
ACP = AI_WORK / "acp"
WORK = AI_WORK / "work"
GT = AI_WORK / "gt"
PACKETS = AI_WORK / "packets"

# 高 ROI 冻结测试集（门限/提示词不在这 6 包上扫）。2026-09-16 状态：
# 4 包已入库，260507__007 archived 0 GT，260507__008 leased 0 GT。
TESTSET_HIGH_ROI = [
    "260507__007__0042_0052",
    "260517__VID_20260517_125036_00_002__0784_0794",
    "260507__008__0375_0385",
    "260116__002__1006_1016",
    "260903__VID_20260903_214111_00_005__0189_0199",
    "260806__VID_20260806_063708_00_005__0817_0827",
]


def prepared_roots() -> List[Path]:
    return [p for p in (ACP, WORK) if p.is_dir()]


def default_work() -> Path:
    """标定 / smoke 的默认工作区：acp 有包就用 acp。"""
    if ACP.is_dir() and any(ACP.glob("*/prepare.json")):
        return ACP
    return WORK


def find_prepared(packet_id: str) -> Path:
    for root in prepared_roots():
        if (root / packet_id / "prepare.json").exists():
            return root
    return default_work()


def labeled_high_roi(gt_dir: Optional[Path] = None) -> List[str]:
    """TESTSET_HIGH_ROI 里本地已有人工 GT 的包（不含 archived / 未入库）。"""
    gt_dir = Path(gt_dir or GT)
    out: List[str] = []
    for pid in TESTSET_HIGH_ROI:
        if any(f.name.startswith(pid + "__") for f in gt_dir.glob(f"{pid}__*.json")):
            out.append(pid)
    return out


def available_gt_packets(gt_dir: Optional[Path] = None, work: Optional[Path] = None) -> List[str]:
    """已 prepare 且有人工 GT 的包。默认扫 acp/ + work/，比只看 work/ 多一倍。"""
    gt_dir = Path(gt_dir or GT)
    roots = [Path(work)] if work is not None else prepared_roots()
    out: List[str] = []
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if d.name in seen or not (d / "prepare.json").exists():
                continue
            if any(f.name.startswith(d.name + "__") for f in gt_dir.glob(f"{d.name}__*.json")):
                seen.add(d.name)
                out.append(d.name)
    return out
