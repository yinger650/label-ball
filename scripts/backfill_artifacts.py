#!/usr/bin/env python3
"""按新编排重做已有包：clip + dog + scan + thumb，上传前缀并登记缩略件。

不管包是否已标注 / 已审核，都不改 packets.state，只更新 aoss_key / meta / thumbs。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aoss_store import Store, load_credentials  # noqa: E402
from packet import CLIP_FPS, PacketMeta, choose_decim, extract_packet, packet_prefix  # noqa: E402
from producer.derive import derive_from_clip  # noqa: E402
from producer.server_api import ServerAPI  # noqa: E402

ARTIFACTS = (
    "clip.mp4", "dog.mp4", "scan_dog.jpg", "scan_time.jpg",
    "thumb/clip.mp4", "thumb/dog.mp4", "thumb/scan_dog.jpg", "thumb/scan_time.jpg",
)


def _store() -> Store:
    return Store(load_credentials())


def _pids(store: Store, include_test: bool) -> List[str]:
    out = []
    for pid in sorted(store.list_packets()):
        if not include_test and pid.startswith("test_"):
            continue
        out.append(pid)
    return out


def _in_shard(pid: str, index: int, total: int) -> bool:
    if total <= 1:
        return True
    return (sum(ord(c) for c in pid) + len(pid)) % total == index


def _load_meta(d: Path, pid: str) -> PacketMeta:
    for p in (d / "meta.json",):
        if p.exists():
            return PacketMeta.from_dict(json.loads(p.read_text(encoding="utf-8")))
    raise FileNotFoundError(f"{pid} 没有 meta.json")


def _prepare_source(store: Store, pid: str, d: Path) -> Tuple[PacketMeta, Path]:
    d.mkdir(parents=True, exist_ok=True)
    zkey = f"packets/{pid}.zip"
    ckey = f"{packet_prefix(pid)}clip.mp4"
    mkey = f"{packet_prefix(pid)}meta.json"
    if store.exists(zkey):
        zpath = d / "packet.zip"
        store.download_file(zkey, zpath)
        return extract_packet(zpath, d)
    if store.exists(ckey):
        clip = d / "clip.mp4"
        store.download_file(ckey, clip)
        if store.exists(mkey):
            store.download_file(mkey, d / "meta.json")
        else:
            idx = store.packet_index(pid) or {}
            (d / "meta.json").write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        return _load_meta(d, pid), clip
    raise FileNotFoundError(f"{pid} 既没有 zip 也没有 clip.mp4")


def backfill_one(pid: str, work: str, dry: bool) -> Dict[str, Any]:
    t0 = time.time()
    store = _store()
    api = ServerAPI()
    d = Path(work) / pid
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    try:
        meta, clip = _prepare_source(store, pid, d)
        if not getattr(meta, "decim", None):
            meta.decim = choose_decim(float(getattr(meta, "fps", 0) or 0))
        meta.clip_fps = float(getattr(meta, "clip_fps", 0) or CLIP_FPS)
        derive_from_clip(clip, d, fps=meta.clip_fps)
        artifacts: Dict[str, Any] = {}
        for rel in ARTIFACTS:
            p = d / rel
            if p.exists():
                artifacts[rel] = {"bytes": p.stat().st_size}
        if "dog.mp4" not in artifacts or "clip.mp4" not in artifacts:
            raise RuntimeError(f"产物不齐：{sorted(artifacts)}")
        meta.artifacts = artifacts
        (d / "meta.json").write_text(meta.to_json(), encoding="utf-8")
        prefix = packet_prefix(pid)
        if not dry:
            store.upload_file(f"{prefix}meta.json", d / "meta.json")
            for rel in artifacts:
                store.upload_file(f"{prefix}{rel}", d / rel)
            store.put_json(f"packets/{pid}.meta.json", json.loads(meta.to_json()))
            payload = {
                "packet_id": pid,
                "aoss_key": prefix,
                "meta": json.loads(meta.to_json()),
                "n_targets": meta.n_targets,
                "size_bytes": sum(int(v["bytes"]) for v in artifacts.values()),
            }
            r = api.register_packet(payload, thumb_dir=d / "thumb")
            if r is None:
                raise RuntimeError(f"register 失败：{api.last_error}")
        return {
            "pid": pid, "ok": True,
            "bytes": sum(int(v["bytes"]) for v in artifacts.values()),
            "sec": round(time.time() - t0, 1),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "pid": pid, "ok": False, "error": str(e),
            "sec": round(time.time() - t0, 1),
            "trace": traceback.format_exc()[-800:],
        }
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    p = argparse.ArgumentParser(description="按新编排重做已有包")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--packet", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--shard", default="0/1", help="i/n，按 packet_id 哈希分片")
    p.add_argument("--include-test", action="store_true")
    p.add_argument("--work", type=Path, default=Path("/mnt/afs/wangmin/data/_ai_label_work/backfill"))
    p.add_argument("--progress", type=Path, default=None)
    a = p.parse_args()
    shard_i, shard_n = (int(x) for x in a.shard.split("/", 1))
    store = _store()
    pids = [a.packet] if a.packet else _pids(store, a.include_test)
    pids = [pid for pid in pids if _in_shard(pid, shard_i, shard_n)]
    if a.limit:
        pids = pids[: a.limit]
    a.work.mkdir(parents=True, exist_ok=True)
    prog = a.progress or (a.work / f"shard_{shard_i}_of_{shard_n}.jsonl")
    print(f"[plan] shard={a.shard} n={len(pids)} workers={a.workers} dry={a.dry_run}", flush=True)
    for pid in pids:
        print(f"  {pid}", flush=True)

    ok = fail = 0
    workers = max(1, int(a.workers))
    if workers > 1:
        import multiprocessing as mp
        try:
            mp.set_start_method("spawn")
        except RuntimeError:
            pass
    with open(prog, "a", encoding="utf-8") as pf:
        if workers == 1:
            futs = [backfill_one(pid, str(a.work), a.dry_run) for pid in pids]
        else:
            futs = []
            with ProcessPoolExecutor(max_workers=workers) as ex:
                pending = {ex.submit(backfill_one, pid, str(a.work), a.dry_run): pid for pid in pids}
                for fut in as_completed(pending):
                    futs.append(fut.result())
        for row in futs:
            pf.write(json.dumps(row, ensure_ascii=False) + "\n")
            pf.flush()
            if row.get("ok"):
                ok += 1
                print(f"[ok] {row['pid']}  {row.get('bytes' ,0)/1e6:.1f} MB  {row.get('sec')}s", flush=True)
            else:
                fail += 1
                print(f"[fail] {row['pid']}: {row.get('error')}", flush=True)
    print(f"[done] shard={a.shard} ok={ok} fail={fail} total={ok+fail}", flush=True)
    summary = {"shard": a.shard, "ok": ok, "fail": fail, "total": ok + fail}
    (a.work / f"shard_{shard_i}_of_{shard_n}.done.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
