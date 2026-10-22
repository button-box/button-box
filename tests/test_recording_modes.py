from speaker_helpers import isolate_audio
from mic_helpers import physical_capture
import contextlib
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

with mock.patch.dict(sys.modules, {"gpiozero": types.SimpleNamespace(Button=object, LED=object)}):
    from messagebox import button_send as runtime

from messagebox.guided_reply import GuidedSession, OutboxStore
from messagebox.settings import defaults


class RecordingModesTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)

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
            stack.enter_context(mock.patch.object(runtime, "RecordingLimitCue"))
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

    def test_buttonbox_lamp_and_mic_start_while_one_cue_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            led = mock.Mock()
            order = []
            cue = mock.Mock()
            cue.poll.return_value = None
            recorder = mock.Mock()
            recorder.communicate.return_value = (b'', b'')
            vad = mock.Mock()
            vad.trim_bounds.return_value = None
            def sound(command, **options):
                led.on.assert_called_once()
                self.assertEqual(Path(command[-1]).name, 'cue-rec_go.wav')
                self.assertEqual(options['lead_in_s'], 0)
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
                 mock.patch.object(runtime, 'RecordingLimitCue'), \
                 mock.patch.object(runtime, 'presence'), \
                 mock.patch.object(runtime.select, 'select', return_value=([], [], [])), \
                 mock.patch.object(runtime.time, 'monotonic', side_effect=[0, 60, 60]):
                physical_capture(runtime, 'person')
            self.assertEqual(order, ['cue_started', 'mic_open'])
            start.assert_called_once()
            blocking_sound.assert_not_called()
            cue.terminate.assert_called_once()
            cue.wait.assert_called_once()
            self.assertFalse(runtime._recording)

    def test_buttonbox_hold_uses_one_press_cue_and_no_second_go_cue(self):
        player = mock.Mock(poll=lambda: 0)
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
        self.assertEqual(start.call_args.kwargs['lead_in_s'], 0)
