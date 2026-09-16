#!/usr/bin/env python3
"""拿已有的人工 GT 当验证集，量出整条工作流到底有多准。

**不先建这个，提示词调优就是盲调。** 改一句话、换一级放大倍数、换一家模型，
凭感觉看两张图是分不出 5% 差异的；这个脚本给三个能横向比较的数：

- **像素误差分布**（中位 / P90 / <8px / <20px 命中率）—— 标注质量的硬指标
- **放弃率** —— 模型说「找不到」的比例。放弃是**好事**（比瞎猜强），但放弃太多
  就等于把活全推给了人工，AI 客户端也就没有价值了
- **各阶段命中率** —— 每一级单独看，答对格子的比例。哪一级掉链子一眼可见，
  这决定该改哪一级的提示词、还是该改它的裁切尺寸

还有两个同样重要的数：**单帧调用数**和**单帧耗时**，它们决定这套东西跑得起跑不起。

结果按 `--tag` 存成 JSONL，同一份验证集上历次运行可以直接 diff——A/B 的意义
全在这里：`--tag base` 跑一遍，改完提示词 `--tag v2` 再跑一遍，
`--compare base v2` 把两次并排列出来。

用法：
    venv/bin/python3 scripts/calibrate.py --tag base --frames 20
    venv/bin/python3 scripts/calibrate.py --tag v2 --frames 20
    venv/bin/python3 scripts/calibrate.py --compare base v2
    venv/bin/python3 scripts/calibrate.py --stages-only --frames 30   # 只测各级命中率，不跑整链
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import track as T, views as V  # noqa: E402
from client.prep import load_prepared, pano_to_roi  # noqa: E402
from client.vlm import VlmPool  # noqa: E402
from client.workflow import Workflow, to_obs  # noqa: E402

WORK = Path("/mnt/afs/wangmin/data/_ai_label_work/work")
GT = Path("/mnt/afs/wangmin/data/_ai_label_work/gt")
RESULTS = Path("/mnt/afs/wangmin/data/_ai_label_work/calib")


def load_gt(gt_dir: Path, packet_id: str, prep) -> Dict[int, Tuple[float, float]]:
    out: Dict[int, Tuple[float, float]] = {}
    for f in sorted(gt_dir.glob(f"{packet_id}__*.json")):
        d = json.loads(f.read_text())
        if d.get("annotator") == "smoketest":
            continue
        for r in d.get("labels", []):
            if r.get("visible") and r.get("pano_x") is not None:
                rx, ry = pano_to_roi(r["pano_x"], r["pano_y"], prep.roi, prep.pano_w)
                if 0 <= rx < prep.size[0] and 0 <= ry < prep.size[1]:
                    out[int(r["frame_idx"])] = (rx, ry)
    return out


def available_packets(work: Path, gt_dir: Path) -> List[str]:
    out = []
    for d in sorted(work.iterdir()):
        if (d / "prepare.json").exists() and any(
            f.name.split("__wangmin")[0] == d.name for f in gt_dir.glob(f"{d.name}__*.json")
        ):
            out.append(d.name)
    return out


# ---------------------------------------------------------------- 指标


PR_TOL = 20.0


def pr_stats(errs: Sequence[float], n_answered: int, n_gt: int, tol: float = PR_TOL) -> dict:
    """精度 / 召回。这是「精度 > 85%」那句话的唯一口径。

    `err_stats` 的 `hit20` 分母是**全部帧**，量的是「这套系统总共标出了多少可用帧」；
    放弃一帧和标错一帧在它眼里一样坏。要把错点换成空缺（错点会污染训练集、还会把
    外推带跑偏），就必须有一个**分母是「答出来的帧」**的数：

        precision = |答出且误差 ≤ tol| / |答出|
        recall    = |答出且误差 ≤ tol| / |GT 里可见的帧|

    `答出` 不含插值帧：插值是后处理编出来的，拿它算精度等于自己给自己加分。
    两个口径并列保留，不互相替代 —— 只看 precision 会奖励「什么都不答」。
    """
    e = np.asarray([v for v in errs], dtype=float)
    hit = int((e <= tol).sum()) if e.size else 0
    return {
        "tol": tol,
        "n_answered": int(n_answered),
        "n_correct": hit,
        "n_gt": int(n_gt),
        "precision": round(hit / max(n_answered, 1), 3),
        "recall": round(hit / max(n_gt, 1), 3),
        "answer_rate": round(n_answered / max(n_gt, 1), 3),
    }


def err_stats(errs: Sequence[float], n_total: int) -> dict:
    e = np.asarray(sorted(errs), dtype=float)
    if e.size == 0:
        return {"n": 0, "median": None, "p90": None, "hit8": 0.0, "hit20": 0.0, "hit50": 0.0}
    return {
        "n": int(e.size),
        "median": round(float(np.median(e)), 2),
        "p90": round(float(np.percentile(e, 90)), 2),
        "mean": round(float(e.mean()), 2),
        # 命中率的分母是**全部帧**而不是答出来的帧：放弃的帧对下游同样是没标上，
        # 用答出的帧当分母会让「放弃得多」看起来像「准得离谱」。
        "hit8": round(float((e < 8).sum() / max(n_total, 1)), 3),
        "hit20": round(float((e < 20).sum() / max(n_total, 1)), 3),
        "hit50": round(float((e < 50).sum() / max(n_total, 1)), 3),
    }


def stage_report(runs, gt: Dict[int, Tuple[float, float]]) -> Dict[str, dict]:
    """逐阶段拆开看。每一级用**它自己的格子尺寸**判命中，
    不然 L1 永远比 L3「差」，看不出是哪一级真的掉链子。"""
    acc: Dict[str, dict] = {}
    for r in runs:
        g = gt.get(r.frame_idx)
        if g is None:
            continue
        for s in r.stages:
            a = acc.setdefault(s.stage, {"n": 0, "skipped": 0, "null": 0, "fail": 0,
                                         "errs": [], "cell_hit": 0, "cell_px": 0.0,
                                         "lat": [], "cached": 0})
            a["n"] += 1
            a["cell_px"] = max(a["cell_px"], s.cell_px)  # 跳过的阶段记的是 0，不能拿它当准
            if s.skipped:
                a["skipped"] += 1
                continue
            a["lat"].append(s.latency)
            a["cached"] += int(s.cached)
            if s.error:
                a["fail"] += 1
                continue
            if s.x is None:
                a["null"] += 1
                continue
            e = float(np.hypot(s.x - g[0], s.y - g[1]))
            a["errs"].append(e)
            # 「答对格子」= 误差在半个格子以内，这是这一级理论上能达到的最好结果
            if s.cell_px and e <= max(s.cell_px, 1.0) * 0.75:
                a["cell_hit"] += 1
    out = {}
    for k, a in acc.items():
        ran = a["n"] - a["skipped"]
        out[k] = {
            "ran": ran, "skipped": a["skipped"], "null_rate": round(a["null"] / max(ran, 1), 3),
            "fail_rate": round(a["fail"] / max(ran, 1), 3),
            "cell_hit_rate": round(a["cell_hit"] / max(ran, 1), 3),
            "cell_px": round(float(a["cell_px"] or 0), 1),
            "median_err": round(float(np.median(a["errs"])), 2) if a["errs"] else None,
            "median_lat": round(float(np.median(a["lat"])), 2) if a["lat"] else None,
            "cache_hits": a["cached"],
        }
    return out


def gated_obs(runs, gate: float) -> list:
    """把 `final_conf` 低于门限的帧改写成「没答」。

    门限扫描要能离线做：整链跑一次要几百次模型调用，而门限只影响本地判定。
    所以这里不重跑工作流，只在 `Obs` 这一层把不达标的帧挖掉，再让 `track.process`
    重新插值、重新统计。扫 10 档的代价是 10 次纯本地计算。
    """
    obs = to_obs(runs)
    if gate <= 0:
        return obs
    by_frame = {int(r.frame_idx): float(getattr(r, "final_conf", None) or r.conf or 0.0)
                for r in runs}
    for o in obs:
        if by_frame.get(int(o.frame_idx), 0.0) < gate:
            o.x, o.y, o.visible, o.conf = None, None, False, 0.0
    return obs


def _pr_from_points(pts, gt, tol: float = PR_TOL) -> dict:
    """轨迹后处理之后、**排除插值帧**的 precision / recall。这是提交出去的那批点。"""
    errs, n_ans = [], 0
    for p in pts:
        g = gt.get(p.frame_idx)
        if p.x is None or p.interp or not p.visible:
            continue
        n_ans += 1
        if g is not None:
            errs.append(float(np.hypot(p.x - g[0], p.y - g[1])))
    n_gt = sum(1 for p in pts if gt.get(p.frame_idx) is not None)
    return pr_stats(errs, n_ans, n_gt, tol)


def sweep_gate(runs, gt, fps: float, gates: Sequence[float], tol: float = PR_TOL) -> List[dict]:
    """离线扫门限，返回每一档的 P/R。选工作点用这个，不要靠感觉调 accept_conf。"""
    out = []
    for g in gates:
        pts, st = T.process(gated_obs(runs, g), fps=fps)
        row = {"gate": round(float(g), 3), **_pr_from_points(pts, gt, tol)}
        row["n_interp"] = int(st.n_interp)
        row["n_missing"] = int(st.n_missing)
        out.append(row)
    return out


def _merge_pr(per_packet: Sequence[Tuple[list, dict, float]], gate: float,
              tol: float = PR_TOL) -> dict:
    """跨包合并 P/R。**必须逐包做后处理再合并**：插值是包内的时间操作，
    把几个包的观测串成一条序列会在包边界上插出根本不存在的轨迹。"""
    errs: List[float] = []
    n_ans = n_gt = 0
    for runs, gt, fps in per_packet:
        pts, _st = T.process(gated_obs(runs, gate), fps=fps)
        for p in pts:
            g = gt.get(p.frame_idx)
            if p.x is None or p.interp or not p.visible:
                continue
            n_ans += 1
            if g is not None:
                errs.append(float(np.hypot(p.x - g[0], p.y - g[1])))
        n_gt += sum(1 for p in pts if gt.get(p.frame_idx) is not None)
    return pr_stats(errs, n_ans, n_gt, tol)


def _merge_track(per_packet: Sequence[Tuple[list, dict, float]], gate: float) -> dict:
    n_interp = n_missing = 0
    for runs, _gt, fps in per_packet:
        _pts, st = T.process(gated_obs(runs, gate), fps=fps)
        n_interp += int(st.n_interp)
        n_missing += int(st.n_missing)
    return {"n_interp": n_interp, "n_missing": n_missing}


def evaluate(runs, gt, prep, fps: float, gate: float = 0.0) -> dict:
    """整链 + 轨迹后处理之后的最终指标。`gate` > 0 时先按 `final_conf` 弃帧。"""
    n = len(runs)
    raw_errs, given = [], 0
    for r in runs:
        g = gt.get(r.frame_idx)
        if g is None or r.x is None:
            continue
        given += 1
        raw_errs.append(float(np.hypot(r.x - g[0], r.y - g[1])))

    pts, st = T.process(gated_obs(runs, gate), fps=fps)
    post_errs, post_interp_errs = [], []
    for p in pts:
        g = gt.get(p.frame_idx)
        if g is None or p.x is None:
            continue
        e = float(np.hypot(p.x - g[0], p.y - g[1]))
        (post_interp_errs if p.interp else post_errs).append(e)

    smooth_errs = []
    for p in pts:
        g = gt.get(p.frame_idx)
        if g is not None and p.smooth_x is not None:
            smooth_errs.append(float(np.hypot(p.smooth_x - g[0], p.smooth_y - g[1])))

    n_gt = sum(1 for r in runs if gt.get(r.frame_idx) is not None)
    return {
        "n_frames": n,
        "gate": round(float(gate), 3),
        "abandon_rate": round(1 - given / max(n, 1), 3),
        "calls_per_frame": round(sum(r.n_calls for r in runs) / max(n, 1), 2),
        "sec_per_frame": round(float(np.mean([r.elapsed for r in runs])) if runs else 0.0, 2),
        "mean_conf": round(float(np.mean([r.conf for r in runs if r.x is not None])) if given else 0.0, 3),
        # 头条指标：分母是「答出来的帧」，放弃不加分
        "pr": _pr_from_points(pts, gt),
        "pr_raw": pr_stats(raw_errs, given, n_gt),
        "raw": err_stats(raw_errs, n),
        "post": err_stats(post_errs + post_interp_errs, n),
        "post_observed": err_stats(post_errs, max(len(post_errs), 1)),
        "post_interp": err_stats(post_interp_errs, max(len(post_interp_errs), 1)),
        "smooth": err_stats(smooth_errs, n),
        "track": asdict(st),
    }


# ---------------------------------------------------------------- 只测单级


def stages_only(pool: VlmPool, prep, gt, frames: List[int], levels: List[str],
                role: str, seed: int) -> Dict[str, dict]:
    """不跑整链，只单独测每一级：用「GT + 该级典型入口误差」当裁切中心。

    整链跑一次很贵（每帧 2–4 次调用），而调提示词时通常只动一级。这个模式让你
    用几十次调用就看出这一级改好了没有。
    """
    from scripts.smoke_vlm import LEVELS, run_trial  # 复用冒烟测试的几何定义

    out: Dict[str, dict] = {}
    for lv in levels:
        errs, nulls, fails, lats = [], 0, 0, []
        for f in frames:
            t = run_trial(pool, role, prep, lv, f, gt[f], random.Random(seed + f), True, None)
            if not t.ok:
                fails += 1
            elif t.said_null:
                nulls += 1
            elif t.err_px is not None:
                errs.append(t.err_px)
            lats.append(t.latency)
        out[lv] = {**err_stats(errs, len(frames)),
                   "null_rate": round(nulls / max(len(frames), 1), 3),
                   "fail_rate": round(fails / max(len(frames), 1), 3),
                   "median_lat": round(float(np.median(lats)), 2) if lats else None,
                   "jitter_px": LEVELS[lv]["jitter"]}
    return out


# ---------------------------------------------------------------- 打印


def print_report(rep: dict) -> None:
    print("\n" + "=" * 78)
    print(f"标定报告  tag={rep['tag']}  workflow={rep.get('workflow')}  {rep['when']}")
    print("=" * 78)
    for pid, r in rep["packets"].items():
        e = r["eval"]
        print(f"\n包 {pid}   {e['n_frames']} 帧   门限 {e.get('gate', 0)}")
        print(f"  放弃率 {e['abandon_rate']:.1%}   单帧 {e['calls_per_frame']:.2f} 次调用 / "
              f"{e['sec_per_frame']:.1f}s   平均置信 {e['mean_conf']:.2f}")
        if e.get("pr"):
            p = e["pr"]
            print(f"  ★ precision@{p['tol']:.0f}px {p['precision']:.1%}  召回 {p['recall']:.1%}  "
                  f"（答出 {p['n_answered']}/{p['n_gt']} 帧，对 {p['n_correct']} 帧）")
        for nm, k in (("模型原始输出", "raw"), ("后处理(含插值)", "post"), ("平滑轨迹", "smooth")):
            s = e[k]
            print(f"  {nm:<14s} 中位 {str(s['median']):>7s}px  P90 {str(s['p90']):>7s}px  "
                  f"<8px {s['hit8']:.0%}  <20px {s['hit20']:.0%}  <50px {s['hit50']:.0%}")
        t = e["track"]
        print(f"  轨迹: 观测 {t['n_observed']}  离群剔除 {t['n_outlier']}  插值 {t['n_interp']}  "
              f"仍缺 {t['n_missing']}  最长空洞 {t['max_gap']}")
        if r.get("stages"):
            print(f"  {'阶段':<12s} {'跑过':>5s} {'跳过':>5s} {'格子命中':>9s} {'每格px':>7s} "
                  f"{'中位误差':>9s} {'说找不到':>9s} {'失败':>7s} {'延迟':>7s}")
            for k, s in r["stages"].items():
                print(f"  {k:<12s} {s['ran']:>5d} {s['skipped']:>5d} {s['cell_hit_rate']:>8.0%} "
                      f"{s['cell_px']:>7.1f} {str(s['median_err']):>9s} {s['null_rate']:>8.0%} "
                      f"{s['fail_rate']:>6.0%} {str(s['median_lat']):>6s}s")
        if r.get("levels"):
            print(f"  {'单级':<8s} {'入口抖动':>8s} {'中位误差':>9s} {'P90':>8s} {'<8px':>6s} "
                  f"{'<20px':>7s} {'放弃':>6s} {'延迟':>7s}")
            for k, s in r["levels"].items():
                print(f"  {k:<8s} {s['jitter_px']:>8d} {str(s['median']):>9s} {str(s['p90']):>8s} "
                      f"{s['hit8']:>5.0%} {s['hit20']:>6.0%} {s['null_rate']:>5.0%} "
                      f"{str(s['median_lat']):>6s}s")
    o = rep.get("overall")
    if o:
        print(f"\n【合计 {o['n_frames']} 帧】放弃率 {o['abandon_rate']:.1%}  "
              f"后处理中位误差 {o['median']}px  <8px {o['hit8']:.0%}  <20px {o['hit20']:.0%}  "
              f"单帧 {o['calls_per_frame']:.2f} 次调用 / {o['sec_per_frame']:.1f}s")
        if o.get("pr"):
            p = o["pr"]
            print(f"【合计 precision@{p['tol']:.0f}px {p['precision']:.1%}  召回 {p['recall']:.1%}】"
                  f"  答出 {p['n_answered']}/{p['n_gt']} 帧，其中对 {p['n_correct']} 帧")
    if rep.get("sweep"):
        print(f"\n门限扫描（挑 precision ≥ 85% 里召回最大的那档）")
        print(f"  {'gate':>6s} {'precision':>10s} {'recall':>8s} {'答出':>6s} {'插值':>6s} {'仍缺':>6s}")
        best = None
        for row in rep["sweep"]:
            mark = ""
            if row["precision"] >= 0.85 and (best is None or row["recall"] > best["recall"]):
                best = row
            print(f"  {row['gate']:>6.2f} {row['precision']:>9.1%} {row['recall']:>7.1%} "
                  f"{row['n_answered']:>6d} {row['n_interp']:>6d} {row['n_missing']:>6d}{mark}")
        if best:
            print(f"  → 工作点 gate={best['gate']:.2f}: precision {best['precision']:.1%} / "
                  f"召回 {best['recall']:.1%}")
        else:
            print("  → 没有任何一档到 85%，说明该改的是判据而不是门限")


def compare(tags: List[str]) -> None:
    rows = []
    for t in tags:
        p = RESULTS / f"{t}.json"
        if not p.exists():
            print(f"没有 {p}")
            continue
        r = json.loads(p.read_text())
        o = r.get("overall") or {}
        rows.append((t, r.get("when", ""), o))
    if not rows:
        return
    print(f"\n{'tag':<12s} {'时间':<17s} {'帧':>5s} {'精度':>6s} {'召回':>6s} {'中位':>7s} "
          f"{'P90':>7s} {'<20px':>7s} {'调用/帧':>8s} {'秒/帧':>7s}")
    for t, when, o in rows:
        pr = o.get("pr") or {}
        print(f"{t:<12s} {when[:16]:<17s} {o.get('n_frames',0):>5d} "
              f"{pr.get('precision',0):>5.0%} {pr.get('recall',0):>5.0%} "
              f"{str(o.get('median')):>7s} {str(o.get('p90')):>7s} "
              f"{o.get('hit20',0):>6.0%} {o.get('calls_per_frame',0):>8.2f} "
              f"{o.get('sec_per_frame',0):>7.1f}")


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description="用人工 GT 标定 AI 标注工作流")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--packets", nargs="+", default=None, help="默认用所有已 prepare 且有 GT 的包")
    ap.add_argument("--work", type=Path, default=WORK)
    ap.add_argument("--gt", type=Path, default=GT)
    ap.add_argument("--workflow", default="erp_ball_v2")
    ap.add_argument("--frames", type=int, default=20, help="每包测多少帧（整链模式下是连续的一段）")
    ap.add_argument("--start", type=int, default=0, help="整链模式下从第几个目标帧开始")
    ap.add_argument("--seed-gt", type=int, default=0, metavar="N",
                    help="用人工 GT 喂前 N 帧当起始先验，绕开冷启动，单独测跟踪精度")
    ap.add_argument("--role", default="primary")
    ap.add_argument("--seed", type=int, default=20260910)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--stages-only", action="store_true", help="只单独测各级，不跑整链")
    ap.add_argument("--levels", nargs="+", default=["P1", "L2", "L3"])
    ap.add_argument("--compare", nargs="+", default=None)
    ap.add_argument("--gate", type=float, default=0.0,
                    help="报告用的弃帧门限（final_conf 低于此当作没答）。0 = 不弃")
    ap.add_argument("--verify-gate", type=float, default=None, metavar="G",
                    help="覆盖 yaml 里的 verify.gate（跑的时候就弃帧，会影响后续帧的外推）。"
                         "扫门限时设 0：跑一遍只记分不弃帧，再离线往上扫")
    ap.add_argument("--sweep-gate", nargs="*", type=float, default=None, metavar="G",
                    help="离线扫门限找工作点。不给值就用 0…0.8 共 9 档")
    a = ap.parse_args()

    if a.compare:
        compare(a.compare)
        return

    pool = VlmPool(use_cache=not a.no_cache)
    pids = a.packets or available_packets(a.work, a.gt)
    if not pids:
        raise SystemExit("没有既 prepare 好又有人工 GT 的包")
    wf = None if a.stages_only else Workflow.load(a.workflow, pool)
    if wf is not None and a.verify_gate is not None:
        wf.verify_gate = float(a.verify_gate)

    rep = {"tag": a.tag, "workflow": a.workflow if not a.stages_only else "stages_only",
           "when": time.strftime("%Y-%m-%d %H:%M"), "role": a.role,
           "verify_gate": (None if wf is None else round(float(wf.verify_gate), 3)),
           "packets": {}}
    gates = list(a.sweep_gate) if a.sweep_gate else [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.75, 0.8]
    all_runs, all_gt, all_n, all_calls, all_sec, all_given = [], {}, 0, 0.0, 0.0, 0
    all_fps: List[float] = []
    per_packet: List[Tuple[list, dict, float]] = []  # (runs, gt, fps) 供合计口径复用

    for pid in pids:
        prep = load_prepared(a.work, pid)
        gt = load_gt(a.gt, pid, prep)
        if not gt:
            continue
        frames = sorted(gt)
        if a.stages_only:
            # 单级测试各帧互相独立，铺开抽样能覆盖更多场景
            pick = [frames[i] for i in np.linspace(0, len(frames) - 1, min(a.frames, len(frames))).astype(int)]
        else:
            # 整链必须跑**连续**的目标帧：外推先验是逐帧接力传下去的，跳着抽
            # 等于每一帧都在冷启动，测出来的是「没有先验时有多差」而不是流程本身
            s = min(a.start, max(0, len(frames) - a.frames))
            pick = frames[s : s + a.frames]
        t0 = time.time()
        if a.stages_only:
            rep["packets"][pid] = {"levels": stages_only(pool, prep, gt, pick, a.levels, a.role, a.seed),
                                   "eval": {"n_frames": len(pick), "abandon_rate": 0.0,
                                            "calls_per_frame": 0, "sec_per_frame": 0, "mean_conf": 0,
                                            "raw": err_stats([], 1), "post": err_stats([], 1),
                                            "smooth": err_stats([], 1),
                                            "track": asdict(T.TrackStats(0, 0, 0, 0, 0, 0.0, 0))}}
            print(f"  {pid}: 单级测完，{time.time()-t0:.0f}s")
            continue
        done = 0

        def tick(_r):
            nonlocal done
            done += 1
            print(f"\r  {pid}: {done}/{len(pick)}", end="", flush=True)

        # --seed-gt：用人工 GT 喂头两帧，把「冷启动准不准」和「跟踪准不准」分开测。
        # 这两件事的失败原因完全不同，混在一个数里就没法判断该改哪儿。
        seed = None
        if a.seed_gt:
            seed = [(f, gt[f][0], gt[f][1]) for f in pick[: a.seed_gt]]
            pick = pick[a.seed_gt :]
        runs = wf.run_packet(prep, frames=pick, on_frame=tick, seed=seed)
        print(f"\r  {pid}: {len(runs)} 帧, {time.time()-t0:.0f}s")
        fps_t = float(prep.meta.fps) / max(int(prep.meta.step or 1), 1)
        ev = evaluate(runs, gt, prep, fps=fps_t, gate=a.gate)
        rep["packets"][pid] = {"eval": ev, "stages": stage_report(runs, gt)}
        if a.sweep_gate is not None:
            rep["packets"][pid]["sweep"] = sweep_gate(runs, gt, fps_t, gates)
        all_runs += runs
        all_fps.append(fps_t)
        per_packet.append((runs, gt, fps_t))
        all_gt.update({(pid, k): v for k, v in gt.items()})
        all_n += len(runs)
        all_calls += sum(r.n_calls for r in runs)
        all_sec += sum(r.elapsed for r in runs)
        all_given += sum(1 for r in runs if r.x is not None)
        for r in runs:
            g = gt.get(r.frame_idx)
            if g is not None and r.x is not None:
                r.__dict__["_err"] = float(np.hypot(r.x - g[0], r.y - g[1]))

    if all_n and not a.stages_only:
        errs = [r.__dict__["_err"] for r in all_runs if "_err" in r.__dict__]
        rep["overall"] = {**err_stats(errs, all_n), "n_frames": all_n,
                          "gate": round(float(a.gate), 3),
                          "abandon_rate": round(1 - all_given / all_n, 3),
                          "calls_per_frame": round(all_calls / all_n, 2),
                          "sec_per_frame": round(all_sec / all_n, 2),
                          "pr": _merge_pr(per_packet, a.gate)}
        if a.sweep_gate is not None:
            rep["sweep"] = [{"gate": round(float(g), 3), **_merge_pr(per_packet, g),
                             **_merge_track(per_packet, g)} for g in gates]

    print_report(rep)
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / f"{a.tag}.json"
    p.write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    print(f"\n写到 {p}   （改完提示词换个 --tag 再跑一遍，然后 --compare 两个 tag）")


if __name__ == "__main__":
    main()
