"""Cloud review presses approve once; wacli keeps its playback contract."""

import contextlib
import fcntl
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from messagebox import cloud_quiet, cloud_setup, maintenance, speaker
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

    def timed_session(self, windows, *, during='review', duration=1, setup_reply=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'recording.wav'
            original = b'original synthetic recording'
            source.write_bytes(original)
            store = OutboxStore(root / 'outbox', transport='cloud')
            io = runtime.PiGuidedIO('person@example.invalid', 'session', 60)
            io.setup_reply = setup_reply
            clock = [0.0]
            origin = [None]
            button = types.SimpleNamespace(is_pressed=False)
            approvals = []
            events = []
            def tick(seconds):
                clock[0] = round(clock[0] + seconds, 6)
                button.is_pressed = origin[0] is not None and any(
                    start <= round(clock[0] - origin[0], 6) < end for start, end in windows)
                if origin[0] is None or clock[0] - origin[0] < windows[-1][0] + runtime.CONFIRM_PRESS_S:
                    self.assertFalse(store.jobs())
            def release():
                opened = None
                while opened is None or clock[0] - opened < runtime.SETTLE_OPEN_S:
                    tick(.005)
                    opened = None if button.is_pressed else clock[0] if opened is None else opened
            def start(command, **kwargs):
                path = Path(command[-1])
                if path == runtime.sound_pack.cue_path('press'):
                    return mock.Mock(poll=lambda: 0)
                if (path == source) == (during == 'review'):
                    origin[0] = clock[0]
                end = clock[0] + duration
                player = mock.Mock(returncode=0)
                player.poll.side_effect = lambda: 0 if clock[0] >= end or player.terminate.called else None
                return player
            def log(kind, **fields):
                if kind == 'guided_press' and fields['action'] == 'approve':
                    approvals.append(clock[0] - origin[0])
            with mock.patch.object(runtime, 'time', types.SimpleNamespace(monotonic=lambda: clock[0], sleep=tick)), \
                 mock.patch.object(runtime, 'POLL_S', .005), \
                 mock.patch.object(runtime, 'button', button, create=True), \
                 mock.patch.object(runtime, 'transport_mode', return_value='cloud'), \
                 mock.patch.object(runtime, 'buttonbox_voice', return_value=True), \
                 mock.patch.object(runtime, 'start_player', side_effect=start), \
                 mock.patch.object(runtime, 'wait_for_stable_open', side_effect=release), \
                 mock.patch.object(runtime, 'play_moment'), \
                 mock.patch.object(runtime, 'log_event', side_effect=log), \
                 mock.patch.object(runtime, 'wait_for_approval', return_value=False), \
                 mock.patch.object(runtime.sound_pack, 'next_send_prompt', return_value=Path('ask-send.wav')), \
                 mock.patch.object(io, 'record', return_value=RecordingResult(str(source), 2, True)), \
                 mock.patch.object(io, 'play_deleted'):
                result = GuidedSession(io, store, lambda kind, **data: events.append(kind)).run(
                    recipient=io.recipient, flow_kind='reply', deleted_cue_path='deleted.wav', account_scope='a' * 64)
            jobs = store.jobs()
            self.assertEqual(len(jobs), len(approvals))
            self.assertEqual(events.count('guided_approved'), len(approvals))
            if jobs:
                self.assertEqual(jobs[0].audio_path.read_bytes(), original)
                self.assertEqual(jobs[0].recipient, io.recipient)
            return result, approvals

    def test_ignored_hold_open_bounce_requires_stable_release_and_new_press(self):
        ignored = [(.2, .6), (.605, 2.1)]
        self.assertEqual(self.timed_session(ignored, duration=2), ('deleted', []))
        result, approvals = self.timed_session([(.2, .6), (.605, 1), (1.6, 1.84)], duration=2)
        self.assertEqual(result, 'approved')
        self.assertEqual(len(approvals), 1)
        self.assertGreaterEqual(approvals[0], 1.68)

    def test_fresh_press_spanning_review_or_question_completion_approves_exact_recording_once(self):
        for during in ('review', 'question'):
            for setup_reply in (False, True):
                with self.subTest(during=during, setup_reply=setup_reply):
                    result, approvals = self.timed_session([(.96, 1.2)], during=during, setup_reply=setup_reply)
                    self.assertEqual(result, 'approved')
                    self.assertEqual(len(approvals), 1)
                    self.assertAlmostEqual(approvals[0], 1.04)

    def test_stalled_review_times_out_reaps_player_and_releases_reservations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / 'maintenance.lock'
            lock.touch()
            clock = [0.0]
            polls = []
            process = mock.Mock(returncode=None)
            process.poll.side_effect = lambda: process.returncode
            def wait(**kwargs):
                if process.returncode is None:
                    raise subprocess.TimeoutExpired('aplay', kwargs['timeout'])
                return process.returncode
            process.wait.side_effect = wait
            process.kill.side_effect = lambda: setattr(process, 'returncode', -9)
            def exclusive():
                with lock.open() as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def tick(seconds):
                self.assertTrue(speaker.is_busy())
                with self.assertRaises(BlockingIOError):
                    exclusive()
                polls.append(clock[0])
                clock[0] += 200.5
                self.assertLessEqual(clock[0], 602, 'playback polled beyond its deadline')
            with mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT': 'cloud'}), \
                 mock.patch.object(maintenance, 'LOCK', lock), \
                 mock.patch.object(maintenance, 'INHIBIT', root / 'inhibit'), \
                 mock.patch.object(cloud_quiet, 'silence_store', cloud_quiet.SilenceStore(root / 'silence.json')), \
                 mock.patch.object(cloud_quiet.subprocess, 'Popen', return_value=process), \
                 mock.patch.object(runtime, 'time', types.SimpleNamespace(monotonic=lambda: clock[0], sleep=tick)), \
                 mock.patch.object(runtime, 'button', types.SimpleNamespace(is_pressed=False), create=True), \
                 mock.patch.object(runtime, 'approve_guided_press') as approve:
                with self.assertRaises(subprocess.TimeoutExpired):
                    with maintenance.interaction() as allowed:
                        self.assertTrue(allowed)
                        runtime.play_audio_for_approval('review.wav', action='approve', ignore_first_s=.5)
                approve.assert_not_called()
                process.terminate.assert_called_once()
                process.kill.assert_called_once()
                self.assertEqual(process.wait.call_count, 2)
                self.assertEqual(len(polls), 3)
                self.assertFalse(speaker.is_busy())
                exclusive()


if __name__ == '__main__':
    unittest.main()
