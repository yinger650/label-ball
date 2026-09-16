#!/usr/bin/env python3
"""Agent 标注内核冒烟：不连真实服务端、不下载 AOSS、不打 VLM。

用开发机上**已经 prepare 好**的包（`/mnt/afs/wangmin/data/_ai_label_work/work`）
把 `lease → view → label(cell) → submit` 走通，重点盯三件容易错的事：

1. **坐标只走 `GridView.cell_center` → `prep.to_pano`**。这个包的 ROI
   x=5626 宽 4036，右边界 9662 已经越过 7680 的全景 0° 接缝，任何地方自己写
   减法都会错一整个 pano 宽——所以断言拿 `prep.to_pano` 的结果直接对。
2. **labels.json 的字段是 `blurred`**，不是 `motion_blur`。
3. **目标帧没标满必须拒绝提交**，且 `submit` 走 `source="human"`。

顺带在进程内喂一遍 stdio JSON-RPC，确认 MCP 那层真的会说协议、工具名没漂。

    venv/bin/python3 client/tests/test_agent_core.py

已解码的包不在（换了机器 / 清了缓存）时**优雅跳过**并打清楚原因，不硬失败。
"""

from __future__ import annotations

import io
import json
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

WORK_SRC = Path("/mnt/afs/wangmin/data/_ai_label_work/work")
GT_DIR = Path("/mnt/afs/wangmin/data/_ai_label_work/gt")
PACKET = "260122__002__1703_1713"
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

    def lease(self, worker: str = "", kind: str = "ai") -> Optional[Lease]:
        self.lease_calls += 1
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
        })
        return {"ok": True, "submission_id": 7, "n_labeled": len(result.get("labels") or [])}


# ---------------------------------------------------------------- 夹具


def _meta() -> dict:
    return json.loads((WORK_SRC / PACKET / "meta.json").read_text())


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
        target_frames=[int(x) for x in meta["targets"][:n_targets]],
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
    assert st["n_labeled"] == 0 and st["next_frame"] == _meta()["targets"][0]

    again = sess.claim(kind="repair")
    assert again["resume"] is True and again["packet_id"] == PACKET, again
    assert api.lease_calls == 1, "接上未完成的会话时不该再向服务端要包"

    # 池空：得换个干净工作区，否则会先接上上面那个未完成的会话（那才是对的行为）
    fresh = Path(tempfile.mkdtemp(prefix="ball_agent_empty_"))
    empty = _session(fresh, FakeAPI(None)).claim()
    assert empty.get("leased") is False and empty["packet_id"] is None, empty
    shutil.rmtree(fresh, ignore_errors=True)
    print(f"  lease → {PACKET} 目标帧 {got['n_targets']} 个；resume 不重复 lease；池空返回 leased=False")
    shutil.rmtree(tmp, ignore_errors=True)


def test_frames_listing() -> None:
    print("== ball_frames：目标帧 / 下一个未标帧 / 外推中心 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_frames_"))
    sess, _api, _ = _claimed(tmp)
    targets = _meta()["targets"][:N_TARGETS]
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
    fi = _meta()["targets"][0]
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
    fi = _meta()["targets"][0]
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
    fi = _meta()["targets"][0]
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
    targets = _meta()["targets"][:N_TARGETS]
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
    print("== ball_submit：缺目标帧必须拒绝；标满后 source=human ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_submit_"))
    sess, api, _ = _claimed(tmp)
    targets = _meta()["targets"][:N_TARGETS]
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
    ok = sess.submit(PACKET)
    assert ok.get("ok"), ok
    assert ok["n_labeled"] == N_TARGETS and ok["submission_id"] == 7, ok
    assert len(api.submitted) == 1
    sub = api.submitted[0]
    assert sub["source"] == "human", sub["source"]
    assert sub["result"]["source"] == "human", sub["result"]["source"]
    assert sub["lease_id"] == "lease-agent-1" and sub["worker"] == "smoke-agent"
    assert sub["result"]["annotator"] == "smoke-agent"
    assert sub["result"]["packet_id"] == PACKET
    assert len(sub["result"]["labels"]) == N_TARGETS
    assert not sub["bundle"]
    # 交完就封口
    assert sess.submit(PACKET).get("error")
    assert sess.label(PACKET, targets[0], visible=False).get("error")
    print(f"  缺 {N_TARGETS - 1} 帧被拒；标满后 submit source=human "
          f"n_labeled={ok['n_labeled']} submission_id={ok['submission_id']}；提交后封口")
    shutil.rmtree(tmp, ignore_errors=True)


def test_submit_rejects_all_invisible() -> None:
    print("== 整包 0 可见拒绝免审入库 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_novis_"))
    sess, api, _ = _claimed(tmp)
    targets = _meta()["targets"][:N_TARGETS]
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
    targets = _meta()["targets"][:N_TARGETS]
    sess.label(PACKET, targets[0], visible=False)
    ok = sess.submit(PACKET, force=True)
    assert ok.get("ok") and ok["forced"] is True and ok["n_labeled"] == 1, ok
    assert api.submitted[0]["source"] == "human"

    tmp2 = Path(tempfile.mkdtemp(prefix="ball_agent_rel_"))
    sess2, api2, _ = _claimed(tmp2)
    rel = sess2.release(PACKET, "球被球员挡住太久，换包")
    assert rel.get("ok") and api2.released == ["球被球员挡住太久，换包"], api2.released
    assert sess2.load_session(PACKET)["done"] is True
    assert sess2.submit(PACKET).get("error")
    print(f"  force 交了 {ok['n_labeled']} 帧残包；release 理由已上报，之后不能再提交")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(tmp2, ignore_errors=True)


def test_out_of_roi_cell_rejected() -> None:
    print("== 窗超出 ROI 时，黑边里的格子不许当答案 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_edge_"))
    sess, _api, _ = _claimed(tmp)
    fi = _meta()["targets"][0]
    got = sess.render_view(PACKET, fi, mode="crop", center=[5, 5], size=480)
    assert got["out_of_roi"] is True, got
    bad = sess.resolve_point(got["view_id"], "A1")  # 左上角整格都在 ROI 外
    assert bad.get("error") and "ROI 之外" in bad["error"], bad
    good = sess.resolve_point(got["view_id"], "H8")
    assert not good.get("error"), good
    print(f"  A1 落在黑边被拒；H8 → {good['roi_xy']} 正常")
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
    assert names == [
        "ball_lease", "ball_status", "ball_frames", "ball_view",
        "ball_view_image", "ball_label", "ball_submit", "ball_release",
    ], names
    for t in got[1]["result"]["tools"]:
        assert t["inputSchema"]["type"] == "object" and t["description"], t["name"]
        assert "handler" not in t, "handler 是本地函数，不许出现在协议里"

    body = json.loads(got[2]["result"]["content"][0]["text"])
    assert got[2]["result"]["isError"] is False and body["packet_id"] == PACKET
    assert got[3]["result"]["isError"] is True, got[3]
    assert got[4]["error"]["code"] == -32601, got[4]
    print(f"  8 个工具全在：{', '.join(names)}")
    print("  未知工具 → isError=true（不是协议错）；未知方法 → -32601")
    shutil.rmtree(tmp, ignore_errors=True)


def test_mcp_view_image_inline() -> None:
    print("== MCP tools/call 出图：路径 + 内联 base64 两条路 ==")
    from client.mcp_server import McpServer

    tmp = Path(tempfile.mkdtemp(prefix="ball_agent_mcpimg_"))
    sess, _api, _ = _claimed(tmp)
    srv = McpServer(lambda: sess)
    fi = _meta()["targets"][0]
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
        test_out_of_roi_cell_rejected,
        test_mcp_stdio_protocol,
        test_mcp_view_image_inline,
    ):
        fn()
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
