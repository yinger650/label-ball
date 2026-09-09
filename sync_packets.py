#!/usr/bin/env python3
"""把 AOSS 上的标注包同步到公网标注站本地磁盘。

SenseCore 公网 endpoint 对部分出口 IP 允许 List/Put，但拒绝 GetObject。
标注员浏览器和公网 VPS 都拿不到 zip，所以需要一台「能 Get」的机器
（内网 / 已放行 IP）跑本脚本，把 packets/*.zip 拉到标注站。

用法（在能 Get 的机器上）:
  export BALL_GT_CREDENTIALS=./credentials.json
  export BALL_GT_SYNC_DEST=root@yinger650.com:/var/lib/ball_gt_packets
  python sync_packets.py --loop
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from aoss_store import Store, load_credentials  # noqa: E402


def sync_once(store: Store, dest: str, local_cache: Path) -> int:
    local_cache.mkdir(parents=True, exist_ok=True)
    packets = store.list_packets()
    n = 0
    for pid, meta in sorted(packets.items()):
        local = local_cache / f"{pid}.zip"
        if local.exists() and local.stat().st_size == meta.get("size"):
            continue
        print(f"[get] {pid} ({meta.get('size', 0)/1e6:.1f} MB)", flush=True)
        tmp = local.with_suffix(".zip.part")
        store.download_file(f"packets/{pid}.zip", tmp)
        tmp.replace(local)
        n += 1
    if dest:
        # rsync 只传 zip；标注站按 packet_id.zip 取用
        cmd = [
            "rsync", "-a", "--partial",
            f"{local_cache}/",
            dest.rstrip("/") + "/",
        ]
        print("[rsync]", " ".join(cmd), flush=True)
        subprocess.check_call(cmd)
    print(f"[done] 新同步 {n} 个，本地共 {len(list(local_cache.glob('*.zip')))} 个", flush=True)
    return n


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--credentials", default=None)
    p.add_argument("--cache", default=str(HERE / ".packet_cache"))
    p.add_argument("--dest", default=os.environ.get("BALL_GT_SYNC_DEST", ""),
                   help="rsync 目标，如 root@host:/var/lib/ball_gt_packets/")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=120)
    args = p.parse_args()

    store = Store(load_credentials(args.credentials))
    cache = Path(args.cache)
    while True:
        try:
            sync_once(store, args.dest, cache)
        except Exception as e:  # noqa: BLE001
            print(f"[err] {type(e).__name__}: {e}", flush=True)
        if not args.loop:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
