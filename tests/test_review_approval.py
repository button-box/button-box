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

    def test_fresh_review_press_interrupts_but_stop_early_held_and_bounce_do_not(self):
        for windows, held, expected in (([(.8, 2)], False, True),
                                        ([(0, .8)], True, False),
                                        ([(.2, .9)], False, False),
                                        ([(.2, .3), (.8, 2)], False, True),
                                        ([(.8, .84)], False, False),
                                        ([], False, False)):
            with self.subTest(windows=windows, held=held):
                result, approvals = self.timed_session(windows, duration=2, held=held)
                self.assertEqual(result, 'approved' if expected else 'deleted')
                self.assertEqual(len(approvals), int(expected))
                if expected:
                    self.assertLess(approvals[0], 1)

    def test_press_during_send_question_interrupts_without_review_dead_time(self):
        result, approvals = self.timed_session([(.1, 2)], during='question')
        self.assertEqual(result, 'approved')
        self.assertLess(approvals[0], .3)

    def test_stalled_review_player_is_killed_before_approval_cue(self):
        result, approvals = self.timed_session([(.8, 2)], stalled=True)
        self.assertEqual(result, 'approved')
        self.assertEqual(len(approvals), 1)

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
                        play = stack.enter_context(mock.patch.object(runtime, 'review_and_wait_for_approval', return_value=during != 'timeout'))
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
                    self.assertEqual(play.call_count, 1)
                    wait.assert_not_called()
                    self.assertFalse(source.exists())
                    if during != 'timeout':
                        self.assertEqual(store.jobs()[0].recipient, io.recipient)
                        self.assertEqual(events.count('guided_approved'), 1)
                        if setup_reply:
                            self.assertEqual(guide.load()['stage'], 'await_listened')

    def test_wacli_review_still_discards_playback_presses_and_waits_for_approval(self):
        with mock.patch.object(runtime, 'transport_mode', return_value='wacli'), \
             mock.patch.object(runtime, 'play_audio_ordinary') as ordinary, \
             mock.patch.object(runtime, 'review_and_wait_for_approval') as interruptible, \
             mock.patch.object(runtime, 'wait_for_approval', return_value=True) as wait:
            io = runtime.PiGuidedIO('person@example.invalid', 'session', 60)
            self.assertTrue(io.review_and_wait_for_approval('review.wav', 10))
        ordinary.assert_called_once_with('review.wav')
        interruptible.assert_not_called()
        wait.assert_called_once_with(10, 'session')

    def timed_session(self, windows, *, during='review', duration=1, setup_reply=False,
                      busy_first_review=False, release_delay=0, fail_question=False, held=False, stalled=False,
                      cleanup_pause=0):
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
            button = types.SimpleNamespace(is_pressed=held)
            approvals = []
            events = []
            review_attempts = []
            question_attempts = []
            players = []
            cleanup_pauses = []
            def tick(seconds):
                if fail_question:
                    self.assertEqual(source.read_bytes(), original)
                clock[0] = round(clock[0] + seconds, 6)
                button.is_pressed = origin[0] is not None and any(
                    start <= round(clock[0] - origin[0], 6) < end for start, end in windows)
                if origin[0] is None or not windows or clock[0] - origin[0] < windows[-1][0] + runtime.CONFIRM_PRESS_S:
                    self.assertFalse(store.jobs())
            def start(command, **kwargs):
                path = Path(command[-1])
                if path == runtime.sound_pack.cue_path('press'):
                    return mock.Mock(poll=lambda: 0)
                if path == source:
                    review_attempts.append(clock[0])
                    self.assertEqual(source.read_bytes(), original)
                    if busy_first_review and len(review_attempts) == 1:
                        if kwargs.get('stderr') is not None:
                            kwargs['stderr'].write(b'aplay: Device or resource busy\n')
                        return mock.Mock(returncode=1, poll=lambda: 1)
                if (path == source) == (during == 'review'):
                    origin[0] = clock[0]
                if path != source:
                    question_attempts.append(clock[0])
                    if fail_question:
                        kwargs['stderr'].write(b'aplay: Input/output error\n')
                        return mock.Mock(returncode=1, poll=lambda: 1)
                end = clock[0] + duration
                player = mock.Mock(returncode=0)
                if stalled and path == source:
                    player.poll.side_effect = lambda: None
                    player.wait.side_effect = [subprocess.TimeoutExpired('aplay', .2), 0]
                else:
                    def poll():
                        if clock[0] >= end or player.terminate.called:
                            # A scheduling pause after the final open sample, while
                            # the exited player releases its speaker reservation.
                            if cleanup_pause and not player.terminate.called and path != source and not cleanup_pauses:
                                cleanup_pauses.append(clock[0])
                                tick(cleanup_pause)
                            return 0
                        return None
                    player.poll.side_effect = poll
                if release_delay:
                    player.reservation_released = types.SimpleNamespace(wait=lambda timeout: tick(release_delay))
                players.append(player)
                return player
            def log(kind, **fields):
                if kind == 'guided_press' and fields['action'] == 'approve':
                    approvals.append(clock[0] - origin[0])
            with mock.patch.object(runtime, 'time', types.SimpleNamespace(monotonic=lambda: clock[0], sleep=tick)), \
                 mock.patch.object(runtime, 'POLL_S', .005), \
                 mock.patch.object(runtime, '_guided_active', True), \
                 mock.patch.object(runtime, 'button', button, create=True), \
                 mock.patch.object(runtime, 'transport_mode', return_value='cloud'), \
                 mock.patch.object(runtime, 'buttonbox_voice', return_value=True), \
                 mock.patch.object(runtime, 'start_player', side_effect=start), \
                 mock.patch.object(runtime, 'wait_for_stable_open') as release, \
                 mock.patch.object(runtime, 'play_moment'), \
                 mock.patch.object(runtime, 'log_event', side_effect=log), \
                 mock.patch.object(runtime, 'wait_for_approval') as wait, \
                 mock.patch.object(runtime.sound_pack, 'next_send_prompt', return_value=Path('ask-send.wav')), \
                 mock.patch.object(io, 'record', return_value=RecordingResult(str(source), 2, True)), \
                 mock.patch.object(io, 'play_deleted'):
                result = GuidedSession(io, store, lambda kind, **data: events.append(kind)).run(
                    recipient=io.recipient, flow_kind='reply', deleted_cue_path='deleted.wav', account_scope='a' * 64)
                release.assert_not_called()
                wait.assert_not_called()
                if fail_question:
                    self.assertEqual(len(question_attempts), 1)
            jobs = store.jobs()
            self.assertEqual(len(jobs), len(approvals))
            self.assertEqual(events.count('guided_approved'), len(approvals))
            if jobs:
                self.assertEqual(jobs[0].audio_path.read_bytes(), original)
                self.assertEqual(jobs[0].recipient, io.recipient)
            self.assertEqual(len(review_attempts), 2 if busy_first_review else 1)
            if busy_first_review:
                self.assertGreaterEqual(review_attempts[1] - review_attempts[0], .15)
                self.assertLess(review_attempts[1] - review_attempts[0], .17)
            if cleanup_pause:
                self.assertEqual(len(cleanup_pauses), 1)
            if stalled:
                players[0].terminate.assert_called_once()
                players[0].kill.assert_called_once()
            return result, approvals

    def test_ignored_hold_open_bounce_requires_stable_release_and_new_press(self):
        ignored = [(.2, .6), (.605, 2.1)]
        self.assertEqual(self.timed_session(ignored, duration=2), ('deleted', []))
        result, approvals = self.timed_session([(.2, .6), (.605, 1), (1.6, 1.84)], duration=2)
        self.assertEqual(result, 'approved')
        self.assertEqual(len(approvals), 1)
        self.assertGreaterEqual(approvals[0], 1.68)
        # An ignored hold must not rearm through a short opening between clips.
        self.assertEqual(self.timed_session([(.2, .995), (1.005, 1.4)]), ('deleted', []))

    def test_fresh_press_after_send_question_approves_without_release_wait_gap(self):
        for setup_reply in (False, True):
            for release_delay in (0, .25):
                with self.subTest(setup_reply=setup_reply, release_delay=release_delay):
                    result, approvals = self.timed_session([(1.1, 1.3)], during='question',
                        setup_reply=setup_reply, release_delay=release_delay)
                    self.assertEqual(result, 'approved')
                    self.assertEqual(len(approvals), 1)
                    self.assertAlmostEqual(approvals[0], 1.18)

    def test_press_during_question_cleanup_pause_approves_exact_recording_once(self):
        for setup_reply in (False, True):
            with self.subTest(setup_reply=setup_reply):
                result, approvals = self.timed_session([(1.010, 1.210)], during='question',
                    setup_reply=setup_reply, cleanup_pause=.020)
                self.assertEqual(result, 'approved')
                self.assertEqual(len(approvals), 1)
                self.assertGreaterEqual(approvals[0], 1.090)
                self.assertLess(approvals[0], 1.210)

    def test_busy_review_retry_is_bounded_and_nonbusy_error_is_not_retried(self):
        for error, expected, attempts in ((b'Device or resource busy', speaker.SpeakerBusy, 2),
                                          (b'Input/output error', subprocess.CalledProcessError, 1)):
            with self.subTest(error=error):
                streams = []
                def start(command, **kwargs):
                    streams.append(kwargs['stderr'])
                    kwargs['stderr'].write(error)
                    return mock.Mock(returncode=1, poll=lambda: 1)
                clock = [0.0]
                def tick(seconds):
                    clock[0] = round(clock[0] + seconds, 6)
                with mock.patch.object(runtime, 'transport_mode', return_value='cloud'), \
                     mock.patch.object(runtime, 'time', types.SimpleNamespace(monotonic=lambda: clock[0], sleep=tick)), \
                     mock.patch.object(runtime, 'button', types.SimpleNamespace(is_pressed=False), create=True), \
                     mock.patch.object(runtime, 'start_player', side_effect=start) as player, \
                     mock.patch.object(runtime, 'approve_guided_press') as approve:
                    with self.assertRaises(expected):
                        runtime.review_and_wait_for_approval('review.wav', lambda: None, 10)
                    self.assertEqual(player.call_count, attempts)
                    self.assertLessEqual(clock[0], .17)
                    approve.assert_not_called()
                self.assertTrue(all(stream.closed for stream in streams))

    def test_busy_review_retries_once_preserves_recording_and_sends_after_fresh_approval(self):
        for setup_reply in (False, True):
            with self.subTest(setup_reply=setup_reply):
                result, approvals = self.timed_session([(1.1, 1.3)], during='question',
                    setup_reply=setup_reply, busy_first_review=True)
                self.assertEqual(result, 'approved')
                self.assertEqual(len(approvals), 1)

    def test_fresh_press_spanning_review_or_question_completion_approves_exact_recording_once(self):
        for during in ('review', 'question'):
            for setup_reply in (False, True):
                with self.subTest(during=during, setup_reply=setup_reply):
                    result, approvals = self.timed_session([(.96, 1.2)], during=during, setup_reply=setup_reply)
                    self.assertEqual(result, 'approved')
                    self.assertEqual(len(approvals), 1)
                    self.assertAlmostEqual(approvals[0], 1.04)

    def test_press_starting_in_final_poll_interval_approves_exact_recording_once(self):
        for during in ('review', 'question'):
            for setup_reply in (False, True):
                with self.subTest(during=during, setup_reply=setup_reply):
                    result, approvals = self.timed_session([(.996, 1.25)], during=during,
                        setup_reply=setup_reply)
                    self.assertEqual(result, 'approved')
                    self.assertEqual(len(approvals), 1)
                    self.assertAlmostEqual(approvals[0], 1.08)

    def test_failed_optional_setup_question_keeps_approval_wait_and_original_reply(self):
        result, approvals = self.timed_session([(.1, .3)], during='question',
            setup_reply=True, fail_question=True)
        self.assertEqual(result, 'approved')
        self.assertEqual(len(approvals), 1)
        self.assertGreaterEqual(approvals[0], .18)
        self.assertLess(approvals[0], .3)
        with self.assertRaises(subprocess.CalledProcessError):
            self.timed_session([(.1, .3)], during='question', fail_question=True)

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
                        runtime.review_and_wait_for_approval('review.wav', lambda: None, 10)
                approve.assert_not_called()
                process.terminate.assert_called_once()
                process.kill.assert_called_once()
                self.assertEqual(process.wait.call_count, 2)
                self.assertEqual(len(polls), 3)
                self.assertFalse(speaker.is_busy())
                exclusive()


if __name__ == '__main__':
    unittest.main()
