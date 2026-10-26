"""Transport handles empty stock and expired local leases before starting a model."""
import importlib.util
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('transport', ROOT/'scripts/prepare_codex_packet.py')
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


def test_expired_lease_and_empty_pool():
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)/'worker-1'
        packet = folder/'work'/'old'
        packet.mkdir(parents=True)
        session = packet/'session.json'
        session.write_text(json.dumps({'lease_id': 'expired', 'done': False}))
        (packet/'labels.json').write_text('{"1":{"visible":false}}')

        class API:
            def heartbeat(self, *args):
                raise transport.ApiError('released', status=409)
            def get(self, *args):
                return {'n_available': 44, 'n_schedulable': 0, 'want_more': 10}

        class Agent:
            def __init__(self, *args, **kwargs):
                assert json.loads(session.read_text())['done'] is True
                assert kwargs['method']['model'] == 'gpt-6.1-sol'
            def claim(self):
                return {'packet_id': None, 'leased': False}

        with patch.object(transport, 'ServerClient', return_value=API()), patch.object(
                transport, 'AgentSession', Agent), patch('sys.argv', ['transport', '--folder', str(folder)]):
            assert transport.main() == 2
        assert json.loads((folder/'status.json').read_text())['state'] == 'waiting_packet'
        assert (packet/'labels.json').read_text() == '{"1":{"visible":false}}'


def test_ready_packet_handoff():
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)/'worker-2'
        (folder/'work').mkdir(parents=True)

        class Agent:
            def claim(self):
                return {'packet_id': 'new'}
            def job_state(self, *args):
                return {'state': 'ready', 'ready': True}

        with patch.object(transport, 'ServerClient'), patch.object(transport, 'AgentSession', return_value=Agent()), patch(
                'sys.argv', ['transport', '--folder', str(folder)]):
            assert transport.main() == 0


if __name__ == '__main__':
    test_expired_lease_and_empty_pool()
    test_ready_packet_handoff()
    print('ok codex_transport')
