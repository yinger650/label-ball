"""多模型投票读点：拿 probe_vlm_coord.py 的 records.jsonl 离线组合，不再调模型。

    PYTHONPATH=. venv/bin/python scripts/vote_vlm_coord.py --gt /tmp/vlmcmp/gt.json \
        --runs artifacts/label-trials/vlm-compare-20261022 artifacts/label-trials/vlm-compare-20261022/codex \
        --out artifacts/label-trials/vlm-compare-20261022/voting.json

每个投票者 = (provider, test)。一帧上各投票者给出 ROI 点或「看不见」；
规则 k/R：找支持者最多的点（与它相距 ≤R 的点数），支持数 ≥k 就输出这些点的中位数，否则弃权（当看不见）。
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import statistics
from pathlib import Path

BEST = {
    "cmp_glm53_flash": "single2x_px",
    "cmp_deepseek": "single1x_n1000",
    "cmp_minimax_m31_flash": "single2x_px",
    "cmp_minimax_m3": "single1x_px",
    "cmp_mimo_pro": "single1x_n1000",
}
SHORT = {"cmp_glm53_flash": "glm", "cmp_deepseek": "ds", "cmp_minimax_m31_flash": "m31f",
         "cmp_minimax_m3": "m3", "cmp_mimo_pro": "mimo", "codex": "codex"}


def load(runs, gt_path):
    gt = json.loads(Path(gt_path).read_text())
    agent = {(pid, int(r["frame_idx"])) for pid, v in gt.items() for r in v["labels"] if r.get("source")}
    by_voter: dict = {}
    cases: dict = {}
    for run in runs:
        for line in (Path(run) / "records.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            key = (r["packet_id"], r["frame_idx"], r["kind"])
            if r["kind"] == "visible" and key[:2] in agent:
                continue
            cases[key] = r["gt"]
            by_voter.setdefault((r["provider"], r["test"]), {})[key] = r.get("pred_roi")
    return cases, by_voter


def vote(preds, k, radius):
    pts = [p for p in preds if p is not None]
    best = []
    for p in pts:
        sup = [q for q in pts if math.hypot(p[0] - q[0], p[1] - q[1]) <= radius]
        if len(sup) > len(best):
            best = sup
    if len(best) < k:
        return None
    return (statistics.median(q[0] for q in best), statistics.median(q[1] for q in best))


def evaluate(cases, preds_of):
    out = {"hit": 0, "le5": 0, "miss": 0, "wrong": 0, "fp": 0, "n_vis": 0, "n_neg": 0, "errs": []}
    for key, g in cases.items():
        p = preds_of(key)
        if key[2] == "visible":
            out["n_vis"] += 1
            if p is None:
                out["miss"] += 1
                continue
            e = math.hypot(p[0] - g[0], p[1] - g[1])
            out["errs"].append(e)
            out["hit" if e <= 10 else "wrong"] += 1
            out["le5"] += e <= 5
        else:
            out["n_neg"] += 1
            out["fp"] += p is not None
    errs = out.pop("errs")
    out["median_err"] = round(statistics.median(errs), 1) if errs else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gt", required=True)
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--codex-test", default="single2x_px")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    cases, by_voter = load(a.runs, a.gt)
    have_codex = [v for v in by_voter if v[0] == "codex"]
    common = set.intersection(*(set(by_voter[(p, t)]) for p, t in BEST.items()))
    if have_codex:
        common &= set.intersection(*(set(by_voter[v]) for v in have_codex))
    cases = {k: v for k, v in cases.items() if k in common}
    rows = []

    def add(name, voters, k=None, radius=None, kind="vote"):
        if kind == "single":
            res = evaluate(cases, lambda key: by_voter[voters[0]].get(key))
        else:
            res = evaluate(cases, lambda key: vote([by_voter[v].get(key) for v in voters], k, radius))
        rows.append({"name": name, "k": k, "R": radius, "n_voters": len(voters), **res})

    for v in sorted(by_voter):
        if v[0] in BEST or v[0] == "codex":
            add(f"{SHORT.get(v[0], v[0])}:{v[1]}", [v], kind="single")
    names = list(BEST)
    for n in range(2, len(names) + 1):
        for sub in itertools.combinations(names, n):
            voters = [(p, BEST[p]) for p in sub]
            for k in range(2, n + 1):
                for radius in (6, 10, 15):
                    add("+".join(SHORT[p] for p in sub), voters, k, radius)
    every = [v for v in by_voter if v[0] in BEST]
    for k in (3, 4, 5, 6, 8):
        for radius in (6, 10, 15):
            add(f"all{len(every)}(5 家×5 配置)", every, k, radius)

    def oracle(key):
        g = cases[key]
        ps = [by_voter[(p, BEST[p])].get(key) for p in BEST]
        ps = [p for p in ps if p is not None]
        if key[2] != "visible" or not ps:
            return None
        return min(ps, key=lambda p: math.hypot(p[0] - g[0], p[1] - g[1]))
    rows.append({"name": "oracle(5 家任一最近，上限)", "k": None, "R": None, "n_voters": 5,
                 **evaluate(cases, oracle)})

    n_vis = sum(k[2] == "visible" for k in cases)
    print(f"共同用例：visible {n_vis}，negative {len(cases) - n_vis}")
    singles = [r for r in rows if r["k"] is None]
    votes = [r for r in rows if r["k"] is not None]
    # 主排序：命中 - 错点 - 误报（错点和误报对 GT 都是坏点）
    votes.sort(key=lambda r: (-(r["hit"] - r["wrong"] - r["fp"]), -r["hit"]))
    fmt = "{:<34} {:>3} {:>3} | hit {:>2} le5 {:>2} wrong {:>2} miss {:>2} fp {:>2} | med {}"
    for r in singles + votes[:25]:
        print(fmt.format(r["name"], r["k"] or "-", r["R"] or "-", r["hit"], r["le5"], r["wrong"], r["miss"],
                         r["fp"], r["median_err"]))
    if a.out:
        a.out.write_text(json.dumps({"n_visible": n_vis, "n_negative": len(cases) - n_vis,
                                     "best_single_config": BEST, "rows": singles + votes},
                                    ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
