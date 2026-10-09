import math
import tempfile
import struct
import unittest
import wave
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import Mock
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with patch.dict(sys.modules, {"gpiozero": gpiozero}):
    import messagebox.button_send as button_send  # noqa: E402
from messagebox.settings import RINGTONES, defaults, in_quiet_hours  # noqa: E402


class FakeLed:
    def __init__(self):
        self.state = None

    def on(self):
        self.state = "on"

    def off(self):
        self.state = "off"


class MorningRingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.marker = Path(temporary.name) / "morning-ring.json"
        self.settings = defaults({"TZ": "America/New_York"})
        self.now = datetime(2026, 10, 9, 7, 0, tzinfo=ZoneInfo("America/New_York"))
        self.waiting = ["waiting.wav"]
        self.button = types.SimpleNamespace(is_pressed=False)
        self.ring = Mock(return_value=True)
        self.volume = Mock(return_value=True)
        self.tick = 100
        for name, value in (
            ("MORNING_RING_FILE", self.marker), ("_morning_ring_window", None),
            ("_recording", False), ("_guided_active", False),
            ("button", self.button), ("ring_alert", self.ring),
            ("apply_master_volume", self.volume), ("log_event", Mock()),
            ("caregiver_settings", lambda: self.settings),
            ("quiet_hours", lambda settings, now=None: in_quiet_hours(settings, now or self.now)),
            ("queued", lambda: self.waiting), ("_known", None),
            ("_seen_ever", set()), ("_ring_last", 0),
        ):
            patched = patch.object(button_send, name, value, create=True)
            patched.start()
            self.addCleanup(patched.stop)
        clock = patch.object(button_send, "datetime", wraps=datetime)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.clock.now.side_effect = lambda zone: self.now.astimezone(zone)

    def poll(self, minute=0):
        self.now = self.now.replace(hour=7, minute=minute)
        self.tick += 60
        with patch.object(button_send.time, "monotonic", return_value=self.tick):
            button_send.maybe_ring()

    def test_both_transports_signal_once_per_local_window_and_across_restart(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), patch.dict(button_send.os.environ, {"MSGBOX_TRANSPORT": mode}):
                self.marker.unlink(missing_ok=True)
                button_send._morning_ring_window = None
                self.ring.reset_mock()
                self.poll()
                self.poll(1)
                # New process state with the same durable marker.
                button_send._morning_ring_window = None
                button_send._known = None
                self.poll(2)
                self.ring.assert_called_once_with(source="quiet_hours_end", settings=self.settings)
                self.now += timedelta(days=1)
                self.poll()
                self.assertEqual(self.ring.call_count, 2)

    def test_empty_queue_consumes_window_without_a_late_morning_ring(self):
        self.waiting = []
        self.poll()
        self.assertTrue(self.marker.is_file())
        self.ring.assert_not_called()
        self.waiting = ["new.wav"]
        self.poll(1)
        self.ring.assert_called_once_with(settings=self.settings)

    def test_arrival_variants_use_normal_alert_and_silent_consumes_window(self):
        for signal in ("ring_and_lamp", "ring_only", "lamp_only", "silent"):
            with self.subTest(signal=signal):
                self.marker.unlink(missing_ok=True)
                button_send._morning_ring_window = None
                self.ring.reset_mock()
                self.volume.reset_mock()
                self.settings["arrival_signal"] = signal
                self.poll()
                self.assertTrue(self.marker.is_file())
                if signal == "silent":
                    self.ring.assert_not_called()
                    self.volume.assert_not_called()
                else:
                    self.ring.assert_called_once_with(source="quiet_hours_end", settings=self.settings)
                    self.volume.assert_called_once_with(self.settings)

    def test_busy_defers_until_idle_and_expires_after_thirty_minutes(self):
        for busy in ("_recording", "_guided_active", "button"):
            for idle_minute in (10, 30):
                with self.subTest(busy=busy, idle_minute=idle_minute):
                    self.marker.unlink(missing_ok=True)
                    button_send._morning_ring_window = None
                    self.ring.reset_mock()
                    if busy == "button":
                        self.button.is_pressed = True
                    else:
                        setattr(button_send, busy, True)
                    self.poll()
                    self.ring.assert_not_called()
                    self.assertFalse(self.marker.exists())
                    if busy == "button":
                        self.button.is_pressed = False
                    else:
                        setattr(button_send, busy, False)
                    self.poll(idle_minute)
                    self.assertEqual(self.ring.call_count, int(idle_minute < 30))

    def test_timezone_quiet_boundary_and_daytime_window(self):
        self.now = datetime(2026, 10, 9, 10, 59, tzinfo=ZoneInfo("UTC"))
        button_send.maybe_morning_ring(self.settings, self.waiting)
        self.ring.assert_not_called()
        self.now += timedelta(minutes=1)  # 07:00 New York, not the host timezone.
        button_send.maybe_morning_ring(self.settings, self.waiting)
        self.ring.assert_called_once()
        self.settings["quiet_hours"] = {"enabled": True, "start": "12:00", "end": "14:00"}
        self.now = datetime(2026, 10, 9, 14, 0, tzinfo=ZoneInfo("America/New_York"))
        button_send.maybe_morning_ring(self.settings, self.waiting)
        self.assertEqual(self.ring.call_count, 2)

    def test_disabled_all_day_and_expired_windows_stay_silent(self):
        for quiet, minute in (({"enabled": False, "start": "22:00", "end": "07:00"}, 0),
                              ({"enabled": True, "start": "07:00", "end": "07:00"}, 0),
                              ({"enabled": True, "start": "22:00", "end": "07:00"}, 31)):
            self.settings["quiet_hours"] = quiet
            self.poll(minute)
        self.ring.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_marker_is_durable_before_audio_and_storage_failure_suppresses_it(self):
        import json
        self.ring.side_effect = lambda **unused: self.assertIn("window", json.loads(self.marker.read_text()))
        self.poll()
        self.ring.assert_called_once()
        self.now += timedelta(days=1)
        self.ring.reset_mock()
        with patch.object(button_send, "atomic_json", side_effect=OSError("synthetic storage failure")):
            self.poll()
        self.ring.assert_not_called()

    def test_failed_volume_application_defers_without_consuming_window(self):
        self.volume.return_value = False
        self.poll()
        self.ring.assert_not_called()
        self.assertFalse(self.marker.exists())
        self.volume.return_value = True
        self.poll(1)
        self.ring.assert_called_once()


class ButtonSettingsBehaviorTests(unittest.TestCase):
    def test_manual_ring_request_remains_in_flight_until_ring_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "ring-request"
            request.touch()
            observed = []

            def ring_alert(*, source):
                observed.append((source, request.exists()))
                return True

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

    def test_manual_ring_request_survives_missing_ringtone(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "ring-request"
            request.touch()

            with patch.object(
                button_send, "RING_REQUEST_FILE", str(request)
            ), patch.object(
                button_send, "ring_alert", return_value=False
            ):
                button_send.maybe_manual_ring()

            self.assertTrue(request.exists())

    def test_missing_ringtone_reports_incomplete_playback(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            button_send, "ringtone_path", return_value=Path(directory) / "missing.wav"
        ), patch.object(button_send, "log"), patch.object(
            button_send.subprocess,
            "Popen",
            side_effect=AssertionError("player started"),
        ):
            self.assertFalse(button_send.ring_alert(source="dashboard", settings={}))

    def test_lamp_schedules_use_each_assets_note_boundaries(self):
        import json
        pack = Path(__file__).resolve().parents[1] / "sounds"
        button_send.ring_lamp_schedule.cache_clear()
        self.addCleanup(button_send.ring_lamp_schedule.cache_clear)
        with patch.object(button_send, "APP_DIR", pack):
            for ringtone_id in RINGTONES:
                with self.subTest(ringtone_id=ringtone_id):
                    schedule = json.loads((pack / "ringtones" / f"{ringtone_id}.lamp.json").read_text())
                    start, end = schedule["lamp_on"][0]
                    self.assertFalse(button_send.ring_lamp_on(start - 0.001, ringtone_id))
                    self.assertTrue(button_send.ring_lamp_on(start, ringtone_id))
                    self.assertFalse(button_send.ring_lamp_on(end, ringtone_id))
                    self.assertFalse(button_send.ring_lamp_on(schedule["seconds"], ringtone_id))
            self.assertTrue(button_send.ring_lamp_on(0.05, "ding_dong"))

    def test_missing_and_invalid_lamp_schedules_keep_the_previous_pattern(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(button_send, "APP_DIR", Path(directory)):
            lamp_dir = Path(directory) / "ringtones"
            lamp_dir.mkdir()
            for content in (None, "not json", '{}',
                            '{"ringtone_id":"sunshine","seconds":15.5,"lamp_on":[[0,"bad"]]}'):
                if content is not None:
                    (lamp_dir / "sunshine.lamp.json").write_text(content)
                button_send.ring_lamp_schedule.cache_clear()
                self.assertTrue(button_send.ring_lamp_on(0.1, "sunshine"))
                self.assertFalse(button_send.ring_lamp_on(0.5, "sunshine"))
        button_send.ring_lamp_schedule.cache_clear()

    def test_button_press_terminates_a_long_ringtone_without_waiting_for_its_end(self):
        process = types.SimpleNamespace(returncode=-15, stopped=False)
        process.poll = lambda: -15 if process.stopped else None
        def terminate():
            process.stopped = True
        process.terminate = terminate
        process.wait = lambda: self.fail("waited for full ringtone")
        with patch.object(button_send, "button", types.SimpleNamespace(is_pressed=True), create=True), \
             patch.object(button_send, "led", FakeLed(), create=True), \
             patch.object(button_send, "ringtone_path", return_value=Path(__file__)), \
             patch.object(button_send.subprocess, "Popen", return_value=process), \
             patch.object(button_send, "refresh_led"), patch.object(button_send, "log"), \
             patch.object(button_send, "log_event"), patch.object(button_send.time, "sleep") as sleep:
            button_send.ring_alert(settings={**defaults({}), "ringtone_id": "sunshine"})
        self.assertTrue(process.stopped)
        sleep.assert_not_called()

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

    def test_bundled_press_is_exactly_the_hold_window_and_matched_loudness(self):
        from messagebox.sound_pack import validate_sounds
        root = Path(__file__).resolve().parents[1] / "sounds"
        validate_sounds(root)
        with wave.open(str(root / "cues/cue-press.wav"), "rb") as cue:
            self.assertEqual(cue.getnframes(), 19200)
            self.assertEqual(cue.getframerate(), 48000)
            samples = struct.unpack("<19200h", cue.readframes(19200))
        peak = max(abs(value) for value in samples) / 32768
        windows = [samples[i:i + 4800] for i in range(0, len(samples) - 4799, 480)]
        loudest = max(math.sqrt(sum(v*v for v in window) / len(window)) / 32768 for window in windows)
        self.assertLessEqual(peak, 10 ** (-1 / 20) + 0.001)
        # v1.1 taps (Dan, 7 Oct) are louder than the v1 -13 dB target; never quieter.
        self.assertGreaterEqual(20 * math.log10(loudest), -13.5)
        self.assertEqual(button_send.MIN_HOLD_S, 0.4)

    def test_runtime_ready_cue_is_logged_once_and_audio_failure_is_non_fatal(self):
        for result, event in (
            (types.SimpleNamespace(returncode=0), "runtime_ready_cue"),
            (types.SimpleNamespace(returncode=1), "runtime_ready_cue_unavailable"),
        ):
            with self.subTest(returncode=result.returncode), patch.object(
                button_send, "beep", return_value=result
            ) as beep, patch.object(button_send, "log_event") as log_event, patch.object(
                button_send, "log"
            ):
                button_send.announce_runtime_ready()
            beep.assert_called_once_with("ready")
            self.assertEqual(log_event.call_args.args[0], event)

    def test_legacy_press_cue_finishes_before_intent_is_returned(self):
        button = types.SimpleNamespace(is_pressed=True)
        with patch.object(button_send, "button", button, create=True), patch.object(
            button_send, "beep", side_effect=lambda _name: setattr(button, "is_pressed", False)
        ) as beep, patch.object(button_send, "log_event"):
            intent = button_send.acknowledge_and_classify_legacy_press(
                pressed_at=button_send.time.monotonic()
            )
        beep.assert_called_once_with("press")
        self.assertEqual(intent, "play")

    def test_elapsed_press_cue_starts_recording_without_an_extra_wait(self):
        sleeps = []
        intent = button_send.wait_for_hold_intent(
            lambda: True,
            button_send.MIN_HOLD_S,
            button_send.POLL_S,
            started_at=10.0,
            monotonic=lambda: 10.0 + button_send.MIN_HOLD_S,
            sleeper=sleeps.append,
        )
        self.assertEqual(intent, "record")
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
