#!/usr/bin/env python3
"""confirm9 到底会不会说「是」？把 GT 位置直接喂给它数一下。

标定里 `confirm9` 的 `null_rate` 是 100%（三个包全是），有两种可能：

1. 它是对的 —— 上游 `propose` 的全局初选中位误差 446–919 px，那些位置上确实没有球
2. 它坏了 —— 不管看到什么都回 false，那整条冷启动路就等于被焊死了

区分这两种情况只需要把**球真正在的位置**喂给它。这个脚本就做这件事，顺便拿
一批故意偏 200 px 的位置当负样本，看它分不分得开。

    ./venv/bin/python scripts/probe_confirm9.py [--packets ...] [--n 6]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import prompting, views as V  # noqa: E402
from client.prep import load_prepared, pano_to_roi  # noqa: E402
from client.vlm import VlmPool  # noqa: E402
from client.workflow import Workflow, _encode_model_image  # noqa: E402

WORK = Path("/mnt/afs/wangmin/data/_ai_label_work/work")
GT = Path("/mnt/afs/wangmin/data/_ai_label_work/gt")


def gt_points(prep, pid: str) -> dict:
    out = {}
    for f in sorted(GT.glob(f"{pid}__*.json")):
        doc = json.loads(f.read_text())
        if doc.get("annotator") == "smoketest":
            continue
        for r in doc.get("labels", []):
            if r.get("visible") and r.get("pano_x") is not None:
                out[int(r["frame_idx"])] = pano_to_roi(
                    float(r["pano_x"]), float(r["pano_y"]), prep.roi, prep.pano_w)
    return out


def ask(wf: Workflow, prep, fi: int, center, size: float) -> tuple[bool, str]:
    img, _frames, labels = V.make_time_grid_view(prep, fi, center, size=size, n=9)
    prompt = prompting.render_plain("confirm9",
                                    extra={"first_label": labels[0] if labels else "t-8"})
    obj, _res = wf.pool.ask_json(wf.default_role, prompt, [_encode_model_image(img)],
                                 system=prompting.SYSTEM, schema={"conf": (int, float)},
                                 required=("moving_ball",))
    said = bool(obj and obj.get("moving_ball") is True)
    return said, str((obj or {}).get("why", ""))[:90]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packets", nargs="+", default=None)
    ap.add_argument("--n", type=int, default=6, help="每包探几帧")
    ap.add_argument("--work", type=Path, default=WORK)
    a = ap.parse_args()

    pool = VlmPool()
    wf = Workflow.load("erp_ball_v2", pool)
    spec = next(s for s in wf.stages if s["id"] == "confirm9")
    size = float((spec.get("view") or {}).get("size", V.TIME_GRID_SIZE))
    pids = a.packets or sorted({p.stem.split("__wangmin")[0] for p in GT.glob("*__wangmin.json")} & {d.name for d in a.work.iterdir() if d.is_dir()})

    hit = miss = fp = tn = 0
    for pid in pids:
        prep = load_prepared(a.work, pid)
        gt = gt_points(prep, pid)
        frames = [f for f in prep.targets if f in gt]
        pick = frames[:: max(1, len(frames) // max(1, a.n))][: a.n]
        print(f"\n== {pid}  窗 {size:.0f}px  探 {len(pick)} 帧")
        for fi in pick:
            gx, gy = gt[fi]
            ok, why = ask(wf, prep, fi, (gx, gy), size)
            hit, miss = (hit + 1, miss) if ok else (hit, miss + 1)
            print(f"  帧 {fi} GT位置        → {'是' if ok else '否'}  {why}")
            # 负样本：故意偏 200 px（还在场内，但那儿没有球）
            ok2, why2 = ask(wf, prep, fi, (gx + 200, gy), size)
            fp, tn = (fp + 1, tn) if ok2 else (fp, tn + 1)
            print(f"  帧 {fi} 偏200px       → {'是' if ok2 else '否'}  {why2}")

    n_pos, n_neg = hit + miss, fp + tn
    print(f"\nGT 位置说「是」 {hit}/{n_pos} = {hit / max(n_pos, 1):.0%}（这是召回，太低就是它坏了）")
    print(f"偏 200px 说「是」 {fp}/{n_neg} = {fp / max(n_neg, 1):.0%}（这是误报，太高就是它没用）")
    if hit == 0:
        print("→ GT 位置一次都没通过：confirm9 是坏的，冷启动被它焊死了")
    elif fp >= hit:
        print("→ 分不开正负样本：这一级白花调用")
    else:
        print("→ 分得开，冷启动的 100% null 是上游 propose 定位太差导致的")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
