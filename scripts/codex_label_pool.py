#!/usr/bin/env python3
"""Three isolated Codex annotators, bounded by the current weekly quota window."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'gpt-6.1-sol'
STOP = threading.Event()


def write_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def read_limits():
    proc = subprocess.Popen(['codex', 'app-server'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    lines = queue.Queue()
    def reader():
        for line in proc.stdout:
            lines.put(line)
    threading.Thread(target=reader, daemon=True).start()
    def send(obj):
        proc.stdin.write(json.dumps(obj) + '\n')
        proc.stdin.flush()
    try:
        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
              'params': {'clientInfo': {'name': 'ball-label-quota', 'version': '1.0'}}})
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            msg = json.loads(lines.get(timeout=max(.1, deadline-time.monotonic())))
            if msg.get('id') == 1:
                if 'error' in msg:
                    raise RuntimeError('app-server initialization failed')
                send({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})
                send({'jsonrpc': '2.0', 'id': 2, 'method': 'account/rateLimits/read', 'params': {}})
            if msg.get('id') == 2:
                result = msg['result']
                bucket = result.get('rateLimitsByLimitId', {}).get('codex') or result['rateLimits']
                weekly = [w for w in [bucket.get('primary'), bucket.get('secondary')]
                          if w and w.get('windowDurationMins') == 10080]
                if not weekly:
                    raise RuntimeError('weekly quota unavailable')
                return {'weekly': weekly[0], 'bucket': bucket}
        raise RuntimeError('quota probe timeout')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def report(env, event, message):
    try:
        subprocess.run(['python3', str(ROOT/'skills/agentboard-report/scripts/report.py'), event, message],
                       cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
    except Exception:
        pass


def prompt(worker):
    return f'''你是足球GT标注员，worker={worker}。完成恰好一个包后结束此进程，不领第二包。
先读 skills/ball-label/SKILL.md，严格按新版直接坐标路线执行。方法已由MCP启动参数固定，禁止更换身份绕过调度。
ball_lease -> status ready -> plan -> overview + scan确认比赛球身份 -> 逐帧coord原图 -> label/absent -> review -> submit。
必须亲自查看每个目标帧RGB图片，使用工具读取返回的本地图片。主视野320–480像素带球员上下文，单帧2倍最近邻。
只标比赛球，静止球也标；备用球不标。逐帧观察球心，拖影取视觉中心，部分遮挡逐帧标记。
scan/增强/外推只辅助放窗，禁止把插值、检测候选或未观察坐标当GT。不要调用其他模型代替你观察。
找不到先扩大搜索范围并看前后动作；未看帧保持missing，不能批量填absent。定位失败按skill结构化释放，不硬交残包。
全部100目标帧标满后，ball_review全部叠加页、absent专项页与轨迹逐页实际看完；修改后重跑并复核，再携有效review_id提交source=ai审核。
以真实submission_id和本地receipt.json为完成依据。缺帧和全不可见不能force提交。
如果领不到包，本次直接结束，外部调度负责等待，不要反复领包。若额度或认证失败，保留本地进度并结束，禁止使用额度重置、购买或更换模型。
下载准备超过10分钟仍无ready或发生连接错误，按download_error释放并结束；不要无限轮询，外部调度会退避。
不得改动代码、skill、配置、服务端或已有其他worker产物。允许写自己的MCP工作目录与本次日志。
遵守AGENTS的AgentBoard开始/结束/中断上报，环境已隔离service key与state目录，不设置AGENTBOARD_RUN_KEY。
最终只用简短JSON报告packet_id、outcome(submitted/released/no_packet/unfinished)、submission_id、reason。'''


def command(worker, work, final):
    mcp_args = ['-m', 'client.mcp_server', '--server', 'https://ballgt.yinger650.com',
                '--worker', worker, '--work', str(work), '--harness', 'codex',
                '--harness-version', '0.163.0-alpha.11.1', '--model', MODEL,
                '--model-version', 'unknown', '--xy-unit', 'px']
    return ['codex', 'exec', '--ignore-user-config', '-m', MODEL,
            '-c', 'model_reasoning_effort="medium"', '-c', 'approval_policy="never"',
            '-c', 'features.multi_agent=false', '-s', 'danger-full-access', '-C', str(ROOT),
            '-c', 'mcp_servers.ball-label.command='+json.dumps(str(ROOT/'venv/bin/python')),
            '-c', 'mcp_servers.ball-label.args='+json.dumps(mcp_args),
            '-c', 'mcp_servers.ball-label.env='+ '{PYTHONPATH='+json.dumps(str(ROOT))+'}',
            '--json', '-o', str(final), prompt(worker)]


def hydrate_cache(work):
    """Reuse immutable decoded media only; never inherit another annotator's labels."""
    cache = Path.home()/'.cache/ball_ai/work'
    for session in work.glob('*/session.json'):
        dest = session.parent
        if (dest/'prepare.json').exists():
            continue
        source = cache/dest.name
        try:
            prepared = json.loads((source/'prepare.json').read_text())
            if prepared.get('state') != 'ready' or not prepared.get('frames'):
                continue
            if not all((source/'raw'/f'{fi}.jpg').is_file() for fi in prepared['frames']):
                continue
            for name in ('raw', 'enh', 'thumb'):
                if (source/name).is_dir():
                    shutil.copytree(source/name, dest/name, dirs_exist_ok=True)
            for name in ('meta.json', 'scan_time.jpg', 'scan_dog.jpg', 'clip.mp4', 'dog.mp4'):
                if (source/name).is_file():
                    shutil.copy2(source/name, dest/name)
            write_json(dest/'prepare.json', prepared)
        except (OSError, ValueError):
            continue


def outcomes(work, since):
    result = []
    for path in work.glob('*/session.json'):
        if path.stat().st_mtime < since-2:
            continue
        sess = json.loads(path.read_text())
        receipt = path.parent/'receipt.json'
        if receipt.exists() and receipt.stat().st_mtime >= since-2:
            result.append({'packet_id': path.parent.name, 'outcome': 'submitted',
                           'receipt': json.loads(receipt.read_text())})
        elif sess.get('released_reason'):
            result.append({'packet_id': path.parent.name, 'outcome': 'released',
                           'reason': sess['released_reason']})
        elif not sess.get('done'):
            result.append({'packet_id': path.parent.name, 'outcome': 'unfinished'})
    return result


def storage_available():
    # A signed read-only HEAD tests the actual download route without model calls.
    probe = '''
from pathlib import Path
import boto3
from botocore.config import Config
from aoss_store import load_credentials
c=load_credentials()
p=next((Path.home()/'.cache/ball_ai/work').glob('*/meta.json'), None)
client=boto3.client('s3',endpoint_url=c.endpoint,aws_access_key_id=c.access_key,aws_secret_access_key=c.secret_key,config=Config(connect_timeout=4,read_timeout=4,retries={'max_attempts':0}))
try:
    if p:
        client.head_object(Bucket=c.bucket,Key=c.prefix.rstrip('/')+'/packets/'+p.parent.name+'/meta.json')
    else:
        client.head_bucket(Bucket=c.bucket)
except Exception:
    raise SystemExit(1)
'''
    try:
        return subprocess.run([str(ROOT/'venv/bin/python'), '-c', probe], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=12).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def has_ready_session(work):
    for path in work.glob('*/session.json'):
        try:
            if not json.loads(path.read_text()).get('done') and json.loads(
                    (path.parent/'prepare.json').read_text()).get('state') == 'ready':
                return True
        except (OSError, ValueError):
            continue
    return False


def weekly_window_ended(original_reset, weekly, now):
    # The service can revise resetsAt by a second; that is not a new week.
    return now >= original_reset or abs(weekly['resetsAt']-original_reset) > 300


def lane(index, out, launch_ok):
    worker = f'wangmin-codex61-p{index}'
    folder = out/worker
    work = folder/'work'
    work.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, AGENTBOARD_PROVIDER='codex', AGENTBOARD_SERVICE_KEY=worker,
               AGENTBOARD_STATE_DIR=str(folder/'board-state'))
    env.pop('AGENTBOARD_RUN_KEY', None)
    Path(env['AGENTBOARD_STATE_DIR']).mkdir(exist_ok=True)
    iteration = max([int(p.stem) for p in folder.glob('*.jsonl') if p.stem.isdigit()] or [0])
    failures = 0
    while not STOP.is_set():
        if not launch_ok.is_set():
            STOP.wait(10)
            continue
        hydrate_cache(work)
        if not has_ready_session(work) and not storage_available():
            write_json(folder/'status.json', {'state': 'waiting_storage', 'iteration': iteration,
                       'model': MODEL, 'reason': 'download_route_unavailable',
                       'updated_at': time.time(), 'next_probe_seconds': 60})
            STOP.wait(60)
            continue
        if STOP.is_set() or not launch_ok.is_set():
            continue
        # Claim, validate old leases and wait for RGB data without spending model tokens.
        write_json(folder/'status.json', {'state': 'preparing', 'model': MODEL,
                   'iteration': iteration, 'updated_at': time.time()})
        with (folder/'transport.log').open('a') as log:
            prep = subprocess.Popen([str(ROOT/'venv/bin/python'),
                    str(ROOT/'scripts/prepare_codex_packet.py'), '--folder', str(folder)],
                    cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
            while prep.poll() is None and not STOP.wait(3):
                hydrate_cache(work)
            if prep.poll() is None:
                os.killpg(prep.pid, signal.SIGTERM)
                try:
                    prep.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(prep.pid, signal.SIGKILL)
                    prep.wait()
        if STOP.is_set():
            break
        if prep.returncode != 0:
            STOP.wait(60)
            continue
        if not launch_ok.is_set():
            continue
        iteration += 1
        stamp = f'{iteration:05d}'
        since = time.time()
        hydrate_cache(work)
        with (folder/f'{stamp}.jsonl').open('w') as stdout, (folder/f'{stamp}.stderr').open('w') as stderr:
            proc = subprocess.Popen(command(worker, work, folder/f'{stamp}.final.json'), cwd=ROOT,
                                    env=env, stdout=stdout, stderr=stderr, start_new_session=True)
            write_json(folder/'status.json', {'state': 'running', 'pid': proc.pid,
                       'iteration': iteration, 'model': MODEL, 'started_at': since, 'work': str(work)})
            last_progress = time.monotonic()
            while proc.poll() is None and not STOP.wait(5):
                hydrate_cache(work)
                if time.monotonic()-last_progress > 180:
                    report(env, 'progress', f'{worker} 第{iteration}次标注运行中，进度保存在独立work目录')
                    last_progress = time.monotonic()
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                report(env, 'interrupt', '标注池停止，标签已落盘；未完成租约由服务端超时回收，禁止伪提交')
        records = outcomes(work, since)
        state = {'state': 'stopped' if STOP.is_set() else 'between_packets', 'iteration': iteration,
                 'exit_code': proc.returncode, 'outcomes': records, 'updated_at': time.time()}
        write_json(folder/'status.json', state)
        with (folder/'runs.jsonl').open('a') as f:
            f.write(json.dumps(state, ensure_ascii=False)+'\n')
        infrastructure = any(r.get('outcome') == 'released' and any(
            code in r.get('reason', '') for code in ('download_error', 'decode_error')) for r in records)
        successes = [r for r in records if r['outcome'] in ('submitted', 'released')] if not infrastructure else []
        failures = 0 if successes else failures+1
        # Pool-empty or interrupted jobs back off; never consume quota with busy retries.
        # The server cools the failed packet, not all work on this lane.
        STOP.wait(60 if infrastructure else (5 if successes else min(1800, 60*2**min(failures, 5))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    lock = (out/'pool.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    limits = read_limits()
    if args.check:
        print(json.dumps({'model': MODEL, 'weekly': limits['weekly'],
                          'workers': 3, 'command_ready': len(command('check', out/'work', out/'final')) > 0}))
        return
    window_file = out/'weekly-window.json'
    if not window_file.exists():
        write_json(window_file, {'resetsAt': limits['weekly']['resetsAt']})
    reset = json.loads(window_file.read_text())['resetsAt']
    env = dict(os.environ, AGENTBOARD_PROVIDER='codex', AGENTBOARD_SERVICE_KEY='ball-codex61-pool',
               AGENTBOARD_STATE_DIR=str(out/'board-state'))
    env.pop('AGENTBOARD_RUN_KEY', None)
    Path(env['AGENTBOARD_STATE_DIR']).mkdir(exist_ok=True)
    report(env, 'start', '3路gpt-6.1-sol标注池，持续到当前周额度耗尽，不重置额度')
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    ready = threading.Event()
    if limits['weekly']['usedPercent'] < 100 and time.time() < reset:
        ready.set()
    threads = [threading.Thread(target=lane, args=(i, out, ready), daemon=True) for i in range(1, 4)]
    for thread in threads:
        thread.start()
    failure_since = None
    reason = 'manual_stop'
    try:
        while not STOP.is_set():
            try:
                limits = read_limits()
                failure_since = None
                weekly = limits['weekly']
                if weekly_window_ended(reset, weekly, time.time()):
                    reason = 'weekly_window_ended'
                    break
                if weekly['usedPercent'] >= 100:
                    reason = 'weekly_quota_exhausted'
                    break
                if shutil.disk_usage(out).free < 10*1024**3:
                    reason = 'disk_space_below_10GB'
                    break
                ready.set()
                lane_states = []
                for i in range(1, 4):
                    try:
                        lane_states.append(json.loads((out/f'wangmin-codex61-p{i}'/'status.json').read_text())['state'])
                    except (OSError, ValueError, KeyError):
                        lane_states.append('starting')
                state = 'waiting_storage' if lane_states == ['waiting_storage']*3 else (
                    'waiting_packet' if lane_states == ['waiting_packet']*3 else 'running')
                write_json(out/'status.json', {'state': state, 'pid': os.getpid(), 'lane_states': lane_states,
                           'model': MODEL, 'workers': 3, 'weekly': weekly, 'updated_at': time.time()})
                report(env, 'progress', f'3路标注池{state}；周已用{weekly["usedPercent"]}%，网络等待不启动模型')
            except Exception as exc:
                ready.clear()
                failure_since = failure_since or time.time()
                write_json(out/'quota-error.json', {'error_type': type(exc).__name__, 'at': time.time()})
                if time.time()-failure_since > 600:
                    reason = 'quota_unavailable_10min'
                    break
            if (out/'STOP').exists():
                reason = 'stop_file'
                break
            STOP.wait(60)
    finally:
        STOP.set()
        ready.clear()
        for thread in threads:
            thread.join(timeout=40)
        for i in range(1, 4):
            path = out/f'wangmin-codex61-p{i}'/'status.json'
            try:
                state = json.loads(path.read_text())
                if state.get('state') != 'stopped':
                    state.update(state='stopped', previous_state=state.get('state'),
                                 reason=reason, updated_at=time.time())
                    write_json(path, state)
            except (OSError, ValueError):
                pass
        write_json(out/'status.json', {'state': 'stopped', 'reason': reason, 'model': MODEL,
                   'weekly': limits['weekly'], 'updated_at': time.time()})
        event = 'succeed' if reason in ('weekly_quota_exhausted', 'weekly_window_ended') else 'interrupt'
        report(env, event, '标注池停止：'+reason+'；已提交结果及未完成标签保留')


if __name__ == '__main__':
    main()
