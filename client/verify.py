#!/usr/bin/env python3
"""本地核验：给模型选出来的那个点打一个「像不像球」的分，**不花任何模型调用**。

## 为什么要有这一层

标定里误差的中位数只有 9.9 px，p90 却到 97 px。也就是说链路本身是准的，毁掉平均
成绩的是少数「换了目标」的帧 —— 球员的白袜子、边线交点、场外的灯。对这些帧，
模型照样给 0.9 的置信度：**它的 conf 只反映「我看见了一个符合描述的东西」，
不反映「这个东西真的是比赛球」。** 所以光调 `accept_conf` 拦不住，得换判据。

而我们手里有一堆模型看不到、但代码算得出的证据：球场几何、原生分辨率的 DoG 响应、
草地的颜色基线、前几帧已定点连成的轨迹。这些全是本地计算，几毫秒一帧。

## 怎么合成

每一项证据映射到 `[floor, 1]` 再**相乘**，而不是加权求和：

    score = zone × (floor_i + (1-floor_i) × v_i) 的连乘

相乘的意义是「几条独立证据都说不像，就真的不像」—— 一项塌掉扣到 0.5 左右，两项
一起塌掉就掉到 0.3。加权求和做不到这件事：单项归零只会按权重扣一点，而恰恰是
「多项同时可疑」才最能区分换了目标的帧。

`floor` 不取 0，是因为每一项都有它合法的失灵场合：球静止时 DoG 很弱、球贴着白色
边线时 RGB 球感差、序列头几帧没有轨迹可比。让任一项单独否决整帧会把召回砍到没法用。

球场几何是唯一的**硬否决**（`zone == out` 直接 0）：那不是「不太像」，那是
物理上不可能 —— 看台和天空里没有比赛球。

## 门限在哪

`judge()` 只产出 `score`，弃不弃帧由调用方按 `final_conf = model_conf × score`
和工作流 yaml 里的 `verify.gate` 决定。门限必须能离线扫（`scripts/calibrate.py
--sweep-gate`），所以判据和门限刻意分开：判据算一次，门限扫十档。

## 判别力实测

各项的参考尺度不是猜的，用 `python -m client.verify --stats` 在人工 GT 上量。两组
样本：299 个人工 GT 点，以及故意偏 40–150 px 的同数量错点（模拟「锁到球员脚上」）。

    项       GT 中位   GT p10    错点中位   错点 p90
    dog       1.000    0.546     0.000     0.831
    rgb       1.000    1.000     0.310     1.000
    kine      1.000    0.882     0.000     0.000
    score     0.998    0.712     0.209     0.393

合成分在 0.40–0.70 之间任取一档都能把两组切开，真实工作点由 `--sweep-gate` 在
整链输出上定（错点是人造的，真实错帧的分布不一样）。两组分布拉不开的项就是没用的
项 —— `dog` 最初写成复用 `propose._score` 的形态分，一量发现形态完全不判别，
才改成现在的「有没有 × 多近」。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from client import field as F

# --- DoG 亮斑 ---
# 这一项量的是**答案离最近的运动亮斑有多远**，不是那个亮斑「像不像球」。
#
# 原本写的是复用 `propose._score` 的形态分，结果在 GT 上一量就发现形态分不判别：
# 人工 GT 点处的形态分中位 0.150，故意偏 40–150 px 的错点处是 0.090 —— 两条分布
# 几乎重叠，面积（266 vs 145）、峰值（255 vs 255）、孤立度（1.0 vs 1.0）同样不分。
# 真正分得开的只有距离：GT 中位 7.8 px / p90 13.2 px，错点中位 19.5 px / p90 28.3 px。
# 而且**有没有亮斑**本身就是强信号：299 个 GT 点全都有，错点只有一半有。
#
# 所以这一项 = 有没有 × 多近，形态只留一个宽松的面积闸门挡掉整个球员那么大的块。
DOG_FULL = 10.0  # 这么近记满分
DOG_ZERO = 28.0  # 这么远记 0；再远视作「附近没有亮斑」
DOG_AREA_MIN = 6.0
DOG_AREA_MAX = 1600.0  # GT 最大 1067、错点最大 1191：这个闸门只挡明显荒谬的块
DOG_FLOOR = 0.60

# --- RGB 球感 ---
RGB_PATCH = 34.0  # patch 边长（ROI 像素）。约两个球径，够拿到草地基线
RGB_MIN_R = 1.5  # 亮斑等效半径下限（px）。比这还小是压缩噪点
RGB_MAX_R = 16.0  # 上限。比这还大是球员/边线
RGB_CONTRAST_REF = 42.0  # 亮斑峰值高出 patch 中位数多少算「明显」
RGB_FLOOR = 0.55

# --- 运动学 ---
# 残差小于 max(KINE_ABS, KINE_K1×拟合尺度) 记满分，大到 KINE_K2 倍时归零。
KINE_ABS = 14.0
KINE_K1 = 2.0
KINE_K2 = 6.0
KINE_MAX = 90.0  # 绝对上限：超过这个残差一律 0，不管拟合尺度多大
KINE_MIN_POINTS = 3  # 少于这么多已定点就不判（拟合尺度没意义）
KINE_FLOOR = 0.45  # 最狠的一项：偏离轨迹几乎等同于换了目标

# --- 级间一致 ---
CHAIN_FLOOR = 0.65


@dataclass
class Evidence:
    """一帧的核验结论。所有字段都要能落进 inspect 报告——门限没法盲调。"""

    score: float = 1.0
    zone: str = F.IN_PLAY
    zone_score: float = 1.0
    dog: Optional[float] = None
    rgb: Optional[float] = None
    kine: Optional[float] = None
    chain: Optional[float] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in ("score", "zone_score", "dog", "rgb", "kine", "chain"):
            if d.get(k) is not None:
                d[k] = round(float(d[k]), 3)
        return d


def _term(v: Optional[float], floor: float) -> float:
    """把一项证据映射到 [floor, 1]。缺这一项时返回 1（中性，不加不减）。"""
    if v is None:
        return 1.0
    return float(floor + (1.0 - floor) * float(np.clip(v, 0.0, 1.0)))


def _falloff(d: float, full: float, zero: float) -> float:
    """距离 d 在 [full, zero] 之间线性衰减。"""
    if d <= full:
        return 1.0
    if d >= zero:
        return 0.0
    return float((zero - d) / max(zero - full, 1e-6))


# ---------------------------------------------------------------- DoG 亮斑


def dog_evidence(prep, frame_idx: int, x: float, y: float,
                 zero: float = DOG_ZERO) -> Tuple[float, dict]:
    """答案处到底有没有东西在动，以及离那个动的东西有多近。

    用**未压暗**的原始 DoG 图：出场区的事已经由 `zone` 硬否决了，这里再压一次
    会让软带里的高抛球连亮斑证据都拿不到。
    """
    from client.propose import SEG_THR

    enh = prep.enh(frame_idx)
    h, w = enh.shape[:2]
    pad = int(zero) + 8
    x0, y0 = int(max(0, x - pad)), int(max(0, y - pad))
    x1, y1 = int(min(w, x + pad)), int(min(h, y + pad))
    if x1 <= x0 or y1 <= y0:
        return 0.0, {"why": "答案在 ROI 外"}
    sub = enh[y0:y1, x0:x1]
    _, bw = cv2.threshold(sub, SEG_THR, 255, cv2.THRESH_BINARY)
    n, _lab, stats, cent = cv2.connectedComponentsWithStats(bw, 8)
    best: Optional[Tuple[float, int]] = None
    for i in range(1, n):
        area = float(stats[i, cv2.CC_STAT_AREA])
        if not (DOG_AREA_MIN <= area <= DOG_AREA_MAX):
            continue
        d = float(np.hypot(float(cent[i][0]) + x0 - x, float(cent[i][1]) + y0 - y))
        if best is None or d < best[0]:
            best = (d, i)
    if best is None:
        return 0.0, {"why": "附近 DoG 上没有球尺度的亮斑"}
    d, i = best
    if d >= zero:
        return 0.0, {"why": f"最近的亮斑在 {d:.0f}px 外", "dist": round(d, 1)}
    return _falloff(d, DOG_FULL, zero), {
        "dist": round(d, 1),
        "area": round(float(stats[i, cv2.CC_STAT_AREA]), 1),
        "peak": int(sub.max()),
    }


# ---------------------------------------------------------------- RGB 球感


def rgb_evidence(prep, frame_idx: int, x: float, y: float,
                 patch: float = RGB_PATCH) -> Tuple[float, dict]:
    """原图上那儿是不是一小团比草地明显亮的、尺寸对得上球的东西。

    「以 RGB 为主」这句口径必须有代码兑现，否则它只是提示词里的一句劝告。判据故意
    只用**局部相对量**（相对本 patch 的中位亮度），不用绝对阈值：夜场、白天、
    不同曝光的包草地亮度差得很远，绝对阈值一定会过拟合到某几个包。
    """
    raw = prep.raw(frame_idx)
    h, w = raw.shape[:2]
    half = patch / 2.0
    x0, y0 = int(max(0, x - half)), int(max(0, y - half))
    x1, y1 = int(min(w, x + half)), int(min(h, y + half))
    if x1 - x0 < 6 or y1 - y0 < 6:
        return 0.0, {"why": "答案太靠边，取不到 patch"}
    sub = raw[y0:y1, x0:x1]
    gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY) if sub.ndim == 3 else sub
    med = float(np.median(gray))
    # 球心那一小块的峰值：只在答案 ±4px 里找，避免把 patch 角上的球员白袜子算进来
    cx, cy = int(round(x)) - x0, int(round(y)) - y0
    r = 4
    core = gray[max(0, cy - r): cy + r + 1, max(0, cx - r): cx + r + 1]
    if core.size == 0:
        return 0.0, {"why": "球心不在 patch 内"}
    peak = float(core.max())
    contrast = peak - med
    if contrast <= 4.0:
        return 0.0, {"why": "答案处不比草地亮", "contrast": round(contrast, 1), "med": round(med, 1)}

    # 亮斑的等效半径：在「中位数 + 一半对比度」这条线上分割，取包含球心的那块
    thr = med + 0.5 * contrast
    _, bw = cv2.threshold(gray, thr, 255, cv2.THRESH_BINARY)
    n, lab, stats, _cent = cv2.connectedComponentsWithStats(bw, 8)
    li = int(lab[min(lab.shape[0] - 1, max(0, cy)), min(lab.shape[1] - 1, max(0, cx))])
    if li <= 0:
        # 球心正好卡在阈值下沿：用核心区里最亮那点所在的块
        ys, xs = np.nonzero(core >= peak - 1)
        if ys.size:
            py = max(0, cy - r) + int(ys[0])
            px = max(0, cx - r) + int(xs[0])
            li = int(lab[min(lab.shape[0] - 1, py), min(lab.shape[1] - 1, px)])
    if li <= 0:
        return 0.0, {"why": "球心不在亮斑里", "contrast": round(contrast, 1)}
    area = float(stats[li, cv2.CC_STAT_AREA])
    rad = float(np.sqrt(area / np.pi))
    size_ok = 1.0 if RGB_MIN_R <= rad <= RGB_MAX_R else _falloff(
        min(abs(rad - RGB_MAX_R), abs(rad - RGB_MIN_R)), 0.0, 8.0)
    con_ok = float(np.clip(contrast / RGB_CONTRAST_REF, 0.0, 1.0))
    v = con_ok * size_ok
    return float(v), {"contrast": round(contrast, 1), "med": round(med, 1),
                      "radius": round(rad, 1), "area": round(area, 1)}


# ---------------------------------------------------------------- 运动学


def kine_evidence(history: Sequence[Tuple[int, float, float]], frame_idx: int,
                  x: float, y: float, step: float = 1.0) -> Tuple[Optional[float], dict]:
    """答案是不是落在前几帧连成的那条轨迹的自然延长线上。

    球的运动是平滑的，所以「拿邻居拟合出来的预测」和「答案」之间的残差有一个很小的
    自然尺度（噪声 2–3 px 加上未知加速度）。锁到球员脚上通常偏 20–100 px —— 在窗口
    里，但相对这个尺度是好几个 σ。这一项因此是**最能识别换了目标**的证据。

    残差尺度用拟合自己的残差 RMS 估，而不是写死一个像素数：慢速球的尺度可以是
    2 px，长传落地反弹段能到 15 px，写死一定会在某一端错。

    `history` 是已定点 `(frame_idx, x, y)`，不含当前帧；不够 `KINE_MIN_POINTS`
    个就返回 None（这一项弃权，不是判 0）。第二遍捡漏时两侧都有点，残差会更小。
    """
    from numpy.polynomial import polynomial as npoly

    pts = [(float(f), float(px), float(py)) for f, px, py in (history or [])
           if f is not None and px is not None and py is not None and int(f) != int(frame_idx)]
    pts.sort(key=lambda q: abs(q[0] - float(frame_idx)))
    pts = pts[:5]
    if len(pts) < KINE_MIN_POINTS:
        return None, {"why": f"只有 {len(pts)} 个已定点，不足 {KINE_MIN_POINTS}"}
    pts.sort(key=lambda q: q[0])
    st = float(step) if step and step > 0 else 1.0
    t = np.array([(q[0] - float(frame_idx)) / st for q in pts], dtype=float)
    vx = np.array([q[1] for q in pts], dtype=float)
    vy = np.array([q[2] for q in pts], dtype=float)
    deg = 2 if len(pts) >= 4 else 1
    cx = npoly.polyfit(t, vx, deg)
    cy = npoly.polyfit(t, vy, deg)
    px, py = float(np.atleast_1d(cx)[0]), float(np.atleast_1d(cy)[0])
    resid = float(np.hypot(x - px, y - py))
    fit = float(np.sqrt(np.mean((vx - npoly.polyval(t, cx)) ** 2
                                + (vy - npoly.polyval(t, cy)) ** 2)))
    full = max(KINE_ABS, KINE_K1 * fit)
    zero = min(KINE_MAX, max(full * 2.0, KINE_K2 * max(fit, 2.0)))
    v = _falloff(resid, full, zero)
    return float(v), {"resid": round(resid, 1), "fit_scale": round(fit, 1),
                      "full": round(full, 1), "zero": round(zero, 1),
                      "predict": [round(px, 1), round(py, 1)], "n_points": len(pts)}


# ---------------------------------------------------------------- 级间一致


def chain_evidence(stages: Sequence[Any], x: float, y: float) -> Tuple[Optional[float], dict]:
    """最细那一级和它上一级对不对得上。对不上说明细化窗里混进了第二个亮斑。

    工作流的 `_accept` 已经用同样的几何在「采纳哪一级」上做过一次判断，这里量的是
    **程度**：刚好卡在容差边缘和完全重合，应该给不一样的置信度。
    """
    ok = [s for s in (stages or []) if getattr(s, "ok", False) and getattr(s, "x", None) is not None]
    if len(ok) < 2:
        return None, {"why": "只有一级给出坐标"}
    a, b = ok[-2], ok[-1]
    cell = max(float(getattr(a, "cell_px", 0.0) or 0.0), 1.0)
    d = float(np.hypot(float(a.x) - float(b.x), float(a.y) - float(b.y)))
    v = _falloff(d, cell, 3.0 * cell + 8.0)
    return float(v), {"pair": [getattr(a, "stage", "?"), getattr(b, "stage", "?")],
                      "dist": round(d, 1), "cell_px": round(cell, 1)}


# ---------------------------------------------------------------- 合成


def judge(
    prep,
    frame_idx: int,
    x: float,
    y: float,
    *,
    history: Optional[Sequence[Tuple[int, float, float]]] = None,
    stages: Optional[Sequence[Any]] = None,
    step: Optional[float] = None,
    geom: Optional[F.FieldGeom] = None,
) -> Evidence:
    """给一个点打核验分。纯本地，不调模型。"""
    ev = Evidence()
    g = geom if geom is not None else F.build(prep)
    ev.zone = g.zone(x, y)
    ev.zone_score = F.ZONE_SCORE[ev.zone]
    if ev.zone == F.OUT:
        ev.score = 0.0
        ev.reasons.append(f"出场区（球场边界上方 {g.boundary_y(x) - y:.0f}px），硬否决")
        return ev
    if ev.zone == F.SOFT:
        ev.reasons.append("在边界上方的空中带里，需要其它证据背书")

    st = float(step) if step else float(getattr(prep.meta, "step", 1) or 1)
    ev.dog, d_dog = dog_evidence(prep, frame_idx, x, y)
    ev.rgb, d_rgb = rgb_evidence(prep, frame_idx, x, y)
    ev.kine, d_kine = kine_evidence(history or (), frame_idx, x, y, st)
    ev.chain, d_chain = chain_evidence(stages or (), x, y)
    ev.detail = {"dog": d_dog, "rgb": d_rgb, "kine": d_kine, "chain": d_chain,
                 "boundary_dy": round(float(g.boundary_y(x)) - float(y), 1)}

    ev.score = float(
        ev.zone_score
        * _term(ev.dog, DOG_FLOOR)
        * _term(ev.rgb, RGB_FLOOR)
        * _term(ev.kine, KINE_FLOOR)
        * _term(ev.chain, CHAIN_FLOOR)
    )
    for name, v, thr in (("DoG 亮斑", ev.dog, 0.25), ("RGB 球感", ev.rgb, 0.25),
                         ("轨迹一致", ev.kine, 0.25), ("级间一致", ev.chain, 0.25)):
        if v is not None and v < thr:
            ev.reasons.append(f"{name}弱({v:.2f})")
    return ev


# ---------------------------------------------------------------- 量一量


def _stats_main() -> None:
    """在人工 GT 上量每一项证据的判别力。

        venv/bin/python -m client.verify --stats

    两组样本：GT 点本身，以及**故意偏 40–150 px 的错点**（模拟「锁到球员脚上」）。
    两组分布拉不开的项就是没用的项，该改判据而不是改权重。
    """
    import argparse
    import json
    from pathlib import Path

    from client.prep import load_prepared, pano_to_roi

    ap = argparse.ArgumentParser(description="核验判据的判别力体检")
    ap.add_argument("--work", type=Path, default=Path("/mnt/afs/wangmin/data/_ai_label_work/work"))
    ap.add_argument("--gt", type=Path, default=Path("/mnt/afs/wangmin/data/_ai_label_work/gt"))
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--seed", type=int, default=20260914)
    a = ap.parse_args()

    rng = np.random.default_rng(a.seed)
    good: Dict[str, List[float]] = {k: [] for k in ("dog", "rgb", "kine", "score")}
    bad: Dict[str, List[float]] = {k: [] for k in ("dog", "rgb", "kine", "score")}

    for d in sorted(a.work.iterdir()):
        if not (d / "prepare.json").exists():
            continue
        prep = load_prepared(a.work, d.name)
        gt: Dict[int, Tuple[float, float]] = {}
        for f in sorted(a.gt.glob(f"{d.name}__*.json")):
            doc = json.loads(f.read_text())
            if doc.get("annotator") == "smoketest":
                continue
            for r in doc.get("labels", []):
                if r.get("visible") and r.get("pano_x") is not None:
                    gt[int(r["frame_idx"])] = pano_to_roi(
                        float(r["pano_x"]), float(r["pano_y"]), prep.roi, prep.pano_w)
        frames = sorted(gt)
        step = float(getattr(prep.meta, "step", 1) or 1)
        for i, fi in enumerate(frames):
            hist = [(f, gt[f][0], gt[f][1]) for f in frames[max(0, i - 5): i]]
            gx, gy = gt[fi]
            for bucket, (px, py) in (
                (good, (gx, gy)),
                (bad, (gx + float(rng.uniform(40, 150)) * float(rng.choice([-1, 1])),
                       gy + float(rng.uniform(-40, 40)))),
            ):
                ev = judge(prep, fi, px, py, history=hist, step=step)
                for k in ("dog", "rgb", "kine"):
                    v = getattr(ev, k)
                    if v is not None:
                        bucket[k].append(float(v))
                bucket["score"].append(ev.score)
        print(f"{d.name}: {len(frames)} 帧")

    print(f"\n{'项':<8s} {'GT 点 med':>10s} {'GT p10':>8s} {'错点 med':>10s} {'错点 p90':>9s} {'判别':>6s}")
    for k in ("dog", "rgb", "kine", "score"):
        gv, bv = np.array(good[k]), np.array(bad[k])
        if gv.size == 0 or bv.size == 0:
            print(f"{k:<8s} 样本不足")
            continue
        sep = float(np.median(gv) - np.median(bv))
        print(f"{k:<8s} {np.median(gv):>10.3f} {np.percentile(gv,10):>8.3f} "
              f"{np.median(bv):>10.3f} {np.percentile(bv,90):>9.3f} {sep:>+6.2f}")


if __name__ == "__main__":
    _stats_main()
