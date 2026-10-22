#!/usr/bin/env python3
"""主循环编排冒烟：不打真实 VLM、不打真实 AOSS、不连真实服务端。

mock lease / heartbeat / release / submit + mock 下载，用已有人工 GT 当 seed，
走通 `lease → (跳过下载) → prep → workflow(seed) → track → submit`。
另测一条：没有种子时必须 release，不得硬跑冷启动。

    venv/bin/python3 client/tests/test_run.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from client.api import Lease, ServerClient  # noqa: E402
from client.prep import load_prepared, pano_to_roi, workspace  # noqa: E402
from client.run import (  # noqa: E402
    MIN_SEED,
    RunConfig,
    process_lease,
    resolve_seed,
    run_loop,
)
from client.workflow import FrameRun  # noqa: E402

WORK_SRC = Path("/mnt/afs/wangmin/data/_ai_label_work/work")
GT_DIR = Path("/mnt/afs/wangmin/data/_ai_label_work/gt")
PACKET = "260122__002__1703_1713"
# 编排冒烟只跑前 8 个目标帧
N_FRAMES = 8


class FakeAPI(ServerClient):
    """不打 HTTP。记录 lease / heartbeat / release / submit 调用。"""

    def __init__(self, lease: Optional[Lease], gt_rows: Optional[List[dict]] = None):
        super().__init__("http://mock.invalid")
        self._lease = lease
        self.gt_rows = gt_rows or []
        self.released: List[str] = []
        self.submitted: List[Dict[str, Any]] = []
        self.heartbeats: List[str] = []
        self.lease_calls = 0

    def lease(self, worker: str = "", kind: str = "ai", packet_id: str = "",
              method: Optional[dict] = None) -> Optional[Lease]:
        self.lease_calls += 1
        return self._lease

    def heartbeat(self, lease_id: str, stage: Optional[str] = None) -> dict:
        self.heartbeats.append(stage or "")
        return {"ok": True}

    def release(self, lease_id: str, reason: str = "released") -> dict:
        self.released.append(reason)
        return {"ok": True}

    def start_heartbeat(self, lease_id: str, interval: float = 60.0):
        class _Nop:
            def stop(self): pass
            def set_stage(self, s): pass
        return _Nop()

    def get_gt(self, packet_id: str) -> List[dict]:
        return list(self.gt_rows)

    def submit(self, result, bundle=None, **kw) -> dict:
        self.submitted.append({
            "result": result,
            "bundle": str(bundle) if bundle else None,
            "source": kw.get("source"),
            "lease_id": kw.get("lease_id"),
            "workflow": kw.get("workflow"),
        })
        return {"ok": True, "submission_id": 1, "n_labeled": len(result.get("labels") or [])}

    def save_seed(self, packet_id, labels, **kw) -> dict:
        vis = [r for r in (labels or []) if r.get("visible") and r.get("pano_x") is not None]
        if kw.get("lease_id"):
            self.release(kw["lease_id"], "seed_ready")
        return {
            "ok": True,
            "n_visible": len(vis),
            "message": f"已保存预标（{len(vis)} 个可见点），包仍在待标注列表",
        }


def _gt_rows(prep) -> List[dict]:
    src = next(GT_DIR.glob(f"{PACKET}__*.json"))
    doc = json.loads(src.read_text())
    return list(doc.get("labels") or [])


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


def _fake_wf(prep, seed=None, frames=None, **kw):
    """用 GT 当「模型输出」，一帧都不打 VLM。"""
    want = list(frames if frames is not None else prep.targets)[:N_FRAMES]
    gt = {int(r["frame_idx"]): r for r in _gt_rows(prep)}
    seed_map = {fi: (x, y) for fi, x, y in (seed or [])}
    out: List[FrameRun] = []
    for fi in want:
        if fi in seed_map:
            x, y = seed_map[fi]
            out.append(FrameRun(frame_idx=fi, x=x, y=y, conf=1.0, visible=True))
            continue
        r = gt.get(fi)
        if r and r.get("visible") and r.get("pano_x") is not None:
            x, y = pano_to_roi(float(r["pano_x"]), float(r["pano_y"]), prep.roi, prep.pano_w)
            out.append(FrameRun(frame_idx=fi, x=x, y=y, conf=0.8, visible=True,
                                occluded=bool(r.get("occluded")), blurred=bool(r.get("blurred"))))
        else:
            out.append(FrameRun(frame_idx=fi, visible=False, x=None, y=None, conf=0.0))
    return out


def _lease(prior=None) -> Lease:
    meta = json.loads((WORK_SRC / PACKET / "meta.json").read_text())
    return Lease(
        lease_id="lease-smoke-1",
        packet_id=PACKET,
        aoss_key=f"packets/{PACKET}.zip",
        meta=meta,
        kind="ai",
        target_frames=list(meta.get("targets") or [])[:N_FRAMES],
        prior_labels=list(prior or []),
    )


def test_lease_and_key_parse() -> None:
    print("== Lease 解析 / aoss_key 归一化 ==")
    from client.fetch import normalize_aoss_key

    empty = Lease.from_response({"lease": None, "reason": "池子空"})
    assert empty is None
    flat = Lease.from_response({
        "lease_id": "L1", "packet_id": "p", "aoss_key": "packets/p.zip",
        "meta": {"roi": [1, 2, 3, 4]}, "target_frames": [10, 13],
        "prior_labels": [{"frame_idx": 10, "visible": True, "pano_x": 1, "pano_y": 2}],
    })
    assert flat and flat.lease_id == "L1" and flat.target_frames == [10, 13]
    wrapped = Lease.from_response({"lease": {"lease_id": "L2", "packet_id": "q", "aoss_key": "k"}})
    assert wrapped and wrapped.lease_id == "L2"
    assert normalize_aoss_key("packets/foo.zip") == "packets/foo.zip"
    assert normalize_aoss_key("ball_det/packets/foo.zip") == "packets/foo.zip"
    assert normalize_aoss_key("http://host/wangmin/ball_det/packets/foo.zip") == "packets/foo.zip"
    print("  lease 空/扁平/包裹 与 aoss_key 前缀剥离都对")


def test_no_seed_releases() -> None:
    print("== no-seed：没有种子必须 release，不得跑 workflow ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_run_noseed_"))
    api = FakeAPI(_lease(prior=[]), gt_rows=[])
    ran = {"wf": 0}

    def boom(*a, **k):
        ran["wf"] += 1
        raise AssertionError("没有 seed 不该进 workflow")

    cfg = RunConfig(
        api=api, work_root=tmp, worker="smoke",
        fetch_fn=lambda **k: Path(k["dest"]),
        prepare_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该 prep")),
        workflow_fn=boom,
        cleanup=False, keep_work=True,
    )
    status = process_lease(cfg, api._lease)
    assert status == "no_seed", status
    assert api.released == ["no_seed"], api.released
    assert not api.submitted
    assert ran["wf"] == 0
    print("  无 seed → release(no_seed)，workflow/prep/submit 都没跑")
    shutil.rmtree(tmp, ignore_errors=True)


def test_seed_from_local_labels() -> None:
    print("== 本机 labels.json ≥2 可见点当作 seed ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_run_local_"))
    _link_work(tmp)
    prep = load_prepared(tmp, PACKET)
    rows = _gt_rows(prep)
    vis = [r for r in rows if r.get("visible") and r.get("pano_x") is not None][:2]
    assert len(vis) >= MIN_SEED
    (workspace(tmp, PACKET) / "labels.json").write_text(
        json.dumps({str(r["frame_idx"]): r for r in vis}, ensure_ascii=False)
    )
    lease = _lease()
    seed, src = resolve_seed(work_root=tmp, packet_id=PACKET, lease=lease, api=FakeAPI(lease))
    assert src == "local-labels" and len(seed) >= 2
    print(f"  解析到 {len(seed)} 个种子，来源 {src} 帧 { [s[0] for s in seed] }")
    shutil.rmtree(tmp, ignore_errors=True)


def test_orchestrate_with_gt_seed() -> None:
    print("== 编排：mock 下载 + GT seed + 假 workflow + 真 track ==")
    assert (WORK_SRC / PACKET / "prepare.json").exists(), f"缺少已解码包 {WORK_SRC / PACKET}"
    tmp = Path(tempfile.mkdtemp(prefix="ball_run_ok_"))
    _link_work(tmp)
    prep0 = load_prepared(tmp, PACKET)
    gt_path = next(GT_DIR.glob(f"{PACKET}__*.json"))
    lease = _lease()
    api = FakeAPI(lease)
    fetched = {"n": 0}

    def fake_fetch(*, aoss_key, dest, **k):
        fetched["n"] += 1
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # 空 zip 占位，prep 走已就绪分支
        return dest

    def fake_prep(zip_path, work_root, packet_id=None, **k):
        assert is_ready_or(work_root, packet_id or PACKET)
        return load_prepared(work_root, packet_id or PACKET)

    def is_ready_or(work_root, pid):
        from client.prep import is_ready
        assert is_ready(work_root, pid)
        return True

    cfg = RunConfig(
        api=api,
        work_root=tmp,
        worker="smoke-ai",
        seed_gt=gt_path,
        fetch_fn=fake_fetch,
        prepare_fn=fake_prep,
        workflow_fn=_fake_wf,
        cleanup=False,
        keep_work=True,
    )
    status = process_lease(cfg, lease)
    assert status == "ok", status
    assert fetched["n"] == 1
    assert len(api.submitted) == 1
    sub = api.submitted[0]
    assert sub["source"] == "ai"
    assert sub["lease_id"] == "lease-smoke-1"
    labels = sub["result"]["labels"]
    assert len(labels) == N_FRAMES
    assert sum(1 for r in labels if r.get("visible")) >= 2
    assert sub["result"]["seed_source"].startswith("seed-gt")
    assert sub["bundle"] is None
    print(f"  submit n_labeled={len(labels)} seed={sub['result']['seed_source']}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_manual_seed_does_not_submit() -> None:
    print("== 人工站冷启动：写 labels + release，不 submit ==")
    from client import manual as M

    tmp = Path(tempfile.mkdtemp(prefix="ball_manual_"))
    _link_work(tmp)
    prep = load_prepared(tmp, PACKET)
    vis = [r for r in _gt_rows(prep) if r.get("visible") and r.get("pano_x") is not None][:2]
    (workspace(tmp, PACKET) / "labels.json").write_text(
        json.dumps({str(r["frame_idx"]): r for r in vis}, ensure_ascii=False)
    )
    (workspace(tmp, PACKET) / "session.json").write_text(json.dumps({
        "packet_id": PACKET, "lease_id": "lease-h", "kind": "human",
        "mode": "seed", "worker": "ann", "done": False,
    }))
    api = FakeAPI(_lease())
    M.init("http://mock.invalid", tmp, "ann", api=api)
    c = M.app.test_client()
    r = c.post(f"/api/finish/{PACKET}", json={"annotator": "ann", "seed_only": True})
    d = r.get_json()
    assert r.status_code == 200 and d.get("seed_only"), d
    assert api.released == ["seed_ready"]
    assert not api.submitted
    meta = c.get(f"/api/meta/{PACKET}").get_json()
    assert meta["mode"] == "seed" and len(meta["labels"]) >= 2
    print(f"  finish seed → {d['message'][:40]}…  meta.mode={meta['mode']}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_manual_seed_all_invisible_submits() -> None:
    print("== 预标整段不可见：允许提交，按无球入库，不卡 2 个可见点 ==")
    from client import manual as M

    tmp = Path(tempfile.mkdtemp(prefix="ball_manual_invis_"))
    _link_work(tmp)
    prep = load_prepared(tmp, PACKET)
    labels = {
        str(t): {"frame_idx": int(t), "visible": False, "pano_x": None, "pano_y": None}
        for t in prep.meta.targets
    }
    (workspace(tmp, PACKET) / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False)
    )
    (workspace(tmp, PACKET) / "session.json").write_text(json.dumps({
        "packet_id": PACKET, "lease_id": "lease-h", "kind": "human",
        "mode": "seed", "worker": "ann", "done": False,
    }))
    api = FakeAPI(_lease())
    M.init("http://mock.invalid", tmp, "ann", api=api)
    c = M.app.test_client()
    r = c.post(f"/api/finish/{PACKET}", json={
        "annotator": "ann", "seed_only": True, "skip_bundle": True,
    })
    d = r.get_json()
    assert r.status_code == 200, d
    assert d.get("no_ball") is True, d
    assert not d.get("seed_only")
    assert api.submitted, "整段不可见应走人工入库，不能只写 seed"
    vis = [x for x in (api.submitted[0]["result"].get("labels") or []) if x.get("visible")]
    assert vis == [], vis
    print(f"  finish all-invisible seed → {d['message'][:50]}")
    shutil.rmtree(tmp, ignore_errors=True)


def test_loop_once_empty() -> None:
    print("== --once 且池空：直接退出 ==")
    api = FakeAPI(None)
    n = run_loop(RunConfig(api=api, work_root=Path(tempfile.mkdtemp()), worker="x"),
                 once=True, idle_sec=0.01)
    assert n == 0 and api.lease_calls == 1
    print("  池空 + once → 成功 0 包，只 lease 一次")


def test_seed_gt_dir_without_match() -> None:
    print("== --seed-gt 目录没有该包文件：不把目录当文件读 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_run_seeddir_"))
    seed_dir = tmp / "gt"
    seed_dir.mkdir()
    (seed_dir / "other__pkg__0000_0001__x.json").write_text("{}", encoding="utf-8")
    lease = _lease(prior=[])
    seed, src = resolve_seed(
        work_root=tmp, packet_id=PACKET, lease=lease, api=FakeAPI(lease), seed_gt=seed_dir,
    )
    assert seed == [] and src == "", (seed, src)
    print("  目录无匹配 → 空种子，没有 IsADirectoryError")
    shutil.rmtree(tmp, ignore_errors=True)


def test_loop_backoff_on_error() -> None:
    print("== 处理失败后主循环要歇一下，不能立刻再领 ==")
    tmp = Path(tempfile.mkdtemp(prefix="ball_run_backoff_"))
    api = FakeAPI(_lease(prior=[]), gt_rows=[])

    def boom(*a, **k):
        raise RuntimeError("simulated")

    cfg = RunConfig(
        api=api, work_root=tmp, worker="smoke",
        fetch_fn=boom, cleanup=False, keep_work=True,
    )
    t0 = time.time()
    n = run_loop(cfg, max_packets=2, idle_sec=0.05)
    elapsed = time.time() - t0
    assert n == 0
    assert api.lease_calls == 2
    assert elapsed >= 0.05, elapsed
    assert api.released, api.released
    print(f"  两包失败 elapsed={elapsed:.3f}s ≥ idle，lease_calls={api.lease_calls} "
          f"released={api.released}")
    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_lease_and_key_parse()
    test_no_seed_releases()
    test_seed_from_local_labels()
    test_orchestrate_with_gt_seed()
    test_manual_seed_does_not_submit()
    test_manual_seed_all_invisible_submits()
    test_loop_once_empty()
    test_seed_gt_dir_without_match()
    test_loop_backoff_on_error()
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
