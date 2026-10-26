#!/usr/bin/env python3
"""Agent 标注内核冒烟：不连真实服务端、不下载 AOSS、不打 VLM。

用开发机上**已经 prepare 好**的包（`/mnt/afs/wangmin/data/_ai_label_work/work`）
把 `lease → view → label(cell) → submit` 走通，重点盯三件容易错的事：

1. **坐标只走 `GridView.cell_center` → `prep.to_pano`**。这个包的 ROI
   x=5626 宽 4036，右边界 9662 已经越过 7680 的全景 0° 接缝，任何地方自己写
   减法都会错一整个 pano 宽——所以断言拿 `prep.to_pano` 的结果直接对。
2. **labels.json 的字段是 `blurred`**，不是 `motion_blur`。
3. **目标帧没标满必须拒绝提交**，且 `submit` 走 `source="ai"` 进审核。

顺带在进程内喂一遍 stdio JSON-RPC，确认 MCP 那层真的会说协议、工具名没漂。

    venv/bin/python3 client/tests/test_agent_core.py

已解码的包不在（换了机器 / 清了缓存）时**优雅跳过**并打清楚原因，不硬失败。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from client import views as V  # noqa: E402
from client.agent_core import AgentSession  # noqa: E402
from client.api import Lease, ServerClient  # noqa: E402
from client.prep import load_prepared, workspace  # noqa: E402

WORK_SRC = Path(os.environ.get("BALL_TEST_WORK") or "/mnt/afs/wangmin/data/_ai_label_work/work")
GT_DIR = Path(os.environ.get("BALL_TEST_GT") or "/mnt/afs/wangmin/data/_ai_label_work/gt")
PACKET = os.environ.get("BALL_TEST_PACKET") or "260122__002__1703_1713"
TARGET_START = int(os.environ.get("BALL_TEST_TARGET_START") or 0)
# 整包 100 帧，冒烟只当前 6 个目标帧是「要标满的」，其余靠 repair 模式裁掉
N_TARGETS = 6


class FakeAPI(ServerClient):
    """不打 HTTP。记录 lease / heartbeat / release / submit。照 test_run.py 的写法。"""

    def __init__(self, lease: Optional[Lease]):
        super().__init__("http://mock.invalid")
        self._lease = lease
        self.released: List[str] = []
        self.submitted: List[Dict[str, Any]] = []
        self.heartbeats: List[str] = []
        self.lease_calls = 0

    def lease(self, worker: str = "", kind: str = "ai", packet_id: str = "",
              method: Optional[Dict[str, str]] = None) -> Optional[Lease]:
        self.lease_calls += 1
        self.lease_method = dict(method or {})
        return self._lease

    def heartbeat(self, lease_id: str, stage: Optional[str] = None) -> dict:
        self.heartbeats.append(stage or "")
        return {"ok": True}

    def release(self, lease_id: str, reason: str = "released") -> dict:
        self.released.append(reason)
        return {"ok": True}

    def get_gt(self, packet_id: str) -> List[dict]:
        return []

    def submit(self, result, bundle=None, **kw) -> dict:
        self.submitted.append({
            "result": result,
            "bundle": str(bundle) if bundle else None,
            "source": kw.get("source"),
            "lease_id": kw.get("lease_id"),
            "worker": kw.get("worker"),
            "method": kw.get("method"),
        })
        return {"ok": True, "submission_id": 7, "n_labeled": len(result.get("labels") or [])}


# ---------------------------------------------------------------- 夹具


def _meta() -> dict:
    return json.loads((WORK_SRC / PACKET / "meta.json").read_text())


def _targets() -> List[int]:
    """冒烟用的目标帧序列；`BALL_TEST_TARGET_START` 跳过包开头 GT 不可见的那段。"""
    return [int(x) for x in _meta()["targets"]][TARGET_START:]


def _gt_rows() -> List[dict]:
    src = next(GT_DIR.glob(f"{PACKET}__*.json"))
    return list(json.loads(src.read_text()).get("labels") or [])


def _link_work(tmp: Path) -> Path:
    """把已解码的包链到临时工作区，避免复制几百 MB JPEG。"""
    src = WORK_SRC / PACKET
    dst = workspace(tmp, PACKET)
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("prepare.json", "meta.json"):
        shutil.copy2(src / name, dst / name)
    for name in ("raw", "enh"):
        target = dst / name
        if target.exists() or target.is_symlink():
            continue
        target.symlink_to(src / name)
    return dst


def _lease(n_targets: int = N_TARGETS, kind: str = "repair") -> Lease:
    """用 repair 模式把「要标满的帧」裁到前几帧，好让缺帧校验跑得快。"""
    meta = _meta()
    return Lease(
        lease_id="lease-agent-1",
        packet_id=PACKET,
        aoss_key=f"packets/{PACKET}.zip",
        meta=meta,
        kind=kind,
        target_frames=_targets()[:n_targets],
    )


def _session(tmp: Path, api: FakeAPI, **kw) -> AgentSession:
    return AgentSession(
        api,
        work_root=tmp,
        worker="smoke-agent",
        # 心跳间隔调大：冒烟期间一条都不该打出去
        hb_interval=3600.0,
        fetch_fn=lambda **k: Path(k["dest"]),
        prepare_fn=lambda *a, **k: load_prepared(tmp, k.get("packet_id") or PACKET),
        method={"harness": "smoke", "harness_version": "0", "model": "fake", "model_version": "1"},
        **kw,
    )


def _claimed(tmp: Path, kind: str = "repair", n: int = N_TARGETS):
    _link_work(tmp)
    api = FakeAPI(_lease(n, kind))
    sess = _session(tmp, api)
    got = sess.claim(kind=kind)
    assert got.get("leased"), got
    for _ in range(100):
        if sess.prepare_state(PACKET)["ready"]:
            break
        time.sleep(0.05)
    return sess, api, got


# ---------------------------------------------------------------- 用例


def test_claim_and_resume() -> None:
    print("== 领包：写 session.json，再领一次接上同一包 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_claim_"))
    sess, api, got = _claimed(tmp)
    assert got["packet_id"] == PACKET and got["resume"] is False
    assert got["lease_id"] == "lease-agent-1"
    assert got["n_targets"] == N_TARGETS, got["n_targets"]
    assert got["roi"] == _meta()["roi"]
    st = sess.prepare_state(PACKET)
    assert st["ready"] and st["state"] == "ready", st
    assert st["n_labeled"] == 0 and st["next_frame"] == _targets()[0]

    again = sess.claim(kind="repair")
    assert again["resume"] is True and again["packet_id"] == PACKET, again
    assert api.lease_calls == 1, "接上未完成的会话时不该再向服务端要包"

    # 池空：得换个干净工作区，否则会先接上上面那个未完成的会话（那才是对的行为）
    fresh = Path(tempfile.mkdtemp(prefix="ball_agent_empty_"))
    empty = _session(fresh, FakeAPI(None)).claim()
    assert empty.get("leased") is False and empty["packet_id"] is None, empty
    shutil.rmtree(fresh, ignore_errors=True)

    # 模型自己传 worker / 占位版本号：不能续上同工作区里别人（wangmin）的租约，也不能改掉启动参数定的方法
    other = _session(tmp, FakeAPI(None))
    other.worker = "dsh-agent"
    hijack = other.claim(worker="smoke-agent", method={"harness_version": "unknown", "model": "gpt"})
    assert hijack.get("leased") is False and hijack["packet_id"] is None, hijack
    assert other.method["harness_version"] == "0" and other.method["model"] == "fake", other.method
    print(f"  lease → {PACKET} 目标帧 {got['n_targets']} 个；resume 不重复 lease；池空返回 leased=False")
    shutil.rmtree(tmp, ignore_errors=True)


def test_frames_listing() -> None:
    print("== ball_frames：目标帧 / 下一个未标帧 / 外推中心 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_frames_"))
    sess, _api, _ = _claimed(tmp)
    targets = _targets()[:N_TARGETS]
    fr = sess.frames(PACKET)
    assert fr["ready"] and fr["targets"] == targets, fr.get("targets")
    assert fr["missing"] == targets and fr["next_frame"] == targets[0]
    assert fr["predicted"] is None, "一个已标点都没有时不该给外推中心"

    # 拿真 GT 填头两帧，外推中心就该出来（且落在 ROI 内）
    prep = load_prepared(tmp, PACKET)
    gt = {int(r["frame_idx"]): r for r in _gt_rows()}
    for fi in targets[:2]:
        r = gt[fi]
        out = sess.label(PACKET, fi, pano_xy=[r["pano_x"], r["pano_y"]])
        assert out.get("ok"), out
    fr = sess.frames(PACKET)
    assert fr["n_labeled"] == 2 and fr["next_frame"] == targets[2]
    assert fr["predicted"] is not None, fr
    cx, cy = fr["predicted"]["center"]
    assert 0 <= cx < prep.size[0] and 0 <= cy < prep.size[1], (cx, cy)
    # 外推点应该离真值不远（这包球在动，线性外推几十像素以内）
    tgt = gt[targets[2]]
    tx, ty = prep.to_roi(float(tgt["pano_x"]), float(tgt["pano_y"]))
    err = float(np.hypot(cx - tx, cy - ty))
    assert err < 200, f"外推误差 {err:.1f}px 太大，说明 roi/pano 换算方向反了"
    assert fr["recent_visible"] and len(fr["recent_visible"][0]) == 3
    print(f"  已标 2 帧 → next={fr['next_frame']} 外推 ({cx:.0f},{cy:.0f}) "
          f"离真值 {err:.1f}px，半径 {fr['predicted']['radius_px']}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_view_global_and_crop() -> None:
    print("== ball_view：global 三段 + crop，网格元数据自洽 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_view_"))
    sess, _api, _ = _claimed(tmp)
    fi = _targets()[0]
    prep = load_prepared(tmp, PACKET)
    roi_w, roi_h = prep.size

    g = sess.render_view(PACKET, fi, mode="global")
    assert not g.get("error"), g
    assert g["cols"] == 9 and g["rows"] == 3, (g["cols"], g["rows"])
    assert len(g["image_paths"]) == 3, g["image_paths"]
    for p in g["image_paths"]:
        path = Path(p)
        assert path.exists() and path.stat().st_size > 0, p
        assert path.parent == workspace(tmp, PACKET) / "views", p
    assert len(g["letters"]) == 9 and g["letters"][0] == "A"
    # 三段字母连续、不重叠，并且拼起来正好是 master 的一套
    lets = [im["letters"] for im in g["images"]]
    assert "".join(lets) == g["letters"], lets
    assert abs(g["cell_w_px"] * g["cols"] - roi_w) < 1.5, (g["cell_w_px"], roi_w)
    assert abs(g["cell_h_px"] * g["rows"] - roi_h) < 1.5, (g["cell_h_px"], roi_h)

    c = sess.render_view(PACKET, fi, mode="crop", center=[roi_w / 2, roi_h / 2], size=480)
    assert not c.get("error"), c
    assert c["cols"] == 8 and c["rows"] == 8 and len(c["image_paths"]) == 1
    assert Path(c["image_paths"][0]).exists()
    assert abs(c["cell_w_px"] * c["cols"] - 480) < 1e-6, c["cell_w_px"]
    assert c["extent"] == [roi_w / 2 - 240, roi_h / 2 - 240, 480.0, 480.0], c["extent"]
    assert c["center_source"] == "given" and c["upscale"] > 1
    # 同样的参数应该复用同一个 view_id（出图有缓存意义，也证明 id 是参数的函数）
    again = sess.render_view(PACKET, fi, mode="crop", center=[roi_w / 2, roi_h / 2], size=480)
    assert again["view_id"] == c["view_id"], (c["view_id"], again["view_id"])
    assert sess.render_view(PACKET, fi, mode="crop", center=[10, 10], size=80)["view_id"] != c["view_id"]

    bad = sess.render_view(PACKET, fi, mode="montage")
    assert bad.get("error") and "mode" in bad["error"], bad
    assert sess.render_view(PACKET, 999999, mode="global").get("error")
    print(f"  global 3 张 {g['cell_w_px']:.0f}px/格；crop 480px→{c['cell_w_px']:.0f}px/格 "
          f"放大 {c['upscale']}×；view_id 可复用；坏 mode/坏帧号都被挡住")
    shutil.rmtree(tmp, ignore_errors=True)


def test_cell_to_pano_is_the_only_channel() -> None:
    print("== 坐标换算：cell → ROI → pano 必须与 prep.to_pano(cell_center) 一致 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_xy_"))
    sess, _api, _ = _claimed(tmp)
    fi = _targets()[0]
    prep = load_prepared(tmp, PACKET)
    roi = prep.roi
    assert roi[0] + roi[2] > prep.pano_w, (
        "这个包的 ROI 本该跨全景 0° 接缝，换包了就得重挑一个，不然测不出接缝 bug"
    )

    center = (prep.size[0] / 2.0, prep.size[1] / 2.0)
    for mode, kw, cell in (
        ("crop", {"center": list(center), "size": 480}, "C3"),
        ("crop", {"center": list(center), "size": 80}, "F7"),
        ("global", {}, "E2"),
    ):
        got = sess.render_view(PACKET, fi, mode=mode, **kw)
        assert not got.get("error"), got
        res = sess.resolve_point(got["view_id"], cell)
        assert not res.get("error"), res
        # 独立重建同一张视图，走 views.py 的格心 + prep.to_pano，逐位对
        if mode == "crop":
            ref = V.make_crop_view(prep, fi, center, float(kw["size"]),
                                   got["cols"], got["rows"], upscale=2, panes=("raw",))
        else:
            ref, _strips = V.make_global_strips(prep, fi, n_strips=3, cols_per_strip=3, rows=3)
        rx, ry = ref.cell_center(cell)
        px, py = prep.to_pano(rx, ry)
        assert abs(res["roi_xy"][0] - rx) < 0.01 and abs(res["roi_xy"][1] - ry) < 0.01, (res, rx, ry)
        assert abs(res["pano_xy"][0] - px) < 0.01 and abs(res["pano_xy"][1] - py) < 0.01, (res, px, py)
        # 接缝那侧的 pano_x 必须已经取过模，不能是 roi[0]+rx 这种越界值
        assert 0 <= res["pano_xy"][0] < prep.pano_w, res
        print(f"  {mode} {cell} → ROI({rx:.1f},{ry:.1f}) → pano({px:.1f},{py:.1f}) 对得上")

    got = sess.render_view(PACKET, fi, mode="crop", center=list(center), size=80)
    assert sess.resolve_point(got["view_id"], "Z9").get("error")
    assert sess.resolve_point("不存在的view", "A1").get("error")
    print("  乱答的格子名和不存在的 view_id 都被挡住，不抛异常")
    shutil.rmtree(tmp, ignore_errors=True)


def test_view_registry_survives_restart() -> None:
    print("== view_id 在新进程里仍能兑现（几何落盘）==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_restart_"))
    sess, api, _ = _claimed(tmp)
    fi = _targets()[0]
    got = sess.render_view(PACKET, fi, mode="crop", center=[2000, 300], size=80)
    first = sess.resolve_point(got["view_id"], "D4")
    fresh = _session(tmp, api)  # 全新 session，内存登记是空的
    again = fresh.resolve_point(got["view_id"], "D4")
    assert again == first, (first, again)
    img = fresh.view_image(got["view_id"])
    assert img["mime"] == "image/png" and img["data"][:8] == b"\x89PNG\r\n\x1a\n"
    assert fresh.view_image(got["view_id"], 3).get("error"), "index 越界该报错"
    print(f"  重建后 D4 仍是 {first['pano_xy']}；内联 PNG {len(img['data'])}B")
    shutil.rmtree(tmp, ignore_errors=True)


def test_label_field_names_and_guard() -> None:
    print("== ball_label：字段是 blurred；不给坐标又不说看不见要被拒 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_label_"))
    sess, _api, _ = _claimed(tmp)
    targets = _targets()[:N_TARGETS]
    fi = targets[0]
    view = sess.render_view(PACKET, fi, mode="crop", center=[2000, 300], size=80)

    out = sess.label(PACKET, fi, view_id=view["view_id"], cell="D4", blurred=True)
    assert out.get("ok"), out
    rec = out["record"]
    assert set(rec) == {"frame_idx", "visible", "pano_x", "pano_y",
                        "occluded", "blurred", "source", "cell"}, sorted(rec)
    assert "motion_blur" not in rec and rec["blurred"] is True
    assert rec["visible"] is True and rec["cell"] == "D4"
    expect = sess.resolve_point(view["view_id"], "D4")
    assert [rec["pano_x"], rec["pano_y"]] == expect["pano_xy"], (rec, expect)
    on_disk = json.loads((workspace(tmp, PACKET) / "labels.json").read_text())
    assert on_disk[str(fi)]["blurred"] is True and "blurred" in on_disk[str(fi)]

    # 看不见：允许，但不许留坐标
    out = sess.label(PACKET, targets[1], visible=False)
    assert out.get("ok") and out["record"]["visible"] is False
    assert out["record"]["pano_x"] is None and out["pano_xy"] is None, out

    # 不给坐标又不说看不见 → 拒绝（不许猜）
    bad = sess.label(PACKET, targets[2])
    assert bad.get("error") and "不许猜" in bad["error"], bad
    # cell 不带 view_id → 拒绝
    assert sess.label(PACKET, targets[2], cell="D4").get("error")
    # 拿别的帧的 view_id 来标这一帧 → 拒绝
    cross = sess.label(PACKET, targets[3], view_id=view["view_id"], cell="D4")
    assert cross.get("error") and "帧" in cross["error"], cross
    # ROI 外的坐标 → 拒绝
    assert sess.label(PACKET, targets[2], roi_xy=[99999, 5]).get("error")

    cleared = sess.label(PACKET, fi, clear=True)
    assert cleared.get("ok") and cleared["cleared"] is True
    assert str(fi) not in json.loads((workspace(tmp, PACKET) / "labels.json").read_text())
    print("  blurred 字段对；visible=false 不留坐标；空答/跨帧/越界/无 view_id 全被拒；clear 能删")
    shutil.rmtree(tmp, ignore_errors=True)


def test_submit_rejects_missing_then_accepts() -> None:
    print("== ball_submit：缺目标帧必须拒绝；标满后 source=ai ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_submit_"))
    sess, api, _ = _claimed(tmp)
    targets = _targets()[:N_TARGETS]
    gt = {int(r["frame_idx"]): r for r in _gt_rows()}

    sess.label(PACKET, targets[0], pano_xy=[gt[targets[0]]["pano_x"], gt[targets[0]]["pano_y"]])
    bad = sess.submit(PACKET)
    assert bad.get("error") and bad["n_missing"] == N_TARGETS - 1, bad
    assert bad["missing"] == targets[1:], bad["missing"]
    assert not api.submitted, "缺帧时一个字节都不该发给服务端"

    for fi in targets[1:]:
        r = gt[fi]
        if r.get("visible") and r.get("pano_x") is not None:
            out = sess.label(PACKET, fi, pano_xy=[r["pano_x"], r["pano_y"]],
                             occluded=bool(r.get("occluded")), blurred=bool(r.get("blurred")))
        else:
            out = sess.label(PACKET, fi, visible=False)
        assert out.get("ok"), out
    rv = sess.review(PACKET)
    assert rv.get("review_id") and rv["full_coverage"], rv
    ok = sess.submit(PACKET, visual_reviewed=True, review_id=rv["review_id"])
    assert ok.get("ok"), ok
    assert ok["n_labeled"] == N_TARGETS and ok["submission_id"] == 7, ok
    assert ok["review_warnings"] == [], ok["review_warnings"]
    receipt = json.loads(Path(ok["receipt"]).read_text())
    assert receipt["review_id"] == rv["review_id"] and receipt["submission_id"] == 7, receipt
    assert len(api.submitted) == 1
    sub = api.submitted[0]
    assert sub["source"] == "ai", sub["source"]
    assert sub["result"]["source"] == "ai", sub["result"]["source"]
    assert sub["lease_id"] == "lease-agent-1" and sub["worker"] == "smoke-agent"
    assert sub["result"]["annotator"] == "smoke-agent"
    assert sub["result"]["packet_id"] == PACKET
    assert len(sub["result"]["labels"]) == N_TARGETS
    assert not sub["bundle"]
    # 交完就封口
    assert sess.submit(PACKET).get("error")
    assert sess.label(PACKET, targets[0], visible=False).get("error")
    print(f"  缺 {N_TARGETS - 1} 帧被拒；标满后 submit source=ai "
          f"n_labeled={ok['n_labeled']} submission_id={ok['submission_id']}；提交后封口")
    shutil.rmtree(tmp, ignore_errors=True)


def test_submit_rejects_all_invisible() -> None:
    print("== 整包 0 可见拒绝免审入库 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_novis_"))
    sess, api, _ = _claimed(tmp)
    targets = _targets()[:N_TARGETS]
    for fi in targets:
        out = sess.label(PACKET, fi, visible=False)
        assert out.get("ok"), out
    bad = sess.submit(PACKET)
    assert bad.get("error") and "0 帧可见" in bad["error"], bad
    assert not api.submitted, "全不可见时一个字节都不该发给服务端"
    ok = sess.submit(PACKET, force=True)
    assert ok.get("ok"), ok
    assert api.submitted, "force 才允许交全不可见"
    print("  标满但 0 可见被拒；force=true 才放行")
    shutil.rmtree(tmp, ignore_errors=True)


def test_submit_force_and_release() -> None:
    print("== force 交残包 / release 换包 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_force_"))
    sess, api, _ = _claimed(tmp)
    targets = _targets()[:N_TARGETS]
    sess.label(PACKET, targets[0], visible=False)
    ok = sess.submit(PACKET, force=True)
    assert ok.get("ok") and ok["forced"] is True and ok["n_labeled"] == 1, ok
    assert api.submitted[0]["source"] == "ai"

    tmp2 = Path(tempfile.mkdtemp(prefix="ball_agent_rel_"))
    sess2, api2, _ = _claimed(tmp2)
    assert sess2.release(PACKET, reason_code="bogus").get("error")
    rel = sess2.release(PACKET, reason_code="lost_after_occlusion", note="球被球员挡住太久")
    assert rel.get("ok") and api2.released == ["hard:lost_after_occlusion: 球被球员挡住太久"], api2.released
    assert sess2.load_session(PACKET)["done"] is True
    assert sess2.submit(PACKET).get("error")
    print(f"  force 交了 {ok['n_labeled']} 帧残包；release 带 reason_code 上报 hard:…，之后不能再提交")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(tmp2, ignore_errors=True)


def test_out_of_roi_cell_rejected() -> None:
    print("== 窗超出 ROI 时，黑边里的格子不许当答案 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_edge_"))
    sess, _api, _ = _claimed(tmp)
    fi = _targets()[0]
    got = sess.render_view(PACKET, fi, mode="crop", center=[5, 5], size=480)
    assert got["out_of_roi"] is True, got
    bad = sess.resolve_point(got["view_id"], "A1")  # 左上角整格都在 ROI 外
    assert bad.get("error") and "ROI 之外" in bad["error"], bad
    good = sess.resolve_point(got["view_id"], "H8")
    assert not good.get("error"), good
    print(f"  A1 落在黑边被拒；H8 → {good['roi_xy']} 正常")
    shutil.rmtree(tmp, ignore_errors=True)


def test_plan_nominate_commit_gate() -> None:
    print("== plan / nominate / commit：过门才写入 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_plan_"))
    sess, _api, _ = _claimed(tmp)
    fi = _targets()[0]
    gt = {int(r["frame_idx"]): r for r in _gt_rows()}
    row = gt[fi]
    rx, ry = sess.prep(PACKET).to_roi(float(row["pano_x"]), float(row["pano_y"]))
    pl = sess.plan(PACKET, detail=True)
    assert pl.get("n_targets") == N_TARGETS and pl["frames"][0]["state"] == "empty", pl
    strip = sess.render_strip(PACKET, fi, center=[rx, ry], size=240)
    assert strip.get("view_id") and strip["size"] == 240, strip
    nom = sess.nominate(PACKET, fi, strip["view_id"], cells=[{"cell": strip["letters"][0] + "1", "conf": 0.4}])
    assert nom.get("ok") and nom["candidates"][0].get("verify"), nom
    # 乱点（ROI 左上）commit 应被拒
    bad = sess.commit(PACKET, track=[{"frame_idx": fi, "x": 8.0, "y": 8.0, "interp": False}])
    assert bad["n_accepted"] == 0 and bad["n_rejected"] >= 1, bad
    good = sess.commit(PACKET, track=[{"frame_idx": fi, "x": rx, "y": ry, "interp": False}])
    assert good["n_accepted"] + good["n_rejected"] == 1, good
    # 旧分数门对静止 / 弱对比的人工 GT 也会拒（默认路线因此改成视觉复核），这里只记结果
    verdict = "收" if good["n_accepted"] else f"也被拒 {good['rejected'][0]['why']}"
    print(f"  plan empty→nominate verify 在；乱点拒、GT 点{verdict}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_mcp_stdio_protocol() -> None:
    print("== MCP stdio：initialize → initialized → tools/list → tools/call ==")
    from client.mcp_server import McpServer, serve

    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_mcp_"))
    sess, _api, _ = _claimed(tmp)
    srv = McpServer(lambda: sess)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18",
                    "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "ball_status", "arguments": {"packet_id": PACKET}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "ball_nope", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "no/such/method"},
    ]
    out = io.StringIO()
    serve(srv, stdin=io.StringIO("\n".join(json.dumps(x) for x in lines) + "\n"), stdout=out)
    got = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
    assert [r["id"] for r in got] == [1, 2, 3, 4, 5], [r.get("id") for r in got]
    assert got[0]["result"]["protocolVersion"] == "2025-06-18"
    assert got[0]["result"]["serverInfo"]["name"] == "ball-gt"
    assert srv.initialized is True, "notifications/initialized 该被收下且不回响应"

    names = [t["name"] for t in got[1]["result"]["tools"]]
    need = {
        "ball_lease", "ball_status", "ball_frames", "ball_plan", "ball_scan",
        "ball_overview", "ball_strip", "ball_nominate", "ball_fit", "ball_follow",
        "ball_commit", "ball_absent", "ball_video", "ball_view",
        "ball_view_image", "ball_label", "ball_submit", "ball_release",
    }
    assert need <= set(names), sorted(need - set(names))
    for t in got[1]["result"]["tools"]:
        assert t["inputSchema"]["type"] == "object" and t["description"], t["name"]
        assert "handler" not in t, "handler 是本地函数，不许出现在协议里"

    body = json.loads(got[2]["result"]["content"][0]["text"])
    assert got[2]["result"]["isError"] is False and body["packet_id"] == PACKET
    assert got[3]["result"]["isError"] is True, got[3]
    assert got[4]["error"]["code"] == -32601, got[4]
    print(f"  {len(names)} 个工具全在：{', '.join(names)}")
    print("  未知工具 → isError=true（不是协议错）；未知方法 → -32601")
    shutil.rmtree(tmp, ignore_errors=True)


def test_mcp_view_image_inline() -> None:
    print("== MCP tools/call 出图：路径 + 内联 base64 两条路 ==")
    from client.mcp_server import McpServer

    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_mcpimg_"))
    sess, _api, _ = _claimed(tmp)
    srv = McpServer(lambda: sess)
    fi = _targets()[0]
    r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "ball_view",
        "arguments": {"packet_id": PACKET, "frame_idx": fi, "mode": "crop",
                      "center": [2000, 300], "size": 80},
    }})
    body = json.loads(r["result"]["content"][0]["text"])
    assert Path(body["image_paths"][0]).exists(), body
    r2 = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "ball_view_image", "arguments": {"view_id": body["view_id"]},
    }})
    blocks = r2["result"]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["mimeType"] == "image/png"
    import base64
    assert base64.b64decode(blocks[0]["data"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert blocks[1]["type"] == "text"
    print(f"  ball_view 给路径 {Path(body['image_paths'][0]).name}；"
          f"ball_view_image 给 {len(blocks[0]['data'])}B base64 PNG")
    shutil.rmtree(tmp, ignore_errors=True)


def _gt_roi(prep, gt: Dict[int, dict], fi: int):
    r = gt[fi]
    return prep.to_roi(float(r["pano_x"]), float(r["pano_y"]))


def test_coord_image_xy_channel() -> None:
    print("== coord 页：image_xy → ROI → pano，跨帧 / 标题条 / 格子 / 缩放都要挡住 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_coord_"))
    sess, _api, _ = _claimed(tmp)
    prep = load_prepared(tmp, PACKET)
    targets = _targets()[:N_TARGETS]
    gt = {int(r["frame_idx"]): r for r in _gt_rows()}
    fi = targets[0]
    rx, ry = _gt_roi(prep, gt, fi)
    cap = 40

    one = sess.render_view(PACKET, fi, mode="coord", center=[rx, ry])
    assert one["frames"] == [fi] and one["scale"] == 2 and one["size"] == 320, one
    assert one["pages"][0]["page_px"] == [640, 640 + cap], one["pages"][0]
    v = sess.render_view(PACKET, fi, mode="coord", center=[rx, ry], size=480, scale=1, frames=targets[:4])
    assert v.get("view_id") and v["frames"] == targets[:4], v
    page = v["pages"][0]
    vid = page["view_id"]
    _, x0, y0, _ = page["tiles"][0]
    ix, iy = rx - x0, ry - y0 + cap
    out = sess.label(PACKET, fi, view_id=vid, image_xy=[ix, iy])
    assert out.get("ok"), out
    got = sess.labels_dict(PACKET)[str(fi)]
    # 这包 ROI 跨 0° 接缝：pano 必须走 prep.to_pano 回绕，和人工 GT 对上
    assert abs(got["pano_x"] - gt[fi]["pano_x"]) < 0.6 and abs(got["pano_y"] - gt[fi]["pano_y"]) < 0.6, got
    seam = prep.to_pano(prep.size[0] - 1, 0)[0] < prep.to_pano(0, 0)[0]

    assert sess.label(PACKET, targets[1], view_id=vid, image_xy=[ix, iy]).get("error"), "跨帧必须拒"
    assert sess.label(PACKET, fi, view_id=vid, image_xy=[10, 10]).get("error"), "标题条必须拒"
    assert sess.label(PACKET, fi, view_id=vid, cell="C3").get("error"), "coord 页不认格子"
    assert sess.label(PACKET, fi, view_id=vid, image_xy=[ix, iy], seen_size=[100, 900]).get("error")
    pw, ph = page["page_px"]
    half = sess.label(PACKET, fi, view_id=vid, image_xy=[ix / 2, iy / 2], seen_size=[pw / 2, ph / 2])
    assert half.get("ok"), half

    pw1, ph1 = one["pages"][0]["page_px"]
    _, ox, oy, _ = one["pages"][0]["tiles"][0]
    nx, ny = ((rx - ox) * 2) * 1000 / pw1, ((ry - oy) * 2 + cap) * 1000 / ph1
    out = sess.label(PACKET, fi, view_id=one["view_id"], image_xy=[nx, ny], xy_unit="n1000")
    assert out.get("ok"), out
    got = sess.labels_dict(PACKET)[str(fi)]
    assert abs(got["pano_x"] - gt[fi]["pano_x"]) < 0.6 and abs(got["pano_y"] - gt[fi]["pano_y"]) < 0.6, got
    assert sess.label(PACKET, fi, view_id=one["view_id"], image_xy=[1200, 500], xy_unit="n1000").get("error")
    assert sess.label(PACKET, fi, view_id=one["view_id"], image_xy=[nx, ny], xy_unit="n1000",
                      seen_size=[pw1, ph1]).get("error"), "n1000 不该再带 seen_size"
    assert sess.label(PACKET, fi, view_id=one["view_id"], image_xy=[nx, ny], xy_unit="mm").get("error")

    r1x, r1y = _gt_roi(prep, gt, targets[1])
    out = sess.label(PACKET, targets[1], view_id=vid, image_xy=[r1x - x0 + 480, r1y - y0 + cap])
    assert out.get("ok"), out
    got1 = sess.labels_dict(PACKET)[str(targets[1])]
    assert abs(got1["pano_x"] - gt[targets[1]]["pano_x"]) < 0.6, got1

    v2 = sess.render_view(PACKET, fi, mode="coord", center=[rx, ry], size=320, scale=2,
                          frames=[targets[2]])
    p2 = v2["pages"][0]
    _, x0, y0, _ = p2["tiles"][0]
    r2x, r2y = _gt_roi(prep, gt, targets[2])
    out = sess.label(PACKET, targets[2], view_id=p2["view_id"],
                     image_xy=[(r2x - x0) * 2, (r2y - y0) * 2 + cap])
    assert out.get("ok"), out
    got2 = sess.labels_dict(PACKET)[str(targets[2])]
    assert abs(got2["pano_x"] - gt[targets[2]]["pano_x"]) < 0.6, got2
    assert sess.render_view(PACKET, fi, mode="coord", size=640, scale=2).get("error"), "超页面预算必须拒"
    print(f"  默认单帧 2×320；1×480 / 2×320 / n1000 往返误差 <0.6px（跨接缝={seam}）；"
          "跨帧 / 标题条 / cell / 错比例 seen_size / n1000 越界全被拒")
    shutil.rmtree(tmp, ignore_errors=True)


def test_review_snapshot_and_compact_plan() -> None:
    print("== ball_review 覆盖未标帧；改标后快照过期；plan 默认不带逐帧表 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_review_"))
    sess, _api, _ = _claimed(tmp)
    targets = _targets()[:N_TARGETS]
    gt = {int(r["frame_idx"]): r for r in _gt_rows()}
    for fi in targets[:3]:
        r = gt[fi]
        sess.label(PACKET, fi, pano_xy=[r["pano_x"], r["pano_y"]])
    sess.label(PACKET, targets[3], visible=False)

    rv = sess.review(PACKET)
    assert rv["missing_frames"] == targets[4:], rv["missing_frames"]
    assert rv["absent_frames"] == [targets[3]], rv["absent_frames"]
    statuses = {t[0]: t[3] for p in rv["pages"] for t in p["tiles"]}
    assert [statuses[f] for f in targets] == ["visible"] * 3 + ["absent"] + ["unlabeled"] * 2, statuses
    assert rv["absent_pages"] and rv["trajectory"], rv.keys()
    for p in rv["image_paths"]:
        assert Path(p).exists(), p

    prog = sess._progress(PACKET, sess.load_session(PACKET), sess._meta_dict(PACKET, sess.load_session(PACKET)))
    assert "review_stale" not in " ".join(
        sess._review_warnings(PACKET, prog["labels"], prog["targets"], rv["review_id"]))
    r = gt[targets[0]]
    sess.label(PACKET, targets[0], pano_xy=[r["pano_x"] + 5, r["pano_y"]])
    prog = sess._progress(PACKET, sess.load_session(PACKET), sess._meta_dict(PACKET, sess.load_session(PACKET)))
    warns = sess._review_warnings(PACKET, prog["labels"], prog["targets"], rv["review_id"])
    assert any(w.startswith("review_stale") for w in warns), warns
    assert sess.review(PACKET)["review_id"] != rv["review_id"]

    plan = sess.plan(PACKET)
    assert "frames" not in plan and plan["empty_frames"] == targets[4:], plan
    assert plan["absent_frames"] == [targets[3]] and plan.get("next"), plan
    assert len(sess.plan(PACKET, detail=True)["frames"]) == N_TARGETS
    print(f"  review {len(rv['pages'])} 页叠加 + {len(rv['absent_pages'])} 页缺球邻帧 + "
          f"{len(rv['trajectory'])} 段轨迹；改标后 review_stale；plan 精简")
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    missing = [p for p in (WORK_SRC / PACKET / "prepare.json", GT_DIR) if not p.exists()]
    if missing:
        print("跳过：这套冒烟要用开发机上已经 prepare 好的包和人工 GT，现在缺：")
        for p in missing:
            print(f"  - {p}")
        print(f"\n补齐办法：先 prepare 一个包到 {WORK_SRC}\n"
              f"  venv/bin/python -m client.prep <packet.zip> --work {WORK_SRC}\n"
              f"（或把 WORK_SRC / GT_DIR / PACKET 改成本机已有的包）")
        return 0

    for fn in (
        test_claim_and_resume,
        test_frames_listing,
        test_view_global_and_crop,
        test_cell_to_pano_is_the_only_channel,
        test_view_registry_survives_restart,
        test_label_field_names_and_guard,
        test_submit_rejects_missing_then_accepts,
        test_submit_rejects_all_invisible,
        test_submit_force_and_release,
        test_plan_nominate_commit_gate,
        test_out_of_roi_cell_rejected,
        test_coord_image_xy_channel,
        test_review_snapshot_and_compact_plan,
        test_mcp_stdio_protocol,
        test_mcp_view_image_inline,
    ):
        fn()
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
