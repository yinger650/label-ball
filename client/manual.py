#!/usr/bin/env python3
"""本地人工标注站：127.0.0.1:8780。

首页三标签列表（待标注 / 待审核 / 待修复），工作台与线上审核页同一套播放器。
领包走服务端 lease，zip 本地 AOSS 直下。提交 `source=human` 免审入库；
预标走 `POST /api/v2/seed`，包仍留在待标注，AI 优先领。

    venv/bin/python3 -m client.manual --server https://ballgt.yinger650.com --worker 你的名字
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client.api import ApiError, ServerClient  # noqa: E402
from client.prep import (  # noqa: E402
    PrepareStatus,
    is_raw_ready,
    is_ready,
    load_prepared,
    prepare_packet,
    workspace,
)
from client.run import (  # noqa: E402
    DEFAULT_WORK,
    MIN_SEED,
    iter_label_rows,
    load_label_doc,
    local_labels_path,
)
from client.sparse import (  # noqa: E402
    all_explicit_invisible,
    fill_sparse_labels,
    seed_submit_error,
    validate_sparse_anchors,
)
from packet import build_result  # noqa: E402

log = logging.getLogger("client.manual")

REPO = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO / "server"

app = Flask(
    __name__,
    static_folder=str(SERVER_DIR / "static"),
    template_folder=str(SERVER_DIR / "templates"),
)

CFG: Dict[str, Any] = {}
# packet_id -> 后台准备任务
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOB_LOCK = threading.Lock()


def _api() -> ServerClient:
    return CFG["api"]


def _work() -> Path:
    return Path(CFG["work_root"])


def _session_path(pid: str) -> Path:
    return workspace(_work(), pid) / "session.json"


def _load_session(pid: str) -> dict:
    p = _session_path(pid)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_session(pid: str, data: dict) -> None:
    p = _session_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _labels_dict(pid: str) -> Dict[str, dict]:
    doc = load_label_doc(local_labels_path(_work(), pid)) or {}
    out: Dict[str, dict] = {}
    if isinstance(doc, dict) and not isinstance(doc.get("labels"), (list, dict)):
        for k, v in doc.items():
            if isinstance(v, dict) and v.get("frame_idx") is not None:
                out[str(int(v["frame_idx"]))] = v
        if out:
            return out
    for r in iter_label_rows(doc):
        if r.get("frame_idx") is None:
            continue
        out[str(int(r["frame_idx"]))] = r
    return out


def _write_labels(pid: str, labels: Dict[str, dict]) -> None:
    p = local_labels_path(_work(), pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")


def _want_frames(sess: dict, meta) -> List[int]:
    want = sess.get("target_frames") if (sess.get("mode") == "repair" or sess.get("kind") == "repair") else meta.targets
    return [int(x) for x in (want or meta.targets)]


def _fill_for_submit(sess: dict, prep, labels: Dict[str, dict]) -> Tuple[Dict[str, dict], List[int], List[int]]:
    want = _want_frames(sess, prep.meta)
    if all_explicit_invisible(labels, want):
        return labels, [], want
    filled_labels, filled_ids = fill_sparse_labels(
        labels, want, prep.meta.roi, prep.meta.pano_size[0], float(prep.meta.fps or 30.0),
    )
    return filled_labels, filled_ids, want


def _merge_prior(pid: str, prior: list, mark_ai: bool = True) -> Dict[str, dict]:
    labels = _labels_dict(pid)
    for r in prior or []:
        if not isinstance(r, dict) or r.get("frame_idx") is None:
            continue
        key = str(int(r["frame_idx"]))
        if key in labels:
            continue
        rec = dict(r)
        rec["frame_idx"] = int(r["frame_idx"])
        if mark_ai:
            rec["from_ai"] = True
        labels[key] = rec
    _write_labels(pid, labels)
    return labels


def _heartbeat_loop(lease_id: str, interval: float) -> None:
    api: ServerClient = _api()
    while True:
        time.sleep(interval)
        sess_hit = False
        for p in _work().glob("*/session.json"):
            try:
                d = json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                continue
            if d.get("lease_id") == lease_id and not d.get("done"):
                sess_hit = True
                break
        if not sess_hit:
            return
        try:
            api.heartbeat(lease_id, "annotate")
        except Exception as e:  # noqa: BLE001
            log.warning("人工站心跳失败: %s", e)


def _start_prepare(pid: str, aoss_key: str) -> None:
    st = PrepareStatus()
    with _JOB_LOCK:
        _JOBS[pid] = {"state": "pending", "msg": "排队下载", "status": st}

    def on_raw() -> None:
        with _JOB_LOCK:
            _JOBS[pid] = {"state": "raw_ready", "msg": "原图已就绪，DoG 后台下载", "status": st}

    def run() -> None:
        try:
            dest_dir = workspace(_work(), pid)
            dest_dir.mkdir(parents=True, exist_ok=True)
            if is_ready(_work(), pid):
                st.set(state="ready", msg="已就绪")
            else:
                from packet import STAGE1_FILES, STAGE2_FILES, is_zip_key
                fetch = CFG.get("fetch_fn")
                store = CFG.get("store")
                if fetch is None:
                    from client.fetch import ensure_packet_files, open_store
                    store = store or open_store()
                    if not is_raw_ready(_work(), pid):
                        st.set(state="pending", msg="正在下载 clip + scan…")
                        ensure_packet_files(store, aoss_key, dest_dir, pid, which=STAGE1_FILES)
                else:
                    dest = dest_dir / "packet.zip"
                    if not dest.exists() and not is_raw_ready(_work(), pid):
                        fetch(aoss_key=aoss_key, dest=dest)
                src = dest_dir if (dest_dir / "clip.mp4").exists() else dest_dir / "packet.zip"
                st.set(state="decoding", msg="正在解码 clip…")
                prepare_packet(src, _work(), packet_id=pid, status=st, on_raw_ready=on_raw, enhance=False)
                if fetch is None and store is not None and not is_zip_key(aoss_key):
                    st.set(state="downloading_dog", msg="正在下载 DoG…")
                    from client.fetch import ensure_packet_files
                    try:
                        ensure_packet_files(store, aoss_key, dest_dir, pid, which=STAGE2_FILES)
                    except Exception as e:  # noqa: BLE001
                        log.warning("DoG 下载失败，改本机计算: %s", e)
                prepare_packet(src, _work(), packet_id=pid, status=st, enhance=True)
            with _JOB_LOCK:
                _JOBS[pid] = {"state": "ready", "msg": "就绪", "status": st}
        except Exception as e:  # noqa: BLE001
            log.exception("准备 %s 失败", pid)
            st.set(state="error", msg=f"{type(e).__name__}: {e}")
            with _JOB_LOCK:
                _JOBS[pid] = {"state": "error", "msg": str(e), "status": st}

    threading.Thread(target=run, name=f"prep-{pid}", daemon=True).start()


# ---------------------------------------------------------------- 页面


@app.get("/")
def page_home():
    return send_from_directory(app.template_folder, "manual_home.html")


@app.get("/a/<packet_id>")
def page_annotate(packet_id: str):
    return send_from_directory(app.template_folder, "workbench.html")


@app.get("/review")
def page_review_queue():
    return send_from_directory(app.template_folder, "review.html")


@app.get("/review/<int:submission_id>")
def page_review(submission_id: int):
    return send_from_directory(app.template_folder, "review.html")


@app.get("/api/whoami")
def api_whoami():
    api: ServerClient = _api()
    return jsonify({"mode": "manual", "server": api.base})


# ---------------------------------------------------------------- API（annotate.js 用的那一套）


@app.post("/api/claim")
def api_claim():
    d = request.get_json(silent=True) or {}
    worker = (
        request.headers.get("X-Annotator")
        or d.get("worker")
        or CFG.get("worker")
        or "anonymous"
    )
    kind = str(d.get("kind") or "human")
    mode = str(d.get("mode") or ("repair" if kind == "repair" else "full"))
    want = str(d.get("packet_id") or "").strip()
    if kind not in ("human", "repair"):
        return jsonify({"error": "kind 只能是 human / repair"}), 400

    # 同一 worker 未完成的会话：没指定包或就是这个包就接着做；点了别的包就放掉旧租约
    for sess_file in _work().glob("*/session.json"):
        try:
            sess = json.loads(sess_file.read_text()) if sess_file.exists() else {}
        except Exception:  # noqa: BLE001
            continue
        if sess.get("worker") != worker or sess.get("done"):
            continue
        pid = sess.get("packet_id") or sess_file.parent.name
        if want and pid != want:
            if sess.get("lease_id"):
                _api().release(sess["lease_id"], "switch_packet")
            sess["done"] = True
            _save_session(pid, sess)
            continue
        job_st = (_JOBS.get(pid) or {}).get("state")
        if is_raw_ready(_work(), pid) or job_st in ("ready", "raw_ready", "enhancing"):
            return jsonify({"packet_id": pid, "resume": True, "mode": sess.get("mode") or mode})
        if job_st != "error":
            if pid not in _JOBS:
                _start_prepare(pid, sess.get("aoss_key") or "")
            return jsonify({"packet_id": pid, "resume": False, "mode": sess.get("mode") or mode})

    try:
        lease = _api().lease(worker, kind, packet_id=want)
    except ApiError as e:
        log.warning("lease 失败: %s", e)
        if e.status == 401:
            return jsonify({
                "error": (
                    "服务端拒绝领包：写接口 token 不对。"
                    "启动时加 --token，或把 token 放到仓库根 .ball_gt_token / 环境变量 BALL_GT_API_TOKEN"
                ),
            }), 401
        return jsonify({"error": str(e)}), (e.status if 400 <= int(e.status or 0) < 600 else 502)
    if lease is None:
        return jsonify({"error": "池子里暂时没有适合的包（修补模式需要 need_human）"}), 409

    pid = lease.packet_id
    _save_session(pid, {
        "packet_id": pid,
        "lease_id": lease.lease_id,
        "kind": kind,
        "mode": mode,
        "worker": worker,
        "aoss_key": lease.aoss_key,
        "target_frames": lease.target_frames,
        "meta": lease.meta,
        "done": False,
    })
    _merge_prior(pid, lease.prior_labels, mark_ai=(kind == "repair" or mode == "repair"))
    threading.Thread(
        target=_heartbeat_loop, args=(lease.lease_id, float(CFG.get("hb_interval") or 120)),
        name=f"hb-{pid}", daemon=True,
    ).start()
    _start_prepare(pid, lease.aoss_key)
    return jsonify({"packet_id": pid, "lease_id": lease.lease_id, "mode": mode, "kind": kind})


@app.get("/api/job/<packet_id>")
def api_job(packet_id: str):
    job = _JOBS.get(packet_id) or {}
    st: Optional[PrepareStatus] = job.get("status")
    snap = st.snapshot() if st else {}
    if snap.get("state"):
        state = snap["state"]
    elif job.get("state"):
        state = job["state"]
    elif is_ready(_work(), packet_id):
        state = "ready"
    elif is_raw_ready(_work(), packet_id):
        state = "raw_ready"
    else:
        state = "pending"
    return jsonify({
        "state": state,
        "msg": snap.get("msg") or job.get("msg") or "",
        "done": snap.get("done", 0),
        "total": snap.get("total", 0),
        "raw_ready": is_raw_ready(_work(), packet_id),
        "enh_ready": is_ready(_work(), packet_id),
    })


@app.get("/api/meta/<packet_id>")
def api_meta(packet_id: str):
    if not is_raw_ready(_work(), packet_id):
        return jsonify({"error": "包还没解码完原图，请稍等"}), 409
    prep = load_prepared(_work(), packet_id)
    sess = _load_session(packet_id)
    meta = prep.meta
    labels = _labels_dict(packet_id)
    mode = sess.get("mode") or "full"
    targets = list(meta.targets)
    if mode == "repair" or sess.get("kind") == "repair":
        tf = sess.get("target_frames") or []
        if tf:
            targets = [int(x) for x in tf]
        mode = "repair"
    return jsonify({
        "packet_id": packet_id,
        "group": meta.group,
        "stem": meta.stem,
        "tag": meta.tag,
        "sampling": meta.sampling,
        "produced_at": meta.produced_at,
        "fps": meta.fps,
        "pano_size": list(meta.pano_size),
        "roi": list(meta.roi),
        "step": meta.step,
        "frames": list(prep.frames),
        "targets": targets,
        "target_frames": sess.get("target_frames") or targets,
        "labels": labels,
        "mode": mode,
        "kind": sess.get("kind") or "human",
        "n_seed_need": MIN_SEED,
        "aoss_key": sess.get("aoss_key") or "",
        "decim": getattr(meta, "decim", 1),
        "clip_fps": getattr(meta, "clip_fps", 30),
        "has_scan": (prep.root / "scan_time.jpg").exists(),
    })


@app.get("/img/<packet_id>/<view>/<int:fi>.jpg")
def api_img(packet_id: str, view: str, fi: int):
    if view not in ("raw", "enh"):
        return jsonify({"error": "view 只能是 raw / enh"}), 400
    prep = load_prepared(_work(), packet_id)
    path = prep.raw_path(fi) if view == "raw" else prep.enh_path(fi)
    if not path.exists():
        return jsonify({"error": f"没有帧 {fi}"}), 404
    return send_file(path, mimetype="image/jpeg", max_age=86400)


@app.get("/img/<packet_id>/scan_<kind>.jpg")
def api_scan(packet_id: str, kind: str):
    if kind not in ("time", "dog"):
        return jsonify({"error": "scan 只能是 time / dog"}), 400
    d = workspace(_work(), packet_id)
    path = d / f"scan_{kind}.jpg"
    if not path.exists():
        return jsonify({"error": "还没有 scan 图"}), 404
    return send_file(path, mimetype="image/jpeg", max_age=86400)


@app.post("/api/label_batch/<packet_id>")
def api_label_batch(packet_id: str):
    """本段清除 / 本段不可见。frames 是 frame_idx 列表。"""
    d = request.get_json(silent=True) or {}
    action = str(d.get("action") or "clear")
    if action not in ("clear", "invisible"):
        return jsonify({"error": "action 只能是 clear / invisible"}), 400
    raw = d.get("frames") or []
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "缺少 frames"}), 400
    labels = _labels_dict(packet_id)
    n = 0
    for x in raw:
        try:
            fi = int(x)
        except (TypeError, ValueError):
            continue
        key = str(fi)
        if action == "clear":
            if key in labels:
                labels.pop(key, None)
                n += 1
        else:
            labels[key] = {
                "frame_idx": fi, "visible": False,
                "pano_x": None, "pano_y": None,
                "occluded": False, "blurred": False,
            }
            n += 1
    _write_labels(packet_id, labels)
    return jsonify({"ok": True, "n": n, "action": action})


@app.post("/api/abandon/<packet_id>")
def api_abandon_local(packet_id: str):
    sess = _load_session(packet_id)
    d = request.get_json(silent=True) or {}
    worker = (
        request.headers.get("X-Annotator")
        or d.get("worker")
        or sess.get("worker")
        or CFG.get("worker")
        or "anonymous"
    )
    reason = str(d.get("reason") or "bad_quality")
    try:
        resp = _api().abandon(packet_id, reason=reason, worker=worker)
    except ApiError as e:
        return jsonify({"error": str(e)}), (e.status if 400 <= int(e.status or 0) < 600 else 502)
    sess["done"] = True
    _save_session(packet_id, sess)
    return jsonify({
        "ok": True,
        "message": "已抛弃本包（数据质量不行），已从审核和修复队列移除",
        **(resp if isinstance(resp, dict) else {}),
    })


@app.post("/api/label/<packet_id>")
def api_label(packet_id: str):
    d = request.get_json(silent=True) or {}
    labels = _labels_dict(packet_id)
    fi = d.get("frame_idx")
    if fi is None:
        return jsonify({"error": "缺少 frame_idx"}), 400
    key = str(int(fi))
    if d.get("clear"):
        labels.pop(key, None)
        _write_labels(packet_id, labels)
        return jsonify({"ok": True, "record": None})
    rec = {
        "frame_idx": int(fi),
        "visible": bool(d.get("visible")),
        "pano_x": d.get("pano_x"),
        "pano_y": d.get("pano_y"),
        "occluded": bool(d.get("occluded")),
        "blurred": bool(d.get("blurred")),
    }
    labels[key] = rec
    _write_labels(packet_id, labels)
    return jsonify({"ok": True, "record": rec})


@app.post("/api/finish/<packet_id>")
def api_finish(packet_id: str):
    d = request.get_json(silent=True) or {}
    sess = _load_session(packet_id)
    worker = (
        request.headers.get("X-Annotator")
        or d.get("annotator")
        or sess.get("worker")
        or CFG.get("worker")
        or "anonymous"
    )
    seed_only = bool(d.get("seed_only") or sess.get("mode") == "seed")
    labels = _labels_dict(packet_id)
    vis = [r for r in labels.values() if r.get("visible") and r.get("pano_x") is not None]
    if seed_only:
        if not is_raw_ready(_work(), packet_id):
            return jsonify({"error": "包还没解码完原图"}), 409
        prep0 = load_prepared(_work(), packet_id)
        want0 = _want_frames(sess, prep0.meta)
        err = seed_submit_error(labels, want0, MIN_SEED)
        if err:
            return jsonify({"error": err}), 400
        if not all_explicit_invisible(labels, want0):
            try:
                resp = _api().save_seed(
                    packet_id, list(labels.values()),
                    worker=worker, lease_id=sess.get("lease_id"),
                )
            except ApiError as e:
                return jsonify({"error": str(e)}), (e.status if 400 <= int(e.status or 0) < 600 else 502)
            sess["done"] = True
            sess["mode"] = "seed"
            _save_session(packet_id, sess)
            return jsonify({
                "ok": True,
                "seed_only": True,
                "n_visible": len(vis),
                "message": (resp or {}).get("message") or f"已保存预标（{len(vis)} 个可见点），包仍在待标注列表",
            })
        # 整段不可见：没有球就是最终答案，按人工提交入库，不留给 AI 冷启动。
        no_ball = True
    else:
        no_ball = False

    if not is_raw_ready(_work(), packet_id):
        return jsonify({"error": "包还没解码完原图"}), 409
    if not is_ready(_work(), packet_id) and not d.get("skip_bundle"):
        dest = workspace(_work(), packet_id)
        src = dest if (dest / "clip.mp4").exists() else dest / "packet.zip"
        prepare_packet(src, _work(), packet_id=packet_id)
    prep = load_prepared(_work(), packet_id)
    meta = prep.meta
    want = _want_frames(sess, meta)
    err = validate_sparse_anchors(labels, want)
    if err and not d.get("force"):
        return jsonify({"error": err}), 400
    labels, filled, want = _fill_for_submit(sess, prep, labels)
    _write_labels(packet_id, labels)
    missing = [t for t in want if str(t) not in labels]
    if missing and not d.get("force"):
        return jsonify({
            "error": f"还有 {len(missing)} 帧无法插值（空洞太大），请再标几个锚点",
            "missing": missing[:20],
            "n_interp": len(filled),
        }), 400

    result = build_result(meta, worker, labels, {"elapsed_sec": None})
    result["source"] = "human"
    resp = _api().submit(
        result, None,
        source="human",
        lease_id=sess.get("lease_id"),
        worker=worker,
    )
    sess["done"] = True
    _save_session(packet_id, sess)
    sid = (resp or {}).get("submission_id")
    return jsonify({
        "ok": True,
        "n_labeled": len(labels),
        "submission_id": sid,
        "n_interp": len(filled),
        "has_bundle": False,
        "review_url": f"/review/{sid}" if sid else None,
        "message": (
            f"整段不可见，已作为无球提交入库（{len(labels)} 帧）"
            if no_ball else
            (
                f"已提交（{len(labels)} 帧，其中插值 {len(filled)}），免审入库"
            )
        ),
        "seed_only": False,
        "no_ball": bool(no_ball),
    })


@app.get("/api/preview_interp/<packet_id>")
def api_preview_interp(packet_id: str):
    """不落盘：把头尾锚点插成全段，给工作台全图画轨迹。"""
    if not is_raw_ready(_work(), packet_id):
        return jsonify({"error": "包还没解码完原图"}), 409
    sess = _load_session(packet_id)
    if sess.get("mode") == "seed":
        return jsonify({"ok": True, "filled": [], "n_filled": 0})
    prep = load_prepared(_work(), packet_id)
    labels = _labels_dict(packet_id)
    want = _want_frames(sess, prep.meta)
    if validate_sparse_anchors(labels, want):
        return jsonify({"ok": True, "filled": [], "n_filled": 0})
    _out, filled_ids, _want = _fill_for_submit(sess, prep, labels)
    recs = []
    for fi in filled_ids:
        rec = _out.get(str(fi))
        if rec:
            recs.append(rec)
    return jsonify({"ok": True, "filled": recs, "n_filled": len(recs)})


# 旧路径兼容：annotate.js 改完后不再走，留着以免有人握着旧缓存
@app.post("/api/build_result/<packet_id>")
def api_build_compat(packet_id: str):
    return api_finish(packet_id)


# ---------------------------------------------------------------- 公网站只读代理 + 再下载


def _proxy_get(path: str):
    try:
        data = _api().get(path, **{k: v for k, v in request.args.items()})
        return jsonify(data)
    except ApiError as e:
        return jsonify({"error": str(e)}), (e.status if 400 <= int(e.status or 0) < 600 else 502)


@app.get("/api/v2/packets")
def proxy_packets():
    return _proxy_get("/api/v2/packets")


@app.get("/api/v2/submissions")
def proxy_submissions():
    return _proxy_get("/api/v2/submissions")


@app.get("/api/v2/submission/<int:submission_id>")
def proxy_submission(submission_id: int):
    return _proxy_get(f"/api/v2/submission/{submission_id}")


@app.post("/api/v2/review/<int:submission_id>")
def proxy_review(submission_id: int):
    try:
        data = _api().post(f"/api/v2/review/{submission_id}", request.get_json(silent=True) or {})
        return jsonify(data)
    except ApiError as e:
        return jsonify({"error": str(e)}), (e.status if 400 <= int(e.status or 0) < 600 else 502)


@app.post("/api/v2/abandon")
def proxy_abandon():
    try:
        data = _api().post("/api/v2/abandon", request.get_json(silent=True) or {})
        return jsonify(data)
    except ApiError as e:
        return jsonify({"error": str(e)}), (e.status if 400 <= int(e.status or 0) < 600 else 502)


@app.post("/api/v2/requeue")
def proxy_requeue():
    try:
        data = _api().post("/api/v2/requeue", request.get_json(silent=True) or {})
        return jsonify(data)
    except ApiError as e:
        return jsonify({"error": str(e)}), (e.status if 400 <= int(e.status or 0) < 600 else 502)


@app.get("/thumb/<packet_id>/<path:name>")
def proxy_thumb(packet_id: str, name: str):
    try:
        _status, body = _api().request("GET", f"/thumb/{packet_id}/{name}")
    except ApiError as e:
        return jsonify({"error": str(e)}), (e.status if 400 <= int(e.status or 0) < 600 else 502)
    mt = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if isinstance(body, dict):
        return jsonify(body)
    return Response(body, mimetype=mt, headers={"Cache-Control": "public, max-age=604800"})


@app.get("/bundle/<int:submission_id>/<path:name>")
def proxy_bundle(submission_id: int, name: str):
    try:
        _status, body = _api().request("GET", f"/bundle/{submission_id}/{name}")
    except ApiError as e:
        return jsonify({"error": str(e)}), (e.status if 400 <= int(e.status or 0) < 600 else 502)
    mt = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if name.endswith(".webp"):
        mt = "image/webp"
    if isinstance(body, dict):
        return jsonify(body)
    return Response(body, mimetype=mt, headers={"Cache-Control": "public, max-age=604800"})


@app.post("/api/prepare/<packet_id>")
def api_prepare(packet_id: str):
    """工作台「下载到本地以便修正」：有 session 就用它的 aoss_key，否则定向领一次。"""
    sess = _load_session(packet_id)
    key = sess.get("aoss_key") or ""
    if not key:
        d = request.get_json(silent=True) or {}
        worker = (
            request.headers.get("X-Annotator")
            or d.get("worker")
            or CFG.get("worker")
            or "anonymous"
        )
        try:
            lease = _api().lease(worker, "repair" if sess.get("kind") == "repair" else "human",
                                 packet_id=packet_id)
        except ApiError as e:
            return jsonify({"error": str(e)}), (e.status if 400 <= int(e.status or 0) < 600 else 502)
        if lease is None:
            return jsonify({"error": "这个包现在不能领（可能已被占用）"}), 409
        key = lease.aoss_key
        _save_session(packet_id, {
            "packet_id": packet_id,
            "lease_id": lease.lease_id,
            "kind": lease.kind,
            "mode": sess.get("mode") or ("repair" if lease.kind == "repair" else "full"),
            "worker": worker,
            "aoss_key": key,
            "target_frames": lease.target_frames,
            "meta": lease.meta,
            "done": False,
        })
        _merge_prior(packet_id, lease.prior_labels, mark_ai=True)
    if packet_id not in _JOBS and not is_ready(_work(), packet_id):
        _start_prepare(packet_id, key)
    return jsonify({"ok": True, "packet_id": packet_id})


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="本地人工标注站")
    p.add_argument("--server", required=True, help="中心服务端 URL")
    p.add_argument("--worker", default=os.environ.get("BALL_GT_ANNOTATOR") or "")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8780)
    p.add_argument("--work", type=Path, default=DEFAULT_WORK)
    p.add_argument("--token", default="")
    p.add_argument("--hb-interval", type=float, default=120.0)
    return p.parse_args(argv)


def init(server: str, work: Path, worker: str = "", token: str = "",
         hb_interval: float = 120.0, api: Optional[ServerClient] = None) -> None:
    try:
        from client.vlm import load_dotenv
        load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    CFG["work_root"] = Path(work)
    CFG["work_root"].mkdir(parents=True, exist_ok=True)
    CFG["worker"] = worker
    CFG["hb_interval"] = hb_interval
    CFG["api"] = api or ServerClient(server, token=token, worker=worker)


def main(argv=None) -> int:
    a = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init(a.server, a.work, a.worker, a.token, a.hb_interval)
    print(f"[manual] http://{a.host}:{a.port}/   server={a.server}  work={a.work}", flush=True)
    app.run(host=a.host, port=a.port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
