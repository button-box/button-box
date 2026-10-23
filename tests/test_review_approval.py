"""Base review discards playback presses and waits for a fresh approval."""

import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from messagebox import cloud_quiet
from messagebox.guided_reply import GuidedSession, OutboxStore, RecordingResult
from speaker_helpers import isolate_audio

with mock.patch.dict(sys.modules, {"gpiozero": types.SimpleNamespace(Button=object, LED=object)}):
    from messagebox import button_send as runtime


class ReviewApprovalTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)

    def session(self, *, transport='cloud', setup_reply=False, fresh_press=True, fail_question=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'recording.wav'
            original = b'original synthetic recording'
            source.write_bytes(original)
            store = OutboxStore(root / 'outbox', transport=transport)
            io = runtime.PiGuidedIO('person@example.invalid', 'session', 60)
            io.setup_reply = setup_reply
            clock = [0.0]
            release_at = [0.0]
            approval_started = [None]
            button = types.SimpleNamespace(is_pressed=False)
            clips = []
            events = []
            def tick(seconds):
                clock[0] = round(clock[0] + seconds, 6)
                button.is_pressed = clock[0] < release_at[0] or (
                    fresh_press and approval_started[0] is not None
                    and .1 <= round(clock[0] - approval_started[0], 6) < .3)
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse(store.jobs())
            def play(path, **kwargs):
                self.assertFalse(store.jobs())
                clips.append(Path(path))
                clock[0] += 1
                if fail_question and Path(path) != source:
                    raise subprocess.CalledProcessError(1, 'aplay', stderr=b'Input/output error')
                # Carry a playback press past the end of each clip.
                release_at[0] = clock[0] + .2
                button.is_pressed = True
            approval_wait = runtime.wait_for_approval
            def wait(timeout, session_id):
                self.assertFalse(button.is_pressed)
                self.assertGreaterEqual(clock[0], release_at[0] + runtime.SETTLE_OPEN_S)
                approval_started[0] = clock[0]
                return approval_wait(timeout, session_id)
            with mock.patch.object(runtime, 'time', types.SimpleNamespace(monotonic=lambda: clock[0], sleep=tick)), \
                 mock.patch.object(runtime, '_guided_active', True), \
                 mock.patch.object(runtime, 'button', button, create=True), \
                 mock.patch.object(runtime, 'transport_mode', return_value=transport), \
                 mock.patch.object(runtime, 'buttonbox_voice', return_value=transport == 'cloud'), \
                 mock.patch.object(runtime, 'play_sound', side_effect=play), \
                 mock.patch.object(runtime, 'play_moment'), \
                 mock.patch.object(runtime, 'start_player'), \
                 mock.patch.object(runtime, 'acknowledge_guided_press'), \
                 mock.patch.object(runtime, 'log_event'), \
                 mock.patch.object(runtime, 'wait_for_approval', side_effect=wait) as approval, \
                 mock.patch.object(runtime.sound_pack, 'next_send_prompt', return_value=Path('ask-send.wav')), \
                 mock.patch.object(io, 'record', return_value=RecordingResult(str(source), 2, True)), \
                 mock.patch.object(io, 'play_deleted'):
                result = GuidedSession(io, store, lambda kind, **data: events.append(kind)).run(
                    recipient=io.recipient, flow_kind='reply', deleted_cue_path='deleted.wav',
                    account_scope='a' * 64 if transport == 'cloud' else None)
                approval.assert_called_once_with(10.0, 'session')
            self.assertEqual(clips[0], source)
            self.assertEqual(len(clips), 2 if transport == 'cloud' else 1)
            self.assertEqual(result, 'approved' if fresh_press else 'deleted')
            self.assertFalse(source.exists())
            self.assertEqual(events.count('guided_approved'), int(fresh_press))
            jobs = store.jobs()
            self.assertEqual(len(jobs), int(fresh_press))
            if jobs:
                self.assertEqual(jobs[0].audio_path.read_bytes(), original)
                self.assertEqual(jobs[0].recipient, io.recipient)

    def test_wacli_review_still_discards_playback_presses_and_waits_for_approval(self):
        self.session(transport='wacli')

    def test_playback_presses_are_discarded_before_fresh_approval_queues_exact_recording(self):
        for setup_reply in (False, True):
            with self.subTest(setup_reply=setup_reply):
                self.session(setup_reply=setup_reply)

    def test_playback_hold_without_fresh_approval_deletes_the_recording(self):
        for setup_reply in (False, True):
            with self.subTest(setup_reply=setup_reply):
                self.session(setup_reply=setup_reply, fresh_press=False)

    def test_failed_optional_setup_question_keeps_approval_wait_and_original_reply(self):
        self.session(setup_reply=True, fail_question=True)
        with self.assertRaises(subprocess.CalledProcessError):
            self.session(fail_question=True)

    def test_busy_review_retry_retains_ordinary_playback_timeout(self):
        busy = mock.Mock(returncode=1)
        busy.communicate.return_value = (None, b'Device or resource busy')
        busy.poll.return_value = 1
        success = mock.Mock(returncode=0)
        success.communicate.return_value = (None, b'')
        success.poll.return_value = 0
        with mock.patch.object(runtime, 'transport_mode', return_value='cloud'), \
             mock.patch.object(runtime, 'buttonbox_voice', return_value=False), \
             mock.patch.object(runtime, 'button', types.SimpleNamespace(is_pressed=False), create=True), \
             mock.patch.object(cloud_quiet, 'start_player', side_effect=[busy, success]) as player, \
             mock.patch.object(cloud_quiet.time, 'sleep') as sleep:
            runtime.PiGuidedIO('person@example.invalid', 'session', 60).play_review('review.wav')
        self.assertEqual(player.call_count, 2)
        busy.communicate.assert_called_once_with(timeout=600)
        success.communicate.assert_called_once_with(timeout=600)
        sleep.assert_called_once_with(.15)


if __name__ == '__main__':
    unittest.main()
