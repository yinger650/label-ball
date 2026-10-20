#!/usr/bin/env python3
"""v2 / v3 × 三视觉模型 × 有/无前 3 帧种子，整包轨迹对比。

12 格逐格跑（避免三家一起 429），VLM 缓存开。扁 ROI 与高 ROI 分开汇总。
结果落到 /mnt/afs/wangmin/data/_ai_label_work/calib/，对比表写 calib/v2v3_compare.md。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from client import data_roots  # noqa: E402
from client.prep import is_ready, prepare_packet  # noqa: E402
from packet import ALL_ARTIFACT_FILES  # noqa: E402
from scripts.calibrate import RESULTS, pr_stats  # noqa: E402

WORKFLOWS = ("erp_ball_v2", "erp_ball_v3")
PROVIDERS = ("minimax", "glm53_flash", "deepseek")
SEEDS = (3, 0)  # 有种子先跑，冷启动更慢且更容易整包放弃
PY = REPO / "venv" / "bin" / "python"
COMPARE_MD = RESULTS / "v2v3_compare.md"


def _report(kind: str, msg: str) -> None:
    script = REPO / "skills" / "agentboard-report" / "scripts" / "report.py"
    if not script.exists():
        return
    env = dict(os.environ)
    env.setdefault("AGENTBOARD_PROVIDER", "cursor")
    try:
        subprocess.run(
            [sys.executable, str(script), kind, msg],
            cwd=str(REPO), env=env, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        )
    except Exception:  # noqa: BLE001
        pass


def _wf_short(wf: str) -> str:
    return "v3" if wf.endswith("v3") else "v2"


def cell_tag(wf: str, provider: str, seed: int) -> str:
    return f"{_wf_short(wf)}_{provider}_seed{seed}"


def cells() -> List[Tuple[str, str, int, str]]:
    out = []
    for wf in WORKFLOWS:
        for provider in PROVIDERS:
            for seed in SEEDS:
                out.append((wf, provider, seed, cell_tag(wf, provider, seed)))
    return out


def _api():
    from client.api import ServerClient
    from client.vlm import load_dotenv

    load_dotenv()
    base = os.environ.get("BALL_GT_SERVER") or "https://ballgt.yinger650.com"
    return ServerClient(base)


def ensure_high_roi_gt(pids: Sequence[str], gt_dir: Path) -> List[str]:
    """把已入库高 ROI 的 GT 拉到本地 gt/，返回真正有可见点的包。"""
    gt_dir.mkdir(parents=True, exist_ok=True)
    have: List[str] = []
    api = None
    for pid in pids:
        existing = list(gt_dir.glob(f"{pid}__*.json"))
        if existing:
            have.append(pid)
            continue
        if api is None:
            api = _api()
        rows = api.get_gt(pid)
        vis = [r for r in rows if r.get("visible") and r.get("pano_x") is not None]
        if not vis:
            print(f"  高 ROI {pid}: 中心库没有可见 GT，跳过")
            continue
        doc = {
            "annotator": "human",
            "packet_id": pid,
            "n_labeled": len(rows),
            "n_visible": len(vis),
            "labels": [
                {
                    "frame_idx": int(r["frame_idx"]),
                    "pano_x": r.get("pano_x"),
                    "pano_y": r.get("pano_y"),
                    "visible": bool(r.get("visible")),
                    "occluded": bool(r.get("occluded")),
                    "blurred": bool(r.get("blurred")),
                    "interp": bool(r.get("interp")),
                    "conf": r.get("conf"),
                    "source": r.get("source") or "human",
                }
                for r in rows
            ],
        }
        dest = gt_dir / f"{pid}__human.json"
        dest.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        print(f"  高 ROI {pid}: 导出 {len(vis)} 个可见点 → {dest.name}")
        have.append(pid)
    return have


def ensure_prepared(pids: Sequence[str], work: Path) -> List[str]:
    """缺 prepare.json 的包从 AOSS 拉 artifacts 再解码。"""
    ready: List[str] = []
    store = None
    for pid in pids:
        root = data_roots.find_prepared(pid)
        if (root / pid / "prepare.json").exists():
            ready.append(pid)
            print(f"  已就绪 {pid} @ {root}")
            continue
        dest = work / pid
        dest.mkdir(parents=True, exist_ok=True)
        if store is None:
            from client.fetch import ensure_packet_files, open_store
            store = open_store()
        from client.fetch import ensure_packet_files

        print(f"  下载+解码 {pid} …")
        key = f"packets/{pid}/"
        try:
            ensure_packet_files(store, key, dest, pid, which=ALL_ARTIFACT_FILES)
        except Exception as e:  # noqa: BLE001
            zip_key = f"packets/{pid}.zip"
            print(f"    前缀失败（{type(e).__name__}），试 zip {zip_key}")
            try:
                ensure_packet_files(store, zip_key, dest, pid)
            except Exception as e2:  # noqa: BLE001
                print(f"    下载失败，跳过 {pid}: {type(e2).__name__}: {e2}")
                continue
        src = dest if (dest / "clip.mp4").exists() else dest / "packet.zip"
        try:
            prepare_packet(src, work, packet_id=pid)
        except Exception as e:  # noqa: BLE001
            print(f"    解码失败，跳过 {pid}: {type(e).__name__}: {e}")
            continue
        ready.append(pid)
        print(f"  就绪 {pid}")
    return ready


def run_cell(tag: str, wf: str, provider: str, seed: int, packets: Sequence[str],
             *, resume: bool) -> Path:
    out = RESULTS / f"{tag}.json"
    if resume and out.exists():
        try:
            doc = json.loads(out.read_text(encoding="utf-8"))
            if doc.get("packets") and doc.get("overall"):
                print(f"  跳过已完成 {tag}（{len(doc['packets'])} 包）")
                return out
        except Exception:  # noqa: BLE001
            pass
    py = str(PY if PY.exists() else sys.executable)
    cmd = [
        py, str(REPO / "scripts" / "calibrate.py"),
        "--tag", tag,
        "--workflow", wf,
        "--provider", provider,
        "--seed-gt", str(seed),
        "--frames", "0",
        "--packets", *packets,
    ]
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    t0 = time.time()
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    r = subprocess.run(cmd, cwd=str(REPO), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"{tag} 退出码 {r.returncode}（{time.time()-t0:.0f}s）")
    print(f"<<< {tag} 完成 {time.time()-t0:.0f}s", flush=True)
    return out


def _merge_eval(packets: Iterable[dict]) -> dict:
    evs = list(packets)
    n_frames = n_given = 0
    n_calls = sec = 0.0
    n_ans = n_gt = n_correct = 0
    raw_w, post_w, sm_w = [], [], []
    for ev in evs:
        n = int(ev.get("n_frames") or 0)
        n_frames += n
        n_given += int(round((1.0 - float(ev.get("abandon_rate") or 0)) * n))
        n_calls += float(ev.get("calls_per_frame") or 0) * n
        sec += float(ev.get("sec_per_frame") or 0) * n
        pr = ev.get("pr") or {}
        n_ans += int(pr.get("n_answered") or 0)
        n_gt += int(pr.get("n_gt") or 0)
        n_correct += int(pr.get("n_correct") or 0)
        for key, bucket in (("raw", raw_w), ("post", post_w), ("smooth", sm_w)):
            s = ev.get(key) or {}
            w = int(s.get("n") or 0)
            if w and s.get("median") is not None:
                bucket.append((w, float(s["median"]), float(s["p90"]) if s.get("p90") is not None else None))
    pr = pr_stats([0.0] * n_correct + [PR_FAKE] * max(n_ans - n_correct, 0), n_ans, n_gt)
    return {
        "n_frames": n_frames,
        "n_packets": len(evs),
        "abandon_rate": round(1 - n_given / max(n_frames, 1), 3),
        "calls_per_frame": round(n_calls / max(n_frames, 1), 2),
        "sec_per_frame": round(sec / max(n_frames, 1), 2),
        "pr": pr,
        "raw_median": _wmed(raw_w, 1),
        "raw_p90": _wmed(raw_w, 2),
        "post_median": _wmed(post_w, 1),
        "post_p90": _wmed(post_w, 2),
        "smooth_median": _wmed(sm_w, 1),
        "smooth_p90": _wmed(sm_w, 2),
    }


# precision 合并只需要 n_correct / n_answered；用假误差喂 pr_stats
PR_FAKE = 999.0


def _wmed(rows: Sequence[Tuple], idx: int) -> Optional[float]:
    """按该包答出帧数加权的近似中位 / p90（没有逐帧误差时的退路）。"""
    xs: List[float] = []
    for row in rows:
        w, *rest = row
        v = rest[idx - 1]
        if v is None:
            continue
        xs += [float(v)] * max(int(w), 1)
    if not xs:
        return None
    return round(float(np.median(xs)), 2)


def subset_summary(rep: dict, want: Sequence[str]) -> dict:
    evs = []
    for pid, row in (rep.get("packets") or {}).items():
        if pid in want and row.get("eval"):
            evs.append(row["eval"])
    out = _merge_eval(evs)
    out["n_packets"] = len(evs)
    o = rep.get("overall") or {}
    if o.get("pr") and len(evs) == len(rep.get("packets") or {}):
        out["pr"] = o["pr"]
        out["n_frames"] = o.get("n_frames", out["n_frames"])
        out["abandon_rate"] = o.get("abandon_rate", out["abandon_rate"])
        out["calls_per_frame"] = o.get("calls_per_frame", out["calls_per_frame"])
        out["sec_per_frame"] = o.get("sec_per_frame", out["sec_per_frame"])
        if o.get("median") is not None:
            out["raw_median"] = o.get("median")
            out["raw_p90"] = o.get("p90")
    return out


def _fmt(v, pct: bool = False) -> str:
    if v is None:
        return "-"
    if pct:
        return f"{float(v):.1%}"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def render_table(rows: List[dict], title: str) -> List[str]:
    lines = [f"## {title}", ""]
    lines.append(
        "| tag | workflow | model | seed | 帧 | 包 | P@20 | 召回 | "
        "中位(raw/post/smooth) | p90(raw/post/smooth) | 放弃 | 调用/帧 | 秒/帧 |"
    )
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|")
    for r in rows:
        pr = r.get("pr") or {}
        lines.append(
            f"| `{r['tag']}` | {r['workflow']} | {r['provider']} | {r['seed']} | "
            f"{r.get('n_frames', 0)} | {r.get('n_packets', 0)} | "
            f"{_fmt(pr.get('precision'), True)} | {_fmt(pr.get('recall'), True)} | "
            f"{_fmt(r.get('raw_median'))} / {_fmt(r.get('post_median'))} / {_fmt(r.get('smooth_median'))} | "
            f"{_fmt(r.get('raw_p90'))} / {_fmt(r.get('post_p90'))} / {_fmt(r.get('smooth_p90'))} | "
            f"{_fmt(r.get('abandon_rate'), True)} | {_fmt(r.get('calls_per_frame'))} | "
            f"{_fmt(r.get('sec_per_frame'))} |"
        )
    lines.append("")
    return lines


def write_compare(cell_reps: Dict[str, dict], flat: Sequence[str], high: Sequence[str]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    blocks = [
        "# v2 / v3 × 三模型轨迹对比",
        "",
        f"时间 {time.strftime('%Y-%m-%d %H:%M')}。主指标是 **track 之后** 的 precision@20 / 召回 / 中位与 p90。",
        f"扁 ROI：`{'` `'.join(flat) or '（无）'}`",
        f"高 ROI：`{'` `'.join(high) or '（无）'}`",
        "",
        "DeepSeek 视觉通道是 `deepseek-flash`（V4.1-Flash）。V4.1 Pro 不支持图。",
        "",
    ]
    for set_name, pids, seed_filter, title in (
        ("flat", flat, 3, "扁 ROI · 有种子（前 3 帧）"),
        ("flat", flat, 0, "扁 ROI · 无种子（冷启动）"),
        ("high", high, 3, "高 ROI · 有种子（前 3 帧）"),
        ("high", high, 0, "高 ROI · 无种子（冷启动）"),
    ):
        rows = []
        suffix = "_high" if set_name == "high" else ""
        for wf, provider, seed, tag in cells():
            if seed != seed_filter:
                continue
            key = tag + suffix
            rep = cell_reps.get(key)
            if not rep:
                continue
            s = subset_summary(rep, pids)
            rows.append({
                "tag": key, "workflow": wf, "provider": provider, "seed": seed, **s,
            })
        blocks += render_table(rows, title)
    blocks += [
        "## 怎么读",
        "",
        "- 无种子：看哪家 + 哪种工作流冷启动还能跟住（召回与 p90）。",
        "- 有种子：看 v3 的 240 窗有没有把中位 / p90 拉下来。",
        "- precision@20 分母是答出且非插值的帧；放弃多会抬精度、压召回。",
        "",
    ]
    COMPARE_MD.write_text("\n".join(blocks), encoding="utf-8")
    print("\n".join(blocks))
    print(f"\n写到 {COMPARE_MD}")
    return COMPARE_MD


def print_console(cell_reps: Dict[str, dict], flat: Sequence[str], high: Sequence[str]) -> None:
    for title, pids, suffix in (("扁 ROI", flat, ""), ("高 ROI", high, "_high")):
        print("\n" + "=" * 78)
        print(title)
        print(f"{'tag':<32s} {'seed':>4s} {'P@20':>7s} {'召回':>7s} "
              f"{'中位p':>8s} {'p90p':>8s} {'放弃':>7s} {'调用':>6s}")
        for wf, provider, seed, tag in cells():
            key = tag + suffix
            rep = cell_reps.get(key)
            if not rep:
                continue
            s = subset_summary(rep, pids)
            pr = s.get("pr") or {}
            print(f"{key:<32s} {seed:>4d} {_fmt(pr.get('precision'), True):>7s} "
                  f"{_fmt(pr.get('recall'), True):>7s} {_fmt(s.get('post_median')):>8s} "
                  f"{_fmt(s.get('post_p90')):>8s} {_fmt(s.get('abandon_rate'), True):>7s} "
                  f"{_fmt(s.get('calls_per_frame')):>6s}")


def main() -> None:
    ap = argparse.ArgumentParser(description="v2/v3 × 三模型 × 种子 轨迹对比")
    ap.add_argument("--only", nargs="+", default=None, help="只跑这些 tag，例如 v3_minimax_seed3")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--skip-prepare", action="store_true")
    ap.add_argument("--report-only", action="store_true", help="只读已有 json 出表")
    a = ap.parse_args()

    gt_dir = data_roots.GT
    work = data_roots.ACP if data_roots.ACP.is_dir() else data_roots.WORK
    flat = data_roots.available_gt_packets(gt_dir)
    # available_gt 可能已经混进高 ROI；扁集排除 TESTSET
    high_ids = set(data_roots.TESTSET_HIGH_ROI)
    flat = [p for p in flat if p not in high_ids]

    if not a.skip_prepare and not a.report_only:
        print("准备高 ROI 包（拉 GT + 解码）…")
        high_gt = ensure_high_roi_gt(data_roots.TESTSET_HIGH_ROI, gt_dir)
        high = ensure_prepared(high_gt, work)
    else:
        high = [p for p in data_roots.labeled_high_roi(gt_dir)
                if (data_roots.find_prepared(p) / p / "prepare.json").exists()]

    print(f"扁 ROI {len(flat)} 包: {flat}")
    print(f"高 ROI {len(high)} 包: {high}")
    if not flat and not high:
        raise SystemExit("没有可跑的包")

    wanted = set(a.only) if a.only else None
    matrix = [c for c in cells() if wanted is None or c[3] in wanted]
    cell_reps: Dict[str, dict] = {}

    jobs: List[Tuple[str, str, str, int, List[str]]] = []
    for wf, provider, seed, tag in matrix:
        if flat:
            jobs.append((tag, wf, provider, seed, list(flat)))
        if high:
            jobs.append((f"{tag}_high", wf, provider, seed, list(high)))

    if not a.report_only:
        for i, (tag, wf, provider, seed, pids) in enumerate(jobs, 1):
            _report("progress", f"矩阵 {i}/{len(jobs)} {tag}")
            print(f"\n==== [{i}/{len(jobs)}] {tag} ====", flush=True)
            path = run_cell(tag, wf, provider, seed, pids, resume=not a.no_resume)
            if path.exists():
                cell_reps[tag] = json.loads(path.read_text(encoding="utf-8"))
    else:
        for tag, *_rest in jobs:
            p = RESULTS / f"{tag}.json"
            if p.exists():
                cell_reps[tag] = json.loads(p.read_text(encoding="utf-8"))

    extra = [t for *_, t in cells()] + [f"{t}_high" for *_, t in cells()]
    for tag in extra:
        if tag in cell_reps:
            continue
        p = RESULTS / f"{tag}.json"
        if p.exists():
            try:
                cell_reps[tag] = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass

    print_console(cell_reps, flat, high)
    write_compare(cell_reps, flat, high)


if __name__ == "__main__":
    main()
