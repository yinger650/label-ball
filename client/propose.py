#!/usr/bin/env python3
"""本地 DoG blob 候选：把「在 4036×658 里找球」降级成「在 8 个候选里挑一个」。

计划的风险一节里预留的退路，实测证明**必须走**：L0 那张 1024 宽的多帧 montage 上，
球只剩 2 px，四家模型全军覆没（8 帧无一命中）。原因不是模型笨，是那张图里根本
没有足够的信息——球和几十个球员碎块在同一个亮度、同一个尺度上。

所以粗定位这一步交给本地算法，它有模型没有的两个优势：

1. **它能按原生分辨率看**。不需要为了塞进上下文而缩图，8 px 的球就是 8 px。
2. **它能用时序**。球的定义就是「在连续帧间沿一条连贯路径移动的孤立小亮斑」，
   这是个可以直接写成代码的判据，不必指望模型从拼图里悟出来。

反过来，本地算法做不到的是**判断这个亮斑到底是不是球**——草地反光、球员小腿、
边线交点都能产出同尺度的响应。所以两边分工：**本地负责召回，模型负责甄别**。
候选点渲染成带编号的视图交给模型选一个，模型的任务从「大海捞针」变成「多选题」。

召回率是这条路的唯一指标：候选里没有真值，后面的模型再准也白搭。
`--eval` 模式直接拿人工 GT 算 top-K 召回。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@dataclass
class Candidate:
    x: float  # ROI 局部像素
    y: float
    score: float  # 归一化 0..1，仅用于排序
    area: float
    peak: int
    elong: float  # 长短轴比，拖影会明显大于 1
    source: str = "blob"

    def as_dict(self) -> dict:
        return {"x": round(self.x, 1), "y": round(self.y, 1), "score": round(self.score, 3),
                "area": round(self.area, 1), "peak": self.peak, "elong": round(self.elong, 2),
                "source": self.source}


# 球在 DoG 图上的形态先验。放宽比放严好：漏掉真值是不可挽回的，
# 多几个假候选只是让模型多做几道选择题。
MIN_AREA = 6
MAX_AREA = 1200
MIN_PEAK = 55
SEG_THR = 60  # 分割阈值。取低一点让暗球也能成块，甄别交给打分而不是阈值
BALL_DIAM = 13.0  # 期望球径（px），实测这批 ROI 上 8–30 px，取中间偏小
DIAM_SIGMA = 11.0


def activity_map(prep, n_samples: int = 24, blur: int = 25) -> np.ndarray:
    """整包的「常年在动」图：滤波图在时间上的均值。

    这是把召回从 7% 拉到可用的关键一步。ROI 的上边缘总会切进一块看台，观众席在
    DoG 上是**一整片持续饱和的碎亮斑**，尺寸、亮度、孤立度和球一模一样——按单帧
    形态根本分不开，实测排名前 5 的候选全在那儿。

    但它们和球有一个决定性的区别：**看台每一帧都在动，球只是路过**。同一个像素上
    对时间求均值，看台是持续高值，草地上球飞过的地方是短暂尖峰、均值接近零。
    一张图就把整条上边缘按下去了，而且不需要任何「y 坐标小于多少」这种会过拟合到
    某个机位的硬编码。

    结果缓存在 prep 上：一包算一次，后面上百帧共用。
    """
    cached = getattr(prep, "_activity", None)
    if cached is not None:
        return cached
    fr = prep.frames
    idx = np.linspace(0, len(fr) - 1, min(n_samples, len(fr))).astype(int)
    acc = None
    for k in idx:
        g = prep.enh(fr[k]).astype(np.float32)
        acc = g if acc is None else acc + g
    m = (acc / max(1, len(idx))).astype(np.float32)
    m = cv2.GaussianBlur(m, (0, 0), blur)
    try:
        prep._activity = m
    except Exception:
        pass
    return m


def _score(enh: np.ndarray, x0: int, y0: int, bw_: int, bh_: int, area: float) -> Tuple[float, int, float]:
    """给一个连通块打分。返回 (score, peak, isolation)。

    第一版只按「阈值档位」排序，结果召回只有 5%——因为一张 DoG 图上过阈的连通块
    有一两百个，光看亮度根本分不出球。真正能把球和球员的碎块区分开的是**孤立度**：

        isolation = 块内峰值 / 块外一圈环形邻域里的峰值

    球周围是干净的草地，DoG 上一片黑，比值能到 3 以上；球员小腿上的一小块亮斑
    周围还是球员，比值接近 1。这一项是整个打分里权重最重的，也是唯一一个
    「用上了『孤立』这个定义」的项——之前那版把球的定义写在注释里，却没写进代码。
    """
    h, w = enh.shape[:2]
    peak = int(enh[y0 : y0 + bh_, x0 : x0 + bw_].max())
    pad = int(max(6, 1.6 * max(bw_, bh_)))
    ax0, ay0 = max(0, x0 - pad), max(0, y0 - pad)
    ax1, ay1 = min(w, x0 + bw_ + pad), min(h, y0 + bh_ + pad)
    ring = enh[ay0:ay1, ax0:ax1].astype(np.int32).copy()
    ring[y0 - ay0 : y0 - ay0 + bh_, x0 - ax0 : x0 - ax0 + bw_] = -1
    rmax = int(ring.max()) if ring.size else 0
    isolation = peak / max(rmax, 12)

    diam = 2.0 * np.sqrt(area / np.pi)
    size = float(np.exp(-((diam - BALL_DIAM) ** 2) / (2 * DIAM_SIGMA**2)))
    elong = max(bw_, bh_) / max(1.0, min(bw_, bh_))
    shape = 1.0 / (1.0 + 0.25 * max(0.0, elong - 2.5))  # 允许拖影拉长，但不能是长条
    fill = area / max(1.0, bw_ * bh_)  # 实心度：球是实的，交叉的线是空的

    s = (peak / 255.0) ** 0.5 * min(isolation, 4.0) / 4.0 * size * shape * (0.5 + 0.5 * fill)
    return float(s), peak, float(isolation)


def find_blobs(
    enh: np.ndarray,
    window: Optional[Tuple[float, float, float]] = None,
    top_k: int = 8,
    min_peak: int = MIN_PEAK,
    min_area: int = MIN_AREA,
    max_area: int = MAX_AREA,
    max_elong: float = 12.0,
    activity: Optional[np.ndarray] = None,
    play: Optional[np.ndarray] = None,
) -> List[Candidate]:
    """在滤波图上找孤立亮斑。`window` = (cx, cy, radius)，给了就只在窗内找。

    单一阈值分割 + 形态打分，不再逐级降阈值：降阈值那套的问题是同一块东西在
    不同档位上重复出现，而档位本身又被当成了主排序键，等于用「有多亮」代替了
    「像不像球」。现在阈值只负责把候选圈出来，排序完全交给 `_score`。

    `play` 是 `field.play_mask` 的硬掩膜：出场区的亮斑连分割都不参与。候选只是
    「值得一看的位置」，而灯光、场外车流在这张图上和球一模一样，让它们进 top-8
    等于把模型往看台上引。冷启动阶段本来也指望不上高抛球。
    """
    h, w = enh.shape[:2]
    _, bw = cv2.threshold(enh, SEG_THR, 255, cv2.THRESH_BINARY)
    if play is not None and play.shape[:2] == (h, w):
        bw = cv2.bitwise_and(bw, play)
    if window is not None:
        cx, cy, r = window
        m = np.zeros((h, w), np.uint8)
        cv2.circle(m, (int(round(cx)), int(round(cy))), int(round(r)), 255, -1)
        bw = cv2.bitwise_and(bw, m)
    # 闭一下：拖影常常断成两三截，不接起来会被拆成好几个小候选
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

    n, _, stats, cent = cv2.connectedComponentsWithStats(bw, 8)
    out: List[Candidate] = []
    for i in range(1, n):
        area = float(stats[i, cv2.CC_STAT_AREA])
        if not (min_area <= area <= max_area):
            continue
        bwid, bhei = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
        elong = max(bwid, bhei) / max(1.0, min(bwid, bhei))
        if elong > max_elong:
            continue  # 边线、广告牌横条
        x0, y0 = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP])
        s, peak, iso = _score(enh, x0, y0, bwid, bhei, area)
        if peak < min_peak:
            continue
        cx_, cy_ = float(cent[i][0]), float(cent[i][1])
        if activity is not None:
            a = float(activity[min(h - 1, max(0, int(cy_))), min(w - 1, max(0, int(cx_)))])
            s *= 1.0 / (1.0 + (a / 8.0) ** 1.5)
        out.append(Candidate(cx_, cy_, s, area, peak, elong))
    out.sort(key=lambda c: -c.score)
    return out[:top_k]


def temporal_consistency(
    prep,
    frame_idx: int,
    cands: Sequence[Candidate],
    span: int = 1,
    match_px: float = 260.0,
) -> List[Candidate]:
    """按「前后帧附近有没有同类亮斑」给候选加权。

    球在相邻目标帧里一定还在附近（step=3 时最多跑 390 px），而草地反光、
    压缩噪点这类假候选是**闪的**——这一帧有下一帧没有。球员则相反，他们连续存在
    但形状大、面积早被 `max_area` 滤掉了。所以时序一致性主要用来压掉闪烁噪点。
    """
    step = int(getattr(prep.meta, "step", 1) or 1)
    act = activity_map(prep)
    play = _play_mask(prep)
    neigh: List[List[Candidate]] = []
    for k in (-span, span):
        fi = frame_idx + k * step
        if prep.has(fi):
            neigh.append(find_blobs(prep.enh(fi), top_k=40, activity=act, play=play))
    if not neigh:
        return list(cands)
    out = []
    for c in cands:
        votes = sum(
            any(np.hypot(c.x - o.x, c.y - o.y) < match_px for o in lst) for lst in neigh
        )
        c2 = Candidate(c.x, c.y, min(1.0, c.score * (1.0 + 0.35 * votes)), c.area, c.peak, c.elong, c.source)
        out.append(c2)
    out.sort(key=lambda c: -c.score)
    return out


def _play_mask(prep) -> Optional[np.ndarray]:
    """`field.play_mask`，拿不到就返回 None（掩膜是加分项，不该拖垮候选生成）。"""
    try:
        from client import field as F

        return F.play_mask(prep)
    except Exception:  # noqa: BLE001
        return None


def propose(
    prep,
    frame_idx: int,
    prior: Optional[Tuple[float, float]] = None,
    radius: float = 420.0,
    top_k: int = 8,
    use_temporal: bool = True,
) -> List[Candidate]:
    """一帧的候选列表。有先验就先在先验邻域里找，找不到再退回全图。"""
    enh = prep.enh(frame_idx)
    act = activity_map(prep)
    play = _play_mask(prep)
    cands: List[Candidate] = []
    if prior is not None:
        cands = find_blobs(enh, window=(prior[0], prior[1], radius), top_k=top_k,
                           activity=act, play=play)
    if not cands:
        cands = find_blobs(enh, top_k=top_k, activity=act, play=play)
    if use_temporal and cands:
        cands = temporal_consistency(prep, frame_idx, cands)
    if prior is not None and cands:
        # 有先验时按距离再排一次：分数相近的情况下离预测近的更可信
        cands.sort(key=lambda c: (-c.score, np.hypot(c.x - prior[0], c.y - prior[1])))
    return cands[:top_k]


def recall_at(cands: Sequence[Candidate], gt: Tuple[float, float], tol: float, k: int) -> bool:
    return any(np.hypot(c.x - gt[0], c.y - gt[1]) <= tol for c in list(cands)[:k])


# ---------------------------------------------------------------- 评测


def main() -> None:
    from client.prep import load_prepared, pano_to_roi

    ap = argparse.ArgumentParser(description="本地 blob 候选的召回率评测")
    ap.add_argument("--packets", nargs="+", default=["260122__002__1703_1713", "260116__004__1533_1543",
                                                     "260108__003__0061_0071"])
    ap.add_argument("--work", type=Path, default=Path("/mnt/afs/wangmin/data/_ai_label_work/work"))
    ap.add_argument("--gt", type=Path, default=Path("/mnt/afs/wangmin/data/_ai_label_work/gt"))
    ap.add_argument("--tol", type=float, default=24.0)
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--radius", type=float, default=420.0)
    a = ap.parse_args()

    tot = {"n": 0, "any": 0}
    per_k = {k: 0 for k in (1, 2, 3, 5, 8)}
    per_k_prior = {k: 0 for k in (1, 2, 3, 5, 8)}
    n_prior = 0
    ranks: List[int] = []
    for pid in a.packets:
        if not (a.work / pid / "prepare.json").exists():
            print(f"跳过 {pid}（还没 prepare）")
            continue
        prep = load_prepared(a.work, pid)
        gt: Dict[int, Tuple[float, float]] = {}
        for f in a.gt.glob(f"{pid}__*.json"):
            d = json.loads(f.read_text())
            if d.get("annotator") == "smoketest":
                continue
            for r in d.get("labels", []):
                if r.get("visible") and r.get("pano_x") is not None:
                    gt[int(r["frame_idx"])] = pano_to_roi(r["pano_x"], r["pano_y"], prep.roi, prep.pano_w)
        pn, pa = 0, 0
        frames = sorted(gt)
        for i, fi in enumerate(frames):
            cs = propose(prep, fi, top_k=a.top_k)
            tot["n"] += 1
            pn += 1
            hit = recall_at(cs, gt[fi], a.tol, a.top_k)
            tot["any"] += hit
            pa += hit
            for k in per_k:
                per_k[k] += recall_at(cs, gt[fi], a.tol, k)
            for j, c in enumerate(cs):
                if np.hypot(c.x - gt[fi][0], c.y - gt[fi][1]) <= a.tol:
                    ranks.append(j)
                    break
            # 带先验（用真值的前一帧模拟外推）
            if i > 0 and frames[i - 1] in gt:
                pr = gt[frames[i - 1]]
                cs2 = propose(prep, fi, prior=pr, radius=a.radius, top_k=a.top_k)
                n_prior += 1
                for k in per_k_prior:
                    per_k_prior[k] += recall_at(cs2, gt[fi], a.tol, k)
        print(f"  {pid}: {pa}/{pn} = {pa/max(pn,1):.0%} (top-{a.top_k}, 容差 {a.tol:.0f}px)")

    n = max(tot["n"], 1)
    print(f"\n全图搜索 ({n} 帧, 容差 {a.tol:.0f}px):")
    for k in sorted(per_k):
        print(f"  top-{k} 召回 {per_k[k]/n:6.1%}")
    if ranks:
        print(f"  命中时的中位排名 {int(np.median(ranks))} (0 起)")
    if n_prior:
        print(f"\n带先验 (半径 {a.radius:.0f}px, {n_prior} 帧):")
        for k in sorted(per_k_prior):
            print(f"  top-{k} 召回 {per_k_prior[k]/n_prior:6.1%}")


if __name__ == "__main__":
    main()
