#!/usr/bin/env python3
"""Send AgentBoard ingest events to https://board.yinger650.com (or AGENTBOARD_URL).

Uses AGENTBOARD_TOKEN (the skill / virtual-machine key). Independent of
board-client on the same host: that process reports the physical machine
with its own token, including proj-* copies of local workspace activity.

If AGENTBOARD_TOKEN is unset, remote ingest is skipped. When a local
board-client advertise file has mode=tee, events are still copied to
loopback for proj-* projection. Agents must not fail the user task.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = "https://board.yinger650.com"
DEFAULT_TTL = 180
SERVICE_KEY_RE = re.compile(r"^[a-z0-9._-]{1,64}$")


def _apply_env_file(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def load_dotenv() -> None:
    """Load nearest .env. Existing process env wins.

    Prefer the consuming project's .env (cwd / parents) so a skill
    symlinked into another repo does not pick up the skill repo's .env
    via Path(__file__).resolve().
    """
    candidates: list[Path] = []
    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        candidates.append(parent / ".env")
        skill_here = (parent / "skills" / "agentboard-report").is_dir()
        cursor_skill = (parent / ".cursor" / "skills" / "agentboard-report").is_dir()
        if skill_here or cursor_skill:
            break
    here = Path(__file__).resolve()
    if len(here.parents) >= 3:
        candidates.append(here.parents[3] / ".env")
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        _apply_env_file(resolved)
        return


load_dotenv()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def infer_provider() -> str:
    if env("AGENTBOARD_PROVIDER"):
        return env("AGENTBOARD_PROVIDER")
    if env("OPENCLAW_HOME") or env("OPENCLAW_STATE_DIR") or env("OPENCLAW_PROFILE"):
        return "openclaw"
    if env("CLAUDECODE") or env("CLAUDE_CODE") or env("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if env("HERMES_HOME") or env("HERMES_PROFILE") or env("HERMES_AGENT"):
        return "hermes"
    if env("PI_HOME") or env("PI_CODING_AGENT") or env("PI_AGENT_DIR"):
        return "pi"
    if env("CODEX_HOME") or env("CODEX_THREAD_ID"):
        return "codex"
    if env("CURSOR_AGENT") or env("CURSOR_TRACE_ID") or env("CURSOR_CLOUD_AGENT"):
        return "cursor"
    return "agent"


def is_openclaw(provider: str) -> bool:
    return provider == "openclaw"


def is_cloud_agent() -> bool:
    v = env("CURSOR_CLOUD_AGENT").lower()
    return bool(v) and v not in ("0", "false", "no", "off")


def slug_key(raw: str, fallback: str, max_len: int = 64) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip("-._")
    if not s:
        s = fallback
    if len(s) <= max_len and SERVICE_KEY_RE.match(s):
        return s
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    keep = max_len - len(digest) - 1
    s = (s[:keep].rstrip("-._") + "-" + digest)[:max_len]
    if not SERVICE_KEY_RE.match(s):
        return (fallback + "-" + digest)[:max_len]
    return s


def find_project_root() -> Path:
    """Nearest project directory for host-side proj-* copies.

    Prefer git root, then a .cursor/.codex directory that is cwd or also has a
    project marker. Skip a parent that only has editor config (e.g. $HOME/.cursor).
    """
    explicit = env("AGENTBOARD_WORKSPACE")
    if explicit:
        return Path(explicit)
    cwd = Path.cwd()
    try:
        cwd = cwd.resolve()
    except OSError:
        pass
    markers = (".git", "go.mod", "package.json", "pyproject.toml", "Cargo.toml", "AGENTS.md", "Makefile")
    for parent in (cwd, *cwd.parents):
        if (parent / ".git").exists():
            return parent
    for parent in (cwd, *cwd.parents):
        if not ((parent / ".cursor").is_dir() or (parent / ".codex").is_dir()):
            continue
        if parent == cwd or any((parent / m).exists() for m in markers):
            return parent
    return cwd


def workspace_info() -> tuple[str, str]:
    root = find_project_root()
    try:
        path = str(root.resolve())
    except OSError:
        path = str(root)
    return path, (root.name or "workspace")


def stamp_host_project(events: list[dict], workspace: str, project: str) -> list[dict]:
    """Copy events and attach workspace metadata for board-client proj-* projection."""
    out: list[dict] = []
    for e in events:
        item = copy.deepcopy(e)
        item["event_id"] = str(uuid.uuid4())
        payload = item.get("payload")
        if isinstance(payload, dict):
            meta = payload.get("metadata")
            if not isinstance(meta, dict):
                meta = {}
                payload["metadata"] = meta
            meta["workspace"] = workspace
            meta["project"] = project
        out.append(item)
    return out


def state_dir() -> Path:
    return Path(env("AGENTBOARD_STATE_DIR") or os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())


def advertise_paths() -> list[Path]:
    out: list[Path] = []
    explicit = env("AGENTBOARD_LOCAL_INGEST_FILE")
    if explicit:
        out.append(Path(explicit))
    out.extend(
        [
            Path("/var/lib/agentboard-client/local-ingest.json"),
            Path("/run/agentboard-client/local-ingest.json"),
            Path("/tmp/agentboard-client/local-ingest.json"),
        ]
    )
    return out


def _health_ok(base: str, timeout: float) -> bool:
    url = base.rstrip("/") + "/health"
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "agentboard-report/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:  # noqa: BLE001
        return False


def read_local_advertise() -> tuple[str, str]:
    """Return (url, mode) from the first readable advertise file."""
    for path in advertise_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        url = str(data.get("url") or "").strip().rstrip("/")
        mode = str(data.get("mode") or "").strip().lower()
        if url:
            return url, mode
    return "", ""


def local_tee_candidate(advertise_url: str, advertise_mode: str, explicit: str) -> str:
    """Pick a loopback tee URL. Refuse old clients that still forward (no mode=tee)."""
    if advertise_mode != "tee":
        return ""
    exp = (explicit or "").strip()
    if exp.lower().startswith("http://") or exp.lower().startswith("https://"):
        return exp.rstrip("/")
    return (advertise_url or "").rstrip("/")


def discover_local_tee(timeout: float = 0.4) -> str:
    """Loopback URL that copies events for host proj-* projection.

    New board-client advertise files include mode=tee. Old files that only
    have a url still forward with the client token — do not post to those.
    """
    url, mode = read_local_advertise()
    cand = local_tee_candidate(url, mode, env("AGENTBOARD_LOCAL_INGEST"))
    if not cand:
        return ""
    if _health_ok(cand, timeout):
        return cand
    adv = (url or "").rstrip("/")
    if adv and adv != cand and _health_ok(adv, timeout):
        return adv
    return ""


def detect_scenario() -> str:
    forced = env("AGENTBOARD_SCENARIO").lower()
    if forced == "board-client":
        return "project"
    if forced in ("project", "cloud", "openclaw"):
        return forced
    if is_cloud_agent():
        return "cloud"
    return "project"


def provider_service_defaults(provider: str) -> tuple[str, str]:
    defaults = {
        "cursor": ("cursor", "Cursor Agent"),
        "codex": ("codex", "Codex"),
        "openclaw": ("openclaw", "OpenClaw"),
        "claude": ("claude", "Claude Code"),
        "hermes": ("hermes", "Hermes"),
        "pi": ("pi", "Pi"),
        "agent": ("agent", "Agent"),
    }
    return defaults.get(provider, defaults["agent"])


def resolve_identity(provider: str, scenario: str) -> tuple[str, str, dict]:
    dkey, dname = provider_service_defaults(provider)
    meta: dict = {"provider": provider, "scenario": scenario}
    cid = conversation_id()
    if cid:
        meta["conversation_id"] = cid
    if scenario == "cloud":
        host = socket.gethostname() or "unknown"
        key = slug_key("cloud-" + host, "cloud-env")
        meta["hostname"] = host
        return key, f"{dname} ({host})", meta
    key = env("AGENTBOARD_SERVICE_KEY") or dkey
    name = env("AGENTBOARD_SERVICE_NAME") or dname
    return key, name, meta


def conversation_id() -> str:
    return env("CURSOR_CONVERSATION_ID") or env("CODEX_THREAD_ID")


def run_key_path(provider: str, service_key: str, coding: bool) -> Path:
    ident = env("AGENTBOARD_RUN_FILE")
    if ident:
        return state_dir() / ident
    if coding:
        cid = conversation_id()
        if cid:
            return state_dir() / f"agentboard-run-{service_key}-{slug_key(cid, 'conv')}"
        return state_dir() / f"agentboard-run-{service_key}-{os.getppid()}"
    key = env("AGENTBOARD_SERVICE_KEY") or provider
    return state_dir() / f"agentboard-run-{key}"


def load_run_key(provider: str, service_key: str, coding: bool) -> str:
    if env("AGENTBOARD_RUN_KEY"):
        return slug_key(env("AGENTBOARD_RUN_KEY"), "run")
    p = run_key_path(provider, service_key, coding)
    if p.exists():
        text = p.read_text(encoding="utf-8").strip()
        return slug_key(text, "run") if text else ""
    return ""


def save_run_key(provider: str, service_key: str, coding: bool, key: str) -> None:
    p = run_key_path(provider, service_key, coding)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(key, encoding="utf-8")
    except OSError:
        pass


def new_run_key(provider: str, service_key: str, coding: bool) -> str:
    key = str(uuid.uuid4())
    save_run_key(provider, service_key, coding, key)
    return key


def resolve_run_key(
    args: argparse.Namespace,
    provider: str,
    service_key: str,
    coding: bool,
    create: bool,
    *,
    fresh: bool = False,
) -> str:
    rk = (args.run_key or "").strip()
    if rk:
        return slug_key(rk, "run")
    if env("AGENTBOARD_RUN_KEY"):
        return slug_key(env("AGENTBOARD_RUN_KEY"), "run")
    if not fresh:
        rk = load_run_key(provider, service_key, coding)
        if rk:
            return rk
    if create:
        return new_run_key(provider, service_key, coding)
    return ""


def envelope(event_type: str, service_key: str, payload: dict, run_key: str = "") -> dict:
    envl = {
        "schema_version": 1,
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": utc_now(),
        "service_key": service_key,
        "payload": payload,
    }
    if run_key:
        envl["run_key"] = run_key
    return envl


def tee_to_local_ingest(events: list[dict]) -> None:
    if not events:
        return
    local = discover_local_tee()
    if not local:
        return
    ws, name = workspace_info()
    post(local, "", stamp_host_project(events, ws, name), 0.8, False)


def post(url: str, token: str, events: list[dict], timeout: float, dry_run: bool) -> int:
    body = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
    if dry_run:
        sys.stdout.write(body.decode("utf-8") + "\n")
        return 0
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "agentboard-report/1.0",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        url.rstrip("/") + "/ingest/v1/events",
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if os.environ.get("AGENTBOARD_VERBOSE"):
                sys.stderr.write(raw.decode("utf-8", "replace") + "\n")
            return 0
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"agentboard-report: HTTP {e.code} {e.read()[:300]!r}\n")
        return 0 if env("AGENTBOARD_SOFT_FAIL", "1") != "0" else 1
    except Exception as e:  # noqa: BLE001 — never block the agent task
        sys.stderr.write(f"agentboard-report: {e}\n")
        return 0 if env("AGENTBOARD_SOFT_FAIL", "1") != "0" else 1


def status_items(_provider: str, extra_meta: dict) -> list[dict]:
    """User-facing keys only. Liveness is service.state TTL, not status rows."""
    return []


def heartbeat_events(
    service_key: str,
    name: str,
    provider: str,
    summary: str,
    ttl: int,
    extra_meta: dict | None = None,
) -> list[dict]:
    meta = extra_meta or {"provider": provider}
    events = [
        envelope(
            "service.state",
            service_key,
            {
                "name": name,
                "type": "agent",
                "state": "running",
                "summary": summary,
                "severity": "normal",
                "ttl_seconds": ttl,
                "metadata": meta,
            },
        ),
    ]
    items = status_items(provider, meta)
    if items:
        events.append(
            envelope(
                "status.upsert",
                service_key,
                {"items": items},
            )
        )
    return events


def interrupt_text(summary: str) -> str:
    text = (summary or "").strip()
    if text.startswith("任务被打断"):
        return text
    if not text:
        return "任务被打断"
    return "任务被打断：" + text


def log_append(service_key: str, provider: str, markdown: str, severity: str, run_key: str) -> dict:
    return envelope(
        "log.append",
        service_key,
        {"markdown": markdown, "severity": severity, "source": provider},
        run_key=run_key,
    )


def run_transition(
    service_key: str,
    name: str,
    provider: str,
    run_key: str,
    status: str,
    summary: str,
    extra: dict | None = None,
    *,
    started: bool = False,
    finished: bool = False,
) -> dict:
    payload = {
        "service_name": name,
        "service_type": "agent",
        "status": status,
        "summary": summary,
        "provider": provider,
        "metadata": extra or {},
    }
    if started:
        payload["started_at"] = utc_now()
    if finished:
        payload["finished_at"] = utc_now()
    return envelope("run.transition", service_key, payload, run_key=run_key)


def build_events(
    cmd: str,
    args: argparse.Namespace,
    service_key: str,
    name: str,
    provider: str,
    extra_meta: dict,
    coding: bool,
) -> list[dict]:
    ttl = args.ttl
    summary = args.message or ""
    meta = extra_meta or {"provider": provider}

    if cmd == "heartbeat":
        if coding:
            return heartbeat_events(service_key, name, provider, "", ttl, meta)
        return heartbeat_events(service_key, name, provider, summary or "heartbeat", ttl, meta)

    if cmd == "start":
        rk = resolve_run_key(args, provider, service_key, coding, create=True, fresh=True)
        text = summary or "task started"
        hb_summary = "" if coding else text
        events = heartbeat_events(service_key, name, provider, hb_summary, ttl, meta)
        events.append(run_transition(service_key, name, provider, rk, "running", text, meta, started=True))
        if coding:
            events.append(log_append(service_key, provider, text, "info", rk))
        return events

    if cmd == "progress":
        rk = resolve_run_key(args, provider, service_key, coding, create=True)
        text = summary or "progress"
        events = [log_append(service_key, provider, text, "info", rk)]
        if coding:
            events.append(run_transition(service_key, name, provider, rk, "running", text, meta))
        return events

    if cmd == "log":
        rk = resolve_run_key(args, provider, service_key, coding, create=False)
        return [log_append(service_key, provider, summary or "(empty)", args.severity, rk)]

    if cmd == "error":
        rk = resolve_run_key(args, provider, service_key, coding, create=False)
        text = summary or "error"
        state_summary = "" if coding else text
        return heartbeat_events(service_key, name, provider, state_summary, ttl, meta)[:1] + [
            log_append(service_key, provider, text, "error", rk),
            envelope(
                "collector.notice",
                service_key,
                {"severity": "error", "code": args.code or "agent_error", "markdown": text, "metadata": meta},
            ),
        ]

    if cmd == "succeed":
        rk = resolve_run_key(args, provider, service_key, coding, create=True)
        text = summary or "succeeded"
        hb_summary = "" if coding else text
        events = heartbeat_events(service_key, name, provider, hb_summary, ttl, meta)
        events.append(run_transition(service_key, name, provider, rk, "succeeded", text, meta, finished=True))
        if coding:
            events.append(log_append(service_key, provider, text, "info", rk))
        return events

    if cmd in ("fail", "interrupt"):
        create = cmd != "interrupt"
        rk = resolve_run_key(args, provider, service_key, coding, create=create)
        if cmd == "interrupt":
            if not rk:
                return []
            text = interrupt_text(summary)
        else:
            text = summary or "failed"
        events: list[dict] = []
        if coding:
            events.extend(heartbeat_events(service_key, name, provider, "", ttl, meta))
        else:
            events.append(
                envelope(
                    "service.state",
                    service_key,
                    {
                        "name": name,
                        "type": "agent",
                        "state": "failed",
                        "summary": text,
                        "severity": "error",
                        "ttl_seconds": ttl,
                        "metadata": meta,
                    },
                )
            )
        events.append(log_append(service_key, provider, text, "error", rk))
        events.append(run_transition(service_key, name, provider, rk, "failed", text, meta, finished=True))
        return events

    if cmd == "notice":
        return [
            envelope(
                "collector.notice",
                service_key,
                {
                    "severity": args.severity,
                    "code": args.code or "notice",
                    "markdown": summary or "notice",
                    "metadata": meta,
                },
            )
        ]
    if cmd == "dead":
        return [
            envelope(
                "service.state",
                service_key,
                {
                    "name": name,
                    "type": "agent",
                    "state": "stopped",
                    "summary": summary or "stopped",
                    "severity": "unknown",
                    "ttl_seconds": ttl,
                    "metadata": meta,
                },
            )
        ]
    raise SystemExit(f"unknown command {cmd}")


def main() -> int:
    p = argparse.ArgumentParser(description="Report agent status to AgentBoard")
    p.add_argument(
        "command",
        choices=["ping", "heartbeat", "start", "progress", "log", "error", "succeed", "fail", "interrupt", "notice", "dead"],
    )
    p.add_argument("message", nargs="?", default="")
    p.add_argument("--severity", default="info")
    p.add_argument("--code", default="")
    p.add_argument("--run-key", default=env("AGENTBOARD_RUN_KEY"))
    p.add_argument("--ttl", type=int, default=int(env("AGENTBOARD_TTL_SECONDS") or DEFAULT_TTL))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--timeout", type=float, default=8.0)
    args = p.parse_args()

    provider = infer_provider()
    coding = not is_openclaw(provider)
    if is_openclaw(provider):
        scenario = "openclaw"
        service_key, name, extra_meta = resolve_identity(provider, "project")
        extra_meta["scenario"] = "openclaw"
        extra_meta["provider"] = provider
        dkey, dname = provider_service_defaults(provider)
        service_key = env("AGENTBOARD_SERVICE_KEY") or dkey
        name = env("AGENTBOARD_SERVICE_NAME") or dname
    else:
        scenario = detect_scenario()
        service_key, name, extra_meta = resolve_identity(provider, scenario)

    token = env("AGENTBOARD_TOKEN")
    url = env("AGENTBOARD_URL") or DEFAULT_URL

    if args.command == "ping":
        if args.dry_run:
            print(json.dumps({"url": url, "has_token": bool(token), "scenario": scenario, "local": False}))
            return 0
        if not token:
            print("agentboard-report: AGENTBOARD_TOKEN unset; skip", file=sys.stderr)
            return 0
        req = urllib.request.Request(
            url.rstrip("/") + "/ingest/v1/ping",
            headers={"Authorization": "Bearer " + token, "User-Agent": "agentboard-report/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                print(resp.read().decode("utf-8", "replace"))
                return 0
        except Exception as e:  # noqa: BLE001
            print(f"agentboard-report: ping failed: {e}", file=sys.stderr)
            return 0

    events = build_events(args.command, args, service_key, name, provider, extra_meta, coding)
    if args.dry_run:
        return post(url, token, events, args.timeout, True)
    if not events:
        return 0
    if token:
        rc = post(url, token, events, args.timeout, False)
        if rc == 0:
            tee_to_local_ingest(events)
        return rc
    tee_to_local_ingest(events)
    if discover_local_tee():
        sys.stderr.write("agentboard-report: AGENTBOARD_TOKEN unset; remote skipped, local tee only\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
