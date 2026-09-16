#!/usr/bin/env python3
"""把 AOSS `results/` 里的旧人工标注一次性迁进中心库。

旧系统没有中心库，一份人工标注就是 `results/<packet_id>__<who>.json` 这么一个对象
（见 `aoss_store.py` 顶部的布局说明）。改成 SQLite 权威之后，「入库」的唯一定义变成
`gt` 表里有行——这批真人标注不搬进来，在新系统眼里就等于不存在：导出训练数据看不到
它们，`scripts/calibrate.py` 也没有验证集可用来量 AI 的准确率。

真人标注免审，所以这里直接写 `gt`（`source='human'`）并把 submissions 行建成
`accepted`，和服务端 `/api/v2/submit` 对 `source=human` 的处理保持一致。同时补一条
`workflow='legacy_manual'`：日后统计 AI 通过率时，得能把这批「不是本系统产生的」提交
从分母里摘出去。

**幂等是硬要求**。AOSS 上的 `results/` 只增不减，这个脚本大概率会被跑很多次（补新
提交的、换库重导、迁移中途失败重来），每跑一次都插一行 submissions 的话，通过率统计
和审核队列会被复制品淹没。判重键取 `(packet_id, worker, source='human',
workflow='legacy_manual')`——同一个人对同一个包在旧系统里只可能有一份结果对象。

这是**开发机**上的一次性脚本，可以持有 AOSS 凭证；服务端进程不行（见 `server/app.py`
的模块 docstring）。库在公网机器上、开发机只连得到 AOSS 时，走 `--server` 模式，把
读 AOSS 和写库拆到两台机器上。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "server"))

import db  # noqa: E402
from aoss_store import Store, load_credentials  # noqa: E402
from collect_results import qc  # noqa: E402  质检口径只有一份，别重写

WORKFLOW = "legacy_manual"
LABEL_KEYS = ("frame_idx", "visible", "occluded", "blurred", "pano_x", "pano_y", "ts")


# ---------------------------------------------------------------- 结果文档整理


def _as_epoch(v: Any) -> Optional[float]:
    """把各种形态的时间戳拍成 epoch 秒。

    旧结果里的时间字段是不同版本的标注站写的：有 epoch 秒、有毫秒、也有 ISO 串。
    这里只是给 submissions 排个序，认不出来就返回 None 让调用方降级。
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        if f > 1e12:  # 毫秒
            f /= 1000.0
        return f if f > 1e8 else None
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            return None
    return None


def result_time(res: dict, labels: List[dict]) -> float:
    """这份结果是什么时候完成的。

    宁可用 labels 里最大的 `ts` 也不要直接用当前时间：submissions 按 created_at 排序
    展示，全填成导入时刻的话，这批历史数据会在时间线上挤成一坨。
    """
    for k in ("finished_at", "submitted_at", "saved_at", "ended_at", "created_at"):
        t = _as_epoch(res.get(k))
        if t:
            return t
    ts = [t for t in (_as_epoch(r.get("ts")) for r in labels) if t]
    return max(ts) if ts else time.time()


def normalize_labels(res: dict) -> Tuple[List[dict], int]:
    """整理 labels：按帧号排序、去重、剔掉标了 visible 却没坐标的行。

    返回 (labels, n_bad)。`qc()` 会对可见帧两两算帧间位移，坐标是 None 就直接抛
    TypeError；`gt` 表里一行「可见但没有坐标」对训练侧也是纯噪声。所以在入口处就把
    这种行降级成不可见，并把条数报出来，而不是让它一路漏到导出。
    """
    by_frame: Dict[int, dict] = {}
    n_bad = 0
    for r in res.get("labels") or []:
        fi = r.get("frame_idx")
        if fi is None:
            continue
        row = {k: r[k] for k in LABEL_KEYS if k in r}
        row["frame_idx"] = int(fi)
        vis = bool(r.get("visible"))
        if vis and (r.get("pano_x") is None or r.get("pano_y") is None):
            vis, n_bad = False, n_bad + 1
        row["visible"] = vis
        row["occluded"] = bool(r.get("occluded"))
        row["blurred"] = bool(r.get("blurred"))
        by_frame[row["frame_idx"]] = row
    return [by_frame[k] for k in sorted(by_frame)], n_bad


# ---------------------------------------------------------------- --server 模式


def api_post(base: str, path: str, payload: dict, token: Optional[str]) -> dict:
    """极简 JSON POST。用标准库是为了让这个脚本在任何一台开发机上都能直接跑
    （`requirements.txt` 里没有 requests，不值得为一次性迁移加依赖）。"""
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    if token:
        req.add_header("X-Api-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (e.read() or b"").decode("utf-8", "replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {e.code} {path}：{detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"连不上 {base}：{e.reason}") from e


def api_imported_set(base: str, token: Optional[str]) -> set:
    """问服务端「哪些 (包, 标注员) 已经导过了」。

    `--server` 模式下判重不能靠本地 SQLite（这台机器上根本没有库），所以开工前一次性
    把已有的 legacy 提交拉下来。一次性拉全量而不是逐份问：旧结果是几百份的量级，一份
    一个往返太慢，而这批数据在迁移期间不会有别人往里写。
    """
    q = urllib.parse.urlencode({"state": "all", "source": "human",
                                "workflow": WORKFLOW, "limit": 5000})
    req = urllib.request.Request(base.rstrip("/") + "/api/v2/submissions?" + q, method="GET")
    if token:
        req.add_header("X-Api-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:  # noqa: BLE001
        # 拉不到就当成「一个都没导过」：重复导入把统计弄脏，总比迁移直接卡死好，
        # 但要明说，让人知道这一趟的幂等失效了
        print(f"[warn] 拉取已导入清单失败（{e}），本次无法判重，可能产生重复提交", flush=True)
        return set()
    return {(s["packet_id"], s["worker"]) for s in (d.get("submissions") or [])}


# ---------------------------------------------------------------- 写库


def already_imported(c, packet_id: str, worker: str) -> Optional[int]:
    r = db.one(
        c,
        "SELECT submission_id FROM submissions WHERE packet_id=? AND worker=? "
        "AND source='human' AND workflow=? ORDER BY submission_id DESC LIMIT 1",
        (packet_id, worker, WORKFLOW),
    )
    return int(r["submission_id"]) if r else None


def insert_submission(c, packet_id: str, worker: str, labels: List[dict], report: dict,
                      n_targets: int, created_at: float) -> int:
    c.execute(
        "INSERT INTO submissions(packet_id, lease_id, worker, source, workflow, model_used, "
        "labels_json, n_targets, n_labeled, n_visible, n_interp, mean_conf, qc_json, state, "
        "n_accepted, n_rejected, created_at, reviewed_at) "
        "VALUES(?,NULL,?,'human',?,NULL,?,?,?,?,0,NULL,?,'accepted',?,0,?,?)",
        (
            packet_id, worker, WORKFLOW, db.jdump(labels),
            n_targets, report["n_labeled"], report["n_visible"], db.jdump(report),
            report["n_labeled"], created_at, created_at,
        ),
    )
    c.commit()
    return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])


def fmt_line(tag: str, report: dict, n_targets: int, notes: List[str], extra: str = "") -> str:
    """逐份的一行进度，格式对齐 `collect_results.run()`，两边输出能直接对照着看。"""
    marks = list(report["flags"]) + notes
    mark = "⚠ " + "；".join(marks) if marks else "ok"
    return (f"  {tag}  {report['n_labeled']}/{n_targets} 帧  "
            f"可见 {report['n_visible']}  {mark}{extra}")


def mark_accepted(c, packet_id: str) -> Optional[str]:
    """把包置成 accepted。返回一句警告文本，没警告就是 None。

    `leased` 不动：迁移是在线上系统跑着的时候做的，抢掉别人手里的租约会让那边提交时
    找不到包的状态基线（`leases.prev_state` 已经记成 leased 了），过期回收还会把包拨回
    一个错误的状态。GT 已经写进去了，包状态晚一步由那条租约收尾即可。
    """
    r = db.one(c, "SELECT state FROM packets WHERE packet_id=?", (packet_id,))
    if r and r["state"] == "leased":
        return "包正被租用中，保留 leased 状态"
    c.execute(
        "UPDATE packets SET state='accepted', need_frames_json=NULL, updated_at=? "
        "WHERE packet_id=? AND state<>'leased'",
        (db.now(), packet_id),
    )
    c.commit()
    return None


# ---------------------------------------------------------------- 主流程


def run(args) -> None:
    done_remote: set = set()
    if args.server:
        c = None
        token = os.environ.get("BALL_GT_API_TOKEN") or None
        print(f"[mode] 经服务端 {args.server} 写库{'（带 token）' if token else ''}", flush=True)
        if not args.force:
            done_remote = api_imported_set(args.server, token)
            print(f"[skip] 服务端已有 {len(done_remote)} 份 legacy 提交", flush=True)
    else:
        token = None
        path = Path(args.db or db.default_db_path())
        c = db.connect(path)
        db.migrate(c)  # 库不存在就建出来，省得先手跑一遍 server/db.py
        print(f"[mode] 直连 SQLite {path}", flush=True)

    store = Store(load_credentials(args.credentials))
    results = store.list_results()
    if not results:
        print("[done] 对象存储上还没有任何标注结果", flush=True)
        return

    want_pids = set(args.packet_id or [])
    pairs = [
        (pid, who)
        for pid in sorted(results)
        for who in sorted(results[pid])
        if (not want_pids or pid in want_pids) and (not args.annotator or who == args.annotator)
    ]
    print(
        f"[scan] 对象存储上 {sum(len(v) for v in results.values())} 份结果 / {len(results)} 个包，"
        f"本次候选 {len(pairs)} 份",
        flush=True,
    )

    n_ok = n_frames = 0
    tally: Counter = Counter()
    skipped: List[Tuple[str, str]] = []
    # meta 只在同一次运行里缓存：一个包可能有多份结果，packet_index 每次都是一趟网络往返
    meta_cache: Dict[str, Optional[dict]] = {}

    for pid, who in pairs:
        if args.limit and n_ok >= args.limit:
            break
        tag = f"{pid}  [{who}]"

        res = store.get_json(f"results/{pid}__{who}.json")
        if not res:
            skipped.append((tag, "结果对象读取失败"))
            print(f"  {tag}  跳过：结果对象读取失败", flush=True)
            continue

        if pid not in meta_cache:
            meta_cache[pid] = store.packet_index(pid)
        meta = meta_cache[pid]
        if not meta or not meta.get("targets"):
            # 没有 meta 就没有 sphere_transform / targets，export_gt 补不了 field_u/v、
            # 也不知道分母是多少。硬导进去只会得到一份算不出覆盖率的脏数据
            skipped.append((tag, "packets/<pid>.meta.json 缺失或没有 targets"))
            print(f"  {tag}  跳过：拿不到包 meta（packets/{pid}.meta.json）", flush=True)
            continue

        labels, n_bad = normalize_labels(res)
        if not labels:
            skipped.append((tag, "结果里没有任何 labels"))
            print(f"  {tag}  跳过：结果里没有任何 labels", flush=True)
            continue
        n_targets = len(meta.get("targets") or []) or int(res.get("n_targets") or len(labels))
        report = qc({**res, "labels": labels, "n_targets": n_targets},
                    float(res.get("fps") or meta.get("fps") or 30.0))
        created = result_time(res, labels)

        notes: List[str] = []
        if n_bad:
            notes.append(f"{n_bad} 帧可见但无坐标，已降级为不可见")

        old_sid = already_imported(c, pid, who) if c is not None else None
        if old_sid and not args.force:
            skipped.append((tag, f"已导入过（submission {old_sid}）"))
            print(f"  {tag}  跳过：已导入过（submission {old_sid}，要重导加 --force）", flush=True)
            continue
        if (pid, who) in done_remote:
            skipped.append((tag, "已导入过（服务端已有）"))
            print(f"  {tag}  跳过：服务端已有这份 legacy 提交（要重导加 --force）", flush=True)
            continue

        if args.dry_run:
            print(fmt_line(tag, report, n_targets, notes, "   [dry-run]"), flush=True)
            n_ok += 1
            n_frames += report["n_labeled"]
            tally[who] += report["n_labeled"]
            continue

        try:
            if c is None:
                api_post(args.server, "/api/v2/register", {"packets": [
                    {"packet_id": pid, "aoss_key": f"packets/{pid}.zip", "meta": meta}
                ]}, token)
                api_post(args.server, "/api/v2/submit", {
                    "packet_id": pid, "source": "human", "worker": who,
                    "workflow": WORKFLOW,
                    "result": {**res, "labels": labels, "n_targets": n_targets},
                }, token)
            else:
                db.register_packet(c, pid, f"packets/{pid}.zip", meta, n_targets=n_targets)
                if old_sid:
                    # 直接删掉旧的那条 legacy 行：gt 靠 upsert 自然被新 submission 覆盖，
                    # 留着旧行只会让「这个包被提交了几次」的统计虚高
                    c.execute("DELETE FROM submissions WHERE submission_id=?", (old_sid,))
                sid = insert_submission(c, pid, who, labels, report, n_targets, created)
                db.write_gt(c, pid, labels, source="human", worker=who, submission_id=sid)
                warn = mark_accepted(c, pid)
                if warn:
                    notes.append(warn)
        except Exception as e:  # noqa: BLE001
            if c is not None:
                c.rollback()
            skipped.append((tag, f"导入失败：{e}"))
            print(f"  {tag}  失败：{e}", flush=True)
            continue

        n_ok += 1
        n_frames += report["n_labeled"]
        tally[who] += report["n_labeled"]
        print(fmt_line(tag, report, n_targets, notes, "   [重导]" if old_sid else ""), flush=True)

    reasons: Counter = Counter(r.split("（")[0] for _, r in skipped)
    print(
        f"\n[done] 导入 {n_ok} 份结果，共 {n_frames} 帧" + ("（dry-run，未写库）" if args.dry_run else "") + "\n"
        f"  按标注员：{dict(tally)}\n"
        f"  跳过：{len(skipped)} 份 {dict(reasons) if reasons else ''}",
        flush=True,
    )
    if c is not None:
        print(f"  gt 表现有 {db.scalar(c, 'SELECT COUNT(*) FROM gt')} 帧 / "
              f"{db.scalar(c, 'SELECT COUNT(DISTINCT packet_id) FROM gt')} 个包", flush=True)
        c.close()


def parse_args():
    p = argparse.ArgumentParser(description="把 AOSS results/ 里的旧人工标注导入中心库")
    p.add_argument("--db", default=None, help="SQLite 路径，默认 db.default_db_path()")
    p.add_argument("--server", default=None,
                   help="不直连库，改为 POST 到该服务端（开发机连得到 AOSS、库在公网时用）")
    p.add_argument("--credentials", default=None, help="AOSS 凭证路径")
    p.add_argument("--packet-id", action="append", default=None, help="只导某个包，可重复")
    p.add_argument("--annotator", default=None, help="只导某个标注员的结果")
    p.add_argument("--limit", type=int, default=0, help="最多导 N 份（调试用）")
    p.add_argument("--dry-run", action="store_true", help="只打印会做什么，不写库")
    p.add_argument("--force", action="store_true", help="重新导入已导入过的")
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
