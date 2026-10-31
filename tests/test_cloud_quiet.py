from speaker_helpers import isolate_audio
import contextlib
import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from messagebox.settings import (SettingsError, defaults, in_quiet_hours,
                                 morning_quiet_end, quiet_schedule, validate)

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with patch.dict(sys.modules, {"gpiozero": gpiozero}):
    from messagebox import button_send
cloud_quiet = button_send.cloud_quiet
CloudRuntime = button_send.cloud_runtime.CloudRuntime
CloudRuntimeError = button_send.CloudRuntimeError


def period(start, end):
    return {"start": start, "end": end}


class QuietScheduleTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)
        self.settings = defaults({"TZ": "Europe/Lisbon"})
        self.settings["quiet_schedules"] = {
            "school_days": [period("22:00", "07:00"), period("13:00", "15:00")],
            "weekend": [period("23:00", "09:00"), period("14:00", "16:00")],
        }

    def local(self, value, timezone=None):
        return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(timezone or self.settings["timezone"]))

    def quiet(self, value):
        return in_quiet_hours(self.settings, self.local(value), cloud=True)

    def test_weekend_switch_uses_the_morning_and_the_daytime(self):
        for value, expected in (
            ("2026-10-08T22:00", True),  # Thursday -> school Friday
            ("2026-10-09T22:30", False), # Friday -> weekend Saturday, starts 23
            ("2026-10-09T23:00", True),
            ("2026-10-10T08:59", True),
            ("2026-10-10T09:00", False),
            ("2026-10-10T13:30", False),
            ("2026-10-10T14:00", True),
            ("2026-10-11T22:00", True),  # Sunday -> school Monday
            ("2026-10-12T07:00", False),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.quiet(value), expected)

    def test_jerusalem_default_and_custom_days(self):
        self.settings["timezone"] = "Asia/Jerusalem"
        self.assertFalse(self.quiet("2026-10-08T22:30"))
        self.assertTrue(self.quiet("2026-10-08T23:00"))
        self.assertTrue(self.quiet("2026-10-09T08:30"))
        self.assertTrue(self.quiet("2026-10-10T22:30")) # school Sunday
        self.settings["weekend_days"] = [2]
        self.assertFalse(self.quiet("2026-10-13T22:30")) # Wednesday morning
        self.assertTrue(self.quiet("2026-10-14T08:30"))
        self.settings["weekend_days"] = []
        self.assertFalse(self.quiet("2026-10-14T08:30"))

    def test_holiday_is_inclusive_and_expires_in_box_timezone(self):
        self.settings["holiday_until"] = "2026-10-13"
        self.assertTrue(self.quiet("2026-10-12T08:30"))
        self.assertTrue(self.quiet("2026-10-13T08:30"))
        self.assertTrue(self.quiet("2026-10-13T22:30")) # next morning is school
        self.assertFalse(self.quiet("2026-10-14T08:30"))
        instant = self.local("2026-10-14T08:30").astimezone(ZoneInfo("America/New_York"))
        self.assertFalse(in_quiet_hours(self.settings, instant, cloud=True))

    def test_overlap_merges_midnight_crossing_and_all_day(self):
        self.settings["quiet_schedules"]["school_days"] = [
            period("22:00", "07:00"), period("06:00", "08:00"), period("13:00", "15:00")]
        self.assertTrue(self.quiet("2026-10-09T07:00"))
        self.assertTrue(self.quiet("2026-10-09T07:59"))
        self.assertFalse(self.quiet("2026-10-09T08:00"))
        self.settings["quiet_schedules"]["school_days"] = [period("07:00", "07:00")]
        self.assertTrue(self.quiet("2026-10-09T20:00"))
        self.assertIsNone(morning_quiet_end(self.settings, self.local("2026-10-09T09:00")))

    def test_morning_rule_latest_end_and_never_naps(self):
        self.settings["quiet_schedules"]["school_days"] = [
            period("22:00", "06:00"), period("05:00", "08:00"), period("13:00", "15:00")]
        current = self.local("2026-10-09T08:00")
        self.assertEqual(morning_quiet_end(self.settings, current), current)
        for end, expected in (("03:59", False), ("04:00", True), ("11:00", True), ("11:01", False)):
            self.settings["quiet_schedules"]["school_days"] = [period("22:00", end)]
            self.assertEqual(morning_quiet_end(self.settings, current) is not None, expected)

    def test_midnight_and_dst_in_box_timezone(self):
        self.settings["timezone"] = "America/New_York"
        for value in ("2026-03-08T00:00", "2026-03-08T01:30", "2026-03-08T03:30"):
            self.assertTrue(self.quiet(value))
        self.assertFalse(self.quiet("2026-03-08T09:00"))
        for fold in (0, 1):
            repeated = self.local("2026-11-01T01:30").replace(fold=fold)
            self.assertTrue(in_quiet_hours(self.settings, repeated.astimezone(ZoneInfo("UTC")), cloud=True))

    def test_legacy_fills_both_schedules_and_wacli_ignores_cloud_fields(self):
        legacy = defaults({"TZ": "Europe/Lisbon"})
        for day in (self.local("2026-10-09T12:00"), self.local("2026-10-10T12:00")):
            self.assertEqual(quiet_schedule(legacy, day.date()), [period("22:00", "07:00")])
        legacy["quiet_hours"]["enabled"] = False
        self.assertEqual(quiet_schedule(legacy, day.date()), [])
        with patch.dict(os.environ, {"MSGBOX_TRANSPORT": "wacli"}), \
             patch.object(cloud_quiet, "muted", side_effect=AssertionError("Cloud control read")), \
             patch.object(cloud_quiet, "quiet_now", side_effect=AssertionError("Cloud control read")):
            self.assertFalse(button_send.quiet_hours(self.settings, self.local("2026-10-10T08:30")))
            self.assertTrue(button_send.quiet_hours(self.settings, self.local("2026-10-09T22:30")))
            self.assertFalse(button_send.box_muted())

    def test_validation_limits_and_old_document_defaults(self):
        old = {key: value for key, value in defaults({}).items()
               if key not in {"quiet_schedules", "weekend_days", "holiday_until", "morning_ring"}}
        self.assertEqual(validate(old), defaults({}))
        for changes in (
            {"quiet_schedules": {"school_days": [], "weekend": [period("13:00", "15:00")] * 4}},
            {"quiet_schedules": {"school_days": [dict(period("13:00", "15:00"), name="Nap")], "weekend": []}},
            {"quiet_schedules": {"school_days": [period("25:00", "15:00")], "weekend": []}},
            {"quiet_schedules": {"school_days": []}}, {"quiet_schedules": []},
            {"weekend_days": [7]}, {"weekend_days": [True]}, {"weekend_days": [1, 1]},
            {"holiday_until": "2026-02-30"}, {"holiday_until": "20261013"}, {"morning_ring": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(SettingsError):
                validate({**self.settings, **changes, "future_optional": True})
        self.assertEqual(validate({**self.settings, "future_optional": True}), self.settings)


class SilenceTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "silence.json"
        self.store = cloud_quiet.SilenceStore(self.path)
        patched = patch.object(cloud_quiet, "silence_store", self.store)
        patched.start()
        self.addCleanup(patched.stop)
        mode = patch.dict(os.environ, {"MSGBOX_TRANSPORT": "cloud"})
        mode.start()
        self.addCleanup(mode.stop)

    def test_mute_quiet_now_expiry_and_manual_are_independent_and_durable(self):
        now = 1_791_000_000
        self.store.apply("quiet_now", now + 3600, 1)
        self.store.apply("mute", True, 2)
        restarted = cloud_quiet.SilenceStore(self.path)
        self.assertTrue(cloud_quiet.quiet_now(restarted.load(), now + 3599))
        self.assertFalse(cloud_quiet.quiet_now(restarted.load(), now + 3600))
        self.assertTrue(restarted.load()["muted"])
        self.store.apply("quiet_now", "manual", 3)
        self.store.apply("mute", False, 4)
        self.assertTrue(cloud_quiet.quiet_now(self.store.load(), now + 86400))
        self.store.apply("quiet_now", None, 5)
        self.assertFalse(cloud_quiet.quiet_now(self.store.load(), now))
        self.assertFalse(cloud_quiet.muted())

    def test_replay_stale_commands_and_corruption_fail_closed(self):
        self.store.apply("quiet_now", "manual", 3)
        self.store.apply("quiet_now", None, 4)
        self.store.apply("quiet_now", "manual", 3)
        self.assertIsNone(self.store.load()["quiet_until"])
        self.store.apply("mute", True, 5)
        self.store.apply("mute", False, 6)
        self.store.apply("mute", True, 5)
        self.assertFalse(cloud_quiet.muted())
        self.path.write_text("not json")
        self.assertTrue(cloud_quiet.muted())
        self.assertTrue(cloud_quiet.quiet_now())
        self.store.apply("mute", False, 7)
        self.assertTrue(cloud_quiet.quiet_now())

    def test_expired_command_does_not_interrupt_new_deliberate_audio(self):
        now = 1_791_000_000
        state = self.store.apply("quiet_now", now - 1, 1, now=now)
        self.assertEqual(state["quiet_sequence"], 1)
        self.assertEqual(state["stop_sequence"], -1)
        self.assertFalse(cloud_quiet.quiet_now(state, now))

    def test_bad_payload_never_mutates_state(self):
        for kind, value in (("mute", 1), ("mute", None), ("quiet_now", True),
                            ("quiet_now", float("nan")), ("quiet_now", "15:00"), ("other", True)):
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                self.store.apply(kind, value, 1)
        self.assertFalse(self.path.exists())

    def test_muted_press_blinks_once_and_preserves_queue_and_saved_volume(self):
        self.store.apply("mute", True, 1)
        settings = defaults({})
        led = Mock()
        with patch.object(button_send, "button", types.SimpleNamespace(is_pressed=True), create=True), \
             patch.object(button_send, "caregiver_settings", return_value=settings), \
             patch.object(button_send.cloud_claim, "consume_claim_press", return_value=False), \
             patch.object(button_send, "led", led, create=True), \
             patch.object(button_send, "wait_for_stable_open"), \
             patch.object(button_send.time, "sleep"), \
             patch.object(button_send, "run_guided_once") as guided, \
             patch.object(button_send, "record_and_send_legacy") as legacy:
            self.assertTrue(button_send.handle_confirmed_press(0))
        self.assertEqual(led.method_calls, [("on", (), {}), ("off", (), {})])
        guided.assert_not_called()
        legacy.assert_not_called()
        self.assertEqual(settings["master_volume_percent"], 50)
        with patch.object(button_send, "queued", return_value=["waiting.wav"]), \
             patch.object(button_send, "led", led, create=True):
            button_send.refresh_led(force=True, settings=settings)
        self.assertEqual(led.method_calls[-1], ("off", (), {}))

    def test_active_player_stops_for_both_controls_and_quiet_now_allows_later_play(self):
        # Real subprocess boundary; no audio hardware. A durable new command
        # must terminate the already-running child within a bounded time.
        for index, (kind, value) in enumerate((("quiet_now", "manual"), ("mute", True)), 1):
            process = cloud_quiet.start_player([sys.executable, "-c", "import time; time.sleep(30)"])
            self.addCleanup(lambda p=process: p.kill() if p.poll() is None else None)
            self.store.apply(kind, value, index)
            self.assertNotEqual(process.wait(timeout=2), 0)
        with self.assertRaises(cloud_quiet.AudioSilenced):
            cloud_quiet.start_player([sys.executable, "-c", "pass"])
        self.store.apply("mute", False, 3)
        # Quiet now remains active; deliberate audio is allowed after its start.
        result = cloud_quiet.play([sys.executable, "-c", "pass"], timeout=2)
        self.assertEqual(result.returncode, 0)

    def test_stalled_approval_player_is_killed_reaped_and_releases_both_locks(self):
        from messagebox import maintenance, speaker
        root = self.path.parent
        lock = root / 'maintenance.lock'
        lock.touch()
        ready = root / 'player-ready'
        popen = subprocess.Popen
        start_player = button_send.start_player
        players = []
        children = []
        script = ('import pathlib,signal,sys,time; '
                  'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                  'pathlib.Path(sys.argv[1]).touch(); time.sleep(30)')
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(maintenance, 'LOCK', lock))
            stack.enter_context(patch.object(maintenance, 'INHIBIT', root / 'inhibit'))
            stack.enter_context(patch.object(button_send, 'APPROVAL_CUE_TIMEOUT_S', .05, create=True))
            stack.enter_context(patch.object(button_send, 'buttonbox_voice', return_value=True))
            stack.enter_context(patch.object(button_send, 'wait_for_confirmed_press', return_value=True))
            stack.enter_context(patch.object(button_send, 'log_event'))

            def launch(*args, **kwargs):
                child = popen([sys.executable, '-c', script, str(ready)])
                children.append(child)
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                stack.enter_context(patch.object(child, 'terminate', wraps=child.terminate))
                stack.enter_context(patch.object(child, 'kill', wraps=child.kill))
                return child

            def start(*args, **kwargs):
                player = start_player(*args, **kwargs)
                players.append(player)
                return player

            stack.enter_context(patch.object(cloud_quiet.subprocess, 'Popen', side_effect=launch))
            stack.enter_context(patch.object(button_send, 'start_player', side_effect=start))
            try:
                started = time.monotonic()
                self.assertTrue(button_send.wait_for_approval(10, 'synthetic'))
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(speaker.is_busy())
                with lock.open('rb') as exclusive:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(exclusive, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertTrue(players[0].reservation_released.wait(2), 'approval player has no cleanup deadline')
                    self.assertTrue(players[0].speaker_released.is_set())
                    self.assertFalse(speaker.is_busy())
                    fcntl.flock(exclusive, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(children[0].returncode, -signal.SIGKILL)
                children[0].terminate.assert_called_once()
                children[0].kill.assert_called_once()
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=2)
                for player in players:
                    self.assertTrue(player.reservation_released.wait(2))
        self.assertEqual(cloud_quiet.play([sys.executable, '-c', 'pass'], timeout=2).returncode, 0)

    def test_deliberate_press_during_quiet_now_still_starts_normal_flow(self):
        self.store.apply("quiet_now", "manual", 1)
        with patch.object(button_send, "button", types.SimpleNamespace(is_pressed=True), create=True), \
             patch.object(button_send.cloud_claim, "consume_claim_press", return_value=False), \
             patch.object(button_send, "caregiver_settings", return_value=defaults({})), \
             patch.object(button_send, "acknowledge_guided_press"), \
             patch.object(button_send, "wait_for_stable_open"), \
             patch.object(button_send, "refresh_led"), \
             patch.object(button_send, "run_guided_once") as guided:
            self.assertTrue(button_send.handle_confirmed_press(0))
            guided.assert_called_once()

    def test_interrupted_message_stays_queued_without_played_receipt(self):
        queue = self.path.parent / "queue"
        queue.mkdir()
        audio = queue / "waiting.wav"
        audio.write_bytes(b"synthetic")
        with patch.object(button_send, "queued", return_value=[audio.name]), \
             patch.object(button_send, "QUEUE_DIR", str(queue)), \
             patch.object(button_send, "queue_metadata", return_value={}), \
             patch.object(button_send, "inbound_audio_authorized", return_value=True), \
             patch.object(button_send, "play_pending_listened"), \
             patch.object(button_send, "play_moment"), \
             patch.object(button_send, "play_sound", side_effect=cloud_quiet.AudioSilenced("synthetic")), \
             patch.object(button_send, "beep"), patch.object(button_send, "log"), \
             patch.object(button_send, "wait_for_stable_open"), patch.object(button_send, "refresh_led"), \
             patch.object(button_send, "archive_played_file") as archive, \
             patch.object(button_send.cloud_runtime, "record_played") as played:
            button_send.play_next_legacy()
        self.assertTrue(audio.exists())
        archive.assert_not_called()
        played.assert_not_called()

    def test_commands_apply_and_ack_replay_without_settings_revision_changes(self):
        client = Mock()
        runtime = CloudRuntime(client, state_path=self.path.parent / "runtime.json",
                               settings_path=self.path.parent / "settings.json")
        before = runtime.settings.load()[0]
        item = {"operation_id": "quiet-test", "sequence": 1, "kind": "quiet_now",
                "payload": {"quiet_until": "manual", "future_optional": True}}
        with patch.object(runtime, "_ack") as ack, patch.object(runtime, "_replay_completed", return_value=False):
            runtime._command(item, 1_791_000_000)
            self.assertEqual(runtime.quiet_state(), {"muted": False, "quiet_until": "manual"})
            self.assertEqual(runtime.settings.load()[0], before)
            runtime._command(item, 1_791_000_000)
            self.assertEqual(ack.call_count, 2)
        self.assertEqual(self.store.load()["quiet_sequence"], 1)
        with patch.object(runtime, "_replay_completed", return_value=False), self.assertRaises(CloudRuntimeError):
            runtime._command({**item, "operation_id": "invalid", "payload": {}}, 1_791_000_000)

    def test_silence_commands_run_before_older_audio_downloads(self):
        now = 1_791_000_000
        client = Mock()
        runtime = CloudRuntime(client, state_path=self.path.parent / "runtime.json",
                               settings_path=self.path.parent / "settings.json")
        def item(kind, sequence, payload):
            return {"operation_id": f"synthetic-{sequence}", "sequence": sequence,
                    "kind": kind, "created_at": now, "expires_at": now + 60, "payload": payload}
        client.inbox.return_value = {"box_id": "synthetic-box", "server_time": now, "items": [
            item("audio", 1, {}), item("mute", 9, {"muted": True}),
            item("quiet_now", 10, {"quiet_until": "manual"})]}
        def download(*unused):
            self.assertTrue(self.store.load()["muted"])
            self.assertEqual(self.store.load()["quiet_until"], "manual")
        with patch.object(runtime, "flush_acks"), patch.object(runtime, "_ack"), \
             patch.object(runtime, "_snapshot", return_value={"box_id": "synthetic-box"}), \
             patch.object(runtime, "_replay_completed", return_value=False), \
             patch.object(runtime, "_audio", side_effect=download) as audio, \
             patch.object(runtime, "recover_outbox"):
            runtime.poll_once()
        audio.assert_called_once()
        self.assertEqual(runtime.state["cursor"], 10)


class MorningRingTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.marker = Path(self.temp.name) / "morning.json"
        self.store = cloud_quiet.SilenceStore(Path(self.temp.name) / "silence.json")
        self.settings = defaults({"TZ": "Europe/Lisbon"})
        self.settings["quiet_schedules"] = {
            "school_days": [period("22:00", "06:00"), period("05:00", "08:00"), period("13:00", "15:00")],
            "weekend": [period("23:00", "09:00")],
        }
        self.ring = Mock(return_value=True)
        for name, value in (("MORNING_RING_FILE", self.marker), ("_morning_ring_window", None),
                            ("_recording", False), ("_guided_active", False),
                            ("button", types.SimpleNamespace(is_pressed=False)),
                            ("ring_alert", self.ring), ("apply_master_volume", Mock(return_value=True))):
            patched = patch.object(button_send, name, value, create=True)
            patched.start()
            self.addCleanup(patched.stop)
        for patched in (patch.dict(os.environ, {"MSGBOX_TRANSPORT": "cloud"}),
                        patch.object(cloud_quiet, "silence_store", self.store)):
            patched.start()
            self.addCleanup(patched.stop)

    def poll(self, value):
        now = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(self.settings["timezone"]))
        return button_send.maybe_morning_ring(self.settings, ["waiting.wav"], now)

    def test_latest_morning_only_never_nap_and_at_most_once_after_restart(self):
        self.assertFalse(self.poll("2026-10-09T06:00"))
        self.assertTrue(self.poll("2026-10-09T08:00"))
        button_send._morning_ring_window = None
        self.assertFalse(self.poll("2026-10-09T08:01"))
        self.assertFalse(self.poll("2026-10-09T15:00"))
        self.ring.assert_called_once()
        self.assertTrue(self.poll("2026-10-10T09:00"))
        self.assertEqual(self.ring.call_count, 2)

    def test_switch_mute_and_quiet_now_consume_without_a_ring_on_expiry(self):
        for control in ("switch", "mute", "quiet_now"):
            with self.subTest(control=control):
                self.marker.unlink(missing_ok=True)
                self.store.path.unlink(missing_ok=True)
                button_send._morning_ring_window = None
                self.ring.reset_mock()
                self.settings["morning_ring"] = control != "switch"
                if control == "mute":
                    self.store.apply("mute", True, 1)
                if control == "quiet_now":
                    self.store.apply("quiet_now", "manual", 3)
                self.assertTrue(self.poll("2026-10-09T08:00"))
                self.settings["morning_ring"] = True
                self.store.apply("mute", False, 2)
                self.store.apply("quiet_now", None, 4)
                self.assertFalse(self.poll("2026-10-09T08:10"))
                self.ring.assert_not_called()

    def test_holiday_and_dst_morning_use_local_time_and_grace(self):
        self.settings["holiday_until"] = "2026-10-13"
        self.assertFalse(self.poll("2026-10-12T08:00"))
        self.assertTrue(self.poll("2026-10-12T09:00"))
        self.assertTrue(self.poll("2026-10-14T08:00"))
        self.assertFalse(self.poll("2026-10-15T08:30"))
        self.assertTrue(self.poll("2026-10-25T09:00"))
        self.assertEqual(self.ring.call_count, 3)

    def test_overlapping_non_morning_period_cannot_create_a_later_ring(self):
        self.settings["quiet_schedules"]["school_days"] = [
            period("22:00", "08:00"), period("07:50", "12:00")]
        self.assertTrue(self.poll("2026-10-09T08:00"))
        self.assertFalse(self.poll("2026-10-09T12:00"))
        self.ring.assert_not_called()
