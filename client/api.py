#!/usr/bin/env python3
"""服务端 v2 HTTP 客户端：lease / heartbeat / release / submit。

租约期间起一条后台心跳线程续期——AI 租约只有 30 分钟，跑长包不续会被
`expire_leases` 收回。token 走 `X-Api-Token` 和 `Authorization: Bearer`
（服务端默认关鉴权；设了 `BALL_GT_API_TOKEN` 才校验写接口）。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("client.api")

_REPO = Path(__file__).resolve().parent.parent


def resolve_api_token(explicit: str = "") -> str:
    """写接口 token：`--token` > `BALL_GT_API_TOKEN` > 仓库 `.env` > `.ball_gt_token`。

    值不打印。公网站开了鉴权时，空 token 会 401，人工站必须能找到这把钥匙。
    """
    if (explicit or "").strip():
        return explicit.strip()
    try:
        from client.vlm import load_dotenv
        load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    env = (os.environ.get("BALL_GT_API_TOKEN") or "").strip()
    if env:
        return env
    for p in (_REPO / ".ball_gt_token", Path.home() / ".ball_gt_token"):
        try:
            if not p.is_file():
                continue
            line = p.read_text(encoding="utf-8").strip().splitlines()
            if line and line[0].strip():
                return line[0].strip()
        except Exception:  # noqa: BLE001
            continue
    return ""


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Lease:
    lease_id: str
    packet_id: str
    aoss_key: str
    meta: Dict[str, Any]
    kind: str
    target_frames: List[int]
    prior_labels: List[dict] = field(default_factory=list)
    expires_at: float = 0.0
    ttl_sec: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @classmethod
    def from_response(cls, payload: Dict[str, Any]) -> Optional["Lease"]:
        """兼容 `{lease: {...}}` 与扁平 `{packet_id, ...}`。池空返回 None。"""
        if not payload:
            return None
        blob = payload.get("lease") if "lease" in payload else payload
        if not blob or not isinstance(blob, dict) or not blob.get("lease_id"):
            return None
        prior = blob.get("prior_labels") or blob.get("gt") or blob.get("seed") or []
        if isinstance(prior, dict):
            prior = list(prior.values())
        return cls(
            lease_id=str(blob["lease_id"]),
            packet_id=str(blob.get("packet_id") or ""),
            aoss_key=str(blob.get("aoss_key") or ""),
            meta=dict(blob.get("meta") or {}),
            kind=str(blob.get("kind") or "ai"),
            target_frames=[int(x) for x in (blob.get("target_frames") or [])],
            prior_labels=[r for r in prior if isinstance(r, dict)],
            expires_at=float(blob.get("expires_at") or 0.0),
            ttl_sec=float(blob.get("ttl_sec") or 0.0),
            raw=dict(blob),
            reason=str(payload.get("reason") or ""),
        )


class Heartbeat:
    """lease 期间周期性 POST /heartbeat。daemon 线程，stop() 后不再打。"""

    def __init__(self, client: "ServerClient", lease_id: str, interval: float = 60.0):
        self.client = client
        self.lease_id = lease_id
        self.interval = max(5.0, float(interval))
        self.stage = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"hb-{lease_id[:8]}", daemon=True)

    def start(self) -> "Heartbeat":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def set_stage(self, stage: str) -> None:
        self.stage = stage

    def _loop(self) -> None:
        # 先等一个 interval，刚 lease 完不必立刻续
        while not self._stop.wait(self.interval):
            try:
                self.client.heartbeat(self.lease_id, self.stage or None)
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat %s 失败: %s", self.lease_id, e)


class ServerClient:
    def __init__(self, base: str, token: str = "", worker: str = "", timeout: float = 60.0):
        self.base = base.rstrip("/")
        self.token = resolve_api_token(token)
        self.worker = worker
        self.timeout = timeout

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = dict(extra or {})
        if self.token:
            h.setdefault("X-Api-Token", self.token)
            h.setdefault("Authorization", f"Bearer {self.token}")
        if self.worker:
            h.setdefault("X-Worker", self.worker)
        return h

    def request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[int, Any]:
        url = self.base + path
        raw = data if data is not None else (
            json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        )
        req = urllib.request.Request(url, data=raw, method=method)
        for k, v in self._headers(headers).items():
            req.add_header(k, v)
        if raw is not None and "Content-Type" not in (headers or {}) and body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                payload = r.read()
                ct = r.headers.get("Content-Type", "")
                return r.status, (json.loads(payload) if "json" in ct else payload)
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                parsed = json.loads(payload)
            except Exception:  # noqa: BLE001
                parsed = payload.decode("utf-8", "replace")
            raise ApiError(
                f"{method} {path} → {e.code}: {parsed if isinstance(parsed, str) else parsed.get('error', parsed)}",
                status=e.code,
                body=parsed,
            ) from None
        except urllib.error.URLError as e:
            raise ApiError(f"{method} {path} 连不上 {self.base}: {e.reason}") from e

    def get(self, path: str, **params) -> Any:
        if params:
            path = path + "?" + urllib.parse.urlencode(params)
        _, body = self.request("GET", path)
        return body

    def post(self, path: str, body: Optional[dict] = None) -> Any:
        _, payload = self.request("POST", path, body=body or {})
        return payload

    def post_multipart(
        self,
        path: str,
        fields: Dict[str, str],
        files: Dict[str, Tuple[str, bytes, str]],
    ) -> Any:
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
        _, payload = self.request(
            "POST",
            path,
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=max(self.timeout, 180.0),
        )
        return payload

    # ---------------------------------------------------------------- 业务

    def lease(self, worker: str = "", kind: str = "ai", packet_id: str = "") -> Optional[Lease]:
        who = worker or self.worker
        body: Dict[str, Any] = {"worker": who, "kind": kind}
        if packet_id:
            body["packet_id"] = packet_id
        payload = self.post("/api/v2/lease", body)
        got = Lease.from_response(payload if isinstance(payload, dict) else {})
        if got is None:
            reason = (payload or {}).get("reason") if isinstance(payload, dict) else "池子空"
            log.info("lease 空: kind=%s worker=%s %s", kind, who, reason)
        return got

    def heartbeat(self, lease_id: str, stage: Optional[str] = None) -> dict:
        return self.post(f"/api/v2/heartbeat/{lease_id}", {"stage": stage} if stage else {})

    def abandon(self, packet_id: str, reason: str = "bad_quality", worker: str = "") -> dict:
        body: Dict[str, Any] = {"packet_id": packet_id, "reason": reason}
        who = worker or self.worker
        if who:
            body["worker"] = who
        return self.post("/api/v2/abandon", body)

    def release(self, lease_id: str, reason: str = "released") -> dict:
        try:
            return self.post(f"/api/v2/release/{lease_id}", {"reason": reason})
        except ApiError as e:
            log.warning("release %s 失败: %s", lease_id, e)
            return {"ok": False, "error": str(e)}

    def start_heartbeat(self, lease_id: str, interval: float = 60.0) -> Heartbeat:
        return Heartbeat(self, lease_id, interval).start()

    def save_seed(
        self,
        packet_id: str,
        labels: Any,
        *,
        worker: Optional[str] = None,
        lease_id: Optional[str] = None,
    ) -> dict:
        body: Dict[str, Any] = {"packet_id": packet_id, "labels": labels}
        who = worker or self.worker
        if who:
            body["worker"] = who
        if lease_id:
            body["lease_id"] = lease_id
        return self.post("/api/v2/seed", body)

    def get_gt(self, packet_id: str) -> List[dict]:
        try:
            payload = self.get(f"/api/v2/gt/{urllib.parse.quote(packet_id)}")
        except ApiError as e:
            log.info("GET gt/%s 失败（不当成致命）: %s", packet_id, e)
            return []
        rows = (payload or {}).get("gt") if isinstance(payload, dict) else None
        return [r for r in (rows or []) if isinstance(r, dict)]

    def submit(
        self,
        result: dict,
        bundle: Optional[Path] = None,
        *,
        source: str = "ai",
        lease_id: Optional[str] = None,
        worker: Optional[str] = None,
        workflow: Optional[str] = None,
        model_used: Optional[str] = None,
    ) -> dict:
        fields = {
            "source": source,
            "packet_id": str(result.get("packet_id") or ""),
        }
        if lease_id:
            fields["lease_id"] = str(lease_id)
        who = worker or self.worker or result.get("annotator")
        if who:
            fields["worker"] = str(who)
        if workflow or result.get("workflow"):
            fields["workflow"] = str(workflow or result.get("workflow"))
        if model_used or result.get("model_used"):
            fields["model_used"] = str(model_used or result.get("model_used"))
        result_bytes = json.dumps(result, ensure_ascii=False).encode()
        files: Dict[str, Tuple[str, bytes, str]] = {
            "result": ("result.json", result_bytes, "application/json"),
        }
        if bundle is not None:
            p = Path(bundle)
            ct = mimetypes.guess_type(p.name)[0] or "application/zip"
            files["bundle"] = (p.name, p.read_bytes(), ct)
        return self.post_multipart("/api/v2/submit", fields, files)
