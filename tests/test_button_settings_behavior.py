import math
import os
import shutil
import struct
import tempfile
import unittest
import wave
import sys
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with patch.dict(sys.modules, {"gpiozero": gpiozero}):
    import messagebox.button_send as button_send  # noqa: E402
from messagebox.settings import defaults  # noqa: E402


class FakeLed:
    def __init__(self):
        self.state = None

    def on(self):
        self.state = "on"

    def off(self):
        self.state = "off"


class ButtonSettingsBehaviorTests(unittest.TestCase):
    def test_manual_ring_request_remains_in_flight_until_ring_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "ring-request"
            request.touch()
            observed = []

            def ring_alert(*, source):
                observed.append((source, request.exists()))

            with patch.object(button_send, "RING_REQUEST_FILE", str(request)), patch.object(
                button_send, "ring_alert", side_effect=ring_alert
            ):
                button_send.maybe_manual_ring()

            self.assertEqual(observed, [("dashboard", True)])
            self.assertFalse(request.exists())

    def test_manual_ring_request_survives_failed_playback_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "ring-request"
            request.touch()

            with patch.object(button_send, "RING_REQUEST_FILE", str(request)), patch.object(
                button_send, "ring_alert", side_effect=RuntimeError("playback failed")
            ):
                with self.assertRaisesRegex(RuntimeError, "playback failed"):
                    button_send.maybe_manual_ring()

            self.assertTrue(request.exists())

    def test_review_approval_requires_a_new_press_after_recording_release(self):
        for fresh_press in (False, True):
            with self.subTest(fresh_press=fresh_press):
                calls = []
                button = types.SimpleNamespace(is_pressed=True)
                tick = [0]
                def release():
                    calls.append("release")
                    button.is_pressed = False
                def now():
                    tick[0] += 1
                    button.is_pressed = fresh_press and tick[0] >= 2
                    return tick[0] * .05
                process = types.SimpleNamespace(polls=0, stopped=False)
                def poll():
                    process.polls += 1
                    return 0 if process.stopped or process.polls > 7 else None
                def terminate():
                    calls.append("terminate")
                    process.stopped = True
                process.poll = poll
                process.terminate = terminate
                process.wait = lambda: None
                def spawn(*args, **kwargs):
                    self.assertFalse(button.is_pressed)
                    calls.append("spawn")
                    return process
                with patch.object(button_send, "button", button, create=True), patch.object(button_send, "wait_for_stable_open", side_effect=release), patch.object(button_send.time, "monotonic", side_effect=now), patch.object(button_send.time, "sleep"), patch.object(button_send.subprocess, "Popen", side_effect=spawn), patch.object(button_send, "acknowledge_guided_press") as acknowledge:
                    result = button_send.play_audio_for_approval("review.wav", "test", action="approve_review")
                self.assertEqual(result, fresh_press)
                self.assertEqual(calls[:2], ["release", "spawn"])
                self.assertEqual(acknowledge.call_count, int(fresh_press))
                self.assertEqual(calls.count("terminate"), int(fresh_press))

    def settings(self, **changes):
        document = defaults({"TZ": "America/New_York"})
        document.update(changes)
        return document

    def test_quiet_hours_cross_midnight_and_dst_boundary(self):
        settings = self.settings()
        zone = ZoneInfo("America/New_York")
        self.assertTrue(button_send.quiet_hours(settings, datetime(2026, 3, 8, 1, 30, tzinfo=zone)))
        self.assertTrue(button_send.quiet_hours(settings, datetime(2026, 3, 8, 3, 30, tzinfo=zone)))
        self.assertFalse(button_send.quiet_hours(settings, datetime(2026, 3, 8, 12, 0, tzinfo=zone)))
        settings["quiet_hours"]["enabled"] = False
        self.assertFalse(button_send.quiet_hours(settings, datetime(2026, 3, 8, 1, 30, tzinfo=zone)))

    def test_short_and_long_press_classification(self):
        presses = iter([True, False])
        self.assertEqual(
            button_send.wait_for_hold_intent(
                presses.__next__,
                0.7,
                0.1,
                monotonic=iter([0.0, 0.1, 0.2]).__next__,
                sleeper=lambda _seconds: None,
            ),
            "play",
        )
        clock = iter([0.0, 0.2, 0.4, 0.8]).__next__
        self.assertEqual(
            button_send.wait_for_hold_intent(
                lambda: True,
                0.7,
                0.1,
                monotonic=clock,
                sleeper=lambda _seconds: None,
            ),
            "record",
        )

    def test_arrival_signal_lamp_combinations(self):
        original_led = getattr(button_send, "led", None)
        try:
            for signal, expected in (
                ("ring_and_lamp", "on"),
                ("ring_only", "off"),
                ("lamp_only", "on"),
                ("silent", "off"),
            ):
                with self.subTest(signal=signal):
                    button_send.led = FakeLed()
                    button_send._led_last = 0.0
                    settings = self.settings(arrival_signal=signal)
                    settings["quiet_hours"]["enabled"] = False
                    with patch.object(button_send, "queued", return_value=["waiting.wav"]), patch.object(
                        button_send.time, "monotonic", return_value=100.0
                    ):
                        button_send.refresh_led(force=True, settings=settings)
                    self.assertEqual(button_send.led.state, expected)
        finally:
            button_send.led = original_led

    def test_press_acknowledgement_is_generated_audibly(self):
        self.assertEqual(button_send.BEEPS["nfc"][1:], button_send.BEEPS["press"][1:])
        with patch.object(button_send.subprocess, "run") as run:
            button_send.make_beeps()

        press_command = run.call_args_list[0].args[0]
        self.assertIn("sine=frequency=880:duration=0.40", press_command)
        self.assertIn("volume=12dB", press_command)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_press_acknowledgement_waveform_meets_signal_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "press.wav")
            with patch.object(
                button_send,
                "BEEPS",
                {"press": (path, "880", "0.40", "12")},
            ):
                button_send.make_beeps()

            with wave.open(path, "rb") as cue:
                self.assertEqual(cue.getsampwidth(), 2)
                sample_rate = cue.getframerate()
                samples = struct.unpack(
                    f"<{cue.getnframes()}h", cue.readframes(cue.getnframes())
                )

            duration_s = len(samples) / sample_rate
            peak = max(abs(sample) for sample in samples)
            rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
            self.assertGreaterEqual(duration_s, 0.39)
            self.assertGreaterEqual(peak, 14000)
            self.assertGreaterEqual(rms, 9000)

            from messagebox.onboarding.nfc import TonePlayer
            import subprocess

            def run(command, **kwargs):
                if command[0] == "ffmpeg":
                    return subprocess.run(command, **kwargs)

            TonePlayer(directory, run=run)("read")
            with wave.open(os.path.join(directory, "read-v3.wav"), "rb") as setup_cue:
                self.assertEqual(setup_cue.getframerate(), sample_rate)
                self.assertEqual(setup_cue.readframes(setup_cue.getnframes()), struct.pack(f"<{len(samples)}h", *samples))

    def test_press_acknowledgement_replaces_a_stale_generated_file(self):
        with patch.object(button_send.os.path, "exists", return_value=True), patch.object(
            button_send.subprocess, "run"
        ) as run:
            button_send.make_beeps()

        self.assertEqual(run.call_count, len(button_send.BEEPS))
        self.assertIn("-y", run.call_args_list[0].args[0])


if __name__ == "__main__":
    unittest.main()
