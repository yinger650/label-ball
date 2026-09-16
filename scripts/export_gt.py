#!/usr/bin/env python3
"""从中心库的 `gt` 表导出训练可用的 GT，取代 `collect_results.py`。

`collect_results.py` 的数据源是 AOSS 上的 `results/` 对象，一份对象一个文件，
文件名带标注员（`<packet_id>__<who>.json`）——因为旧系统里「谁标的」就是主键的一部分，
同一个包被两个人标过就有两份互不相干的答案，下游得自己挑。

中心库把这件事收敛了：`gt` 按 `(packet_id, frame_idx)` 唯一，一个包只有**一份**权威
GT。代价是这份 GT 可能是拼出来的——AI 标了九成、人工修补了被打回的那几帧——所以逐帧
带上 `source` / `worker` / `interp` / `conf`，导出文档顶层再给 `sources` 帧数分布和
`annotators` 名单。训练侧要只用真人帧，靠 `--source human` 或 JSONL 里的 `source` 列筛。

**只读**。`field_u/v` 的补齐和 `qc()` 都直接复用 `collect_results.py` 的实现：换算口径
和质检阈值必须和旧导出完全一致，否则新旧两批 GT 训出来的模型没法比。

输出：
    <out-dir>/<packet_id>.json     单包 GT，schema 对齐 packet.py 的 build_result()
    <out-dir>/index.json           每包一行摘要
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "server"))

import db  # noqa: E402
from collect_results import OUT_DIR, load_field_fn, qc  # noqa: E402
from packet import PACKET_VERSION  # noqa: E402

JSONL_KEYS = ("packet_id", "frame_idx", "pano_x", "pano_y", "field_u", "field_v",
              "visible", "source", "interp", "conf")


def gt_rows(c, packet_id: str, source: str) -> List[dict]:
    """一个包的全部 GT 帧，按帧号排序。

    排序交给 SQLite 而不是 Python：`qc()` 靠相邻两行算帧间位移，顺序错了速度统计全废。
    """
    sql = "SELECT * FROM gt WHERE packet_id=?"
    args: List[Any] = [packet_id]
    if source != "all":
        sql += " AND source=?"
        args.append(source)
    return db.rows(c, sql + " ORDER BY frame_idx", args)


def to_label(r: dict) -> dict:
    """gt 行 → 结果文档里的一行 label。

    SQLite 没有布尔类型，取出来是 0/1；下游 `eval_vs_gt.py` 之类的脚本读的是旧导出，
    那里是真正的 true/false，所以在这里转回去，省得下游踩 `"visible": 0` 的坑。
    """
    return {
        "frame_idx": int(r["frame_idx"]),
        "pano_x": r["pano_x"],
        "pano_y": r["pano_y"],
        "visible": bool(r["visible"]),
        "occluded": bool(r["occluded"]),
        "blurred": bool(r["blurred"]),
        "interp": bool(r["interp"]),
        "conf": r["conf"],
        "source": r["source"],
        "worker": r["worker"],
    }


def fill_field_uv(labels: List[dict], tf: Optional[dict], field_fn) -> int:
    """补 `field_u/v`。只补可见帧——不可见帧根本没有像素坐标可换算。"""
    if field_fn is None or not tf:
        return 0
    n = 0
    for r in labels:
        if not r.get("visible") or r.get("pano_x") is None:
            continue
        try:
            uv = field_fn(float(r["pano_x"]), float(r["pano_y"]), tf)
        except Exception:  # noqa: BLE001
            uv = None
        if uv is not None:
            r["field_u"], r["field_v"] = float(uv[0]), float(uv[1])
            n += 1
    return n


def build_doc(p: dict, meta: dict, labels: List[dict], report: dict) -> dict:
    """拼导出文档。字段顺序和 `packet.py: build_result()` 对齐，末尾接中心库特有的几项。"""
    src: Counter = Counter(r["source"] for r in labels)
    workers = sorted({r["worker"] for r in labels if r.get("worker")})
    confs = [float(r["conf"]) for r in labels if r.get("conf") is not None]
    # 顶层 annotator 是给旧下游看的单值字段：混合来源没法用一个人代表，明说 mixed
    # 比随便挑一个更诚实
    main = workers[0] if len(workers) == 1 else "mixed"
    return {
        "version": PACKET_VERSION,
        "kind": "ball_gt",
        "packet_id": p["packet_id"],
        "group": meta.get("group") or p["group_name"],
        "stem": meta.get("stem") or p["stem"],
        "tag": meta.get("tag") or p["tag"],
        "annotator": main,
        "fps": meta.get("fps"),
        "pano_size": meta.get("pano_size"),
        "roi": meta.get("roi"),
        "frame_start": meta.get("frame_start"),
        "frame_end": meta.get("frame_end"),
        "step": meta.get("step"),
        "sampling": meta.get("sampling") or p["sampling"],
        "n_targets": report["n_targets"],
        "n_labeled": report["n_labeled"],
        "n_visible": report["n_visible"],
        "n_occluded": sum(1 for r in labels if r.get("occluded")),
        "n_blurred": sum(1 for r in labels if r.get("blurred")),
        "n_interp": sum(1 for r in labels if r.get("interp")),
        "mean_conf": round(sum(confs) / len(confs), 4) if confs else None,
        "sources": {"ai": src.get("ai", 0), "human": src.get("human", 0)},
        "annotators": workers,
        "labels": labels,
        "qc": report,
    }


def run(args) -> None:
    db_path = Path(args.db or db.default_db_path())
    c = db.connect(db_path)
    # 只读脚本不该建表：`connect()` 会把空文件创出来，指错路径时不拦一下就是一串
    # "no such table" 的 traceback，而正确的提示是「库不在这儿」
    if not db.one(c, "SELECT name FROM sqlite_master WHERE type='table' AND name='gt'"):
        raise SystemExit(f"[err] {db_path} 不是中心库（没有 gt 表）。先跑 server/db.py 建库")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    field_fn = None if args.no_field_uv else load_field_fn()

    want = set(args.packet_id or [])
    # 从 packets 出发而不是从 gt 出发：没有 meta 就补不了 field_u/v、也填不了 roi/fps，
    # 这种包导出来下游也用不了
    packets = db.rows(
        c,
        "SELECT * FROM packets WHERE packet_id IN (SELECT DISTINCT packet_id FROM gt) "
        "ORDER BY packet_id",
    )
    if want:
        packets = [p for p in packets if p["packet_id"] in want]
    if not packets:
        print("[done] gt 表里还没有任何已入库的包", flush=True)
        c.close()
        return
    print(f"[scan] 本次候选 {len(packets)} 个包，来源筛选 = {args.source}", flush=True)

    index: List[dict] = []
    flat: List[dict] = []
    n_pts = n_vis = n_interp = 0
    src_tally: Counter = Counter()
    worker_tally: Counter = Counter()
    skipped: List[str] = []

    for p in packets:
        pid = p["packet_id"]
        meta = db.jload(p["meta_json"], {}) or {}
        labels = [to_label(r) for r in gt_rows(c, pid, args.source)]
        if not labels:
            continue

        n_uv = fill_field_uv(labels, meta.get("sphere_transform"), field_fn)
        n_targets = int(p["n_targets"] or len(meta.get("targets") or []) or len(labels))
        report = qc({"labels": labels, "n_targets": n_targets},
                    float(meta.get("fps") or 30.0))

        if report["coverage"] < args.min_coverage:
            skipped.append(f"{pid}（覆盖率 {report['coverage']}）")
            continue
        if args.exclude_flagged and report["flags"]:
            skipped.append(f"{pid}（{'；'.join(report['flags'])}）")
            continue

        doc = build_doc(p, meta, labels, report)
        path = out_dir / f"{pid}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False))

        n_pts += report["n_labeled"]
        n_vis += report["n_visible"]
        n_interp += doc["n_interp"]
        src_tally.update({k: v for k, v in doc["sources"].items() if v})
        for r in labels:
            worker_tally[r.get("worker") or "?"] += 1
        index.append({
            "packet_id": pid, "annotator": doc["annotator"], "file": path.name,
            "group": doc["group"], "stem": doc["stem"], "tag": doc["tag"],
            "sampling": doc["sampling"], "n_field_uv": n_uv,
            **report,
            "sources": doc["sources"], "annotators": doc["annotators"],
            "n_interp": doc["n_interp"], "mean_conf": doc["mean_conf"],
        })
        if args.jsonl:
            flat += [{**{k: r.get(k) for k in JSONL_KEYS}, "packet_id": pid} for r in labels]

        mark = "⚠ " + "；".join(report["flags"]) if report["flags"] else "ok"
        print(
            f"  {pid}  [{doc['annotator']}]  {report['n_labeled']}/{report['n_targets']} 帧  "
            f"可见 {report['n_visible']}  ai/human {doc['sources']['ai']}/{doc['sources']['human']}  "
            f"运动占比 {report['moving_ratio']}  {mark}",
            flush=True,
        )

    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2))
    if args.jsonl:
        jp = Path(args.jsonl)
        jp.parent.mkdir(parents=True, exist_ok=True)
        with jp.open("w", encoding="utf-8") as f:
            for r in flat:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    bad = [r for r in index if r["flags"]]
    print(
        f"\n[done] {len(index)} 个包，共 {n_pts} 帧（可见 {n_vis}，插值 {n_interp}）\n"
        f"  按来源：{dict(src_tally)}\n"
        f"  按 worker：{dict(worker_tally)}\n"
        f"  有 QC 标记：{len(bad)} 个包\n"
        f"  跳过未导出：{len(skipped)} 个包" + (f" {skipped[:5]}" if skipped else "") + "\n"
        f"  输出：{out_dir}" + (f"\n  JSONL：{args.jsonl}（{len(flat)} 行）" if args.jsonl else ""),
        flush=True,
    )
    c.close()


def parse_args():
    p = argparse.ArgumentParser(description="从中心库 gt 表导出训练可用的足球 GT")
    p.add_argument("--db", default=None, help="SQLite 路径，默认 db.default_db_path()")
    p.add_argument("--out-dir", default=str(OUT_DIR))
    p.add_argument("--packet-id", action="append", default=None, help="只导某些包，可重复")
    p.add_argument("--source", choices=("ai", "human", "all"), default="all", help="只导某个来源的帧")
    p.add_argument("--min-coverage", type=float, default=0.0, help="覆盖率低于此值的包不导出")
    p.add_argument("--exclude-flagged", action="store_true", help="有 QC 标记的包不导出")
    p.add_argument("--no-field-uv", action="store_true", help="跳过 field_u/v 补齐")
    p.add_argument("--jsonl", default=None, help="额外摊平成一行一帧的 JSONL，训练侧直接读")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
