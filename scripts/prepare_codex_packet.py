#!/usr/bin/env python3
"""Lease/prepare one packet locally; no model is invoked by this process."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from client.agent_core import AgentSession
from client.api import ApiError, ServerClient


def write_status(folder, **data):
    try:
        data.setdefault('iteration', json.loads((folder/'status.json').read_text()).get('iteration', 0))
    except (OSError, ValueError):
        pass
    data.update(updated_at=time.time(), model='gpt-6.1-sol')
    temp = folder/'status.transport.tmp'
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    temp.replace(folder/'status.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    worker, work = folder.name, folder/'work'
    api = ServerClient('https://ballgt.yinger650.com', worker=worker, timeout=15)
    # A stopped client may have left a lease that has since been reclaimed.
    for path in work.glob('*/session.json'):
        session = json.loads(path.read_text())
        if session.get('done'):
            continue
        try:
            api.heartbeat(session['lease_id'], 'prepare')
        except ApiError as exc:
            if exc.status not in (404, 409):
                write_status(folder, state='waiting_server', reason='lease_validation_unavailable')
                return 3
            session.update(done=True, stale_lease_reason='lease_expired_or_released')
            tmp = path.with_suffix('.transport.tmp')
            tmp.write_text(json.dumps(session, ensure_ascii=False)+'\n')
            tmp.replace(path)
    agent = AgentSession(api, work_root=work, worker=worker, method={
        'harness': 'codex', 'harness_version': '0.163.0-alpha.11.1',
        'model': 'gpt-6.1-sol', 'model_version': 'unknown'})
    claim = agent.claim()
    if claim.get('error'):
        write_status(folder, state='waiting_server', reason='lease_request_failed')
        return 3
    packet = claim.get('packet_id')
    if not packet:
        try:
            pool = api.get('/api/v2/pool')
        except ApiError:
            pool = {}
        write_status(folder, state='waiting_packet', reason='no_eligible_packet',
                     next_probe_seconds=60, pool={k: pool.get(k) for k in (
                         'n_available', 'n_schedulable', 'want_more', 'n_live_producers')})
        return 2
    deadline = time.monotonic()+600
    while time.monotonic() < deadline:
        job = agent.job_state(packet)
        write_status(folder, state='preparing', packet_id=packet, preparation=job)
        if job.get('ready'):
            print(json.dumps({'packet_id': packet, 'ready': True}))
            return 0
        if job.get('state') == 'error':
            agent.release(packet, reason_code='download_error', note='Local preparation failed before model startup')
            write_status(folder, state='waiting_storage', reason='packet_preparation_failed', next_probe_seconds=60)
            return 3
        time.sleep(3)
    agent.release(packet, reason_code='download_error', note='Local preparation timed out after 10 minutes')
    write_status(folder, state='waiting_storage', reason='packet_preparation_timeout', next_probe_seconds=60)
    return 3


if __name__ == '__main__':
    try:
        code = main()
    except Exception as exc:
        folder_arg = sys.argv[sys.argv.index('--folder')+1]
        write_status(Path(folder_arg), state='waiting_server', reason='transport_failed',
                     error_type=type(exc).__name__, next_probe_seconds=60)
        code = 3
    raise SystemExit(code)
