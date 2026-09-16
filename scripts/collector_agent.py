#!/usr/bin/env python3
"""开发机常驻：从 ACP + AFS 日志看产包作业，把作业级状态推给服务端。

死掉的 `make_packets.py` 发不出死讯，所以这条通道必须在作业外面。跑在开发机上
是因为**服务端不持有 ACP / AOSS 凭证**，公网也未必通 ACP endpoint。

    ./venv/bin/python scripts/collector_agent.py
    ./venv/bin/python scripts/collector_agent.py --once
    ./venv/bin/python scripts/collector_agent.py --auto-resubmit --keep 40

ACP 调用走 `~/.cursor/skills/acp-api/scripts/acp_cli.py` 的 list / get / workers，
可用 `--acp-cli` 换路径，或 `--fake-acp jobs.json` 做冒烟（不真打 ACP）。

不要把 ACP key 发给服务端。这里只上报 job_id / acp_state / node / log 摘要。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from producer.server_api import ServerAPI  # noqa: E402

DEFAULT_ACP_CLI = os.path.expanduser("~/.cursor/skills/acp-api/scripts/acp_cli.py")
DEFAULT_POLL = 300
PRODUCER_NEEDLE = "ball_gt_produce"
LIVE_STATES = {"RUNNING", "PENDING", "WAITING", "QUEUED", "STARTING"}
ERROR_LINE = re.compile(
    r"\[(fail|fatal|err)\]|Traceback \(most recent call last\)|^[A-Za-z]*Error:|Permission denied",
    re.I,
)


class AcpBackend(Protocol):
    def list_jobs(self) -> List[dict]: ...
    def get_job(self, name: str) -> Optional[dict]: ...
    def workers(self, name: str) -> dict: ...


class AcpCliBackend:
    """调 acp_cli.py。list 用 --json；get / workers 本身就是 JSON。"""

    def __init__(self, cli: str, python: str, env_file: Optional[str] = None) -> None:
        self.cli = cli
        self.python = python
        self.env_file = env_file

    def _run(self, args: List[str]) -> Tuple[int, str]:
        env = dict(os.environ)
        if self.env_file:
            env["ACP_ENV_FILE"] = self.env_file
        p = subprocess.run(
            [self.python, self.cli, *args],
            capture_output=True, text=True, env=env,
        )
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        return p.returncode, out

    def _parse(self, raw: str) -> Any:
        raw = raw.strip()
        if not raw:
            return {}
        # acp_cli 可能在 JSON 前面打 [afs] 行
        start = raw.find("{")
        if start < 0:
            start = raw.find("[")
        if start < 0:
            raise ValueError(f"ACP 输出不是 JSON：{raw[:200]}")
        return json.loads(raw[start:])

    def list_jobs(self) -> List[dict]:
        rc, raw = self._run(["list", "--json", "--page-size", "100"])
        if rc != 0:
            raise RuntimeError(f"acp list 失败（exit {rc}）：{raw[-400:]}")
        obj = self._parse(raw)
        if isinstance(obj, list):
            return obj
        return obj.get("training_jobs") or obj.get("jobs") or []

    def get_job(self, name: str) -> Optional[dict]:
        rc, raw = self._run(["get", name])
        if rc != 0:
            low = raw.lower()
            if "404" in low or "not found" in low or "notfound" in low:
                return None
            raise RuntimeError(f"acp get {name} 失败（exit {rc}）：{raw[-400:]}")
        obj = self._parse(raw)
        return obj if isinstance(obj, dict) else None

    def workers(self, name: str) -> dict:
        rc, raw = self._run(["workers", name])
        if rc != 0:
            return {}
        obj = self._parse(raw)
        return obj if isinstance(obj, dict) else {}


class FakeAcpBackend:
    """冒烟用：从 JSON 文件读作业列表。get 查不到就返回 None（gone）。"""

    def __init__(self, path: Path) -> None:
        self.doc = json.loads(path.read_text())
        self.jobs = list(self.doc.get("jobs") or [])

    def list_jobs(self) -> List[dict]:
        return list(self.jobs)

    def get_job(self, name: str) -> Optional[dict]:
        for j in self.jobs:
            if str(j.get("name") or "") == name:
                return j
        return None

    def workers(self, name: str) -> dict:
        for j in self.jobs:
            if str(j.get("name") or "") == name:
                return j.get("workers") or {}
        return {}


def is_producer_job(job: dict, extra_ids: Optional[set] = None) -> bool:
    name = str(job.get("name") or "")
    display = str(job.get("display_name") or "")
    if PRODUCER_NEEDLE in display or PRODUCER_NEEDLE in name:
        return True
    if extra_ids and name in extra_ids:
        return True
    return False


def worker_node(workers_obj: dict) -> str:
    items = workers_obj.get("workers") or workers_obj.get("items") or []
    if isinstance(items, dict):
        items = list(items.values())
    for w in items:
        if not isinstance(w, dict):
            continue
        for k in ("node", "node_name", "hostname", "host", "name"):
            v = w.get(k)
            if v:
                return str(v)
    return ""


def classify(
    acp_state: Optional[str],
    hb_age_sec: Optional[float],
    poll_sec: int,
) -> str:
    """作业级健康度（给日志 / 自动重提用；服务端自己还会再判一次）。

    心跳超过 2 × poll_sec 未到 → stale；ACP FAILED → failed；查不到 → gone；
    RUNNING 但无进程心跳 → stuck（卡住，大概率 ffmpeg 或上传挂住）。
    """
    acp = (acp_state or "").upper()
    if acp in ("FAILED", "ERROR"):
        return "failed"
    if acp in ("GONE", "NOTFOUND", "NOT_FOUND", "DELETED") or acp_state is None:
        return "gone"
    limit = 2 * int(poll_sec)
    no_proc = hb_age_sec is None
    stale = (not no_proc) and hb_age_sec > limit
    if acp in ("RUNNING",) and no_proc:
        return "stuck"
    if stale:
        return "stale"
    if acp in LIVE_STATES:
        return "running"
    if acp in ("SUCCEEDED", "COMPLETED", "STOPPED", "TERMINATED"):
        return "idle"
    return "stale"


def tail_errors(log_path: Path, n_lines: int = 200) -> List[str]:
    if not log_path.is_file():
        return []
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    hits = [ln for ln in lines[-n_lines:] if ERROR_LINE.search(ln)]
    return hits[-8:]


def match_log(log_dir: Path, job_name: str, run_stamp: str = "") -> Optional[Path]:
    cands = sorted(log_dir.glob(f"{job_name}_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if run_stamp:
        for p in cands:
            if run_stamp in p.name:
                return p
    return cands[0] if cands else None


def extra_job_ids(api: ServerAPI) -> set:
    doc = api.producers()
    if not doc:
        return set()
    return {str(r.get("job_id") or "") for r in (doc.get("producers") or []) if r.get("job_id")}


def producers_index(api: ServerAPI) -> Dict[str, dict]:
    doc = api.producers()
    if not doc:
        return {}
    return {str(r.get("job_id") or ""): r for r in (doc.get("producers") or []) if r.get("job_id")}


def producer_hb_age(index: Dict[str, dict], job_id: str) -> Optional[float]:
    r = index.get(job_id) or {}
    age = r.get("hb_age_sec")
    return float(age) if age is not None else None


def poll_once(
    acp: AcpBackend,
    api: ServerAPI,
    *,
    poll_sec: int,
    log_dir: Path,
    job_name: str,
    known_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """一轮：列 ACP、归因、推心跳。返回本轮快照（冒烟 / 自动重提用）。"""
    jobs = [j for j in acp.list_jobs() if is_producer_job(j, known_ids)]
    seen = {str(j.get("name") or "") for j in jobs if j.get("name")}
    gone_ids = [jid for jid in (known_ids or set()) if jid.startswith("pt-") and jid not in seen]
    by_id = producers_index(api)
    last_err = getattr(poll_once, "_last_err", {})
    poll_once._last_err = last_err  # type: ignore[attr-defined]

    snap: Dict[str, Any] = {"jobs": [], "gone": gone_ids, "live": []}
    for j in jobs:
        name = str(j.get("name") or "")
        detail = acp.get_job(name)
        if detail is None:
            acp_state = "GONE"
            node = ""
        else:
            acp_state = str(detail.get("state") or j.get("state") or "")
            try:
                node = worker_node(acp.workers(name))
            except Exception:  # noqa: BLE001
                node = ""
        stamp = str((detail or j).get("run_stamp") or "")
        log_path = match_log(log_dir, job_name, stamp)
        errs = tail_errors(log_path) if log_path else []
        hb_age = producer_hb_age(by_id, name)
        health = classify(acp_state if detail is not None else None, hb_age, poll_sec)
        rec = {
            "job_id": name,
            "acp_state": acp_state,
            "node": node,
            "log_path": str(log_path) if log_path else "",
            "last_error": errs[-1] if errs else "",
            "health": health,
            "hb_age_sec": hb_age,
            "display_name": (detail or j).get("display_name") or job_name,
        }
        snap["jobs"].append(rec)
        if health in ("running", "stuck") or acp_state.upper() in LIVE_STATES:
            snap["live"].append(name)

        # 外部轮询带 acp_state、不带 loop_iter → 服务端刷 last_acp_poll 而不是进程心跳
        err = rec["last_error"] or None
        if err and last_err.get(name) == err:
            err = None
        elif err:
            last_err[name] = err
        hb = api.heartbeat(
            name,
            acp_state=acp_state,
            node=node or None,
            log_path=rec["log_path"] or None,
            last_error=err,
            poll_sec=poll_sec,
            display_name=str(rec["display_name"]),
        )
        if hb is None and api.enabled:
            print(f"[warn] 心跳上报失败 {name}：{api.last_error}", flush=True)
        else:
            print(
                f"[acp] {name}  {acp_state}  health={health}  "
                f"hb_age={hb_age}  node={node or '-'}",
                flush=True,
            )
        if health in ("failed", "gone", "stuck", "stale"):
            api.event(name, "acp_state", f"{health}: {acp_state}", extra={"health": health})

    for jid in gone_ids:
        api.heartbeat(jid, acp_state="GONE", poll_sec=poll_sec, display_name=job_name)
        api.event(jid, "acp_state", "gone: ACP 查不到")
        snap["jobs"].append({"job_id": jid, "acp_state": "GONE", "health": "gone"})
        print(f"[acp] {jid}  GONE", flush=True)
    return snap


def should_resubmit(pool: Optional[dict], snap: Dict[str, Any]) -> bool:
    if not pool:
        return False
    if int(pool.get("n_available") or 0) > 0:
        return False
    return not snap.get("live")


def resubmit(submit_sh: Path, keep: int, run: Optional[Callable] = None) -> int:
    cmd = [str(submit_sh), "--async", "--keep", str(keep)]
    print(f"[resubmit] {' '.join(cmd)}", flush=True)
    if run is not None:
        return int(run(cmd))
    p = subprocess.run(cmd)
    return p.returncode


def run(args) -> int:
    api = ServerAPI(base=args.server, token=args.token, timeout=args.timeout)
    if args.fake_acp:
        acp: AcpBackend = FakeAcpBackend(Path(args.fake_acp))
    else:
        if not Path(args.acp_cli).is_file():
            print(f"[err] 找不到 ACP CLI: {args.acp_cli}", flush=True)
            return 2
        acp = AcpCliBackend(args.acp_cli, args.acp_python, args.acp_env)
    log_dir = Path(args.log_dir)
    submit_sh = Path(args.submit)
    last_resubmit = 0.0
    stamp_file = log_dir / ".collector_last_resubmit"
    if stamp_file.exists():
        try:
            last_resubmit = float(stamp_file.read_text().strip())
        except ValueError:
            last_resubmit = 0.0

    known = extra_job_ids(api)
    while True:
        try:
            snap = poll_once(
                acp, api,
                poll_sec=args.poll_sec,
                log_dir=log_dir,
                job_name=args.job_name,
                known_ids=known,
            )
            known |= {j["job_id"] for j in snap["jobs"] if j.get("job_id")}
            pool = api.pool(keep_available=args.keep, batch=10)
            if pool:
                print(
                    f"[pool] available={pool.get('n_available')} want_more={pool.get('want_more')} "
                    f"live={len(snap.get('live') or [])}",
                    flush=True,
                )
            if args.auto_resubmit and should_resubmit(pool, snap):
                now = time.time()
                if now - last_resubmit < args.resubmit_interval:
                    print(
                        f"[resubmit] 跳过：距上次仅 {now - last_resubmit:.0f}s "
                        f"< {args.resubmit_interval}s",
                        flush=True,
                    )
                else:
                    rc = resubmit(submit_sh, args.keep)
                    last_resubmit = now
                    log_dir.mkdir(parents=True, exist_ok=True)
                    stamp_file.write_text(str(last_resubmit))
                    if rc != 0:
                        print(f"[resubmit] submit.sh 退出 {rc}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[error] 本轮失败：{e}", flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.poll_sec)


def parse_args(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(description="ACP 产包作业监控代理（开发机）")
    p.add_argument("--server", default=os.environ.get("BALL_GT_SERVER") or "",
                   help="服务端 URL，空则只打印不推送")
    p.add_argument("--token", default=os.environ.get("BALL_GT_API_TOKEN") or "")
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--poll-sec", type=int, default=DEFAULT_POLL)
    p.add_argument("--job-name", default=os.environ.get("BALL_GT_JOB_NAME") or PRODUCER_NEEDLE)
    p.add_argument("--log-dir", default=str(REPO / "logs"))
    p.add_argument("--acp-cli", default=os.environ.get("ACP_CLI") or DEFAULT_ACP_CLI)
    p.add_argument("--acp-python", default=sys.executable)
    p.add_argument("--acp-env", default=os.environ.get("ACP_ENV_FILE") or "")
    p.add_argument("--fake-acp", default="", help="冒烟：读这份 JSON 当 ACP 列表，不真打 ACP")
    p.add_argument("--once", action="store_true", help="只轮询一轮然后退出")
    p.add_argument("--auto-resubmit", action="store_true",
                   help="池子见底且无存活产包作业时重跑 submit.sh（默认关）")
    p.add_argument("--keep", type=int, default=40, help="重提时传给 submit.sh --keep")
    p.add_argument("--resubmit-interval", type=int, default=1800, help="最小重提间隔（秒）")
    p.add_argument("--submit", default=str(REPO / "producer" / "submit.sh"))
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run(parse_args()))
