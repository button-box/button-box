"""Cloud delivery contract and speaker/voice rollback regressions, without devices."""
import copy
import fcntl
import importlib.util
import json
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from messagebox import cloud_runtime, speaker
from messagebox.settings import SettingsStore, VOICE_PACKS, defaults
import test_cloud_runtime as runtime_tests
from test_cloud_runtime import NOW, OP, button_send
from test_cloud_voice import wav

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = ROOT / 'tests/fixtures/pi-v0.3.12'


class GreetingContractServer(runtime_tests.FakeClient):
    """Executable Cloud requirement, not a test of the deployed Cloud service."""
    def __init__(self):
        super().__init__()
        self.current_capabilities = {}
        self.delivered = []

    def heartbeat(self, state):
        # Replace the prior snapshot. Merging would retain a removed capability.
        self.current_capabilities = copy.deepcopy(state['capabilities'])
        if self.current_capabilities.get('guided_setup_v1') is not True:
            self.items = [item for item in self.items if item['kind'] != 'setup_greeting']
        return super().heartbeat(state)

    def greeting(self, item):
        if self.current_capabilities.get('guided_setup_v1') is not True:
            return False
        self.items.append(item)
        return True

    def inbox(self, cursor):
        result = super().inbox(cursor)
        # Recheck at delivery, including commands queued under a previous build.
        result['items'] = [item for item in result['items'] if item['kind'] != 'setup_greeting'
                           or self.current_capabilities.get('guided_setup_v1') is True]
        self.delivered.append(copy.deepcopy(result['items']))
        return result


class VoiceRoundTwoTests(unittest.TestCase):
    setUp = runtime_tests.CloudRuntimeTests.setUp

    def test_current_heartbeat_contract_cancels_redelivery_after_pre_ack_rollback(self):
        server = GreetingContractServer()
        self.runtime.client = server
        step1 = json.loads((ROOT / 'docs/fixtures/voice-onboarding-step1-04d53cd.json').read_text())
        legacy_capabilities = step1['capabilities']
        self.assertNotIn('guided_setup_v1', legacy_capabilities)
        audio = wav()
        import hashlib
        server.audio = audio
        item = {'operation_id': OP, 'kind': 'setup_greeting', 'sequence': 1,
                'created_at': NOW, 'expires_at': NOW + 3600,
                'payload': {'voice_pack': 'buttonbox', 'clip_id': 'a' * 64, 'text_hash': 'b' * 64,
                            'sha256': hashlib.sha256(audio).hexdigest(), 'content_type': 'audio/wav',
                            'media_url': 'https://button.box/cloud-api/v1/device/media/greeting'}}
        with mock.patch.object(cloud_runtime, 'capabilities', return_value=legacy_capabilities):
            self.runtime.heartbeat()
        self.assertFalse(server.greeting(item))
        self.assertEqual(server.inbox(0)['items'], [])

        self.runtime.heartbeat()
        self.assertIs(server.current_capabilities['guided_setup_v1'], True)
        self.assertTrue(server.greeting(item))
        class PowerLoss(BaseException):
            pass
        with mock.patch.object(self.runtime, '_ack', side_effect=PowerLoss):
            with self.assertRaises(PowerLoss):
                self.runtime._command(item, NOW)
        self.assertIn(OP, json.loads(self.runtime.state_path.read_text())['pending_optional_audio'])
        self.assertEqual(server.acks, [])
        self.assertEqual(server.items, [item])  # The server never received the ACK.

        namespace = dict(vars(cloud_runtime))
        for method in ('run', 'poll_once'):
            exec((HISTORICAL / (method + '.py.txt')).read_text(), namespace)
        stop = mock.Mock()
        stop.is_set.side_effect = [False, True]  # One startup iteration, even if the contract fails.
        wake = types.SimpleNamespace(is_set=lambda: False, clear=mock.Mock(), wait=mock.Mock())
        namespace['threading'] = types.SimpleNamespace(Event=lambda: wake)
        received = []
        order = []
        def heartbeat():
            order.append('heartbeat')
            server.heartbeat({'capabilities': legacy_capabilities,
                              'settings': self.runtime.settings.load()[0], 'applied_revision': 0})
        def command(command, now):
            received.append(command['kind'])
            self.assertIn(command['kind'], step1['command_kinds'])
            stop.set()
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
        self.assertEqual(received, ['settings'])
        self.assertNotIn('guided_setup_v1', server.current_capabilities)
        self.assertTrue(all(i['kind'] != 'setup_greeting' for i in server.delivered[-1]))
        self.runtime.heartbeat()  # Upgrading again must not revive the cancelled command.
        self.assertTrue(all(i['kind'] != 'setup_greeting' for i in server.inbox(0)['items']))

    def test_greeting_checks_current_capability_at_delivery_not_only_enqueue(self):
        server = GreetingContractServer()
        server.current_capabilities = {'guided_setup_v1': True}
        self.assertTrue(server.greeting({'kind': 'setup_greeting'}))
        for unsupported in ({}, {'guided_setup_v1': False}, {'guided_setup_v1': 1}):
            server.current_capabilities = unsupported
            self.assertEqual(server.inbox(0)['items'], [])

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

    def test_cloud_pending_settings_without_voice_key_keep_the_missing_key_default(self):
        document = {**defaults({'TZ': 'UTC'}), 'revision': 7}
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
