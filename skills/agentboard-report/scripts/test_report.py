#!/usr/bin/env python3
"""Dry-run checks for report.py scenario / run_key / log.append behavior."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = Path(__file__).resolve().parent / "report.py"


def run_report(*args: str, extra_env: dict[str, str] | None = None) -> dict:
    env = os.environ.copy()
    env.update(
        {
            "AGENTBOARD_PROVIDER": "cursor",
            "AGENTBOARD_TOKEN": "abp_m_test",
            "AGENTBOARD_SOFT_FAIL": "1",
        }
    )
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--dry-run"],
        cwd=str(ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


class ReportScenarios(unittest.TestCase):
    def types(self, body: dict) -> list[str]:
        return [e["event_type"] for e in body["events"]]

    def test_project_start_new_run_and_log(self):
        body = run_report(
            "start",
            "实现 M7",
            extra_env={
                "AGENTBOARD_SCENARIO": "project",
                "CURSOR_CONVERSATION_ID": "11111111-2222-3333-4444-555555555555",
                "CURSOR_CLOUD_AGENT": "",
            },
        )
        types = self.types(body)
        self.assertIn("log.append", types)
        self.assertIn("run.transition", types)
        run = next(e for e in body["events"] if e["event_type"] == "run.transition")
        self.assertRegex(run["run_key"], r"^[0-9a-f-]{36}$")
        self.assertNotEqual(run["run_key"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(run["payload"]["metadata"]["conversation_id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(run["payload"]["status"], "running")
        svc = next(e for e in body["events"] if e["event_type"] == "service.state")
        self.assertEqual(svc["service_key"], "cursor")
        self.assertEqual(svc["payload"]["summary"], "")

    def test_each_start_mints_a_new_run_key(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cid = "cccccccccccccccc-dddd-eeee-ffff-000000000001"
            extra = {
                "AGENTBOARD_SCENARIO": "project",
                "AGENTBOARD_STATE_DIR": td,
                "CURSOR_CONVERSATION_ID": cid,
                "CURSOR_CLOUD_AGENT": "",
            }
            first = run_report("start", "任务一", extra_env=extra)
            second = run_report("start", "任务二", extra_env=extra)
            k1 = next(e for e in first["events"] if e["event_type"] == "run.transition")["run_key"]
            k2 = next(e for e in second["events"] if e["event_type"] == "run.transition")["run_key"]
            self.assertNotEqual(k1, k2)
            third = run_report("succeed", "任务二完成", extra_env=extra)
            k3 = next(e for e in third["events"] if e["event_type"] == "run.transition")["run_key"]
            self.assertEqual(k3, k2)

    def test_local_client_does_not_hijack_identity(self):
        body = run_report(
            "start",
            "改 ingest",
            extra_env={
                "AGENTBOARD_SCENARIO": "board-client",
                "CURSOR_CONVERSATION_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "CURSOR_CLOUD_AGENT": "",
            },
        )
        svc = next(e for e in body["events"] if e["event_type"] == "service.state")
        self.assertEqual(svc["service_key"], "cursor")
        self.assertNotIn("workspace", svc["payload"].get("metadata") or {})
        types = self.types(body)
        self.assertNotIn("status.upsert", types)

    def test_project_heartbeat_omits_telemetry_status(self):
        body = run_report(
            "heartbeat",
            extra_env={"AGENTBOARD_SCENARIO": "project", "CURSOR_CLOUD_AGENT": ""},
        )
        types = self.types(body)
        self.assertNotIn("status.upsert", types)

    def test_cloud_service_and_succeed_log(self):
        body = run_report(
            "succeed",
            "已完成",
            extra_env={
                "AGENTBOARD_SCENARIO": "cloud",
                "CURSOR_CONVERSATION_ID": "99999999-8888-7777-6666-555555555555",
                "CURSOR_CLOUD_AGENT": "1",
            },
        )
        types = self.types(body)
        self.assertIn("log.append", types)
        svc = next(e for e in body["events"] if e["event_type"] == "service.state")
        self.assertTrue(svc["service_key"].startswith("cloud-"))
        run = next(e for e in body["events"] if e["event_type"] == "run.transition")
        self.assertEqual(run["payload"]["status"], "succeeded")

    def test_fail_does_not_mark_service_failed(self):
        body = run_report(
            "fail",
            "测试失败",
            extra_env={
                "AGENTBOARD_SCENARIO": "project",
                "CURSOR_CONVERSATION_ID": "12121212-3434-5656-7878-909090909090",
            },
        )
        states = [e for e in body["events"] if e["event_type"] == "service.state"]
        self.assertTrue(states)
        self.assertNotEqual(states[0]["payload"]["state"], "failed")
        run = next(e for e in body["events"] if e["event_type"] == "run.transition")
        self.assertEqual(run["payload"]["status"], "failed")

    def test_interrupt_prefixes_message(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            extra = {
                "AGENTBOARD_SCENARIO": "project",
                "AGENTBOARD_STATE_DIR": td,
                "CURSOR_CONVERSATION_ID": "abababab-1111-2222-3333-444444444444",
            }
            start = run_report("start", "正在改上报", extra_env=extra)
            rk = next(e for e in start["events"] if e["event_type"] == "run.transition")["run_key"]
            body = run_report("interrupt", "用户停止", extra_env=extra)
            run = next(e for e in body["events"] if e["event_type"] == "run.transition")
            self.assertEqual(run["run_key"], rk)
            self.assertEqual(run["payload"]["status"], "failed")
            self.assertEqual(run["payload"]["summary"], "任务被打断：用户停止")
            log = next(e for e in body["events"] if e["event_type"] == "log.append")
            self.assertEqual(log["payload"]["markdown"], "任务被打断：用户停止")
            states = [e for e in body["events"] if e["event_type"] == "service.state"]
            self.assertTrue(states)
            self.assertNotEqual(states[0]["payload"]["state"], "failed")

    def test_interrupt_without_run_is_empty(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            body = run_report(
                "interrupt",
                "无任务",
                extra_env={
                    "AGENTBOARD_SCENARIO": "project",
                    "AGENTBOARD_STATE_DIR": td,
                    "CURSOR_CONVERSATION_ID": "ffffffffffff-0000-1111-2222-333333333333",
                },
            )
            self.assertEqual(body.get("events"), [])

    def test_interrupt_keeps_existing_prefix(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            extra = {
                "AGENTBOARD_SCENARIO": "project",
                "AGENTBOARD_STATE_DIR": td,
                "CURSOR_CONVERSATION_ID": "cdcdeeee-1111-2222-3333-444444444555",
            }
            run_report("start", "任务", extra_env=extra)
            body = run_report("interrupt", "任务被打断：会话超时", extra_env=extra)
            run = next(e for e in body["events"] if e["event_type"] == "run.transition")
            self.assertEqual(run["payload"]["summary"], "任务被打断：会话超时")

    def test_progress_updates_run_summary(self):
        body = run_report(
            "progress",
            "已写完投影",
            extra_env={
                "AGENTBOARD_SCENARIO": "project",
                "CURSOR_CONVERSATION_ID": "abababab-cdcd-efef-aaaa-bbbbbbbbbbbb",
            },
        )
        types = self.types(body)
        self.assertEqual(types.count("log.append"), 1)
        self.assertEqual(types.count("run.transition"), 1)
        run = next(e for e in body["events"] if e["event_type"] == "run.transition")
        self.assertEqual(run["payload"]["status"], "running")
        self.assertEqual(run["payload"]["summary"], "已写完投影")
        self.assertNotIn("started_at", run["payload"])

    def test_heartbeat_empty_summary(self):
        body = run_report(
            "heartbeat",
            extra_env={"AGENTBOARD_SCENARIO": "project", "CURSOR_CLOUD_AGENT": ""},
        )
        svc = next(e for e in body["events"] if e["event_type"] == "service.state")
        self.assertEqual(svc["payload"]["summary"], "")


class HostProjectCopy(unittest.TestCase):
    def test_stamp_does_not_mutate_original(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import report as reportmod  # noqa: E402

        events = [{"event_type": "log.append", "event_id": "old", "payload": {"markdown": "x"}}]
        out = reportmod.stamp_host_project(events, "/repo/demo", "demo")
        self.assertEqual(out[0]["payload"]["metadata"]["workspace"], "/repo/demo")
        self.assertEqual(out[0]["payload"]["metadata"]["project"], "demo")
        self.assertNotEqual(out[0]["event_id"], "old")
        self.assertNotIn("metadata", events[0]["payload"])

    def test_find_project_root_skips_parent_cursor_only(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import report as reportmod  # noqa: E402
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "wangmin"
            nested = home / "code" / "app"
            nested.mkdir(parents=True)
            (home / ".cursor").mkdir()
            old = os.getcwd()
            os.chdir(nested)
            try:
                root = reportmod.find_project_root()
            finally:
                os.chdir(old)
            self.assertEqual(root.resolve(), nested.resolve())

    def test_find_project_root_uses_marker(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import report as reportmod  # noqa: E402
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "my-ai-dashboard"
            sub = proj / "internal"
            sub.mkdir(parents=True)
            (proj / ".cursor").mkdir()
            (proj / "go.mod").write_text("module x\n", encoding="utf-8")
            old = os.getcwd()
            os.chdir(sub)
            try:
                root = reportmod.find_project_root()
            finally:
                os.chdir(old)
            self.assertEqual(root.resolve(), proj.resolve())

    def test_local_tee_candidate_ignores_non_url_override(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import report as reportmod  # noqa: E402

        self.assertEqual(
            reportmod.local_tee_candidate("http://127.0.0.1:7438", "tee", "1"),
            "http://127.0.0.1:7438",
        )
        self.assertEqual(
            reportmod.local_tee_candidate("http://127.0.0.1:7438", "tee", "http://127.0.0.1:9"),
            "http://127.0.0.1:9",
        )
        self.assertEqual(reportmod.local_tee_candidate("http://127.0.0.1:7438", "", "http://127.0.0.1:9"), "")


class ProviderInfer(unittest.TestCase):
    def test_infer_claude_hermes_pi(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import report as reportmod  # noqa: E402

        old = os.environ.copy()
        try:
            for k in list(os.environ):
                if k.startswith(("AGENTBOARD_", "OPENCLAW_", "CODEX_", "CURSOR_", "CLAUDE", "HERMES_", "PI_")):
                    os.environ.pop(k, None)
            os.environ["CLAUDECODE"] = "1"
            self.assertEqual(reportmod.infer_provider(), "claude")
            os.environ.pop("CLAUDECODE")
            os.environ["HERMES_HOME"] = "/tmp/hermes"
            self.assertEqual(reportmod.infer_provider(), "hermes")
            os.environ.pop("HERMES_HOME")
            os.environ["PI_HOME"] = "/tmp/pi"
            self.assertEqual(reportmod.infer_provider(), "pi")
            key, name = reportmod.provider_service_defaults("claude")
            self.assertEqual(key, "claude")
            self.assertEqual(name, "Claude Code")
        finally:
            os.environ.clear()
            os.environ.update(old)


class LocalTeeWithoutToken(unittest.TestCase):
    def test_no_token_still_tees_when_advertise_tee(self):
        import http.server
        import tempfile
        import threading

        received: list[bytes] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or "0")
                received.append(self.rfile.read(n))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *_args):
                return

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                adv = Path(td) / "local-ingest.json"
                adv.write_text(
                    json.dumps({"url": f"http://127.0.0.1:{port}", "mode": "tee"}),
                    encoding="utf-8",
                )
                env = os.environ.copy()
                env.update(
                    {
                        "AGENTBOARD_PROVIDER": "cursor",
                        "AGENTBOARD_TOKEN": "",
                        "AGENTBOARD_SOFT_FAIL": "1",
                        "AGENTBOARD_LOCAL_INGEST_FILE": str(adv),
                        "AGENTBOARD_STATE_DIR": td,
                        "CURSOR_CONVERSATION_ID": "tee-no-token-0000-1111-2222-333333333333",
                    }
                )
                proc = subprocess.run(
                    [sys.executable, str(SCRIPT), "start", "仅本机 ingest"],
                    cwd=str(ROOT),
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 0)
                self.assertIn("local tee only", proc.stderr)
                self.assertTrue(received, proc.stderr)
                body = json.loads(received[0])
                types = [e["event_type"] for e in body["events"]]
                self.assertIn("run.transition", types)
                meta = body["events"][0]["payload"].get("metadata") or {}
                self.assertIn("workspace", meta)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
