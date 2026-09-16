#!/usr/bin/env python3
"""本机逐步点选调试站：人按提示词点格子，或换模型让 VLM 点同一张图。

不连中心服务端，不 lease。用已经 prepare 好的包（默认
`/mnt/afs/wangmin/data/_ai_label_work/{work,acp}`）。

    venv/bin/python3 -m client.lab
    # 打开 http://127.0.0.1:8782/
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client.lab_core import (  # noqa: E402
    DEFAULT_ACP,
    DEFAULT_GT,
    DEFAULT_WORK,
    LabSession,
    discover_packets,
)
from client.run import DEFAULT_WORK as CACHE_WORK  # noqa: E402
from client.vlm import VlmPool, load_dotenv, load_providers, role_map  # noqa: E402
from client.workflow import Workflow  # noqa: E402

log = logging.getLogger("client.lab")
REPO = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO / "server"

app = Flask(
    __name__,
    static_folder=str(SERVER_DIR / "static"),
    template_folder=str(SERVER_DIR / "templates"),
)

CFG: Dict[str, Any] = {}
_SESS: Dict[str, LabSession] = {}
_LOCK = threading.Lock()


def _roots() -> List[Path]:
    return list(CFG.get("roots") or [])


def _gt_dir() -> Path:
    return Path(CFG["gt_dir"])


def _pool() -> VlmPool:
    if CFG.get("pool") is None:
        CFG["pool"] = VlmPool()
    return CFG["pool"]


def _wf() -> Workflow:
    if CFG.get("wf") is None:
        CFG["wf"] = Workflow.load(CFG.get("workflow") or "erp_ball_v1", _pool())
    return CFG["wf"]


def _sess(pid: str) -> Optional[LabSession]:
    return _SESS.get(pid)


# ---------------------------------------------------------------- 页面


@app.get("/")
def page_home():
    return send_from_directory(app.template_folder, "lab.html")


@app.get("/p/<packet_id>")
def page_play(packet_id: str):
    return send_from_directory(app.template_folder, "lab.html")


# ---------------------------------------------------------------- API


@app.get("/api/bootstrap")
def api_bootstrap():
    load_dotenv()
    cfgs = load_providers()
    roles = role_map()
    pool = _pool()
    providers = []
    for name, c in cfgs.items():
        providers.append({
            "name": name,
            "model": c.model,
            "has_key": c.has_key,
            "enabled": c.enabled,
            "notes": c.notes,
        })
    stages = []
    for s in _wf().stages:
        vs = s.get("view") or {}
        g = s.get("grid") or {}
        stages.append({
            "id": s.get("id"),
            "kind": s.get("kind"),
            "when": s.get("when", ""),
            "size": vs.get("size"),
            "grid": [g.get("cols"), g.get("rows")] if g else None,
            "prompt": s.get("prompt"),
            "model": s.get("model"),
        })
    return jsonify({
        "packets": discover_packets(_roots()),
        "providers": providers,
        "roles": roles,
        "available": pool.available(),
        "workflow": _wf().name,
        "stages": stages,
        "gt_dir": str(_gt_dir()),
        "roots": [str(p) for p in _roots()],
    })


@app.post("/api/open")
def api_open():
    d = request.get_json(silent=True) or {}
    pid = str(d.get("packet_id") or "").strip()
    if not pid:
        return jsonify({"error": "缺少 packet_id"}), 400
    work_root = d.get("work_root")
    if work_root:
        wr = Path(work_root)
    else:
        wr = next((r for r in _roots() if (r / pid / "prepare.json").exists()), None)
        if wr is None:
            return jsonify({"error": f"找不到已解码的包 {pid}"}), 404
        wr = Path(wr)
    seed_n = int(d.get("seed_n", 0))
    start = int(d.get("start", 0))
    try:
        sess = LabSession.open(pid, wr, _wf(), _gt_dir(), seed_n=seed_n, start=start)
    except Exception as e:  # noqa: BLE001
        log.exception("打开 %s 失败", pid)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    with _LOCK:
        _SESS[pid] = sess
    return jsonify(sess.state())


@app.get("/api/state/<packet_id>")
def api_state(packet_id: str):
    s = _sess(packet_id)
    if s is None:
        return jsonify({"error": "还没打开这个包，先点「打开」"}), 404
    return jsonify(s.state())


@app.post("/api/click/<packet_id>")
def api_click(packet_id: str):
    s = _sess(packet_id)
    if s is None:
        return jsonify({"error": "还没打开这个包"}), 404
    d = request.get_json(silent=True) or {}
    if d.get("null"):
        return jsonify(s.commit_cell(None, conf=float(d.get("conf", 0.85)), source="human"))
    if "x" in d and "y" in d:
        strip = d.get("strip")
        hit = s.click_canvas(
            float(d["x"]), float(d["y"]),
            strip=None if strip is None else int(strip),
        )
        if hit.get("error") and hit.get("x") is None:
            return jsonify(hit), 400
        if d.get("commit") and hit.get("x") is not None:
            spec = s.pending_spec or {}
            pixel = spec.get("kind") == "local_propose" or (
                s.pending_view is not None and s.pending_view.kind == "global"
            )
            if pixel:
                return jsonify(s.commit_xy(
                    float(hit["x"]), float(hit["y"]),
                    conf=float(d.get("conf", 0.85)), source="human",
                ))
            cell = d.get("cell") or hit.get("cell")
            if cell:
                return jsonify(s.commit_cell(cell, conf=float(d.get("conf", 0.85)), source="human"))
        return jsonify(hit)
    if "cell" in d:
        return jsonify(s.commit_cell(
            d.get("cell"),
            conf=float(d.get("conf", 0.85)),
            blurred=bool(d.get("blurred")),
            source="human",
        ))
    return jsonify({"error": "需要 cell 或画布坐标 x,y"}), 400


@app.post("/api/ask/<packet_id>")
def api_ask(packet_id: str):
    s = _sess(packet_id)
    if s is None:
        return jsonify({"error": "还没打开这个包"}), 404
    d = request.get_json(silent=True) or {}
    provider = d.get("provider") or None
    adopt = bool(d.get("adopt"))
    try:
        sug = s.ask_model(_pool(), provider=provider, use_cache=not d.get("no_cache"))
    except Exception as e:  # noqa: BLE001
        log.exception("问模型失败")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    if sug.get("error") and not sug.get("cell") and "provider" not in sug:
        return jsonify(sug), 400
    if adopt and not sug.get("error"):
        return jsonify(s.adopt_suggestion(sug.get("provider") or provider or ""))
    return jsonify({"suggestion": sug, "state": s.state()})


@app.post("/api/adopt/<packet_id>")
def api_adopt(packet_id: str):
    s = _sess(packet_id)
    if s is None:
        return jsonify({"error": "还没打开这个包"}), 404
    d = request.get_json(silent=True) or {}
    provider = str(d.get("provider") or "")
    return jsonify(s.adopt_suggestion(provider))


@app.post("/api/shift/<packet_id>")
def api_shift(packet_id: str):
    s = _sess(packet_id)
    if s is None:
        return jsonify({"error": "还没打开这个包"}), 404
    d = request.get_json(silent=True) or {}
    out = s.shift_window(int(d.get("dx", 0)), int(d.get("dy", 0)))
    return jsonify(out), (400 if out.get("error") else 200)


@app.post("/api/undo/<packet_id>")
def api_undo(packet_id: str):
    s = _sess(packet_id)
    if s is None:
        return jsonify({"error": "还没打开这个包"}), 404
    return jsonify(s.undo())


@app.post("/api/next/<packet_id>")
def api_next(packet_id: str):
    s = _sess(packet_id)
    if s is None:
        return jsonify({"error": "还没打开这个包"}), 404
    return jsonify(s.next_frame())


@app.post("/api/goto/<packet_id>")
def api_goto(packet_id: str):
    s = _sess(packet_id)
    if s is None:
        return jsonify({"error": "还没打开这个包"}), 404
    d = request.get_json(silent=True) or {}
    if d.get("frame_idx") is None:
        return jsonify({"error": "缺少 frame_idx"}), 400
    return jsonify(s.goto_frame(int(d["frame_idx"])))


@app.post("/api/flag/<packet_id>")
def api_flag(packet_id: str):
    s = _sess(packet_id)
    if s is None:
        return jsonify({"error": "还没打开这个包"}), 404
    d = request.get_json(silent=True) or {}
    if "show_gt" in d:
        s.show_gt = bool(d["show_gt"])
        s._refresh_overview()
    return jsonify(s.state())


@app.get("/api/view/<packet_id>/<int:idx>.jpg")
def api_view_idx(packet_id: str, idx: int):
    s = _sess(packet_id)
    if s is None:
        return jsonify({"error": "没有当前视图"}), 404
    if 0 <= idx < len(s.view_jpegs):
        return Response(s.view_jpegs[idx], mimetype="image/jpeg")
    if idx == 0 and s.view_jpeg:
        return Response(s.view_jpeg, mimetype="image/jpeg")
    return jsonify({"error": "没有这张图"}), 404


@app.get("/api/view/<packet_id>.jpg")
def api_view(packet_id: str):
    s = _sess(packet_id)
    if s is None or not s.view_jpeg:
        return jsonify({"error": "没有当前视图"}), 404
    return Response(s.view_jpeg, mimetype="image/jpeg")


@app.get("/api/overview/<packet_id>.jpg")
def api_overview(packet_id: str):
    s = _sess(packet_id)
    if s is None or not s.overview_jpeg:
        return jsonify({"error": "没有总览"}), 404
    return Response(s.overview_jpeg, mimetype="image/jpeg")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="本机逐步点选调试站")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8782)
    p.add_argument("--work", type=Path, default=DEFAULT_WORK)
    p.add_argument("--extra", type=Path, action="append", default=[])
    p.add_argument("--gt", type=Path, default=DEFAULT_GT)
    p.add_argument("--workflow", default="erp_ball_v1")
    return p.parse_args(argv)


def init(work: Path, extra: Optional[List[Path]] = None, gt: Optional[Path] = None,
         workflow: str = "erp_ball_v1") -> None:
    load_dotenv()
    roots: List[Path] = []
    for p in [work, *(extra or []), DEFAULT_ACP, CACHE_WORK]:
        if p and Path(p).is_dir() and Path(p) not in roots:
            roots.append(Path(p))
    CFG["roots"] = roots
    CFG["gt_dir"] = Path(gt or DEFAULT_GT)
    CFG["workflow"] = workflow
    CFG["pool"] = None
    CFG["wf"] = None


def main(argv=None) -> int:
    a = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    extra = list(a.extra)
    if DEFAULT_ACP not in extra and DEFAULT_ACP.is_dir():
        extra.append(DEFAULT_ACP)
    init(a.work, extra, a.gt, a.workflow)
    n = len(discover_packets(_roots()))
    print(f"[lab] http://{a.host}:{a.port}/  packets={n}  work={a.work}  gt={a.gt}", flush=True)
    app.run(host=a.host, port=a.port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
