#!/usr/bin/env python3
"""把 VLM 逐帧的粗估计整理成一条能直接用的球轨迹。

工作流给出的是**逐帧独立**的估计，三种毛病叠在一起：有些帧答不出来（null）、
有些帧答错（锁到了球员的白袜子或场地线交点上）、每一帧都带 2–3 px 的量化噪声
（坐标取自模型选中的网格格心）。逐帧独立的错误必须靠**时间维**来收拾，这个模块
就是唯一持有整段时间信息的地方。

三个判断上的取舍，都是被真实数据逼出来的：

1. **剔离群点，靠"抽掉这一帧后还剩不剩矛盾"，不靠速度阈值。** 目标帧之间隔
   step=3 帧，快球的目标帧间位移能到 100–390 px，跟"模型看错了"造成的跳变在
   数量级上重合，单看位移分不开。这里改成看**两侧各自的单边外推**：对每个点分别
   用它左边和右边的邻居做局部多项式外推，取两个残差的**较小值**。孤立跳变对两侧
   都对不上（min 很大）；而"慢→快"的拐点虽然对不上一侧，却总能被另一侧的同向
   邻居预测出来（min≈0），连续快速运动的段内点更是两侧都对得上。这个 min 就是
   区分"孤立跳变"和"真实快球"的全部机关。
2. **空缺太长就不插。** 三次样条在长空洞里会鼓包，鼓出来的位置比"没有位置"更坏：
   下游审核页用它当裁切窗中心，鼓错了整个画面就找不到球。所以 >
   `max_interp_gap` 个目标帧的洞一律留 None，让审核页把它显式暴露给人。
3. **平滑只写 `smooth_x/smooth_y`，绝不回写 `x/y`。** 审核页的裁切窗跟平滑轨迹，
   标记点画在原始 `x/y` 上——两者分离时，标记点相对窗心的抖动就是肉眼可见的
   误差信号；一旦把平滑值回写进 x，这个信号就永远看不见了。

坐标一律是 **ROI 局部像素**，调用方（prep.py 的 `to_roi`）已经处理过全景 0° 接缝，
这里不做任何环绕或 clamp——插值溢出 ROI 边界一点点是正常的，怎么处理交给调用方。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from numpy.polynomial import polynomial as npoly

# 目标帧间隔上的最大位移（px）。默认 400 是按真实快球给的上限：球速中位数约
# 24 px/原始帧，step=3 时目标帧间位移常见 70–120 px、极快球到 390 px。
DEFAULT_MAX_SPEED_PX = 400.0
# 单边外推残差的绝对下限（px）。噪声只有 2–3 px，但局部二次外推会把噪声放大到
# ~3σ，再叠上真实的加速度残差；低于这个值一律不当离群点，宁可留错也不要过度剔除。
DEFAULT_MIN_OUTLIER_PX = 60.0
DEFAULT_SMOOTH_WINDOW = 5
DEFAULT_MAX_INTERP_GAP = 8
# 单边拟合取几个邻居。4 个点配二次拟合是噪声放大与曲率跟随之间的折中。
DEFAULT_FIT_SPAN = 4
# 一次 process 里最多剔掉这么大比例的观测。真剔到这个量级说明模型整体跑飞了，
# 应该让审核页看到原始的乱，而不是看到一条被样条编出来的假轨迹。
MAX_OUTLIER_FRAC = 0.25


@dataclass
class Obs:
    """工作流对单帧的一次观测。"""

    frame_idx: int
    x: Optional[float]  # ROI 局部像素坐标，None 表示这一帧模型没给出结果
    y: Optional[float]
    conf: float = 0.0  # 0..1
    visible: bool = True
    occluded: bool = False
    blurred: bool = False
    stages: List[str] = field(default_factory=list)  # 走过的工作流阶段名，仅透传


@dataclass
class TrackPoint:
    """轨迹后处理的输出，逐帧一条。"""

    frame_idx: int
    x: Optional[float]  # 最终采纳的原始（未平滑）坐标；被剔除且插不出来时为 None
    y: Optional[float]
    smooth_x: Optional[float]  # 平滑后的坐标，审核页用它当裁切窗中心
    smooth_y: Optional[float]
    conf: float
    interp: bool  # 这一帧的坐标是插值来的（模型没答 或 答案被判离群）
    outlier: bool  # 原始观测被判为离群点而剔除
    visible: bool
    occluded: bool
    blurred: bool
    stages: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TrackStats:
    """一段轨迹的质量摘要。审核页拿它排优先级，workflow.py 拿它决定要不要重跑。"""

    n_total: int
    n_observed: int  # 模型给了可用坐标的帧数（visible=False 与 NaN 不计）
    n_outlier: int
    n_interp: int
    n_missing: int  # 最终仍然没有坐标的帧（长空洞 + 首尾外侧无法插值的）
    mean_conf: float  # 只统计最终有坐标的帧
    # 最长的连续"没有可用观测"段（按目标帧计，含 null / 不可见 / 被剔的帧）。
    # 用的是插值**之前**的口径：它衡量的是模型有多长时间完全瞎了，比 n_missing
    # 更适合当"这个 packet 要不要换提示词重跑"的判据。
    max_gap: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- 小工具


def _has_xy(o: Obs) -> bool:
    """这一帧算不算"模型给出了位置"。

    `visible=False` 的观测即使带着坐标也不算：模型明确说了球不可见，那点坐标
    是它上一次的猜测残留，拿去拟合会污染邻居。它们等同于缺失帧，但输出时保持
    `visible=False`。

    NaN/inf 也一律当缺失。这不是防御性洁癖：JSON 解析和"模型答了个空字符串再被
    float() 掉"都能产出 nan，而一个 nan 会顺着拟合和样条把整段轨迹染成 nan。
    """
    if o.x is None or o.y is None or not o.visible:
        return False
    try:
        return bool(np.isfinite(float(o.x)) and np.isfinite(float(o.y)))
    except (TypeError, ValueError):
        return False


def _infer_step(frames: np.ndarray) -> float:
    """目标帧之间的名义间隔。所有"每步位移"都按它归一化，因此本模块对
    step=1（逐帧标注）还是 step=3（当前 packet 约定）都成立。"""
    if frames.size < 2:
        return 1.0
    d = np.diff(frames)
    d = d[d > 0]
    if d.size == 0:
        return 1.0
    return float(np.median(d))


def _speed_limit(max_speed_px: float, max_speed_px_per_sec: Optional[float], fps: float) -> float:
    """把速度上限统一换算成"每个目标帧间隔的位移上限"。

    `max_speed_px_per_sec` 给了就以它为准（fps 是目标帧率，不是原始视频帧率），
    这样调用方可以按物理速度配参数而不用关心 step。
    """
    if max_speed_px_per_sec is not None and fps > 0:
        return float(max_speed_px_per_sec) / float(fps)
    return float(max_speed_px)


def _wls_predict(t: np.ndarray, v: np.ndarray, deg: int, w: Optional[np.ndarray] = None) -> float:
    """加权最小二乘多项式拟合后在 t=0 处取值。

    调用方把横坐标平移到"待预测点 = 0"，于是常数项就是预测值，不用再算一次多项式。
    """
    deg = int(max(0, min(deg, len(t) - 1)))
    if w is None:
        c = npoly.polyfit(t, v, deg)
    else:
        c = npoly.polyfit(t, v, deg, w=np.sqrt(np.maximum(w, 1e-9)))
    return float(np.atleast_1d(c)[0])


def _side_resid(
    frames: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    side: Sequence[int],
    i: int,
    step: float,
) -> Optional[float]:
    """用 `side` 上的点做局部多项式外推到第 i 帧，返回残差距离（px）。"""
    n = len(side)
    if n < 2:
        return None
    sl = list(side)
    t = (frames[sl] - frames[i]) / step
    deg = 2 if n >= 4 else 1
    px = _wls_predict(t, xs[sl], deg)
    py = _wls_predict(t, ys[sl], deg)
    return float(np.hypot(px - xs[i], py - ys[i]))


def _best_side_resid(
    frames: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    ordered: Sequence[int],
    i: int,
    step: float,
) -> Optional[float]:
    """同一侧换几种窗长各拟合一次，取最小残差（`ordered` 按离 i 由近到远）。

    只用一个固定窗长会翻车：一次长传只持续 5 个目标帧，4 点窗必然跨过"慢→快"或
    "快→慢"的拐点，拟合出来的抛物线纯属胡说，段内的点两侧都对不上，真实快球就被
    当成离群点剔了（实测踩过）。改成"任一侧、任一窗长能解释得通就算通过"：孤立跳变
    在所有窗长下都对不上（每个窗都不含它自己，预测的都是真实位置），而快球段内的点
    总有一个短窗恰好落在同一段运动里。**宁可漏剔，不可错剔。**
    """
    best: Optional[float] = None
    for k in range(2, len(ordered) + 1):
        r = _side_resid(frames, xs, ys, ordered[:k], i, step)
        if r is None:
            continue
        best = r if best is None else min(best, r)
    return best


def _robust_scale(vals: Sequence[float]) -> float:
    """残差幅值的稳健尺度。用中位数而不是均值：离群点自己也在这堆残差里。"""
    a = np.asarray([v for v in vals if v is not None], dtype=float)
    if a.size == 0:
        return 0.0
    return float(np.median(a))


def _natural_cubic(t: np.ndarray, v: np.ndarray, tq: np.ndarray) -> np.ndarray:
    """自然三次样条求值（自己解三对角方程，因为环境里没有 scipy）。

    自然边界（两端二阶导为 0）而不是 not-a-knot：球在窗口边缘常常正好处于空缺段
    附近，not-a-knot 会让端点段的三次项自由发挥、鼓出几百像素。
    """
    n = t.size
    if n < 3:
        return np.interp(tq, t, v)
    h = np.diff(t)
    if np.any(h <= 0):
        return np.interp(tq, t, v)
    m = np.zeros(n, dtype=float)  # 二阶导
    k = n - 2
    A = np.zeros((k, k), dtype=float)
    rhs = np.zeros(k, dtype=float)
    for i in range(1, n - 1):
        r = i - 1
        A[r, r] = 2.0 * (h[i - 1] + h[i])
        if r > 0:
            A[r, r - 1] = h[i - 1]
        if r < k - 1:
            A[r, r + 1] = h[i]
        rhs[r] = 6.0 * ((v[i + 1] - v[i]) / h[i] - (v[i] - v[i - 1]) / h[i - 1])
    try:
        m[1:-1] = np.linalg.solve(A, rhs)
    except np.linalg.LinAlgError:
        return np.interp(tq, t, v)
    j = np.clip(np.searchsorted(t, tq) - 1, 0, n - 2)
    hj = h[j]
    a = (t[j + 1] - tq) / hj
    b = (tq - t[j]) / hj
    out = a * v[j] + b * v[j + 1] + ((a**3 - a) * m[j] + (b**3 - b) * m[j + 1]) * (hj**2) / 6.0
    return np.where(np.isfinite(out), out, np.interp(tq, t, v))


# ---------------------------------------------------------------- 离群检测


def detect_outliers(
    obs: Sequence[Obs],
    *,
    fps: float = 10.0,
    max_speed_px: float = DEFAULT_MAX_SPEED_PX,
    max_speed_px_per_sec: Optional[float] = None,
    outlier_sigma: float = 3.0,
    min_outlier_px: float = DEFAULT_MIN_OUTLIER_PX,
    fit_span: int = DEFAULT_FIT_SPAN,
    passes: int = 2,
    conf_ref: float = 0.5,
    conf_relax: float = 0.6,
    max_outlier_frac: float = MAX_OUTLIER_FRAC,
) -> List[bool]:
    """逐帧判断观测是不是离群点，返回与 `obs` 等长的布尔列表。

    `obs` 需按 frame_idx 升序（`process` 会先排好）。没有坐标或 `visible=False`
    的帧恒为 False——它们是缺失帧，不是错答案。

    三路证据：

    * **单边外推残差**：左右两侧、每侧换 2…`fit_span` 种窗长各外推一次到本帧，取所有
      结果里的最小残差。孤立跳变对任何一侧任何窗长都对不上；运动拐点、连续快球总有
      一个窗恰好落在同一段运动里。**这是主判据**，详见 `_best_side_resid`。
    * **速度门**：与前后邻居的每步位移都超过上限，而"抽掉本帧后"前后邻居之间的
      每步位移仍在上限内——只有孤立跳变会这样，连续快球抽掉一帧后位移只会更大。
    * **置信度**：低 conf 的点门限按比例放低（`conf_relax`…1 倍），模型自己都没把握时
      更容易被剔掉。

    只有一侧有邻居（序列头尾）时门限加倍：那里本来就分不清"跳变"和"运动刚开始"，
    宁可漏剔。
    """
    n = len(obs)
    flags = [False] * n
    if n == 0:
        return flags

    ok = [_has_xy(o) for o in obs]
    frames = np.asarray([float(o.frame_idx) for o in obs], dtype=float)
    xs = np.asarray([float(o.x) if ok[i] else np.nan for i, o in enumerate(obs)], dtype=float)
    ys = np.asarray([float(o.y) if ok[i] else np.nan for i, o in enumerate(obs)], dtype=float)
    confs = np.asarray([float(o.conf or 0.0) for o in obs], dtype=float)
    step = _infer_step(frames)
    vmax = _speed_limit(max_speed_px, max_speed_px_per_sec, fps)
    n_ok = sum(ok)
    budget = max(1, int(np.floor(max_outlier_frac * max(1, n_ok))))

    for _ in range(max(1, int(passes))):
        idx = [i for i in range(n) if ok[i] and not flags[i]]
        # 点太少时任何"局部拟合"都是在拟合噪声，一个都不剔
        if len(idx) < 5:
            break

        resid: List[Tuple[int, float, float]] = []  # (帧下标, min 残差, 门限倍数)
        for pos, i in enumerate(idx):
            left = list(reversed(idx[max(0, pos - fit_span) : pos]))  # 由近到远
            right = idx[pos + 1 : pos + 1 + fit_span]
            rl = _best_side_resid(frames, xs, ys, left, i, step)
            rr = _best_side_resid(frames, xs, ys, right, i, step)
            if rl is not None and rr is not None:
                resid.append((i, min(rl, rr), 1.0))
            elif rl is not None or rr is not None:
                resid.append((i, float(rl if rl is not None else rr), 2.0))
        if not resid:
            break

        two_sided = [r for _, r, b in resid if b == 1.0]
        scale = _robust_scale(two_sided if two_sided else [r for _, r, _ in resid])
        thr_base = max(float(min_outlier_px), float(outlier_sigma) * scale)

        scored: List[Tuple[float, int]] = []
        for i, r, boundary in resid:
            c = float(np.clip(confs[i] / max(conf_ref, 1e-6), 0.0, 1.0))
            thr = thr_base * boundary * (conf_relax + (1.0 - conf_relax) * c)
            if r > thr:
                scored.append((r / max(thr, 1e-6), i))

        # 速度门：孤立跳变的补充证据（残差尺度被整体噪声抬高时仍能抓住）
        for pos, i in enumerate(idx):
            if pos == 0 or pos == len(idx) - 1:
                continue
            a, b = idx[pos - 1], idx[pos + 1]
            s_in = float(np.hypot(xs[i] - xs[a], ys[i] - ys[a])) / max((frames[i] - frames[a]) / step, 1e-6)
            s_out = float(np.hypot(xs[b] - xs[i], ys[b] - ys[i])) / max((frames[b] - frames[i]) / step, 1e-6)
            bypass = float(np.hypot(xs[b] - xs[a], ys[b] - ys[a])) / max((frames[b] - frames[a]) / step, 1e-6)
            if s_in > vmax and s_out > vmax and bypass <= vmax:
                ratio = min(s_in, s_out) / max(vmax, 1e-6)
                if not any(j == i for _, j in scored):
                    scored.append((ratio, i))

        if not scored:
            break
        # 超预算时只剔最离谱的几个：宁可留下一堆乱点让人看见，也不要编一条假轨迹
        scored.sort(key=lambda p: -p[0])
        room = budget - sum(1 for f in flags if f)
        if room <= 0:
            break
        for _, i in scored[:room]:
            flags[i] = True

    return flags


# ---------------------------------------------------------------- 插值


def interpolate(
    frames: Sequence[int],
    xs: Sequence[Optional[float]],
    ys: Sequence[Optional[float]],
    *,
    fps: float = 10.0,
    max_interp_gap: int = DEFAULT_MAX_INTERP_GAP,
    max_speed_px: float = DEFAULT_MAX_SPEED_PX,
    max_speed_px_per_sec: Optional[float] = None,
    velocity_guard: float = 2.0,
    spline_anchors: int = 4,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[bool]]:
    """填补空缺帧，返回 `(xs_out, ys_out, filled)`。

    * 只填**两侧都有已定帧**的空缺段；首尾外侧不外推（外推出来的位置没有任何约束，
      拿去当裁切窗中心比留空更危险）。
    * 段长 > `max_interp_gap` 个目标帧的洞保持 None。
    * 每段单独取两侧最多 `spline_anchors` 个锚点做自然三次样条；锚点不足 4 个退化为
      线性。**用局部锚点而不是全局样条**：全局样条会让一个长洞的两端牵动整条曲线的
      二阶导，把好段也带歪。
    * 填完做速度约束：段内每步位移超过 `velocity_guard × 速度上限`，说明两端锚点本身
      就不可信（样条在鼓包），退回线性插值。
    """
    n = len(frames)
    xo: List[Optional[float]] = [None if x is None else float(x) for x in xs]
    yo: List[Optional[float]] = [None if y is None else float(y) for y in ys]
    filled = [False] * n
    if n == 0:
        return xo, yo, filled

    f = np.asarray([float(v) for v in frames], dtype=float)
    step = _infer_step(f)
    vmax = _speed_limit(max_speed_px, max_speed_px_per_sec, fps)
    anchors = [i for i in range(n) if xo[i] is not None and yo[i] is not None]
    if len(anchors) < 2:
        return xo, yo, filled

    ax = np.asarray([xo[i] for i in anchors], dtype=float)
    ay = np.asarray([yo[i] for i in anchors], dtype=float)
    af = f[anchors]

    for k in range(len(anchors) - 1):
        lo, hi = anchors[k], anchors[k + 1]
        gap = list(range(lo + 1, hi))
        if not gap or len(gap) > int(max_interp_gap):
            continue
        s = max(0, k - spline_anchors + 1)
        e = min(len(anchors), k + 1 + spline_anchors)
        tq = f[gap]
        if e - s >= 4:
            gx = _natural_cubic(af[s:e], ax[s:e], tq)
            gy = _natural_cubic(af[s:e], ay[s:e], tq)
        else:
            gx = np.interp(tq, af[s:e], ax[s:e])
            gy = np.interp(tq, af[s:e], ay[s:e])

        # 速度约束：把两端锚点也串进来算每步位移
        seq_f = np.concatenate(([f[lo]], tq, [f[hi]]))
        seq_x = np.concatenate(([ax[k]], gx, [ax[k + 1]]))
        seq_y = np.concatenate(([ay[k]], gy, [ay[k + 1]]))
        d = np.hypot(np.diff(seq_x), np.diff(seq_y)) / np.maximum(np.diff(seq_f) / step, 1e-6)
        chord = float(np.hypot(ax[k + 1] - ax[k], ay[k + 1] - ay[k])) / max((f[hi] - f[lo]) / step, 1e-6)
        if not np.all(np.isfinite(d)) or float(np.max(d)) > velocity_guard * max(vmax, chord):
            gx = np.interp(tq, af[k : k + 2], ax[k : k + 2])
            gy = np.interp(tq, af[k : k + 2], ay[k : k + 2])

        for j, i in enumerate(gap):
            xo[i] = float(gx[j])
            yo[i] = float(gy[j])
            filled[i] = True

    return xo, yo, filled


# ---------------------------------------------------------------- 平滑


def smooth(
    frames: Sequence[int],
    xs: Sequence[Optional[float]],
    ys: Sequence[Optional[float]],
    *,
    window: int = DEFAULT_SMOOTH_WINDOW,
    degree: int = 2,
    weights: Optional[Sequence[float]] = None,
) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """Savitzky–Golay 风格的局部加权多项式平滑，返回 `(sx, sy)`。

    * **保端点**：窗口在段边界处收缩到可用范围。二阶多项式对直线和抛物线是无偏的，
      所以收缩窗口不会把首尾点往段内拉——首尾点正是审核页最容易被人盯上的地方。
    * 按**连续已定段**分段处理：跨过 None 空洞平滑等于把洞两侧的位置搅在一起，
      而那两侧可能隔着好几秒。
    * `weights`（一般传 conf）让模型没把握的点少说话；全零权重时退化为等权。
    """
    n = len(frames)
    sx: List[Optional[float]] = [None] * n
    sy: List[Optional[float]] = [None] * n
    if n == 0:
        return sx, sy

    f = np.asarray([float(v) for v in frames], dtype=float)
    step = _infer_step(f)
    w = (
        np.clip(np.asarray([float(v) for v in weights], dtype=float), 0.0, None)
        if weights is not None
        else np.ones(n, dtype=float)
    )
    half = max(0, int(window) // 2)

    segs: List[List[int]] = []
    cur: List[int] = []
    for i in range(n):
        if xs[i] is None or ys[i] is None:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(i)
    if cur:
        segs.append(cur)

    for seg in segs:
        m = len(seg)
        vx = np.asarray([float(xs[i]) for i in seg], dtype=float)  # type: ignore[arg-type]
        vy = np.asarray([float(ys[i]) for i in seg], dtype=float)  # type: ignore[arg-type]
        vf = f[seg]
        vw = w[seg]
        if float(np.max(vw)) <= 0.0:
            vw = np.ones(m, dtype=float)
        # 权重下限保证低 conf 的点不会被完全踢出窗口（否则窗口会悄悄变短）
        vw = np.maximum(vw, 0.15)
        for p in range(m):
            a, b = max(0, p - half), min(m, p + half + 1)
            t = (vf[a:b] - vf[p]) / step
            deg = min(int(degree), (b - a) - 1)
            if deg <= 0:
                sx[seg[p]], sy[seg[p]] = float(vx[p]), float(vy[p])
                continue
            sx[seg[p]] = _wls_predict(t, vx[a:b], deg, vw[a:b])
            sy[seg[p]] = _wls_predict(t, vy[a:b], deg, vw[a:b])

    return sx, sy


# ---------------------------------------------------------------- 外推（工作流抄近路用）


def predict_next(
    points: Sequence[Any],
    target_frame: int,
    *,
    span: int = 4,
    min_points: int = 1,
    step: Optional[float] = None,
    base_unc_px: float = 6.0,
    unknown_speed_px: float = 300.0,
    max_unc_px: float = 1000.0,
) -> Optional[Tuple[float, float, float]]:
    """线性外推到 `target_frame`，返回 `(x, y, 半径不确定度 px)`。

    `points` 是已定帧，按 frame_idx 升序，元素可以是 `TrackPoint`、`Obs`，也可以是
    `(frame_idx, x, y)` 元组；坐标为 None 的元素自动跳过。

    工作流的"外推抄近路"阶段拿这个当先验：先在 `(x, y)` 附近半径 `unc` 的小窗里
    找球，找到就省掉一整轮粗查。所以 **`unc` 宁大勿小**——小了会把球框在窗外，
    抄近路直接变成抄错路。不确定度随外推距离增长，含三项：拟合残差、速度乘以
    一个"加速度未知"比例、以及一个二次项（隔得越远，未知加速度积得越多）；上限
    `max_unc_px` 是 ROI 尺度，到了这个数就等于"没有先验，老实全 ROI 搜"。

    `step` 是目标帧间隔（packet 的 `meta.step`）。不传就从 `points` 的帧号里猜；
    只有 1 个点时猜不出来，此时不传 `step` 会得到偏大的半径（保守，但没用）。

    只有 1 个已定点时返回该点位置加一个很大的半径（按 `unknown_speed_px` 算）；
    一个点都没有返回 None。想要"少于 2 点就放弃"的旧语义，传 `min_points=2`。
    """
    pts: List[Tuple[float, float, float]] = []
    for p in points or []:
        if isinstance(p, (tuple, list)):
            if len(p) < 3:
                continue
            fi, x, y = p[0], p[1], p[2]
        else:
            fi, x, y = getattr(p, "frame_idx", None), getattr(p, "x", None), getattr(p, "y", None)
        if fi is None or x is None or y is None:
            continue
        pts.append((float(fi), float(x), float(y)))
    pts.sort(key=lambda q: q[0])
    if len(pts) < max(1, int(min_points)):
        return None
    if not pts:
        return None

    f = np.asarray([q[0] for q in pts], dtype=float)
    x = np.asarray([q[1] for q in pts], dtype=float)
    y = np.asarray([q[2] for q in pts], dtype=float)
    st = float(step) if step and step > 0 else _infer_step(f)
    ahead = max(0.0, (float(target_frame) - f[-1]) / st)

    if len(pts) == 1:
        unc = base_unc_px + unknown_speed_px * max(ahead, 1.0)
        return float(x[0]), float(y[0]), float(min(unc, max_unc_px))

    use = slice(max(0, len(pts) - max(2, int(span))), len(pts))
    t = (f[use] - float(target_frame)) / st
    px = _wls_predict(t, x[use], 1)
    py = _wls_predict(t, y[use], 1)

    # 残差用同一个一次拟合在各已用点上的偏差；两点时恒为 0，靠 base 兜底
    tb = (f[use] - f[use][-1]) / st
    cx = npoly.polyfit(tb, x[use], 1)
    cy = npoly.polyfit(tb, y[use], 1)
    rx = x[use] - npoly.polyval(tb, cx)
    ry = y[use] - npoly.polyval(tb, cy)
    resid = float(np.sqrt(np.mean(rx**2 + ry**2)))
    speed = float(np.hypot(cx[1], cy[1]))  # 每目标帧位移

    unc = base_unc_px + resid + (0.30 * speed + 1.5 * resid) * ahead + 0.05 * speed * ahead**2
    return float(px), float(py), float(min(unc, max_unc_px))


# ---------------------------------------------------------------- 主流程


def process(
    obs: Sequence[Obs],
    *,
    fps: float = 10.0,
    max_speed_px: float = DEFAULT_MAX_SPEED_PX,
    outlier_sigma: float = 3.0,
    smooth_window: int = DEFAULT_SMOOTH_WINDOW,
    max_interp_gap: int = DEFAULT_MAX_INTERP_GAP,
    max_speed_px_per_sec: Optional[float] = None,
    min_outlier_px: float = DEFAULT_MIN_OUTLIER_PX,
    fit_span: int = DEFAULT_FIT_SPAN,
    outlier_passes: int = 2,
    max_outlier_frac: float = MAX_OUTLIER_FRAC,
    smooth_degree: int = 2,
    velocity_guard: float = 2.0,
    spline_anchors: int = 4,
    interp_conf_decay: float = 0.7,
    detect: bool = True,
) -> Tuple[List[TrackPoint], TrackStats]:
    """剔离群 → 插值 → 平滑，一次跑完整段轨迹。

    输入不必有序、可以有重复帧号（同一帧多次观测时保留 conf 最高的那次）。
    空列表、全 None、只有一两个观测都不会抛异常，只是能做的事变少。

    `x/y` 是最终采纳的**原始**坐标（离群或缺失帧上是插值值），`smooth_x/smooth_y`
    是平滑值。审核页用后者当裁切窗中心、用前者画标记。
    """
    rows = _dedup_sorted(obs)
    n = len(rows)
    if n == 0:
        return [], TrackStats(0, 0, 0, 0, 0, 0.0, 0)

    frames = [int(o.frame_idx) for o in rows]
    ok = [_has_xy(o) for o in rows]
    n_observed = sum(ok)

    outlier = (
        detect_outliers(
            rows,
            fps=fps,
            max_speed_px=max_speed_px,
            max_speed_px_per_sec=max_speed_px_per_sec,
            outlier_sigma=outlier_sigma,
            min_outlier_px=min_outlier_px,
            fit_span=fit_span,
            passes=outlier_passes,
            max_outlier_frac=max_outlier_frac,
        )
        if detect
        else [False] * n
    )

    accepted = [ok[i] and not outlier[i] for i in range(n)]
    raw_x: List[Optional[float]] = [float(rows[i].x) if accepted[i] else None for i in range(n)]  # type: ignore[arg-type]
    raw_y: List[Optional[float]] = [float(rows[i].y) if accepted[i] else None for i in range(n)]  # type: ignore[arg-type]

    fx, fy, filled = interpolate(
        frames,
        raw_x,
        raw_y,
        fps=fps,
        max_interp_gap=max_interp_gap,
        max_speed_px=max_speed_px,
        max_speed_px_per_sec=max_speed_px_per_sec,
        velocity_guard=velocity_guard,
        spline_anchors=spline_anchors,
    )

    conf = [float(rows[i].conf or 0.0) if accepted[i] else 0.0 for i in range(n)]
    anchors = [i for i in range(n) if accepted[i]]
    for i in range(n):
        if not filled[i]:
            continue
        conf[i] = _interp_conf(i, anchors, conf, interp_conf_decay)

    sx, sy = smooth(frames, fx, fy, window=smooth_window, degree=smooth_degree, weights=conf)

    points: List[TrackPoint] = []
    for i, o in enumerate(rows):
        points.append(
            TrackPoint(
                frame_idx=frames[i],
                x=fx[i],
                y=fy[i],
                smooth_x=sx[i],
                smooth_y=sy[i],
                conf=conf[i],
                interp=bool(filled[i]),
                outlier=bool(outlier[i]),
                visible=bool(o.visible),
                occluded=bool(o.occluded),
                blurred=bool(o.blurred),
                stages=list(o.stages or []),
            )
        )

    have = [p.conf for p in points if p.x is not None]
    stats = TrackStats(
        n_total=n,
        n_observed=n_observed,
        n_outlier=sum(1 for f in outlier if f),
        n_interp=sum(1 for f in filled if f),
        n_missing=sum(1 for p in points if p.x is None),
        mean_conf=float(np.mean(have)) if have else 0.0,
        max_gap=_max_run([not a for a in accepted]),
    )
    return points, stats


def _dedup_sorted(obs: Sequence[Obs]) -> List[Obs]:
    """按帧号升序，同帧多次观测保留 conf 最高的一次（工作流重试会产生重复）。"""
    best: Dict[int, Obs] = {}
    for o in obs or []:
        cur = best.get(int(o.frame_idx))
        if cur is None:
            best[int(o.frame_idx)] = o
            continue
        # 有坐标的优先于没坐标的，其次比 conf
        if (_has_xy(o), float(o.conf or 0.0)) > (_has_xy(cur), float(cur.conf or 0.0)):
            best[int(o.frame_idx)] = o
    return [best[k] for k in sorted(best)]


def _interp_conf(i: int, anchors: Sequence[int], conf: Sequence[float], decay: float) -> float:
    """插值帧的置信度：两端锚点 conf 的较小值，按到最近锚点的距离衰减。

    插值帧必须明显低于观测帧，审核页才能按 conf 排序把它们排到前面让人先看。
    """
    if not anchors:
        return 0.0
    lo = None
    hi = None
    for a in anchors:
        if a < i:
            lo = a
        elif a > i:
            hi = a
            break
    if lo is None and hi is None:
        return 0.0
    if lo is None:
        base, d = conf[hi], hi - i  # type: ignore[index]
    elif hi is None:
        base, d = conf[lo], i - lo
    else:
        base = min(conf[lo], conf[hi])
        d = min(i - lo, hi - i)
    return float(max(0.0, base * (decay ** max(1, int(d)))))


def _max_run(mask: Sequence[bool]) -> int:
    """最长连续 True 段长度。"""
    best = cur = 0
    for m in mask:
        cur = cur + 1 if m else 0
        best = max(best, cur)
    return best
