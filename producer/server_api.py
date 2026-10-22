"""产包端对服务端 v2 的薄封装。标准库 urllib，不 import client/。

服务端 URL 来自 `BALL_GT_SERVER`。空 = 离线，所有方法返回 None，产包照旧能跑。
可选 `BALL_GT_API_TOKEN`（对应服务端写接口的 `X-Api-Token`）。

调用方把「不可达」当成 None，自己决定要不要退回 `--keep-available`。
这个模块不持有、不转发 ACP / AOSS 凭证。
"""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_TIMEOUT = 15.0


def resolve_job_id() -> str:
    """作业 id：环境变量 → sidecar 文件 → hostname-pid。"""
    for key in ("BALL_GT_JOB_ID", "ACP_JOB_ID"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    path = (os.environ.get("BALL_GT_JOB_ID_FILE") or os.environ.get("BALL_GT_JOB_ENV_FILE") or "").strip()
    if path:
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("'\"")
            if k in ("BALL_GT_JOB_ID", "ACP_JOB_ID") and v:
                return v
        # 纯文本文件：整份就是 job_id
        raw = text.strip()
        if raw and "\n" not in raw and "=" not in raw:
            return raw
    return f"{socket.gethostname()}-{os.getpid()}"


def resolve_node() -> str:
    return (
        (os.environ.get("BALL_GT_NODE") or "").strip()
        or (os.environ.get("HOSTNAME") or "").strip()
        or socket.gethostname()
    )


class ServerAPI:
    """对 `/api/v2/*` 的 JSON 客户端。`enabled=False` 时全部短路为 None。"""

    def __init__(
        self,
        base: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base = (base if base is not None else os.environ.get("BALL_GT_SERVER") or "").rstrip("/")
        self.token = token if token is not None else (os.environ.get("BALL_GT_API_TOKEN") or "")
        self.timeout = float(timeout)
        self.enabled = bool(self.base)
        self.last_error: Optional[str] = None

    def heartbeat(
        self,
        job_id: str,
        *,
        loop_iter: Optional[int] = None,
        stage: Optional[str] = None,
        n_produced: Optional[int] = None,
        last_error: Optional[str] = None,
        keep_available: Optional[int] = None,
        batch: Optional[int] = None,
        poll_sec: Optional[int] = None,
        node: Optional[str] = None,
        acp_state: Optional[str] = None,
        log_path: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {"job_id": job_id}
        for k, v in (
            ("loop_iter", loop_iter),
            ("stage", stage),
            ("n_produced", n_produced),
            ("last_error", last_error),
            ("keep_available", keep_available),
            ("batch", batch),
            ("poll_sec", poll_sec),
            ("node", node),
            ("acp_state", acp_state),
            ("log_path", log_path),
            ("display_name", display_name),
        ):
            if v is not None:
                body[k] = v
        return self.post("/api/v2/producer/heartbeat", body)

    def register_job(
        self,
        job_id: str,
        *,
        display_name: Optional[str] = None,
        pool: Optional[str] = None,
        resource: Optional[str] = None,
        node: Optional[str] = None,
        run_stamp: Optional[str] = None,
        acp_state: Optional[str] = None,
        log_path: Optional[str] = None,
        poll_sec: Optional[int] = None,
        extra: Optional[dict] = None,
        msg: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {"job_id": job_id}
        for k, v in (
            ("display_name", display_name),
            ("pool", pool),
            ("resource", resource),
            ("node", node),
            ("run_stamp", run_stamp),
            ("acp_state", acp_state),
            ("log_path", log_path),
            ("poll_sec", poll_sec),
            ("extra", extra),
            ("msg", msg),
        ):
            if v is not None:
                body[k] = v
        return self.post("/api/v2/producer/register", body)

    def event(
        self,
        job_id: str,
        kind: str,
        msg: str = "",
        extra: Optional[dict] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {"job_id": job_id, "kind": kind, "msg": msg}
        if extra is not None:
            body["extra"] = extra
        return self.post("/api/v2/producer/event", body)

    def register_packets(self, packets: List[dict]) -> Optional[Dict[str, Any]]:
        if len(packets) == 1:
            return self.post("/api/v2/register", packets[0])
        return self.post("/api/v2/register", {"packets": packets})

    def register_packet(
        self,
        packet: dict,
        thumb_dir: Optional[Path] = None,
    ) -> Optional[Dict[str, Any]]:
        if not thumb_dir or not Path(thumb_dir).is_dir():
            return self.post("/api/v2/register", packet)
        fields = {
            "packet_id": str(packet.get("packet_id") or ""),
            "aoss_key": str(packet.get("aoss_key") or ""),
            "n_targets": str(packet.get("n_targets") or ""),
            "size_bytes": str(packet.get("size_bytes") or ""),
            "producer_job_id": str(packet.get("producer_job_id") or ""),
            "producer_node": str(packet.get("producer_node") or ""),
            "priority": str(packet.get("priority") or 0),
            "meta": json.dumps(packet.get("meta") or {}, ensure_ascii=False),
        }
        files: Dict[str, Tuple[str, bytes, str]] = {}
        mapping = {
            "thumb_clip": "clip.mp4",
            "thumb_dog": "dog.mp4",
            "thumb_scan_dog": "scan_dog.jpg",
            "thumb_scan_time": "scan_time.jpg",
        }
        td = Path(thumb_dir)
        for field, name in mapping.items():
            p = td / name
            if not p.exists():
                continue
            ct = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            files[field] = (name, p.read_bytes(), ct)
        return self.post_multipart("/api/v2/register", fields, files)

    def post_multipart(
        self,
        path: str,
        fields: Dict[str, str],
        files: Dict[str, Tuple[str, bytes, str]],
    ) -> Optional[Dict[str, Any]]:
        boundary = "----ball" + uuid.uuid4().hex
        parts: List[bytes] = []
        for k, v in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            )
        for k, (name, content, ct) in files.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                f'filename="{name}"\r\nContent-Type: {ct}\r\n\r\n'.encode()
                + content + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        return self._request(
            self.base + path,
            method="POST",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=max(self.timeout, 180.0),
        )

    def pool(self, keep_available: Optional[int] = None, batch: Optional[int] = None) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if keep_available is not None:
            params["keep_available"] = keep_available
        if batch is not None:
            params["batch"] = batch
        return self.get("/api/v2/pool", **params)

    def producers(self) -> Optional[Dict[str, Any]]:
        return self.get("/api/v2/producers")

    def blocklist(self) -> Optional[Dict[str, Any]]:
        """停用视频（video_keys = group__stem）+ 已移除包。服务端不可达返回 None。"""
        return self.get("/api/v2/blocklist")

    def get(self, path: str, **params: Any) -> Optional[Dict[str, Any]]:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._request(url, method="GET")

    def post(self, path: str, body: Optional[dict] = None) -> Optional[Dict[str, Any]]:
        return self._request(
            self.base + path,
            method="POST",
            data=json.dumps(body or {}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    def _request(
        self,
        url: str,
        method: str,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            self.last_error = "BALL_GT_SERVER 未设"
            return None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if self.token:
            req.add_header("X-Api-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
                self.last_error = None
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = (e.read() or b"").decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            self.last_error = f"HTTP {e.code} {method} {url}: {detail}"
            return None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, json.JSONDecodeError) as e:
            reason = getattr(e, "reason", e)
            self.last_error = f"{type(e).__name__}: {reason}"
            return None
