#!/usr/bin/env python3
"""Read-only localhost dashboard for the Codex annotation pool."""
import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def recent_events(path):
    """Only expose tool names and annotator messages, never shell commands/results."""
    try:
        with path.open('rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell()-160000))
            lines = f.read().decode('utf-8', 'replace').splitlines()
        modified = path.stat().st_mtime
    except OSError:
        return [], None
    result = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get('item', {})
        if event.get('type') != 'item.completed':
            continue
        if item.get('type') == 'mcp_tool_call':
            result.append({'kind': 'tool', 'text': item.get('tool', ''),
                           'error': bool(item.get('error'))})
        elif item.get('type') == 'agent_message':
            result.append({'kind': 'message', 'text': item.get('text', '')[:350]})
    return result[-8:], modified


def packet_progress(path):
    session = read_json(path)
    rows = read_json(path.parent/'labels.json')
    targets = {str(f) for f in session.get('target_frames', [])}
    visible = absent = 0
    for frame in targets:
        row = rows.get(frame, {})
        if row.get('visible') is False:
            absent += 1
        elif row.get('visible') is True and row.get('pano_x') is not None and row.get('pano_y') is not None:
            visible += 1
    return {'packet_id': session.get('packet_id', path.parent.name),
            'total': len(targets), 'labeled': visible+absent, 'visible': visible,
            'absent': absent, 'missing': max(0, len(targets)-visible-absent),
            'done': bool(session.get('done')), 'started_at': session.get('started_at'),
            'reason': session.get('released_reason', ''),
            'prepared': read_json(path.parent/'prepare.json').get('state') == 'ready',
            'updated_at': max(path.stat().st_mtime,
                              (path.parent/'labels.json').stat().st_mtime if (path.parent/'labels.json').exists() else 0)}


def snapshot(out):
    pool = read_json(out/'status.json')
    launch = read_json(out/'launch.json')
    supervisor_alive = alive(pool.get('pid', launch.get('supervisor_pid')))
    enabled = supervisor_alive and pool.get('state') != 'stopped'
    workers = []
    receipts = {}
    released = 0
    for folder in sorted(out.glob('wangmin-codex61-p*')):
        if not folder.is_dir():
            continue
        status = read_json(folder/'status.json')
        logs = sorted(p for p in folder.glob('*.jsonl') if p.stem.isdigit())
        events, activity = recent_events(logs[-1]) if logs else ([], None)
        packets = []
        for path in (folder/'work').glob('*/session.json'):
            try:
                packets.append(packet_progress(path))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        active = sorted((p for p in packets if not p['done']), key=lambda p: p['updated_at'], reverse=True)
        for path in (folder/'work').glob('*/receipt.json'):
            r = read_json(path)
            if not r.get('submission_id'):
                continue
            receipts[str(r['submission_id'])] = {k: r.get(k) for k in (
                'submission_id', 'packet_id', 'submitted_at', 'n_targets', 'n_labeled', 'review_state')}
            receipts[str(r['submission_id'])]['worker'] = folder.name
        # Count attempts, not unique packets: a released packet can be claimed again.
        try:
            for line in (folder/'runs.jsonl').read_text().splitlines():
                try:
                    released += sum(r.get('outcome') == 'released' for r in json.loads(line).get('outcomes', []))
                except (ValueError, TypeError):
                    continue
        except OSError:
            pass
        process_alive = alive(status.get('pid'))
        state = status.get('state', 'unknown') if enabled else 'stopped'
        if state == 'running' and not process_alive:
            state = 'process_exited'
        if state in ('preparing', 'waiting_packet', 'waiting_storage', 'waiting_server'):
            # Transport runs locally between model sessions; its current status
            # must not inherit an old model's "no packet" message/activity time.
            events, activity = [], status.get('updated_at')
        last_tool = next((e['text'] for e in reversed(events) if e['kind'] == 'tool'), '')
        current = active[0] if active else None
        last_packet = max(packets, key=lambda p: p['updated_at']) if packets else None
        stage = {'ball_review': '视觉复核', 'ball_submit': '正在提交',
                 'ball_overview': '确认比赛球', 'ball_scan': '确认比赛球',
                 'ball_plan': '准备标注', 'ball_lease': '领取包',
                 'ball_status': '下载 / 解码'}.get(last_tool, '逐帧标注')
        if current and current['total'] and current['labeled'] == current['total'] and stage == '逐帧标注':
            stage = '已标满，待复核提交'
        if state != 'running':
            stage = {'waiting_storage': '等待下载连接', 'between_packets': '退避 / 等待下一包',
                     'waiting_packet': '等待新包', 'preparing': '本地下载 / 解码',
                     'waiting_server': '等待调度连接',
                     'stopped': '已停止', 'process_exited': '标注进程已退出'}.get(state, '等待启动')
        elif not current and last_tool == 'ball_submit' and last_packet and not last_packet['reason']:
            stage = '已提交，等待下一包'
        workers.append({'id': folder.name, 'state': state, 'stage': stage,
                        'process_alive': process_alive, 'packet': current, 'last_packet': last_packet,
                        'iteration': status.get('iteration', 0), 'started_at': status.get('started_at'),
                        'updated_at': status.get('updated_at'), 'last_activity': activity,
                        'events': events, 'last_reason': status.get('reason') or next(
                            (p['reason'] for p in sorted(packets, key=lambda p: p['updated_at'], reverse=True) if p['reason']), '')})
    submissions = sorted(receipts.values(), key=lambda r: r.get('submitted_at') or 0, reverse=True)
    inventory = next((read_json(folder/'status.json').get('pool') for folder in sorted(out.glob('wangmin-codex61-p*'))
                      if read_json(folder/'status.json').get('pool')), {})
    return {'at': time.time(), 'pool_inventory': inventory, 'pool_name': out.name, 'model': pool.get('model', launch.get('model')),
            'state': pool.get('state', 'unknown') if enabled else 'stopped',
            'reason': pool.get('reason'), 'supervisor_alive': supervisor_alive,
            'quota': pool.get('weekly', {}), 'quota_updated_at': pool.get('updated_at'),
            'workers': workers, 'submissions': submissions,
            'summary': {'submitted_packets': len(submissions),
                        'submitted_frames': sum(r.get('n_targets') or 0 for r in submissions),
                        'active_labeled': sum(w['packet']['labeled'] for w in workers if w['packet']),
                        'active_total': sum(w['packet']['total'] for w in workers if w['packet']),
                        'active_workers': sum(w['state'] == 'running' for w in workers),
                        'working_workers': sum(w['state'] in ('running', 'preparing') for w in workers),
                        'released_attempts': released}}


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, out, **kwargs):
        self.out = out
        super().__init__(*args, **kwargs)

    def do_GET(self):
        route = self.path.split('?', 1)[0]
        if route == '/api/progress':
            body = json.dumps(snapshot(self.out), ensure_ascii=False).encode()
            content_type = 'application/json; charset=utf-8'
        elif route == '/':
            body = (ROOT/'scripts/label_progress_ui.html').read_bytes()
            content_type = 'text/html; charset=utf-8'
        elif route == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
            return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    out = args.out.resolve()
    if not (out/'launch.json').is_file():
        parser.error('--out must point to an existing annotation pool')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, out=out))
    print(f'Annotation progress: http://127.0.0.1:{args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
