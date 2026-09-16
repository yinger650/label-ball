#!/usr/bin/env python3
"""按 lease 带回的 `aoss_key` 把包下到本地工作区。

服务端不持有 AOSS 凭证，下载必须在能 GetObject 的客户端做。
新格式 aoss_key 是前缀 `packets/<id>/`；旧 zip 以 `.zip` 结尾。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence
from urllib.parse import urlparse

from packet import ALL_ARTIFACT_FILES, STAGE1_FILES, STAGE2_FILES, is_zip_key, prefix_from_key

log = logging.getLogger("client.fetch")

Progress = Optional[Callable[[int], None]]


def open_store(cred_path: Optional[str] = None):
    """构造 `aoss_store.Store`。凭证查找顺序见 `aoss_store.load_credentials`。"""
    from aoss_store import Store, load_credentials

    return Store(load_credentials(cred_path))


def normalize_aoss_key(aoss_key: str, prefix: str = "ball_det/") -> str:
    """把 lease 的 aoss_key 收成 Store.download_file 要的相对键。"""
    key = (aoss_key or "").strip()
    if not key:
        raise ValueError("空的 aoss_key")
    if "://" in key:
        path = urlparse(key).path.lstrip("/")
        parts = path.split("/", 1)
        key = parts[1] if len(parts) == 2 else path
    key = key.lstrip("/")
    pref = prefix.rstrip("/") + "/"
    if key.startswith(pref):
        key = key[len(pref):]
    return key


def zip_dest(work_root: Path, packet_id: str) -> Path:
    return Path(work_root) / packet_id / "packet.zip"


def download_packet(
    store,
    aoss_key: str,
    dest: Path,
    progress: Progress = None,
    skip_if_exists: bool = True,
) -> Path:
    """下载 zip 到 `dest`。已存在且非空则跳过。"""
    dest = Path(dest)
    if skip_if_exists and dest.exists() and dest.stat().st_size > 0:
        log.info("zip 已在本地，跳过下载: %s (%d bytes)", dest, dest.stat().st_size)
        return dest
    sub = normalize_aoss_key(aoss_key, getattr(store, "prefix", "ball_det/"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("下载 s3://%s/%s%s → %s", getattr(store, "bucket", "?"), store.prefix, sub, dest)
    store.download_file(sub, dest, progress=progress)
    return dest


def download_artifacts(
    store,
    aoss_key: str,
    dest_dir: Path,
    which: Sequence[str] = ALL_ARTIFACT_FILES,
    packet_id: str = "",
    progress: Progress = None,
    skip_if_exists: bool = True,
) -> Path:
    """按对象下载到 dest_dir。`which` 是相对包前缀的文件名。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    raw = normalize_aoss_key(aoss_key, getattr(store, "prefix", "ball_det/"))
    if is_zip_key(raw):
        return download_packet(store, aoss_key, dest_dir / "packet.zip",
                               progress=progress, skip_if_exists=skip_if_exists)
    prefix = prefix_from_key(raw, packet_id)
    for name in which:
        dest = dest_dir / name
        if skip_if_exists and dest.exists() and dest.stat().st_size > 0:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        sub = f"{prefix}{name}"
        log.info("下载 %s → %s", sub, dest)
        store.download_file(sub, dest, progress=progress)
    return dest_dir


def ensure_packet_files(
    store,
    aoss_key: str,
    dest_dir: Path,
    packet_id: str,
    which: Iterable[str] = STAGE1_FILES,
    progress: Progress = None,
) -> Path:
    dest_dir = Path(dest_dir)
    if is_zip_key(aoss_key):
        return download_packet(store, aoss_key, dest_dir / "packet.zip", progress=progress)
    return download_artifacts(
        store, aoss_key, dest_dir, which=tuple(which),
        packet_id=packet_id, progress=progress,
    )


# 给调用方复用阶段常量
STAGE1 = STAGE1_FILES
STAGE2 = STAGE2_FILES
