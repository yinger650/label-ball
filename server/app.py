#!/usr/bin/env python3
"""足球 GT 标注系统服务端 v2（公网，只管调度与审核）。

和 v1 的区别是权威搬了家：v1 的任务状态由 AOSS 上对象的存在与否表达、服务端只做
解码+DoG；v2 的权威是 `server/db.py` 里的 SQLite，服务端只做四件事：

    发包（lease） · 收包（submit） · 审核（review） · 看板与采集端监控

**这个进程不持有 AOSS / ACP 凭证**，也不下载 clip / dog。重活（下载、解码、VLM）
在能 GetObject 的客户端本地做；审核用的缩略件由产包端 register 时 POST 落盘，
因此公网站不再需要 zip 镜像（`sync_packets.py` 废弃）。

人工标注 UI 由 `client/manual.py` 在标注员本机起——那里才能直连 AOSS 拿全清对象。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request, send_file, send_from_directory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import db  # noqa: E402
from collect_results import qc  # noqa: E402  沿用已有质检口径，别重写一份

app = Flask(__name__, static_folder=str(HERE / "static"), template_folder=str(HERE / "templates"))

CFG: Dict[str, Any] = {}

MAX_THUMB_BYTES = 80 * 1024 * 1024
THUMB_SUFFIXES = {".mp4", ".jpg", ".jpeg", ".json", ".webp"}
THUMB_FIELDS = {
    "thumb_clip": "clip.mp4",
    "thumb_dog": "dog.mp4",
    "thumb_scan_dog": "scan_dog.jpg",
    "thumb_scan_time": "scan_time.jpg",
}
SAFE_ID = re.compile(r"^[A-Za-z0-9._@\-]+$")


# ---------------------------------------------------------------- 基础设施


def conn():
    return db.conn(CFG["db_path"])


def thumb_root() -> Path:
    return CFG["thumb_root"]  # type: ignore[return-value]


def bundle_root() -> Path:
    """兼容旧配置名：缩略件目录。"""
    return thumb_root()


def body() -> Dict[str, Any]:
    """取请求正文。multipart 的 submit 也要能读到这些字段，所以三处都看。"""
    d: Dict[str, Any] = {}
    j = request.get_json(silent=True)
    if isinstance(j, dict):
        d.update(j)
    if request.form:
        d.update({k: v for k, v in request.form.items() if k not in d})
    for k, v in request.args.items():
        d.setdefault(k, v)
    return d


def worker_of(d: Optional[Dict[str, Any]] = None) -> str:
    d = d if d is not None else body()
    return str(
        request.headers.get("X-Worker")
        or request.headers.get("X-Annotator")
        or d.get("worker")
        or d.get("annotator")
        or "anonymous"
    )


def bad(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


@app.before_request
def _auth():
    """可选的共享 token。

    计划里没写鉴权，但这台机器在公网上，任何人都能 POST 假提交把 gt 表污染掉。
    所以给一个默认关闭的开关：设了 BALL_GT_API_TOKEN 就校验写接口，没设照旧全开
    （内网自测、开发机跑脚本时不用配）。读接口和看板一律不校验。
    """
    token = CFG.get("api_token")
    if not token or request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if not request.path.startswith("/api/"):
        return None
    # 公网审核页是浏览器直接 POST，审核员没有 API token。token 只挡
    # 机器写入口（lease/submit/register），避免匿名污染 gt。
    if request.path.startswith("/api/v2/review/") or request.path in (
        "/api/v2/abandon", "/api/v2/requeue",
    ):
        return None
    got = request.headers.get("X-Api-Token") or request.args.get("token") or (request.form.get("token") or "")
    if got != token:
        return bad("token 不对（X-Api-Token）", 401)
    return None


# ---------------------------------------------------------------- 页面


@app.get("/")
def page_index():
    return send_from_directory(app.template_folder, "index.html")


@app.get("/review")
def page_review_queue():
    return send_from_directory(app.template_folder, "review.html")


@app.get("/review/<int:submission_id>")
def page_review(submission_id: int):
    return send_from_directory(app.template_folder, "review.html")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "ts": time.time()})


@app.get("/api/whoami")
def api_whoami():
    return jsonify({
        "mode": "server_v2",
        "db": str(CFG["db_path"]),
        "thumb_root": str(thumb_root()),
        "bundle_root": str(thumb_root()),
        "keep_available": CFG["keep_available"],
        "auth": bool(CFG.get("api_token")),
    })


# ---------------------------------------------------------------- 注册


def _store_thumbs(pid: str, files) -> int:
    """落盘产包端 POST 上来的缩略件。返回写入文件数。"""
    d = thumb_root() / pid
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    total = 0
    for field, name in THUMB_FIELDS.items():
        f = files.get(field)
        if not f or not getattr(f, "filename", None):
            continue
        if Path(name).suffix.lower() not in THUMB_SUFFIXES:
            continue
        dest = d / name
        tmp = dest.with_suffix(dest.suffix + ".part")
        size = 0
        with open(tmp, "wb") as out:
            while True:
                chunk = f.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                total += len(chunk)
                if total > MAX_THUMB_BYTES:
                    tmp.unlink(missing_ok=True)
                    raise ValueError(f"缩略件超过 {MAX_THUMB_BYTES // (1024*1024)} MB")
                out.write(chunk)
        if size:
            tmp.replace(dest)
            n += 1
        else:
            tmp.unlink(missing_ok=True)
    return n


def _register_one(c, it: dict, thumb_n: int = 0) -> str:
    pid = str(it.get("packet_id") or "")
    meta = it.get("meta") or {}
    if isinstance(meta, str):
        meta = db.jload(meta, {}) or {}
    if not pid or not SAFE_ID.match(pid):
        raise ValueError("packet_id 缺失或非法")
    if not isinstance(meta, dict) or not meta.get("targets"):
        raise ValueError("meta 缺失或没有 targets")
    aoss_key = it.get("aoss_key") or f"packets/{pid}/"
    return db.register_packet(
        c, pid, aoss_key, meta,
        n_targets=it.get("n_targets"),
        size_bytes=it.get("size_bytes"),
        producer_job_id=it.get("producer_job_id"),
        producer_node=it.get("producer_node"),
        priority=int(it.get("priority") or 0),
        thumb_ready=1 if thumb_n else None,
    )


@app.post("/api/v2/register")
def api_register():
    """产包端注册包（幂等，按 packet_id）。JSON 批量或 multipart（带缩略件）。"""
    c = conn()
    out = {"registered": 0, "updated": 0, "errors": []}
    if request.files:
        d = body()
        meta = d.get("meta")
        if isinstance(meta, str):
            meta = db.jload(meta, {}) or {}
        it = {**d, "meta": meta}
        pid = str(it.get("packet_id") or (meta or {}).get("packet_id") or "")
        it["packet_id"] = pid
        try:
            n = _store_thumbs(pid, request.files) if pid else 0
            what = _register_one(c, it, thumb_n=n)
            out[what] += 1
            out["thumb_files"] = n
            if it.get("producer_job_id"):
                c.execute(
                    "UPDATE producers SET n_produced=n_produced+1, updated_at=? WHERE job_id=?",
                    (db.now(), it["producer_job_id"]),
                )
                c.commit()
        except Exception as e:  # noqa: BLE001
            out["errors"].append({"packet_id": pid, "error": str(e)})
    else:
        d = request.get_json(silent=True) or {}
        items = d.get("packets") if isinstance(d.get("packets"), list) else [d]
        for it in items:
            try:
                what = _register_one(c, it)
            except Exception as e:  # noqa: BLE001
                out["errors"].append({"packet_id": str(it.get("packet_id") or ""), "error": str(e)})
                continue
            out[what] += 1
            if it.get("producer_job_id"):
                c.execute(
                    "UPDATE producers SET n_produced=n_produced+1, updated_at=? WHERE job_id=?",
                    (db.now(), it["producer_job_id"]),
                )
                c.commit()
    out["ok"] = not out["errors"]
    return jsonify(out), (200 if out["ok"] else 207)


@app.post("/api/v2/seed")
def api_seed():
    """保存人工预标种子。不建 submission、不写 gt，包保持 available。

    AI 领包时会把 seed 并进 prior_labels，并优先选有种子的包。
    """
    d = body()
    packet_id = str(d.get("packet_id") or "")
    if not packet_id or not SAFE_ID.match(packet_id):
        return bad("缺少 packet_id")
    raw = d.get("labels")
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list) or not raw:
        return bad("缺少 labels")
    labels, n_degraded = _labels_of({"labels": raw})
    vis = [r for r in labels if r.get("visible") and r.get("pano_x") is not None]
    worker = worker_of(d)
    lease_id = d.get("lease_id")
    c = conn()
    p = db.one(c, "SELECT * FROM packets WHERE packet_id=?", (packet_id,))
    if not p:
        return bad(f"未注册的包 {packet_id}", 404)
    if p["state"] not in ("available", "leased", "need_human"):
        return bad(f"这个状态下不能写预标（当前是 {p['state']}）")
    meta = db.jload(p["meta_json"], {}) or {}
    targets = [int(x) for x in (meta.get("targets") or [])]
    by_fi = {int(r["frame_idx"]): r for r in labels if r.get("frame_idx") is not None}
    all_invis = bool(targets) and all(
        (by_fi.get(t) or {}).get("visible") is False for t in targets
    )
    if len(vis) < 2 and not all_invis:
        return bad(
            f"预标至少要 2 个可见点，或把整段标成不可见后再提交。现在可见 {len(vis)}"
        )
    if lease_id:
        lease = db.one(c, "SELECT * FROM leases WHERE lease_id=?", (lease_id,))
        if not lease or lease["packet_id"] != packet_id:
            return bad("lease 与 packet 对不上", 409)
        db.release_lease(c, str(lease_id), "seed_ready")
    t = db.now()
    c.execute(
        "UPDATE packets SET seed_json=?, seed_worker=?, seed_at=?, updated_at=? WHERE packet_id=?",
        (db.jdump(labels), worker, t, t, packet_id),
    )
    c.commit()
    return jsonify({
        "ok": True,
        "packet_id": packet_id,
        "n_seed": len(labels),
        "n_visible": len(vis),
        "n_degraded": n_degraded,
        "message": (
            f"整段不可见，已记下预标（{len(labels)} 帧）"
            if all_invis else
            f"已保存预标（{len(vis)} 个可见点），包仍在待标注列表，AI 会优先领"
        ),
    })


@app.post("/api/v2/requeue")
def api_requeue():
    """待修复包打回待标注池。可一次多个。只收 `need_human`。"""
    d = body()
    ids = d.get("packet_ids") or d.get("packet_id")
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, list) or not ids:
        return bad("缺少 packet_ids")
    reason = str(d.get("reason") or "requeue_label")
    out = db.requeue_packets(conn(), [str(x) for x in ids], reason=reason)
    out["ok"] = bool(out["moved"])
    return jsonify(out)


@app.post("/api/v2/archive")
def api_archive():
    """把无效 / 测试包移出领取池。只收 available / need_human。"""
    d = body()
    ids = d.get("packet_ids") or d.get("packet_id")
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, list) or not ids:
        return bad("缺少 packet_ids")
    reason = str(d.get("reason") or "archived")
    out = db.archive_packets(conn(), [str(x) for x in ids], reason=reason)
    out["ok"] = bool(out["archived"]) and not out["skipped"]
    return jsonify(out)


@app.post("/api/v2/abandon")
def api_abandon():
    """数据质量不行：归档并移出审核 / 修复队列。没有球请标不可见，不要走这里。"""
    d = body()
    packet_id = str(d.get("packet_id") or "")
    if not packet_id or not SAFE_ID.match(packet_id):
        return bad("缺少 packet_id")
    reason = str(d.get("reason") or "bad_quality")
    worker = worker_of(d)
    out = db.abandon_packet(conn(), packet_id, reason=reason, worker=worker)
    if not out.get("ok"):
        return bad(out.get("error") or "抛弃失败", 404)
    return jsonify(out)


# ---------------------------------------------------------------- 领包


@app.post("/api/v2/lease")
def api_lease():
    """领一个包。

    `kind=repair` 时只返回被打回的帧号，并把 AI 的答案作为初值一起给出去——
    人工只修那几帧，这是整套系统的价值所在。
    """
    d = body()
    kind = str(d.get("kind") or "ai")
    if kind not in db.LEASE_KINDS:
        return bad(f"kind 只能是 {'/'.join(db.LEASE_KINDS)}")
    worker = worker_of(d)
    want = str(d.get("packet_id") or "").strip() or None
    c = conn()
    db.expire_leases(c)

    p = db.pick_packet(c, worker, kind, want_packet=want)
    if not p:
        cnt = db.pool_counts(c)
        return jsonify({
            "lease": None,
            "reason": (
                f"指定的包 {want} 现在不能领（不存在或状态不对）"
                if want else "池子里暂时没有适合该 worker 的包"
            ),
            "n_available": cnt["available"],
            "n_need_human": cnt["need_human"],
        })

    meta = db.jload(p["meta_json"], {}) or {}
    targets = [int(x) for x in (meta.get("targets") or [])]
    prior: List[dict] = []
    if kind == "repair":
        need = db.jload(p["need_frames_json"], []) or []
        targets = [int(x) for x in need] or targets
        last = db.one(
            c,
            "SELECT submission_id, labels_json FROM submissions WHERE packet_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (p["packet_id"],),
        )
        if last:
            keep = set(targets)
            prior = [r for r in (db.jload(last["labels_json"], []) or [])
                     if int(r.get("frame_idx", -1)) in keep]
    seed = db.jload(p.get("seed_json"), []) or []
    if isinstance(seed, dict):
        seed = list(seed.values())
    have = {int(r.get("frame_idx", -1)) for r in prior}
    for r in seed:
        if not isinstance(r, dict) or r.get("frame_idx") is None:
            continue
        fi = int(r["frame_idx"])
        if fi not in have:
            prior.append(r)
            have.add(fi)

    lease = db.open_lease(c, p, worker, kind)
    return jsonify({"lease": {
        **lease,
        "packet_id": p["packet_id"],
        "aoss_key": p["aoss_key"],
        "kind": kind,
        "meta": meta,
        "target_frames": targets,
        "n_targets": len(targets),
        "prior_labels": prior,
    }})


@app.post("/api/v2/heartbeat/<lease_id>")
def api_heartbeat(lease_id: str):
    """续期。AI 租约 30 分钟，跑长包要靠这个续，不然会被 expire_leases 收回。"""
    d = body()
    c = conn()
    r = db.one(c, "SELECT * FROM leases WHERE lease_id=?", (lease_id,))
    if not r:
        return bad("没有这个 lease", 404)
    if r["released_at"] is not None:
        return bad("该 lease 已释放，请重新 lease", 409)
    t = db.now()
    exp = t + db.lease_ttl(r["kind"])
    c.execute(
        "UPDATE leases SET expires_at=?, last_heartbeat=?, stage=COALESCE(?, stage) WHERE lease_id=?",
        (exp, t, d.get("stage"), lease_id),
    )
    c.commit()
    return jsonify({"ok": True, "expires_at": exp, "packet_id": r["packet_id"]})


@app.post("/api/v2/release/<lease_id>")
def api_release(lease_id: str):
    d = body()
    ok = db.release_lease(conn(), lease_id, str(d.get("reason") or "released"))
    return jsonify({"ok": ok}) if ok else bad("lease 不存在或已释放", 404)


# ---------------------------------------------------------------- 提交


def _labels_of(result: dict) -> Tuple[List[dict], int]:
    """整理提交上来的 labels：按帧号去重排序，返回 (labels, 被降级的行数)。

    这里是**唯一**一道拦「visible=true 但没有坐标」的关口，必须拦住：`qc()` 对可见帧
    两两算帧间位移，坐标是 None 就直接 TypeError，提交接口 500；就算绕过 QC 写进了
    `gt`，`scripts/export_gt.py` 之后也会在同一处炸掉，而那时候已经查不出是哪个客户端
    塞进来的了。所以在入口降级成不可见，并把条数回给客户端，让它自己知道有帧没标好。

    排序也在这里做：`qc()` 靠相邻两行算速度，顺序乱了整个速度统计就是错的，而客户端
    并发跑工作流、labels 的顺序并不保证。
    """
    rows = result.get("labels")
    if isinstance(rows, dict):  # 人工标注工具的工作副本是 {frame_idx: rec}
        rows = [rows[k] for k in sorted(rows, key=lambda s: int(s))]
    by_frame: Dict[int, dict] = {}
    n_degraded = 0
    for r in rows or []:
        if not isinstance(r, dict) or r.get("frame_idx") is None:
            continue
        row = dict(r)
        try:
            row["frame_idx"] = int(r["frame_idx"])
        except (TypeError, ValueError):
            continue
        if row.get("visible") and (row.get("pano_x") is None or row.get("pano_y") is None):
            row["visible"] = False
            n_degraded += 1
        by_frame[row["frame_idx"]] = row
    return [by_frame[k] for k in sorted(by_frame)], n_degraded


@app.post("/api/v2/submit")
def api_submit():
    """收一次提交。

    multipart：`result`（result.json）。人工直接发 JSON 正文也行。审核画面走产包时
    上传的缩略件，不再收 sprite zip。

    `source=human` 直接写 `gt`（免审，人工就是权威）；`source=ai` 进 `submissions`
    等审核。QC 在这里算好存下来，审核页按 QC 标记排序，可疑的先审。
    """
    d = body()
    result: Optional[dict] = None
    if "result" in request.files:
        try:
            result = json.loads(request.files["result"].read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            return bad(f"result.json 解析失败：{e}")
    elif isinstance(d.get("result"), dict):
        result = d["result"]
    elif isinstance(d.get("result"), str):
        result = db.jload(d["result"])
    if not isinstance(result, dict):
        return bad("缺少 result（multipart 文件字段或 JSON 正文的 result）")

    c = conn()
    packet_id = str(d.get("packet_id") or result.get("packet_id") or "")
    lease_id = d.get("lease_id")
    lease = db.one(c, "SELECT * FROM leases WHERE lease_id=?", (lease_id,)) if lease_id else None
    if lease and not packet_id:
        packet_id = lease["packet_id"]
    if not packet_id:
        return bad("缺少 packet_id")
    p = db.one(c, "SELECT * FROM packets WHERE packet_id=?", (packet_id,))
    if not p:
        return bad(f"未注册的包 {packet_id}", 404)

    source = str(d.get("source") or result.get("source") or ("human" if not d.get("workflow") else "ai"))
    if source not in ("ai", "human"):
        return bad("source 只能是 ai / human")
    worker = worker_of(d) if worker_of(d) != "anonymous" else str(result.get("annotator") or "anonymous")
    labels, n_degraded = _labels_of(result)
    if not labels:
        return bad("result 里没有任何 labels")

    meta = db.jload(p["meta_json"], {}) or {}
    n_targets = int(p["n_targets"] or len(meta.get("targets") or []) or len(labels))
    vis = [r for r in labels if r.get("visible")]
    confs = [float(r["conf"]) for r in labels if r.get("conf") is not None]
    # qc() 要的是 collect_results 那份 result 文档形态，补齐它依赖的字段再算
    report = qc({**result, "labels": labels, "n_targets": n_targets},
                float(result.get("fps") or meta.get("fps") or 30.0))

    t = db.now()
    state = "accepted" if source == "human" else "pending"
    cur = c.execute(
        "INSERT INTO submissions(packet_id, lease_id, worker, source, workflow, model_used, "
        "labels_json, n_targets, n_labeled, n_visible, n_interp, mean_conf, qc_json, state, "
        "elapsed_sec, n_vlm_calls, created_at, reviewed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            packet_id, lease_id, worker, source,
            d.get("workflow") or result.get("workflow"),
            d.get("model_used") or result.get("model_used"),
            db.jdump(labels), n_targets, len(labels), len(vis),
            sum(1 for r in labels if r.get("interp")),
            round(sum(confs) / len(confs), 4) if confs else None,
            db.jdump(report), state,
            result.get("elapsed_sec"), result.get("n_vlm_calls"),
            t, t if source == "human" else None,
        ),
    )
    sid = int(cur.lastrowid)
    c.execute("UPDATE packets SET labeled_at=?, updated_at=? WHERE packet_id=?", (t, t, packet_id))
    c.commit()

    n_gt = 0
    if source == "human":
        # 修补模式只重标了被打回的帧，其余 AI 帧要留着，所以 write_gt 是 upsert
        n_gt = db.write_gt(c, packet_id, labels, source="human", worker=worker, submission_id=sid)
        need_left = _remaining_need_frames(c, p, labels)
        c.execute(
            "UPDATE submissions SET n_accepted=? WHERE submission_id=?", (n_gt, sid),
        )
        _set_packet_state(c, packet_id, "need_human" if need_left else "accepted", need_left)
    else:
        _set_packet_state(c, packet_id, "submitted", None)

    if lease_id:
        db.release_lease(c, str(lease_id), "submitted")
        # release 会把包拨回 prev_state，提交的状态才是对的，拨回去要改正
        _set_packet_state(
            c, packet_id,
            "submitted" if source == "ai" else ("need_human" if _need_frames(c, packet_id) else "accepted"),
            _need_frames(c, packet_id),
        )

    return jsonify({
        "ok": True,
        "submission_id": sid,
        "state": state,
        "n_labeled": len(labels),
        "n_degraded": n_degraded,
        "n_gt_written": n_gt,
        "qc": report,
        "thumb": {"base": f"/thumb/{packet_id}", "ready": bool(p.get("thumb_ready"))},
    })


def _need_frames(c, packet_id: str) -> Optional[List[int]]:
    r = db.one(c, "SELECT need_frames_json FROM packets WHERE packet_id=?", (packet_id,))
    v = db.jload(r["need_frames_json"], None) if r else None
    return [int(x) for x in v] if v else None


def _remaining_need_frames(c, p: Dict[str, Any], labels: List[dict]) -> Optional[List[int]]:
    """人工提交后还剩哪些打回帧没修。修完了包才算 accepted。"""
    need = db.jload(p["need_frames_json"], None)
    if not need:
        return None
    done = {int(r["frame_idx"]) for r in labels}
    left = [int(f) for f in need if int(f) not in done]
    return left or None


def _set_packet_state(c, packet_id: str, state: str, need_frames: Optional[List[int]]) -> None:
    t = db.now()
    extra = ""
    args: list = [state, db.jdump(need_frames) if need_frames else None, t]
    if state in ("accepted", "need_human"):
        extra = ", reviewed_at=?"
        args.append(t)
    args.append(packet_id)
    c.execute(
        f"UPDATE packets SET state=?, need_frames_json=?, updated_at=?{extra} WHERE packet_id=?",
        args,
    )
    c.commit()


# ---------------------------------------------------------------- 审核


@app.post("/api/v2/review/<int:submission_id>")
def api_review(submission_id: int):
    """审核一次提交（AI 待审，或已入库的人工 / agent / 已通过 AI）。

    `decision`：`accept` 整包通过 / `reject` 整包打回 / `partial` 逐帧。
    `frames`：`{"<frame_idx>": true|false}`，只在 partial 下生效，没提到的帧算通过。

    通过的帧写进 `gt`（这就是入库），打回的帧从 `gt` 删掉，并写进
    `packets.need_frames_json`，包置为 `need_human`，人工用 `kind=repair` 领回去只修这几帧。
    已入库再审打回时，原先免审写进去的行也会被删——否则「再审」只是记一条 reviews，GT 不动。
    """
    d = body()
    decision = str(d.get("decision") or "")
    if decision not in ("accept", "reject", "partial"):
        return bad("decision 只能是 accept / reject / partial")
    reviewer = str(d.get("reviewer") or worker_of(d))

    c = conn()
    s = db.one(c, "SELECT * FROM submissions WHERE submission_id=?", (submission_id,))
    if not s:
        return bad("没有这个 submission", 404)

    labels = db.jload(s["labels_json"], []) or []
    per_frame: Dict[str, Any] = d.get("frames") if isinstance(d.get("frames"), dict) else {}
    all_frames = [int(r["frame_idx"]) for r in labels if r.get("frame_idx") is not None]

    if decision == "accept":
        ok_frames, bad_frames = all_frames, []
    elif decision == "reject":
        ok_frames, bad_frames = [], all_frames
    else:
        rejected = {int(k) for k, v in per_frame.items() if not v}
        ok_frames = [f for f in all_frames if f not in rejected]
        bad_frames = [f for f in all_frames if f in rejected]
        if not bad_frames:
            decision = "accept"

    t = db.now()
    c.execute(
        "INSERT INTO reviews(submission_id, reviewer, decision, per_frame_json, notes, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (submission_id, reviewer, decision, db.jdump(per_frame) if per_frame else None,
         d.get("notes"), t),
    )
    n_removed = db.delete_gt_frames(c, s["packet_id"], bad_frames)
    src = str(s["source"] or "ai")
    n_gt = db.write_gt(
        c, s["packet_id"], labels, source=src, worker=s["worker"],
        submission_id=submission_id, only_frames=ok_frames,
    ) if ok_frames else 0
    sub_state = {"accept": "accepted", "reject": "rejected", "partial": "partial"}[decision]
    c.execute(
        "UPDATE submissions SET state=?, reviewed_at=?, n_accepted=?, n_rejected=? "
        "WHERE submission_id=?",
        (sub_state, t, len(ok_frames), len(bad_frames), submission_id),
    )
    c.commit()
    _set_packet_state(
        c, s["packet_id"],
        "need_human" if bad_frames else "accepted",
        bad_frames or None,
    )
    return jsonify({
        "ok": True,
        "decision": decision,
        "n_accepted": len(ok_frames),
        "n_rejected": len(bad_frames),
        "n_gt_written": n_gt,
        "n_gt_removed": n_removed,
        "packet_state": "need_human" if bad_frames else "accepted",
    })


# ---------------------------------------------------------------- 查询


def _qc_flags(s: Dict[str, Any]) -> List[str]:
    return (db.jload(s.get("qc_json"), {}) or {}).get("flags") or []


@app.get("/api/v2/packets")
def api_packets():
    """包列表。看板一屏要的东西一次给全，不让前端再逐包补查。"""
    state = request.args.get("state")
    q = request.args.get("q")
    limit = min(1000, int(request.args.get("limit") or 200))
    offset = int(request.args.get("offset") or 0)

    where, args = [], []
    if state and state != "all":
        where.append("p.state = ?")
        args.append(state)
    if q:
        where.append("p.packet_id LIKE ?")
        args.append(f"%{q}%")
    # 主提交按 n_labeled 降序挑，**不是**按时间降序：修补模式下人工只重标被打回的两三帧，
    # 那条提交才是最新的，但拿它当这个包的代表会让一个已经 100% 入库的包在列表上显示成
    # 「覆盖率 4%、QC 警告」。代表这个包的应该是那条覆盖全帧的提交。
    sql = (
        "SELECT p.packet_id, p.state, p.priority, p.n_targets, p.created_at, p.updated_at, "
        "  p.produced_at, p.labeled_at, p.reviewed_at, p.thumb_ready, p.decim, p.clip_fps, "
        "  p.group_name, p.stem, p.tag, p.sampling, p.size_bytes, p.producer_job_id, "
        "  p.producer_node, p.need_frames_json, p.seed_json, p.seed_worker, p.seed_at, "
        "  (SELECT COUNT(*) FROM gt g WHERE g.packet_id=p.packet_id) n_gt, "
        "  (SELECT COUNT(*) FROM gt g WHERE g.packet_id=p.packet_id AND g.visible=1) n_gt_visible, "
        "  (SELECT COUNT(*) FROM submissions x WHERE x.packet_id=p.packet_id) n_submissions, "
        "  (SELECT MAX(x.created_at) FROM submissions x WHERE x.packet_id=p.packet_id) last_submitted_at, "
        "  s.submission_id, s.source, s.worker, s.workflow, s.model_used, s.state sub_state, "
        "  s.n_labeled, s.n_visible, s.n_interp, s.mean_conf, s.qc_json, s.bundle_path, "
        "  s.created_at submitted_at "
        "FROM packets p LEFT JOIN submissions s ON s.submission_id = ("
        "  SELECT submission_id FROM submissions x WHERE x.packet_id=p.packet_id "
        "  ORDER BY x.n_labeled DESC, x.created_at DESC LIMIT 1) "
        + ("WHERE " + " AND ".join(where) + " " if where else "")
        + "ORDER BY p.updated_at DESC LIMIT ? OFFSET ?"
    )
    c = conn()
    out = []
    for r in db.rows(c, sql, args + [limit, offset]):
        r["coverage"] = round(r["n_labeled"] / r["n_targets"], 3) if r.get("n_labeled") and r["n_targets"] else None
        r["gt_coverage"] = round(r["n_gt"] / r["n_targets"], 3) if r["n_targets"] else None
        r["qc_flags"] = _qc_flags(r)
        r["need_frames"] = db.jload(r.pop("need_frames_json"), None)
        seed = db.jload(r.pop("seed_json"), None)
        r["has_seed"] = bool(seed)
        r["n_seed"] = len(seed) if isinstance(seed, list) else (len(seed) if isinstance(seed, dict) else 0)
        r.pop("qc_json", None)
        out.append(r)
    total = int(db.scalar(c, "SELECT COUNT(*) FROM packets" + (" WHERE " + " AND ".join(
        w.replace("p.", "") for w in where) if where else ""), args))
    return jsonify({"packets": out, "total": total, "counts": db.pool_counts(c)})


@app.get("/api/v2/submissions")
def api_submissions():
    """待审队列。

    计划只列了 `/api/v2/packets`，但审核页要的是「下一个该审的提交」而不是包，
    而且要**按 QC 标记排序**（可疑的先审）。所以单开一个，排序在服务端算好。

    `source` / `workflow` 两个筛选是给 `scripts/import_legacy.py --server` 用的：它得先
    问一句「这份旧结果导过没有」才能做到幂等，否则每跑一次就多一行 submissions。
    """
    state = request.args.get("state") or "pending"
    limit = min(5000, int(request.args.get("limit") or 100))
    source = request.args.get("source") or "ai"
    where, args = [], []
    if source != "all":
        where.append("s.source=?")
        args.append(source)
    if state != "all":
        where.append("s.state=?")
        args.append(state)
    if request.args.get("workflow"):
        where.append("s.workflow=?")
        args.append(request.args["workflow"])
    if request.args.get("packet_id"):
        where.append("s.packet_id=?")
        args.append(request.args["packet_id"])
    rs = db.rows(
        conn(),
        "SELECT s.*, p.state packet_state, p.thumb_ready FROM submissions s "
        "JOIN packets p ON p.packet_id=s.packet_id "
        + ("WHERE " + " AND ".join(where) + " " if where else "")
        + "ORDER BY s.created_at ASC LIMIT ?",
        args + [limit],
    )
    out = []
    for r in rs:
        flags = _qc_flags(r)
        out.append({
            "submission_id": r["submission_id"], "packet_id": r["packet_id"],
            "source": r["source"], "worker": r["worker"], "workflow": r["workflow"],
            "model_used": r["model_used"], "state": r["state"], "packet_state": r["packet_state"],
            "n_targets": r["n_targets"], "n_labeled": r["n_labeled"], "n_visible": r["n_visible"],
            "n_interp": r["n_interp"], "mean_conf": r["mean_conf"],
            "coverage": round(r["n_labeled"] / r["n_targets"], 3) if r["n_targets"] else None,
            "qc_flags": flags,
            "has_bundle": bool(r.get("thumb_ready") or r["bundle_path"]),
            "has_thumb": bool(r.get("thumb_ready")),
            "created_at": r["created_at"],
        })
    # 有 QC 标记的排前面，其次低置信、插值多的
    out.sort(key=lambda x: (
        -len(x["qc_flags"]),
        x["mean_conf"] if x["mean_conf"] is not None else 1.0,
        -(x["n_interp"] or 0),
        x["created_at"],
    ))
    return jsonify({"submissions": out, "n": len(out)})


def _frames_from_labels(labels, meta: dict) -> List[dict]:
    by_fi = {}
    for r in labels or []:
        if isinstance(r, dict) and r.get("frame_idx") is not None:
            by_fi[int(r["frame_idx"])] = r
    targets = meta.get("targets") or sorted(by_fi)
    out = []
    for i, fi in enumerate(targets):
        r = by_fi.get(int(fi), {})
        out.append({
            "i": i, "frame_idx": int(fi),
            "pano_x": r.get("pano_x"), "pano_y": r.get("pano_y"),
            "visible": r.get("visible"), "interp": r.get("interp"),
            "occluded": r.get("occluded"), "blurred": r.get("blurred"),
            "conf": r.get("conf"),
        })
    return out


@app.get("/api/v2/submission/<int:submission_id>")
def api_submission(submission_id: int):
    """审核页：labels + 缩略件地址 + QC。"""
    c = conn()
    s = db.one(c, "SELECT * FROM submissions WHERE submission_id=?", (submission_id,))
    if not s:
        return bad("没有这个 submission", 404)
    p = db.one(c, "SELECT * FROM packets WHERE packet_id=?", (s["packet_id"],)) or {}
    meta = db.jload(p.get("meta_json"), {}) or {}
    labels = db.jload(s["labels_json"], [])
    pid = p.get("packet_id")
    tdir = thumb_root() / str(pid) if pid else None
    files = sorted(f.name for f in tdir.iterdir() if f.is_file()) if tdir and tdir.is_dir() else []
    thumb_ready = bool(p.get("thumb_ready") or ("clip.mp4" in files))
    old_dir = _legacy_bundle_dir(s)
    old_files, old_man = [], None
    if old_dir:
        old_files = sorted(f.name for f in old_dir.iterdir() if f.is_file())
        mp = old_dir / "manifest.json"
        if mp.exists():
            old_man = db.jload(mp.read_text(encoding="utf-8"), None)
    nxt = db.one(
        c,
        "SELECT submission_id FROM submissions WHERE state=? AND submission_id<>? "
        "ORDER BY created_at ASC LIMIT 1",
        (s["state"], submission_id),
    )
    packet_meta = {k: meta.get(k) for k in
                   ("group", "stem", "tag", "fps", "clip_fps", "decim", "roi", "pano_size",
                    "step", "targets", "frame_start", "frame_end", "clip_frames", "sampling")}
    return jsonify({
        "submission": {
            **{k: s[k] for k in s if k not in ("labels_json", "qc_json")},
            "labels": labels,
            "qc": db.jload(s["qc_json"], {}),
        },
        "packet": {
            "packet_id": pid, "state": p.get("state"),
            "n_targets": p.get("n_targets"), "aoss_key": p.get("aoss_key"),
            "need_frames": db.jload(p.get("need_frames_json"), None),
            "produced_at": p.get("produced_at"),
            "labeled_at": p.get("labeled_at"),
            "reviewed_at": p.get("reviewed_at"),
            "thumb_ready": int(p.get("thumb_ready") or 0),
            "meta": packet_meta,
        },
        "thumb": {"base": f"/thumb/{pid}", "files": files, "ready": thumb_ready},
        "frames": _frames_from_labels(labels, meta),
        "bundle": {
            "base": f"/bundle/{submission_id}",
            "files": old_files,
            "manifest": old_man,
        },
        "reviews": db.rows(c, "SELECT * FROM reviews WHERE submission_id=? ORDER BY created_at",
                           (submission_id,)),
        "next_submission_id": nxt["submission_id"] if nxt else None,
    })


def _legacy_bundle_dir(s) -> Optional[Path]:
    """旧 sprite 审核包目录。新提交不再写，线上存量仍要能播。"""
    bp = s.get("bundle_path")
    if bp:
        p = Path(bp)
        if p.is_file():
            p = p.parent
        if p.is_dir():
            return p
    d = thumb_root() / str(s.get("submission_id") or "")
    if d.is_dir() and (d / "manifest.json").exists():
        return d
    return None


def _send_static(p: Path):
    r = send_file(p, conditional=True)
    r.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return r


@app.get("/thumb/<packet_id>/<path:name>")
def api_thumb_file(packet_id: str, name: str):
    """发产包端上传的缩略视频 / scan 图。"""
    if not SAFE_ID.match(packet_id) or ".." in Path(name).parts or name.startswith("/"):
        return bad("非法路径", 400)
    if Path(name).suffix.lower() not in THUMB_SUFFIXES:
        return bad("非法后缀", 400)
    p = thumb_root() / packet_id / name
    if not p.exists():
        return bad("文件不存在", 404)
    return _send_static(p)


@app.get("/bundle/<int:submission_id>/<path:name>")
def api_bundle_file_legacy(submission_id: int, name: str):
    """旧 sprite 审核包；没有则回落到该包缩略件。"""
    if ".." in Path(name).parts or name.startswith("/"):
        return bad("非法路径", 400)
    if Path(name).suffix.lower() not in THUMB_SUFFIXES:
        return bad("非法后缀", 400)
    s = db.one(conn(), "SELECT * FROM submissions WHERE submission_id=?", (submission_id,))
    if not s:
        return bad("没有这个 submission", 404)
    d = _legacy_bundle_dir(s)
    if d:
        p = d / name
        if p.exists():
            return _send_static(p)
    pid = s.get("packet_id")
    if pid:
        return api_thumb_file(pid, name)
    return bad("文件不存在", 404)


@app.get("/api/v2/gt/<packet_id>")
def api_gt(packet_id: str):
    """某个包已入库的帧。修补模式要拿它当初值，导出脚本自测也用它。"""
    return jsonify({
        "packet_id": packet_id,
        "gt": db.rows(conn(), "SELECT * FROM gt WHERE packet_id=? ORDER BY frame_idx", (packet_id,)),
    })


# ---------------------------------------------------------------- 统计


@app.get("/api/v2/stats")
def api_stats():
    """看板的全部数字。

    最重要的一块是 `review_rate`：按模型 / 工作流的审核通过率，这是判断 AI 到底
    能不能用的唯一指标。包级通过率（整包一次过）和帧级通过率都给，因为「整包过不了
    但九成帧能用」才是这套系统预期的常态。
    """
    hours = float(request.args.get("hours") or 24)
    buckets = min(288, int(request.args.get("buckets") or 48))
    c = conn()
    db.expire_leases(c)
    t_end = db.now()
    t0 = t_end - hours * 3600
    step = (t_end - t0) / buckets

    def series(sql: str, args=()) -> List[int]:
        out = [0] * buckets
        for r in db.rows(c, sql, tuple(args) + (t0,)):
            i = min(buckets - 1, max(0, int((r["ts"] - t0) / step)))
            out[i] += int(r.get("n") or 1)
        return out

    throughput = {
        "t0": t0, "t_end": t_end, "step_sec": step, "buckets": buckets,
        "registered": series("SELECT created_at ts FROM packets WHERE created_at >= ?"),
        "leased": series("SELECT created_at ts FROM leases WHERE created_at >= ?"),
        "submitted_ai": series(
            "SELECT created_at ts FROM submissions WHERE source='ai' AND created_at >= ?"),
        "submitted_human": series(
            "SELECT created_at ts FROM submissions WHERE source='human' AND created_at >= ?"),
        "reviewed": series("SELECT created_at ts FROM reviews WHERE created_at >= ?"),
        "gt_frames": series("SELECT updated_at ts, COUNT(*) n FROM gt WHERE updated_at >= ? "
                            "GROUP BY CAST(updated_at AS INTEGER)"),
    }

    review_rate = []
    for r in db.rows(
        c,
        "SELECT COALESCE(model_used,'(未标注模型)') model_used, COALESCE(workflow,'(未标注工作流)') workflow, "
        "  COUNT(*) n_sub, "
        "  SUM(state='accepted') n_pkt_accepted, SUM(state='partial') n_pkt_partial, "
        "  SUM(state='rejected') n_pkt_rejected, SUM(state='pending') n_pending, "
        "  SUM(n_accepted) f_ok, SUM(n_rejected) f_bad, AVG(mean_conf) conf, "
        "  AVG(CAST(n_interp AS REAL)/NULLIF(n_labeled,0)) interp_ratio "
        "FROM submissions WHERE source='ai' GROUP BY model_used, workflow ORDER BY n_sub DESC",
    ):
        judged = (r["n_pkt_accepted"] or 0) + (r["n_pkt_partial"] or 0) + (r["n_pkt_rejected"] or 0)
        frames = (r["f_ok"] or 0) + (r["f_bad"] or 0)
        r["n_judged"] = judged
        r["packet_pass_rate"] = round((r["n_pkt_accepted"] or 0) / judged, 3) if judged else None
        r["frame_pass_rate"] = round((r["f_ok"] or 0) / frames, 3) if frames else None
        r["mean_conf"] = round(r["conf"], 3) if r["conf"] is not None else None
        r["interp_ratio"] = round(r["interp_ratio"], 3) if r["interp_ratio"] is not None else None
        r.pop("conf", None)
        review_rate.append(r)

    workers = db.rows(
        c,
        "SELECT worker, source, COUNT(*) n_frames, SUM(visible) n_visible, MAX(updated_at) last_at "
        "FROM gt GROUP BY worker, source ORDER BY n_frames DESC LIMIT 50",
    )
    return jsonify({
        "counts": db.pool_counts(c),
        "gt": {
            "n_frames": int(db.scalar(c, "SELECT COUNT(*) FROM gt")),
            "n_visible": int(db.scalar(c, "SELECT COUNT(*) FROM gt WHERE visible=1")),
            "n_packets": int(db.scalar(c, "SELECT COUNT(DISTINCT packet_id) FROM gt")),
            "n_ai": int(db.scalar(c, "SELECT COUNT(*) FROM gt WHERE source='ai'")),
            "n_human": int(db.scalar(c, "SELECT COUNT(*) FROM gt WHERE source='human'")),
            "n_interp": int(db.scalar(c, "SELECT COUNT(*) FROM gt WHERE interp=1")),
        },
        "throughput": throughput,
        "review_rate": review_rate,
        "workers": workers,
        "pool": db.pool_view(c, CFG["keep_available"], CFG["batch"]),
        "pool_series": db.pool_depth_series(c, hours=hours, buckets=buckets),
        "n_open_leases": int(db.scalar(
            c, "SELECT COUNT(*) FROM leases WHERE released_at IS NULL")),
    })


# ---------------------------------------------------------------- 采集端


@app.post("/api/v2/producer/register")
def api_producer_register():
    """`submit.sh` 拿到 `pt-*` 之后登记。幂等：同一个 job_id 重提只更新。"""
    d = body()
    job_id = str(d.get("job_id") or "")
    if not job_id:
        return bad("缺少 job_id")
    c = conn()
    t = db.now()
    exist = db.one(c, "SELECT job_id FROM producers WHERE job_id=?", (job_id,))
    fields = {
        "display_name": d.get("display_name"), "pool": d.get("pool"),
        "resource": d.get("resource"), "node": d.get("node"),
        "run_stamp": d.get("run_stamp"), "acp_state": d.get("acp_state"),
        "log_path": d.get("log_path"),
        "poll_sec": int(d["poll_sec"]) if d.get("poll_sec") else None,
        "extra_json": db.jdump(d.get("extra")) if d.get("extra") is not None else None,
    }
    if exist:
        sets = ", ".join(f"{k}=COALESCE(?, {k})" for k in fields)
        c.execute(f"UPDATE producers SET {sets}, updated_at=? WHERE job_id=?",
                  list(fields.values()) + [t, job_id])
    else:
        cols = ", ".join(fields)
        c.execute(
            f"INSERT INTO producers(job_id, {cols}, health, started_at, created_at, updated_at) "
            f"VALUES(?, {', '.join('?' * len(fields))}, 'running', ?, ?, ?)",
            [job_id] + list(fields.values()) + [d.get("started_at") or t, t, t],
        )
    c.commit()
    db.add_producer_event(c, job_id, "start" if not exist else "acp_state",
                          str(d.get("msg") or ("登记作业" if not exist else "更新作业信息")),
                          {k: v for k, v in fields.items() if v is not None})
    return jsonify({"ok": True, "job_id": job_id, "new": not exist})


@app.post("/api/v2/producer/heartbeat")
def api_producer_heartbeat():
    """作业内心跳，也是产包端拿补包指令的地方。

    顺手把 `want_more` 一起返回：产包端每轮本来就要发心跳，再单独打一次
    `/api/v2/pool` 是白往返。两条通道（作业内 / collector_agent）都打这个口，
    区别只在带哪些字段：作业内带 loop_iter / stage，外部轮询带 acp_state / node。
    """
    d = body()
    job_id = str(d.get("job_id") or "")
    if not job_id:
        return bad("缺少 job_id")
    c = conn()
    t = db.now()
    if not db.one(c, "SELECT job_id FROM producers WHERE job_id=?", (job_id,)):
        # 没登记过就自动建一条：ACP 上手动重跑的作业不该因为漏登记就监控不到
        c.execute(
            "INSERT INTO producers(job_id, display_name, health, started_at, created_at, updated_at) "
            "VALUES(?,?,'running',?,?,?)",
            (job_id, d.get("display_name") or job_id, t, t, t),
        )
        c.commit()
        db.add_producer_event(c, job_id, "start", "心跳自动登记")

    from_acp = bool(d.get("acp_state"))
    sets = {
        "loop_iter": d.get("loop_iter"), "stage": d.get("stage"),
        "n_produced": d.get("n_produced"), "last_error": d.get("last_error"),
        "acp_state": d.get("acp_state"), "node": d.get("node"),
        "log_path": d.get("log_path"),
        "poll_sec": int(d["poll_sec"]) if d.get("poll_sec") else None,
    }
    assign = ", ".join(f"{k}=COALESCE(?, {k})" for k in sets)
    # 外部轮询不该刷进程级心跳时间戳，否则「作业还活着但进程卡住」永远判不出 stale
    stamp = "last_acp_poll=?" if from_acp and d.get("loop_iter") is None else "last_heartbeat=?"
    c.execute(f"UPDATE producers SET {assign}, {stamp}, updated_at=? WHERE job_id=?",
              list(sets.values()) + [t, t, job_id])
    c.commit()
    db.refresh_producer_health(c)
    if d.get("last_error"):
        db.add_producer_event(c, job_id, "error", str(d["last_error"]))

    pool = db.pool_view(c, int(d.get("keep_available") or CFG["keep_available"]),
                       int(d.get("batch") or CFG["batch"]))
    r = db.one(c, "SELECT health FROM producers WHERE job_id=?", (job_id,)) or {}
    return jsonify({"ok": True, "health": r.get("health"), "want_more": pool["want_more"], "pool": pool})


@app.post("/api/v2/producer/event")
def api_producer_event():
    d = body()
    job_id = str(d.get("job_id") or "")
    kind = str(d.get("kind") or "progress")
    if not job_id:
        return bad("缺少 job_id")
    c = conn()
    eid = db.add_producer_event(c, job_id, kind, str(d.get("msg") or ""), d.get("extra"))
    if kind == "produced":
        c.execute("UPDATE producers SET n_produced=n_produced+1, updated_at=? WHERE job_id=?",
                  (db.now(), job_id))
        c.commit()
    elif kind in ("error", "exit"):
        c.execute("UPDATE producers SET last_error=?, updated_at=? WHERE job_id=?",
                  (str(d.get("msg") or kind), db.now(), job_id))
        c.commit()
    return jsonify({"ok": True, "event_id": eid})


@app.get("/api/v2/producers")
def api_producers():
    c = conn()
    db.refresh_producer_health(c)
    t = db.now()
    out = []
    for r in db.rows(c, "SELECT * FROM producers ORDER BY COALESCE(last_heartbeat, started_at, 0) DESC"):
        r["hb_age_sec"] = round(t - r["last_heartbeat"], 1) if r["last_heartbeat"] else None
        r["acp_age_sec"] = round(t - r["last_acp_poll"], 1) if r["last_acp_poll"] else None
        r["n_packets"] = int(db.scalar(
            c, "SELECT COUNT(*) FROM packets WHERE producer_job_id=?", (r["job_id"],)))
        r["rate_per_h"] = db.rate_per_h(
            c, "SELECT COUNT(*) FROM packets WHERE producer_job_id=? AND created_at >= ?",
            (r["job_id"],))
        r["extra"] = db.jload(r.pop("extra_json"), None)
        r["events"] = db.rows(
            c, "SELECT event_id, ts, kind, msg FROM producer_events WHERE job_id=? "
               "ORDER BY ts DESC LIMIT 30", (r["job_id"],))
        out.append(r)
    return jsonify({
        "producers": out,
        "pool": db.pool_view(c, CFG["keep_available"], CFG["batch"]),
        "pool_series": db.pool_depth_series(c, hours=float(request.args.get("hours") or 24)),
    })


@app.get("/api/v2/pool")
def api_pool():
    """产包端补池的控制信号。

    产包端**不能**再用 `store.pool_status()` 数 AOSS 上的待领包：AOSS 上不再写
    claims/，那个数恒等于全部已产包，池子会看起来永远是满的。按这里的 `want_more`
    干活。服务端不可达时产包端要退回按固定目标数生产（那部分在产包端实现）。
    """
    c = conn()
    keep = int(request.args.get("keep_available") or CFG["keep_available"])
    batch = int(request.args.get("batch") or CFG["batch"])
    return jsonify(db.pool_view(c, keep, batch))


# ---------------------------------------------------------------- 启动


def init_runtime(
    db_path: Optional[str] = None,
    bundle_root_path: Optional[str] = None,
    keep_available: Optional[int] = None,
    batch: Optional[int] = None,
) -> None:
    CFG["db_path"] = Path(db_path or db.default_db_path())
    CFG["thumb_root"] = Path(
        bundle_root_path or os.environ.get("BALL_GT_THUMBS") or os.environ.get("BALL_GT_BUNDLES")
        or (CFG["db_path"].parent / "thumbs")
    )
    CFG["thumb_root"].mkdir(parents=True, exist_ok=True)
    CFG["bundle_root"] = CFG["thumb_root"]
    CFG["keep_available"] = int(keep_available or os.environ.get("BALL_GT_KEEP_AVAILABLE") or 40)
    CFG["batch"] = int(batch or os.environ.get("BALL_GT_BATCH") or 10)
    CFG["api_token"] = os.environ.get("BALL_GT_API_TOKEN") or ""
    db.migrate(db.connect(CFG["db_path"]))


# waitress / gunicorn 直接 import app 时也要有运行时配置
init_runtime()


def parse_args():
    p = argparse.ArgumentParser(description="足球 GT 标注系统服务端 v2（调度 + 审核 + 看板）")
    p.add_argument("--host", default="127.0.0.1", help="公网部署时由 nginx 反代，默认只听本机")
    p.add_argument("--port", type=int, default=8780)
    p.add_argument("--db", default=None, help=f"SQLite 路径，默认 {db.default_db_path()}")
    p.add_argument("--bundles", default=None, help="缩略件目录，默认 <db 同级>/thumbs")
    p.add_argument("--thumbs", default=None, help="同 --bundles")
    p.add_argument("--keep-available", type=int, default=None, help="产包端要维持的待领包数（want_more 的基准）")
    p.add_argument("--batch", type=int, default=None, help="单轮补包上限")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main() -> None:
    a = parse_args()
    init_runtime(a.db, a.thumbs or a.bundles, a.keep_available, a.batch)
    print("[mode]  server_v2（不持有 AOSS / ACP 凭证）", flush=True)
    print(f"[db]    {CFG['db_path']}", flush=True)
    print(f"[thumb] {CFG['thumb_root']}", flush=True)
    print(f"[pool]  keep_available={CFG['keep_available']} batch={CFG['batch']}", flush=True)
    print(f"[auth]  {'共享 token 已开' if CFG['api_token'] else '无鉴权（设 BALL_GT_API_TOKEN 可开）'}", flush=True)
    print(f"[open]  http://127.0.0.1:{a.port}/", flush=True)
    app.run(host=a.host, port=a.port, debug=a.debug, threaded=True)


if __name__ == "__main__":
    main()
