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
        assert listed["part"]["status_text"] == "eve 部分通过，2 帧待人工修复"
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


CODEX = {"harness": "codex", "harness_version": "0.48", "model": "gpt-6-astra", "model_version": "2026-09"}
DSH = {"harness": "dsh", "harness_version": "0.4.1", "model": "glm-5.3-flash", "model_version": ""}


def _ai_sub(c, pid, info, labels):
    method = db.method_key("ai", "agent", info)
    cur = c.execute(
        "INSERT INTO submissions(packet_id, worker, source, labels_json, n_targets, n_labeled, state, "
        "created_at, harness, harness_version, model, model_version, method) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "agent", "ai", db.jdump(labels), len(labels), len(labels), "pending", db.now(),
         info["harness"], info["harness_version"], info["model"], info["model_version"], method),
    )
    c.execute("UPDATE packets SET state='submitted' WHERE packet_id=?", (pid,))
    c.commit()
    return int(cur.lastrowid)


LABELS = [{"frame_idx": f, "visible": True, "pano_x": 100.0 + f, "pano_y": 50.0} for f in (1, 2, 3)]


def test_method_key_and_tag():
    assert db.method_key("ai", "x", CODEX) == "ai:codex@0.48|gpt-6-astra@2026-09"
    assert db.method_key("ai", "x", {"model": "m"}) == db.UNKNOWN_AI_METHOD
    assert db.method_key("human", "alice") == "human:alice"
    assert db.method_tag("ai", "x", CODEX, "2026-10-20") == "codex 0.48 · gpt-6-astra 2026-09 · 2026-10-20"


def test_relabel_blocks_same_method_only():
    with tempfile.TemporaryDirectory() as td:
        c = db.connect(Path(td) / "t.sqlite3")
        db.migrate(c)
        _pkt(c, "p1")
        sid = _ai_sub(c, "p1", CODEX, LABELS)
        out = db.review_submission(c, sid, "reject", reviewer="rv")
        assert out["decision"] == "relabel" and out["packet_state"] == "available", out
        assert int(db.scalar(c, "SELECT COUNT(*) FROM gt WHERE packet_id='p1'")) == 0
        codex = db.method_key("ai", "agent", CODEX)
        assert out["blocked_methods"] == [codex]

        assert db.pick_packet(c, "ai-2", "ai", method=codex) is None
        assert db.pick_packet(c, "ai-2", "ai", want_packet="p1", method=codex) is None
        assert db.pick_packet(c, "ai-2", "ai", method=db.UNKNOWN_AI_METHOD) is None
        got = db.pick_packet(c, "ai-2", "ai", method=db.method_key("ai", "agent", DSH))
        assert got and got["packet_id"] == "p1"
        got = db.pick_packet(c, "bob", "human", method="human:bob")
        assert got and got["packet_id"] == "p1"

        listed = {p["packet_id"]: p for p in db.list_packets(c)["packets"]}
        assert listed["p1"]["status_text"].startswith("待重标"), listed["p1"]["status_text"]
        c.close()


def test_accept_repair_remove():
    with tempfile.TemporaryDirectory() as td:
        c = db.connect(Path(td) / "t.sqlite3")
        db.migrate(c)
        db.register_packet(c, "g__s__0010_0020", "k", {"targets": [1, 2, 3], "group": "g", "stem": "s",
                                                      "produced_at": "2026-10-01T08:00:00+00:00"})
        _pkt(c, "ok")
        _pkt(c, "fix")

        sid = _ai_sub(c, "ok", CODEX, LABELS)
        out = db.review_submission(c, sid, "accept", reviewer="rv")
        assert out["packet_state"] == "accepted" and out["n_gt_written"] == 3

        sid = _ai_sub(c, "fix", CODEX, LABELS)
        out = db.review_submission(c, sid, "repair", reviewer="rv", frames={"2": False, "3": True})
        assert out["packet_state"] == "need_human" and out["n_rejected"] == 1, out
        row = db.one(c, "SELECT need_frames_json FROM packets WHERE packet_id='fix'")
        assert db.jload(row["need_frames_json"]) == [2]
        assert int(db.scalar(c, "SELECT COUNT(*) FROM gt WHERE packet_id='fix'")) == 2
        assert db.pick_packet(c, "carol", "repair")["packet_id"] == "fix"

        sid = _ai_sub(c, "g__s__0010_0020", CODEX, LABELS)
        out = db.review_submission(c, sid, "remove", reviewer="rv", reason="画面花屏",
                                   ban_source_video=True)
        assert out["packet_state"] == "archived" and out["banned_video"] == "g__s", out
        p = db.one(c, "SELECT * FROM packets WHERE packet_id='g__s__0010_0020'")
        assert p["state"] == "archived" and p["removed_reason"] == "画面花屏" and p["removed_by"] == "rv"
        assert p["produced_at"] and p["produced_at"] < db.now()
        assert db.one(c, "SELECT state FROM submissions WHERE submission_id=?", (sid,))["state"] == "rejected"
        bl = db.blocklist(c)
        assert bl["video_keys"] == ["g__s"] and "g__s__0010_0020" in bl["packets"], bl
        c.close()


def test_release_classification():
    assert db.classify_release("hard:cannot_locate: lost after kick") == "hard"
    assert db.classify_release("cannot_locate_match_ball_at_cold_start") == "hard"
    assert db.classify_release("cannot_reliably_locate_air_ball_in_RGB_after_kick") == "hard"
    assert db.classify_release("cannot_reliably_distinguish_match_ball_from_far_goal_fence") == "hard"
    assert db.classify_release("cannot_locate_match_ball_reliably_after_occlusion") == "hard"
    assert db.classify_release("error:download_error: GetObject 403") == "infra"
    assert db.classify_release("packet_download_GetObject_failed") == "infra"
    assert db.classify_release("no_seed") == "infra"
    assert db.classify_release("user_stop") == "normal"
    assert db.classify_release("released") == "normal"


def _release(c, pid, worker, method, reason, ago=0.0):
    p = db.one(c, "SELECT * FROM packets WHERE packet_id=?", (pid,))
    lease = db.open_lease(c, p, worker, "ai", method=method)
    db.release_lease(c, lease["lease_id"], reason)
    if ago:
        c.execute("UPDATE leases SET released_at=released_at-? WHERE lease_id=?", (ago, lease["lease_id"]))
        c.commit()


def test_hard_release_cools_same_method_only():
    with tempfile.TemporaryDirectory() as td:
        c = db.connect(Path(td) / "t.sqlite3")
        db.migrate(c)
        _pkt(c, "hard", priority=5)
        _pkt(c, "easy")
        codex = db.method_key("ai", "agent", CODEX)
        dsh = db.method_key("ai", "agent", DSH)

        _release(c, "hard", "ai-1", codex, "hard:cannot_locate: cold start")
        # 换个 worker 名字、同一方法也领不到；其他方法和人工照常
        assert db.pick_packet(c, "ai-9", "ai", method=codex)["packet_id"] == "easy"
        assert db.pick_packet(c, "ai-1", "ai", method=dsh)["packet_id"] == "hard"
        assert db.pick_packet(c, "bob", "human", method="human:bob")["packet_id"] == "hard"

        # 旧客户端的自由文本也按 hard 冷却
        _pkt(c, "legacy", priority=9)
        _release(c, "legacy", "ai-1", codex, "cannot_locate_match_ball_at_cold_start")
        assert db.pick_packet(c, "ai-1", "ai", method=codex)["packet_id"] == "easy"

        # 冷却到期后能再领
        c.execute("UPDATE leases SET released_at=released_at-? WHERE packet_id IN ('hard','legacy')",
                  (db.HARD_COOLDOWN_SEC + 1,))
        c.commit()
        assert db.pick_packet(c, "ai-1", "ai", method=codex)["packet_id"] == "legacy"
        c.close()


def test_infra_release_short_cooldown_per_worker():
    with tempfile.TemporaryDirectory() as td:
        c = db.connect(Path(td) / "t.sqlite3")
        db.migrate(c)
        _pkt(c, "net", priority=5)
        _pkt(c, "other")
        codex = db.method_key("ai", "agent", CODEX)
        _release(c, "net", "ai-1", codex, "error:download_error: GetObject failed")
        assert db.pick_packet(c, "ai-1", "ai", method=codex)["packet_id"] == "other"
        # 网络错误不算标注能力失败：同方法另一个 worker 立刻能领
        assert db.pick_packet(c, "ai-2", "ai", method=codex)["packet_id"] == "net"
        c.execute("UPDATE leases SET released_at=released_at-? WHERE packet_id='net'",
                  (db.FAIL_COOLDOWN_SEC + 1,))
        c.commit()
        assert db.pick_packet(c, "ai-1", "ai", method=codex)["packet_id"] == "net"
        # 普通释放（换包 / 用户叫停）不冷却
        _release(c, "other", "ai-1", codex, "user_stop")
        assert db.pick_packet(c, "ai-1", "ai", method=codex)["packet_id"] == "net"
        c.close()


if __name__ == "__main__":
    test_release_classification()
    test_hard_release_cools_same_method_only()
    test_infra_release_short_cooldown_per_worker()
    test_want_packet_and_seed_priority()
    test_requeue_need_human_to_available()
    test_status_recall_and_delete()
    test_method_key_and_tag()
    test_relabel_blocks_same_method_only()
    test_accept_repair_remove()
    print("ok db_lease")
