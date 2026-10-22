"""Review regressions: rollback, malformed input, priority, and local cache scope."""
import hashlib
import importlib.util
import json
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from messagebox import cloud_setup, cloud_runtime
from messagebox.settings import SettingsStore, defaults
from messagebox.cloud_voice import VoiceCache
import test_cloud_runtime as runtime_tests
from test_cloud_runtime import NOW, OP, button_send
from test_cloud_voice import wav

FIXTURES = Path(__file__).parent / 'fixtures/pi-v0.3.12'


class VoiceReviewTests(unittest.TestCase):
    setUp = runtime_tests.CloudRuntimeTests.setUp

    def greeting(self):
        self.client.audio = wav()
        return {'operation_id': OP, 'kind': 'setup_greeting', 'expires_at': NOW + 60,
                'payload': {'voice_pack': 'buttonbox', 'clip_id': 'a' * 64, 'text_hash': 'b' * 64,
                            'sha256': hashlib.sha256(self.client.audio).hexdigest(),
                            'content_type': 'audio/wav', 'media_url': 'https://button.box/cloud-api/v1/device/media/greeting'}}

    def test_stalled_greeting_never_blocks_a_real_note_or_another_poll(self):
        self.runtime.heartbeat()
        greeting = dict(self.greeting(), sequence=1, created_at=NOW)
        entered, release = threading.Event(), threading.Event()
        def media(url, *, limit, deadline=None):
            if url.endswith('/greeting'):
                self.assertIsNotNone(deadline)
                entered.set()
                release.wait(5)
                return wav()
            return b'OggS real note'
        self.client.media = media
        try:
            self.client.items = [greeting]
            started = time.monotonic()
            self.runtime.poll_once()
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(entered.wait(1))
            job = self.runtime._greeting_download
            real = runtime_tests.CloudRuntimeTests.audio_item(self)
            real['operation_id'] = 'real-note-operation'
            real['sequence'] = 2
            real['payload']['sha256'] = hashlib.sha256(b'OggS real note').hexdigest()
            self.client.items = [greeting, real]
            started = time.monotonic()
            self.runtime.poll_once()
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(list(self.runtime.queue_dir.glob('*.wav')))
            self.assertTrue(job['thread'].is_alive())
            # Total deadline wins even while the synthetic network stays stuck.
            with mock.patch.object(cloud_runtime.time, 'monotonic', return_value=job['deadline'] + 1):
                self.runtime._finish_previews()
            self.runtime.flush_acks()
            self.assertEqual(next(a['state'] for a in self.client.acks if a['operation_id'] == OP), 'rejected')
            self.assertFalse(list(self.runtime.optional_audio_requests.directory.glob('*.json')))
            self.runtime.poll_once()
            self.assertIs(self.runtime._greeting_download, job)  # No extra stuck workers.
        finally:
            release.set()
            if self.runtime._greeting_download:
                self.runtime._greeting_download['thread'].join(2)
        self.runtime._finish_previews()
        self.assertFalse(list(self.runtime.optional_audio_requests.directory.glob('*.json')))

    def test_real_notes_run_before_a_greeting_in_the_same_poll(self):
        self.runtime.heartbeat()
        greeting = dict(self.greeting(), sequence=1, created_at=NOW)
        real = runtime_tests.CloudRuntimeTests.audio_item(self)
        real['operation_id'], real['sequence'] = 'real-note-operation', 2
        self.client.items = [greeting, real]
        with mock.patch.object(self.runtime, '_command') as command:
            self.runtime.poll_once()
        self.assertEqual([call.args[0]['kind'] for call in command.call_args_list], ['audio', 'setup_greeting'])

    def test_greeting_queued_under_an_old_voice_is_skipped(self):
        self.runtime.heartbeat()
        self.runtime._command(self.greeting(), NOW)
        runtime_tests.finish_greeting(self.runtime)
        request_path = next(self.runtime.optional_audio_requests.directory.glob('*.json'))
        self.assertEqual(json.loads(request_path.read_text())['voice_pack'], 'buttonbox')
        guide = cloud_setup.SetupGuide()
        guide.change('start')
        guide.advance({'scan'}, 'names')
        settings = {**defaults({}), 'voice_pack': 'jessica'}
        with mock.patch.multiple(button_send, cloud_audio_requests=self.runtime.audio_requests,
            cloud_optional_audio_requests=self.runtime.optional_audio_requests, _recording=False, _guided_active=False,
            button=types.SimpleNamespace(is_pressed=False), create=True), \
             mock.patch.object(button_send, 'transport_mode', return_value='cloud'), \
             mock.patch.object(button_send.cloud_runtime, 'account_scope', return_value='a' * 64), \
             mock.patch.object(button_send, 'caregiver_settings', return_value=settings), \
             mock.patch.object(button_send, 'quiet_hours', return_value=False), \
             mock.patch.object(button_send, 'queued', return_value=[]), \
             mock.patch.object(button_send, 'play_optional_greeting') as play:
            self.assertFalse(button_send.maybe_play_cloud_sound())
        play.assert_not_called()
        self.assertFalse(guide.load().get('greeting_spoken'))
        self.assertEqual(self.runtime.optional_audio_requests.outcome('setup_greeting:' + OP), 'rejected')

    def test_v0312_reader_finishes_legacy_audio_with_new_greeting_pending(self):
        self.runtime.heartbeat()
        self.runtime._command(self.greeting(), NOW)
        runtime_tests.finish_greeting(self.runtime)
        self.runtime._command({'operation_id': 'voice-preview', 'kind': 'voice_preview',
            'expires_at': NOW + 60, 'payload': {'voice_pack': 'buttonbox'}}, NOW)
        self.runtime.audio_requests.enqueue_for('legacy', 'preview', 'a' * 64, 60, ringtone_id='hello_piano')
        self.runtime.state['pending_previews']['legacy-command'] = 'legacy'
        self.runtime._save()
        spec = importlib.util.spec_from_file_location('v0312_audio_requests', FIXTURES / 'audio_requests.py')
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        reader = old.AudioRequests(self.runtime.audio_requests.directory, clock=lambda: NOW,
                                   monotonic=lambda: self.mono[0], boot_id="test-boot")
        namespace = {"json": json, "CloudRuntimeError": RuntimeError}
        exec((FIXTURES / 'finish_previews.py.txt').read_text(), namespace)
        exec((FIXTURES / 'runtime_state.py.txt').read_text(), namespace)
        previous = types.SimpleNamespace(state=namespace['_load'](self.runtime),
            audio_requests=reader, _ack=mock.Mock(), _save=mock.Mock())
        # This executes the actual old strict reader, not a current-code model.
        namespace['_finish_previews'](previous)
        self.assertEqual(len(previous.state['pending_optional_audio']), 2)
        with reader.owner():
            request = reader.claim_next('a' * 64)
            self.assertEqual(request['kind'], 'preview')
            reader.finish(request, 'played')
        namespace['_finish_previews'](previous)
        previous._ack.assert_called_once_with('legacy-command', 'applied')
        self.assertEqual(previous.state['pending_previews'], {})
        self.assertEqual(len(list(self.runtime.optional_audio_requests.directory.glob('*.json'))), 2)

    def test_malformed_greeting_urls_are_rejected_then_next_command_runs(self):
        self.runtime.heartbeat()
        for value in ({'invalid': True}, [], None, 7, '', 'x' * 2049):
            with self.subTest(value=type(value).__name__):
                item = self.greeting()
                item['operation_id'] = 'invalid-' + str(type(value).__name__) + str(len(str(value)))
                item['payload']['media_url'] = value
                self.client.items = [dict(item, sequence=1, created_at=NOW)]
                with mock.patch.object(self.client, 'media', side_effect=AssertionError('must not download')):
                    self.runtime.poll_once()
                self.assertEqual(self.client.acks[-1]['state'], 'rejected')
                self.assertEqual(self.client.acks[-1]['error_code'], 'setup_greeting_invalid')
        item = runtime_tests.CloudRuntimeTests.settings_command(self, master_volume_percent=39)
        self.runtime._command(item, NOW)
        self.assertEqual(self.runtime.settings.load()[0]['master_volume_percent'], 39)

    def test_cloud_explicit_jessica_replays_at_original_revision(self):
        self.runtime.heartbeat()
        store = SettingsStore(self.runtime.settings.path, environ={'MSGBOX_TRANSPORT': 'cloud'})
        document = {**defaults({}), 'revision': 7, 'voice_pack': 'jessica'}
        store.path.write_text(json.dumps(document))
        self.runtime.settings = store
        self.assertEqual(store.load(), (document, False))
        self.runtime._command({'operation_id': OP, 'kind': 'settings', 'expires_at': NOW + 60,
            'payload': {'expected_revision': 6, 'desired_revision': 7, 'settings': document}}, NOW)
        self.assertEqual(store.load(), (document, False))

    def test_invalid_guide_shapes_disable_without_restarting_setup(self):
        guide = cloud_setup.SetupGuide(self.root / 'guide.json')
        for state in ({'stage': [], 'spoken': []}, {'stage': {}, 'spoken': []}, [], None,
                      {'stage': 'names', 'spoken': [{}]}, {'stage': 'names', 'spoken': None},
                      {'stage': 'names', 'spoken': [], 'local_id': []},
                      {'stage': 'names', 'spoken': [], 'recipient': {}},
                      {'stage': 'names', 'spoken': [], 'greeting_spoken': []}):
            with self.subTest(state=state):
                guide.path.write_text(json.dumps(state))
                with self.assertLogs('messagebox.cloud_setup', level='WARNING') as logs:
                    self.assertEqual(guide.load(), {'stage': 'done', 'spoken': []})
                self.assertIn('guide disabled', logs.output[0])
                with self.assertLogs('messagebox.cloud_setup', level='WARNING'):
                    self.assertEqual(guide.change('start')['stage'], 'done')
                self.assertFalse(guide.speak(mock.Mock()))

    def test_cache_is_scoped_by_voice_pack_and_text_hash(self):
        cache = VoiceCache(self.root / 'cache')
        payload = self.greeting()['payload']
        audio = self.client.audio
        variants = [audio[:-2] + index.to_bytes(2, 'little') for index in range(3)]
        self.client.media = mock.Mock(side_effect=variants)
        paths = []
        for (pack, text), data in zip([('buttonbox', 'b' * 64), ('jessica', 'b' * 64),
                                       ('jessica', 'c' * 64)], variants):
            payload = {**payload, 'sha256': hashlib.sha256(data).hexdigest()}
            paths.append(cache.download(self.client, payload, voice_pack=pack, text_hash=text))
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual([Path(path).read_bytes() for path in paths], variants)
        self.assertEqual(self.client.media.call_count, 3)
        self.assertEqual(cache.download(self.client, payload, voice_pack='jessica', text_hash='c' * 64), paths[-1])
        self.assertEqual(self.client.media.call_count, 3)

    def test_idle_greeting_plays_through_button_owner_then_acknowledges(self):
        self.runtime.heartbeat()
        self.runtime._command(self.greeting(), NOW)
        runtime_tests.finish_greeting(self.runtime)
        guide = cloud_setup.SetupGuide()
        guide.change('start')
        guide.advance({'scan'}, 'names')
        player = mock.Mock(returncode=0)
        player.poll.return_value = 0
        with mock.patch.multiple(button_send, cloud_audio_requests=self.runtime.audio_requests,
            cloud_optional_audio_requests=self.runtime.optional_audio_requests, _recording=False, _guided_active=False,
            button=types.SimpleNamespace(is_pressed=False), create=True), \
             mock.patch.object(button_send, 'transport_mode', return_value='cloud'), \
             mock.patch.object(button_send.cloud_runtime, 'account_scope', return_value='a' * 64), \
             mock.patch.object(button_send.cloud_runtime, 'STATE_FILE', self.runtime.state_path), \
             mock.patch.object(button_send, 'quiet_hours', return_value=False), \
             mock.patch.object(button_send, 'queued', return_value=[]), \
             mock.patch.object(button_send, 'start_player', return_value=player) as start:
            self.assertTrue(button_send.maybe_play_cloud_sound())
        start.assert_called_once()
        self.assertTrue(guide.load()['greeting_spoken'])
        self.runtime._finish_previews()
        self.runtime.flush_acks()
        self.assertEqual(self.client.acks[-1]['state'], 'applied')

    def test_waiting_real_note_skips_greeting_without_consuming_a_press(self):
        self.runtime.heartbeat()
        self.runtime._command(self.greeting(), NOW)
        runtime_tests.finish_greeting(self.runtime)
        guide = cloud_setup.SetupGuide()
        guide.change('start')
        guide.advance({'scan'}, 'names')
        with mock.patch.multiple(button_send, cloud_audio_requests=self.runtime.audio_requests,
            cloud_optional_audio_requests=self.runtime.optional_audio_requests, _recording=False, _guided_active=False,
            button=types.SimpleNamespace(is_pressed=False), create=True), \
             mock.patch.object(button_send, 'transport_mode', return_value='cloud'), \
             mock.patch.object(button_send.cloud_runtime, 'account_scope', return_value='a' * 64), \
             mock.patch.object(button_send, 'queued', return_value=['real-note']), \
             mock.patch.object(button_send, 'play_optional_greeting') as play, \
             mock.patch.object(button_send, 'wait_for_stable_open') as discard:
            self.assertFalse(button_send.maybe_play_cloud_sound())
        play.assert_not_called()
        discard.assert_not_called()
        self.assertEqual(self.runtime.optional_audio_requests.outcome('setup_greeting:' + OP), 'rejected')

    def test_new_message_or_press_interrupts_greeting_and_keeps_button_level(self):
        for incoming, pressed in ((['real-note'], False), ([], True)):
            button = types.SimpleNamespace(is_pressed=False)
            player = mock.Mock()
            player.poll.return_value = None
            def arrived():
                button.is_pressed = pressed
                return incoming
            with mock.patch.object(button_send, 'queued', side_effect=[[], arrived()]), \
                 mock.patch.object(button_send, 'button', button, create=True), \
                 mock.patch.object(button_send, 'quiet_hours', return_value=False), \
                 mock.patch.object(button_send, 'start_player', return_value=player), \
                 mock.patch.object(button_send, 'wait_for_stable_open') as discard:
                # Press arrives after the initial idle check.
                button.is_pressed = False
                def poll():
                    button.is_pressed = pressed
                    return None
                player.poll.side_effect = poll
                self.assertFalse(button_send.play_optional_greeting('synthetic.wav'))
            player.terminate.assert_called_once()
            discard.assert_not_called()
            self.assertEqual(button.is_pressed, pressed)


if __name__ == '__main__':
    unittest.main()
