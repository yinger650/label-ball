#!/usr/bin/env python3
"""多供应商的 OpenAI 兼容视觉模型池。

四家（智谱 / MiniMax / DeepSeek / 火山豆包）都提供 OpenAI 兼容的 `/chat/completions`，图片走
base64 `image_url`，所以适配层可以薄到只剩配置。真正需要代码处理的只有四件事：

1. **必须关掉思考**。我们要的是「哪个格子」这一个词，不是几百 token 的推理过程。
   各家关思考的字段名不一样，而且写错了会直接 400，所以 `extra_body` 里的每个键
   都做了「被拒就去掉再试一次」的降级，并把去掉的键记下来——否则一次参数拼写
   错误会被当成「这家不可用」。
2. **Key 只从环境变量取**。配置文件里只写变量名，任何情况下不打印、不落盘、
   不进缓存键的明文部分。
3. **限流与并发**。单包上百帧、每帧一到两次调用，不限速会直接被 429 打死。
   每家一个信号量 + 一个令牌桶。
4. **响应缓存**。调提示词时同一张图会被反复打，缓存键是
   `sha256(图片字节 ‖ 提示词 ‖ 模型 ‖ 采样参数)`，改提示词自动失效，不改就不花钱。

失败处理是「解析失败重试一次 → 换 fallback 供应商 → 指数退避」，三步都走完还不行
就让这一帧返回 null，交给 `track.py` 插值。**宁可放弃一帧，也不要让模型瞎猜**。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROVIDERS = Path(__file__).resolve().parent / "providers.yaml"
DEFAULT_CACHE = Path("~/.cache/ball_ai/vlm_cache.sqlite").expanduser()


# ---------------------------------------------------------------- .env


def load_dotenv(path: Optional[Path] = None) -> None:
    """把仓库根 `.env` 里的键读进 os.environ（不覆盖已有的）。

    只解析 `KEY=VALUE`，不做 shell 展开。**任何时候都不打印值。**
    """
    p = path or (REPO_ROOT / ".env")
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


# ---------------------------------------------------------------- 配置


@dataclass
class ProviderCfg:
    name: str
    base_url: str
    model: str
    api_key_env: str
    roles: List[str] = field(default_factory=list)
    max_concurrency: int = 4
    rpm: int = 60
    timeout: float = 90.0
    max_tokens: int = 256
    temperature: float = 0.0
    detail: Optional[str] = None  # 进 image_url 的 detail 字段（MiniMax / 部分家支持）
    max_image_side: Optional[int] = None  # 本地先缩到这个长边，省得被服务端乱压
    image_format: str = "png"
    extra_body: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    notes: str = ""

    @property
    def has_key(self) -> bool:
        return bool(os.environ.get(self.api_key_env))


def load_providers(path: Optional[Path] = None) -> Dict[str, ProviderCfg]:
    import yaml

    p = Path(path or DEFAULT_PROVIDERS)
    doc = yaml.safe_load(p.read_text()) or {}
    out: Dict[str, ProviderCfg] = {}
    for name, d in (doc.get("providers") or {}).items():
        d = dict(d or {})
        if "api_key" in d or "key" in d:
            raise ValueError(f"{p}: 供应商 {name} 里出现了疑似明文 key 的字段，只允许写 api_key_env")
        cfg = ProviderCfg(name=name, **d)
        override = os.environ.get("GLM_BASE_URL", "").strip()
        if override and cfg.api_key_env == "GLM_KEY":
            cfg.base_url = override.rstrip("/")
        out[name] = cfg
    return out


def role_map(path: Optional[Path] = None) -> Dict[str, List[str]]:
    """角色 → 供应商优先级列表。`primary` / `fallback` / `voter` 由工作流引用。"""
    import yaml

    doc = yaml.safe_load(Path(path or DEFAULT_PROVIDERS).read_text()) or {}
    return {k: list(v) for k, v in (doc.get("roles") or {}).items()}


# ---------------------------------------------------------------- 结果


@dataclass
class VlmResult:
    ok: bool
    text: str = ""
    provider: str = ""
    model: str = ""
    latency: float = 0.0
    cached: bool = False
    usage: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    dropped_params: List[str] = field(default_factory=list)
    finish_reason: str = ""
    reasoning: str = ""

    @property
    def reasoning_leaked(self) -> bool:
        """关思考没生效的信号：回答里带 <think>、或者长得离谱。"""
        t = self.text or ""
        return bool(self.reasoning) or bool(re.search(r"<think|◁think|reasoning_content", t, re.I)) or len(t) > 1500


_THINK_TAG = re.compile(r"<think>(.*?)</think>|◁think▷(.*?)◁/think▷", re.S | re.I)


def _message_text(msg) -> str:
    if msg is None:
        return ""
    c = getattr(msg, "content", "") or ""
    if isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, dict) and p.get("text"):
                parts.append(str(p["text"]))
            elif isinstance(p, str):
                parts.append(p)
            else:
                t = getattr(p, "text", None)
                if t:
                    parts.append(str(t))
        return "\n".join(parts)
    return str(c)


def extract_reasoning(msg, text: str = "") -> str:
    """从 message.reasoning_content / <think> 标签里抽出思考正文。"""
    bits: List[str] = []
    extra = None
    if msg is not None:
        extra = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
        if extra is None:
            dump = getattr(msg, "model_extra", None) or {}
            if isinstance(dump, dict):
                extra = dump.get("reasoning_content") or dump.get("reasoning")
        if extra is None and hasattr(msg, "model_dump"):
            try:
                d = msg.model_dump()
                extra = d.get("reasoning_content") or d.get("reasoning")
            except Exception:
                extra = None
    if extra:
        bits.append(str(extra).strip())
    for m in _THINK_TAG.finditer(text or ""):
        bits.append((m.group(1) or m.group(2) or "").strip())
    out, seen = [], set()
    for b in bits:
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return "\n\n".join(out)


# ---------------------------------------------------------------- 缓存


class ResponseCache:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or DEFAULT_CACHE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS resp ("
            "k TEXT PRIMARY KEY, provider TEXT, model TEXT, text TEXT,"
            "usage TEXT, latency REAL, ts REAL)"
        )
        self._db.commit()

    @staticmethod
    def key(images: Sequence[bytes], prompt: str, system: str, model: str, params: dict) -> str:
        h = hashlib.sha256()
        for b in images:
            h.update(hashlib.sha256(b).digest())
        h.update(prompt.encode())
        h.update((system or "").encode())
        h.update(model.encode())
        h.update(json.dumps(params, sort_keys=True, default=str).encode())
        return h.hexdigest()

    def get(self, k: str) -> Optional[VlmResult]:
        with self._lock:
            row = self._db.execute("SELECT provider,model,text,usage,latency FROM resp WHERE k=?", (k,)).fetchone()
        if not row:
            return None
        usage = json.loads(row[3] or "{}")
        return VlmResult(
            ok=True, provider=row[0], model=row[1], text=row[2],
            usage=usage, latency=row[4] or 0.0, cached=True,
            reasoning=str(usage.get("reasoning") or ""),
        )

    def put(self, k: str, r: VlmResult) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO resp VALUES (?,?,?,?,?,?,?)",
                (k, r.provider, r.model, r.text, json.dumps(r.usage), r.latency, time.time()),
            )
            self._db.commit()


# ---------------------------------------------------------------- 限流


class _Limiter:
    """信号量 + 令牌桶。两个都要：并发管住瞬时压力，rpm 管住平均速率。"""

    def __init__(self, concurrency: int, rpm: int):
        self.sem = threading.Semaphore(max(1, concurrency))
        self.interval = 60.0 / max(1, rpm)
        self._next = 0.0
        self._lock = threading.Lock()

    def __enter__(self):
        self.sem.acquire()
        with self._lock:
            wait = max(0.0, self._next - time.monotonic())
            self._next = max(self._next, time.monotonic()) + self.interval
        if wait > 0:
            time.sleep(wait)
        return self

    def __exit__(self, *a):
        self.sem.release()
        return False


# ---------------------------------------------------------------- 供应商


# 这些键一旦被服务端拒绝就单独摘掉重试。关思考是「能关就关」，关不掉不该整家不可用。
_DROPPABLE = ("thinking", "enable_thinking", "reasoning", "reasoning_effort", "thinking_budget", "chat_template_kwargs")
_PARAM_ERR = re.compile(
    r"(unsupported|unknown|invalid|unrecognized|not support|无效|不支持|不存在).{0,80}"
    r"(param|field|argument|key|thinking|reasoning|detail|参数|字段)",
    re.I,
)


class Provider:
    def __init__(self, cfg: ProviderCfg, cache: Optional[ResponseCache] = None):
        self.cfg = cfg
        self.cache = cache
        self.limiter = _Limiter(cfg.max_concurrency, cfg.rpm)
        self._bad_params: set = set()
        self._client = None

    # --- 客户端 ---

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            key = os.environ.get(self.cfg.api_key_env)
            if not key:
                raise RuntimeError(f"环境变量 {self.cfg.api_key_env} 没有设置")
            self._client = OpenAI(base_url=self.cfg.base_url, api_key=key, timeout=self.cfg.timeout, max_retries=0)
        return self._client

    # --- 消息 ---

    def _image_part(self, data: bytes, fmt: str) -> dict:
        mime = {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg", "jpeg": "image/jpeg"}[fmt]
        url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        part: Dict[str, Any] = {"type": "image_url", "image_url": {"url": url}}
        if self.cfg.detail:
            part["image_url"]["detail"] = self.cfg.detail
        return part

    def _video_part(self, spec: dict) -> dict:
        """MiniMax-M3 的 video_url 块。url 可以是 https / data:video/mp4;base64 / mm_file://。"""
        vu: Dict[str, Any] = {"url": spec["url"]}
        if spec.get("detail") or self.cfg.detail:
            vu["detail"] = spec.get("detail") or self.cfg.detail
        if spec.get("fps") is not None:
            vu["fps"] = float(spec["fps"])
        if spec.get("max_long_side_pixel") is not None:
            vu["max_long_side_pixel"] = int(spec["max_long_side_pixel"])
        return {"type": "video_url", "video_url": vu}

    def _messages(
        self,
        prompt: str,
        images: Sequence[bytes],
        system: str,
        fmt: str,
        videos: Sequence[dict] = (),
    ) -> List[dict]:
        content: List[dict] = [self._video_part(v) for v in videos]
        content.extend(self._image_part(b, fmt) for b in images)
        content.append({"type": "text", "text": prompt})
        msgs: List[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": content})
        return msgs

    def upload_file(self, path: Path, purpose: str = "video") -> str:
        """走 Files API，返回 `mm_file://{id}`。URL/base64 视频上限 50 MB 时用这个。"""
        p = Path(path)
        with p.open("rb") as f:
            up = self.client.files.create(file=(p.name, f, "video/mp4"), purpose=purpose)
        fid = getattr(up, "id", None) or (up.get("id") if isinstance(up, dict) else None)
        if not fid:
            raise RuntimeError(f"Files API 没返回 id: {up}")
        return f"mm_file://{fid}"

    def _body(self) -> dict:
        return {k: v for k, v in (self.cfg.extra_body or {}).items() if k not in self._bad_params}

    # --- 调用 ---

    def call(
        self,
        prompt: str,
        images: Sequence[bytes] = (),
        system: str = "",
        max_tokens: Optional[int] = None,
        image_format: Optional[str] = None,
        use_cache: bool = True,
        retries: int = 2,
        videos: Sequence[dict] = (),
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> VlmResult:
        fmt = image_format or self.cfg.image_format
        max_tokens = max_tokens or self.cfg.max_tokens
        body_cfg = extra_body if extra_body is not None else self.cfg.extra_body
        params = {
            "t": self.cfg.temperature, "mt": max_tokens, "eb": body_cfg,
            "d": self.cfg.detail, "vid": [v.get("url", "")[:80] for v in videos],
            "vfps": [v.get("fps") for v in videos],
            "vside": [v.get("max_long_side_pixel") for v in videos],
        }
        ck = ResponseCache.key(images, prompt, system, f"{self.cfg.name}/{self.cfg.model}", params)
        if use_cache and self.cache:
            hit = self.cache.get(ck)
            if hit:
                return hit

        msgs = self._messages(prompt, images, system, fmt, videos=videos)
        last = ""
        for attempt in range(max(1, retries)):
            try:
                with self.limiter:
                    t0 = time.monotonic()
                    used_body = extra_body if extra_body is not None else self._body()
                    resp = self.client.chat.completions.create(
                        model=self.cfg.model,
                        messages=msgs,
                        temperature=self.cfg.temperature,
                        max_tokens=max_tokens,
                        extra_body=used_body or None,
                    )
                dt = time.monotonic() - t0
                ch = resp.choices[0] if resp.choices else None
                msg = getattr(ch, "message", None) if ch else None
                text = _message_text(msg)
                reasoning = extract_reasoning(msg, text)
                usage = resp.usage.model_dump() if getattr(resp, "usage", None) else {}
                if reasoning:
                    usage["reasoning"] = reasoning
                    usage["reasoning_content_len"] = len(reasoning)
                r = VlmResult(ok=bool(text.strip()), text=text, provider=self.cfg.name, model=self.cfg.model,
                              latency=dt, usage=usage, dropped_params=sorted(self._bad_params),
                              finish_reason=getattr(ch, "finish_reason", "") or "",
                              reasoning=reasoning)
                if not r.ok:
                    r.error = f"空回答 (finish_reason={r.finish_reason})"
                    last = r.error
                    continue
                if use_cache and self.cache:
                    self.cache.put(ck, r)
                return r
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                dropped = self._drop_bad_param(last)
                if dropped:
                    continue  # 摘掉不认的参数立刻重试，不算退避
                if attempt + 1 < max(1, retries):
                    time.sleep(1.5 * (2**attempt))
        return VlmResult(ok=False, provider=self.cfg.name, model=self.cfg.model, error=last,
                         dropped_params=sorted(self._bad_params))

    def _drop_bad_param(self, msg: str) -> bool:
        if not _PARAM_ERR.search(msg):
            return False
        for k in _DROPPABLE:
            if k in self._bad_params:
                continue
            if k in msg or k in (self.cfg.extra_body or {}):
                self._bad_params.add(k)
                return True
        return False

    def list_models(self) -> List[str]:
        try:
            return sorted(m.id for m in self.client.models.list().data)
        except Exception:
            return []


# ---------------------------------------------------------------- JSON


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Optional[dict]:
    """从模型回答里抠出第一个 JSON 对象。

    模型总会加点废话或者 markdown 围栏，指望它输出纯 JSON 是不现实的；
    但**只接受能解析成对象的东西**，抠不出来就当这次调用失败，绝不猜。
    """
    if not text:
        return None
    m = _FENCE.search(text)
    cands = ([m.group(1)] if m else []) + [text]
    for c in cands:
        c = c.strip()
        try:
            v = json.loads(c)
            if isinstance(v, dict):
                return v
        except Exception:
            pass
        depth, start = 0, -1
        for i, ch in enumerate(c):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        v = json.loads(c[start : i + 1])
                        if isinstance(v, dict):
                            return v
                    except Exception:
                        pass
    return None


def validate(obj: dict, schema: Dict[str, type], required: Sequence[str] = ()) -> Tuple[bool, str]:
    """极简 schema 校验。够用就行——我们只有「格子号 + 置信度」这一种形状。"""
    for k in required:
        if k not in obj:
            return False, f"缺字段 {k}"
    for k, t in schema.items():
        if k in obj and obj[k] is not None and not isinstance(obj[k], t):
            return False, f"{k} 类型应为 {t}，实际 {type(obj[k]).__name__}"
    return True, ""


# ---------------------------------------------------------------- 池


class VlmPool:
    """按角色取供应商，带自动降级。"""

    def __init__(self, providers_path: Optional[Path] = None, cache_path: Optional[Path] = None,
                 use_cache: bool = True):
        load_dotenv()
        self.cfgs = load_providers(providers_path)
        self.roles = role_map(providers_path)
        self.cache = ResponseCache(cache_path) if use_cache else None
        self._providers: Dict[str, Provider] = {}
        self.calls = 0
        self.cache_hits = 0
        self._lock = threading.Lock()

    def provider(self, name: str) -> Provider:
        with self._lock:
            if name not in self._providers:
                self._providers[name] = Provider(self.cfgs[name], self.cache)
            return self._providers[name]

    def chain(self, role: str) -> List[str]:
        """角色对应的供应商链，按顺序降级。没配这个角色就退到 primary。"""
        names = self.roles.get(role) or self.roles.get("primary") or list(self.cfgs)
        return [n for n in names if n in self.cfgs and self.cfgs[n].enabled and self.cfgs[n].has_key]

    def available(self) -> List[str]:
        return [n for n, c in self.cfgs.items() if c.enabled and c.has_key]

    def pin(self, name: str) -> None:
        """把所有角色链收成这一家，禁止降级。对比实验用。"""
        if name not in self.cfgs:
            raise ValueError(f"未知供应商 {name}")
        cfg = self.cfgs[name]
        if not cfg.enabled:
            raise RuntimeError(f"{name} 已停用")
        if not cfg.has_key:
            raise RuntimeError(f"{name} 没有 key（环境变量 {cfg.api_key_env}）")
        self.roles = {role: [name] for role in (self.roles or {"primary": []})}
        if "primary" not in self.roles:
            self.roles["primary"] = [name]

    def _chain_or_named(self, role: str, provider: Optional[str] = None) -> Tuple[List[str], str]:
        """指定供应商时不走角色降级，方便对比「这一家到底选了哪格」。"""
        if provider:
            if provider not in self.cfgs:
                return [], f"未知供应商 {provider}"
            cfg = self.cfgs[provider]
            if not cfg.enabled:
                return [], f"{provider} 已停用"
            if not cfg.has_key:
                return [], f"{provider} 没有 key（环境变量 {cfg.api_key_env}）"
            return [provider], ""
        chain = self.chain(role)
        if not chain:
            return [], f"角色 {role} 没有可用供应商（key 缺失或都被停用）"
        return chain, ""

    def ask(self, role: str, prompt: str, images: Sequence[bytes] = (), system: str = "",
            max_tokens: Optional[int] = None, use_cache: bool = True,
            provider: Optional[str] = None, videos: Sequence[dict] = (),
            extra_body: Optional[Dict[str, Any]] = None) -> VlmResult:
        chain, err = self._chain_or_named(role, provider)
        if not chain:
            return VlmResult(ok=False, error=err)
        last = VlmResult(ok=False)
        for name in chain:
            r = self.provider(name).call(
                prompt, images, system, max_tokens, use_cache=use_cache, videos=videos,
                extra_body=extra_body,
            )
            with self._lock:
                self.calls += 1
                self.cache_hits += int(r.cached)
            if r.ok:
                return r
            last = r
        return last

    def ask_json(self, role: str, prompt: str, images: Sequence[bytes] = (), system: str = "",
                 schema: Optional[Dict[str, type]] = None, required: Sequence[str] = (),
                 max_tokens: Optional[int] = None, use_cache: bool = True,
                 provider: Optional[str] = None,
                 videos: Sequence[dict] = (),
                 extra_body: Optional[Dict[str, Any]] = None) -> Tuple[Optional[dict], VlmResult]:
        chain, err = self._chain_or_named(role, provider)
        if not chain:
            return None, VlmResult(ok=False, error=err)
        last = VlmResult(ok=False)
        for name in chain:
            p = self.provider(name)
            for attempt in range(2):  # 解析失败给一次重试（第二次绕开缓存）
                r = p.call(
                    prompt, images, system, max_tokens,
                    use_cache=use_cache and attempt == 0, videos=videos,
                    extra_body=extra_body,
                )
                with self._lock:
                    self.calls += 1
                    self.cache_hits += int(r.cached)
                last = r
                if not r.ok:
                    break
                obj = extract_json(r.text)
                if obj is None:
                    r.error = "回答里找不到 JSON"
                    continue
                if schema:
                    ok, why = validate(obj, schema, required)
                    if not ok:
                        r.error = f"schema 不过: {why}"
                        continue
                return obj, r
        return None, last
