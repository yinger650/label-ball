#!/usr/bin/env python3
"""足球 GT 标注任务池的对象存储后端。

对象存储上的布局（桶 `wangmin`，前缀 `ball_det/`）：

    packets/<packet_id>/…            新格式：meta.json + clip.mp4 + dog.mp4 + scan + thumb/
    packets/<packet_id>.zip          旧 zip（迁移期仍认）
    packets/<packet_id>.meta.json    轻量索引：列任务时不必下载整包
    claims/<packet_id>/<who>.json    领取标记
    results/<packet_id>__<who>.json  标注结果
    produced.jsonl                   生产端已产出记录（去重用，只追加）

**没有中心服务**：任务池状态完全由对象的存在与否表达，生产端与各标注员之间只通过
对象存储通信。这样标注工具可以直接分发给任何能连内网的机器，不需要额外部署。

领取用「先到先得 + 乐观复核」：写下自己的 claim 后重新列一次，若别人的 claim 时间戳
更早就让出。凭证是共享的，做不到真正的互斥，但标注员数量是个位数、包有几百个，
撞车概率极低，而复核能把撞车变成「一方让出」而不是「两人重复标同一包」。
"""

from __future__ import annotations

import configparser
import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

DEFAULT_ENDPOINT = "https://aoss-external.cn-sh-01b.sensecoreapi-oss.cn"
DEFAULT_BUCKET = "wangmin"
DEFAULT_PREFIX = "ball_det/"
# 领取多久无结果就视为放弃，重新放回池子
CLAIM_TTL_SEC = 36 * 3600


@dataclass
class Credentials:
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str = DEFAULT_BUCKET
    prefix: str = DEFAULT_PREFIX
    source: str = ""

    @property
    def redacted(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "bucket": self.bucket,
            "prefix": self.prefix,
            "access_key": self.access_key[:4] + "…",
            "source": self.source,
        }


def _from_aoss_conf(path: Path) -> Optional[Credentials]:
    """开发机上的便利路径：直接借用 aoss.conf 的 tetrasvideo 凭证。

    注意 endpoint 不能用 aoss.conf 里 tetrasvideo 自己的 host_base（那是 cn-sh-01），
    `wangmin` 桶在 **cn-sh-01b** 上，只有换到这个 endpoint 才看得到。
    """
    if not path.exists():
        return None
    cp = configparser.ConfigParser()
    cp.read(path)
    if "tetrasvideo" not in cp:
        return None
    s = cp["tetrasvideo"]
    if not s.get("access_key"):
        return None
    return Credentials(
        endpoint=DEFAULT_ENDPOINT,
        access_key=s["access_key"],
        secret_key=s["secret_key"],
        source=f"{path} [tetrasvideo]",
    )


def _read_dotenv(path: Path) -> Dict[str, str]:
    """读取项目 `.env` 中的 AOSS 配置，不修改进程环境。"""
    keys = {
        "AOSS_ENDPOINT", "AOSS_ACCESS_KEY", "AOSS_SECRET_KEY",
        "AOSS_BUCKET", "AOSS_PREFIX",
    }
    if not path.exists():
        return {}
    out: Dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip() in keys:
            out[key.strip()] = value.strip().strip("\"'")
    return out


def _from_env(values: Dict[str, str], source: str) -> Optional[Credentials]:
    access_key = values.get("AOSS_ACCESS_KEY", "")
    secret_key = values.get("AOSS_SECRET_KEY", "")
    if not access_key or not secret_key:
        return None
    return Credentials(
        endpoint=values.get("AOSS_ENDPOINT") or DEFAULT_ENDPOINT,
        access_key=access_key,
        secret_key=secret_key,
        bucket=values.get("AOSS_BUCKET") or DEFAULT_BUCKET,
        prefix=values.get("AOSS_PREFIX") or DEFAULT_PREFIX,
        source=source,
    )


def load_credentials(explicit: Optional[str] = None) -> Credentials:
    """按优先级查找凭证：显式路径 → 环境变量 → 项目 .env → 凭证文件 → aoss.conf。"""
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("BALL_GT_CREDENTIALS"):
        candidates.append(Path(os.environ["BALL_GT_CREDENTIALS"]))
    here = Path(__file__).resolve().parent
    process_env = {key: os.environ[key] for key in (
        "AOSS_ENDPOINT", "AOSS_ACCESS_KEY", "AOSS_SECRET_KEY", "AOSS_BUCKET", "AOSS_PREFIX",
    ) if os.environ.get(key)}
    cred = _from_env(process_env, "environment")
    if cred:
        return cred
    cred = _from_env(_read_dotenv(here / ".env"), str(here / ".env"))
    if cred:
        return cred
    candidates += [
        here / "credentials.json",
        Path.home() / ".ball_gt_credentials.json",
    ]
    for p in candidates:
        if p.exists():
            d = json.loads(p.read_text())
            return Credentials(
                endpoint=d.get("endpoint", DEFAULT_ENDPOINT),
                access_key=d["access_key"],
                secret_key=d["secret_key"],
                bucket=d.get("bucket", DEFAULT_BUCKET),
                prefix=d.get("prefix", DEFAULT_PREFIX),
                source=str(p),
            )
    for conf in (Path("/mnt/afs/wangmin/aoss.conf"), Path.home() / "aoss.conf"):
        cred = _from_aoss_conf(conf)
        if cred:
            return cred
    raise SystemExit(
        "找不到对象存储凭证。请把 credentials.json 放到以下任一位置：\n"
        f"  {here / 'credentials.json'}\n"
        f"  {Path.home() / '.ball_gt_credentials.json'}\n"
        "格式见 credentials.example.json"
    )


def default_annotator() -> str:
    return os.environ.get("BALL_GT_ANNOTATOR") or f"{os.environ.get('USER', 'anon')}@{socket.gethostname()}"


class Store:
    """标注任务池。所有方法都直接打对象存储，不做本地缓存（池子小，列举很快）。"""

    def __init__(self, cred: Credentials):
        import boto3
        from botocore.config import Config

        self.cred = cred
        self.bucket = cred.bucket
        self.prefix = cred.prefix.rstrip("/") + "/"
        self.s3 = boto3.client(
            "s3",
            endpoint_url=cred.endpoint,
            aws_access_key_id=cred.access_key,
            aws_secret_access_key=cred.secret_key,
            config=Config(retries={"max_attempts": 5, "mode": "standard"}),
        )

    # ---------- 底层 ----------

    def _key(self, *parts: str) -> str:
        return self.prefix + "/".join(p.strip("/") for p in parts)

    def _list(self, sub: str) -> List[dict]:
        out: List[dict] = []
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self._key(sub)}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            out += r.get("Contents") or []
            if not r.get("IsTruncated"):
                break
            token = r.get("NextContinuationToken")
        return out

    def put_json(self, sub: str, obj: dict) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self._key(sub),
            Body=json.dumps(obj, ensure_ascii=False).encode(),
            ContentType="application/json",
        )

    def get_json(self, sub: str) -> Optional[dict]:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=self._key(sub))["Body"].read()
        except Exception:
            return None
        return json.loads(body)

    def exists(self, sub: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(sub))
            return True
        except Exception:
            return False

    def upload_file(self, sub: str, path: Path, progress: Optional[Callable] = None) -> None:
        self.s3.upload_file(str(path), self.bucket, self._key(sub), Callback=progress)

    def download_file(self, sub: str, path: Path, progress: Optional[Callable] = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        self.s3.download_file(self.bucket, self._key(sub), str(tmp), Callback=progress)
        tmp.replace(path)

    def append_line(self, sub: str, obj: dict) -> None:
        """对象存储没有 append，用「读-改-写」模拟。只有生产端单写者会调用。"""
        old = b""
        try:
            old = self.s3.get_object(Bucket=self.bucket, Key=self._key(sub))["Body"].read()
        except Exception:
            pass
        line = json.dumps(obj, ensure_ascii=False).encode() + b"\n"
        self.s3.put_object(Bucket=self.bucket, Key=self._key(sub), Body=old + line)

    # ---------- 任务池 ----------

    def list_packets(self) -> Dict[str, dict]:
        """列出所有包的轻量索引：{packet_id: meta 摘要}。新前缀与旧 zip 都认。"""
        out: Dict[str, dict] = {}
        base = self._key("packets") + "/"
        for o in self._list("packets/"):
            rel = o["Key"][len(base):] if o["Key"].startswith(base) else o["Key"].rsplit("/", 1)[-1]
            if rel.endswith(".zip") and "/" not in rel:
                pid = rel[: -len(".zip")]
                out.setdefault(pid, {"packet_id": pid, "size": o["Size"],
                                     "last_modified": str(o["LastModified"]), "kind": "zip"})
            elif rel.endswith("/meta.json"):
                pid = rel[: -len("/meta.json")]
                if "/" not in pid:
                    rec = out.setdefault(pid, {"packet_id": pid, "size": 0,
                                               "last_modified": str(o["LastModified"]), "kind": "dir"})
                    rec["kind"] = "dir"
                    rec["size"] = rec.get("size") or o["Size"]
            elif rel.endswith(".meta.json") and "/" not in rel:
                pid = rel[: -len(".meta.json")]
                out.setdefault(pid, {"packet_id": pid, "size": o["Size"],
                                     "last_modified": str(o["LastModified"]), "kind": "meta"})
        return out

    def packet_index(self, packet_id: str) -> Optional[dict]:
        return self.get_json(f"packets/{packet_id}/meta.json") or self.get_json(f"packets/{packet_id}.meta.json")

    def list_claims(self) -> Dict[str, List[dict]]:
        out: Dict[str, List[dict]] = {}
        # 注意用 _key("claims") + "/"：_key 会剥掉尾部斜杠，直接拿 _key("claims/")
        # 的长度去切会多留一个前导 "/"，packet_id 就被解析成空串，领取过的包永远认不回来
        base = self._key("claims") + "/"
        for o in self._list("claims/"):
            rel = o["Key"][len(base) :]
            if "/" not in rel:
                continue
            pid, who = rel.split("/", 1)
            out.setdefault(pid, []).append(
                {"who": who[:-5] if who.endswith(".json") else who, "key": rel, "mtime": o["LastModified"].timestamp()}
            )
        return out

    def list_results(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for o in self._list("results/"):
            name = o["Key"].rsplit("/", 1)[-1]
            if not name.endswith(".json") or "__" not in name:
                continue
            pid, who = name[: -len(".json")].rsplit("__", 1)
            out.setdefault(pid, []).append(who)
        return out

    def pool_status(self, annotator: str) -> dict:
        """一次列举算出池子全貌，供 UI 展示与领取决策。"""
        packets = self.list_packets()
        claims = self.list_claims()
        results = self.list_results()
        now = time.time()
        avail, mine, taken, done = [], [], [], []
        for pid in sorted(packets):
            if pid in results:
                done.append(pid)
                continue
            cs = claims.get(pid) or []
            if any(c["who"] == annotator for c in cs):
                mine.append(pid)
            elif cs and any(now - c["mtime"] < CLAIM_TTL_SEC for c in cs):
                taken.append(pid)
            else:
                avail.append(pid)
        return {
            "n_packets": len(packets),
            "available": avail,
            "mine": mine,
            "taken": taken,
            "done": done,
            "packets": packets,
        }

    def claim(self, packet_id: str, annotator: str) -> bool:
        """尝试领取。返回 False 表示已被别人抢先（调用方应换一个包）。"""
        now = time.time()
        existing = (self.list_claims().get(packet_id) or [])
        for c in existing:
            if c["who"] != annotator and now - c["mtime"] < CLAIM_TTL_SEC:
                return False
        self.put_json(
            f"claims/{packet_id}/{annotator}.json",
            {"packet_id": packet_id, "annotator": annotator, "claimed_at": now},
        )
        # 乐观复核：同一瞬间两人都写成功时，让时间戳更早的一方保留
        for c in self.list_claims().get(packet_id) or []:
            if c["who"] != annotator and c["mtime"] < now - 1.0:
                return False
        return True

    def submit_result(self, packet_id: str, annotator: str, result: dict) -> str:
        sub = f"results/{packet_id}__{annotator}.json"
        self.put_json(sub, result)
        return sub
