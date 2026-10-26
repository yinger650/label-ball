"""Production must refill when raw stock cannot be leased by current annotators."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'server'))
import db


def packet(c, name):
    db.register_packet(c, name, f'packets/{name}/', {'targets': [1, 2, 3]})
    return db.one(c, 'SELECT * FROM packets WHERE packet_id=?', (name,))


def test_cooling_stock_does_not_starve_production():
    with tempfile.TemporaryDirectory() as td:
        c = db.connect(Path(td)/'db.sqlite3')
        db.migrate(c)
        method = 'ai:codex@test|gpt-6.1-sol@unknown'
        hard = packet(c, 'hard')
        lease = db.open_lease(c, hard, 'worker-1', 'ai', method=method)
        db.release_lease(c, lease['lease_id'], 'hard:cannot_locate')
        view = db.pool_view(c, 1, 10)
        assert view['n_available'] == 1
        assert view['n_schedulable'] == 0 and view['want_more'] == 1
        assert db.pick_packet(c, 'worker-1', 'ai', method=method) is None
        # A second worker with the same model must not bypass the hard cooldown.
        assert db.pick_packet(c, 'worker-2', 'ai', method=method) is None
        packet(c, 'fresh')
        view = db.pool_view(c, 1, 10)
        assert view['n_available'] == 2 and view['n_schedulable'] == 1 and view['want_more'] == 0
        assert db.pick_packet(c, 'worker-1', 'ai', method=method)['packet_id'] == 'fresh'
        c.close()


def test_method_rejected_and_pending_stock_are_not_capacity():
    with tempfile.TemporaryDirectory() as td:
        c = db.connect(Path(td)/'db.sqlite3')
        db.migrate(c)
        method = 'ai:codex@test|gpt-6.1-sol@unknown'
        p = packet(c, 'rejected')
        lease = db.open_lease(c, p, 'worker-1', 'ai', method=method)
        db.release_lease(c, lease['lease_id'], 'user_stop')
        c.execute('UPDATE packets SET blocked_methods_json=? WHERE packet_id=?',
                  (db.jdump([method]), 'rejected'))
        packet(c, 'pending')
        c.execute("INSERT INTO submissions(packet_id,worker,source,labels_json,n_targets,n_labeled,state,created_at) "
                  "VALUES('pending','other','ai','[]',3,3,'pending',?)", (db.now(),))
        c.commit()
        view = db.pool_view(c, 3, 2)
        assert view['n_available'] == 2 and view['n_schedulable'] == 0
        assert view['want_more'] == 2 and view['want_more_total'] == 3
        c.close()


def test_no_recent_consumer_preserves_raw_target():
    with tempfile.TemporaryDirectory() as td:
        c = db.connect(Path(td)/'db.sqlite3')
        db.migrate(c)
        p = packet(c, 'one')
        assert db.pool_view(c, 2, 10)['want_more'] == 1
        lease = db.open_lease(c, p, 'old-worker', 'ai', method='old-model')
        db.release_lease(c, lease['lease_id'], 'hard:cannot_locate')
        c.execute('UPDATE leases SET created_at=0,last_heartbeat=0')
        c.commit()
        view = db.pool_view(c, 2, 10)
        assert view['n_recent_ai_consumers'] == 0 and view['n_schedulable'] == 1
        c.close()


if __name__ == '__main__':
    test_cooling_stock_does_not_starve_production()
    test_method_rejected_and_pending_stock_are_not_capacity()
    test_no_recent_consumer_preserves_raw_target()
    print('ok pool_capacity')
