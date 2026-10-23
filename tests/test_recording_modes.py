from speaker_helpers import isolate_audio
from mic_helpers import physical_capture
import contextlib
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

with mock.patch.dict(sys.modules, {"gpiozero": types.SimpleNamespace(Button=object, LED=object)}):
    from messagebox import button_send as runtime

from messagebox import speaker
from messagebox.guided_reply import GuidedSession, OutboxStore
from messagebox.settings import defaults


class RecordingModesTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)

    def capture_cue_overlap(self, root, *, stop_at, speech_windows, transport='cloud', talk='tap', card_prompt=False):
        clock = [0.0]
        button = types.SimpleNamespace(is_pressed=talk == 'hold')
        recorder = mock.Mock()
        recorder.poll.return_value = None
        chunks = []
        observed = {'start_sound': 0, 'start_end': 0, 'reminder_end': 0}
        led = mock.Mock()
        led.on.side_effect = lambda: observed.setdefault('lamp_at', clock[0])

        def microphone(*args, **kwargs):
            observed['mic_at'] = clock[0]
            return recorder

        def elapsed():
            return round(clock[0] - observed['mic_at'], 6)

        def tick(*args):
            clock[0] = round(clock[0] + .02, 6)
            button.is_pressed = elapsed() < stop_at if talk == 'hold' else elapsed() >= stop_at
            return [recorder.stdout], [], []

        def read(*args):
            voiced = any(start <= elapsed() < end for start, end in speech_windows)
            level = 3000 if voiced else 0
            if clock[0] < observed['reminder_end']:
                level += 6000
            if observed['start_sound'] <= clock[0] < observed['start_end']:
                level += 9000
            expected = struct.pack('<h', level) * 320
            chunks.append(expected)
            return expected

        def drain(**kwargs):
            remainder = b''
            if clock[0] < observed['reminder_end']:
                remainder = struct.pack('<h', 6000) * 3200
                chunks.append(remainder)
            return remainder, b''
        recorder.communicate.side_effect = drain

        def player(command, **kwargs):
            if Path(command[-1]).name in {'cue-rec_go.wav', 'cue-press.wav'}:
                def launch(clipped, **options):
                    with wave.open(str(clipped[-1])) as cue:
                        observed['cue_seconds'] = cue.getnframes() / cue.getframerate()
                    observed['start_sound'] = clock[0] + kwargs['lead_in_s']
                    observed['start_end'] = clock[0] + observed['cue_seconds']
                    def wait(**unused):
                        clock[0] = max(clock[0], observed['start_end'])
                        return 0
                    return types.SimpleNamespace(poll=lambda: None if clock[0] < observed['start_end'] else 0,
                        wait=wait, returncode=0, terminate=lambda: None)
                self.assertEqual(kwargs.pop('max_seconds'), 5)
                return speaker.start_player(command, popen=launch, **kwargs)
            observed['reminder_end'] = clock[0] + 2.3
            return mock.Mock(poll=lambda: None if clock[0] < observed['reminder_end'] else 0)

        with contextlib.ExitStack() as stack:
            for name, value in (('TEMP_DIR', str(root)), ('button', button), ('led', led)):
                stack.enter_context(mock.patch.object(runtime, name, value, create=True))
            stack.enter_context(mock.patch.object(runtime.sound_pack, 'SOUND_DIR', Path(__file__).resolve().parents[1] / 'sounds'))
            stack.enter_context(mock.patch.object(runtime, 'transport_mode', return_value=transport))
            stack.enter_context(mock.patch.object(runtime, 'buttonbox_voice', return_value=transport == 'cloud'))
            stack.enter_context(mock.patch.object(runtime, 'play_sound'))
            stack.enter_context(mock.patch.object(runtime, 'box_muted', return_value=False))
            stack.enter_context(mock.patch.object(runtime, 'quiet_hours', return_value=False))
            stack.enter_context(mock.patch.object(runtime, 'open_microphone', side_effect=microphone))
            stack.enter_context(mock.patch.object(runtime, 'start_player', side_effect=player))
            stack.enter_context(mock.patch.object(runtime, 'presence'))
            stack.enter_context(mock.patch.object(runtime, 'acknowledge_guided_press'))
            stack.enter_context(mock.patch.object(runtime, 'wait_for_stable_open'))
            stack.enter_context(mock.patch.object(runtime.time, 'monotonic', side_effect=lambda: clock[0]))
            stack.enter_context(mock.patch.object(runtime.select, 'select', side_effect=tick))
            stack.enter_context(mock.patch.object(runtime.os, 'read', side_effect=read))
            result = physical_capture(runtime, 'person@example.invalid', talk_mode=talk, card_prompt=card_prompt)
        return result, b''.join(chunks), observed

    def test_speech_during_start_cue_is_kept_from_the_first_sample(self):
        for talk, card_prompt in (('tap', False), ('hold', True)):
            with self.subTest(talk=talk), tempfile.TemporaryDirectory() as directory:
                result, pcm, observed = self.capture_cue_overlap(Path(directory), stop_at=2,
                    speech_windows=[(0, .36)], talk=talk, card_prompt=card_prompt)
                self.assertEqual(observed['lamp_at'], 0)
                self.assertAlmostEqual(observed['cue_seconds'], .27)
                self.assertAlmostEqual(observed['mic_at'], 0)
                self.assertAlmostEqual(observed['start_sound'], .15)
                self.assertTrue(result.meaningful)
                self.assertIn(struct.pack('<h', 12000), pcm)
                with wave.open(result.path) as audio:
                    self.assertEqual(audio.readframes(audio.getnframes()), pcm)

    def test_start_beep_alone_does_not_count_as_speech(self):
        with tempfile.TemporaryDirectory() as directory:
            result, pcm, observed = self.capture_cue_overlap(Path(directory), stop_at=2, speech_windows=[])
            self.assertFalse(result.meaningful)
            self.assertIsNone(result.path)
            self.assertLess(observed['mic_at'], observed['start_sound'])
            self.assertIn(struct.pack('<h', 9000), pcm)

    def test_reminder_only_audio_including_drain_is_kept_for_review(self):
        for stop_at in (40.5, 42.5):
            with self.subTest(stop_at=stop_at), tempfile.TemporaryDirectory() as directory:
                result, pcm, observed = self.capture_cue_overlap(Path(directory), stop_at=stop_at,
                                                                speech_windows=[])
                self.assertGreater(observed['reminder_end'], 40)
                self.assertIn(struct.pack('<h', 6000), pcm)
                self.assertTrue(result.meaningful)
                self.assertTrue(result.review_required)
                with wave.open(result.path) as audio:
                    self.assertEqual(audio.readframes(audio.getnframes()), pcm)

    def test_speech_only_inside_reminder_is_kept_and_requires_review_in_both_modes(self):
        from test_guided_reply import FakeIO
        for transport in ('cloud', 'wacli'):
            for approved in (False, True):
                with self.subTest(transport=transport, approved=approved), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    offset = 0 if transport == 'cloud' else 15
                    result, pcm, _ = self.capture_cue_overlap(root, stop_at=42.5 + offset,
                        speech_windows=[(40.02 + offset, 42.25 + offset)], transport=transport)
                    self.assertTrue(result.meaningful)
                    self.assertTrue(result.review_required)
                    self.assertAlmostEqual(result.duration, 42.58 + offset, delta=.04)
                    with wave.open(result.path) as audio:
                        self.assertEqual(audio.readframes(audio.getnframes()), pcm)
                    io = FakeIO([result], approve_initial=approved)
                    store = OutboxStore(root / 'outbox', transport=transport)
                    events = []
                    outcome = GuidedSession(io, store, lambda kind, **data: events.append(kind)).run(
                        recipient='person@example.invalid', flow_kind='standalone',
                        deleted_cue_path='cue-deleted.wav', review_before_send=False,
                        account_scope='a' * 64 if transport == 'cloud' else None)
                    self.assertEqual(outcome, 'approved' if approved else 'deleted')
                    self.assertIn(('wait', 10.0), io.calls)
                    self.assertIn('guided_review_played', events)
                    self.assertNotIn('guided_recording_empty', events)
                    self.assertEqual(len(store.jobs()), int(approved))

    def test_speech_during_forty_second_reminder_keeps_the_recorded_tail(self):
        for transport in ('cloud', 'wacli'):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                # wacli keeps its five-second warning at 55 seconds.
                offset = 0 if transport == 'cloud' else 15
                result, pcm, observed = self.capture_cue_overlap(Path(directory), stop_at=42.5 + offset,
                    speech_windows=[(0, 1.5), (40.02 + offset, 42.25 + offset)], transport=transport)
                self.assertTrue(result.meaningful)
                self.assertAlmostEqual(result.recorded_seconds, 42.58 + offset, delta=.04)
                self.assertAlmostEqual(result.duration, result.recorded_seconds, delta=.001)
                with wave.open(result.path) as audio:
                    self.assertEqual(audio.readframes(audio.getnframes()), pcm)

    def capture(self, root, talk, *, maximum=30, stop=True, silent=False, short=False,
                carried_press=False):
        clock = [0.0]
        switch = types.SimpleNamespace(is_pressed=talk == "hold" or carried_press)
        stop_at = 0.5 if short else 2.0
        recorder = mock.Mock()
        recorder.poll.return_value = None
        order = []

        def tick(*args):
            clock[0] = round(clock[0] + 0.02, 2)
            if talk == "hold":
                switch.is_pressed = not stop or clock[0] < stop_at
            else:
                switch.is_pressed = ((carried_press and clock[0] < 0.4)
                                     or (stop and clock[0] >= stop_at))
            return [], [], []

        def finish(**kwargs):
            level = 0 if silent else 3000
            return struct.pack("<h", level) * round(clock[0] * 16000), b""

        def cue(command, **kwargs):
            self.assertEqual(Path(command[-1]).name, "cue-rec_go.wav")
            order.append("cue_finished")

        def microphone(command, **kwargs):
            self.assertEqual(order, ["cue_finished"])
            self.assertEqual(command[0], "arecord")
            order.append("microphone")
            return recorder

        recorder.communicate.side_effect = finish
        with contextlib.ExitStack() as stack:
            for name, value in (("button", switch), ("led", mock.Mock()), ("TEMP_DIR", str(root))):
                stack.enter_context(mock.patch.object(runtime, name, value, create=True))
            stack.enter_context(mock.patch.object(runtime.time, "monotonic", side_effect=lambda: clock[0]))
            stack.enter_context(mock.patch.object(runtime.select, "select", side_effect=tick))
            stack.enter_context(mock.patch.object(runtime.subprocess, "run", side_effect=cue))
            stack.enter_context(mock.patch.object(runtime.subprocess, "Popen", side_effect=microphone))
            stack.enter_context(mock.patch.object(runtime, "RecordingLimitCue", return_value=mock.Mock(process=None, intervals=[], played=False)))
            stack.enter_context(mock.patch.object(runtime, "presence"))
            stack.enter_context(mock.patch.object(runtime, "acknowledge_guided_press"))
            stack.enter_context(mock.patch.object(runtime, "wait_for_stable_open"))
            result = physical_capture(runtime, "person@example.invalid", max_seconds=maximum,
                                                     talk_mode=talk, card_prompt=carried_press)
        self.assertEqual(order, ["cue_finished", "microphone"])
        return result

    def test_capture_and_send_every_row_in_both_transports(self):
        for transport in ("wacli", "cloud"):
            for talk in ("tap", "hold"):
                for review in (False, True):
                    with self.subTest(transport=transport, talk=talk, review=review), tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        recording = self.capture(root, talk, carried_press=talk == "tap")
                        self.assertFalse(recording.timed_out)
                        self.assertGreaterEqual(recording.recorded_seconds, 2)
                        io = runtime.PiGuidedIO("person@example.invalid", "session", 30, talk_mode=talk)
                        store = OutboxStore(root / "outbox", transport=transport)
                        with mock.patch.object(io, "record", return_value=recording), \
                             mock.patch.object(io, "play_ordinary") as playback, \
                             mock.patch.object(io, "wait_for_approval", return_value=True) as approval:
                            result = GuidedSession(io, store, lambda *a, **kw: None).run(
                                recipient=io.recipient, flow_kind="standalone", deleted_cue_path="deleted",
                                review_before_send=review, account_scope="a" * 64 if transport == "cloud" else None)
                        self.assertEqual(result, "approved")
                        self.assertFalse(Path(recording.path).exists())
                        self.assertEqual(store.jobs()[0].recipient, io.recipient)
                        self.assertEqual(playback.call_count, int(review))
                        self.assertEqual(approval.call_count, int(review))

    def test_limit_stops_and_keeps_both_capture_modes_for_review(self):
        for talk in ("tap", "hold"):
            for maximum in (30, 60, 120):
                with self.subTest(talk=talk, maximum=maximum), tempfile.TemporaryDirectory() as directory:
                    result = self.capture(Path(directory), talk, maximum=maximum, stop=False)
                    self.assertTrue(result.timed_out)
                    self.assertAlmostEqual(result.recorded_seconds, maximum, delta=0.03)

    def test_short_and_silent_capture_in_both_modes(self):
        for talk in ("tap", "hold"):
            with self.subTest(talk=talk), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                short = self.capture(root, talk, short=True)
                self.assertLess(short.recorded_seconds, 1.5)
                silent = self.capture(root, talk, silent=True)
                self.assertFalse(silent.meaningful)
                self.assertIsNone(silent.path)

    def test_dispatch_uses_talk_mode_independently_of_review(self):
        for talk in ("tap", "hold"):
            for review in (False, True):
                settings = {**defaults({}), "talk_mode": talk, "review_before_send": review}
                with self.subTest(talk=talk, review=review), \
                     mock.patch.object(runtime, "button", types.SimpleNamespace(is_pressed=True), create=True), \
                     mock.patch.object(runtime, "transport_mode", return_value="wacli"), \
                     mock.patch.object(runtime, "caregiver_settings", return_value=settings), \
                     mock.patch.object(runtime, "run_guided_once") as tap, \
                     mock.patch.object(runtime, "record_and_send_legacy") as hold, \
                     mock.patch.object(runtime, "acknowledge_guided_press"), \
                     mock.patch.object(runtime, "wait_for_stable_open"), \
                     mock.patch.object(runtime, "refresh_led"):
                    self.assertTrue(runtime.handle_confirmed_press(0))
                    target = tap if talk == "tap" else hold
                    self.assertEqual(target.call_args.args[0]["review_before_send"], review)
                    self.assertEqual(tap.call_count, int(talk == "tap"))
                    self.assertEqual(hold.call_count, int(talk == "hold"))

    def test_buttonbox_standalone_tap_does_not_acknowledge_twice_or_wait_for_release(self):
        with mock.patch.object(runtime, 'button', types.SimpleNamespace(is_pressed=True), create=True), \
             mock.patch.object(runtime, 'transport_mode', return_value='cloud'), \
             mock.patch.object(runtime, 'buttonbox_voice', return_value=True), \
             mock.patch.object(runtime.cloud_claim, 'consume_claim_press', return_value=False), \
             mock.patch.object(runtime.cloud_runtime, 'play_reactions'), \
             mock.patch.object(runtime, 'caregiver_settings', return_value={**defaults({}), 'talk_mode': 'tap'}), \
             mock.patch.object(runtime, 'queued', return_value=[]), \
             mock.patch.object(runtime, 'run_guided_once') as run, \
             mock.patch.object(runtime, 'acknowledge_guided_press') as acknowledge, \
             mock.patch.object(runtime, 'wait_for_stable_open') as release, \
             mock.patch.object(runtime, 'refresh_led'):
            self.assertTrue(runtime._handle_confirmed_press(0))
        run.assert_called_once()
        acknowledge.assert_not_called()
        release.assert_not_called()

    def test_buttonbox_lamp_precedes_cue_and_mic_does_not_wait_for_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            led = mock.Mock()
            order = []
            cue = mock.Mock()
            cue.poll.return_value = None
            cue.wait.return_value = 0
            recorder = mock.Mock()
            recorder.communicate.return_value = (b'', b'')
            vad = mock.Mock()
            vad.trim_bounds.return_value = None
            def sound(command, **options):
                led.on.assert_called_once()
                self.assertEqual(Path(command[-1]).name, 'cue-rec_go.wav')
                self.assertEqual(options['lead_in_s'], .15)
                self.assertEqual(options['clip_seconds'], .12)
                self.assertEqual(options['max_seconds'], 5)
                order.append('cue_started')
                return cue
            def microphone(*args, **kwargs):
                self.assertEqual(order, ['cue_started'])
                cue.wait.assert_not_called()
                order.append('mic_open')
                return recorder
            with mock.patch.object(runtime, 'TEMP_DIR', directory), \
                 mock.patch.object(runtime, 'buttonbox_voice', return_value=True), \
                 mock.patch.object(runtime, 'button', types.SimpleNamespace(is_pressed=True), create=True), \
                 mock.patch.object(runtime, 'led', led, create=True), \
                 mock.patch.object(runtime, 'start_player', side_effect=sound) as start, \
                 mock.patch.object(runtime, 'play_sound') as blocking_sound, \
                 mock.patch.object(runtime, 'open_microphone', side_effect=microphone), \
                 mock.patch.object(runtime, 'EnergyVAD', return_value=vad), \
                 mock.patch.object(runtime, 'RecordingLimitCue', return_value=mock.Mock(process=None, intervals=[], played=False)), \
                 mock.patch.object(runtime, 'presence'), \
                 mock.patch.object(runtime.select, 'select', return_value=([], [], [])), \
                 mock.patch.object(runtime.time, 'monotonic', side_effect=[0, 60, 60]):
                physical_capture(runtime, 'person')
            self.assertEqual(order, ['cue_started', 'mic_open'])
            start.assert_called_once()
            blocking_sound.assert_not_called()
            cue.terminate.assert_called_once()
            cue.wait.assert_called_once_with(timeout=.2)
            self.assertFalse(runtime._recording)

    def test_buttonbox_hold_uses_one_press_cue_and_no_second_go_cue(self):
        player = mock.Mock(poll=lambda: 0)
        player.wait.return_value = 0
        with mock.patch.object(runtime, 'buttonbox_voice', return_value=True), \
             mock.patch.object(runtime, 'led', mock.Mock(), create=True), \
             mock.patch.object(runtime, 'start_player', return_value=player) as start, \
             mock.patch.object(runtime, 'wait_for_hold_intent', return_value='record') as classify, \
             mock.patch.object(runtime, 'log_event'):
            self.assertEqual(runtime.acknowledge_and_classify_legacy_press(12), 'record')
            with runtime.recording_start('hold'):
                pass
        classify.assert_called_once()
        self.assertEqual(classify.call_args.kwargs['started_at'], 12)
        start.assert_called_once()
        self.assertEqual(Path(start.call_args.args[0][-1]).name, 'cue-press.wav')
        self.assertEqual(start.call_args.kwargs['lead_in_s'], .15)

    def test_wacli_keeps_synchronous_go_even_with_buttonbox_pack_selected(self):
        for talk, card_prompt in (('tap', False), ('hold', False), ('hold', True)):
            with self.subTest(talk=talk, card_prompt=card_prompt), \
                 mock.patch.object(runtime, 'transport_mode', return_value='wacli'), \
                 mock.patch.object(runtime.sound_pack, 'current_voice_pack', return_value='buttonbox'), \
                 mock.patch.object(runtime, 'play_sound') as play, \
                 mock.patch.object(runtime, 'start_player') as start:
                with runtime.recording_start(talk, card_prompt=card_prompt):
                    play.assert_called_once_with(runtime.sound_pack.cue_path('rec_go'), timeout=5)
                start.assert_not_called()

    def test_start_player_launch_failure_prevents_capture(self):
        with mock.patch.object(runtime, 'buttonbox_voice', return_value=True), \
             mock.patch.object(runtime, 'led', mock.Mock(), create=True), \
             mock.patch.object(runtime, 'start_player', side_effect=OSError('synthetic playback failure')), \
             mock.patch.object(runtime, 'open_microphone') as microphone:
            with self.assertRaises(OSError):
                physical_capture(runtime, 'person@example.invalid')
        microphone.assert_not_called()

    def test_capture_failure_reaps_parallel_start_cue(self):
        cue = mock.Mock(poll=lambda: None)
        cue.wait.side_effect = [subprocess.TimeoutExpired('aplay', .2), 0]
        with mock.patch.object(runtime, 'buttonbox_voice', return_value=True), \
             mock.patch.object(runtime, 'button', types.SimpleNamespace(is_pressed=True), create=True), \
             mock.patch.object(runtime, 'led', mock.Mock(), create=True), \
             mock.patch.object(runtime, 'start_player', return_value=cue), \
             mock.patch.object(runtime, 'open_microphone', side_effect=OSError('synthetic microphone failure')):
            with self.assertRaises(OSError):
                physical_capture(runtime, 'person@example.invalid')
        cue.terminate.assert_called_once()
        cue.kill.assert_called_once()
        self.assertEqual(cue.wait.call_count, 2)

    def test_stalled_hold_press_cue_is_reaped_before_classification(self):
        cue = mock.Mock()
        cue.poll.return_value = None
        cue.wait.side_effect = [subprocess.TimeoutExpired('aplay', 5),
                               subprocess.TimeoutExpired('aplay', .2), 0]
        led = mock.Mock()
        with mock.patch.object(runtime, 'buttonbox_voice', return_value=True), \
             mock.patch.object(runtime, 'led', led, create=True), \
             mock.patch.object(runtime, 'start_player', return_value=cue), \
             mock.patch.object(runtime, 'wait_for_hold_intent') as classify:
            with self.assertRaises(subprocess.TimeoutExpired):
                runtime.acknowledge_and_classify_legacy_press(0)
        classify.assert_not_called()
        cue.terminate.assert_called_once()
        cue.kill.assert_called_once()
        self.assertEqual(cue.wait.call_count, 3)
        led.on.assert_called_once()
        led.off.assert_called_once()

    def test_approval_arms_sending_for_tap_review_and_hold_release_only_in_cloud(self):
        for transport in ('cloud', 'wacli'):
            for talk, review in (('tap', True), ('hold', False)):
                with self.subTest(transport=transport, talk=talk), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / 'child.wav'
                    source.write_bytes(b'synthetic recording')
                    store = OutboxStore(root / 'outbox', transport=transport)
                    io = runtime.PiGuidedIO('person@example.invalid', 'session', 60, talk_mode=talk)
                    with mock.patch.object(runtime, 'transport_mode', return_value=transport), \
                         mock.patch.object(runtime.sound_pack, 'current_voice_pack', return_value='buttonbox'), \
                         mock.patch.object(runtime, '_sending_feedback', None), \
                         mock.patch.object(runtime, 'sender_wake') as wake, \
                         mock.patch.object(runtime, 'log_event'), \
                         mock.patch.object(io, 'record', return_value=runtime.RecordingResult(str(source), 2, True)), \
                         mock.patch.object(io, 'play_review'), \
                         mock.patch.object(io, 'wait_for_approval', return_value=True):
                        result = GuidedSession(io, store, lambda kind, **data: runtime.recording_session_event(None, kind, **data)).run(
                            recipient=io.recipient, flow_kind='standalone', deleted_cue_path='deleted',
                            review_before_send=review, account_scope='a' * 64 if transport == 'cloud' else None)
                        self.assertEqual(result, 'approved')
                        if transport == 'cloud':
                            self.assertEqual(runtime._sending_feedback['message_id'], store.jobs()[0].message_id)
                            wake.set.assert_called_once()
                        else:
                            self.assertIsNone(runtime._sending_feedback)
                            wake.set.assert_not_called()

    def test_buttonbox_send_approval_beeps_without_blocking_queue_on_release(self):
        with mock.patch.object(runtime, 'buttonbox_voice', return_value=True), \
             mock.patch.object(runtime, 'wait_for_confirmed_press', return_value=True), \
             mock.patch.object(runtime, 'start_player', return_value=mock.Mock()) as start, \
             mock.patch.object(runtime, 'acknowledge_guided_press') as blocking_ack, \
             mock.patch.object(runtime, 'wait_for_stable_open') as release, \
             mock.patch.object(runtime, 'log_event'):
            self.assertTrue(runtime.wait_for_approval(10, 'session'))
        start.assert_called_once()
        self.assertEqual(Path(start.call_args.args[0][-1]).name, 'cue-press.wav')
        self.assertEqual(start.call_args.kwargs['lead_in_s'], 0)
        blocking_ack.assert_not_called()
        release.assert_not_called()
