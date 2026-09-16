#!/usr/bin/env python3
"""服务端冒烟：建库 → 注册 → 领取 → 提交 → 审核 → 打回修补 → 入库 → 导出，全走真 HTTP。

用 `make_fake_bundle.py` 造假 result / meta / 缩略件，起一个真的 Flask 进程打一遍 v2 API。
**不涉及 AI 客户端、不碰 AOSS**：跨端联调由后续的全链路测试负责，这里只保证服务端
内部那条路是通的。

真起进程 + urllib 打 HTTP，而不是用 Flask 的 test_client：multipart 注册缩略件、
`send_file` 的条件请求、`/thumb` 静态分发这些恰恰是 test_client 最容易蒙过去的地方。

    ./venv/bin/python scripts/smoke_server.py
    ./venv/bin/python scripts/smoke_server.py --keep-alive   # 跑完不退，好用浏览器看页面
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "server"))

import make_fake_bundle  # noqa: E402


# ---------------------------------------------------------------- HTTP 客户端


class Client:
    def __init__(self, base: str, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token

    def _req(self, req: urllib.request.Request) -> Tuple[int, Any]:
        if self.token:
            req.add_header("X-Api-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                ct = r.headers.get("Content-Type", "")
                return r.status, (json.loads(raw) if "json" in ct else raw)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:  # noqa: BLE001
                return e.code, raw

    def get(self, path: str, **params) -> Tuple[int, Any]:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._req(urllib.request.Request(url, method="GET"))

    def post(self, path: str, body: Optional[dict] = None, worker: str = "") -> Tuple[int, Any]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body or {}, ensure_ascii=False).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        if worker:
            req.add_header("X-Worker", worker)
        return self._req(req)

    def post_multipart(self, path: str, fields: Dict[str, str], files: Dict[str, Path],
                       worker: str = "") -> Tuple[int, Any]:
        """手搓 multipart：标准库没有现成的，而 requests 没装（也不该为自测加依赖）。"""
        boundary = "----smoke" + uuid.uuid4().hex
        parts: List[bytes] = []
        for k, v in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            )
        for k, p in files.items():
            ct = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                f'filename="{p.name}"\r\nContent-Type: {ct}\r\n\r\n'.encode()
                + p.read_bytes() + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(self.base + path, data=b"".join(parts), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        if worker:
            req.add_header("X-Worker", worker)
        return self._req(req)


# ---------------------------------------------------------------- 断言


class Checks:
    def __init__(self) -> None:
        self.rows: List[Tuple[bool, str, str]] = []

    def __call__(self, ok: bool, name: str, detail: str = "") -> bool:
        self.rows.append((bool(ok), name, detail))
        print(f"  {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""), flush=True)
        return bool(ok)

    @property
    def n_fail(self) -> int:
        return sum(1 for ok, _, _ in self.rows if not ok)

    def report(self) -> int:
        print(f"\n[result] {len(self.rows) - self.n_fail}/{len(self.rows)} 项通过", flush=True)
        if self.n_fail:
            print("  失败项：", flush=True)
            for ok, name, detail in self.rows:
                if not ok:
                    print(f"    - {name}  {detail}", flush=True)
        return 1 if self.n_fail else 0


# ---------------------------------------------------------------- 起服务


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_server(py: str, db: Path, bundles: Path, port: int, log: Path,
                 keep_available: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("BALL_GT_API_TOKEN", None)  # 自测不开鉴权，免得被开发机上的环境变量带跑
    f = open(log, "wb")
    proc = subprocess.Popen(
        [py, "server/app.py", "--host", "127.0.0.1", "--port", str(port),
         "--db", str(db), "--bundles", str(bundles), "--keep-available", str(keep_available)],
        cwd=str(REPO), stdout=f, stderr=subprocess.STDOUT, env=env,
    )
    cli = Client(f"http://127.0.0.1:{port}")
    for _ in range(120):
        if proc.poll() is not None:
            print(log.read_text(errors="replace"), flush=True)
            raise SystemExit(f"服务端启动即退出（exit {proc.returncode}），日志见上")
        try:
            if cli.get("/healthz")[0] == 200:
                return proc
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    proc.kill()
    print(log.read_text(errors="replace"), flush=True)
    raise SystemExit("服务端 30s 内没起来")


# ---------------------------------------------------------------- 主流程


def run(args) -> int:
    ck = Checks()
    work = Path(args.work or tempfile.mkdtemp(prefix="ball_gt_smoke_"))
    work.mkdir(parents=True, exist_ok=True)
    db_path = work / "ball_gt.sqlite3"
    bundles = work / "bundles"
    log = work / "server.log"
    py = str(REPO / "venv" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable
    print(f"[work] {work}\n[py]   {py}", flush=True)

    # ---- 造两个假包。第二个用小 ROI，逼出 region 轨道，好让审核页的固定区域模式有数据
    print("\n[1/9] 造假包 + 缩略件", flush=True)
    pkt_a = make_fake_bundle.build(
        work / "pkt_a", "smokegrp__stemA__0010_0020", n=48, cols=14, step=3, fps=30.0,
        frame_start=300, roi=[1200, 900, 3600, 1700], pano=[7680, 3840], seed=1,
    )
    pkt_b = make_fake_bundle.build(
        work / "pkt_b", "smokegrp__stemB__0030_0040", n=32, cols=14, step=3, fps=30.0,
        frame_start=900, roi=[1200, 900, 700, 600], pano=[7680, 3840], seed=3,
    )
    ck(pkt_a["thumbs"].is_dir() and (pkt_a["thumbs"] / "clip.mp4").exists()
       and pkt_b["thumbs"].is_dir(),
       "两个假包的缩略件都生成了")
    ck(pkt_a["meta"].get("decim") == 1 and pkt_a["meta"].get("clip_fps") == 30.0,
       "meta 带 decim / clip_fps")

    print("\n[2/9] 起服务端", flush=True)
    port = free_port()
    proc = start_server(py, db_path, bundles, port, log, args.keep_available)
    cli = Client(f"http://127.0.0.1:{port}")
    try:
        st, who = cli.get("/api/whoami")
        ck(st == 200 and who.get("mode") == "server_v2", "服务端起来了", f"port {port}")
        import sqlite3

        mode = sqlite3.connect(str(db_path)).execute("PRAGMA journal_mode").fetchone()[0]
        ck(mode.lower() == "wal", "SQLite 开了 WAL", f"journal_mode={mode}")

        # ---- 注册
        print("\n[3/9] 注册包", flush=True)
        st, r = cli.post("/api/v2/register", {"packets": [
            {"packet_id": p["packet_id"], "aoss_key": f"packets/{p['packet_id']}/",
             "meta": p["meta"], "producer_job_id": "pt-smoke01", "producer_node": "node-smoke"}
            for p in (pkt_a, pkt_b)
        ]})
        ck(st == 200 and r.get("registered") == 2, "批量注册 2 个包", json.dumps(r, ensure_ascii=False))
        st, r2 = cli.post("/api/v2/register", {
            "packet_id": pkt_a["packet_id"], "aoss_key": f"packets/{pkt_a['packet_id']}/",
            "meta": pkt_a["meta"]})
        ck(st == 200 and r2.get("updated") == 1 and r2.get("registered") == 0,
           "重复注册是幂等的（只更新，不新增）")
        st, r3 = cli.post("/api/v2/register", {"packet_id": "bad id/../x", "meta": {}})
        ck(r3.get("errors"), "非法 packet_id 被拒")
        st, r4 = cli.post("/api/v2/register", {
            "packet_id": "test_skip__stem__0000_0010",
            "aoss_key": "packets/test_skip__stem__0000_0010.zip",
            "meta": {**pkt_a["meta"], "group": "test_skip", "targets": [1, 2, 3]},
            "n_targets": 3,
        })
        ck(st == 200 and r4.get("registered") == 1, "多注册一个测试包，后面归档")
        st, ar = cli.post("/api/v2/archive", {
            "packet_ids": ["test_skip__stem__0000_0010"], "reason": "test group",
        })
        ck(st == 200 and ar.get("archived") == ["test_skip__stem__0000_0010"],
           "归档测试包", json.dumps(ar, ensure_ascii=False))
        st, ar2 = cli.post("/api/v2/archive", {
            "packet_ids": ["test_skip__stem__0000_0010"], "reason": "again",
        })
        ck(st == 200 and ar2.get("skipped"), "已经 archived 的包不会再归档")
        st, rt = cli.post_multipart(
            "/api/v2/register",
            {"packet_id": pkt_a["packet_id"],
             "aoss_key": f"packets/{pkt_a['packet_id']}/",
             "meta": json.dumps(pkt_a["meta"], ensure_ascii=False),
             "producer_job_id": "pt-smoke01", "producer_node": "node-smoke"},
            {"thumb_clip": pkt_a["thumbs"] / "clip.mp4",
             "thumb_dog": pkt_a["thumbs"] / "dog.mp4",
             "thumb_scan_dog": pkt_a["thumbs"] / "scan_dog.jpg",
             "thumb_scan_time": pkt_a["thumbs"] / "scan_time.jpg"},
        )
        ck(st == 200 and rt.get("updated") == 1 and rt.get("thumb_files") == 4,
           "multipart 注册写入 4 个缩略件", json.dumps(rt, ensure_ascii=False))
        dest_b = bundles / pkt_b["packet_id"]
        dest_b.mkdir(parents=True, exist_ok=True)
        for name in ("clip.mp4", "dog.mp4", "scan_dog.jpg", "scan_time.jpg"):
            shutil.copy2(pkt_b["thumbs"] / name, dest_b / name)

        # ---- 池子与采集端
        print("\n[4/9] 池子与采集端监控", flush=True)
        st, pool = cli.get("/api/v2/pool")
        ck(st == 200 and pool["n_available"] == 2, "pool 看到 2 个待领包")
        ck(pool["want_more"] == min(args.keep_available - 2, pool["batch"]),
           "pool 返回 want_more（产包端的补包指令）", f"want_more={pool['want_more']}")
        st, _ = cli.post("/api/v2/producer/register", {
            "job_id": "pt-smoke01", "display_name": "ball_gt_produce", "pool": "wm-team-4090",
            "node": "node-smoke", "acp_state": "RUNNING", "poll_sec": 300,
            "log_path": "/tmp/ball_gt_produce.log"})
        ck(st == 200, "产包作业登记")
        st, hb = cli.post("/api/v2/producer/heartbeat", {
            "job_id": "pt-smoke01", "loop_iter": 7, "stage": "ffmpeg 切片", "n_produced": 2,
            "keep_available": args.keep_available, "batch": pool["batch"]})
        ck(st == 200 and hb.get("health") == "running" and "want_more" in hb,
           "心跳返回 health 与 want_more（省掉一次单独问 pool 的往返）")
        cli.post("/api/v2/producer/event",
                 {"job_id": "pt-smoke01", "kind": "produced", "msg": pkt_a["packet_id"]})
        st, prods = cli.get("/api/v2/producers")
        p0 = (prods.get("producers") or [{}])[0]
        ck(st == 200 and p0.get("job_id") == "pt-smoke01" and p0.get("health") == "running",
           "producers 列表带健康度与心跳时延", f"hb_age={p0.get('hb_age_sec')}s")
        ck(p0.get("n_packets") == 2 and any(e["kind"] == "produced" for e in p0.get("events") or []),
           "包能追回是哪个作业产的，事件时间线有 produced")
        st, hb2 = cli.post("/api/v2/producer/heartbeat", {
            "job_id": "pt-smoke01", "acp_state": "FAILED", "node": "node-smoke"})
        ck(hb2.get("health") == "failed", "ACP 报 FAILED 时健康度归因为 failed")
        cli.post("/api/v2/producer/heartbeat",
                 {"job_id": "pt-smoke01", "acp_state": "RUNNING", "loop_iter": 8})

        # ---- 领包
        print("\n[5/9] 领包与租约", flush=True)
        st, r = cli.post("/api/v2/lease", {"worker": "ai-1", "kind": "ai"})
        lease_a = (r or {}).get("lease") or {}
        ck(st == 200 and lease_a.get("packet_id") in (pkt_a["packet_id"], pkt_b["packet_id"]),
           "AI 领到一个包", lease_a.get("packet_id", ""))
        ck(len(lease_a.get("target_frames") or []) == lease_a.get("n_targets"),
           "lease 返回 target_frames", f"{lease_a.get('n_targets')} 帧")
        ck(lease_a.get("meta", {}).get("roi") is not None and lease_a.get("aoss_key"),
           "lease 带上 meta 与 aoss_key（客户端据此自己去 AOSS 下载）")
        st, hb = cli.post(f"/api/v2/heartbeat/{lease_a['lease_id']}", {"stage": "工作流 refine2"})
        ck(st == 200 and hb["expires_at"] > time.time() + 60, "心跳续期成功")

        st, r = cli.post("/api/v2/lease", {"worker": "ai-1", "kind": "ai"})
        lease_other = (r or {}).get("lease") or {}
        ck(lease_other.get("packet_id") and lease_other["packet_id"] != lease_a["packet_id"],
           "同一 worker 不会被重复发同一个包")
        st, r = cli.post("/api/v2/lease", {"worker": "ai-1", "kind": "ai"})
        ck(st == 200 and (r or {}).get("lease") is None, "池子空了就返回 lease=null 而不是报错")
        st, r = cli.post(f"/api/v2/release/{lease_other['lease_id']}", {"reason": "smoke 换包"})
        ck(st == 200 and r.get("ok"), "release 把包放回池子")
        # 换过包的 worker 必须还能再领回来。按「领过就排除」实现的话，单客户端部署下
        # 被 release 过一次的包就再没人捡，会一直烂在池子里
        st, r = cli.post("/api/v2/lease", {"worker": "ai-1", "kind": "ai"})
        again = (r or {}).get("lease") or {}
        ck(again.get("packet_id") == lease_other["packet_id"],
           "release 过的包同一个 worker 还能再领（不会被永久拉黑）")
        cli.post(f"/api/v2/release/{again['lease_id']}", {"reason": "no_seed"})
        st, r = cli.post("/api/v2/lease", {"worker": "ai-1", "kind": "ai"})
        cool = (r or {}).get("lease")
        ck(cool is None, "刚 no_seed 释放的包，同一 worker 冷却期内领不到")
        st, r = cli.post("/api/v2/lease", {"worker": "ai-2", "kind": "ai"})
        other_worker = (r or {}).get("lease") or {}
        ck(other_worker.get("packet_id") == lease_other["packet_id"],
           "别的 worker 仍能领到刚被 no_seed 释放的包")
        cli.post(f"/api/v2/release/{other_worker['lease_id']}", {"reason": "smoke 复位"})
        st, pool = cli.get("/api/v2/pool")
        ck(pool["n_available"] == 1 and pool["n_leased"] == 1, "release 后池子计数对得上")

        # 领到哪个包就用哪份假数据提交，不假设顺序
        cur = pkt_a if lease_a["packet_id"] == pkt_a["packet_id"] else pkt_b
        other = pkt_b if cur is pkt_a else pkt_a

        # ---- AI 提交
        print("\n[6/9] AI 提交（只传 labels）", flush=True)
        st, sub = cli.post_multipart(
            "/api/v2/submit",
            {"lease_id": lease_a["lease_id"], "source": "ai", "packet_id": cur["packet_id"],
             "workflow": "erp_ball_v1", "model_used": "glm-4.6v-flash"},
            {"result": cur["out"] / "result.json"},
            worker="ai-1",
        )
        sid = (sub or {}).get("submission_id")
        ck(st == 200 and sid and sub["state"] == "pending",
           "AI 提交进 submissions 等审核（不直接入库）", f"submission_id={sid}")
        ck(sub.get("n_gt_written") == 0, "AI 提交没有写 gt（必须先过审核）")
        ck(isinstance(sub.get("qc"), dict) and "coverage" in sub["qc"],
           "submit 时算好 QC 存下来", f"flags={sub['qc'].get('flags')}")
        ck((sub.get("thumb") or {}).get("base") == f"/thumb/{cur['packet_id']}",
           "submit 回缩略件地址")

        st, files = cli.get(f"/thumb/{cur['packet_id']}/clip.mp4")
        ck(st == 200 and isinstance(files, bytes) and len(files) > 0,
           "缩略视频能按静态文件取到", f"{len(files)} B")
        st, _ = cli.get(f"/thumb/{cur['packet_id']}/../../../etc/passwd")
        ck(st in (400, 404), "缩略件路径穿越被拒", f"HTTP {st}")

        st, q = cli.get("/api/v2/submissions", state="pending")
        ck(st == 200 and any(s["submission_id"] == sid for s in q["submissions"]),
           "待审队列里能看到它")
        st, det = cli.get(f"/api/v2/submission/{sid}")
        ck(st == 200 and (det.get("thumb") or {}).get("base") == f"/thumb/{cur['packet_id']}",
           "submission 详情回 thumb.base")
        ck(len(det.get("frames") or []) == len(det["submission"]["labels"]),
           "frames 由 labels 现算",
           f"{len(det.get('frames') or [])} vs {len(det['submission']['labels'])}")
        ck(det["packet"]["meta"]["roi"] == cur["meta"]["roi"], "详情带 packet meta")
        ck(det["packet"].get("produced_at") and det["packet"].get("labeled_at"),
           "详情带生产/标注时间戳")

        # ---- 审核：逐帧打回
        print("\n[7/9] 审核与打回", flush=True)
        labels = det["submission"]["labels"]
        reject = [labels[3]["frame_idx"], labels[7]["frame_idx"]]
        st, rv = cli.post(f"/api/v2/review/{sid}", {
            "reviewer": "smoke-reviewer", "decision": "partial",
            "frames": {str(f): False for f in reject}, "notes": "冒烟：打回两帧"})
        ck(st == 200 and rv["n_rejected"] == 2 and rv["packet_state"] == "need_human",
           "逐帧打回 → 包转 need_human", json.dumps(rv, ensure_ascii=False))
        ck((det.get("frames") or [{}])[3].get("pano_x") is not None,
           "详情 frames 带 pano 坐标（canvas 叠加用）")
        ck(rv["n_gt_written"] == len(labels) - 2, "只有通过的帧入库")
        st, gt = cli.get(f"/api/v2/gt/{cur['packet_id']}")
        ck(len(gt["gt"]) == len(labels) - 2 and all(g["source"] == "ai" for g in gt["gt"]),
           "gt 表里是 AI 来源的通过帧")
        ck(all(g["frame_idx"] not in reject for g in gt["gt"]), "被打回的帧没入库")

        st, r = cli.post("/api/v2/lease", {"worker": "human-1", "kind": "repair"})
        rep = (r or {}).get("lease") or {}
        ck(sorted(rep.get("target_frames") or []) == sorted(reject),
           "repair 租约只发被打回的帧", str(rep.get("target_frames")))
        ck(len(rep.get("prior_labels") or []) == 2,
           "repair 带上 AI 的答案作为初值（人工只修这几帧）")

        fixed = {"packet_id": cur["packet_id"], "annotator": "human-1", "fps": 30.0,
                 "n_targets": len(labels),
                 "labels": [{"frame_idx": f, "pano_x": 3000.0 + i, "pano_y": 1500.0 + i,
                             "visible": True, "occluded": False, "blurred": False}
                            for i, f in enumerate(reject)]}
        st, sub2 = cli.post("/api/v2/submit", {
            "packet_id": cur["packet_id"], "source": "human", "lease_id": rep["lease_id"],
            "result": fixed}, worker="human-1")
        ck(st == 200 and sub2["state"] == "accepted" and sub2["n_gt_written"] == 2,
           "人工提交免审、直接入库")
        st, gt = cli.get(f"/api/v2/gt/{cur['packet_id']}")
        by_src = {g["frame_idx"]: g["source"] for g in gt["gt"]}
        ck(len(gt["gt"]) == len(labels), "修补后这个包的帧齐了", f"{len(gt['gt'])}/{len(labels)}")
        ck(all(by_src.get(f) == "human" for f in reject),
           "修补的帧覆盖成了人工来源（upsert 没把 AI 帧删掉）")
        st, pk = cli.get("/api/v2/packets", state="accepted")
        ck(any(p["packet_id"] == cur["packet_id"] for p in pk["packets"]),
           "打回帧修完后包转 accepted")

        # ---- 另一个包：人工整包提交
        print("\n[8/9] 人工整包提交", flush=True)
        st, r = cli.post("/api/v2/lease", {"worker": "human-2", "kind": "human"})
        lh = (r or {}).get("lease") or {}
        ck(lh.get("packet_id") == other["packet_id"], "人工领到另一个包")
        res = dict(other["result"])
        res["annotator"] = "human-2"
        st, sub3 = cli.post("/api/v2/submit", {
            "packet_id": other["packet_id"], "source": "human", "lease_id": lh["lease_id"],
            "result": res}, worker="human-2")
        ck(st == 200 and sub3["n_gt_written"] == len(res["labels"]),
           "人工整包提交一次入库", f"{sub3['n_gt_written']} 帧")
        sid_h = sub3["submission_id"]
        st, queued = cli.get("/api/v2/submissions?state=accepted&source=human")
        ck(st == 200 and any(x["submission_id"] == sid_h for x in queued["submissions"]),
           "已入库人工提交出现在审核队列")
        reject_one = int(res["labels"][0]["frame_idx"])
        st, rv2 = cli.post(f"/api/v2/review/{sid_h}", {
            "reviewer": "qc-1", "decision": "partial",
            "frames": {str(reject_one): False}, "notes": "再审已入库人工包"})
        ck(st == 200 and rv2.get("n_rejected") == 1 and rv2["packet_state"] == "need_human",
           "已入库再审打回 → 包转 need_human", json.dumps(rv2, ensure_ascii=False))
        ck(int(rv2.get("n_gt_removed") or 0) == 1, "打回帧从 GT 删掉")
        st, gt = cli.get(f"/api/v2/gt/{other['packet_id']}")
        ck(all(g["frame_idx"] != reject_one for g in gt["gt"]), "打回帧已不在 gt")
        ck(gt["gt"] and all(g["source"] == "human" for g in gt["gt"]),
           "留下的仍是人工来源")
        st, rv3 = cli.post(f"/api/v2/review/{sid_h}", {"reviewer": "qc-1", "decision": "accept"})
        ck(st == 200 and rv3["packet_state"] == "accepted"
           and rv3["n_gt_written"] == len(res["labels"]),
           "再审整包通过 → 包回到 accepted，GT 写回")

        # ---- 统计
        print("\n[9/9] 看板统计与导出", flush=True)
        st, stats = cli.get("/api/v2/stats", hours=24, buckets=24)
        rr = stats.get("review_rate") or []
        ck(st == 200 and rr and rr[0]["frame_pass_rate"] is not None,
           "stats 给出按模型/工作流的帧级通过率",
           f"{rr[0]['model_used']} / {rr[0]['workflow']}: 帧级 {rr[0]['frame_pass_rate']}, "
           f"包级 {rr[0]['packet_pass_rate']}")
        ck(stats["gt"]["n_ai"] > 0 and stats["gt"]["n_human"] > 0,
           "gt 里 AI 与人工帧都有", json.dumps(stats["gt"], ensure_ascii=False))
        ck(len(stats["throughput"]["registered"]) == 24 and sum(stats["throughput"]["registered"]) == 3,
           "吞吐曲线分桶正确（含已归档的测试包）")
        ck(len(stats["pool_series"]) == 24, "池深曲线有数据（重放注册与领取事件算出来的）")
        st, pk = cli.get("/api/v2/packets", state="all")
        ck(st == 200 and pk["total"] == 3 and all(p["qc_flags"] is not None for p in pk["packets"]),
           "包列表带 QC 标记与覆盖率（含已归档）")

        # 脏提交放在最后：它会把 other 拨回 submitted，前面的状态断言必须已经跑完。
        # 「可见但没有坐标」必须在提交入口就降级——qc() 对这种行会 TypeError 让接口 500，
        # 万一漏进 gt，export_gt.py 之后会在同一处炸，那时已经查不出是哪个客户端塞的了
        st, dirty = cli.post("/api/v2/submit", {
            "packet_id": other["packet_id"], "source": "ai", "workflow": "erp_ball_v1",
            "model_used": "dirty-test",
            "result": {"labels": [
                {"frame_idx": 900, "visible": True, "pano_x": None, "pano_y": None},
                {"frame_idx": 906, "visible": True, "pano_x": 1000.0, "pano_y": 500.0},
                {"frame_idx": 903, "visible": True, "pano_x": 1010.0, "pano_y": 505.0},
                {"frame_idx": 903, "visible": False, "pano_x": None, "pano_y": None},
            ]}}, worker="dirty-1")
        ck(st == 200 and dirty.get("n_degraded") == 1 and dirty.get("n_labeled") == 3,
           "「可见但无坐标」被降级、重复帧号被去重，qc() 没崩",
           f"n_degraded={dirty.get('n_degraded')} n_labeled={dirty.get('n_labeled')}")
        if dirty.get("submission_id"):
            st, ddet = cli.get(f"/api/v2/submission/{dirty['submission_id']}")
            got = [r["frame_idx"] for r in ddet["submission"]["labels"]]
            ck(got == sorted(got), "labels 入库前按帧号排好序（qc 的速度统计靠相邻两行）", str(got))

        export = REPO / "scripts" / "export_gt.py"
        if export.exists():
            out_dir = work / "collected"
            p = subprocess.run([py, str(export), "--db", str(db_path), "--out-dir", str(out_dir),
                                "--jsonl", str(work / "gt.jsonl")],
                               cwd=str(REPO), capture_output=True, text=True)
            files = sorted(f.name for f in out_dir.glob("*.json")) if out_dir.exists() else []
            ck(p.returncode == 0 and (out_dir / "index.json").exists(),
               "export_gt.py 从 gt 表导出成功", f"{len(files)} 个文件 " + (p.stderr[-300:] if p.returncode else ""))
            if (work / "gt.jsonl").exists():
                n = sum(1 for _ in open(work / "gt.jsonl"))
                ck(n == stats["gt"]["n_frames"], "JSONL 摊平帧数与 gt 表一致", f"{n} 行")
        else:
            print("  · scripts/export_gt.py 还没写好，跳过导出检查", flush=True)

        if args.keep_alive:
            print(f"\n[keep-alive] 服务端还在跑：http://127.0.0.1:{port}/"
                  f"\n  看板 http://127.0.0.1:{port}/   审核 http://127.0.0.1:{port}/review"
                  f"\n  库 {db_path}\n  Ctrl-C 结束", flush=True)
            try:
                proc.wait()
            except KeyboardInterrupt:
                pass
    finally:
        if not args.keep_alive:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    rc = ck.report()
    if rc:
        print(f"\n[log] 服务端日志：{log}", flush=True)
        tail = log.read_text(errors="replace").splitlines()[-25:]
        print("\n".join("  " + t for t in tail), flush=True)
    if args.work is None and not args.keep and not rc:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"\n[keep] 工作目录留着了：{work}", flush=True)
    return rc


def parse_args():
    p = argparse.ArgumentParser(description="服务端 v2 冒烟测试（不涉及 AI 客户端与 AOSS）")
    p.add_argument("--work", default=None, help="工作目录，默认临时目录（跑过就删）")
    p.add_argument("--keep", action="store_true", help="通过也保留工作目录")
    p.add_argument("--keep-alive", action="store_true", help="跑完不关服务端，好用浏览器看页面")
    p.add_argument("--keep-available", type=int, default=40, help="传给服务端的池子目标数")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
