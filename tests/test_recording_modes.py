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
            result = runtime.capture_guided_recording("person@example.invalid", max_seconds=maximum,
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

    def test_limit_discards_tap_but_stops_and_keeps_hold(self):
        for talk in ("tap", "hold"):
            for maximum in (30, 60, 120):
                with self.subTest(talk=talk, maximum=maximum), tempfile.TemporaryDirectory() as directory:
                    result = self.capture(Path(directory), talk, maximum=maximum, stop=False)
                    self.assertEqual(result.timed_out, talk == "tap")
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
