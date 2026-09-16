#!/usr/bin/env python3
"""精度链路自测：出场区掩膜、本地核验、扫描图、九宫格、以及它们在工作流里的接线。

这些改动的共同点是**它们的价值全在数字上**——掩膜压掉了多少假亮斑、核验能不能把
错点和 GT 点分开、门限扫描有没有单调性。所以这个文件的断言尽量都是量化的，
用一个已 prepare 好的真实包加人工 GT，不打任何真实模型调用。

    venv/bin/python3 client/tests/test_precision.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from client import field as F, verify as Vf, views as V  # noqa: E402
from client.prep import load_prepared, pano_to_roi  # noqa: E402
from client.propose import find_blobs  # noqa: E402
from client.vlm import VlmPool  # noqa: E402
from client.workflow import FrameRun, StageRun, Workflow  # noqa: E402

WORK = Path("/mnt/afs/wangmin/data/_ai_label_work/work")
GT = Path("/mnt/afs/wangmin/data/_ai_label_work/gt")
PACKET = "260122__002__1703_1713"


def _gt(prep, packet_id: str = PACKET) -> dict:
    out = {}
    for f in sorted(GT.glob(f"{packet_id}__*.json")):
        doc = json.loads(f.read_text())
        if doc.get("annotator") == "smoketest":
            continue
        for r in doc.get("labels", []):
            if r.get("visible") and r.get("pano_x") is not None:
                out[int(r["frame_idx"])] = pano_to_roi(
                    float(r["pano_x"]), float(r["pano_y"]), prep.roi, prep.pano_w)
    return out


# ---------------------------------------------------------------- 掩膜


def test_boundary_never_kills_gt() -> None:
    print("== 边界曲线：所有人工 GT 点都判在场内 ==")
    prep = load_prepared(WORK, PACKET)
    g = F.build(prep)
    assert g.ok, "这个包应该有 field_corners_pano"
    gt = _gt(prep)
    assert gt, "读不到人工 GT"
    margins = [gy - float(g.boundary_y(gx)) for gx, gy in gt.values()]
    bad = [m for m in margins if m < -g.air_margin]
    assert not bad, f"{len(bad)} 个 GT 点被判出场，最差 {min(margins):.0f}px"
    # 边界曲线必须在接缝上连续：x 和 x+roi_w 是同一根柱子
    w = prep.size[0]
    for x in (0.0, 137.0, w - 1.0):
        a, b = float(g.boundary_y(x)), float(g.boundary_y(x + w))
        assert abs(a - b) < 1e-6, f"x={x} 接缝不连续: {a} vs {b}"
    print(f"  {len(gt)} 个点，最小余量 {min(margins):.0f}px（air_margin={g.air_margin:.0f}）"
          f"，接缝连续")


def test_zone_three_levels() -> None:
    print("== 三档判定：场内 / 软带 / 出场 ==")
    prep = load_prepared(WORK, PACKET)
    g = F.build(prep)
    x = 2000.0
    yb = float(g.boundary_y(x))
    assert g.zone(x, yb + 100) == F.IN_PLAY
    assert g.zone(x, yb - g.air_margin + 1) == F.IN_PLAY
    assert g.zone(x, yb - g.air_margin - 10) == F.SOFT
    assert g.zone(x, yb - g.air_margin - g.soft_margin - 10) == F.OUT
    assert F.ZONE_SCORE[F.OUT] == 0.0, "出场必须是硬否决"
    print(f"  边界 y={yb:.0f}：场内/软带/出场三档的分界与 ZONE_SCORE 都对")


def test_mask_kills_out_of_play_blobs() -> None:
    print("== 掩膜：出场区的假亮斑不再产出候选 ==")
    prep = load_prepared(WORK, PACKET)
    g = F.build(prep)
    fi = prep.targets[len(prep.targets) // 2]
    enh = prep.enh(fi)
    play = F.play_mask(prep)
    before = find_blobs(enh, top_k=200)
    after = find_blobs(enh, top_k=200, play=play)

    def n_out(cs):
        return sum(1 for c in cs if not g.in_play(c.x, c.y)
                   or c.y < float(g.y_in_play(c.x)))

    assert n_out(after) == 0, f"掩膜后还有 {n_out(after)} 个出场区候选"
    assert n_out(before) > 0, "这一帧本来就没有出场区候选，换个帧测"
    keep = (play > 0).mean()
    assert 0.5 < keep < 1.0, f"掩膜保留比例 {keep:.1%} 不合理"
    print(f"  出场区候选 {n_out(before)} → 0；掩膜保留 ROI 的 {keep:.0%}")


def test_suppress_dims_but_keeps_field() -> None:
    print("== 压暗：出场区亮斑掉一个数量级，场内像素分毫不动 ==")
    prep = load_prepared(WORK, PACKET)
    g = F.build(prep)
    fi = prep.targets[len(prep.targets) // 2]
    enh = prep.enh(fi)
    out = F.suppress_out_of_play(prep, enh, is_enh=True)
    band = int(max(1, np.floor(float(g.y_in_play(np.arange(prep.size[0], dtype=float)).min())) - 4))
    assert enh[:band].max() > 80, "这个包顶部本来就没有亮斑，换个包测"
    assert out[:band].max() < enh[:band].max() * 0.35, \
        f"顶部亮斑没被压下去: {enh[:band].max()} → {out[:band].max()}"
    deep = int(prep.size[1] * 0.6)
    assert np.array_equal(enh[deep:], out[deep:]), "场内区域被改动了"
    # 绝不能原地改：帧缓存是共享的 LRU
    assert enh is not out and prep.enh(fi).max() == enh.max()
    print(f"  顶部峰值 {enh[:band].max()} → {out[:band].max()}；场内逐像素相同；原图未被改动")


# ---------------------------------------------------------------- 核验


def test_verify_separates_gt_from_wrong() -> None:
    print("== 核验：GT 点与故意偏 40–150px 的错点分得开 ==")
    prep = load_prepared(WORK, PACKET)
    gt = _gt(prep)
    frames = sorted(gt)
    step = float(getattr(prep.meta, "step", 1) or 1)
    rng = np.random.default_rng(20260914)
    good, bad = [], []
    for i, fi in enumerate(frames):
        hist = [(f, gt[f][0], gt[f][1]) for f in frames[max(0, i - 5): i]]
        gx, gy = gt[fi]
        good.append(Vf.judge(prep, fi, gx, gy, history=hist, step=step).score)
        off = float(rng.uniform(40, 150)) * float(rng.choice([-1, 1]))
        bad.append(Vf.judge(prep, fi, gx + off, gy + float(rng.uniform(-40, 40)),
                            history=hist, step=step).score)
    good, bad = np.array(good), np.array(bad)
    gp10, bp90 = np.percentile(good, 10), np.percentile(bad, 90)
    assert np.median(good) > 0.7, f"GT 点的核验分中位只有 {np.median(good):.3f}"
    assert np.median(bad) < 0.4, f"错点的核验分中位到了 {np.median(bad):.3f}"
    assert gp10 > bp90, f"两组重叠：GT p10={gp10:.3f} <= 错点 p90={bp90:.3f}"
    print(f"  GT: med={np.median(good):.3f} p10={gp10:.3f}　"
          f"错点: med={np.median(bad):.3f} p90={bp90:.3f}　可用门限区间 ({bp90:.2f}, {gp10:.2f})")


def test_verify_hard_veto_out_of_play() -> None:
    print("== 核验：答案落在看台上直接判 0 ==")
    prep = load_prepared(WORK, PACKET)
    g = F.build(prep)
    fi = prep.targets[5]
    x = 2000.0
    y = float(g.boundary_y(x)) - g.air_margin - g.soft_margin - 50
    ev = Vf.judge(prep, fi, x, y)
    assert ev.zone == F.OUT and ev.score == 0.0, ev.as_dict()
    assert "硬否决" in " ".join(ev.reasons)
    # 场内的同一个 x 不该被否
    ev2 = Vf.judge(prep, fi, x, float(g.boundary_y(x)) + 120)
    assert ev2.zone == F.IN_PLAY and ev2.score > 0
    print(f"  y={y:.0f} 判 out 得 0 分；y={g.boundary_y(x)+120:.0f} 判 in_play 得 {ev2.score:.2f}")


def test_verify_gate_drops_frame() -> None:
    print("== 核验门：不达门限的帧写成 visible=false，坐标不落盘 ==")
    prep = load_prepared(WORK, PACKET)
    g = F.build(prep)
    wf = Workflow.load("erp_ball_v2", VlmPool())
    wf.verify_gate = 0.99  # 故意调到几乎不可能达到
    fi = prep.targets[5]
    gt = _gt(prep)
    gx, gy = gt[sorted(gt)[0]]
    fr = FrameRun(frame_idx=fi, x=gx, y=gy, conf=0.9)
    wf._verify(fr, prep, history=None)
    assert fr.x is None and fr.visible is False, "门限没生效"
    assert fr.rejected is not None and abs(fr.rejected[0] - gx) < 1e-6
    assert fr.final_conf > 0 and fr.drop_reason, "final_conf / 原因必须留下来供离线扫门限"
    # 门限为 0 时只记分不弃帧
    wf.verify_gate = 0.0
    fr2 = FrameRun(frame_idx=fi, x=gx, y=gy, conf=0.9)
    wf._verify(fr2, prep, history=None)
    assert fr2.x is not None and fr2.verify_score is not None
    print(f"  gate=0.99 弃帧且留下 final_conf={fr.final_conf:.3f}；gate=0 只记分（"
          f"verify_score={fr2.verify_score:.3f}）")


# ---------------------------------------------------------------- RGB 主 / DoG 辅


def test_rgb_required_blocks_dog_only_stage() -> None:
    print("== rgb_required：只看滤波图的级不能被采纳，但可以当中心 ==")
    wf = Workflow.load("erp_ball_v2", VlmPool())
    assert wf.rgb_required, "v2 必须打开 rgb_required"
    fr = FrameRun(frame_idx=100)
    fr.stages = [
        StageRun(stage="refine2", kind="vlm_grid", ok=True, x=100.0, y=100.0,
                 conf=0.8, cell_px=10.0, panes=("raw", "enh")),
        StageRun(stage="dogonly", kind="vlm_grid", ok=True, x=104.0, y=101.0,
                 conf=0.9, cell_px=5.0, panes=("enh",)),
    ]
    wf._accept(fr)
    assert (fr.x, fr.y) == (100.0, 100.0), f"纯 DoG 的级被采纳了: {(fr.x, fr.y)}"
    assert "无 RGB 背书" in fr.stages[1].reason
    # 关掉这条规则，同一份数据就会采纳更细的那一级 —— 证明差别来自规则本身
    wf.rgb_required = False
    fr2 = FrameRun(frame_idx=100)
    fr2.stages = [
        StageRun(stage="refine2", kind="vlm_grid", ok=True, x=100.0, y=100.0,
                 conf=0.8, cell_px=10.0, panes=("raw", "enh")),
        StageRun(stage="dogonly", kind="vlm_grid", ok=True, x=104.0, y=101.0,
                 conf=0.9, cell_px=5.0, panes=("enh",)),
    ]
    wf._accept(fr2)
    assert (fr2.x, fr2.y) == (104.0, 101.0)
    print("  开着规则采纳 refine2(100,100)，关掉规则才采纳纯 DoG 的 (104,101)")


def test_center_only_stage_not_accepted() -> None:
    print("== accept: false：扫描图那一级只给中心，不当答案 ==")
    wf = Workflow.load("erp_ball_v2", VlmPool())
    scan = next(s for s in wf.stages if s["id"] == "scan")
    assert scan.get("accept") is False, "v2 的 scan 级必须 accept: false"
    fr = FrameRun(frame_idx=100)
    fr.stages = [StageRun(stage="scan", kind="vlm_grid", ok=True, x=50.0, y=50.0,
                          conf=0.9, cell_px=30.0, panes=("scan_rgb", "raw"),
                          center_only=True)]
    wf._accept(fr)
    assert fr.x is None and fr.visible is False
    print("  只有 scan 成功时整帧不采纳（等 refine3 收尾）")


# ---------------------------------------------------------------- 跟丢要重新找球


def test_lost_track_reanchors_then_cold_starts() -> None:
    """连续弃帧之后要分两档重找，不能顺着旧点继续外推。

    这是标定 v2_online06 实测出来的坑：门限 0.6 在线跑 precision 63.6%，比同一批
    数据离线扫出来的 85.2% 还低 —— 弃帧之后 `has_prior` 一直是真、外推照旧从更旧
    的点往下滚（predict1 中位误差 105–128 px），没有任何一级负责重新找球。

    两档分别是：`lost_after` 起只拿最后一个已定点当锚（不再拟合速度），
    `cold_after` 起连锚都扔掉退回冷启动。
    """
    print("== 跟丢：先锚回最后一个可信点，再退回冷启动 ==")
    prep = load_prepared(WORK, PACKET)
    wf = Workflow.load("erp_ball_v2", VlmPool())
    assert 1 <= wf.lost_after < wf.cold_after, \
        f"两档顺序反了：lost_after={wf.lost_after} cold_after={wf.cold_after}"
    last_good = (130.0, 200.0)
    seen: list = []  # 每帧真正拿到的 (prior, lost)

    def fake_run_frame(_prep, frame_idx, prior=None, prior_r=0.0, motion_px=0.0,
                       trace=False, history=None, lost=0):
        seen.append((prior, lost))
        # 头两帧答出来给外推打基线（第二帧就是 last_good），之后一律答不出来
        if len(seen) <= 2:
            return FrameRun(frame_idx=frame_idx, x=100.0 + 30 * (len(seen) - 1),
                            y=200.0, visible=True, conf=0.9)
        return FrameRun(frame_idx=frame_idx, visible=False)

    wf.run_frame = fake_run_frame  # type: ignore[method-assign]
    wf.run_packet(prep, frames=prep.targets[:wf.cold_after + 4], second_pass=False)

    priors = [p for p, _l in seen]
    losts = [l for _p, l in seen]
    # 丢一帧还不算跟丢 —— 单帧抖动不该把链路打回去重找
    early = [i for i, l in enumerate(losts) if 0 < l < wf.lost_after]
    assert early and all(priors[i] is not None for i in early), f"丢一帧就重找，太急了：{losts}"
    assert priors[early[-1]] != last_good, "还在跟的时候应该是外推位置，不是锚点"
    # 第一档：锚回最后一个已定点，而且之后每一帧都停在那儿不再往外滚
    mid = [i for i, l in enumerate(losts) if wf.lost_after <= l < wf.cold_after]
    assert mid, f"没有落在第一档的帧：{losts}"
    for i in mid:
        assert priors[i] is not None and priors[i] == last_good, \
            f"第 {i} 帧（lost={losts[i]}）的先验是 {priors[i]}，应该锚在 {last_good}"
    # 第二档：连锚都扔掉，冷启动那几级（when 里写 not has_prior）才会跑
    cold = [i for i, l in enumerate(losts) if l >= wf.cold_after]
    assert cold and all(priors[i] is None for i in cold), \
        f"到了 cold_after 还没退回冷启动：{losts}"
    stages = [s for s in wf.stages if "not has_prior" in str(s.get("when", ""))]
    assert {"propose", "confirm9"} <= {s["id"] for s in stages}, \
        "冷启动必须包含 propose + confirm9，否则重找到的点没有时间确认"
    print(f"  lost 序列 {losts}；第 {mid[0]} 帧起锚在 {last_good}，"
          f"第 {cold[0]} 帧起退回冷启动（{'+'.join(s['id'] for s in stages)}）")


def test_scan_triggers_on_stale_prior_not_on_model_saying_null() -> None:
    """scan 级的触发条件必须看先验准不准，不能看模型愿不愿意说找不到。

    v1 的 streak 挂在 `not refine2.ok` 上，实测三个包全是 ran=0 —— 模型的
    `null_rate` 是 0，它永远给答案，于是跟丢之后没有任何一级负责重新找球。
    """
    print("== scan 触发条件：看先验，不看模型说不说 null ==")
    from client.workflow import eval_when

    wf = Workflow.load("erp_ball_v2", VlmPool())
    scan = next(s for s in wf.stages if s["id"] == "scan")
    when = str(scan["when"])
    assert "refine2" not in when, f"scan 又挂回 refine2 的结果上了：{when}"
    base = {"has_prior": True, "prior_r": 10.0, "lost": 0, "motion_px": 30.0,
            "frame_idx": 0, "n_calls": 0, "true": True, "false": False, "null": None}
    assert not eval_when(when, base), "跟得好的时候不该多花这次调用"
    assert eval_when(when, {**base, "lost": 1}), "上一帧被弃掉时必须重新定位"
    assert eval_when(when, {**base, "prior_r": 120.0}), "外推不确定半径大时必须重新定位"
    assert not eval_when(when, {**base, "has_prior": False, "lost": 3}), \
        "没有先验时走冷启动，不该再花 scan 这一次"
    # scan 必须排在 refine2 之前，否则它给不了主力级的中心
    ids = [s["id"] for s in wf.stages]
    assert ids.index("scan") < ids.index("refine2"), "scan 要在 refine2 之前给出中心"
    assert "scan" in str(next(s for s in wf.stages if s["id"] == "refine2")["view"]["center"])
    print(f"  when=`{when}`，且排在 refine2 前面给它当中心")


# ---------------------------------------------------------------- 新视图


def test_scan_rgb_channels() -> None:
    print("== 扫描图：三个通道装三段时间，静止物三色齐亮 ==")
    prep = load_prepared(WORK, PACKET)
    step = int(getattr(prep.meta, "step", 1) or 1)
    fi = prep.targets[len(prep.targets) // 2]
    img = V.temporal_scan_rgb(prep, fi, span=4, step=step)
    assert img.ndim == 3 and img.shape[2] == 3
    cur = prep.enh(fi)
    assert np.array_equal(img[:, :, 2], cur), "R 通道必须就是当前帧"
    # 当前帧不许参与前后两个通道：否则球在三通道齐亮，和灯光一样变白
    past = V._maxproj_range(prep, fi, range(-4, -V.SCAN_GAP + 1), step)
    assert np.array_equal(img[:, :, 0], past), "B 通道应是过去段的最大投影"
    assert V.SCAN_GAP >= 2, "跳过 t±1 才能把颜色组拉开（实测中位 16.3→12.2px）"
    # 一个每帧都在动的位置（球员密集区）应该三通道都亮 → 接近灰白
    gray_like = ((img.min(axis=2) > 60) & (img.max(axis=2) > 120)).mean()
    assert gray_like > 0, "整张图没有任何三色齐亮的像素，编码可能没生效"
    print(f"  R=当前帧、B=过去段（gap={V.SCAN_GAP}）；三色齐亮像素占 {gray_like:.2%}")


def test_scan_localizes_ball_within_cell() -> None:
    print("== 扫描图：红色区域离球心的距离够小，能当 240px 窗的中心 ==")
    prep = load_prepared(WORK, PACKET)
    gt = _gt(prep)
    step = int(getattr(prep.meta, "step", 1) or 1)
    ds = []
    for fi in sorted(gt)[::3]:
        gx, gy = gt[fi]
        img = V.temporal_scan_rgb(prep, fi, span=4, step=step).astype(np.int16)
        R = 30
        y0, x0 = max(0, int(gy) - R), max(0, int(gx) - R)
        p = img[y0: int(gy) + R + 1, x0: int(gx) + R + 1]
        red = p[:, :, 2] - np.maximum(p[:, :, 0], p[:, :, 1])
        if red.max() <= 0:
            continue
        ys, xs = np.nonzero(red >= max(30, 0.6 * red.max()))
        ds.append(float(np.hypot(x0 + xs.mean() - gx, y0 + ys.mean() - gy)))
    d = np.array(ds)
    # 这是「够当中心」的口径，不是「够当答案」的口径 —— 后者要 refine3 来做
    assert np.median(d) < 20, f"红区重心中位偏 {np.median(d):.1f}px，当中心都不够"
    print(f"  {d.size} 帧：med={np.median(d):.1f}px p90={np.percentile(d,90):.1f}px"
          f"（够当中心，不够当答案，所以 scan 级 accept: false）")


def test_time_grid_has_no_selectable_cells() -> None:
    print("== 九宫格：9 格是 9 个时刻，图上不许有选格网格 ==")
    prep = load_prepared(WORK, PACKET)
    gt = _gt(prep)
    fi = sorted(gt)[len(gt) // 2]
    gx, gy = gt[fi]
    img, frames, labels = V.make_time_grid_view(prep, fi, (gx, gy), n=9)
    assert len(frames) == 9 and len(labels) == 9
    assert labels[-1] == "t0", f"最后一格必须是当前帧，拿到 {labels[-1]}"
    # yaml 里的窗不许小于标定值：小了球会飞出早期那几格，confirm9 就一律回「没有球」
    wf = Workflow.load("erp_ball_v2", VlmPool())
    c9 = next(s for s in wf.stages if s["id"] == "confirm9")
    assert float(c9["view"]["size"]) >= V.TIME_GRID_SIZE, \
        f"confirm9 的窗 {c9['view']['size']} 小于标定值 {V.TIME_GRID_SIZE}"
    assert frames[-1] == fi and frames[-2] == fi - V.TIME_GRID_FRAME_STEP, \
        "时间顺序必须是从早到晚，且相邻格隔 TIME_GRID_FRAME_STEP 个原始帧"
    # 每一格的取景范围相同 —— 静止物在 9 格里的位置才可比
    assert img.shape[0] > img.shape[1], "3×3 每格带标题条，应该比宽略高"
    # 没有 GridView 返回值可用，就没法误用它去 ball_label
    assert not isinstance(img, V.GridView)
    print(f"  帧 {frames[0]}…{frames[-1]}，标签 {labels[0]}…{labels[-1]}，"
          f"图 {img.shape[1]}×{img.shape[0]}，不返回 GridView")


def test_time_grid_shows_motion_and_keeps_ball() -> None:
    """九宫格要同时满足两件事，而它们互相拉扯：

    时间跨度太短，球在 9 格里挪不了一个球径，看着和静止的灯光没区别，这一问白花；
    跨度太长，球早飞出取景框，9 格里只剩两三格看得见它。所以窗口和帧间隔是扫出来的
    工作点（见 `views.make_time_grid_view` 的说明），这里守住两头的下界。
    """
    print("== 九宫格：跨度够看出移动，同时球别太早飞出框 ==")
    prep = load_prepared(WORK, PACKET)
    gt = _gt(prep)
    frames = sorted(gt)
    fa = np.array(frames, dtype=float)
    xs = np.array([gt[f][0] for f in frames])
    ys = np.array([gt[f][1] for f in frames])
    size = V.TIME_GRID_SIZE
    fstep = V.TIME_GRID_FRAME_STEP
    n_vis, spans = [], []
    for fi in frames:
        gx, gy = gt[fi]
        if fi - 8 * fstep < fa[0]:
            continue
        seen = 0
        for k in range(9):
            f0 = fi - k * fstep
            ox = float(np.interp(f0, fa, xs))
            oy = float(np.interp(f0, fa, ys))
            if abs(ox - gx) <= size / 2 and abs(oy - gy) <= size / 2:
                seen += 1
            if k == 8:
                spans.append(max(abs(ox - gx), abs(oy - gy)))
        n_vis.append(seen)
    v, s = np.array(n_vis), np.array(spans)
    assert v.size > 0
    # 至少 5 格看得见球，才谈得上「逐格平移」
    ok = float((v >= 5).mean())
    assert ok > 0.8, f"只有 {ok:.0%} 的帧有 ≥5 格看得见球"
    ball_diam = 20.0
    assert np.median(s) > ball_diam, \
        f"9 格总位移中位只有 {np.median(s):.0f}px，不到一个球径，看不出在动"
    print(f"  {ok:.0%} 的帧 ≥5 格看得见球；9 格总位移 med={np.median(s):.0f}px "
          f"p90={np.percentile(s, 90):.0f}px（窗 {size:.0f}px、每格隔 {fstep} 原始帧）")


# ---------------------------------------------------------------- 门限扫描


def test_sweep_gate_monotone() -> None:
    print("== 门限扫描：门限升高时答出的帧数单调不增 ==")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import calibrate as C  # noqa: E402

    runs = []
    rng = np.random.default_rng(7)
    for i in range(30):
        fi = 100 + i * 3
        fr = FrameRun(frame_idx=fi, x=100.0 + i * 12, y=200.0, conf=0.9)
        fr.verify_score = float(rng.uniform(0.1, 1.0))
        fr.final_conf = fr.conf * fr.verify_score
        runs.append(fr)
    gt = {r.frame_idx: (100.0 + i * 12, 200.0) for i, r in enumerate(runs)}
    rows = C.sweep_gate(runs, gt, fps=10.0, gates=[0.0, 0.2, 0.4, 0.6, 0.8])
    ans = [r["n_answered"] for r in rows]
    assert ans == sorted(ans, reverse=True), f"答出帧数不单调: {ans}"
    assert rows[0]["precision"] > 0 and rows[0]["n_gt"] == len(runs)
    print(f"  门限 0→0.8 答出帧数 {ans}，precision/recall 口径的分母都对")


def main() -> int:
    for fn in (
        test_boundary_never_kills_gt,
        test_zone_three_levels,
        test_mask_kills_out_of_play_blobs,
        test_suppress_dims_but_keeps_field,
        test_verify_separates_gt_from_wrong,
        test_verify_hard_veto_out_of_play,
        test_verify_gate_drops_frame,
        test_rgb_required_blocks_dog_only_stage,
        test_center_only_stage_not_accepted,
        test_lost_track_reanchors_then_cold_starts,
        test_scan_triggers_on_stale_prior_not_on_model_saying_null,
        test_scan_rgb_channels,
        test_scan_localizes_ball_within_cell,
        test_time_grid_has_no_selectable_cells,
        test_time_grid_shows_motion_and_keeps_ball,
        test_sweep_gate_monotone,
    ):
        fn()
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
