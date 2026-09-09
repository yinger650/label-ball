#!/usr/bin/env python3
"""足球 GT 标注服务（公网/局域网）。

对象存储的读写全部由**浏览器客户端**完成（领取、下载 zip、上传结果）。
本服务只做：

    接收客户端上传的 zip → 解码 + DoG 增强 → 提供帧图与标注缓存 → 组装结果 JSON

这样公网机器不需要持有 AOSS 凭证，大流量也不经过窄带服务器。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from flask import Flask, jsonify, request, send_file, send_from_directory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from packet import PacketMeta, build_result  # noqa: E402
from prepare import PrepareStatus, load_frames, prepare_packet, workspace  # noqa: E402

app = Flask(__name__, static_folder=str(HERE / "static"), template_folder=str(HERE / "templates"))

CFG: Dict[str, object] = {}
JOBS: Dict[str, PrepareStatus] = {}
JOBS_LOCK = threading.Lock()

# 单包 zip 上限（防误传）
MAX_ZIP_BYTES = 200 * 1024 * 1024


def work_root() -> Path:
    return CFG["work_root"]  # type: ignore[return-value]


def meta_of(packet_id: str) -> PacketMeta:
    d = workspace(work_root(), packet_id)
    return PacketMeta.from_dict(json.loads((d / "meta.json").read_text()))


def labels_path(packet_id: str) -> Path:
    return workspace(work_root(), packet_id) / "labels.json"


def read_labels(packet_id: str) -> dict:
    try:
        return json.loads(labels_path(packet_id).read_text())
    except Exception:
        return {}


def write_labels(packet_id: str, labels: dict) -> None:
    p = labels_path(packet_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(labels, ensure_ascii=False))
    tmp.replace(p)


def annotator_of() -> str:
    return (
        request.headers.get("X-Annotator")
        or request.args.get("annotator")
        or (request.get_json(silent=True) or {}).get("annotator")
        or "anonymous"
    )


# ---------------------------------------------------------------- 页面


@app.get("/")
def index():
    return send_from_directory(app.template_folder, "index.html")


@app.get("/annotate/<packet_id>")
def annotate_page(packet_id: str):
    return send_from_directory(app.template_folder, "annotate.html")


# ---------------------------------------------------------------- 本地工作区（不含 AOSS）


@app.get("/api/whoami")
def api_whoami():
    return jsonify({"mode": "client_aoss", "work_root": str(work_root())})


@app.post("/api/local_status")
def api_local_status():
    """客户端已从 AOSS 知道「我的包」列表，来问本机准备进度与标注进度。"""
    body = request.get_json(silent=True) or {}
    ids: List[str] = list(body.get("packet_ids") or [])
    local = {}
    for pid in ids:
        d = workspace(work_root(), pid)
        ready = (d / "prepare.json").exists() and load_frames(work_root(), pid) is not None
        labels = read_labels(pid) if ready else {}
        n_targets = 0
        if (d / "meta.json").exists():
            try:
                n_targets = meta_of(pid).n_targets
            except Exception:
                pass
        with JOBS_LOCK:
            job = JOBS.get(pid)
        submitted = (d / "submitted.json").exists()
        local[pid] = {
            "ready": ready,
            "n_labeled": len(labels),
            "n_targets": n_targets,
            "job": job.snapshot() if job else None,
            "submitted": submitted,
            "has_zip": (d / "packet.zip").exists(),
            "mirrored": (Path(CFG["packet_mirror"]) / f"{pid}.zip").exists(),  # type: ignore[arg-type]
        }
    return jsonify({"local": local})


@app.post("/api/ingest/<packet_id>")
def api_ingest(packet_id: str):
    """接收客户端上传的 zip，落盘后启动准备。"""
    if "/" in packet_id or ".." in packet_id:
        return jsonify({"error": "非法 packet_id"}), 400
    f = request.files.get("file") or request.files.get("zip")
    if f is None:
        return jsonify({"error": "缺少 file 字段（multipart）"}), 400

    d = workspace(work_root(), packet_id)
    d.mkdir(parents=True, exist_ok=True)
    zpath = d / "packet.zip"
    tmp = d / "packet.zip.part"
    size = 0
    with open(tmp, "wb") as out:
        while True:
            chunk = f.stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ZIP_BYTES:
                tmp.unlink(missing_ok=True)
                return jsonify({"error": f"zip 超过 {MAX_ZIP_BYTES // (1024*1024)} MB"}), 413
            out.write(chunk)
    tmp.replace(zpath)
    return _start_after_zip(packet_id, size)


@app.post("/api/pull/<packet_id>")
def api_pull(packet_id: str):
    """从本机镜像目录取 zip 并开始准备。

    公网无法对 AOSS 做 GetObject（List/Put 可以），所以 zip 由能下载的内网机
    同步到 PACKET_MIRROR（默认 /var/lib/ball_gt_packets）。浏览器只负责领取/提交。
    """
    if "/" in packet_id or ".." in packet_id:
        return jsonify({"error": "非法 packet_id"}), 400
    mirror = Path(CFG["packet_mirror"])  # type: ignore[arg-type]
    src = mirror / f"{packet_id}.zip"
    if not src.exists():
        return jsonify({
            "error": f"本站尚未同步到该包（{src.name}）。请稍等同步任务，或联系管理员。",
            "mirror": str(mirror),
        }), 404
    d = workspace(work_root(), packet_id)
    d.mkdir(parents=True, exist_ok=True)
    zpath = d / "packet.zip"
    if not zpath.exists() or zpath.stat().st_size != src.stat().st_size:
        shutil.copy2(src, zpath)
    return _start_after_zip(packet_id, zpath.stat().st_size)


def _start_after_zip(packet_id: str, size: int):
    d = workspace(work_root(), packet_id)
    with JOBS_LOCK:
        old = JOBS.get(packet_id)
        if old and old.state == "error":
            JOBS.pop(packet_id, None)
            for sub in ("raw", "enh"):
                shutil.rmtree(d / sub, ignore_errors=True)
            for name in ("prepare.json", "meta.json", "clip.mp4"):
                p = d / name
                if p.exists():
                    p.unlink()
    job = start_prepare(packet_id)
    return jsonify({"packet_id": packet_id, "bytes": size, "job": job.snapshot()})


def start_prepare(packet_id: str) -> PrepareStatus:
    with JOBS_LOCK:
        job = JOBS.get(packet_id)
        if job and job.state not in ("error",):
            return job
        job = PrepareStatus()
        JOBS[packet_id] = job
    t = threading.Thread(
        target=prepare_packet,
        args=(work_root(), packet_id, job, int(CFG["workers"])),
        daemon=True,
    )
    t.start()
    return job


@app.post("/api/prepare/<packet_id>")
def api_prepare(packet_id: str):
    d = workspace(work_root(), packet_id)
    if not (d / "packet.zip").exists():
        return jsonify({"error": "本地无 zip，请先 ingest"}), 409
    return jsonify(start_prepare(packet_id).snapshot())


@app.get("/api/prepare/<packet_id>")
def api_prepare_status(packet_id: str):
    with JOBS_LOCK:
        job = JOBS.get(packet_id)
    if job:
        return jsonify(job.snapshot())
    if load_frames(work_root(), packet_id) is not None:
        return jsonify({"state": "ready", "done": 0, "total": 0, "msg": "就绪"})
    return jsonify({"state": "pending", "done": 0, "total": 0, "msg": ""})


# ---------------------------------------------------------------- 标注


@app.get("/api/meta/<packet_id>")
def api_meta(packet_id: str):
    frames = load_frames(work_root(), packet_id)
    if frames is None:
        return jsonify({"error": "该包尚未准备好"}), 409
    m = meta_of(packet_id)
    d = dict(m.__dict__)
    d.pop("sphere_transform", None)
    d["frames"] = frames
    d["labels"] = read_labels(packet_id)
    d["annotator"] = annotator_of()
    return jsonify(d)


@app.get("/img/<packet_id>/<view>/<int:frame_idx>.jpg")
def api_img(packet_id: str, view: str, frame_idx: int):
    sub = {"raw": "raw", "enh": "enh"}.get(view)
    if sub is None:
        return "bad view", 404
    p = workspace(work_root(), packet_id) / sub / f"{frame_idx}.jpg"
    if not p.exists():
        return "not found", 404
    r = send_file(p, mimetype="image/jpeg", conditional=True)
    r.headers["Cache-Control"] = "public, max-age=86400"
    return r


@app.post("/api/label/<packet_id>")
def api_label(packet_id: str):
    rec = request.get_json(silent=True) or {}
    fi = rec.get("frame_idx")
    if fi is None:
        return jsonify({"error": "缺少 frame_idx"}), 400
    labels = read_labels(packet_id)

    if rec.get("clear"):
        labels.pop(str(fi), None)
        write_labels(packet_id, labels)
        return jsonify({"ok": True, "n_labeled": len(labels)})

    out = {
        "frame_idx": int(fi),
        "visible": bool(rec.get("visible")),
        "occluded": bool(rec.get("occluded")),
        "blurred": bool(rec.get("blurred")),
        "pano_x": None,
        "pano_y": None,
        "ts": time.time(),
    }
    if out["visible"]:
        if rec.get("pano_x") is None:
            return jsonify({"error": "可见但没有坐标"}), 400
        out["pano_x"] = float(rec["pano_x"])
        out["pano_y"] = float(rec["pano_y"])
    labels[str(fi)] = out
    write_labels(packet_id, labels)
    return jsonify({"ok": True, "n_labeled": len(labels), "record": out})


@app.post("/api/build_result/<packet_id>")
def api_build_result(packet_id: str):
    """组装待上传的结果 JSON；不写 AOSS。客户端拿到后自行 PutObject。"""
    m = meta_of(packet_id)
    labels = read_labels(packet_id)
    body = request.get_json(silent=True) or {}
    who = body.get("annotator") or annotator_of()
    missing = [t for t in m.targets if str(t) not in labels]
    if missing and not body.get("force"):
        return jsonify({"error": f"还有 {len(missing)} 帧未标注", "missing": missing[:20]}), 400

    d = workspace(work_root(), packet_id)
    started = 0.0
    try:
        started = json.loads((d / "prepare.json").read_text()).get("prepared_at", 0.0)
    except Exception:
        pass
    ts = [r.get("ts", 0) for r in labels.values() if r.get("ts")]
    result = build_result(
        m,
        who,
        labels,
        {
            "started_at": started,
            "finished_at": time.time(),
            "label_span_sec": round(max(ts) - min(ts), 1) if len(ts) > 1 else 0.0,
            "forced": bool(body.get("force")),
            "n_missing": len(missing),
        },
    )
    result_key = f"results/{packet_id}__{who}.json"
    return jsonify({"ok": True, "result": result, "result_key": result_key, "n_labeled": result["n_labeled"]})


@app.post("/api/cleanup/<packet_id>")
def api_cleanup(packet_id: str):
    """客户端已成功把结果写进 AOSS 后调用，释放本机帧缓存。"""
    body = request.get_json(silent=True) or {}
    d = workspace(work_root(), packet_id)
    (d / "submitted.json").write_text(
        json.dumps({"key": body.get("key", ""), "at": time.time(), "annotator": body.get("annotator", "")})
    )
    freed = cleanup_cache(d) if CFG.get("cleanup", True) else 0
    with JOBS_LOCK:
        JOBS.pop(packet_id, None)
    return jsonify({"ok": True, "freed_mb": round(freed / 1e6)})


def cleanup_cache(d: Path) -> int:
    freed = 0
    for sub in ("raw", "enh"):
        p = d / sub
        if p.is_dir():
            freed += sum(f.stat().st_size for f in p.glob("*.jpg"))
            shutil.rmtree(p, ignore_errors=True)
    for name in ("clip.mp4", "packet.zip"):
        f = d / name
        if f.exists():
            freed += f.stat().st_size
            f.unlink()
    try:
        st = json.loads((d / "prepare.json").read_text())
        st["state"] = "submitted"
        (d / "prepare.json").write_text(json.dumps(st, ensure_ascii=False))
    except Exception:
        pass
    return freed


# ---------------------------------------------------------------- 启动


def init_runtime(
    work_root_path: Optional[str] = None,
    workers: int = 2,
    cleanup: bool = True,
    packet_mirror: Optional[str] = None,
) -> None:
    root = Path(
        work_root_path
        or os.environ.get("BALL_GT_WORK")
        or str(Path.home() / "ball_gt_work")
    )
    CFG["work_root"] = root
    CFG["work_root"].mkdir(parents=True, exist_ok=True)
    CFG["workers"] = int(os.environ.get("BALL_GT_WORKERS", workers))
    CFG["cleanup"] = cleanup
    CFG["packet_mirror"] = Path(
        packet_mirror
        or os.environ.get("BALL_GT_PACKET_MIRROR")
        or "/var/lib/ball_gt_packets"
    )


# waitress / gunicorn 直接 import app 时也要有运行时配置
init_runtime()


def parse_args():
    p = argparse.ArgumentParser(description="足球 GT 标注服务（客户端承接 AOSS）")
    p.add_argument("--host", default="127.0.0.1", help="公网部署时由 nginx 反代，默认只听本机")
    p.add_argument("--port", type=int, default=8780)
    p.add_argument("--work-root", default=None)
    p.add_argument("--workers", type=int, default=2, help="增强视图线程数（公网小机建议 2）")
    p.add_argument("--no-cleanup", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    init_runtime(args.work_root, workers=args.workers, cleanup=not args.no_cleanup)
    print(f"[mode]  client_aoss（服务端不访问对象存储）", flush=True)
    print(f"[work]  {CFG['work_root']}", flush=True)
    print(f"[open]  http://127.0.0.1:{args.port}/", flush=True)
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
