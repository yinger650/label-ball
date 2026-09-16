#!/usr/bin/env python3
"""把 AOSS `packets/` 上已有的包补登记到中心库。

产包端直连服务端失败时（ACP 节点不出网、心跳通道废了）用这个补登。
在能 GetObject 的机器上跑：List `packets/` + 读 `meta.json` → POST `/api/v2/register`。

幂等：服务端按 `packet_id` 去重，重复跑只会 update 元数据、不会把状态拨回 available。

    ./venv/bin/python scripts/register_packets.py
    ./venv/bin/python scripts/register_packets.py --server https://example.com --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from aoss_store import Store, load_credentials  # noqa: E402
from producer.server_api import ServerAPI, resolve_node  # noqa: E402


def build_item(pid: str, meta: dict, size: Optional[int], job_id: str, node: str,
               info: Optional[dict] = None) -> Optional[dict]:
    if not isinstance(meta, dict) or not meta.get("targets"):
        return None
    item: Dict[str, Any] = {
        "packet_id": pid,
        "aoss_key": (
            f"packets/{pid}/" if (info or {}).get("kind") == "dir" else f"packets/{pid}.zip"
        ),
        "meta": meta,
        "n_targets": len(meta.get("targets") or []),
    }
    if size is not None:
        item["size_bytes"] = int(size)
    if job_id:
        item["producer_job_id"] = job_id
    if node:
        item["producer_node"] = node
    return item


def run(args) -> int:
    cred = load_credentials(args.credentials)
    store = Store(cred)
    print(f"[store] {json.dumps(cred.redacted, ensure_ascii=False)}", flush=True)

    api = ServerAPI(base=args.server, token=args.token, timeout=args.timeout)
    if not api.enabled and not args.dry_run:
        print("[err] 未设 --server / BALL_GT_SERVER，补登无处可去", flush=True)
        return 2

    job_id = args.job_id or ""
    node = args.node or resolve_node()

    packets = store.list_packets()
    print(f"[scan] AOSS packets/ 上 {len(packets)} 个包（zip 或前缀）", flush=True)

    items: List[dict] = []
    skipped = 0
    for pid, info in sorted(packets.items()):
        if pid.startswith("test_"):
            skipped += 1
            print(f"  跳过 {pid}：test_ 测试组，不进生产池", flush=True)
            continue
        meta = store.packet_index(pid)
        item = build_item(pid, meta or {}, info.get("size"), job_id, node, info)
        if item is None:
            skipped += 1
            print(f"  跳过 {pid}：meta.json 缺失或没有 targets", flush=True)
            continue
        items.append(item)

    print(f"[plan] 可登记 {len(items)}，跳过 {skipped}", flush=True)
    if args.dry_run:
        for it in items[:20]:
            print(f"  {it['packet_id']}  n_targets={it['n_targets']}", flush=True)
        if len(items) > 20:
            print(f"  … 还有 {len(items) - 20} 个", flush=True)
        return 0

    n_reg = n_upd = 0
    errors: List[dict] = []
    batch = max(1, int(args.batch))
    for i in range(0, len(items), batch):
        chunk = items[i : i + batch]
        r = api.register_packets(chunk)
        if r is None:
            print(f"[fail] 批次 {i // batch + 1}：{api.last_error}", flush=True)
            errors.append({"offset": i, "error": api.last_error})
            continue
        n_reg += int(r.get("registered") or 0)
        n_upd += int(r.get("updated") or 0)
        for e in r.get("errors") or []:
            errors.append(e)
        print(
            f"[ok] 批次 {i // batch + 1}  registered={r.get('registered')} "
            f"updated={r.get('updated')} errors={len(r.get('errors') or [])}",
            flush=True,
        )
    print(f"[done] registered={n_reg} updated={n_upd} errors={len(errors)}", flush=True)
    return 1 if errors and n_reg + n_upd == 0 else 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="把 AOSS packets/ 补登记到服务端（幂等）")
    p.add_argument("--server", default=os.environ.get("BALL_GT_SERVER") or "")
    p.add_argument("--token", default=os.environ.get("BALL_GT_API_TOKEN") or "")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--credentials", default=None)
    p.add_argument("--batch", type=int, default=50)
    p.add_argument("--job-id", default=os.environ.get("BALL_GT_JOB_ID") or "",
                   help="写进 producer_job_id；空则不填（避免本机 hostname-pid 污染归属）")
    p.add_argument("--node", default="")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run(parse_args()))
