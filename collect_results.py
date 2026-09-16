#!/usr/bin/env python3
"""回收标注结果：从对象存储下载、补齐球场坐标、质检、汇总成训练可用的 GT。

    python collect_results.py --out-dir ./collected

标注端只存全景像素坐标。`field_u/v` 在这一步补齐，不在标注站算：
优先 import FootballDet360 的 `pano_pixel_to_field`；找不到则尝试本目录
`ball_field_uv.py`；再没有就跳过补齐，不阻塞回收。

输出：
    <out-dir>/<packet_id>__<annotator>.json
    <out-dir>/index.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_fb = os.environ.get("FOOTBALLDET360_ROOT", "/mnt/afs/wangmin/code/FootballDet360")
if _fb:
    sys.path.insert(0, _fb)

from aoss_store import Store, load_credentials  # noqa: E402

OUT_DIR = Path(
    os.environ.get(
        "BALL_GT_COLLECT_DIR",
        "/mnt/afs/wangmin/data/_research_ball_det/ball_gt/collected",
    )
)


def load_field_fn():
    """取权威 ERP→球场换算。拿不到就跳过补齐，不阻塞回收。"""
    try:
        from football360.task2.v21.detector_v20 import pano_pixel_to_field

        return pano_pixel_to_field
    except Exception as e1:  # noqa: BLE001
        try:
            from ball_field_uv import pano_pixel_to_field  # optional local helper

            return pano_pixel_to_field
        except Exception as e2:  # noqa: BLE001
            print(
                f"[warn] 无法 import 球场坐标函数（{e1} / {e2}），本次跳过 field_u/v 补齐",
                flush=True,
            )
            return None


def qc(result: dict, fps: float) -> dict:
    """质检。目的是把明显不可用的包挑出来人工复核，而不是自动丢弃。"""
    labels = result.get("labels") or []
    vis = [r for r in labels if r.get("visible")]
    n_t = result.get("n_targets") or len(labels)

    speeds: List[float] = []
    for a, b in zip(vis, vis[1:]):
        dt = b["frame_idx"] - a["frame_idx"]
        if dt <= 0:
            continue
        speeds.append(math.hypot(b["pano_x"] - a["pano_x"], b["pano_y"] - a["pano_y"]) / dt)

    flags: List[str] = []
    cover = len(labels) / max(1, n_t)
    if cover < 0.98:
        flags.append(f"覆盖率仅 {cover*100:.0f}%")
    vis_ratio = len(vis) / max(1, len(labels))
    if vis_ratio < 0.5:
        flags.append(f"可见帧仅 {vis_ratio*100:.0f}%（可能是难窗，也可能是漏标）")
    # 单目标约定下，位置不该出现瞬间大跨度跳变
    jumps = sum(1 for s in speeds if s > 220)
    if jumps > max(2, 0.05 * len(speeds)):
        flags.append(f"{jumps} 处帧间位移 >220 px/帧（疑似标到了另一个球）")
    # 阈值用「每源帧 px」。0.4 太紧：VLM 在静止备用球上仍有 1–2 px 抖动，线上
    # 整包通过率 0 的那批几乎全没打上这面旗。2.0（step=3 时约 6 px/格）仍远低于
    # 带球走的位移，能把「锁死场边球 / 场外灯」挑出来。
    if speeds and st.median(speeds) < 2.0 and len(vis) > 20:
        flags.append("位置几乎不动（疑似标成了场边备用球）")
    if vis and len(vis) > 20:
        xs = [float(r["pano_x"]) for r in vis]
        ys = [float(r["pano_y"]) for r in vis]
        span = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        if span < 80:
            flags.append(f"整段轨迹跨度仅 {span:.0f} px（疑似锁死静止目标）")

    return {
        "n_labeled": len(labels),
        "n_targets": n_t,
        "coverage": round(cover, 3),
        "n_visible": len(vis),
        "visible_ratio": round(vis_ratio, 3),
        "speed_med_px": round(st.median(speeds), 1) if speeds else None,
        "speed_p90_px": round(sorted(speeds)[int(0.9 * len(speeds))], 1) if speeds else None,
        "moving_ratio": round(sum(1 for s in speeds if s >= 2.0) / len(speeds), 3) if speeds else None,
        "flags": flags,
    }


def run(args) -> None:
    store = Store(load_credentials(args.credentials))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    field_fn = load_field_fn()

    results = store.list_results()
    if not results:
        print("[done] 对象存储上还没有任何标注结果", flush=True)
        return
    print(f"[scan] 已提交 {sum(len(v) for v in results.values())} 份结果，涉及 {len(results)} 个包", flush=True)

    index: List[dict] = []
    n_pts = n_vis = 0
    tally: Counter = Counter()
    for pid in sorted(results):
        for who in sorted(results[pid]):
            res = store.get_json(f"results/{pid}__{who}.json")
            if not res:
                print(f"[skip] 读取失败 {pid}__{who}", flush=True)
                continue

            tf = None
            if field_fn is not None:
                idx = store.packet_index(pid) or {}
                tf = idx.get("sphere_transform")
            n_uv = 0
            for r in res.get("labels") or []:
                if not (r.get("visible") and tf):
                    continue
                try:
                    uv = field_fn(float(r["pano_x"]), float(r["pano_y"]), tf)
                except Exception:
                    uv = None
                if uv is not None:
                    r["field_u"], r["field_v"] = float(uv[0]), float(uv[1])
                    n_uv += 1

            report = qc(res, float(res.get("fps") or 30.0))
            res["qc"] = report
            path = out_dir / f"{pid}__{who}.json"
            path.write_text(json.dumps(res, ensure_ascii=False))

            n_pts += report["n_labeled"]
            n_vis += report["n_visible"]
            tally[who] += report["n_labeled"]
            index.append({
                "packet_id": pid, "annotator": who, "file": path.name,
                "group": res.get("group"), "stem": res.get("stem"), "tag": res.get("tag"),
                "sampling": (store.packet_index(pid) or {}).get("sampling"),
                "n_field_uv": n_uv, **report,
            })
            mark = "⚠ " + "；".join(report["flags"]) if report["flags"] else "ok"
            print(
                f"  {pid}  [{who}]  {report['n_labeled']}/{report['n_targets']} 帧  "
                f"可见 {report['n_visible']}  运动占比 {report['moving_ratio']}  {mark}",
                flush=True,
            )

    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2))
    bad = [r for r in index if r["flags"]]
    print(
        f"\n[done] {len(index)} 份结果，共 {n_pts} 个标注点（可见 {n_vis}）\n"
        f"  按标注员：{dict(tally)}\n"
        f"  需人工复核：{len(bad)} 份\n"
        f"  输出：{out_dir}",
        flush=True,
    )


def parse_args():
    p = argparse.ArgumentParser(description="回收并汇总足球 GT 标注结果")
    p.add_argument("--out-dir", default=str(OUT_DIR))
    p.add_argument("--credentials", default=None)
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
