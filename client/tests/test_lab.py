#!/usr/bin/env python3
"""逐步点选调试：不打真实 VLM。用已解码包 + 人工 GT 当人点的格子。

    venv/bin/python3 client/tests/test_lab.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from client import field as F  # noqa: E402
from client.lab_core import (  # noqa: E402
    DEFAULT_GT,
    DEFAULT_WORK,
    LabSession,
    discover_packets,
)
from client.lab import app, init  # noqa: E402
from client.prep import load_prepared, pano_to_roi  # noqa: E402
from client.vlm import VlmPool  # noqa: E402
from client.workflow import Workflow  # noqa: E402

PACKET = "260122__002__1703_1713"
WORK = DEFAULT_WORK
GT = DEFAULT_GT


def _wf() -> Workflow:
    return Workflow.load("erp_ball_v1", VlmPool())


def test_extract_reasoning() -> None:
    from client.vlm import extract_reasoning
    print("== 抽出思考正文 ==")

    class Msg:
        reasoning_content = "中段滤波图有孤立白斑"

    assert "孤立白斑" in extract_reasoning(Msg(), "")
    tagged = extract_reasoning(None, "<think>先排除球员</think>\n{\"cell\":\"F2\"}")
    assert "先排除球员" in tagged
    print("  reasoning_content 与 <think> 都能抽出")


def test_parse_shift() -> None:
    from client import prompting
    print("== 解析 grid shift 指令 ==")
    assert prompting.parse_shift({"cell": None, "shift": {"x": -1, "y": 1}}) == (-1, 1)
    assert prompting.parse_shift({"shift": [2, -2]}) == (2, -2)
    assert prompting.parse_shift({"shift": "x-1, y+2"}) == (-1, 2)
    assert prompting.parse_shift({"shift_x": 1, "shift_y": 0}) == (1, 0)
    # 超出上限要夹住，0/缺失/脏输入当作「没说」
    assert prompting.parse_shift({"shift": {"x": 9, "y": -9}}) == (
        prompting.MAX_SHIFT_CELLS, -prompting.MAX_SHIFT_CELLS)
    assert prompting.parse_shift({"shift": {"x": 0, "y": 0}}) is None
    assert prompting.parse_shift({"cell": None, "shift": None}) is None
    assert prompting.parse_shift({"cell": "F2"}) is None
    assert prompting.parse_shift(None) is None and prompting.parse_shift({"shift": "左边"}) is None
    print("  四种写法都认，越界夹到 ±3，没说就是 None")


def test_shift_window_moves_crop() -> None:
    print("== 挪窗：局部窗按格平移，参考图跟着重画 ==")
    sess = LabSession.open(PACKET, WORK, _wf(), GT, seed_n=2)
    st = sess.state()
    assert st["pending"]["can_shift"], st["pending"]
    cx0, cy0 = sess.pending_center
    cell_w, cell_h = sess.pending_view.cell_w, sess.pending_view.cell_h
    st = sess.shift_window(1, -2)
    cx1, cy1 = sess.pending_center
    assert abs(cx1 - (cx0 + cell_w)) < 1e-6 and abs(cy1 - (cy0 - 2 * cell_h)) < 1e-6, (cx0, cx1)
    assert len(st["pending"]["shifts"]) == 1, st["pending"]["shifts"]
    assert st["pending"]["n_images"] == 2 and sess.pending_context is not None
    assert sess.shift_window(0, 0).get("error")
    print(f"  中心 ({cx0:.0f},{cy0:.0f}) → ({cx1:.0f},{cy1:.0f})，每格 {cell_w:.0f}px")


class _ScriptedPool:
    """按脚本回答的假模型池，用来验证 shift 循环，不打真实 VLM。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.n = 0

    def ask_json(self, role, prompt, images=(), **kw):
        from client.vlm import VlmResult
        obj = self.answers[min(self.n, len(self.answers) - 1)]
        self.n += 1
        return obj, VlmResult(ok=True, text=json.dumps(obj), provider="fake", model="fake")


def test_workflow_shift_then_answer() -> None:
    print("== 工作流：模型要求挪窗 → 重裁再问 ==")
    prep = load_prepared(WORK, PACKET)
    wf = _wf()
    wf.pool = _ScriptedPool([{"cell": None, "shift": {"x": 2, "y": -1}}, {"cell": "D4", "conf": 0.9}])
    spec = next(s for s in wf.stages if s.get("id") == "refine2")
    fi = prep.targets[0]
    r = wf._run_vlm(prep, fi, spec, (2000.0, 300.0), None, 0.0, False)
    cell_px = 80.0 / 8
    assert wf.pool.n == 2, wf.pool.n
    assert r.shifts and r.shifts[0]["dx"] == 2 and r.shifts[0]["dy"] == -1, r.shifts
    assert abs(r.center[0] - (2000 + 2 * cell_px)) < 1e-6, r.center
    assert abs(r.center[1] - (300 - cell_px)) < 1e-6, r.center
    assert r.ok and r.cell == "D4" and not r.no_ball
    print(f"  挪窗 {r.shifts[0]['dx']},{r.shifts[0]['dy']} → 中心 {r.center}，第二次答 {r.cell}")


def test_workflow_no_ball_gives_up() -> None:
    print("== 工作流：两张图都没球 → 这一级放弃，不再挪窗 ==")
    prep = load_prepared(WORK, PACKET)
    wf = _wf()
    wf.pool = _ScriptedPool([{"cell": None, "shift": None, "conf": 0.2}])
    spec = next(s for s in wf.stages if s.get("id") == "refine2")
    r = wf._run_vlm(prep, prep.targets[0], spec, (2000.0, 300.0), None, 0.0, False)
    assert wf.pool.n == 1 and not r.ok and r.no_ball and not r.shifts, (wf.pool.n, r.reason)
    # no_ball 要能被 when 表达式读到，否则 streak 还会接着钻
    from client.workflow import _Ref
    assert _Ref(r).no_ball is True and _Ref(None).no_ball is False
    print(f"  只问一次就收手：{r.reason}")


def test_workflow_review_keeps_same_cell() -> None:
    print("== review：第二次同格，置信度不动 ==")
    prep = load_prepared(WORK, PACKET)
    wf = _wf()
    wf.review = True
    wf.pool = _ScriptedPool([{"cell": "D4", "conf": 0.9}, {"cell": "D4", "conf": 0.95}])
    spec = next(s for s in wf.stages if s.get("id") == "refine2")
    r = wf._run_vlm(prep, prep.targets[0], spec, (2000.0, 300.0), None, 0.0, False)
    assert wf.pool.n == 2 and r.ok and r.cell == "D4" and not r.review_changed
    assert abs(r.conf - 0.9) < 1e-6, r.conf
    print(f"  初选=review={r.cell} conf={r.conf}")


def test_workflow_review_takes_second_and_decays() -> None:
    print("== review：第二次改格，用第二次并降 conf ==")
    from client import prompting
    prep = load_prepared(WORK, PACKET)
    wf = _wf()
    wf.review = True
    wf.pool = _ScriptedPool([{"cell": "D4", "conf": 0.9}, {"cell": "E4", "conf": 0.85}])
    spec = next(s for s in wf.stages if s.get("id") == "refine2")
    r = wf._run_vlm(prep, prep.targets[0], spec, (2000.0, 300.0), None, 0.0, False)
    assert r.ok and r.cell == "E4" and r.first_cell == "D4" and r.review_changed
    assert abs(r.conf - min(0.85, 0.9) * prompting.REVIEW_CONF_DECAY) < 1e-6, r.conf
    xy = r.x, r.y
    assert wf._build_view(prep, prep.targets[0], spec, (2000.0, 300.0), ("raw", "enh")).cell_of(*xy) == "E4"
    print(f"  {r.first_cell}→{r.cell} conf {0.9}→{r.conf:.3f}")


def test_workflow_review_null_rejects() -> None:
    print("== review：第二次说没球 → 这一级放弃 ==")
    prep = load_prepared(WORK, PACKET)
    wf = _wf()
    wf.review = True
    wf.pool = _ScriptedPool([{"cell": "D4", "conf": 0.9}, {"cell": None}])
    spec = next(s for s in wf.stages if s.get("id") == "refine2")
    r = wf._run_vlm(prep, prep.targets[0], spec, (2000.0, 300.0), None, 0.0, False)
    assert not r.ok and r.no_ball and r.review_changed and r.first_cell == "D4"
    print(f"  初选 {r.first_cell} 被 review 否定：{r.reason}")


def test_workflow_shift_rounds_capped() -> None:
    print("== 工作流：挪窗次数封顶 ==")
    from client import prompting
    prep = load_prepared(WORK, PACKET)
    wf = _wf()
    wf.pool = _ScriptedPool([{"cell": None, "shift": {"x": 1, "y": 0}}])
    spec = next(s for s in wf.stages if s.get("id") == "refine2")
    r = wf._run_vlm(prep, prep.targets[0], spec, (2000.0, 300.0), None, 0.0, False)
    assert wf.pool.n == prompting.MAX_SHIFT_ROUNDS + 1, wf.pool.n
    assert len(r.shifts) == prompting.MAX_SHIFT_ROUNDS and not r.ok, r.shifts
    print(f"  最多挪 {prompting.MAX_SHIFT_ROUNDS} 次后失败：{r.reason}")


def test_refine_prompt_says_ball_center_and_shift() -> None:
    print("== refine 提示词：球心 + shift 指令 ==")
    sess = LabSession.open(PACKET, WORK, _wf(), GT, seed_n=2)
    p = sess.state()["pending"]["prompt"]
    assert "球心" in p, p[:200]
    assert "shift" in p and "最多 ±3 格" in p, p[-400:]
    assert "都找不到球" in p or "都看不见" in p, p[-400:]
    print("  含球心选格规则、shift 用法、两图都没球时的放弃写法")


def test_field_boundary_gt_inside() -> None:
    print("== 球场边界：人工 GT 判在场内、天空点判出场 ==")
    prep = load_prepared(WORK, PACKET)
    geom = F.build(prep)
    assert geom.ok, geom.as_dict()
    doc = json.loads(next(GT.glob(f"{PACKET}__*.json")).read_text())
    n_in = 0
    rows = [r for r in doc["labels"] if r.get("visible") and r.get("pano_x") is not None]
    for row in rows:
        gx, gy = pano_to_roi(float(row["pano_x"]), float(row["pano_y"]), prep.roi, prep.pano_w)
        n_in += geom.zone(gx, gy) == F.IN_PLAY
    # 一个都不许误杀：air_margin 就是按这条量出来的
    assert n_in == len(rows), f"{len(rows) - n_in}/{len(rows)} 个 GT 点被判出场"
    gx, _gy = pano_to_roi(float(rows[0]["pano_x"]), float(rows[0]["pano_y"]),
                          prep.roi, prep.pano_w)
    assert geom.zone(gx, 8.0) != F.IN_PLAY
    print(f"  {len(rows)} 个 GT 点全在场内；y=8（看台）判 {geom.zone(gx, 8.0)}")


def test_session_seeded_human_clicks() -> None:
    print("== 带 2 帧种子：人按 GT 格子走完一帧 ==")
    wf = _wf()
    sess = LabSession.open(PACKET, WORK, wf, GT, seed_n=2)
    st = sess.state()
    assert st["seed_n"] == 2 and len(st["seed_frames"]) == 2, st
    assert st["pending"] is not None, st
    # 有先验时应跳过 propose / refine1
    skipped = {s["stage"] for s in st["stages"] if s["skipped"]}
    assert "propose" in skipped and "refine1" in skipped, skipped
    assert st["pending"]["stage"] in ("predict1", "refine2"), st["pending"]
    assert st["pending"]["n_images"] == 2, st["pending"]
    assert st["pending"]["images"][0]["role"] == "context"
    assert "足球" in st["pending"]["prompt"] or "格子" in st["pending"]["prompt"]
    assert "轨迹" in st["pending"]["prompt"] or "前几帧" in st["pending"]["prompt"]
    assert "原图" in st["pending"]["prompt"] or "RGB" in st["pending"]["prompt"]

    gt = sess.gt[sess.fr.frame_idx]
    n_commit = 0
    while sess.pending_view is not None and n_commit < 6:
        cell = sess.pending_view.cell_of(gt[0], gt[1])
        assert cell, f"GT 不在 {sess.pending_spec.get('id')} 窗内"
        st = sess.commit_cell(cell, conf=0.9, source="human")
        n_commit += 1
    assert sess.pending_spec is None, "一帧的 vlm 级应当走完"
    acc = sess.state()["accepted"]
    assert acc and acc["gt_err"] is not None and acc["gt_err"] < 12, acc
    assert acc.get("in_field") is not False
    print(f"  点了 {n_commit} 级，采纳误差 {acc['gt_err']}px，阶段 {[s['stage'] for s in st['stages'] if not s['skipped']]}")


def test_cold_start_global_then_refine1() -> None:
    print("== 冷启动：无种子第一级是整幅 ROI（propose） ==")
    sess = LabSession.open(PACKET, WORK, _wf(), GT, seed_n=0)
    st = sess.state()
    assert st["pending"] and st["pending"]["stage"] == "propose", st["pending"]
    assert st["pending"]["pixel_click"]
    assert st["pending"]["kind"] == "local_propose"
    assert st["pending"]["n_images"] == 3, st["pending"]
    assert st["pending"]["grid"] == [9, 3], st["pending"]
    assert st["pending"]["cell_px"] > 400, st["pending"]
    assert len(sess.pending_strips) == 3
    assert all(abs(sv.upscale - 1.0) < 1e-6 for sv in sess.pending_strips)
    assert [sv.letters() for sv in sess.pending_strips] == ["ABC", "DEF", "GHJ"]
    assert "冷启动" in st["pending"]["prompt"]
    assert "1:1" in st["pending"]["prompt"] or "原分辨率" in st["pending"]["prompt"]
    gt = sess.gt[sess.fr.frame_idx]
    st = sess.commit_xy(gt[0], gt[1], conf=0.9, source="human")
    assert st["pending"] and st["pending"]["stage"] == "refine1", st["pending"]
    assert st["pending"]["size"] == 480
    assert st["pending"]["n_images"] == 2, st["pending"]
    assert st["pending"]["images"][0]["role"] == "context"
    assert st["pending"]["images"][0]["clickable"] is False
    assert "RGB" in st["pending"]["prompt"] or "原图" in st["pending"]["prompt"]
    assert "参考" in st["pending"]["prompt"]
    n_c = len(next(s for s in st["stages"] if s["stage"] == "propose").get("candidates") or [])
    print(f"  全局点选后进入 refine1，窗 {st['pending']['size']}px，propose 候选 {n_c} 个")


def test_global_click_uses_pixel_not_cell_center() -> None:
    print("== 冷启动：在对应那张 1:1 条带上点，用精确 ROI 坐标 ==")
    sess = LabSession.open(PACKET, WORK, _wf(), GT, seed_n=0)
    gt = sess.gt[sess.fr.frame_idx]
    strip_i = next(i for i, sv in enumerate(sess.pending_strips) if sv.contains(gt[0], gt[1]))
    view = sess.pending_strips[strip_i]
    assert view.kind == "global" and abs(view.upscale - 1.0) < 1e-6
    assert strip_i == 1, (gt, strip_i, view.extent)
    L = view.layout()
    x0, y0, w, h = view.extent
    cx = L["body_x"] + (gt[0] - x0) / w * L["body_w"]
    cy = L["body_y"] + (gt[1] - y0) / h * L["body_h"]
    hit = sess.click_canvas(cx, cy, strip=strip_i)
    err = float(np.hypot(hit["x"] - gt[0], hit["y"] - gt[1]))
    cell_xy = sess.pending_view.cell_center(hit["cell"])
    cell_err = float(np.hypot(cell_xy[0] - gt[0], cell_xy[1] - gt[1]))
    assert err < 2.0, (err, hit, gt)
    st = sess.commit_xy(hit["x"], hit["y"], conf=0.9, source="human")
    prop = next(s for s in st["stages"] if s["stage"] == "propose")
    assert abs(prop["x"] - gt[0]) < 2 and abs(prop["y"] - gt[1]) < 2, prop
    print(f"  图{strip_i + 1} 点击误差 {err:.2f}px，同格格心误差 {cell_err:.1f}px，格子 {hit['cell']}")


def test_undo_back_to_propose() -> None:
    print("== 回退：初选提交后能退回 propose ==")
    sess = LabSession.open(PACKET, WORK, _wf(), GT, seed_n=0)
    gt = sess.gt[sess.fr.frame_idx]
    st = sess.commit_xy(gt[0], gt[1], conf=0.9, source="human")
    assert st["pending"] and st["pending"]["stage"] == "refine1", st["pending"]
    assert st["can_undo"]
    st = sess.undo()
    assert st["pending"] and st["pending"]["stage"] == "propose", st["pending"]
    assert st["pending"]["n_images"] == 3
    err = sess.undo()
    assert err.get("error"), err
    print("  commit → refine1 → undo → propose；再 undo 被拒绝")


def test_next_frame_coarse_uses_history() -> None:
    print("== 后续帧粗选提示词带上前几帧 ==")
    sess = LabSession.open(PACKET, WORK, _wf(), GT, seed_n=1)
    st = sess.state()
    assert st["pending"], st
    assert "前几帧" in st["pending"]["prompt"] or "橙色" in st["pending"]["prompt"], st["pending"]["prompt"][:200]
    print(f"  种子 1 后停在 {st['pending']['stage']}，提示词含历史点")


def test_flask_bootstrap_and_click() -> None:
    print("== Flask：bootstrap + 打开 + 点格子 ==")
    init(WORK, extra=[], gt=GT)
    c = app.test_client()
    b = c.get("/api/bootstrap").get_json()
    ids = [p["packet_id"] for p in b["packets"]]
    assert PACKET in ids, ids
    r0 = c.post("/api/open", json={"packet_id": PACKET, "work_root": str(WORK), "seed_n": 0})
    d0 = r0.get_json()
    assert r0.status_code == 200 and d0["pending"]["stage"] == "propose", d0
    assert d0["pending"]["pixel_click"] and d0["pending"]["kind"] == "local_propose"
    assert d0["pending"]["n_images"] == 3 and len(d0["pending"]["strips"]) == 3
    for i in range(3):
        vi = c.get(f"/api/view/{PACKET}/{i}.jpg")
        assert vi.status_code == 200 and vi.mimetype.startswith("image/"), (i, vi.status_code)
    L0 = d0["pending"]["strips"][0]["layout"]
    cx0 = L0["body_x"] + L0["body_w"] / L0["cols"] * 0.5
    cy0 = L0["body_y"] + L0["body_h"] / L0["rows"] * 0.5
    d1 = c.post(f"/api/click/{PACKET}", json={"x": cx0, "y": cy0, "strip": 0, "commit": True}).get_json()
    assert d1["pending"] and d1["pending"]["stage"] == "refine1", d1.get("pending")
    d2u = c.post(f"/api/undo/{PACKET}").get_json()
    assert d2u["pending"] and d2u["pending"]["stage"] == "propose", d2u.get("pending")
    r = c.post("/api/open", json={"packet_id": PACKET, "work_root": str(WORK), "seed_n": 2})
    d = r.get_json()
    assert r.status_code == 200 and d.get("pending"), d
    view = c.get(f"/api/view/{PACKET}.jpg")
    assert r.status_code == 200 and view.status_code == 200 and view.mimetype.startswith("image/")
    assert d["pending"]["n_images"] == 2
    ctx = c.get(f"/api/view/{PACKET}/0.jpg")
    loc = c.get(f"/api/view/{PACKET}/1.jpg")
    assert ctx.status_code == 200 and loc.status_code == 200
    # 用布局算 A1 的画布坐标再点（局部图，不带 strip）
    L = d["pending"]["layout"]
    cx = L["body_x"] + L["body_w"] / L["cols"] * 0.5
    cy = L["body_y"] + L["body_h"] / L["rows"] * 0.5
    hit = c.post(f"/api/click/{PACKET}", json={"x": cx, "y": cy}).get_json()
    assert hit.get("cell") == "A1", hit
    d2 = c.post(f"/api/click/{PACKET}", json={"cell": "A1", "conf": 0.8}).get_json()
    assert any(s.get("cell") == "A1" and not s.get("skipped") for s in d2["stages"]), d2["stages"]
    home = c.get("/")
    assert r.status_code == 200 and home.status_code == 200 and b"lab.js" in home.data
    print(f"  bootstrap {len(ids)} 包，打开后点 A1 写入当前级")


def test_discover() -> None:
    print("== 发现本机已解码包 ==")
    pkts = discover_packets([WORK])
    assert any(p["packet_id"] == PACKET for p in pkts)
    print(f"  work 下 {len(pkts)} 个包")


def main() -> None:
    assert (WORK / PACKET / "prepare.json").exists(), f"缺少 {WORK / PACKET}"
    test_extract_reasoning()
    test_parse_shift()
    test_shift_window_moves_crop()
    test_workflow_shift_then_answer()
    test_workflow_no_ball_gives_up()
    test_workflow_review_keeps_same_cell()
    test_workflow_review_takes_second_and_decays()
    test_workflow_review_null_rejects()
    test_workflow_shift_rounds_capped()
    test_refine_prompt_says_ball_center_and_shift()
    test_field_boundary_gt_inside()
    test_cold_start_global_then_refine1()
    test_global_click_uses_pixel_not_cell_center()
    test_undo_back_to_propose()
    test_session_seeded_human_clicks()
    test_next_frame_coarse_uses_history()
    test_flask_bootstrap_and_click()
    test_discover()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
