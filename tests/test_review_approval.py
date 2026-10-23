"""Cloud review presses approve once; wacli keeps its playback contract."""

import contextlib
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from messagebox import cloud_setup
from messagebox.guided_reply import GuidedSession, OutboxStore, RecordingResult
from speaker_helpers import isolate_audio

with mock.patch.dict(sys.modules, {"gpiozero": types.SimpleNamespace(Button=object, LED=object)}):
    from messagebox import button_send as runtime


class ReviewApprovalTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)

    def playback(self, windows, *, ignore_first_s=.5, held=False, stalled=False):
        clock = [0.0]
        button = types.SimpleNamespace(is_pressed=held)
        player = mock.Mock(returncode=0)
        player.poll.side_effect = lambda: 0 if clock[0] >= 2 or player.terminate.called and not stalled else None
        if stalled:
            player.wait.side_effect = [subprocess.TimeoutExpired('aplay', .2), 0]
        def tick(_):
            clock[0] = round(clock[0] + .02, 6)
            button.is_pressed = any(start <= clock[0] < end for start, end in windows)
        def release():
            while button.is_pressed:
                tick(.02)
        with mock.patch.object(runtime, 'button', button, create=True), \
             mock.patch.object(runtime.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(runtime.time, 'sleep', side_effect=tick), \
             mock.patch.object(runtime, 'wait_for_stable_open', side_effect=release), \
             mock.patch.object(runtime, 'start_player', return_value=player), \
             mock.patch.object(runtime, 'approve_guided_press', return_value=True) as approve:
            result = runtime.play_audio_for_approval('review.wav', 'session', action='approve',
                                                     ignore_first_s=ignore_first_s)
        self.assertEqual(approve.call_count, int(result))
        self.assertEqual(player.terminate.call_count, int(result))
        self.assertEqual(player.kill.call_count, int(stalled))
        return result, clock[0]

    def test_fresh_review_press_interrupts_but_stop_early_held_and_bounce_do_not(self):
        for windows, held, expected in (([(.8, 2)], False, True),
                                        ([(0, .8)], True, False),
                                        ([(.2, .9)], False, False),
                                        ([(.2, .3), (.8, 2)], False, True),
                                        ([(.8, .84)], False, False),
                                        ([], False, False)):
            with self.subTest(windows=windows, held=held):
                result, elapsed = self.playback(windows, held=held)
                self.assertEqual(result, expected)
                if expected:
                    self.assertLess(elapsed, 1)

    def test_press_during_send_question_interrupts_without_review_dead_time(self):
        result, elapsed = self.playback([(.1, 2)], ignore_first_s=0)
        self.assertTrue(result)
        self.assertLess(elapsed, .3)

    def test_stalled_review_player_is_killed_before_approval_cue(self):
        self.assertTrue(self.playback([(.8, 2)], stalled=True)[0])

    def test_review_or_question_approval_queues_exact_recipient_without_second_wait(self):
        for setup_reply in (False, True):
            for during in ('review', 'question', 'timeout'):
                with self.subTest(setup_reply=setup_reply, during=during), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / 'recording.wav'
                    source.write_bytes(b'synthetic')
                    store = OutboxStore(root / 'outbox', transport='cloud')
                    guide = cloud_setup.SetupGuide()
                    guide.change('start')
                    guide.change('stage', stage='recording', scope='a' * 64)
                    io = runtime.PiGuidedIO('person@example.invalid', 'session', 60)
                    io.setup_reply = setup_reply
                    with contextlib.ExitStack() as stack:
                        stack.enter_context(mock.patch.object(runtime, 'transport_mode', return_value='cloud'))
                        stack.enter_context(mock.patch.object(runtime, 'buttonbox_voice', return_value=True))
                        stack.enter_context(mock.patch.object(runtime, 'play_moment'))
                        stack.enter_context(mock.patch.object(runtime.sound_pack, 'next_send_prompt', return_value=Path('ask-send.wav')))
                        stack.enter_context(mock.patch.object(runtime, '_sending_feedback', None))
                        wake = stack.enter_context(mock.patch.object(runtime, 'sender_wake'))
                        stack.enter_context(mock.patch.object(runtime, 'log_event'))
                        stack.enter_context(mock.patch.object(io, 'play_deleted'))
                        stack.enter_context(mock.patch.object(io, 'record', return_value=RecordingResult(str(source), 2, True)))
                        play = stack.enter_context(mock.patch.object(runtime, 'play_audio_for_approval',
                            side_effect=[during == 'review', during == 'question']))
                        wait = stack.enter_context(mock.patch.object(runtime, 'wait_for_approval', return_value=False))
                        events = []
                        def event(kind, **data):
                            events.append(kind)
                            runtime.recording_session_event(None, kind, **data)
                        result = GuidedSession(io, cloud_setup.ReplyOutbox(store, guide) if setup_reply else store,
                            event).run(recipient=io.recipient,
                                flow_kind='reply', deleted_cue_path='deleted.wav', account_scope='a' * 64)
                        if during != 'timeout':
                            self.assertEqual(runtime._sending_feedback['message_id'], store.jobs()[0].message_id)
                            wake.set.assert_called_once()
                        else:
                            self.assertIsNone(runtime._sending_feedback)
                    self.assertEqual(result, 'deleted' if during == 'timeout' else 'approved')
                    self.assertEqual(play.call_count, 1 if during == 'review' else 2)
                    self.assertEqual(wait.call_count, int(during == 'timeout'))
                    self.assertFalse(source.exists())
                    if during != 'timeout':
                        self.assertEqual(store.jobs()[0].recipient, io.recipient)
                        self.assertEqual(events.count('guided_approved'), 1)
                        if setup_reply:
                            self.assertEqual(guide.load()['stage'], 'await_listened')

    def test_wacli_review_still_discards_playback_presses_and_waits_for_approval(self):
        with mock.patch.object(runtime, 'transport_mode', return_value='wacli'), \
             mock.patch.object(runtime, 'play_audio_ordinary') as ordinary, \
             mock.patch.object(runtime, 'play_audio_for_approval') as interruptible, \
             mock.patch.object(runtime, 'wait_for_approval', return_value=True) as wait:
            io = runtime.PiGuidedIO('person@example.invalid', 'session', 60)
            io.play_review('review.wav')
            self.assertTrue(io.wait_for_approval(10))
        ordinary.assert_called_once_with('review.wav')
        interruptible.assert_not_called()
        wait.assert_called_once_with(10, 'session')


if __name__ == '__main__':
    unittest.main()
