import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from messagebox import cloud_health, cloud_quiet

NOW = 1_800_000_000


class HealthTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.audio = self.root / "audio-health.json"
        for name, value in (("AUDIO_FILE", self.audio), ("SERVICES_FILE", self.root / "restarts.json"),
                            ("APP_DIR", self.root / "app"), ("CLOUD_DIR", self.root / "cloud"),
                            ("NFC_HEALTH_FILE", self.root / "nfc-health")):
            patcher = mock.patch.object(cloud_health, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        cloud_health._cache.clear()
        self.addCleanup(cloud_health._cache.clear)


class AudioHealthTests(HealthTestCase):
    def test_audio_ok_is_unknown_then_follows_the_last_player_answer(self):
        self.assertIsNone(cloud_health.audio_report()["ok"])
        cloud_health.record_audio("message", now=NOW)
        self.assertIs(cloud_health.audio_report(now=NOW)["ok"], True)
        cloud_health.record_audio("cue", "play_failed", now=NOW)
        self.assertIs(cloud_health.audio_report(now=NOW)["ok"], False)
        cloud_health.record_audio("cue", now=NOW + 1)
        self.assertIs(cloud_health.audio_report(now=NOW + 1)["ok"], True)

    def test_failures_24h_counts_by_kind_in_a_rolling_window(self):
        cloud_health.record_audio("voice_line", "file_missing", now=NOW - 86400 + 10)
        cloud_health.record_audio("voice_line", "file_missing", now=NOW)
        cloud_health.record_audio("ringtone", "play_failed", now=NOW)
        cloud_health.record_audio("speech", "play_failed", now=NOW)  # Unknown kinds are not counted.
        counts = cloud_health.audio_report(now=NOW)["failures_24h"]
        self.assertEqual(counts, {"message": 0, "voice_line": 2, "cue": 0, "ringtone": 1, "preview": 0})
        self.assertEqual(cloud_health.audio_report(now=NOW + 20)["failures_24h"]["voice_line"], 1)

    def test_failure_list_is_bounded(self):
        for second in range(cloud_health.MAX_FAILURES_PER_KIND + 50):
            cloud_health.record_audio("cue", "play_failed", now=NOW + second)
        state = json.loads(self.audio.read_text())
        self.assertEqual(len(state["failures"]["cue"]), cloud_health.MAX_FAILURES_PER_KIND)

    def test_last_error_is_a_code_and_a_time_only(self):
        self.assertIsNone(cloud_health.audio_report()["last_error"])
        cloud_health.record_audio("message", "play_failed", now=NOW)
        cloud_health.record_audio("message", "Bad Code /opt/x.wav", now=NOW + 5)  # Rejected.
        self.assertEqual(cloud_health.audio_report(now=NOW)["last_error"], {"code": "play_failed", "at": NOW})

    def test_player_outcomes_ignore_deliberate_stops(self):
        existing = self.root / "clip.wav"
        existing.write_bytes(b"RIFF")
        def finish(returncode, path=existing, **flags):
            cloud_health.player_finished(SimpleNamespace(returncode=returncode, health_kind="message", **flags), path)
        finish(1, health_stopped=True)
        finish(-15)
        self.assertFalse(self.audio.exists())
        finish(1)
        self.assertEqual(cloud_health.audio_report()["last_error"]["code"], "play_failed")
        finish(1, path=self.root / "missing.wav")
        self.assertEqual(cloud_health.audio_report()["last_error"]["code"], "file_missing")
        finish(-9, health_code="play_timeout", health_stopped=True)
        self.assertEqual(cloud_health.audio_report()["last_error"]["code"], "play_timeout")
        self.assertEqual(cloud_health.audio_report()["failures_24h"]["message"], 3)

    def test_kind_comes_from_the_file_or_the_preview_scope(self):
        app = cloud_health.APP_DIR
        self.assertEqual(cloud_health.classify(app / "sounds/cues/cue-sent.wav"), "cue")
        self.assertEqual(cloud_health.classify(app / "sounds/voices/pirate/voice-online.wav"), "voice_line")
        self.assertEqual(cloud_health.classify(app / "ringtones/bouncy.wav"), "ringtone")
        self.assertEqual(cloud_health.classify("/var/lib/messagebox/queue/a.wav"), "message")
        with cloud_health.audio_kind("preview"):
            self.assertEqual(cloud_health.classify(app / "ringtones/bouncy.wav"), "preview")
        self.assertEqual(cloud_health.classify(app / "ringtones/bouncy.wav"), "ringtone")


class CloudPlayerHookTests(HealthTestCase):
    def setUp(self):
        super().setUp()
        store = cloud_quiet.SilenceStore(self.root / "silence.json")
        patcher = mock.patch.object(cloud_quiet, "silence_store", store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait_for_report(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            report = cloud_health.audio_report()
            if predicate(report):
                return report
            time.sleep(0.02)
        self.fail("audio health was not recorded")

    def test_cloud_player_records_failure_success_and_unavailable_player(self):
        script = self.root / "fails.py"
        script.write_text("import sys\nsys.exit(1)\n")
        with self.assertRaises(subprocess.CalledProcessError):
            cloud_quiet.play([sys.executable, str(script)], timeout=5)
        report = self.wait_for_report(lambda r: r["ok"] is False)
        self.assertEqual(report["last_error"]["code"], "play_failed")
        script.write_text("")
        cloud_quiet.play([sys.executable, str(script)], timeout=5)
        self.wait_for_report(lambda r: r["ok"] is True)
        with mock.patch.object(cloud_quiet.subprocess, "Popen", side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                cloud_quiet.start_player(["aplay", str(script)])
        self.assertEqual(cloud_health.audio_report()["last_error"]["code"], "player_unavailable")

    def test_a_stopped_player_is_not_a_failure(self):
        script = self.root / "sleeps.py"
        script.write_text("import time\ntime.sleep(30)\n")
        process = cloud_quiet.start_player([sys.executable, str(script)])
        process.terminate()
        process.wait(timeout=5)
        process.reservation_released.wait(2)
        time.sleep(0.1)
        self.assertEqual(cloud_health.audio_report()["failures_24h"]["message"], 0)


class SoundCheckTests(HealthTestCase):
    def manifest(self, entries):
        path = self.root / "app" / "release-manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"files": entries}))
        return path

    def sound(self, source, installed, data=b"sound"):
        target = self.root / "app" / installed.removeprefix("/opt/messagebox/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"source": source, "installed": installed, "sha256": hashlib.sha256(b"sound").hexdigest()}

    def test_sounds_ok_when_every_bundled_file_matches(self):
        self.manifest([self.sound("sounds/cues/cue-sent.wav", "/opt/messagebox/sounds/cues/cue-sent.wav"),
                       self.sound("sounds/ringtones/bouncy.wav", "/opt/messagebox/ringtones/bouncy.wav"),
                       {"source": "messagebox/x.py", "installed": "/opt/messagebox/messagebox/x.py", "sha256": "0" * 64}])
        self.assertEqual(cloud_health.check_sounds(), (True, []))

    def test_sounds_bad_lists_pack_and_line_only_and_is_capped(self):
        entries = [self.sound("sounds/voices/pirate/voice-online.wav",
                              "/opt/messagebox/sounds/voices/pirate/voice-online.wav", b"changed")]
        entries += [{"source": f"sounds/voice/voice-{n:02d}.wav", "installed": f"/opt/messagebox/sounds/voice/voice-{n:02d}.wav",
                     "sha256": "0" * 64} for n in range(20)]  # Missing files are bad too.
        self.manifest(entries)
        ok, bad = cloud_health.check_sounds()
        self.assertIs(ok, False)
        self.assertEqual(len(bad), cloud_health.MAX_BAD)
        self.assertIn("pirate/voice-online.wav", bad)
        self.assertTrue(all(cloud_health._BAD.fullmatch(name) and "/opt" not in name for name in bad))

    def test_no_manifest_reports_unknown(self):
        check = cloud_health.SoundCheck(clock=lambda: NOW)
        check.run_once()
        self.assertEqual(check.result, {"ok": None, "bad": [], "checked_at": None})
        self.manifest([self.sound("sounds/cues/cue-sent.wav", "/opt/messagebox/sounds/cues/cue-sent.wav")])
        check.run_once()
        self.assertEqual(check.result, {"ok": True, "bad": [], "checked_at": NOW})

    def test_schedule_is_once_after_start_then_every_six_hours(self):
        waits, runs = [], []

        class Stop:
            def wait(self, delay):
                waits.append(delay)
                return len(waits) > 3
        check = cloud_health.SoundCheck(check=lambda: runs.append(1) or (True, []), clock=lambda: NOW)
        with mock.patch.object(cloud_health, "lower_priority") as lower:
            check.loop(Stop())
        lower.assert_called_once()
        self.assertEqual(waits, [cloud_health.SOUND_CHECK_FIRST_S] + [6 * 3600] * 3)
        self.assertEqual(len(runs), 3)

    def test_check_runs_at_lowest_priority_on_a_daemon_thread(self):
        calls = []
        with mock.patch.object(cloud_health.os, "setpriority", side_effect=lambda *a: calls.append(a), create=True):
            stop = threading.Event()
            stop.set()
            thread = cloud_health.SoundCheck(check=lambda: (True, [])).start(stop)
            thread.join(2)
        self.assertTrue(thread.daemon)
        self.assertEqual(calls[0][2], 19)

    def test_failed_check_keeps_the_last_result(self):
        check = cloud_health.SoundCheck(check=mock.Mock(side_effect=[(False, ["cues/cue-sent.wav"]), OSError]))
        stop = mock.Mock()
        stop.wait.side_effect = [False, False, True]
        with mock.patch.object(cloud_health, "lower_priority"):
            check.loop(stop)
        self.assertEqual(check.result["bad"], ["cues/cue-sent.wav"])


def systemctl(blocks, returncode=0):
    def run(command, **_kwargs):
        if command[0] == "timedatectl":
            return SimpleNamespace(returncode=0, stdout="yes\n")
        return SimpleNamespace(returncode=returncode, stdout="\n\n".join(
            "\n".join(f"{key}={value}" for key, value in block.items()) for block in blocks))
    return run


class SystemHealthTests(HealthTestCase):
    def unit(self, name, restarts, enabled="enabled", load="loaded"):
        return {"Id": f"messagebox-{name}.service", "LoadState": load, "UnitFileState": enabled, "NRestarts": restarts}

    def test_units_are_read_in_one_call_and_named_without_prefix(self):
        calls = []
        run = systemctl([self.unit("poller", 2), self.unit("sync", 0), self.unit("dash", 0, load="not-found")])
        units = cloud_health.read_units(lambda command, **kw: calls.append(command) or run(command, **kw))
        self.assertEqual(len(calls), 1)
        self.assertEqual(units, {"poller": {"restarts": 2, "enabled": True}, "sync": {"restarts": 0, "enabled": True}})

    def test_restarts_24h_is_a_delta_against_a_baseline(self):
        first = cloud_health.restarts_24h({"poller": 5, "sync": 1}, now=NOW, boot="b", uptime=3 * 86400)
        self.assertEqual(first, {"poller": 0, "sync": 0})
        later = cloud_health.restarts_24h({"poller": 7, "sync": 1}, now=NOW + 7200, boot="b", uptime=3 * 86400)
        self.assertEqual(later, {"poller": 2, "sync": 0})
        # The first sample leaves the window; the next one becomes the baseline.
        self.assertEqual(cloud_health.restarts_24h({"poller": 9, "sync": 1}, now=NOW + 86400 + 60,
                                                   boot="b", uptime=4 * 86400), {"poller": 2, "sync": 0})

    def test_restarts_after_reboot_count_from_zero(self):
        cloud_health.restarts_24h({"poller": 50}, now=NOW, boot="old", uptime=86400 * 9)
        self.assertEqual(cloud_health.restarts_24h({"poller": 3}, now=NOW + 600, boot="new", uptime=500),
                         {"poller": 3})

    def test_nfc_ok_follows_the_reader_heartbeat_file(self):
        enabled = {"nfc": {"restarts": 0, "enabled": True}}
        self.assertEqual(cloud_health._nfc(None), {"ok": None})
        self.assertEqual(cloud_health._nfc({"nfc": {"restarts": 0, "enabled": False}}), {"ok": None})
        self.assertEqual(cloud_health._nfc(enabled), {"ok": False})
        cloud_health.NFC_HEALTH_FILE.write_text("")
        self.assertEqual(cloud_health._nfc(enabled), {"ok": True})
        self.assertEqual(cloud_health._nfc(enabled, now=time.time() + 120), {"ok": False})

    def test_clock_synced(self):
        self.assertIs(cloud_health.read_clock(lambda *a, **k: SimpleNamespace(returncode=0, stdout="yes\n")), True)
        self.assertIs(cloud_health.read_clock(lambda *a, **k: SimpleNamespace(returncode=0, stdout="no\n")), False)
        self.assertIsNone(cloud_health.read_clock(lambda *a, **k: SimpleNamespace(returncode=1, stdout="")))

    def test_disk_memory_and_uptime(self):
        meminfo = self.root / "meminfo"
        meminfo.write_text("MemTotal:  948304 kB\nMemAvailable:  215040 kB\n")
        uptime = self.root / "uptime"
        uptime.write_text("12345.67 40000.00\n")
        self.assertEqual(cloud_health._memory(meminfo), {"free_mb": 210})
        self.assertEqual(cloud_health._uptime(uptime), 12345)
        self.assertIsNone(cloud_health._uptime(self.root / "missing"))
        self.assertEqual(cloud_health._memory(self.root / "missing"), {"free_mb": None})
        with mock.patch.object(cloud_health, "DATA_DIR", self.root):
            self.assertGreaterEqual(cloud_health._disk()["free_mb"], 0)

    def test_collect_reports_every_field_with_cached_subprocesses(self):
        calls = []
        run = systemctl([self.unit("poller", 1), self.unit("nfc", 0)])
        def counted(command, **kwargs):
            calls.append(command[0])
            return run(command, **kwargs)
        cloud_health.record_audio("cue", "play_failed", now=int(time.time()))
        cloud_health.NFC_HEALTH_FILE.write_text("")
        with mock.patch.object(cloud_health.subprocess, "run", counted), \
             mock.patch.object(cloud_health, "_uptime", return_value=3 * 86400):
            health = cloud_health.collect()
            cloud_health.collect()
        self.assertEqual(sorted(calls), ["systemctl", "timedatectl"])  # Cached for the next heartbeat.
        self.assertEqual(health["version"], 1)
        self.assertEqual(health["audio"]["failures_24h"]["cue"], 1)
        self.assertEqual(set(health["sounds"]), {"ok", "bad", "checked_at"})
        self.assertEqual(health["services"], {"restarts_24h": {"poller": 0, "nfc": 0}})
        self.assertEqual(health["nfc"], {"ok": True})
        self.assertEqual(health["clock"], {"synced": True})
        self.assertIsInstance(health["disk"]["free_mb"], int)

    def test_collection_failures_report_null_and_never_raise(self):
        def broken(*_args, **_kwargs):
            raise OSError("unavailable")
        with mock.patch.object(cloud_health.subprocess, "run", broken), \
             mock.patch.object(cloud_health, "_memory", broken), \
             mock.patch.object(cloud_health, "_disk", broken), \
             mock.patch.object(cloud_health, "_uptime", broken), \
             mock.patch.object(cloud_health, "audio_report", broken):
            health = cloud_health.heartbeat_fields()["health"]
        self.assertEqual(health["memory"], {"free_mb": None})
        self.assertEqual(health["disk"], {"free_mb": None})
        self.assertIsNone(health["uptime_s"])
        self.assertEqual(health["services"], {"restarts_24h": None})
        self.assertEqual(health["nfc"], {"ok": None})
        self.assertEqual(health["clock"], {"synced": None})
        self.assertIsNone(health["audio"]["ok"])
        with mock.patch.object(cloud_health, "collect", broken):
            self.assertEqual(cloud_health.heartbeat_fields(), {})

    def test_worst_case_health_fits_2048_bytes(self):
        health = worst_case_health()
        self.assertGreater(len(json.dumps(health, separators=(",", ":"))), cloud_health.MAX_BYTES)
        fitted = cloud_health.fit(health)
        self.assertLessEqual(len(json.dumps(fitted, separators=(",", ":")).encode()), cloud_health.MAX_BYTES)
        self.assertTrue(fitted["sounds"]["bad"])


def worst_case_health():
    return {
        "version": 1,
        "audio": {"ok": False, "failures_24h": dict.fromkeys(cloud_health.AUDIO_KINDS, 10 ** 9),
                  "last_error": {"code": "e" * 64, "at": 4_102_444_799}},
        "sounds": {"ok": False, "bad": [f"{n:0>40}/{'l' * 80}" for n in range(10)], "checked_at": 4_102_444_799},
        "disk": {"free_mb": 10 ** 9}, "memory": {"free_mb": 10 ** 9},
        "services": {"restarts_24h": {f"{n:x>40}": 10 ** 9 for n in range(12)}},
        "nfc": {"ok": False}, "clock": {"synced": False}, "uptime_s": 10 ** 10,
    }


if __name__ == "__main__":
    unittest.main()
