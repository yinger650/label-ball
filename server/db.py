#!/usr/bin/env python3
"""标注系统的中心库（SQLite）。

旧系统没有中心库：任务状态由 AOSS 上 `packets/` / `claims/` / `results/` 对象的存在
与否表达（见 `aoss_store.py` 的 `pool_status`）。这套办法在「几百个包、个位数标注员」
的规模下能跑，但 AI 客户端加进来之后就不够了——审核、逐帧打回、按模型统计通过率都
需要能查询的状态，而对象存在性表达不出这些。

所以权威搬到这里。**这个模块不碰对象存储**：`packets.aoss_key` 只是一个字符串，
下载由能 GetObject 的客户端自己做。服务端进程始终不持有 AOSS / ACP 凭证。

WAL 是必须的：审核页在读、客户端在提交、产包端每几分钟一次心跳，默认的
rollback journal 下写会阻塞读，审核页会卡住。WAL 让读写互不阻塞。

「入库」的定义只有一处：`gt` 表里有行。人工 / agent 提交直接写进去，AI 提交必须先过审核。
已入库的提交仍可再审；打回的帧从 `gt` 删掉，包转 `need_human`。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

SCHEMA_VERSION = 3

# 租约时长。AI 客户端跑一个包约几分钟到十几分钟，30 分钟够宽松，靠心跳续期；
# 人工沿用旧系统的 CLAIM_TTL_SEC（36 小时），因为标注员会跨天做完一个包。
LEASE_TTL_AI = 30 * 60
LEASE_TTL_HUMAN = 36 * 3600
LEASE_TTL_REPAIR = 12 * 3600
# 同一 worker 刚因 no_seed / 处理失败释放的包，冷却期内不再发给他。
# 否则客户端会立刻领回同一个最老的 available 包，空转打爆租约表。
FAIL_COOLDOWN_SEC = 15 * 60

PACKET_STATES = ("available", "leased", "submitted", "accepted", "need_human", "archived")
LEASE_KINDS = ("ai", "human", "repair")
SUBMISSION_STATES = ("pending", "accepted", "rejected", "partial")

_LOCAL = threading.local()


def default_db_path() -> Path:
    return Path(os.environ.get("BALL_GT_DB") or (Path.home() / "ball_gt_work" / "ball_gt.sqlite3"))


# ---------------------------------------------------------------- 连接


def connect(path: Optional[Path | str] = None) -> sqlite3.Connection:
    """新建一条连接并打好 pragma。

    `check_same_thread=False` + 每线程一条连接（见 `conn()`）：Flask 是多线程的，
    共享一条连接会在并发提交时报 "recursive use of cursors"。
    """
    p = Path(path or default_db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=30.0, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    # busy_timeout 而不是靠应用层重试：WAL 下唯一会撞的是「两个写者」，
    # 等一会儿就好，没必要把 OperationalError 抛到 API 层
    c.execute("PRAGMA busy_timeout=30000")
    return c


def conn(path: Optional[Path | str] = None) -> sqlite3.Connection:
    """取当前线程的连接（首次调用时建库）。"""
    p = str(Path(path or default_db_path()))
    cache: Dict[str, sqlite3.Connection] = getattr(_LOCAL, "conns", None) or {}
    if p not in cache:
        c = connect(p)
        migrate(c)
        cache[p] = c
        _LOCAL.conns = cache
    return cache[p]


def close_all() -> None:
    for c in (getattr(_LOCAL, "conns", None) or {}).values():
        c.close()
    _LOCAL.conns = {}


# ---------------------------------------------------------------- schema

DDL_V1 = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- 待标注包。meta_json 直接是 packet.py 的 PacketMeta，客户端拿到就能干活，
-- 服务端不需要理解里面的 ROI / 标定字段。
CREATE TABLE IF NOT EXISTS packets (
  packet_id        TEXT PRIMARY KEY,
  aoss_key         TEXT NOT NULL,
  meta_json        TEXT NOT NULL,
  n_targets        INTEGER NOT NULL DEFAULT 0,
  state            TEXT NOT NULL DEFAULT 'available',
  priority         INTEGER NOT NULL DEFAULT 0,
  -- 打回后待人工修补的帧号（JSON 数组）。repair 租约只发这些帧
  need_frames_json TEXT,
  producer_job_id  TEXT,
  producer_node    TEXT,
  size_bytes       INTEGER,
  group_name       TEXT,
  stem             TEXT,
  tag              TEXT,
  sampling         TEXT,
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packets_state ON packets(state, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_packets_created ON packets(created_at);
CREATE INDEX IF NOT EXISTS idx_packets_producer ON packets(producer_job_id);

CREATE TABLE IF NOT EXISTS leases (
  lease_id        TEXT PRIMARY KEY,
  packet_id       TEXT NOT NULL REFERENCES packets(packet_id) ON DELETE CASCADE,
  worker          TEXT NOT NULL,
  kind            TEXT NOT NULL,
  prev_state      TEXT,
  stage           TEXT,
  created_at      REAL NOT NULL,
  expires_at      REAL NOT NULL,
  last_heartbeat  REAL,
  released_at     REAL,
  released_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_leases_open ON leases(released_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_leases_packet ON leases(packet_id, created_at);
CREATE INDEX IF NOT EXISTS idx_leases_worker ON leases(worker, packet_id);

-- 一次提交。人工提交建的行直接是 accepted（免审）；AI 提交是 pending，等审核。
CREATE TABLE IF NOT EXISTS submissions (
  submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
  packet_id     TEXT NOT NULL REFERENCES packets(packet_id) ON DELETE CASCADE,
  lease_id      TEXT,
  worker        TEXT NOT NULL,
  source        TEXT NOT NULL,          -- ai | human
  workflow      TEXT,
  model_used    TEXT,
  labels_json   TEXT NOT NULL,
  n_targets     INTEGER NOT NULL DEFAULT 0,
  n_labeled     INTEGER NOT NULL DEFAULT 0,
  n_visible     INTEGER NOT NULL DEFAULT 0,
  n_interp      INTEGER NOT NULL DEFAULT 0,
  mean_conf     REAL,
  qc_json       TEXT,
  bundle_path   TEXT,
  bundle_bytes  INTEGER,
  state         TEXT NOT NULL DEFAULT 'pending',
  n_accepted    INTEGER NOT NULL DEFAULT 0,
  n_rejected    INTEGER NOT NULL DEFAULT 0,
  elapsed_sec   REAL,
  n_vlm_calls   INTEGER,
  created_at    REAL NOT NULL,
  reviewed_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_sub_state ON submissions(state, created_at);
CREATE INDEX IF NOT EXISTS idx_sub_packet ON submissions(packet_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sub_model ON submissions(model_used, workflow);
CREATE INDEX IF NOT EXISTS idx_sub_created ON submissions(created_at);

CREATE TABLE IF NOT EXISTS reviews (
  review_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  submission_id  INTEGER NOT NULL REFERENCES submissions(submission_id) ON DELETE CASCADE,
  reviewer       TEXT NOT NULL,
  decision       TEXT NOT NULL,        -- accept | reject | partial
  per_frame_json TEXT,                 -- {"<frame_idx>": true/false}，缺省视为通过
  notes          TEXT,
  created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_sub ON reviews(submission_id, created_at);

-- 这张表就是「入库」。导出训练数据只看它。
CREATE TABLE IF NOT EXISTS gt (
  packet_id     TEXT NOT NULL,
  frame_idx     INTEGER NOT NULL,
  pano_x        REAL,
  pano_y        REAL,
  visible       INTEGER NOT NULL DEFAULT 0,
  occluded      INTEGER NOT NULL DEFAULT 0,
  blurred       INTEGER NOT NULL DEFAULT 0,
  interp        INTEGER NOT NULL DEFAULT 0,
  conf          REAL,
  source        TEXT NOT NULL,          -- ai | human
  worker        TEXT,
  submission_id INTEGER,
  updated_at    REAL NOT NULL,
  PRIMARY KEY (packet_id, frame_idx)
);
CREATE INDEX IF NOT EXISTS idx_gt_source ON gt(source);
CREATE INDEX IF NOT EXISTS idx_gt_updated ON gt(updated_at);

-- ACP 上的常驻产包作业（pt-*）。两条通道都写这张表：作业内心跳写 loop_iter /
-- stage / n_produced，开发机上的 collector_agent 写 acp_state / node / log_path。
CREATE TABLE IF NOT EXISTS producers (
  job_id         TEXT PRIMARY KEY,
  display_name   TEXT,
  pool           TEXT,
  resource       TEXT,
  node           TEXT,
  run_stamp      TEXT,
  acp_state      TEXT,
  health         TEXT,
  started_at     REAL,
  last_heartbeat REAL,
  last_acp_poll  REAL,
  loop_iter      INTEGER,
  stage          TEXT,
  n_produced     INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT,
  log_path       TEXT,
  poll_sec       INTEGER,
  extra_json     TEXT,
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_producers_hb ON producers(last_heartbeat);

CREATE TABLE IF NOT EXISTS producer_events (
  event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id     TEXT NOT NULL,
  ts         REAL NOT NULL,
  kind       TEXT NOT NULL,   -- start | progress | produced | error | acp_state | exit
  msg        TEXT,
  extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_pevents_job ON producer_events(job_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pevents_ts ON producer_events(ts DESC);
"""


def _ensure_column(c: sqlite3.Connection, table: str, name: str, decl: str) -> None:
    cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    if name not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def migrate(c: sqlite3.Connection) -> int:
    """建表 / 升级。返回迁移后的版本号。

    每一版都写成独立的一段并且幂等，这样线上库直接跑一遍就升级完，不需要导出重建。
    """
    c.executescript(DDL_V1)
    cur = c.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    have = int(cur["value"]) if cur else 0
    if have < 1:
        c.execute(
            "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("1",),
        )
        have = 1
    if have < 2:
        # 人工预标的种子：不建 submission、不写 gt，包仍 available。AI 领包时优先选这些。
        _ensure_column(c, "packets", "seed_json", "TEXT")
        _ensure_column(c, "packets", "seed_worker", "TEXT")
        _ensure_column(c, "packets", "seed_at", "REAL")
        c.execute(
            "INSERT INTO schema_meta(key, value) VALUES('version', '2') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        )
        have = 2
    if have < 3:
        _ensure_column(c, "packets", "produced_at", "REAL")
        _ensure_column(c, "packets", "labeled_at", "REAL")
        _ensure_column(c, "packets", "reviewed_at", "REAL")
        _ensure_column(c, "packets", "decim", "INTEGER")
        _ensure_column(c, "packets", "clip_fps", "REAL")
        _ensure_column(c, "packets", "clip_frames", "INTEGER")
        _ensure_column(c, "packets", "thumb_ready", "INTEGER NOT NULL DEFAULT 0")
        c.execute(
            "INSERT INTO schema_meta(key, value) VALUES('version', '3') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        )
        have = 3
    c.commit()
    return have


# ---------------------------------------------------------------- 小工具


def now() -> float:
    return time.time()


def new_lease_id() -> str:
    return uuid.uuid4().hex


def jdump(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False)


def jload(s: Optional[str], default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return default


def rows(c: sqlite3.Connection, sql: str, args: Sequence = ()) -> List[Dict[str, Any]]:
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(c: sqlite3.Connection, sql: str, args: Sequence = ()) -> Optional[Dict[str, Any]]:
    r = c.execute(sql, args).fetchone()
    return dict(r) if r else None


def scalar(c: sqlite3.Connection, sql: str, args: Sequence = (), default: Any = 0) -> Any:
    r = c.execute(sql, args).fetchone()
    if not r or r[0] is None:
        return default
    return r[0]


def lease_ttl(kind: str) -> int:
    return {"ai": LEASE_TTL_AI, "human": LEASE_TTL_HUMAN, "repair": LEASE_TTL_REPAIR}.get(kind, LEASE_TTL_AI)


# ---------------------------------------------------------------- 包


def parse_produced_at(meta: dict) -> Optional[float]:
    s = meta.get("produced_at") or ""
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None


def register_packet(
    c: sqlite3.Connection,
    packet_id: str,
    aoss_key: str,
    meta: dict,
    *,
    n_targets: Optional[int] = None,
    size_bytes: Optional[int] = None,
    producer_job_id: Optional[str] = None,
    producer_node: Optional[str] = None,
    priority: int = 0,
    thumb_ready: Optional[int] = None,
) -> str:
    """幂等注册。返回 'registered' | 'updated'。

    重复注册只补元数据、**绝不把状态拨回 available**：产包端重启后会把整个
    `packets/` 重登一遍，若顺手重置状态，正在标的包会被再发一次。
    """
    t = now()
    n_t = int(n_targets if n_targets is not None else len(meta.get("targets") or []))
    produced = parse_produced_at(meta)
    decim = int(meta.get("decim") or 1)
    clip_fps = float(meta.get("clip_fps") or 30.0)
    clip_frames = int(meta.get("clip_frames") or 0) or None
    exist = one(c, "SELECT state FROM packets WHERE packet_id=?", (packet_id,))
    if exist:
        c.execute(
            "UPDATE packets SET aoss_key=?, meta_json=?, n_targets=?, "
            "size_bytes=COALESCE(?, size_bytes), "
            "producer_job_id=COALESCE(?, producer_job_id), "
            "producer_node=COALESCE(?, producer_node), "
            "priority=?, produced_at=COALESCE(?, produced_at), "
            "decim=?, clip_fps=?, clip_frames=COALESCE(?, clip_frames), "
            "thumb_ready=CASE WHEN ? IS NULL THEN thumb_ready ELSE ? END, "
            "updated_at=? WHERE packet_id=?",
            (aoss_key, jdump(meta), n_t, size_bytes, producer_job_id, producer_node,
             priority, produced, decim, clip_fps, clip_frames,
             thumb_ready, thumb_ready, t, packet_id),
        )
        c.commit()
        return "updated"
    c.execute(
        "INSERT INTO packets(packet_id, aoss_key, meta_json, n_targets, state, priority, "
        "producer_job_id, producer_node, size_bytes, group_name, stem, tag, sampling, "
        "created_at, updated_at, produced_at, decim, clip_fps, clip_frames, thumb_ready) "
        "VALUES(?,?,?,?,'available',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (packet_id, aoss_key, jdump(meta), n_t, priority, producer_job_id, producer_node,
         size_bytes, meta.get("group"), meta.get("stem"), meta.get("tag"),
         meta.get("sampling"), t, t, produced or t, decim, clip_fps, clip_frames,
         int(thumb_ready or 0)),
    )
    c.commit()
    return "registered"


def expire_leases(c: sqlite3.Connection) -> int:
    """把过期未心跳的租约收回，包放回原状态。

    没有后台线程：每次 lease / pool / stats 之前顺手扫一遍。租约表很小（一个包一行），
    全表扫的代价远低于维护一个定时线程。
    """
    t = now()
    stale = rows(
        c,
        "SELECT lease_id, packet_id, prev_state FROM leases "
        "WHERE released_at IS NULL AND expires_at < ?",
        (t,),
    )
    for r in stale:
        c.execute(
            "UPDATE leases SET released_at=?, released_reason='expired' WHERE lease_id=?",
            (t, r["lease_id"]),
        )
        # 只有还停在 leased 的包才回退：已经提交过的包状态更新，不能被过期租约拨回去
        c.execute(
            "UPDATE packets SET state=?, updated_at=? WHERE packet_id=? AND state='leased'",
            (r["prev_state"] or "available", t, r["packet_id"]),
        )
    if stale:
        c.commit()
    return len(stale)


def pick_packet(
    c: sqlite3.Connection,
    worker: str,
    kind: str,
    want_packet: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """挑一个包给这个 worker。

    发包策略（计划「发包策略」一节）：
      - AI 不领已有 pending AI 提交的包（免得两个客户端各算一遍同一个包）
      - need_human 只发给 human / repair
      - 同一个包不重复发给同一个 worker

    「不重复发给同一个 worker」按**有没有提交过**判，不按有没有领过判。领过就永久排除
    的话，一个 worker 每次 release（换包、客户端崩了重启）都会把那个包对自己拉黑；单
    客户端部署下，被 release 过一次的包就再没人捡，会一直烂在池子里。而 repair 连提交
    过也不排除：同一个人完全可能被二次打回、要再修一遍同几帧。
    """
    if kind == "repair":
        want_states = ("need_human",)
    elif kind == "human":
        # 人工优先修补打回帧——那是 AI 顶不住的一成，价值最高；没有再拿新包
        want_states = ("need_human", "available")
    else:
        want_states = ("available",)

    if want_packet:
        r = one(c, "SELECT * FROM packets WHERE packet_id=?", (want_packet,))
        if not r:
            return None
        if r["state"] not in want_states:
            return None
        return r

    # `?='ai'` 让这条子查询只在 AI 领包时生效：非 AI 时子查询恒空，NOT EXISTS 恒真
    no_pending_ai = (
        "  AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.packet_id=p.packet_id "
        "                  AND s.state='pending' AND s.source='ai' AND ?='ai') "
    )
    not_mine = (
        "" if kind == "repair" else
        "  AND NOT EXISTS (SELECT 1 FROM submissions m WHERE m.packet_id=p.packet_id "
        "                  AND m.worker=?) "
    )
    # 刚失败释放的包先让给别人 / 等冷却；repair 不受这条限制
    fail_cool = (
        "" if kind == "repair" else
        "  AND NOT EXISTS (SELECT 1 FROM leases f WHERE f.packet_id=p.packet_id "
        "                  AND f.worker=? AND f.released_at IS NOT NULL "
        "                  AND f.released_at > ? "
        "                  AND (f.released_reason='no_seed' "
        "                       OR f.released_reason LIKE 'error:%')) "
    )
    # AI 优先领人类预标过的包，好跳过冷启动
    order = (
        "ORDER BY (p.seed_json IS NOT NULL AND p.seed_json != '') DESC, "
        "p.priority DESC, p.created_at ASC LIMIT 1"
        if kind == "ai" else
        "ORDER BY p.priority DESC, p.created_at ASC LIMIT 1"
    )
    for st in want_states:
        args = [st, kind]
        if kind != "repair":
            args += [worker, worker, now() - FAIL_COOLDOWN_SEC]
        r = one(
            c,
            "SELECT * FROM packets p WHERE p.state=? " + no_pending_ai + not_mine
            + fail_cool + order,
            args,
        )
        if r:
            return r
    return None


def open_lease(c: sqlite3.Connection, packet: Dict[str, Any], worker: str, kind: str) -> Dict[str, Any]:
    t = now()
    ttl = lease_ttl(kind)
    lid = new_lease_id()
    c.execute(
        "INSERT INTO leases(lease_id, packet_id, worker, kind, prev_state, created_at, "
        "expires_at, last_heartbeat) VALUES(?,?,?,?,?,?,?,?)",
        (lid, packet["packet_id"], worker, kind, packet["state"], t, t + ttl, t),
    )
    c.execute("UPDATE packets SET state='leased', updated_at=? WHERE packet_id=?", (t, packet["packet_id"]))
    c.commit()
    return {"lease_id": lid, "expires_at": t + ttl, "ttl_sec": ttl}


def release_lease(c: sqlite3.Connection, lease_id: str, reason: str = "released") -> bool:
    t = now()
    r = one(c, "SELECT * FROM leases WHERE lease_id=?", (lease_id,))
    if not r or r["released_at"] is not None:
        return False
    c.execute("UPDATE leases SET released_at=?, released_reason=? WHERE lease_id=?", (t, reason, lease_id))
    c.execute(
        "UPDATE packets SET state=?, updated_at=? WHERE packet_id=? AND state='leased'",
        (r["prev_state"] or "available", t, r["packet_id"]),
    )
    c.commit()
    return True


def requeue_packets(
    c: sqlite3.Connection,
    packet_ids: Sequence[str],
    reason: str = "requeue_label",
) -> Dict[str, Any]:
    """待修复 → 待标注：`need_human` 改回 `available`，清空待修帧清单。

    已写入的 GT 不动（整包打回时本来就空）。leased / accepted 不动。
    """
    t = now()
    moved: List[str] = []
    skipped: List[Dict[str, str]] = []
    for pid in packet_ids:
        pid = str(pid or "").strip()
        if not pid:
            continue
        r = one(c, "SELECT packet_id, state FROM packets WHERE packet_id=?", (pid,))
        if not r:
            skipped.append({"packet_id": pid, "reason": "不存在"})
            continue
        if r["state"] != "need_human":
            skipped.append({"packet_id": pid, "reason": f"当前是 {r['state']}，只能打回待修复包"})
            continue
        c.execute(
            "UPDATE packets SET state='available', need_frames_json=NULL, updated_at=? "
            "WHERE packet_id=?",
            (t, pid),
        )
        moved.append(pid)
    if moved:
        c.commit()
    return {
        "moved": moved,
        "skipped": skipped,
        "reason": reason,
        "packet_state": "available",
    }


def archive_packets(
    c: sqlite3.Connection,
    packet_ids: Sequence[str],
    reason: str = "archived",
) -> Dict[str, Any]:
    """把包标成 archived，不再进入 lease 池。

    只动 `available` / `need_human`。已经 leased / submitted / accepted 的不动，
    免得把别人手里的活或待审卷宗藏起来。
    """
    t = now()
    archived: List[str] = []
    skipped: List[Dict[str, str]] = []
    for pid in packet_ids:
        pid = str(pid or "").strip()
        if not pid:
            continue
        r = one(c, "SELECT packet_id, state FROM packets WHERE packet_id=?", (pid,))
        if not r:
            skipped.append({"packet_id": pid, "reason": "不存在"})
            continue
        if r["state"] not in ("available", "need_human"):
            skipped.append({"packet_id": pid, "reason": f"当前是 {r['state']}，不能归档"})
            continue
        c.execute(
            "UPDATE packets SET state='archived', updated_at=? WHERE packet_id=?",
            (t, pid),
        )
        archived.append(pid)
    if archived:
        c.commit()
    return {"archived": archived, "skipped": skipped, "reason": reason}


def abandon_packet(
    c: sqlite3.Connection,
    packet_id: str,
    *,
    reason: str = "bad_quality",
    worker: str = "",
) -> Dict[str, Any]:
    """质量不行：归档包、清 GT、待审提交打成 rejected，从审核/修复队列消失。

    没有球不该走这里——人工把整段标不可见再提交即可。
    """
    pid = str(packet_id or "").strip()
    p = one(c, "SELECT * FROM packets WHERE packet_id=?", (pid,))
    if not p:
        return {"ok": False, "error": "不存在"}
    if p["state"] == "archived":
        return {"ok": True, "packet_id": pid, "already": True, "reason": reason}

    t = now()
    open_leases = rows(
        c, "SELECT lease_id FROM leases WHERE packet_id=? AND released_at IS NULL", (pid,),
    )
    for r in open_leases:
        c.execute(
            "UPDATE leases SET released_at=?, released_reason=? WHERE lease_id=?",
            (t, f"abandoned:{reason}", r["lease_id"]),
        )
    n_gt = int(c.execute("DELETE FROM gt WHERE packet_id=?", (pid,)).rowcount or 0)
    pending = rows(
        c, "SELECT submission_id, n_labeled FROM submissions WHERE packet_id=? AND state='pending'",
        (pid,),
    )
    for s in pending:
        c.execute(
            "UPDATE submissions SET state='rejected', n_accepted=0, n_rejected=?, reviewed_at=? "
            "WHERE submission_id=?",
            (int(s["n_labeled"] or 0), t, s["submission_id"]),
        )
        c.execute(
            "INSERT INTO reviews(submission_id, reviewer, decision, notes, created_at) "
            "VALUES(?,?,?,?,?)",
            (s["submission_id"], worker or "abandon", "reject",
             f"抛弃本包：{reason}", t),
        )
    c.execute(
        "UPDATE packets SET state='archived', need_frames_json=NULL, updated_at=? WHERE packet_id=?",
        (t, pid),
    )
    c.commit()
    return {
        "ok": True,
        "packet_id": pid,
        "reason": reason,
        "n_gt_removed": n_gt,
        "n_pending_rejected": len(pending),
        "n_leases_released": len(open_leases),
        "packet_state": "archived",
    }


def is_fail_release(reason: Optional[str]) -> bool:
    """no_seed / 处理异常的释放：不算真正消耗，也不该立刻再发给同一 worker。"""
    s = str(reason or "")
    return s == "no_seed" or s.startswith("error:")


# ---------------------------------------------------------------- GT


def write_gt(
    c: sqlite3.Connection,
    packet_id: str,
    labels: Iterable[dict],
    *,
    source: str,
    worker: str,
    submission_id: Optional[int],
    only_frames: Optional[Iterable[int]] = None,
) -> int:
    """把标注写进 gt（按 (packet_id, frame_idx) upsert）。返回写入行数。

    upsert 而不是先删后插：修补模式下人工只重标被打回的几帧，其余 AI 帧要留着。
    """
    keep = set(int(f) for f in only_frames) if only_frames is not None else None
    t = now()
    n = 0
    for r in labels:
        fi = r.get("frame_idx")
        if fi is None:
            continue
        fi = int(fi)
        if keep is not None and fi not in keep:
            continue
        c.execute(
            "INSERT INTO gt(packet_id, frame_idx, pano_x, pano_y, visible, occluded, blurred, "
            "interp, conf, source, worker, submission_id, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(packet_id, frame_idx) DO UPDATE SET "
            "  pano_x=excluded.pano_x, pano_y=excluded.pano_y, visible=excluded.visible, "
            "  occluded=excluded.occluded, blurred=excluded.blurred, interp=excluded.interp, "
            "  conf=excluded.conf, source=excluded.source, worker=excluded.worker, "
            "  submission_id=excluded.submission_id, updated_at=excluded.updated_at",
            (
                packet_id, fi,
                None if r.get("pano_x") is None else float(r["pano_x"]),
                None if r.get("pano_y") is None else float(r["pano_y"]),
                1 if r.get("visible") else 0,
                1 if r.get("occluded") else 0,
                1 if r.get("blurred") else 0,
                1 if r.get("interp") else 0,
                None if r.get("conf") is None else float(r["conf"]),
                source, worker, submission_id, t,
            ),
        )
        n += 1
    c.commit()
    return n


def delete_gt_frames(
    c: sqlite3.Connection,
    packet_id: str,
    frames: Iterable[int],
) -> int:
    """从 gt 删掉指定帧。已入库再审打回时用。返回删除行数。"""
    fs = [int(f) for f in frames]
    if not fs:
        return 0
    qs = ",".join("?" * len(fs))
    cur = c.execute(
        f"DELETE FROM gt WHERE packet_id=? AND frame_idx IN ({qs})",
        [packet_id, *fs],
    )
    return int(cur.rowcount or 0)


# ---------------------------------------------------------------- 采集端


def producer_health(row: Dict[str, Any], poll_sec: int = 300, t: Optional[float] = None) -> str:
    """判定产包作业健康度。

    **死掉的进程发不出死讯**，所以健康度不能只看心跳有没有到，还要用 ACP 作业状态
    归因（计划「判定规则」）：心跳超时 → 先看 ACP 说什么，FAILED 就是 failed、
    查不到就是 gone、还 RUNNING 就是卡住（stale，大概率 ffmpeg 或上传挂住）。
    """
    t = t if t is not None else now()
    acp = (row.get("acp_state") or "").upper()
    if acp in ("FAILED", "ERROR"):
        return "failed"
    if acp in ("GONE", "NOTFOUND", "NOT_FOUND", "DELETED"):
        return "gone"
    if acp in ("SUCCEEDED", "COMPLETED", "STOPPED", "TERMINATED"):
        return "idle"
    hb = row.get("last_heartbeat")
    limit = 2 * int(row.get("poll_sec") or poll_sec)
    if hb is None:
        # 只有 collector_agent 报了 ACP 状态、作业内心跳没通（ACP 节点可能不出网）
        return "running" if acp in ("RUNNING", "PENDING") else "stale"
    if t - hb > limit:
        return "stale"
    return "running"


def refresh_producer_health(c: sqlite3.Connection) -> None:
    t = now()
    for r in rows(c, "SELECT * FROM producers"):
        h = producer_health(r, t=t)
        if h != r.get("health"):
            c.execute("UPDATE producers SET health=?, updated_at=? WHERE job_id=?", (h, t, r["job_id"]))
    c.commit()


def add_producer_event(c: sqlite3.Connection, job_id: str, kind: str, msg: str = "", extra: Any = None) -> int:
    c.execute(
        "INSERT INTO producer_events(job_id, ts, kind, msg, extra_json) VALUES(?,?,?,?,?)",
        (job_id, now(), kind, msg or "", jdump(extra) if extra is not None else None),
    )
    c.commit()
    return int(scalar(c, "SELECT last_insert_rowid()"))


# ---------------------------------------------------------------- 池子


def pool_counts(c: sqlite3.Connection) -> Dict[str, int]:
    out = {s: 0 for s in PACKET_STATES}
    for r in rows(c, "SELECT state, COUNT(*) n FROM packets GROUP BY state"):
        out[r["state"]] = int(r["n"])
    out["n_pending_review"] = int(scalar(c, "SELECT COUNT(*) FROM submissions WHERE state='pending'"))
    out["n_total"] = int(scalar(c, "SELECT COUNT(*) FROM packets"))
    return out


def rate_per_h(c: sqlite3.Connection, sql: str, args: Sequence = (), window_h: float = 1.0) -> float:
    """近 window_h 小时的速率（个/小时）。"""
    t0 = now() - window_h * 3600
    n = int(scalar(c, sql, tuple(args) + (t0,)))
    return round(n / window_h, 2)


def pool_view(c: sqlite3.Connection, keep_available: int, batch: int, window_h: float = 1.0) -> Dict[str, Any]:
    """`GET /api/v2/pool` 的正文，也是产包端补池的**控制信号**。

    旧的 `store.pool_status()` 数 AOSS 上没有 claim 的 zip。改成 SQLite 权威后
    AOSS 上不再写 claims/，那个数会恒等于全部已产包，产包端会以为池子永远满的、
    从此再不补包。所以补几个由这里说，产包端照 `want_more` 干活。
    """
    expire_leases(c)
    cnt = pool_counts(c)
    n_avail = cnt["available"]
    produce = rate_per_h(c, "SELECT COUNT(*) FROM packets WHERE created_at >= ?", (), window_h)
    consume = rate_per_h(
        c,
        "SELECT COUNT(*) FROM leases WHERE kind IN ('ai','human') AND created_at >= ? "
        "AND (released_reason IS NULL OR (released_reason<>'no_seed' "
        "AND released_reason NOT LIKE 'error:%'))",
        (),
        window_h,
    )
    net = consume - produce
    eta = round(n_avail / net * 60, 1) if net > 0.01 else None
    want = max(0, keep_available - n_avail)
    return {
        "n_available": n_avail,
        "n_leased": cnt["leased"],
        "n_pending_review": cnt["n_pending_review"],
        "n_submitted": cnt["submitted"],
        "n_accepted": cnt["accepted"],
        "n_need_human": cnt["need_human"],
        "n_total": cnt["n_total"],
        "produce_rate_per_h": produce,
        "consume_rate_per_h": consume,
        "eta_empty_min": eta,
        "keep_available": keep_available,
        "batch": batch,
        "want_more": min(want, batch) if batch > 0 else want,
        "want_more_total": want,
        "n_live_producers": int(
            scalar(c, "SELECT COUNT(*) FROM producers WHERE health IN ('running','idle')")
        ),
        "ts": now(),
    }


def pool_depth_series(c: sqlite3.Connection, hours: float = 24.0, buckets: int = 96) -> List[Dict[str, Any]]:
    """重放事件算出池深随时间的曲线。

    没有专门的采样表：`available` 的历史完全可以由「包何时注册」和「租约何时开」
    重放出来，而采样表会在服务重启或请求稀疏时留下空洞。代价是每次查询要扫两张
    小表，几千行的规模下无所谓。
    """
    t_end = now()
    t0 = t_end - hours * 3600
    step = (t_end - t0) / max(1, buckets)

    # 先算 t0 时刻的存量，再往后逐桶累加增减。
    # 租约会释放回池：只扣还没还的、以及提交走了的，空转 no_seed/error 不能把池深打成 0。
    base = int(scalar(c, "SELECT COUNT(*) FROM packets WHERE created_at < ?", (t0,))) - int(
        scalar(
            c,
            "SELECT COUNT(*) FROM leases WHERE created_at < ? AND ("
            "  released_at IS NULL OR released_at >= ? "
            "  OR (released_reason IS NOT NULL AND released_reason<>'no_seed' "
            "      AND released_reason NOT LIKE 'error:%'))",
            (t0, t0),
        )
    )
    adds = rows(c, "SELECT created_at ts FROM packets WHERE created_at >= ?", (t0,))
    subs = rows(
        c,
        "SELECT created_at ts, released_reason FROM leases WHERE created_at >= ?",
        (t0,),
    )
    delta = [0] * (buckets + 1)
    for r in adds:
        delta[min(buckets, int((r["ts"] - t0) / step))] += 1
    for r in subs:
        if is_fail_release(r.get("released_reason")):
            continue
        delta[min(buckets, int((r["ts"] - t0) / step))] -= 1

    out: List[Dict[str, Any]] = []
    cur = base
    for i in range(buckets):
        cur += delta[i]
        out.append({"ts": t0 + (i + 1) * step, "n_available": max(0, cur),
                    "produced": delta[i] if delta[i] > 0 else 0})
    # 生产/消费两条线单独出，看板要叠在池深上
    prod = [0] * buckets
    cons = [0] * buckets
    for r in adds:
        i = min(buckets - 1, int((r["ts"] - t0) / step))
        prod[i] += 1
    for r in subs:
        if is_fail_release(r.get("released_reason")):
            continue
        i = min(buckets - 1, int((r["ts"] - t0) / step))
        cons[i] += 1
    per_h = 3600.0 / step
    for i, o in enumerate(out):
        o["produce_rate_per_h"] = round(prod[i] * per_h, 2)
        o["consume_rate_per_h"] = round(cons[i] * per_h, 2)
    return out


# ---------------------------------------------------------------- CLI

def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="标注系统中心库：建库 / 迁移 / 概况")
    p.add_argument("--db", default=None)
    p.add_argument("--stats", action="store_true", help="打印各表行数与池子概况")
    a = p.parse_args()

    path = Path(a.db or default_db_path())
    c = connect(path)
    v = migrate(c)
    mode = scalar(c, "PRAGMA journal_mode", (), "?")
    print(f"[db]   {path}")
    print(f"[ver]  schema v{v}   journal_mode={mode}")
    if a.stats:
        for t in ("packets", "leases", "submissions", "reviews", "gt", "producers", "producer_events"):
            print(f"  {t:16s} {scalar(c, f'SELECT COUNT(*) FROM {t}'):>8}")
        print("  pool:", jdump(pool_view(c, 40, 10)))
    c.close()


if __name__ == "__main__":
    main()
