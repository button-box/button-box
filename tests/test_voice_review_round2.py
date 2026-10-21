"""Cloud delivery contract and speaker/voice rollback regressions, without devices."""
import copy
import fcntl
import importlib.util
import io
import json
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import threading
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from messagebox import cloud_runtime, speaker
from messagebox.cloud_device import CloudDeviceClient
from messagebox.settings import SettingsStore, VOICE_PACKS, defaults
import test_cloud_runtime as runtime_tests
from test_cloud_runtime import NOW, OP, button_send
from test_cloud_voice import wav

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = ROOT / 'tests/fixtures/pi-v0.3.12'


class GreetingContractServer(runtime_tests.FakeClient):
    """Test-only Cloud contract. Each offer commits before the HTTP response."""
    def __init__(self, database, *, online_seconds=120):
        super().__init__()
        self.database = database
        self.online_seconds = online_seconds  # Test value; Cloud owns BOX_ONLINE_SECONDS.
        self.delivered = []
        with sqlite3.connect(database) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS heartbeat (id INTEGER PRIMARY KEY, capabilities TEXT, received_at INTEGER);
                CREATE TABLE IF NOT EXISTS greetings (
                    operation_id TEXT PRIMARY KEY, command TEXT, state TEXT,
                    offered_at INTEGER, expires_at INTEGER, terminal_at INTEGER);
            ''')

    def _maintain(self, db):
        latest = db.execute('SELECT capabilities, received_at FROM heartbeat WHERE id=1').fetchone()
        eligible = (latest is not None
                    and 0 <= self.server_time - latest[1] <= self.online_seconds
                    and json.loads(latest[0]).get('guided_setup_v1') is True)
        if not eligible:
            db.execute("UPDATE greetings SET state='cancelled', terminal_at=? WHERE state IN ('pending', 'offered')",
                       (self.server_time,))
        db.execute("UPDATE greetings SET state='expired', terminal_at=? "
                   "WHERE state IN ('pending', 'offered') AND expires_at<=?",
                   (self.server_time, self.server_time))
        db.execute('DELETE FROM greetings WHERE terminal_at<=?', (self.server_time - 30 * 86400,))
        return eligible

    def heartbeat(self, state):
        with sqlite3.connect(self.database) as db:
            db.execute('BEGIN IMMEDIATE')
            # Replace the snapshot; merging could retain a removed capability.
            db.execute('INSERT OR REPLACE INTO heartbeat VALUES (1, ?, ?)',
                       (json.dumps(state['capabilities']), self.server_time))
            self._maintain(db)
        return super().heartbeat(state)

    def greeting(self, item):
        with sqlite3.connect(self.database) as db:
            db.execute('BEGIN IMMEDIATE')
            command = copy.deepcopy(item)
            command['expires_at'] = min(item['expires_at'], item['created_at'] + 600)
            command['payload'] = {key: value for key, value in item['payload'].items() if key in
                                  {'voice_pack', 'clip_id', 'text_hash', 'sha256', 'content_type', 'media_url'}}
            db.execute("INSERT OR IGNORE INTO greetings VALUES (?, ?, 'pending', NULL, ?, NULL)",
                       (item['operation_id'], json.dumps(command), command['expires_at']))
            self._maintain(db)
        return True

    def inbox(self, cursor, *, features=None):
        with sqlite3.connect(self.database) as db:
            db.execute('BEGIN IMMEDIATE')
            self._maintain(db)
            # Compare and change in one statement. Commit before returning any offer.
            offered = []
            if isinstance(features, str) and 'guided_setup_v1' in features.split(','):
                offered = db.execute("UPDATE greetings SET state='offered', offered_at=? "
                                     "WHERE state='pending' RETURNING command", (self.server_time,)).fetchall()
        result = super().inbox(cursor)
        result['items'] = list(result['items']) + [json.loads(row[0]) for row in offered]
        self.delivered.append(copy.deepcopy(result['items']))
        return result

    def ack(self, ack):
        # Pi wire names are unchanged. Unknown operations/states have no effect.
        state = {'received': 'received', 'applied': 'played',
                 'rejected': 'failed', 'expired': 'expired'}.get(ack['state'])
        with sqlite3.connect(self.database) as db:
            db.execute('BEGIN IMMEDIATE')
            self._maintain(db)
            if state is not None:
                db.execute("UPDATE greetings SET state=?, terminal_at=? "
                           "WHERE operation_id=? AND state IN ('offered', 'received')",
                           (state, self.server_time, ack['operation_id']))
        return super().ack(ack)

    def row(self, operation_id=OP):
        with sqlite3.connect(self.database) as db:
            return db.execute('SELECT state, offered_at, expires_at FROM greetings WHERE operation_id=?',
                              (operation_id,)).fetchone()

class VoiceRoundTwoTests(unittest.TestCase):
    setUp = runtime_tests.CloudRuntimeTests.setUp

    def server(self, name='greetings.sqlite'):
        return GreetingContractServer(self.root / name)

    def greeting(self):
        audio = wav()
        return {'operation_id': OP, 'kind': 'setup_greeting', 'sequence': 1,
                'created_at': NOW, 'expires_at': NOW + 600,
                'payload': {'voice_pack': 'buttonbox', 'clip_id': 'a' * 64, 'text_hash': 'b' * 64,
                            'sha256': hashlib.sha256(audio).hexdigest(), 'content_type': 'audio/wav',
                            'media_url': 'https://button.box/cloud-api/v1/device/media/greeting'}}

    def legacy_once(self, server, *, heartbeat_fails=False):
        step1 = json.loads((ROOT / 'docs/fixtures/voice-onboarding-step1-04d53cd.json').read_text())
        namespace = dict(vars(cloud_runtime))
        for method in ('run', 'poll_once'):
            exec((HISTORICAL / (method + '.py.txt')).read_text(), namespace)
        stop = mock.Mock()
        stop.is_set.side_effect = [False, True]
        wake = types.SimpleNamespace(is_set=lambda: False, clear=mock.Mock(), wait=mock.Mock())
        namespace['threading'] = types.SimpleNamespace(Event=lambda: wake)
        received, order, errors = [], [], []
        namespace['_log_failure'] = lambda context, exc, *args: errors.append((context, str(exc)))
        def heartbeat():
            order.append('heartbeat')
            if heartbeat_fails:
                raise cloud_runtime.CloudDeviceError('synthetic unavailable heartbeat')
            server.heartbeat({'capabilities': step1['capabilities'],
                              'settings': self.runtime.settings.load()[0], 'applied_revision': 0})
        def command(command, now):
            received.append(command['kind'])
            self.assertIn(command['kind'], step1['command_kinds'])
        legacy = types.SimpleNamespace(state=copy.deepcopy(self.runtime.state), client=server,
            monotonic=lambda: 0, heartbeat=heartbeat, _snapshot=self.runtime._snapshot,
            _command=command, _save=mock.Mock(), flush_acks=mock.Mock(),
            _finish_listened=mock.Mock(), _finish_previews=mock.Mock(), _finish_nfc=mock.Mock(),
            _finish_settings=mock.Mock(), _replay_completed=lambda operation: False,
            expire_local=mock.Mock(), recover_outbox=mock.Mock())
        def poll():
            order.append('inbox')
            namespace['poll_once'](legacy)
        legacy.poll_once = poll
        server.items.append({'operation_id': 'ordinary1234567890123456', 'sequence': 2, 'kind': 'settings',
            'created_at': NOW, 'expires_at': NOW + 60, 'payload': {}})
        with mock.patch.dict('os.environ', {'MSGBOX_CLOUD_EVENTS': '0'}):
            namespace['run'](legacy, stop=stop)
        self.assertEqual(order, ['heartbeat', 'inbox'])
        return received, errors

    def assert_pre_ack_rollback(self, *, heartbeat_fails):
        server = self.server()
        self.runtime.client = server
        step1 = json.loads((ROOT / 'docs/fixtures/voice-onboarding-step1-04d53cd.json').read_text())
        with mock.patch.object(cloud_runtime, 'capabilities', return_value=step1['capabilities']):
            self.runtime.heartbeat()
        item = self.greeting()
        never_capable = {**item, 'operation_id': 'nevercapable12345678901234'}
        self.assertTrue(server.greeting(never_capable))
        self.assertEqual(server.inbox(0, features='guided_setup_v1')['items'], [])
        self.assertEqual(server.row(never_capable['operation_id'])[0], 'cancelled')
        self.runtime.heartbeat()
        server.audio = wav()
        self.assertTrue(server.greeting(item))
        offered = server.inbox(0, features='guided_setup_v1')['items']
        self.assertEqual(offered, [item])
        self.assertEqual(server.row(), ('offered', NOW, NOW + 600))
        class PowerLoss(BaseException):
            pass
        with mock.patch.object(self.runtime, '_ack', side_effect=PowerLoss):
            with self.assertRaises(PowerLoss):
                self.runtime._command(offered[0], NOW)
        self.assertIn(OP, json.loads(self.runtime.state_path.read_text())['pending_optional_audio'])
        self.assertEqual(server.acks, [])
        # A still-fresh old capability cannot cause a second offer, even after restart.
        self.assertEqual(self.server().inbox(0, features='guided_setup_v1')['items'], [])
        received, errors = self.legacy_once(server, heartbeat_fails=heartbeat_fails)
        self.assertEqual(received, ['settings'])
        self.assertEqual(errors, [('heartbeat', 'synthetic unavailable heartbeat')] if heartbeat_fails else [])
        self.assertEqual(server.row()[0], 'offered' if heartbeat_fails else 'cancelled')
        self.runtime.heartbeat()  # Upgrading again cannot revive the row.
        self.assertTrue(all(i['kind'] != 'setup_greeting' for i in server.inbox(0, features='guided_setup_v1')['items']))
        delivered = [i for response in server.delivered for i in response if i['kind'] == 'setup_greeting']
        self.assertEqual(delivered, [item])

    def test_current_heartbeat_contract_cancels_redelivery_after_pre_ack_rollback(self):
        self.assert_pre_ack_rollback(heartbeat_fails=False)

    def test_at_most_once_survives_failed_rollback_heartbeat_and_lost_ack(self):
        self.assert_pre_ack_rollback(heartbeat_fails=True)

    def test_old_runtime_crashes_if_cloud_breaks_the_no_redelivery_rule(self):
        self.runtime.heartbeat()
        server = runtime_tests.FakeClient()  # Deliberately broken delivery, not the contract server.
        server.items = [self.greeting()]
        received, errors = self.legacy_once(server, heartbeat_fails=True)
        self.assertEqual(received, [])
        self.assertIn(('poll', 'cloud inbox item is invalid'), errors)

    def test_current_poll_feature_is_required_even_with_fresh_guided_heartbeat(self):
        for index, features in enumerate(('guided_setup_v1', 'future_v2,guided_setup_v1')):
            with self.subTest(features=features):
                server = self.server(f'poll-features-{index}.sqlite')
                self.runtime.client = server
                self.runtime.heartbeat()
                server.greeting(self.greeting())
                for unsupported in (None, '', 'future_v2', 'not_guided_setup_v1'):
                    self.assertEqual(server.inbox(0, features=unsupported)['items'], [])
                    self.assertEqual(server.row(), ('pending', None, NOW + 600))
                self.assertEqual(server.inbox(0, features=features)['items'], [self.greeting()])
                self.assertEqual(server.row()[0], 'offered')
                self.assertEqual(server.inbox(0, features=features)['items'], [])

    def test_first_offer_cannot_reach_old_runtime_after_failed_rollback_heartbeat(self):
        server = self.server()
        self.runtime.client = server
        self.runtime.heartbeat()  # Cloud still has the new runtime's fresh capability.
        server.greeting(self.greeting())
        received, errors = self.legacy_once(server, heartbeat_fails=True)
        self.assertEqual(received, ['settings'])
        self.assertEqual(errors, [('heartbeat', 'synthetic unavailable heartbeat')])
        self.assertEqual(server.row(), ('pending', None, NOW + 600))
        self.assertTrue(all(item['kind'] != 'setup_greeting' for item in server.delivered[-1]))
        # Use the real request code for the later compatible poll, without networking.
        def opener(request, *, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            response = server.inbox(int(query['cursor'][0]), features=query.get('features', [None])[0])
            stream = io.BytesIO(json.dumps(response).encode())
            stream.status = 200
            return stream
        current = CloudDeviceClient('https://example.invalid/cloud-api/v1',
            {'device_id': 'synthetic-device-001', 'credential': 'x' * 43}, opener=opener)
        self.assertEqual([item['kind'] for item in current.inbox(0)['items']],
                         ['settings', 'setup_greeting'])
        self.assertEqual([item['kind'] for item in current.inbox(0)['items']], ['settings'])

    def test_atomic_offer_is_at_most_once_with_concurrent_polls_and_lost_response(self):
        server = self.server()
        self.runtime.client = server
        self.runtime.heartbeat()
        self.assertTrue(server.greeting(self.greeting()))
        barrier = threading.Barrier(8)
        def poll(_):
            barrier.wait(timeout=5)
            return server.inbox(0, features='guided_setup_v1')['items']
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(poll, range(8)))
        self.assertEqual([item for response in responses for item in response], [self.greeting()])
        self.assertEqual(server.row(), ('offered', NOW, NOW + 600))
        # Discard the response and all clients. Persistent offered state still blocks retry.
        restarted = self.server()
        restarted.greeting(self.greeting())  # A repeated enqueue cannot reset the single row.
        self.assertEqual(restarted.inbox(0, features='guided_setup_v1')['items'], [])
        restarted.server_time = NOW + 599
        restarted.heartbeat({'capabilities': {'guided_setup_v1': True}, 'settings': {}, 'applied_revision': 0})
        self.assertEqual(restarted.row()[0], 'offered')
        self.assertEqual(restarted.inbox(0, features='guided_setup_v1')['items'], [])
        restarted.server_time = NOW + 600
        restarted.heartbeat({'capabilities': {'guided_setup_v1': True}, 'settings': {}, 'applied_revision': 0})
        self.assertEqual(restarted.row()[0], 'expired')
        self.assertEqual(restarted.inbox(0, features='guided_setup_v1')['items'], [])

    def test_greeting_checks_current_capability_and_freshness_and_cancels_pending_or_offered(self):
        for offered in (False, True):
            for index, unsupported in enumerate(({}, {'guided_setup_v1': False}, {'guided_setup_v1': 1}, 'stale', 'missing')):
                with self.subTest(offered=offered, unsupported=unsupported):
                    server = self.server(f'gate-{offered}-{index}.sqlite')
                    self.runtime.client = server
                    self.runtime.heartbeat()
                    server.greeting(self.greeting())
                    if offered:
                        self.assertEqual(server.inbox(0, features='guided_setup_v1')['items'], [self.greeting()])
                    if unsupported == 'stale':
                        server.server_time += server.online_seconds + 1
                    elif unsupported == 'missing':
                        with sqlite3.connect(server.database) as db:
                            db.execute('DELETE FROM heartbeat')
                    else:
                        server.heartbeat({'capabilities': unsupported, 'settings': {}, 'applied_revision': 0})
                    self.assertEqual(server.inbox(0, features='guided_setup_v1')['items'], [])
                    self.assertEqual(server.row()[0], 'cancelled')
                    self.runtime.heartbeat()
                    self.assertEqual(server.inbox(0, features='guided_setup_v1')['items'], [])

    def test_applied_wire_ack_maps_to_played_and_rejects_extra_names(self):
        self.assert_ack_mapping('played', 'played')

    def test_rejected_wire_ack_maps_to_failed_and_terminal_retention(self):
        self.assert_ack_mapping('rejected', 'failed')

    def test_expired_ack_mapping_and_terminal_retention(self):
        self.assert_ack_mapping('expired', 'expired')

    def assert_ack_mapping(self, outcome, expected):
        server = self.server(outcome + '.sqlite')
        self.runtime.client = server
        self.runtime.heartbeat()
        server.audio = wav()
        item = self.greeting()
        item['expires_at'] = NOW + 3600
        item['payload']['text'] = 'synthetic greeting text must not be stored'
        self.assertTrue(server.greeting(item))
        offered = server.inbox(0, features='guided_setup_v1')['items'][0]
        self.assertEqual(offered['expires_at'], NOW + 600)
        for unknown in ('unknown', 'played', 'failed'):
            server.ack({'operation_id': OP, 'state': unknown})
            self.assertEqual(server.row()[0], 'offered')
        server.ack({'operation_id': 'unknown-operation', 'state': 'played'})
        self.assertEqual(server.row()[0], 'offered')
        self.runtime._command(offered, NOW)
        self.runtime.flush_acks()
        self.assertEqual(server.row()[0], 'received')
        for unknown in ('played', 'failed'):
            server.ack({'operation_id': OP, 'state': unknown})
            self.assertEqual(server.row()[0], 'received')
        with self.runtime.optional_audio_requests.owner():
            request = self.runtime.optional_audio_requests.claim_next('a' * 64)
            self.runtime.optional_audio_requests.finish(request, outcome)
        self.runtime._finish_previews()
        self.runtime.flush_acks()
        self.assertEqual(server.row()[0], expected)
        server.ack({'operation_id': OP, 'state': 'received'})  # Late ACK cannot regress final state.
        self.assertEqual(server.row()[0], expected)
        with sqlite3.connect(server.database) as db:
            command = json.loads(db.execute('SELECT command FROM greetings').fetchone()[0])
            self.assertNotIn('text', command['payload'])
            self.assertIn('clip_id', command['payload'])
        server.server_time = NOW + 30 * 86400 - 1
        server.inbox(0, features='guided_setup_v1')
        self.assertIsNotNone(server.row())
        server.server_time += 1
        server.inbox(0, features='guided_setup_v1')
        self.assertIsNone(server.row())

    def test_busy_speaker_skips_greeting_before_a_press_or_note_can_be_trapped(self):
        launch = mock.Mock()
        result = []
        with mock.patch.object(button_send, 'button', types.SimpleNamespace(is_pressed=False), create=True), \
             mock.patch.object(button_send, 'queued', return_value=[]), \
             mock.patch.object(button_send, 'quiet_hours', return_value=False), \
             mock.patch.object(button_send, 'start_player',
                 side_effect=lambda args, **kw: speaker.start_player(args, popen=launch, **kw)), \
             mock.patch.object(button_send, 'wait_for_stable_open') as discard:
            with speaker.LOCK_FILE.open('a+b') as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                worker = threading.Thread(target=lambda: result.append(button_send.play_optional_greeting('synthetic.wav')))
                worker.start()
                try:
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(result, [False])
                    self.assertTrue(speaker.is_busy())
                    launch.assert_not_called()
                    discard.assert_not_called()
                finally:
                    fcntl.flock(held, fcntl.LOCK_UN)
                    worker.join(2)

    def test_press_or_note_after_lock_acquisition_prevents_launch_without_discard(self):
        lock_operation = speaker.fcntl.flock
        for interrupt in ('press', 'note'):
            button = types.SimpleNamespace(is_pressed=False)
            notes = []
            launch = mock.Mock()
            def flock(lock, operation):
                result = lock_operation(lock, operation)
                if operation & fcntl.LOCK_EX:
                    button.is_pressed = interrupt == 'press'
                    if interrupt == 'note':
                        notes.append('real-note')
                return result
            with mock.patch.object(speaker.fcntl, 'flock', side_effect=flock), \
                 mock.patch.object(button_send, 'button', button, create=True), \
                 mock.patch.object(button_send, 'queued', side_effect=lambda: notes), \
                 mock.patch.object(button_send, 'quiet_hours', return_value=False), \
                 mock.patch.object(button_send, 'start_player',
                     side_effect=lambda args, **kw: speaker.start_player(args, popen=launch, **kw)), \
                 mock.patch.object(button_send, 'wait_for_stable_open') as discard:
                self.assertFalse(button_send.play_optional_greeting('synthetic.wav'))
                launch.assert_not_called()
                discard.assert_not_called()
                self.assertEqual(button.is_pressed, interrupt == 'press')
                self.assertEqual(bool(notes), interrupt == 'note')
            self.assertFalse(speaker.is_busy())

    def test_cloud_pending_settings_without_voice_key_keep_the_applied_voice(self):
        for pack in ('jessica', 'buttonbox'):
            with self.subTest(pack=pack):
                document = {**defaults({'TZ': 'UTC'}), 'revision': 7, 'voice_pack': pack}
                self.runtime.state['pending_settings'][OP] = {key: value for key, value in document.items()
                                                             if key != 'voice_pack'}
                cloud_runtime.atomic_json(cloud_runtime.APPLIED_FILE, {'revision': 7, 'settings': document})
                self.runtime._finish_settings()
                self.runtime.flush_acks()
                self.assertEqual(self.runtime.client.acks[-1]['state'], 'applied')
                self.assertEqual(self.runtime.state['pending_settings'], {})

    def test_wacli_pre_upgrade_writer_jessica_and_ambiguous_saved_values_survive(self):
        spec = importlib.util.spec_from_file_location('v0312_settings', HISTORICAL / 'settings.py')
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        path = self.root / 'legacy-settings.json'
        writer = old.SettingsStore(path, environ={'TZ': 'UTC', 'MSGBOX_TRANSPORT': 'wacli'})
        original, _ = writer.load()
        for pack in ('pirate', 'jessica'):
            candidate = {key: value for key, value in original.items() if key not in {'version', 'revision'}}
            original = writer.update({**candidate, 'voice_pack': pack}, original['revision'])
        self.assertEqual(original['revision'], 2)
        self.assertFalse(path.with_suffix('.voice-selection.json').exists())
        before = path.read_bytes()
        self.assertEqual(SettingsStore(path, environ={'MSGBOX_TRANSPORT': 'wacli'}).load(), (original, False))
        self.assertEqual(path.read_bytes(), before)
        for pack in (*VOICE_PACKS, None, 'unknown', []):
            document = {**defaults({}), 'revision': 7, 'voice_pack': pack}
            path.write_text(json.dumps(document))
            before = path.read_bytes()
            loaded, warning = SettingsStore(path, environ={'MSGBOX_TRANSPORT': 'wacli'}).load()
            self.assertFalse(warning)
            self.assertEqual(loaded['revision'], 7)
            self.assertEqual(loaded['voice_pack'], pack if isinstance(pack, str) and pack in VOICE_PACKS else 'jessica')
            self.assertEqual(path.read_bytes(), before)
        del document['voice_pack']
        path.write_text(json.dumps(document))
        self.assertEqual(SettingsStore(path).load()[0]['voice_pack'], 'buttonbox')
        path.unlink()
        self.assertEqual(SettingsStore(path).load()[0]['voice_pack'], 'buttonbox')


if __name__ == '__main__':
    unittest.main()
