#!/usr/bin/env python3
"""多家多模态模型直接读球心像素的对比：标注主路线（coord 页 + image_xy）的能力上限。

数据：若干包的人工 GT（只用人工点过的帧：不用插值帧，也不用 `source=agent` 的预标——
人工审核通过的提交里也可能夹着偏 30–100 px 的 AI 预标）。每包抽可见帧 + 负样本：
真 absent 帧（窗心放在前后可见点插值处）和「窗口离球 ≥300 px」的合成负样本。
窗心相对 GT 随机偏 ±150 px，不把答案放在正中。

看法 × 坐标约定（`<view>_<unit>`），每个都是一次调用：
- single1x：480 px 窗原样；single2x：320 px 窗最近邻放大到 640
- page：真实 coord 页（2×2 × 480 px，40 px 标题条写帧号），按 RV.page_to_roi 换算
- px：要求报本图像素；n1000：要求报 0–1000 归一化坐标（不少模型的定位默认约定）
px 答案另记 err_if_n1000：模型被要求报像素却自己用了归一化时，这个数会很小

    venv/bin/python scripts/probe_vlm_coord.py --gt /tmp/vlmcmp/gt.json \
        --providers cmp_deepseek,cmp_glm53_flash,cmp_mimo_pro,cmp_minimax_m3 \
        --out artifacts/label-trials/vlm-compare-20261022

`--gt` 是 {packet_id: {submission_id, labels}}（服务端只读导出）。不代表整包标注成功率。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import review_views as RV  # noqa: E402
from client.agent_core import AgentSession  # noqa: E402
from client.prep import load_prepared  # noqa: E402
from client.views import crop_pad  # noqa: E402
from client.vlm import Provider, extract_json, load_dotenv, load_providers  # noqa: E402

SYSTEM = "你是足球比赛视频的标注员，只标比赛中正在使用的那一个球。只回 JSON，不要解释。"
UNITS = {
    "px": "坐标单位是像素：左上角 (0,0)，右下角 ({w},{h})",
    "n1000": "坐标用 0–1000 归一化：左上角 (0,0)，右下角 (1000,1000)，与图的实际像素无关",
}
PROMPT_ONE = (
    "这是 8K 全景足球视频的一块局部原图，宽 {w} px、高 {h} px。{unit}。"
    "球可能在滚动、在空中，也可能静止在地上或球员脚下；场边的备用球不算。"
    '能看见比赛用球就回 {{"visible": true, "x": 球心横坐标, "y": 球心纵坐标}}；'
    '看不见或不确定回 {{"visible": false}}，不要猜。'
)
PROMPT_PAGE = (
    "这张图宽 {w} px、高 {h} px，{unit}，坐标相对整张图。它由 {n} 个小图拼成，每个小图是同一场比赛"
    "不同帧的局部原图，小图上方 {cap} px 深色标题条里写着帧号（f 开头）。"
    "球可能在滚动、在空中，也可能静止；场边备用球不算。"
    "对每一帧：看得见比赛用球就报球心在**整张图**里的坐标，看不见或不确定写 visible=false，不要猜。"
    '回 {{"frames": [{{"f": 帧号, "visible": true, "x": .., "y": ..}}, ...]}}，帧号是 {frames}。'
)
SIZE_1X, SIZE_2X, PAGE_SIZE = 480, 320, 480


class CodexProvider:
    """把 `codex exec` 包成和 vlm.Provider 一样的 call()。空临时目录里跑（不读仓库 AGENTS.md），只读沙箱。

    用的是 ChatGPT 登录的 Codex 当前默认模型（`~/.codex/config.toml`）；`codex:<model>` 可指定。
    usage 里 n_commands > 0 说明模型没听话自己跑了命令看图。
    """

    BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"

    def __init__(self, model: str = None, effort: str = "medium"):
        import types
        conf = Path.home() / ".codex/config.toml"
        default = next((ln.split("=", 1)[1].strip().strip('"') for ln in conf.read_text().splitlines()
                        if ln.startswith("model =")), "codex-default") if conf.exists() else "codex-default"
        self.model, self.effort = model, effort
        self.cfg = types.SimpleNamespace(model=model or default, max_concurrency=3)

    def call(self, prompt, images=(), system=None, use_cache=False, max_tokens=None, **_):
        import subprocess
        import tempfile
        import types
        with tempfile.TemporaryDirectory(prefix="codexprobe_") as d:
            paths = []
            for i, b in enumerate(images):
                q = Path(d) / f"img{i}.png"
                q.write_bytes(b)
                paths.append(str(q))
            text = ((system + "\n\n") if system else "") + "不要运行任何命令，不要读写文件，直接看附图回答。" + prompt
            cmd = [self.BIN, "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
                   "-c", f"model_reasoning_effort={self.effort}", "--json", "-o", f"{d}/out.txt"]
            if self.model:
                cmd += ["-m", self.model]
            cmd += [text, "-i", *paths]
            try:
                p = subprocess.run(cmd, cwd=d, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                return types.SimpleNamespace(ok=False, text="", error="timeout", usage={}, reasoning="")
            usage, n_cmd, err = {}, 0, ""
            for line in p.stdout.splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") == "turn.completed":
                    u = e.get("usage") or {}
                    usage = {"prompt_tokens": u.get("input_tokens"), "completion_tokens": u.get("output_tokens")}
                elif e.get("type") in ("error", "turn.failed"):
                    err = str(e.get("message") or e.get("error"))[:200]
                elif (e.get("item") or {}).get("type") == "command_execution" and e.get("type") == "item.completed":
                    n_cmd += 1
            usage["n_commands"] = n_cmd
            out = Path(d) / "out.txt"
            txt = out.read_text() if out.exists() else ""
            ok = p.returncode == 0 and bool(txt)
            return types.SimpleNamespace(ok=ok, text=txt, error="" if ok else (err or p.stderr[-200:]),
                                         usage=usage, reasoning="")


def png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def prepare_all(work: Path, pids) -> None:
    sess = AgentSession(None, work_root=work, worker="probe")
    for pid in pids:
        if (work / pid / "prepare.json").exists():
            continue
        sess.start_prepare(pid, f"packets/{pid}/")
        t0 = time.time()
        while True:
            st = sess.job_state(pid)
            if st["state"] == "ready":
                break
            if st["state"] == "error" or time.time() - t0 > 900:
                raise RuntimeError(f"{pid}: {st}")
            time.sleep(2)
        print(f"prepared {pid} ({time.time() - t0:.0f}s)", flush=True)


def build_cases(work: Path, gt: dict, n_vis: int, rng: random.Random, include_agent: bool = False) -> list:
    cases = []
    for pid, sub in gt.items():
        prep = load_prepared(work, pid)
        rw, rh = prep.size
        labels = sorted(sub["labels"], key=lambda r: int(r["frame_idx"]))
        vis = [r for r in labels if r.get("visible") and not r.get("interp") and r.get("pano_x") is not None
               and (include_agent or not r.get("source")) and prep.has(int(r["frame_idx"]))]
        pts = [(int(r["frame_idx"]), *prep.to_roi(float(r["pano_x"]), float(r["pano_y"]))) for r in vis]
        pts = [p for p in pts if 0 <= p[1] < rw and 0 <= p[2] < rh]
        for fi, gx, gy in sorted(rng.sample(pts, min(n_vis, len(pts)))):
            cases.append({"packet_id": pid, "frame_idx": fi, "kind": "visible", "gt": [gx, gy],
                          "center": [gx + rng.uniform(-150, 150), gy + rng.uniform(-150, 150)]})
        absent = [int(r["frame_idx"]) for r in labels if not r.get("visible") and prep.has(int(r["frame_idx"]))]
        for fi in sorted(rng.sample(absent, min(2, len(absent)))):
            ctr = RV.interp_center(fi, [(p[0], p[1], p[2]) for p in sorted(pts)])
            if ctr:
                cases.append({"packet_id": pid, "frame_idx": fi, "kind": "absent", "gt": None, "center": list(ctr)})
        for _ in range(50):
            fi, gx, gy = rng.choice(pts)
            ang, dist = rng.uniform(0, 2 * math.pi), rng.uniform(380, 600)
            cx, cy = gx + dist * math.cos(ang), gy + dist * math.sin(ang)
            if 260 <= cx < rw - 260 and 260 <= cy < rh - 260:
                cases.append({"packet_id": pid, "frame_idx": fi, "kind": "far", "gt": None, "center": [cx, cy],
                              "ball_roi": [gx, gy]})
                break
    return cases


def crop(prep, c: dict, size: int, scale: int):
    x0, y0 = math.floor(c["center"][0] - size / 2), math.floor(c["center"][1] - size / 2)
    img = crop_pad(prep.raw(c["frame_idx"]), x0, y0, size, size)
    if scale != 1:
        img = cv2.resize(img, (size * scale, size * scale), interpolation=cv2.INTER_NEAREST)
    return img, x0, y0


def score(c: dict, pred) -> dict:
    """pred = (rx, ry) ROI 坐标或 None（说看不见 / 没答）。"""
    if c["kind"] == "visible":
        if pred is None:
            return {"outcome": "miss"}
        e = math.hypot(pred[0] - c["gt"][0], pred[1] - c["gt"][1])
        return {"outcome": "hit" if e <= 10 else "wrong", "err": round(e, 1)}
    return {"outcome": "fp" if pred is not None else "tn"}


def to_px(v: float, unit: str, extent: int) -> float:
    return v * extent / 1000.0 if unit == "n1000" else v


def run_single(prov: Provider, prep, c: dict, size: int, scale: int, unit: str) -> dict:
    img, x0, y0 = crop(prep, c, size, scale)
    w = h = size * scale
    t0 = time.time()
    r = prov.call(PROMPT_ONE.format(w=w, h=h, unit=UNITS[unit].format(w=w, h=h)), images=[png(img)],
                  system=SYSTEM, use_cache=False)
    rec = {"latency": round(time.time() - t0, 2), "ok": r.ok, "usage": r.usage or {},
           "raw": (r.text or r.error or "")[:200]}
    js = extract_json(r.text or "") if r.ok else None
    pred = None
    if isinstance(js, dict) and js.get("visible") and js.get("x") is not None:
        try:
            ax, ay = float(js["x"]), float(js["y"])
            pred = (x0 + to_px(ax, unit, w) / scale, y0 + to_px(ay, unit, h) / scale)
            if c["kind"] == "visible" and unit == "px":
                alt = (x0 + ax * w / 1000.0 / scale, y0 + ay * h / 1000.0 / scale)
                rec["err_if_n1000"] = round(math.hypot(alt[0] - c["gt"][0], alt[1] - c["gt"][1]), 1)
        except (TypeError, ValueError):
            pred = None
    rec["parsed"] = isinstance(js, dict)
    rec["pred_roi"] = None if pred is None else [round(pred[0], 1), round(pred[1], 1)]
    rec.update(score(c, pred))
    return rec


def run_page(prov: Provider, prep, group: list, out_png: Path, unit: str) -> list:
    specs = [RV.TileSpec(frame_idx=c["frame_idx"], center=tuple(c["center"])) for c in group]
    page = RV.render_pages(prep, specs, PAGE_SIZE, 1)[0]
    if not out_png.exists():
        cv2.imwrite(str(out_png), page.image)
    w, h = page.size
    frames = [c["frame_idx"] for c in group]
    t0 = time.time()
    r = prov.call(PROMPT_PAGE.format(w=w, h=h, n=len(group), cap=RV.CAPTION, frames=frames,
                                     unit=UNITS[unit].format(w=w, h=h)),
                  images=[png(page.image)], system=SYSTEM, use_cache=False, max_tokens=None)
    lat = round(time.time() - t0, 2)
    js = extract_json(r.text or "") if r.ok else None
    answers = {}
    if isinstance(js, dict):
        for a in js.get("frames") or []:
            if isinstance(a, dict) and a.get("f") is not None:
                try:
                    answers[int(str(a["f"]).lstrip("f"))] = a
                except ValueError:
                    pass
    recs = []
    for i, c in enumerate(group):
        a = answers.get(c["frame_idx"]) or {}
        pred, note = None, ""
        if a.get("visible") and a.get("x") is not None:
            try:
                hit = RV.page_to_roi(page.tiles, page.size,
                                     (to_px(float(a["x"]), unit, w), to_px(float(a["y"]), unit, h)))
            except (TypeError, ValueError):
                hit = {"error": "bad xy"}
            if hit.get("error"):
                note = hit["error"]
            elif hit["frame_idx"] != c["frame_idx"]:
                note = f"点落在 f{hit['frame_idx']} 的格子里"
            else:
                pred = hit["roi_xy"]
        rec = {"latency": lat if i == 0 else 0.0, "ok": r.ok, "usage": (r.usage or {}) if i == 0 else {},
               "raw": (r.text or r.error or "")[:300] if i == 0 else "", "parsed": isinstance(js, dict),
               "pred_roi": None if pred is None else [round(pred[0], 1), round(pred[1], 1)], "note": note}
        rec.update(score(c, pred))
        if note and c["kind"] == "visible":
            rec["outcome"] = "wrong"
        if note and c["kind"] != "visible":
            rec["outcome"] = "fp"
        recs.append(rec)
    return recs


def summarize(recs: list) -> dict:
    vis = [r for r in recs if r["kind"] == "visible"]
    neg = [r for r in recs if r["kind"] != "visible"]
    errs = [r["err"] for r in vis if "err" in r]
    calls = [r for r in recs if r["latency"]]
    tok_in = [r["usage"].get("prompt_tokens") for r in calls if r["usage"].get("prompt_tokens")]
    tok_out = [r["usage"].get("completion_tokens") for r in calls if r["usage"].get("completion_tokens")]
    return {
        "n_visible": len(vis), "n_negative": len(neg),
        "hit_le10": sum(r["outcome"] == "hit" for r in vis),
        "hit_le5": sum(r.get("err", 1e9) <= 5 for r in vis),
        "hit_rate": round(sum(r["outcome"] == "hit" for r in vis) / max(1, len(vis)), 3),
        "miss": sum(r["outcome"] == "miss" for r in vis),
        "wrong": sum(r["outcome"] == "wrong" for r in vis),
        "median_err": round(statistics.median(errs), 1) if errs else None,
        "looks_n1000": sum(r.get("err_if_n1000", 1e9) <= 10 < r.get("err", 0) for r in vis),
        "fp": sum(r["outcome"] == "fp" for r in neg),
        "api_errors": sum(not r["ok"] for r in calls),
        "unparsed": sum(r["ok"] and not r["parsed"] for r in calls),
        "n_calls": len(calls),
        "mean_latency": round(statistics.mean(r["latency"] for r in calls), 2) if calls else None,
        "mean_prompt_tokens": round(statistics.mean(tok_in)) if tok_in else None,
        "mean_completion_tokens": round(statistics.mean(tok_out)) if tok_out else None,
    }


def resummarize(a) -> int:
    gt = json.loads(a.gt.read_text())
    agent = {(pid, int(r["frame_idx"])) for pid, v in gt.items() for r in v["labels"] if r.get("source")}
    rows = [json.loads(line) for line in (a.out / "records.jsonl").read_text().splitlines() if line.strip()]
    kept = [r for r in rows if not (r["kind"] == "visible" and (r["packet_id"], r["frame_idx"]) in agent)]
    grouped: dict = {}
    for r in kept:
        grouped.setdefault(r["provider"], {}).setdefault(r["test"], []).append(r)
    report = json.loads((a.out / "report.json").read_text())
    report["excluded_agent_frames"] = sorted(f"{p}:{f}" for p, f in agent
                                             if any(r["packet_id"] == p and r["frame_idx"] == f for r in rows))
    for name, tests in grouped.items():
        report["results"][name].update({t: summarize(rs) for t, rs in tests.items()})
    (a.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["results"], ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gt", type=Path, required=True)
    ap.add_argument("--providers", required=True)
    ap.add_argument("--work", type=Path, default=Path.home() / ".cache/ball_ai/replay")
    ap.add_argument("--n-vis", type=int, default=6)
    ap.add_argument("--seed", type=int, default=20261022)
    ap.add_argument("--tests", default="single1x_px,single1x_n1000,single2x_px,page_px,page_n1000")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--include-agent-gt", action="store_true",
                    help="抽样时保留 source=agent 的帧（复现 2026-10-22 第一轮样本；汇总仍剔除）")
    ap.add_argument("--summarize-only", action="store_true",
                    help="不调模型，按当前 GT 过滤规则重算 out/records.jsonl 的汇总")
    a = ap.parse_args()
    if a.summarize_only:
        return resummarize(a)

    load_dotenv()
    gt = json.loads(a.gt.read_text())
    prepare_all(a.work, gt)
    cases = build_cases(a.work, gt, a.n_vis, random.Random(a.seed), a.include_agent_gt)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "pages").mkdir(exist_ok=True)
    preps = {pid: load_prepared(a.work, pid) for pid in gt}
    tests = a.tests.split(",")
    print(f"{len(cases)} cases: " + json.dumps({k: sum(c['kind'] == k for c in cases)
                                                 for k in ('visible', 'absent', 'far')}), flush=True)

    cfgs = load_providers()
    for name in a.providers.split(","):
        if name.startswith("codex"):
            cfgs[name] = CodexProvider(name.partition(":")[2] or None).cfg
    lock = threading.Lock()
    rec_path = a.out / "records.jsonl"
    rec_path.write_text("")
    all_recs: dict = {}

    def emit(name: str, test: str, c: dict, rec: dict) -> None:
        row = {"provider": name, "model": cfgs[name].model, "test": test, "packet_id": c["packet_id"],
               "frame_idx": c["frame_idx"], "kind": c["kind"],
               "gt": None if c["gt"] is None else [round(c["gt"][0], 1), round(c["gt"][1], 1)], **rec}
        with lock:
            all_recs.setdefault((name, test), []).append(row)
            with rec_path.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def run_model(name: str) -> None:
        prov = CodexProvider(name.partition(":")[2] or None) if name.startswith("codex") else Provider(cfgs[name])
        jobs = []
        by_pid: dict = {}
        for c in cases:
            by_pid.setdefault(c["packet_id"], []).append(c)
        for test in tests:
            if test.startswith("page"):
                for cs in by_pid.values():
                    jobs += [(test, None, cs[i:i + 4]) for i in range(0, len(cs), 4)]
            else:
                jobs += [(test, c, None) for c in cases]

        def one(job):
            test, c, group = job
            view, unit = test.rsplit("_", 1)
            try:
                if view == "page":
                    tag = f"{group[0]['packet_id'][:24]}_{group[0]['frame_idx']}"
                    for cc, rec in zip(group, run_page(prov, preps[group[0]["packet_id"]], group,
                                                       a.out / "pages" / f"{tag}.png", unit)):
                        emit(name, test, cc, rec)
                else:
                    size, scale = (SIZE_1X, 1) if view == "single1x" else (SIZE_2X, 2)
                    emit(name, test, c, run_single(prov, preps[c["packet_id"]], c, size, scale, unit))
            except Exception as e:  # noqa: BLE001
                for cc in (group or [c]):
                    emit(name, test, cc, {"latency": 0.0, "ok": False, "usage": {}, "raw": f"{type(e).__name__}: {e}"[:200],
                                          "parsed": False, "pred_roi": None, **score(cc, None)})

        with ThreadPoolExecutor(max(1, int(cfgs[name].max_concurrency or 1))) as ex:
            list(ex.map(one, jobs))
        print(f"done {name}", flush=True)

    names = a.providers.split(",")
    with ThreadPoolExecutor(len(names)) as ex:
        list(ex.map(run_model, names))

    report = {"cases": len(cases), "seed": a.seed, "packets": {p: v["submission_id"] for p, v in gt.items()},
              "results": {}}
    for name in names:
        report["results"][name] = {"model": cfgs[name].model,
                                   **{t: summarize(all_recs.get((name, t), [])) for t in tests}}
    (a.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["results"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
