#!/usr/bin/env python3
"""产包端挂钩 + collector_agent + 补登 的冒烟（不提交 ACP、不碰 AOSS、不跑 ffmpeg）。

    ./venv/bin/python scripts/smoke_producer.py

覆盖：
  * 服务端心跳 want_more=3 → 本轮产 3 个
  * 服务端超时 → 退回 --keep-available（再被 batch 封顶）
  * 上传成功后打 /register 与 /producer/event kind=produced
  * collector_agent 的 ACP 可 mock：FAILED / gone / stuck / 自动重提判定
  * register_packets 组包与幂等 POST
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from packet import PacketMeta  # noqa: E402
from producer.make_packets import Reporter, decide_need  # noqa: E402
from producer.server_api import ServerAPI, resolve_job_id  # noqa: E402
from scripts.collector_agent import (  # noqa: E402
    FakeAcpBackend,
    classify,
    is_producer_job,
    poll_once,
    should_resubmit,
)
from scripts.register_packets import build_item  # noqa: E402


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
            for ok, name, detail in self.rows:
                if not ok:
                    print(f"    - {name}  {detail}", flush=True)
        return 1 if self.n_fail else 0


# ---------------------------------------------------------------- mock HTTP


class MockState:
    def __init__(self) -> None:
        self.want_more = 3
        self.delay = 0.0
        self.reqs: List[Tuple[str, str, Any]] = []
        self.n_available = 1


def _handler(state: MockState):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A003
            return

        def _read_json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                return json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                return {}

        def _send(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except BrokenPipeError:
                pass

        def do_GET(self):  # noqa: N802
            if state.delay:
                time.sleep(state.delay)
            path = urllib.parse.urlparse(self.path).path
            state.reqs.append(("GET", path, {}))
            if path == "/api/v2/pool":
                self._send(200, {
                    "n_available": state.n_available, "n_leased": 0,
                    "n_pending_review": 0, "want_more": state.want_more,
                    "batch": 10, "keep_available": 40,
                })
            elif path == "/api/v2/producers":
                self._send(200, {"producers": []})
            else:
                self._send(404, {"error": "no"})

        def do_POST(self):  # noqa: N802
            if state.delay:
                time.sleep(state.delay)
            path = urllib.parse.urlparse(self.path).path
            body = self._read_json()
            state.reqs.append(("POST", path, body))
            if path == "/api/v2/producer/heartbeat":
                self._send(200, {
                    "ok": True, "health": "running",
                    "want_more": state.want_more,
                    "pool": {"n_available": state.n_available, "want_more": state.want_more},
                })
            elif path == "/api/v2/producer/register":
                self._send(200, {"ok": True, "job_id": body.get("job_id"), "new": True})
            elif path == "/api/v2/producer/event":
                self._send(200, {"ok": True, "event_id": 1})
            elif path == "/api/v2/register":
                n = len(body.get("packets") or [body])
                self._send(200, {"registered": n, "updated": 0, "errors": []})
            else:
                self._send(404, {"error": "no"})

    return H


def start_mock(state: MockState) -> Tuple[ThreadingHTTPServer, str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _handler(state))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{port}"


def ns(**kw):
    return argparse.Namespace(
        poll_sec=kw.get("poll_sec", 300),
        keep_available=kw.get("keep_available", 40),
        batch=kw.get("batch", 10),
    )


def fake_meta(pid: str) -> PacketMeta:
    return PacketMeta(
        packet_id=pid, group="g", stem="s", tag="0010_0020",
        start_sec=10, end_sec=20, fps=30.0, pano_size=[7680, 3840],
        roi=[0, 0, 100, 100], frame_start=300, frame_end=599,
        clip_frames=300, step=3, targets=[300, 303, 306],
    )


# ---------------------------------------------------------------- 用例


def test_want_more_and_fallback(ck: Checks) -> None:
    print("\n[1] make_packets 补池：want_more 与超时兜底", flush=True)
    state = MockState()
    state.want_more = 3
    httpd, base = start_mock(state)
    try:
        os.environ["BALL_GT_SERVER"] = base
        os.environ.pop("BALL_GT_API_TOKEN", None)
        api = ServerAPI(base=base, timeout=2.0)
        args = ns(keep_available=40, batch=10)
        rep = Reporter(api, "pt-smoke", args)
        produced: List[int] = []

        def produce_fn(n: int) -> int:
            produced.append(n)
            return n

        need = decide_need(rep, args)
        if need > 0:
            produce_fn(min(need, args.batch))
        ck(need == 3 and produced == [3], "服务端 want_more=3 时本轮产 3 个", f"need={need} produced={produced}")
        ck(any(p == "/api/v2/producer/heartbeat" for _, p, _ in state.reqs),
           "主循环打了作业内心跳")

        state.reqs.clear()
        state.delay = 1.2
        api_slow = ServerAPI(base=base, timeout=0.3)
        args2 = ns(keep_available=7, batch=10)
        rep2 = Reporter(api_slow, "pt-smoke", args2)
        produced2: List[int] = []
        need2 = decide_need(rep2, args2)
        if need2 > 0:
            produced2.append(min(need2, args2.batch))
        ck(need2 == 7 and produced2 == [7],
           "服务端超时时退回 keep-available（7，未被 batch 截断）",
           f"need={need2} produced={produced2} err={api_slow.last_error}")

        args3 = ns(keep_available=40, batch=10)
        rep3 = Reporter(api_slow, "pt-smoke", args3)
        need3 = decide_need(rep3, args3)
        ck(need3 == 10, "超时 + keep=40 时本轮最多 batch=10", f"need={need3}")
    finally:
        httpd.shutdown()
        os.environ.pop("BALL_GT_SERVER", None)


def test_register_on_produced(ck: Checks) -> None:
    print("\n[2] 上传成功后 register + produced 事件", flush=True)
    state = MockState()
    httpd, base = start_mock(state)
    try:
        api = ServerAPI(base=base, timeout=2.0)
        rep = Reporter(api, "pt-smoke", ns())
        meta = fake_meta("grp__stem__0010_0020")
        rep.produced(meta.packet_id, meta, size_bytes=12345, sampling="active")
        paths = [p for _, p, _ in state.reqs]
        ck("/api/v2/register" in paths, "POST /api/v2/register")
        ck("/api/v2/producer/event" in paths, "POST /api/v2/producer/event")
        ev = next(b for m, p, b in state.reqs if p.endswith("/event"))
        ck(ev.get("kind") == "produced" and ev.get("msg") == meta.packet_id,
           "event kind=produced 带 packet_id")
        reg = next(b for m, p, b in state.reqs if p.endswith("/register") and "packet_id" in b)
        ck(reg.get("producer_job_id") == "pt-smoke" and reg.get("aoss_key") == f"packets/{meta.packet_id}.zip",
           "register 带 aoss_key 与 producer_job_id")
        ck(isinstance(reg.get("meta"), dict) and reg["meta"].get("targets"),
           "register 带 meta.targets")
    finally:
        httpd.shutdown()


def test_job_id(ck: Checks) -> None:
    print("\n[3] job_id 解析", flush=True)
    old = {k: os.environ.pop(k, None) for k in
           ("BALL_GT_JOB_ID", "ACP_JOB_ID", "BALL_GT_JOB_ID_FILE", "BALL_GT_JOB_ENV_FILE")}
    try:
        os.environ["BALL_GT_JOB_ID"] = "pt-from-env"
        ck(resolve_job_id() == "pt-from-env", "BALL_GT_JOB_ID 优先")
        os.environ.pop("BALL_GT_JOB_ID")
        os.environ["ACP_JOB_ID"] = "pt-acp"
        ck(resolve_job_id() == "pt-acp", "ACP_JOB_ID 次之")
        os.environ.pop("ACP_JOB_ID")
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("BALL_GT_JOB_ID=pt-sidecar\n")
            path = f.name
        os.environ["BALL_GT_JOB_ENV_FILE"] = path
        ck(resolve_job_id() == "pt-sidecar", "sidecar 文件")
        os.environ.pop("BALL_GT_JOB_ENV_FILE")
        Path(path).unlink(missing_ok=True)
        got = resolve_job_id()
        ck("-" in got and got.split("-")[-1].isdigit(), "没有环境变量时用 hostname-pid", got)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_collector(ck: Checks) -> None:
    print("\n[4] collector_agent（ACP mock）", flush=True)
    ck(classify("FAILED", None, 300) == "failed", "ACP FAILED → failed")
    ck(classify(None, None, 300) == "gone", "查不到 → gone")
    ck(classify("RUNNING", None, 300) == "stuck", "RUNNING 但无进程心跳 → stuck")
    ck(classify("RUNNING", 700, 300) == "stale", "心跳超过 2×poll_sec → stale")
    ck(classify("RUNNING", 10, 300) == "running", "心跳新鲜 → running")
    ck(is_producer_job({"name": "pt-1", "display_name": "ball_gt_produce"}),
       "筛 display_name=ball_gt_produce")
    ck(not is_producer_job({"name": "pt-other", "display_name": "train-foo"}),
       "不把别的 pt-* 训练作业算进产包")

    work = Path(tempfile.mkdtemp(prefix="ball_gt_coll_"))
    fake = work / "acp.json"
    fake.write_text(json.dumps({"jobs": [
        {"name": "pt-live", "state": "RUNNING", "display_name": "ball_gt_produce",
         "workers": {"workers": [{"node": "node-a"}]}},
        {"name": "pt-dead", "state": "FAILED", "display_name": "ball_gt_produce"},
    ]}))
    (work / "ball_gt_produce_20260101_000000.log").write_text(
        "[ok] x\n[fail] 切片失败 abc\n"
    )
    state = MockState()
    httpd, base = start_mock(state)
    try:
        api = ServerAPI(base=base, timeout=2.0)
        acp = FakeAcpBackend(fake)
        snap = poll_once(acp, api, poll_sec=300, log_dir=work, job_name="ball_gt_produce",
                         known_ids={"pt-gone-old"})
        ids = {j["job_id"]: j for j in snap["jobs"]}
        ck("pt-live" in ids and ids["pt-dead"]["acp_state"] == "FAILED",
           "list/get 筛出产包作业")
        ck("pt-gone-old" in snap["gone"], "ACP 查不到的已知 pt-* 标 gone")
        ck(ids["pt-live"].get("node") == "node-a", "workers 取出 node")
        ck("切片失败" in (ids["pt-live"].get("last_error") or ""),
           "tail 日志抓到 [fail] 行")
        hb_jobs = {b.get("job_id") for m, p, b in state.reqs if p.endswith("/heartbeat")}
        ck("pt-live" in hb_jobs and "pt-dead" in hb_jobs,
           "作业级视角 POST /producer/heartbeat")
        ck(not any("CLUSTER_ACCESS" in json.dumps(b) for *_, b in state.reqs),
           "心跳正文不含 ACP key")

        pool_empty = {"n_available": 0}
        pool_ok = {"n_available": 12}
        live_snap = {"live": ["pt-live"]}
        empty_snap = {"live": []}
        ck(should_resubmit(pool_empty, empty_snap), "池子见底 + 无存活作业 → 该重提")
        ck(not should_resubmit(pool_empty, live_snap), "还有存活作业不重提")
        ck(not should_resubmit(pool_ok, empty_snap), "池子没见底不重提")
    finally:
        httpd.shutdown()

    state2 = MockState()
    httpd2, base2 = start_mock(state2)
    try:
        p = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "collector_agent.py"),
             "--once", "--fake-acp", str(fake), "--log-dir", str(work),
             "--server", base2],
            cwd=str(REPO), capture_output=True, text=True,
        )
        ck(p.returncode == 0, "collector_agent --once --fake-acp 退出 0",
           (p.stderr or p.stdout)[-200:])
    finally:
        httpd2.shutdown()


def test_register_packets(ck: Checks) -> None:
    print("\n[5] register_packets 组包", flush=True)
    meta = {"packet_id": "g__s__0010_0020", "targets": [1, 2, 3], "group": "g", "stem": "s"}
    item = build_item("g__s__0010_0020", meta, 999, "pt-x", "node-1")
    ck(item is not None and item["n_targets"] == 3 and item["aoss_key"].endswith(".zip"),
       "有 targets 的 meta 能组出 register 项")
    ck(build_item("bad", {"targets": []}, 1, "", "") is None, "没有 targets 就跳过")

    state = MockState()
    httpd, base = start_mock(state)
    try:
        api = ServerAPI(base=base, timeout=2.0)
        r = api.register_packets([item, {**item, "packet_id": "g__s__0020_0030"}])
        ck(r is not None and r.get("registered") == 2, "批量 POST /api/v2/register", str(r))
        r2 = api.register_packets([item])
        ck(r2 is not None and r2.get("registered") == 1, "单条同样走 register（幂等由服务端保证）")
    finally:
        httpd.shutdown()


def test_submit_sh(ck: Checks) -> None:
    print("\n[6] submit.sh 语法与 dry-run", flush=True)
    sh = REPO / "producer" / "submit.sh"
    p = subprocess.run(["bash", "-n", str(sh)], capture_output=True, text=True)
    ck(p.returncode == 0, "submit.sh bash -n", p.stderr)
    env = dict(os.environ)
    env["BALL_GT_SERVER"] = "http://example.invalid"
    p = subprocess.run([str(sh), "--dry-run", "--once", "2"], cwd=str(REPO),
                       capture_output=True, text=True, env=env)
    ck(p.returncode == 0 and "DRY-RUN" in (p.stdout + p.stderr),
       "submit.sh --dry-run 不提交 ACP", (p.stdout + p.stderr)[-180:])
    ck("BALL_GT_JOB_ENV_FILE=" in (p.stdout + p.stderr) or "server:" in (p.stdout + p.stderr).lower(),
       "dry-run 命令行带 sidecar / server 提示")


def main() -> int:
    ck = Checks()
    test_want_more_and_fallback(ck)
    test_register_on_produced(ck)
    test_job_id(ck)
    test_collector(ck)
    test_register_packets(ck)
    test_submit_sh(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
