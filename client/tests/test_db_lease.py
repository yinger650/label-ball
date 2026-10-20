#!/usr/bin/env python3
"""定向 lease、种子优先、migrate v2。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))

import db  # noqa: E402


def _pkt(c, pid, state="available", seed=None, priority=0):
    db.register_packet(c, pid, f"packets/{pid}.zip", {"targets": [1, 2, 3]}, priority=priority)
    if state != "available":
        c.execute("UPDATE packets SET state=? WHERE packet_id=?", (state, pid))
        c.commit()
    if seed is not None:
        c.execute(
            "UPDATE packets SET seed_json=?, seed_worker=?, seed_at=? WHERE packet_id=?",
            (db.jdump(seed), "wangmin", db.now(), pid),
        )
        c.commit()


def test_want_packet_and_seed_priority():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.sqlite3"
        c = db.connect(path)
        v = db.migrate(c)
        assert v >= 2
        cols = {r[1] for r in c.execute("PRAGMA table_info(packets)")}
        assert "seed_json" in cols

        _pkt(c, "old_plain")
        _pkt(c, "seeded", seed=[{"frame_idx": 1, "visible": True, "pano_x": 1, "pano_y": 2}])
        _pkt(c, "need_me", state="need_human")

        got = db.pick_packet(c, "ai-1", "ai")
        assert got and got["packet_id"] == "seeded", got

        directed = db.pick_packet(c, "wangmin", "human", want_packet="old_plain")
        assert directed and directed["packet_id"] == "old_plain"

        bad = db.pick_packet(c, "wangmin", "repair", want_packet="old_plain")
        assert bad is None

        repair = db.pick_packet(c, "wangmin", "repair", want_packet="need_me")
        assert repair and repair["packet_id"] == "need_me"

        missing = db.pick_packet(c, "wangmin", "human", want_packet="no_such")
        assert missing is None

        # 抛弃：leased / 待审都会从队列消失
        lid = db.open_lease(c, directed, "wangmin", "human")
        c.execute(
            "INSERT INTO submissions(packet_id, worker, source, labels_json, n_targets, "
            "n_labeled, state, created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("old_plain", "ai-1", "ai", "[]", 3, 3, "pending", db.now()),
        )
        c.commit()
        out = db.abandon_packet(c, "old_plain", reason="bad_quality", worker="wangmin")
        assert out["ok"] and out["packet_state"] == "archived", out
        assert out["n_pending_rejected"] == 1
        row = db.one(c, "SELECT state FROM packets WHERE packet_id=?", ("old_plain",))
        assert row["state"] == "archived"
        sub = db.one(c, "SELECT state FROM submissions WHERE packet_id=?", ("old_plain",))
        assert sub["state"] == "rejected"
        c.close()


def test_requeue_need_human_to_available():
    with tempfile.TemporaryDirectory() as td:
        c = db.connect(Path(td) / "t.sqlite3")
        db.migrate(c)
        _pkt(c, "fix_me", state="need_human")
        c.execute(
            "UPDATE packets SET need_frames_json=? WHERE packet_id=?",
            (db.jdump([10, 20, 30]), "fix_me"),
        )
        _pkt(c, "ok_avail")
        _pkt(c, "done", state="accepted")
        c.commit()

        out = db.requeue_packets(c, ["fix_me", "ok_avail", "done", "nope"])
        assert out["moved"] == ["fix_me"], out
        reasons = {x["packet_id"]: x["reason"] for x in out["skipped"]}
        assert "ok_avail" in reasons and "done" in reasons and "nope" in reasons

        row = db.one(c, "SELECT state, need_frames_json FROM packets WHERE packet_id=?", ("fix_me",))
        assert row["state"] == "available"
        assert row["need_frames_json"] is None
        c.close()


def _sub(c, pid, worker, source, state):
    cur = c.execute(
        "INSERT INTO submissions(packet_id, worker, source, labels_json, n_targets, "
        "n_labeled, state, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (pid, worker, source, "[]", 3, 3, state, db.now()),
    )
    c.commit()
    return int(cur.lastrowid)


def test_status_recall_and_delete():
    with tempfile.TemporaryDirectory() as td:
        c = db.connect(Path(td) / "t.sqlite3")
        db.migrate(c)
        _pkt(c, "fresh")
        _pkt(c, "busy")
        _pkt(c, "waiting", state="submitted")
        _pkt(c, "done", state="accepted")
        _pkt(c, "part", state="need_human")
        c.execute(
            "UPDATE packets SET need_frames_json=? WHERE packet_id=?",
            (db.jdump([2, 3]), "part"),
        )
        c.execute("UPDATE packets SET produced_at=? WHERE packet_id=?", (10, "fresh"))
        c.execute("UPDATE packets SET produced_at=? WHERE packet_id=?", (50, "done"))
        c.commit()

        busy = db.one(c, "SELECT * FROM packets WHERE packet_id=?", ("busy",))
        db.open_lease(c, busy, "alice", "human")
        _sub(c, "waiting", "bob", "ai", "pending")
        sid = _sub(c, "done", "carol", "ai", "accepted")
        part_sid = _sub(c, "part", "carol", "ai", "partial")
        t = db.now()
        c.execute(
            "INSERT INTO reviews(submission_id, reviewer, decision, created_at) VALUES(?,?,?,?)",
            (sid, "dave", "accept", t),
        )
        c.execute(
            "INSERT INTO reviews(submission_id, reviewer, decision, created_at) VALUES(?,?,?,?)",
            (part_sid, "eve", "partial", t),
        )
        c.execute(
            "INSERT INTO gt(packet_id, frame_idx, visible, source, worker, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            ("done", 1, 1, "ai", "carol", t),
        )
        c.commit()

        listed = {p["packet_id"]: p for p in db.list_packets(c, order="produced")["packets"]}
        assert listed["busy"]["status_text"] == "被 alice 领取标注中（人工）", listed["busy"]
        assert listed["busy"]["lease_worker"] == "alice"
        assert listed["waiting"]["status_text"] == "bob 标注完，待审核"
        assert listed["done"]["status_text"] == "dave 已完成审核入库"
        assert listed["part"]["status_text"] == "eve 部分通过，剩余 2 帧待重标"
        assert listed["fresh"]["status_text"] == "待领取"
        order = [p["packet_id"] for p in db.list_packets(c, order="produced")["packets"]]
        assert order.index("done") < order.index("fresh"), order

        out = db.recall_packets(c, ["busy", "fresh"])
        assert out["moved"] == ["busy"], out
        assert any(s["packet_id"] == "fresh" for s in out["skipped"])
        row = db.one(c, "SELECT state FROM packets WHERE packet_id=?", ("busy",))
        assert row["state"] == "available"
        lease = db.one(c, "SELECT released_reason FROM leases WHERE packet_id=?", ("busy",))
        assert lease["released_reason"] == "recalled"

        gone = db.delete_packets(c, ["done", "nope"])
        assert gone["deleted"] == ["done"], gone
        assert db.one(c, "SELECT packet_id FROM packets WHERE packet_id=?", ("done",)) is None
        assert int(db.scalar(c, "SELECT COUNT(*) FROM gt WHERE packet_id=?", ("done",))) == 0
        assert int(db.scalar(c, "SELECT COUNT(*) FROM submissions WHERE packet_id=?", ("done",))) == 0
        assert int(db.scalar(c, "SELECT COUNT(*) FROM reviews WHERE submission_id=?", (sid,))) == 0
        c.close()


if __name__ == "__main__":
    test_want_packet_and_seed_priority()
    test_requeue_need_human_to_available()
    test_status_recall_and_delete()
    print("ok db_lease")
