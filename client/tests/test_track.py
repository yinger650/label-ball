#!/usr/bin/env python3
"""client/track.py 的自测。

直接跑：`venv/bin/python3 client/tests/test_track.py`（也兼容 pytest 收集）。

用合成轨迹而不是真实 GT：这里要验的是**后处理算法本身**在已知真值下的行为
（插值误差、离群判定、端点保持），真实数据既没有真值也复现不了"孤立跳变"和
"连续快球"这两种必须区分开的输入。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from client.track import (  # noqa: E402
    Obs,
    TrackPoint,
    detect_outliers,
    interpolate,
    predict_next,
    process,
    smooth,
)

STEP = 3
F0 = 1000


def frames(n: int) -> List[int]:
    return [F0 + STEP * i for i in range(n)]


def parabola(n: int) -> Tuple[np.ndarray, np.ndarray]:
    """一次抛物线飞行：x 匀速 90 px/目标帧，y 是带重力的抛物线。"""
    i = np.arange(n, dtype=float)
    x = 200.0 + 90.0 * i
    y = 420.0 - 30.0 * i + 0.8 * i * i
    return x, y


def make_obs(
    fs: List[int],
    x: np.ndarray,
    y: np.ndarray,
    *,
    drop: Optional[List[int]] = None,
    conf: float = 0.8,
) -> List[Obs]:
    drop = drop or []
    out: List[Obs] = []
    for i, f in enumerate(fs):
        if i in drop:
            out.append(Obs(frame_idx=f, x=None, y=None, conf=0.0, stages=["l1"]))
        else:
            out.append(Obs(frame_idx=f, x=float(x[i]), y=float(y[i]), conf=conf, stages=["l1", "l2"]))
    return out


def report(name: str, extra: str = "") -> None:
    print(f"  [ok] {name}{(' — ' + extra) if extra else ''}")


# ---------------------------------------------------------------- 用例


def test_parabola_with_dropouts() -> None:
    print("test_parabola_with_dropouts")
    n = 60
    rng = np.random.default_rng(20260910)
    fs = frames(n)
    tx, ty = parabola(n)
    # 2 px 量化噪声（模型选中的网格格心）
    nx = tx + rng.uniform(-2.0, 2.0, n)
    ny = ty + rng.uniform(-2.0, 2.0, n)
    # 随机挖掉 15%，但保证首尾在，否则那些帧无法插值（这是模块的设计约定）
    k = int(round(0.15 * n))
    drop = sorted(rng.choice(np.arange(2, n - 2), size=k, replace=False).tolist())
    obs = make_obs(fs, nx, ny, drop=drop)

    pts, st = process(obs)
    assert len(pts) == n
    assert st.n_total == n
    assert st.n_observed == n - k, (st.n_observed, n - k)
    assert st.n_outlier == 0, f"干净轨迹不该剔任何点，实际剔了 {st.n_outlier}"
    assert st.n_interp == k, (st.n_interp, k)
    assert st.n_missing == 0
    assert st.max_gap == max(_runs(drop)), (st.max_gap, _runs(drop))

    err = [math.hypot(pts[i].x - tx[i], pts[i].y - ty[i]) for i in drop]
    assert max(err) < 5.0, f"插值最大误差 {max(err):.2f} px"
    # 插值帧的 conf 必须明显低于观测帧
    ic = [pts[i].conf for i in drop]
    oc = [pts[i].conf for i in range(n) if i not in drop]
    assert max(ic) < min(oc), (max(ic), min(oc))
    assert all(pts[i].interp for i in drop)
    assert not any(pts[i].interp for i in range(n) if i not in drop)
    # 平滑应当把 2 px 噪声压下去
    e_raw = float(np.mean([math.hypot(pts[i].x - tx[i], pts[i].y - ty[i]) for i in range(n)]))
    e_sm = float(np.mean([math.hypot(pts[i].smooth_x - tx[i], pts[i].smooth_y - ty[i]) for i in range(n)]))
    assert e_sm < e_raw, (e_sm, e_raw)
    report(
        "抛物线 + 2px 噪声 + 15% 缺失",
        f"n_interp={st.n_interp} 最大插值误差={max(err):.2f}px 平滑前后平均误差 {e_raw:.2f}→{e_sm:.2f}px "
        f"max_gap={st.max_gap} mean_conf={st.mean_conf:.3f}",
    )


def test_isolated_outlier() -> None:
    print("test_isolated_outlier")
    n = 40
    fs = frames(n)
    tx, ty = parabola(n)
    bad = 20
    nx, ny = tx.copy(), ty.copy()
    nx[bad] += 600.0  # 模型锁到了远处球员的白袜子上
    ny[bad] -= 250.0
    obs = make_obs(fs, nx, ny)

    pts, st = process(obs)
    assert st.n_outlier == 1, f"应当只剔 1 个点，实际 {st.n_outlier}: {[p.frame_idx for p in pts if p.outlier]}"
    assert pts[bad].outlier and pts[bad].interp
    d = math.hypot(pts[bad].x - tx[bad], pts[bad].y - ty[bad])
    assert d < 15.0, f"剔除后插值回来的位置偏差 {d:.2f} px"
    assert st.n_interp == 1 and st.n_missing == 0
    # 邻居不该被牵连
    assert not pts[bad - 1].outlier and not pts[bad + 1].outlier
    report("孤立大幅跳变", f"outlier@frame={pts[bad].frame_idx} 恢复偏差={d:.2f}px conf={pts[bad].conf:.3f}")

    # 纯 detect_outliers 接口也要给出同样结论
    fl = detect_outliers(obs)
    assert sum(fl) == 1 and fl[bad]
    report("detect_outliers 单独调用一致")


def test_sustained_fast_motion() -> None:
    print("test_sustained_fast_motion")
    # 前 12 帧慢速，中间连续 5 帧每帧 300 px（一脚长传），之后 10 帧慢速
    xs: List[float] = []
    ys: List[float] = []
    x, y = 300.0, 400.0
    for _ in range(12):
        xs.append(x)
        ys.append(y)
        x += 20.0
        y += 4.0
    for _ in range(5):
        x += 300.0
        y -= 60.0
        xs.append(x)
        ys.append(y)
    for _ in range(10):
        x += 18.0
        y += 6.0
        xs.append(x)
        ys.append(y)
    n = len(xs)
    rng = np.random.default_rng(7)
    ax = np.asarray(xs) + rng.uniform(-2, 2, n)
    ay = np.asarray(ys) + rng.uniform(-2, 2, n)
    obs = make_obs(frames(n), ax, ay)
    pts, st = process(obs)
    assert st.n_outlier == 0, f"真实快球被误剔: {[p.frame_idx for p in pts if p.outlier]}"
    assert st.n_interp == 0 and st.n_missing == 0
    report("连续 5 帧 300px/帧 的真实快球", f"n_outlier={st.n_outlier}")

    # 再来一遍：速度超过默认上限（500 px/帧），依然不能剔——抽掉任一帧位移只会更大
    xs2 = [300.0 + 500.0 * i for i in range(6)]
    seq = [300.0 - 20.0 * (6 - i) for i in range(6)] + xs2 + [xs2[-1] + 15.0 * (i + 1) for i in range(6)]
    n2 = len(seq)
    obs2 = make_obs(frames(n2), np.asarray(seq), np.full(n2, 400.0))
    fl2 = detect_outliers(obs2)
    assert sum(fl2) == 0, f"500px/帧 的连续运动被误剔: {fl2}"
    report("连续 6 帧 500px/帧（超默认速度上限）仍不误剔")

    # 对照组：把同样幅度的位移做成"跳出去又跳回来"，必须被剔
    base = [300.0 + 20.0 * i for i in range(20)]
    base[10] += 500.0
    obs3 = make_obs(frames(20), np.asarray(base), np.full(20, 400.0))
    fl3 = detect_outliers(obs3)
    assert fl3[10] and sum(fl3) == 1, f"对照组孤立跳变没被剔: {fl3}"
    report("对照组：同幅度孤立跳变被剔")


def test_spline_guard_falls_back_to_linear() -> None:
    print("test_spline_guard_falls_back_to_linear")
    fs = list(range(1000, 1030, 3))
    n = len(fs)
    xs: List[Optional[float]] = [None] * n
    ys: List[Optional[float]] = [None] * n
    # 4 个锚点、中间空洞 5 个目标帧。加速度先大后收，样条会鼓到 260+，线性约 155。
    xs[0], ys[0] = 0.0, 0.0
    xs[1], ys[1] = 10.0, 50.0
    xs[2], ys[2] = 20.0, 150.0
    xs[8], ys[8] = 80.0, 160.0
    _lx, lin_y, _ = interpolate(fs, xs, ys, spline_max_gap=5)
    _sx, spl_y, _ = interpolate(fs, xs, ys, spline_max_gap=99)
    mid = 5
    assert lin_y[mid] is not None and abs(lin_y[mid] - 155.0) < 5.0, lin_y[mid]
    assert spl_y[mid] is not None
    assert abs(spl_y[mid] - lin_y[mid]) > 40.0, (spl_y[mid], lin_y[mid])
    report("k>=5 的空洞用线性，样条会被挡住", f"lin={lin_y[mid]:.1f} spline={spl_y[mid]:.1f}")


def test_long_gap_stays_missing() -> None:
    print("test_long_gap_stays_missing")
    n = 50
    fs = frames(n)
    tx, ty = parabola(n)
    hole = list(range(18, 30))  # 12 个目标帧 > max_interp_gap=8
    short = [40, 41]
    obs = make_obs(fs, tx, ty, drop=hole + short)
    pts, st = process(obs)
    assert st.n_missing == len(hole), (st.n_missing, len(hole))
    assert all(pts[i].x is None and pts[i].y is None for i in hole)
    assert all(pts[i].smooth_x is None for i in hole)
    assert all(not pts[i].interp for i in hole)
    assert st.n_interp == len(short)
    assert st.max_gap == len(hole)
    # 洞两侧的平滑要分段：洞左边最后一点不能被右边的位置拽走
    assert pts[17].smooth_x is not None and pts[30].smooth_x is not None
    assert abs(pts[17].smooth_x - tx[17]) < 5.0
    report(
        "12 帧长空洞",
        f"n_missing={st.n_missing} n_interp={st.n_interp} max_gap={st.max_gap}",
    )

    # 段长正好等于 max_interp_gap 要能插上
    obs2 = make_obs(fs, tx, ty, drop=list(range(10, 18)))
    _, st2 = process(obs2)
    assert st2.n_interp == 8 and st2.n_missing == 0, (st2.n_interp, st2.n_missing)
    report("段长恰好 = max_interp_gap 能插上", f"n_interp={st2.n_interp}")


def test_smooth_keeps_endpoints() -> None:
    print("test_smooth_keeps_endpoints")
    n = 30
    fs = frames(n)
    x = np.asarray([100.0 + 37.0 * i for i in range(n)])
    y = np.asarray([200.0 - 11.0 * i for i in range(n)])
    sx, sy = smooth(fs, list(x), list(y))
    d0 = math.hypot(sx[0] - x[0], sy[0] - y[0])
    d1 = math.hypot(sx[-1] - x[-1], sy[-1] - y[-1])
    assert d0 < 1.0 and d1 < 1.0, (d0, d1)
    # 直线上任何点都不该被移动
    assert max(math.hypot(sx[i] - x[i], sy[i] - y[i]) for i in range(n)) < 1e-6
    report("直线平滑首尾偏移", f"首={d0:.2e}px 尾={d1:.2e}px")

    # 抛物线（二阶）同样应当无偏
    tx, ty = parabola(n)
    sx2, sy2 = smooth(fs, list(tx), list(ty))
    dmax = max(math.hypot(sx2[i] - tx[i], sy2[i] - ty[i]) for i in range(n))
    assert dmax < 1e-6, dmax
    report("抛物线平滑无偏", f"最大偏移={dmax:.2e}px")

    # 带噪声的直线：端点也不能被拉偏太多，且中段噪声要被压下去
    rng = np.random.default_rng(3)
    nx = x + rng.uniform(-2, 2, n)
    ny = y + rng.uniform(-2, 2, n)
    sx3, sy3 = smooth(fs, list(nx), list(ny))
    e0 = math.hypot(sx3[0] - x[0], sy3[0] - y[0])
    mid = float(np.mean([math.hypot(sx3[i] - x[i], sy3[i] - y[i]) for i in range(5, n - 5)]))
    raw_mid = float(np.mean([math.hypot(nx[i] - x[i], ny[i] - y[i]) for i in range(5, n - 5)]))
    assert e0 < 4.0, e0
    assert mid < raw_mid, (mid, raw_mid)
    report("带噪直线", f"端点偏差={e0:.2f}px 中段误差 {raw_mid:.2f}→{mid:.2f}px")

    # 平滑绝不回写 x/y
    obs = make_obs(fs, nx, ny)
    pts, _ = process(obs)
    assert all(abs(pts[i].x - nx[i]) < 1e-9 for i in range(n)), "process 改写了原始 x"
    assert any(abs(pts[i].smooth_x - nx[i]) > 1e-6 for i in range(n)), "smooth_x 跟 x 完全一样，没平滑"
    report("process 不回写原始 x/y")


def test_predict_next() -> None:
    print("test_predict_next")
    n = 8
    pts = [TrackPoint(F0 + STEP * i, 100.0 + 60.0 * i, 300.0 + 25.0 * i, None, None, 0.8, False, False, True, False, False, []) for i in range(n)]
    nxt = F0 + STEP * n
    got = predict_next(pts, nxt)
    assert got is not None
    px, py, unc = got
    err = math.hypot(px - (100.0 + 60.0 * n), py - (300.0 + 25.0 * n))
    assert err < 1.0, f"匀速直线外推误差 {err:.3f} px"
    u = [predict_next(pts, F0 + STEP * (n + k))[2] for k in range(4)]  # type: ignore[index]
    assert all(u[i] < u[i + 1] for i in range(len(u) - 1)), u
    report("匀速直线外推", f"误差={err:.3f}px 不确定度 1/2/3/4 步={['%.1f' % v for v in u]}")

    # 元组输入、Obs 输入都要能吃
    tup = [(p.frame_idx, p.x, p.y) for p in pts]
    g2 = predict_next(tup, nxt)
    assert g2 is not None and abs(g2[0] - px) < 1e-9
    g3 = predict_next([Obs(p.frame_idx, p.x, p.y, 0.5) for p in pts], nxt)
    assert g3 is not None and abs(g3[0] - px) < 1e-9
    report("接受 TrackPoint / 元组 / Obs 三种输入")

    # 退化：0 个点 → None；1 个点 → 位置 + 大半径；2 个点 → 精确线性
    assert predict_next([], nxt) is None
    one = predict_next(pts[:1], nxt, step=STEP)
    assert one is not None and 100.0 < one[2] <= 1000.0, one
    assert one[0] == pts[0].x and one[1] == pts[0].y
    assert predict_next(pts[:1], nxt, min_points=2) is None
    two = predict_next(pts[:2], F0 + STEP * 2)
    assert two is not None and math.hypot(two[0] - (100.0 + 120.0), two[1] - (300.0 + 50.0)) < 1e-6
    # 坐标为 None 的点被跳过
    assert predict_next([TrackPoint(F0, None, None, None, None, 0.0, False, False, True, False, False, [])], nxt) is None
    report("predict_next 退化输入", f"单点不确定度={one[2]:.0f}px")


def test_degenerate_inputs() -> None:
    print("test_degenerate_inputs")
    pts, st = process([])
    assert pts == [] and st.n_total == 0 and st.mean_conf == 0.0

    allnull = [Obs(F0 + STEP * i, None, None) for i in range(10)]
    pts, st = process(allnull)
    assert len(pts) == 10 and st.n_observed == 0 and st.n_missing == 10 and st.n_interp == 0
    assert st.max_gap == 10 and all(p.x is None and p.smooth_x is None for p in pts)

    one = [Obs(F0, 10.0, 20.0, 0.9)]
    pts, st = process(one)
    assert len(pts) == 1 and st.n_observed == 1 and st.n_missing == 0
    assert pts[0].smooth_x == 10.0 and not pts[0].interp

    two = [Obs(F0, 10.0, 20.0, 0.9), Obs(F0 + STEP, 40.0, 30.0, 0.9)]
    pts, st = process(two)
    assert len(pts) == 2 and st.n_outlier == 0 and st.n_missing == 0
    # 两个点只能做一次线性拟合，等于原样返回
    assert abs(pts[0].smooth_x - 10.0) < 1e-6 and abs(pts[1].smooth_x - 40.0) < 1e-6

    # 中间一个观测、两侧全空：两侧外侧不外推
    mid = [Obs(F0 + STEP * i, None, None) for i in range(5)]
    mid[2] = Obs(F0 + STEP * 2, 50.0, 60.0, 0.7)
    pts, st = process(mid)
    assert st.n_missing == 4 and st.n_interp == 0 and st.n_observed == 1

    # 全部 visible=False（模型说球不可见）：不当离群，坐标可以插，但 visible 保持 False
    n = 12
    tx, ty = parabola(n)
    invis = [Obs(F0 + STEP * i, float(tx[i]), float(ty[i]), 0.4, visible=False) for i in range(n)]
    pts, st = process(invis)
    assert st.n_observed == 0 and st.n_outlier == 0 and st.n_missing == n
    assert all(not p.visible for p in pts)

    # 只有中间几帧不可见：应当被插值填上，visible 仍是 False
    obs = make_obs(frames(n), tx, ty)
    for i in (5, 6):
        obs[i] = Obs(obs[i].frame_idx, float(tx[i]), float(ty[i]), 0.2, visible=False, occluded=True)
    pts, st = process(obs)
    assert st.n_interp == 2 and st.n_outlier == 0
    assert all(pts[i].interp and not pts[i].visible and pts[i].occluded for i in (5, 6))
    assert all(abs(pts[i].x - tx[i]) < 3.0 for i in (5, 6))

    # 乱序 + 重复帧号
    shuffled = [obs[i] for i in (3, 0, 2, 1)] + [Obs(obs[1].frame_idx, 1.0, 2.0, 0.05)]
    pts, st = process(shuffled)
    assert [p.frame_idx for p in pts] == sorted({o.frame_idx for o in shuffled})
    assert abs(pts[1].x - tx[1]) < 3.0, "重复帧应保留 conf 高的那条"

    # NaN 不能进来搅局（工作流偶发 float('nan')）：当缺失帧处理，不许染色整段
    tx10, ty10 = parabola(10)
    nanobs = make_obs(frames(10), tx10, ty10)
    nanobs[4] = Obs(nanobs[4].frame_idx, float("nan"), float("nan"), 0.5)
    nanobs[6] = Obs(nanobs[6].frame_idx, float("inf"), 0.0, 0.5)
    pts, st = process(nanobs)
    assert len(pts) == 10 and st.n_observed == 8 and st.n_interp == 2
    assert all(p.x is not None and math.isfinite(p.x) and math.isfinite(p.smooth_x) for p in pts)
    assert abs(pts[4].x - tx10[4]) < 3.0 and abs(pts[6].x - tx10[6]) < 3.0

    # detect=False 时一个都不剔（调用方想自己把关时用）
    spiky = make_obs(frames(20), *parabola(20))
    spiky[9] = Obs(spiky[9].frame_idx, 9999.0, 9999.0, 0.9)
    _, st_on = process(spiky)
    _, st_off = process(spiky, detect=False)
    assert st_on.n_outlier == 1 and st_off.n_outlier == 0

    # interpolate / smooth 空输入
    assert interpolate([], [], []) == ([], [], [])
    assert smooth([], [], []) == ([], [])
    assert detect_outliers([]) == []
    # 极端参数
    pts, st = process(make_obs(frames(20), *parabola(20)), smooth_window=1, max_interp_gap=0, outlier_sigma=0.5)
    assert len(pts) == 20
    report("退化输入全部不抛异常")


def test_fuzz_many_seeds() -> None:
    """换 30 组噪声/缺失/离群位置，确认结论不是靠某一个种子蒙对的。

    干净数据上误剔一个点，代价是标注员在审核页多看一屏；漏剔一个点，代价是审核页
    上有个明显跳出去的标记——后者远好于前者，所以这里对"零误剔"卡得比"必然剔中"严。
    """
    print("test_fuzz_many_seeds")
    worst_interp = 0.0
    false_pos = 0
    caught = 0
    for seed in range(30):
        rng = np.random.default_rng(seed)
        n = int(rng.integers(30, 100))
        fs = frames(n)
        tx, ty = parabola(n)
        nx = tx + rng.uniform(-2.5, 2.5, n)
        ny = ty + rng.uniform(-2.5, 2.5, n)
        k = int(rng.integers(1, max(2, n // 6)))
        drop = sorted(rng.choice(np.arange(2, n - 2), size=k, replace=False).tolist())

        # 干净（只有噪声 + 缺失）：不该剔任何点
        clean = make_obs(fs, nx, ny, drop=drop)
        pts, st = process(clean)
        false_pos += st.n_outlier
        e = [math.hypot(pts[i].x - tx[i], pts[i].y - ty[i]) for i in drop if pts[i].x is not None]
        worst_interp = max(worst_interp, max(e) if e else 0.0)

        # 注入一个孤立跳变：应该被剔中
        bad = int(rng.integers(6, n - 6))
        while bad in drop:
            bad = int(rng.integers(6, n - 6))
        ang = float(rng.uniform(0, 2 * math.pi))
        mag = float(rng.uniform(300, 900))
        sx2, sy2 = nx.copy(), ny.copy()
        sx2[bad] += mag * math.cos(ang)
        sy2[bad] += mag * math.sin(ang)
        pts2, st2 = process(make_obs(fs, sx2, sy2, drop=drop))
        if pts2[bad].outlier:
            caught += 1
        assert st2.n_outlier <= 3, (seed, st2.n_outlier)

    assert false_pos == 0, f"干净数据上误剔了 {false_pos} 个点"
    assert caught >= 29, f"30 次注入只抓到 {caught}"
    assert worst_interp < 6.0, worst_interp
    report(
        "30 组随机种子",
        f"干净数据误剔={false_pos} 孤立跳变抓中={caught}/30 最大插值误差={worst_interp:.2f}px",
    )


def _runs(idx: List[int]) -> List[int]:
    """把下标列表拆成连续段的长度（用来对 max_gap）。"""
    if not idx:
        return [0]
    out, cur = [], 1
    for a, b in zip(idx, idx[1:]):
        if b == a + 1:
            cur += 1
        else:
            out.append(cur)
            cur = 1
    out.append(cur)
    return out


def main() -> int:
    tests = [
        test_parabola_with_dropouts,
        test_isolated_outlier,
        test_sustained_fast_motion,
        test_spline_guard_falls_back_to_linear,
        test_long_gap_stays_missing,
        test_smooth_keeps_endpoints,
        test_predict_next,
        test_degenerate_inputs,
        test_fuzz_many_seeds,
    ]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 组用例通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
