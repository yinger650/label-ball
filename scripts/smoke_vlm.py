#!/usr/bin/env python3
"""第一件该做的事：三家模型到底能不能看见那颗 8–30 px 的球。

这个脚本的结论决定整套「纯 VLM 逐级粗到细」方案值不值得往下做，所以它必须打在
**真实数据**上，而不是合成图：从 AOSS 下载的真实 packet 里切 L2 裁切图，用真实的
人工 GT 当答案，逐家逐帧打分。

它验五件事：

1. **连通性 / 鉴权**：三把 key 都能通到各自的 base_url
2. **`thinking` 关掉是否生效**：回答里不该出现 `<think>`，也不该有 `reasoning_content`；
   顺带看 completion_tokens 是不是掉到个位数
3. **返回能不能解析**：能不能稳定抠出 `{"cell": ..., "conf": ...}`
4. **它到底能不能看见球**：把模型选的格子中心和人工 GT 比，算像素误差
5. **顺带看看每图消耗多少视觉 token**（DeepSeek 的 384 上限是不是真的致命）

裁切中心**不放在 GT 上**，而是按上一级的典型误差随机偏移——把球放在正中央等于
提前告诉模型答案，测出来的准确率是假的。

用法：
    venv/bin/python3 scripts/smoke_vlm.py                       # 默认 L2，8 帧，全部供应商
    venv/bin/python3 scripts/smoke_vlm.py --levels L1 L2 L3     # 多级一起测
    venv/bin/python3 scripts/smoke_vlm.py --providers glm45v    # 只测一家
    venv/bin/python3 scripts/smoke_vlm.py --frames 20 --no-cache
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import data_roots, prompting, views as V  # noqa: E402
from client.prep import load_prepared, pano_to_roi, prepare_packet  # noqa: E402
from client.vlm import VlmPool, extract_json  # noqa: E402

DEFAULT_WORK = data_roots.default_work()
DEFAULT_GT = data_roots.GT
DEFAULT_PACKETS = data_roots.PACKETS
OUT_DIR = Path("/mnt/afs/wangmin/data/_ai_label_work/smoke")


# 每一级的几何参数与「上一级传下来的中心该有多大误差」。
# jitter 取自 client/tests/test_views.py 实测的逐级理论误差。
LEVELS = {
    "L0": dict(kind="montage", cols=8, rows=4, width=1024, prompt="coarse", jitter=0),
    "L1": dict(kind="crop", size=480, upscale=2, resample="lanczos", panes=("raw", "enh"),
               cols=6, rows=6, prompt="refine", jitter=70),
    # 外推入口：已有前两帧就直接从这一级起跑，跳过 L0/L1。窗口按 step=3 时
    # 快球两帧间约 390 px 的位移留冗余，jitter 用线性外推的实测残差。
    "P1": dict(kind="crop", size=200, upscale=5, resample="nearest", panes=("raw", "enh"),
               cols=8, rows=8, prompt="refine", jitter=60),
    "L2": dict(kind="crop", size=80, upscale=8, resample="nearest", panes=("raw", "enh"),
               cols=8, rows=8, prompt="refine", jitter=16),
    "L3": dict(kind="crop", size=45, upscale=16, resample="nearest", panes=("raw", "enh"),
               cols=9, rows=9, prompt="refine", jitter=5),
    "streak": dict(kind="crop", size=240, upscale=4, resample="nearest",
                   panes=("enh_maxproj", "enh"), cols=6, rows=6, prompt="streak", jitter=40),
}


@dataclass
class Trial:
    provider: str
    level: str
    frame: int
    ok: bool = False
    err_px: Optional[float] = None
    cell: Optional[str] = None
    gt_cell: Optional[str] = None
    conf: Optional[float] = None
    said_null: bool = False
    latency: float = 0.0
    cached: bool = False
    thinking_leak: bool = False
    completion_tokens: Optional[int] = None
    prompt_tokens: Optional[int] = None
    error: str = ""
    raw: str = ""


def load_gt(gt_dir: Path, packet_id: str, prep) -> Dict[int, Tuple[float, float]]:
    out: Dict[int, Tuple[float, float]] = {}
    for f in sorted(gt_dir.glob(f"{packet_id}__*.json")):
        d = json.loads(f.read_text())
        if d.get("annotator") == "smoketest":
            continue  # 那是别人的冒烟数据，只有 3 个点而且是乱点的
        for r in d.get("labels", []):
            if r.get("visible") and r.get("pano_x") is not None:
                rx, ry = pano_to_roi(r["pano_x"], r["pano_y"], prep.roi, prep.pano_w)
                if 0 <= rx < prep.size[0] and 0 <= ry < prep.size[1]:
                    out[int(r["frame_idx"])] = (rx, ry)
    return out


def build_view(prep, level: str, frame: int, center: Tuple[float, float]) -> V.GridView:
    L = LEVELS[level]
    if L["kind"] == "montage":
        frames = prep.neighbors(frame, span=3, count=6)
        return V.make_montage_view(prep, frames, frame, cols=L["cols"], rows=L["rows"],
                                   width=L["width"], pane="enh")
    return V.make_crop_view(prep, frame, center, size=L["size"], cols=L["cols"], rows=L["rows"],
                            upscale=L["upscale"], resample=L["resample"], panes=L["panes"])


def run_trial(pool: VlmPool, provider: str, prep, level: str, frame: int,
              gt: Tuple[float, float], rng: random.Random, use_cache: bool,
              dump: Optional[Path]) -> Trial:
    L = LEVELS[level]
    j = L["jitter"]
    ang = rng.uniform(0, 2 * np.pi)
    rad = j * np.sqrt(rng.uniform(0, 1))  # 面积均匀，别都堆在边上
    center = (gt[0] + rad * np.cos(ang), gt[1] + rad * np.sin(ang))
    view = build_view(prep, level, frame, center)

    n_frames = 6 if L["kind"] == "montage" else 1
    prompt = prompting.render(L["prompt"], view, n_frames=n_frames)
    img = V.encode(view.image, "png")
    if dump:
        dump.mkdir(parents=True, exist_ok=True)
        (dump / f"{level}_{frame}.png").write_bytes(img)

    p = pool.provider(provider)
    r = p.call(prompt, [img], system=prompting.SYSTEM, use_cache=use_cache)
    t = Trial(provider=provider, level=level, frame=frame, latency=r.latency, cached=r.cached,
              raw=(r.text or "")[:300], thinking_leak=r.reasoning_leaked,
              completion_tokens=r.usage.get("completion_tokens"),
              prompt_tokens=r.usage.get("prompt_tokens"),
              gt_cell=view.cell_of(*gt))
    if r.usage.get("reasoning_content_len"):
        t.thinking_leak = True
    if not r.ok:
        t.error = r.error
        return t
    obj = extract_json(r.text)
    if obj is None or "cell" not in obj:
        t.error = "解析不出 {cell:...}"
        return t
    t.ok = True
    t.conf = obj.get("conf") if isinstance(obj.get("conf"), (int, float)) else None
    cell = obj.get("cell")
    if cell is None or (isinstance(cell, str) and cell.strip().lower() in ("null", "none", "")):
        t.said_null = True
        return t
    t.cell = str(cell)
    c = view.cell_center(t.cell)
    if c is None:
        t.error = f"格子名不合法: {cell!r}"
        t.ok = False
        return t
    t.err_px = float(np.hypot(c[0] - gt[0], c[1] - gt[1]))
    return t


# ---------------------------------------------------------------- 汇总


def summarize(trials: List[Trial], levels: List[str], providers: List[str]) -> str:
    lines: List[str] = []
    w = "  {:<12s} {:>4s} {:>6s} {:>7s} {:>7s} {:>7s} {:>7s} {:>6s} {:>6s} {:>7s} {:>6s}"
    for lv in levels:
        cell_px = None
        sub_all = [t for t in trials if t.level == lv]
        if not sub_all:
            continue
        lines.append(f"\n【{lv}】" + (f"  每格 {cell_px}" if cell_px else ""))
        lines.append(w.format("供应商", "n", "答出率", "中位误差", "P90误差", "<8px", "<20px", "放弃率", "废答", "延迟", "视觉tok"))
        for pv in providers:
            sub = [t for t in sub_all if t.provider == pv]
            if not sub:
                continue
            n = len(sub)
            answered = [t for t in sub if t.err_px is not None]
            nulls = [t for t in sub if t.said_null]
            bad = [t for t in sub if not t.ok]
            errs = sorted(t.err_px for t in answered)
            lat = [t.latency for t in sub if not t.cached and t.latency > 0]
            ptok = [t.prompt_tokens for t in sub if t.prompt_tokens]
            lines.append(w.format(
                pv, str(n),
                f"{len(answered)/n:.0%}",
                f"{np.median(errs):.1f}" if errs else "-",
                f"{np.percentile(errs, 90):.1f}" if errs else "-",
                f"{sum(e < 8 for e in errs)/n:.0%}" if errs else "0%",
                f"{sum(e < 20 for e in errs)/n:.0%}" if errs else "0%",
                f"{len(nulls)/n:.0%}",
                str(len(bad)),
                f"{np.median(lat):.1f}s" if lat else "cache",
                str(int(np.median(ptok))) if ptok else "-",
            ))
    return "\n".join(lines)


def health_table(trials: List[Trial], providers: List[str]) -> str:
    lines = ["\n【连通性 / 鉴权 / 关思考】", "  {:<12s} {:>8s} {:>8s} {:>10s} {:>12s} {:>10s}".format(
        "供应商", "调用", "失败", "JSON可解析", "思考泄漏", "输出tok中位")]
    for pv in providers:
        sub = [t for t in trials if t.provider == pv]
        if not sub:
            continue
        ctok = [t.completion_tokens for t in sub if t.completion_tokens is not None]
        lines.append("  {:<12s} {:>8d} {:>8d} {:>10s} {:>12s} {:>10s}".format(
            pv, len(sub), sum(1 for t in sub if not t.ok),
            f"{sum(1 for t in sub if t.ok)/len(sub):.0%}",
            f"{sum(1 for t in sub if t.thinking_leak)} 次",
            str(int(np.median(ctok))) if ctok else "-"))
        errs = {t.error for t in sub if t.error}
        for e in list(errs)[:2]:
            lines.append(f"      失败样例: {e[:120]}")
    return "\n".join(lines)


def verdict(trials: List[Trial], providers: List[str]) -> str:
    """把结论写成一句人话——这才是这个脚本存在的意义。"""
    out = ["\n" + "=" * 78, "结论", "=" * 78]
    best = None
    for pv in providers:
        sub = [t for t in trials if t.level == "L2" and t.provider == pv]
        if not sub:
            continue
        ans = [t.err_px for t in sub if t.err_px is not None]
        hit = sum(1 for e in ans if e < 20) / len(sub) if sub else 0
        med = float(np.median(ans)) if ans else float("inf")
        lat = np.median([t.latency for t in sub if not t.cached and t.latency > 0] or [0])
        verd = "看得见" if hit >= 0.6 else ("勉强" if hit >= 0.3 else "看不见")
        out.append(f"  {pv:<12s} L2 命中(<20px) {hit:.0%}  中位误差 {med:.1f}px  中位延迟 {lat:.1f}s  → {verd}")
        if hit >= 0.6 and (best is None or med < best[1]):
            best = (pv, med)
    if best:
        out.append(f"\n  可以往下做：{best[0]} 在 L2 上能稳定看见球（中位误差 {best[1]:.1f} px）。")
    else:
        out.append("\n  三家在 L2 都看不见球。按计划的风险预案，应在 L2 之前插入本地 DoG blob")
        out.append("  候选做约束的 `local_propose` 阶段，把模型的任务从「找球」降级为「在几个")
        out.append("  候选里挑一个」。")
    return "\n".join(out)


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description="VLM 冒烟测试：三家模型能不能看见球")
    ap.add_argument("--packet", default="260122__002__1703_1713")
    ap.add_argument("--work", type=Path, default=DEFAULT_WORK)
    ap.add_argument("--gt", type=Path, default=DEFAULT_GT)
    ap.add_argument("--packets", type=Path, default=DEFAULT_PACKETS)
    ap.add_argument("--levels", nargs="+", default=["L2"])
    ap.add_argument("--providers", nargs="+", default=None)
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=20260910)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--dump", action="store_true", help="把入模图片落盘，方便肉眼核对")
    ap.add_argument("--json-out", type=Path, default=None)
    a = ap.parse_args()

    pool = VlmPool(use_cache=not a.no_cache)
    provs = a.providers or [n for n in pool.available() if "text" not in n]
    missing = [n for n, c in pool.cfgs.items() if not c.has_key]
    print(f"可用供应商: {provs}" + (f"   (缺 key: {missing})" if missing else ""))

    work = a.work
    if not (work / a.packet / "prepare.json").exists():
        found = data_roots.find_prepared(a.packet)
        if (found / a.packet / "prepare.json").exists():
            work = found
    zp = a.packets / f"{a.packet}.zip"
    if not (work / a.packet / "prepare.json").exists() and zp.exists():
        print(f"准备 {a.packet} …")
        prepare_packet(zp, work)
    prep = load_prepared(work, a.packet)
    gt = load_gt(a.gt, a.packet, prep)
    if not gt:
        raise SystemExit(f"{a.packet} 没有可用的人工 GT，无从判断模型答得对不对")

    rng = random.Random(a.seed)
    frames = sorted(gt)
    pick = [frames[i] for i in np.linspace(0, len(frames) - 1, min(a.frames, len(frames))).astype(int)]
    print(f"包 {a.packet}: ROI {prep.size}, 人工 GT {len(gt)} 帧, 抽测 {len(pick)} 帧")
    print(f"级别 {a.levels}，每级每家 {len(pick)} 次调用，共 {len(a.levels)*len(provs)*len(pick)} 次\n")

    dump = (OUT_DIR / a.packet) if a.dump else None
    jobs = [(pv, lv, f) for lv in a.levels for pv in provs for f in pick]
    trials: List[Trial] = []
    t0 = time.time()

    def work(job):
        pv, lv, f = job
        r = random.Random(a.seed + f)
        try:
            return run_trial(pool, pv, prep, lv, f, gt[f], r, not a.no_cache,
                             dump if pv == provs[0] else None)
        except Exception as e:  # noqa: BLE001
            return Trial(provider=pv, level=lv, frame=f, error=f"{type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, t in enumerate(ex.map(work, jobs), 1):
            trials.append(t)
            mark = "·" if t.cached else ("✓" if t.err_px is not None and t.err_px < 20 else
                                         ("○" if t.said_null else ("✗" if not t.ok else "×")))
            print(f"\r  {i}/{len(jobs)} {mark}", end="", flush=True)
    print(f"\n耗时 {time.time()-t0:.0f}s，缓存命中 {pool.cache_hits}/{pool.calls}")

    print(health_table(trials, provs))
    print(summarize(trials, a.levels, provs))
    print(verdict(trials, provs))

    # 几条原始回答，判断提示词有没有被理解
    print("\n【原始回答抽样】")
    for pv in provs:
        s = [t for t in trials if t.provider == pv and t.level == a.levels[-1]][:2]
        for t in s:
            print(f"  {pv}/{t.level}/f{t.frame}: GT在{t.gt_cell} 答{t.cell!r} "
                  f"conf={t.conf} err={t.err_px if t.err_px is None else round(t.err_px,1)} "
                  f"| {t.raw[:110]!r}")

    outp = a.json_out or (OUT_DIR / f"smoke_{a.packet}_{int(time.time())}.json")
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps([asdict(t) for t in trials], ensure_ascii=False, indent=1))
    print(f"\n明细写到 {outp}")
    if dump:
        print(f"入模图片在 {dump}")


if __name__ == "__main__":
    main()
