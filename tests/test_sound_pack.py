from speaker_helpers import isolate_audio, assert_padded_playback
from mic_helpers import physical_capture
import contextlib
import json
import queue
import shutil
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
from messagebox.guided_reply import GuidedSession, OutboxStore, RecordingResult
from test_guided_reply import FakeIO

sound_pack = runtime.sound_pack
ROOT = Path(__file__).resolve().parents[1]


class SoundPackTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "sound-state.json"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(runtime, "button", types.SimpleNamespace(is_pressed=False), create=True))
        self.stack.enter_context(mock.patch.object(runtime, "led", mock.Mock(), create=True))
        self.stack.enter_context(mock.patch.object(runtime, "log_event"))
        self.stack.enter_context(mock.patch.object(runtime, "quiet_hours", return_value=False))
        self.stack.enter_context(mock.patch.object(runtime, "presence"))
        online_transition = sound_pack.online_transition
        self.stack.enter_context(mock.patch.object(sound_pack, "online_transition", side_effect=lambda *a, **kw: online_transition(*a, path=self.state, **kw)))
        consume = sound_pack.consume_moment
        self.stack.enter_context(mock.patch.object(sound_pack, "consume_moment", side_effect=lambda name, **kw: consume(name, self.state, **kw)))

    def test_pack_rejects_missing_stereo_truncated_and_wrong_checksums(self):
        sound_pack.validate_sounds(ROOT / "sounds")
        for damage in ("missing", "stereo", "truncated", "checksum", "duration", "symlink"):
            with self.subTest(damage=damage):
                target = self.root / damage
                shutil.copytree(ROOT / "sounds", target)
                asset = target / "cues/cue-press.wav"
                if damage == "missing":
                    asset.unlink()
                elif damage == "symlink":
                    asset.unlink()
                    asset.symlink_to(ROOT / "sounds/cues/cue-press.wav")
                elif damage in ("stereo", "duration"):
                    with wave.open(str(asset), "wb") as output:
                        output.setparams((2 if damage == "stereo" else 1, 2, 48000, 0, "NONE", ""))
                        output.writeframes(b"\0" * 400)
                elif damage == "truncated":
                    asset.write_bytes(asset.read_bytes()[:60])
                else:
                    asset.write_bytes(asset.read_bytes()[:-2] + b"xx")
                with self.assertRaisesRegex(ValueError, "cues"):
                    sound_pack.validate_sounds(target)
                shutil.rmtree(target)

    def test_current_pack_paths_unknown_default_and_missing_file_logs_once(self):
        with mock.patch.object(sound_pack, "SOUND_DIR", ROOT / "sounds"), \
             mock.patch.object(sound_pack._settings, "snapshot", return_value={"voice_pack": "dj"}):
            self.assertEqual(sound_pack.voice_path("count-new"), ROOT / "sounds/voices/dj/voice-count-new.wav")
            self.assertEqual(sound_pack.voice_path("listened", "unknown"), ROOT / "sounds/voice/voice-listened.wav")
        with mock.patch.object(sound_pack, "SOUND_DIR", self.root), \
             mock.patch.object(sound_pack, "_missing_voice_files", set()), \
             self.assertLogs("messagebox.sound_pack", level="WARNING") as logs:
            for _ in range(2):
                self.assertEqual(sound_pack.voice_path("msg-start", "alien"), self.root / "voice/voice-msg-start.wav")
            self.assertEqual(len(logs.output), 1)
        with self.assertRaises(ValueError):
            sound_pack.voice_path("../private")

    def test_capabilities_only_advertise_complete_validated_packs(self):
        from messagebox.cloud_device import capabilities
        import importlib
        capability_sounds = importlib.import_module("messagebox.sound_pack")
        from messagebox.settings import VOICE_PACKS
        target = self.root / "sounds"
        shutil.copytree(ROOT / "sounds", target)
        with mock.patch.object(capability_sounds, "SOUND_DIR", target):
            self.assertEqual(capabilities()["voice_packs"], list(VOICE_PACKS))
            (target / "voices/dj/voice-count-new.wav").unlink()
            (target / "voices/alien/voice-online.wav").write_bytes(b"damaged")
            self.assertEqual(capabilities()["voice_packs"], ["jessica", "pirate", "robot", "french", "charlie", "tata", "buttonbox"])
            shutil.rmtree(target / "voices")
            self.assertEqual(capabilities()["voice_packs"], ["jessica"])

    def test_install_requires_valid_audio_and_manifest_for_every_voice_pack(self):
        from messagebox.settings import VOICE_PACKS
        target = self.root / "sounds"
        shutil.copytree(ROOT / "sounds", target)
        sound_pack.validate_sounds(target)
        for pack in VOICE_PACKS[1:]:
            for name in ("manifest.json", "voice-count-new.wav"):
                with self.subTest(pack=pack, file=name):
                    path = target / "voices" / pack / name
                    original = path.read_bytes()
                    path.write_bytes(b"invalid")
                    with self.assertRaisesRegex(ValueError, pack):
                        sound_pack.validate_sounds(target)
                    path.write_bytes(original)

    def test_runtime_can_restart_with_missing_optional_pack_but_install_rejects_it(self):
        target = self.root / "sounds"
        shutil.copytree(ROOT / "sounds", target)
        shutil.rmtree(target / "voices/dj")
        with mock.patch.object(sound_pack, "SOUND_DIR", target):
            runtime.validate_sounds()
            with self.assertRaisesRegex(ValueError, "dj"):
                sound_pack.validate_sounds()
            (target / "voice/voice-listened.wav").unlink()
            with self.assertRaisesRegex(ValueError, "voice"):
                runtime.validate_sounds()

    def test_recording_flow_has_only_go_cue_before_microphone(self):
        order = []
        recorder = mock.Mock()
        recorder.communicate.return_value = (b"", b"")
        vad = mock.Mock()
        vad.trim_bounds.return_value = None
        def play(command, **kwargs):
            order.append(Path(command[-1]).name)
            return types.SimpleNamespace(returncode=0)
        def capture(command, **kwargs):
            self.assertEqual(order, ["cue-rec_go.wav"])
            order.append("mic")
            return recorder
        with mock.patch.object(runtime, "TEMP_DIR", str(self.root)), \
             mock.patch.object(runtime.subprocess, "run", side_effect=play), \
             mock.patch.object(runtime.subprocess, "Popen", side_effect=capture), \
             mock.patch.object(runtime.time, "monotonic", side_effect=[0, 60, 60]), \
             mock.patch.object(runtime, "EnergyVAD", return_value=vad), \
             mock.patch.object(runtime, "RecordingLimitCue"), \
             mock.patch.object(runtime.select, "select", return_value=([], [], [])):
            io = runtime.PiGuidedIO("synthetic", "session", 60)
            io.record = lambda: physical_capture(runtime, "synthetic", "session", 60)
            session = GuidedSession(io,
                                    OutboxStore(self.root / "outbox"), lambda *a, **kw: None)
            result = session.run(recipient="synthetic", flow_kind="standalone",
                                 deleted_cue_path=str(sound_pack.cue_path("deleted")))
        self.assertEqual(result, "deleted")
        self.assertEqual(order, ["cue-rec_go.wav", "mic", "cue-deleted.wav"])
        vad.start.assert_called_once_with(0)

    def test_setup_voice_is_buttonbox_and_once_despite_old_online_receipt(self):
        self.state.write_text('{"online":true}')
        def play(command, **kwargs):
            assert_padded_playback(self, command[-1], ROOT / "sounds/voices/buttonbox/voice-setup-scan.wav")
            return types.SimpleNamespace(returncode=0)
        with mock.patch.object(runtime.cloud_claim, "setup_online_cue", return_value=True), \
             mock.patch.object(sound_pack, "SOUND_DIR", ROOT / "sounds"), \
             mock.patch.object(sound_pack, "current_voice_pack", return_value="alien"), \
             mock.patch.object(runtime.subprocess, "run", side_effect=play) as run:
            runtime.play_setup_online(runtime.claim_beeps())
            runtime.play_setup_online(runtime.claim_beeps())
        self.assertEqual([Path(call.args[0][-1]).name for call in run.call_args_list],
                         ["voice-setup-scan.wav"])

    def test_online_ready_edges_survive_restart_and_rate_limit_flapping(self):
        def edge(boot, online, mono, wall, audible=True):
            return sound_pack.online_transition(boot, online, mono=mono, wall=wall, audible=audible)
        self.assertTrue(edge("boot", True, 10, 1000))
        self.assertFalse(edge("boot", True, 700, 1690))  # Service restart, still online.
        self.assertFalse(edge("boot", False, 3700, 4690))
        self.assertTrue(edge("boot", True, 3702, 4692))
        self.assertFalse(edge("boot", False, 4000, 4990))
        self.assertFalse(edge("boot", True, 4100, 5090))  # Second outage within the hour.
        self.assertFalse(edge("boot", True, 9000, 9990))  # No delayed flap replay.
        self.assertFalse(edge("next-boot", True, 2, 5100))  # Reboot within cooldown.
        self.assertTrue(edge("later-boot", True, 2, 8400))
        self.assertFalse(edge("clock-back", True, 2, 1200))

    def test_runtime_online_ready_uses_current_pack_and_silent_edges_stay_silent(self):
        cloud = types.SimpleNamespace(state={"snapshot": {"boot_id": "boot", "verified_mono": 10}}, boot_id="boot")
        with mock.patch.object(runtime, "transport_mode", return_value="cloud"), \
             mock.patch.object(runtime.cloud_runtime, "CloudRuntime", return_value=cloud), \
             mock.patch.object(runtime, "caregiver_settings", return_value={"master_volume_percent": 50}) as settings, \
             mock.patch.object(runtime.time, "monotonic", return_value=10), \
             mock.patch.object(sound_pack, "SOUND_DIR", ROOT / "sounds"), \
             mock.patch.object(sound_pack, "current_voice_pack", return_value="pirate"), \
             mock.patch.object(runtime, "play_audio_ordinary") as play:
            self.assertTrue(runtime.maybe_play_connectivity())
            self.assertFalse(runtime.maybe_play_connectivity())
            self.assertEqual(play.call_args_list, [mock.call(ROOT / "sounds/cues/cue-connected.wav"),
                                                 mock.call(ROOT / "sounds/voices/pirate/voice-online-ready.wav")])
            for boot, quiet, volume in (("quiet", True, 50), ("mute", False, 0)):
                cloud.boot_id = cloud.state["snapshot"]["boot_id"] = boot
                settings.return_value = {"master_volume_percent": volume}
                with mock.patch.object(runtime, "quiet_hours", return_value=quiet):
                    self.assertFalse(runtime.maybe_play_connectivity())
                settings.return_value = {"master_volume_percent": 50}
                self.assertFalse(runtime.maybe_play_connectivity())
            self.assertEqual(play.call_count, 2)

    def test_wacli_connectivity_stays_silent_and_legacy_online_voice_is_unchanged(self):
        with mock.patch.object(runtime, "transport_mode", return_value="wacli"), \
             mock.patch.object(runtime.cloud_runtime, "CloudRuntime") as cloud, \
             mock.patch.object(runtime, "play_moment") as play:
            self.assertFalse(runtime.maybe_play_connectivity())
            cloud.assert_not_called()
            sound_pack.online_transition.assert_not_called()
            runtime.announce_first_online()
            runtime.announce_first_online()
            play.assert_called_once_with(voice="online")

    def test_message_voice_intro_is_cloud_opt_in_current_pack_and_never_quiet(self):
        french = {"master_volume_percent": 50, "voice_pack": "french"}
        cases = (
            ("cloud", {**french, "announce_messages_voice": True}, False,
             ROOT / "sounds/voices/french/voice-msg-start.wav"),
            ("cloud", {**french, "announce_messages_voice": False}, False, None),
            ("cloud", french, False, None),  # Missing setting: today's cue only.
            ("cloud", {**french, "announce_messages_voice": True}, True, None),  # Quiet or mute.
            ("wacli", {**french, "announce_messages_voice": True}, False, None),
        )
        for mode, settings, quiet, expected in cases:
            with self.subTest(mode=mode, settings=settings, quiet=quiet), \
                 mock.patch.object(runtime, "transport_mode", return_value=mode), \
                 mock.patch.object(runtime, "caregiver_settings", return_value=settings), \
                 mock.patch.object(runtime, "quiet_hours", return_value=quiet), \
                 mock.patch.object(sound_pack, "SOUND_DIR", ROOT / "sounds"), \
                 mock.patch.object(runtime, "play_audio_ordinary") as play:
                self.assertEqual(runtime.message_intro_voice(), expected)
                runtime.play_message_start()
                self.assertEqual(play.call_args_list,
                                 [mock.call(ROOT / "sounds/cues/cue-msg_start.wav")]
                                 + ([mock.call(expected)] if expected else []))

    def test_message_age_line_follows_the_intro_only_on_cloud_and_never_quiet(self):
        old = {"cloud_created_at": 1_760_000_000}
        played_at = old["cloud_created_at"] + 86400
        pirate = {"master_volume_percent": 50, "voice_pack": "pirate", "timezone": "Europe/Lisbon"}
        line = ROOT / "sounds/voices/pirate/voice-age-yesterday.wav"
        cases = (
            ("cloud", old, played_at, False, line),
            ("cloud", old, None, False, None),                          # Clock not verified.
            ("cloud", old, played_at, True, None),                      # Mute, Quiet now or schedule.
            ("cloud", {}, played_at, False, None),                      # No send time.
            ("cloud", old, old["cloud_created_at"] + 3600, False, None),  # Too new.
            ("cloud", None, played_at, False, None),
            ("wacli", old, played_at, False, None),
        )
        for mode, metadata, now, quiet, expected in cases:
            with self.subTest(mode=mode, metadata=metadata, now=now, quiet=quiet), \
                 mock.patch.object(runtime, "transport_mode", return_value=mode), \
                 mock.patch.object(runtime, "caregiver_settings", return_value=pirate), \
                 mock.patch.object(runtime, "quiet_hours", return_value=quiet), \
                 mock.patch.object(runtime.cloud_runtime, "verified_now", return_value=now), \
                 mock.patch.object(sound_pack, "SOUND_DIR", ROOT / "sounds"):
                self.assertEqual(runtime.message_age_voice(metadata), expected)

    def test_message_start_order_is_cue_intro_age_then_message(self):
        settings = {"master_volume_percent": 50, "voice_pack": "jessica", "timezone": "Europe/Lisbon",
                    "announce_messages_voice": True}
        metadata = {"cloud_created_at": 1_760_000_000}
        with mock.patch.object(runtime, "transport_mode", return_value="cloud"), \
             mock.patch.object(runtime, "caregiver_settings", return_value=settings), \
             mock.patch.object(runtime, "quiet_hours", return_value=False), \
             mock.patch.object(runtime.cloud_runtime, "verified_now", return_value=1_760_000_000 + 2 * 86400), \
             mock.patch.object(sound_pack, "SOUND_DIR", ROOT / "sounds"), \
             mock.patch.object(runtime, "play_audio_ordinary") as play:
            runtime.play_message_start(metadata)
        self.assertEqual(play.call_args_list, [
            mock.call(ROOT / "sounds/cues/cue-msg_start.wav"),
            mock.call(ROOT / "sounds/voice/voice-msg-start.wav"),
            mock.call(ROOT / "sounds/voice/voice-age-2-days-ago.wav"),
        ])

    def test_guided_playback_says_the_age_between_intro_and_message(self):
        io = FakeIO(recordings=[RecordingResult(None, 0, False)])
        with tempfile.TemporaryDirectory() as directory:
            session = GuidedSession(io, OutboxStore(directory), lambda *a, **kw: None)
            session.run(recipient="synthetic", flow_kind="reply", deleted_cue_path="deleted",
                        incoming_path="message", incoming_cue_path="start", incoming_voice_path="intro",
                        incoming_age_path="age", incoming_end_path="end", auto_record_after_incoming=False)
        self.assertEqual([call[1] for call in io.calls[:5]], ["start", "intro", "age", "message", "end"])

    def test_all_set_waits_for_completion_and_runs_once_across_restart(self):
        request = self.root / "setup-complete.json"
        with mock.patch.object(sound_pack, "ALL_SET_REQUEST", request), mock.patch.object(runtime, "play_moment") as play:
            runtime.announce_all_set()
            play.assert_not_called()
            request.write_text('{"complete":true}')
            runtime.announce_all_set()
            runtime.announce_all_set()
            play.assert_called_once_with("all_set", "all-set")

    def test_incoming_message_bookends_precede_recording_without_countdown(self):
        io = FakeIO(recordings=[RecordingResult(None, 0, False)])
        with tempfile.TemporaryDirectory() as directory:
            session = GuidedSession(io, OutboxStore(directory), lambda *a, **kw: None)
            session.run(recipient="synthetic", flow_kind="reply", deleted_cue_path="deleted",
                        incoming_path="message", incoming_cue_path="start", incoming_end_path="end")
        self.assertEqual(io.calls[:4], [("ordinary", "start"), ("ordinary", "message"),
                                       ("ordinary", "end"), ("record",)])

    def test_quiet_hours_block_listened_still_trying_offline_but_press_answers_play(self):
        snapshot = {"boot_id": "boot", "verified_mono": 0}
        cloud = types.SimpleNamespace(state={"snapshot": snapshot}, boot_id="boot")
        with mock.patch.object(runtime, "quiet_hours", return_value=True), \
             mock.patch.object(runtime, "receipt_store", create=True) as receipts, \
             mock.patch.object(runtime, "play_moment") as play, \
             mock.patch.object(runtime, "transport_mode", return_value="cloud"), \
             mock.patch.object(runtime.cloud_runtime, "CloudRuntime", return_value=cloud), \
             mock.patch.object(runtime.time, "monotonic", return_value=700), \
             mock.patch.object(runtime, "_offline_announced", False), \
             mock.patch.object(runtime, "stuck_notices", queue.SimpleQueue()), \
             mock.patch.object(runtime, "beep") as beep:
            self.assertEqual(runtime.play_pending_listened(), 0)
            self.assertFalse(runtime.maybe_play_still_trying())
            self.assertFalse(runtime.maybe_play_connectivity())
            receipts.claim_next.assert_not_called()
            play.assert_not_called()
            runtime.acknowledge_guided_press("start")
            beep.assert_called_once_with("press")
            runtime.prompt_for_token()
            # Press response remains audible during quiet hours.
            play.assert_called_once_with(voice="card-needed")

    def test_existing_snapshot_drives_offline_once_and_recovery(self):
        cloud = types.SimpleNamespace(state={"snapshot": {"boot_id": "boot", "verified_mono": 0}}, boot_id="boot")
        with mock.patch.object(runtime, "transport_mode", return_value="cloud"), \
             mock.patch.object(runtime.cloud_runtime, "CloudRuntime", return_value=cloud), \
             mock.patch.object(runtime, "caregiver_settings", return_value={"master_volume_percent": 50}), \
             mock.patch.object(runtime.time, "monotonic", return_value=599) as now, \
             mock.patch.object(runtime, "_offline_announced", False), \
             mock.patch.object(runtime, "play_moment") as play:
            self.assertFalse(runtime.maybe_play_connectivity())
            now.return_value = 600
            self.assertTrue(runtime.maybe_play_connectivity())
            self.assertFalse(runtime.maybe_play_connectivity())
            runtime._offline_announced = False  # Service restart, same heartbeat.
            self.assertFalse(runtime.maybe_play_connectivity())
            cloud.state["snapshot"]["verified_mono"] = 600
            self.assertTrue(runtime.maybe_play_connectivity())
            self.assertEqual(play.call_args_list, [mock.call("offline"), mock.call("connected", "online-ready")])
            cloud.state["snapshot"] = None
            self.assertFalse(runtime.maybe_play_connectivity())

    def test_short_heartbeat_gaps_never_say_offline_or_online(self):
        # Heartbeats can go stale for 1-9 minutes many times a day.
        snapshot = {"boot_id": "boot", "verified_mono": 0}
        cloud = types.SimpleNamespace(state={"snapshot": snapshot}, boot_id="boot")
        with mock.patch.object(runtime, "transport_mode", return_value="cloud"), \
             mock.patch.object(runtime.cloud_runtime, "CloudRuntime", return_value=cloud), \
             mock.patch.object(runtime, "caregiver_settings", return_value={"master_volume_percent": 50}), \
             mock.patch.object(runtime.time, "monotonic", return_value=0) as now, \
             mock.patch.object(runtime, "_offline_announced", False), \
             mock.patch.object(runtime, "play_moment") as play:
            self.assertTrue(runtime.maybe_play_connectivity())  # First check-in after boot.
            for _ in range(12):  # Twelve 9-minute gaps, each followed by a good heartbeat.
                now.return_value = snapshot["verified_mono"] + 590
                self.assertFalse(runtime.maybe_play_connectivity())
                snapshot["verified_mono"] = now.return_value
                self.assertFalse(runtime.maybe_play_connectivity())
            self.assertEqual(play.call_args_list, [mock.call("connected", "online-ready")])

    def test_listened_uses_cue_then_family_clip_or_default_voice(self):
        old_default = str(runtime.APP_DIR / "sounds/listen-receipts/someone-listened.wav")
        for clip in ("", old_default, "/synthetic/family.wav"):
            notice = types.SimpleNamespace(clip=clip, listener_name="Synthetic")
            receipts = mock.Mock()
            receipts.claim_next.side_effect = [notice, None]
            order = []
            with mock.patch.object(runtime, "receipt_store", receipts, create=True), \
                 mock.patch.object(runtime.os.path, "exists", return_value=True), \
                 mock.patch.object(runtime, "play_moment", side_effect=lambda cue: order.append(cue)), \
                 mock.patch.object(runtime, "play_audio_ordinary", side_effect=lambda path: order.append(path)):
                self.assertEqual(runtime.play_pending_listened(), 1)
            expected = runtime.LISTENED_FALLBACK_WAV if clip in ("", old_default) else clip
            self.assertEqual(order, ["listened", expected])
            receipts.complete.assert_called_once_with(notice)

    def test_limit_warning_at_five_seconds_for_all_limits_stays_in_the_recording(self):
        # Dan, 2026-10-07: keep the warning in the recording so no words are lost.
        self.assertFalse(hasattr(sound_pack, "mute_pcm"))
        for maximum in (30, 60, 120):
            with self.subTest(maximum=maximum), mock.patch.object(runtime.subprocess, "Popen") as popen:
                process = popen.return_value
                process.poll.side_effect = [None, 0]
                cue = runtime.RecordingLimitCue(10, maximum)
                cue.update(10 + maximum - 5.01)
                popen.assert_not_called()
                cue.update(10 + maximum - 5)
                popen.assert_called_once()
                self.assertEqual(Path(popen.call_args.args[0][-1]).name, "cue-rec_limit.wav")
                cue.update(10 + maximum - 4.45)
                self.assertEqual(cue.intervals[0][0], maximum - 5)
                self.assertAlmostEqual(cue.intervals[0][1], maximum - 4.45)

    def test_rec_go_playback_completes_before_arecord_starts(self):
        order = []
        def run(command, **kw):
            self.assertEqual(command[0], "aplay")
            self.assertEqual(Path(command[-1]).name, "cue-rec_go.wav")
            order.append("cue_finished")
            return subprocess.CompletedProcess(command, 0)
        def popen(command, **kw):
            self.assertEqual(command[0], "arecord")
            self.assertEqual(order, ["cue_finished"])
            order.append("arecord")
            process = mock.Mock()
            process.communicate.return_value = (b"", None)
            return process
        vad = mock.Mock()
        vad.silence_expired.return_value = True
        vad.trim_bounds.return_value = None
        with mock.patch.object(runtime, "TEMP_DIR", str(self.root)), \
             mock.patch.object(runtime.time, "monotonic", side_effect=[0, 60, 60]), \
             mock.patch.object(runtime, "RecordingLimitCue"), \
             mock.patch.object(runtime.subprocess, "run", side_effect=run), \
             mock.patch.object(runtime.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(runtime.select, "select", return_value=([], [], [])), \
             mock.patch.object(runtime, "EnergyVAD", return_value=vad):
            self.assertFalse(physical_capture(runtime, "synthetic").meaningful)
        self.assertEqual(order, ["cue_finished", "arecord"])

    def test_failed_go_cue_never_opens_microphone(self):
        with mock.patch.object(runtime.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "aplay")), \
             mock.patch.object(runtime.subprocess, "Popen") as popen:
            with self.assertRaises(subprocess.CalledProcessError):
                physical_capture(runtime, "synthetic")
        popen.assert_not_called()

    def test_still_trying_deferred_during_capture_and_once_per_job(self):
        notices = queue.SimpleQueue()
        key = ("guided", "synthetic-job")
        notices.put(key)
        with mock.patch.object(runtime, "stuck_notices", notices), \
             mock.patch.object(runtime, "compatible_guided_jobs", return_value=[types.SimpleNamespace(message_id=key[1])]), \
             mock.patch.object(runtime, "play_moment") as play, \
             mock.patch.object(runtime, "_recording", True):
            self.assertFalse(runtime.maybe_play_still_trying())
            runtime._recording = False
            runtime._guided_active = True
            self.assertFalse(runtime.maybe_play_still_trying())
            runtime._guided_active = False
            self.assertTrue(runtime.maybe_play_still_trying())
            notices.put(key)
            self.assertFalse(runtime.maybe_play_still_trying())
            play.assert_called_once_with("still_trying", "stuck")

    def test_sender_queues_stuck_only_after_three_failures_in_both_modes(self):
        for guided in (True, False):
            with self.subTest(guided=guided):
                notices = queue.SimpleQueue()
                job = types.SimpleNamespace(message_id="synthetic-guided")
                sleeps = []
                def sleep(seconds):
                    sleeps.append(seconds)
                    if len(sleeps) == 4:
                        raise InterruptedError("end synthetic loop")
                with mock.patch.object(runtime, "transport_mode", return_value="wacli"), \
                     mock.patch.object(runtime, "compatible_guided_jobs", return_value=[job] if guided else []), \
                     mock.patch.object(runtime, "compatible_legacy_outbox_files", return_value=[] if guided else ["synthetic.wav"]), \
                     mock.patch.object(runtime, "send_guided_job", return_value=False) as send_guided, \
                     mock.patch.object(runtime, "send_legacy_outbox_file", return_value=False) as send_legacy, \
                     mock.patch.object(runtime, "stuck_notices", notices), \
                     mock.patch.object(runtime.time, "sleep", side_effect=sleep), \
                     mock.patch.object(runtime, "beep") as beep:
                    with self.assertRaises(InterruptedError):
                        runtime.sender_loop()
                self.assertEqual(notices.get_nowait(), ("guided", job.message_id) if guided else ("legacy", "synthetic.wav"))
                self.assertTrue(notices.empty())
                self.assertEqual(sleeps, [5, 10, 15, 20])
                self.assertEqual((send_guided if guided else send_legacy).call_count, 4)
                beep.assert_not_called()

    def test_guided_saved_wav_keeps_warning_and_words(self):
        pcm = struct.pack("<h", 5000) * (60 * 16000)
        recorder = mock.Mock()
        recorder.poll.return_value = None
        recorder.communicate.return_value = (pcm, None)
        warning = mock.Mock()
        warning.poll.side_effect = [None, 0]
        vad = mock.Mock()
        vad.silence_expired.return_value = False
        vad.trim_bounds.return_value = (0, 60 * 16000)
        with mock.patch.object(runtime, "TEMP_DIR", str(self.root)), \
             mock.patch.object(runtime.subprocess, "run"), \
             mock.patch.object(runtime.subprocess, "Popen", side_effect=[recorder, warning]), \
             mock.patch.object(runtime.time, "monotonic", side_effect=[0, 55, 55.55, 60, 60, 60]), \
             mock.patch.object(runtime.select, "select", return_value=([], [], [])), \
             mock.patch.object(runtime, "EnergyVAD", return_value=vad):
            result = physical_capture(runtime, "synthetic", max_seconds=60)
        with wave.open(result.path, "rb") as saved:
            actual = saved.readframes(saved.getnframes())
        # The recording is saved unchanged; speech during the warning is kept.
        self.assertEqual(actual, pcm)
        self.assertEqual(vad.start.call_count, 1)

    def test_hold_recording_saved_wav_keeps_warning_and_words(self):
        pcm = struct.pack("<h", 5000) * (30 * 16000)
        recorder = mock.Mock()
        recorder.poll.return_value = None
        recorder.communicate.return_value = (pcm, None)
        warning = mock.Mock()
        warning.poll.side_effect = [None, 0]
        vad = mock.Mock()
        vad.trim_bounds.return_value = (0, 30 * 16000)
        with mock.patch.object(runtime, "button", types.SimpleNamespace(is_pressed=True)), \
             mock.patch.object(runtime, "TEMP_DIR", str(self.root)), \
             mock.patch.object(runtime.subprocess, "run"), \
             mock.patch.object(runtime.subprocess, "Popen", side_effect=[recorder, warning]), \
             mock.patch.object(runtime.time, "monotonic", side_effect=[0, 25, 25.55, 30, 30, 30]), \
             mock.patch.object(runtime.select, "select", return_value=([], [], [])), \
             mock.patch.object(runtime, "EnergyVAD", return_value=vad):
            result = physical_capture(runtime, "synthetic", max_seconds=30, talk_mode="hold")
        self.assertFalse(result.timed_out)
        with wave.open(result.path, "rb") as source:
            self.assertEqual(source.readframes(source.getnframes()), pcm)


class MessageAgeLineTests(unittest.TestCase):
    """Every voice says every message age phrase offline, in English."""

    WHEN = {
        "this-morning": "this morning", "this-afternoon": "this afternoon", "this-evening": "this evening",
        "last-night": "last night", "yesterday": "yesterday", "2-days-ago": "two days ago",
        "3-days-ago": "three days ago", "4-days-ago": "four days ago", "5-days-ago": "five days ago",
        "6-days-ago": "six days ago", "last-week": "last week", "a-while-ago": "a while ago",
    }

    def directories(self, root=ROOT / "sounds"):
        from messagebox.settings import VOICE_PACKS
        return [root / "voice", *(root / "voices" / pack for pack in VOICE_PACKS[1:])]

    def test_every_voice_ships_every_age_line_with_its_manifest_transcript_and_checksum(self):
        import hashlib
        from messagebox.message_age import PHRASE_KEYS
        for directory in self.directories():
            with self.subTest(directory=directory.name):
                rows = {row["file"]: row for row in
                        (json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["age_lines"]
                         if directory.name != "buttonbox" else [row for row in json.loads((directory / "manifest.json").read_text())["lines"] if row["file"].startswith("voice-age-")])}
                self.assertEqual(set(rows), {f"voice-age-{key}.wav" for key in PHRASE_KEYS})
                for key in PHRASE_KEYS:
                    path = directory / f"voice-age-{key}.wav"
                    self.assertIn(f"from {self.WHEN[key]}", rows[path.name]["text"].lower())
                    self.assertEqual(rows[path.name]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                    self.assertTrue(sound_pack._playable_wav(path))

    def test_missing_age_lines_fall_back_to_jessica_then_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "sounds/voice", root / "voice")
            shutil.copytree(ROOT / "sounds/voices/robot", root / "voices/robot")
            with mock.patch.object(sound_pack, "SOUND_DIR", root):
                self.assertEqual(sound_pack.age_line_path("last-night", "robot"), root / "voices/robot/voice-age-last-night.wav")
                self.assertEqual(sound_pack.age_line_path("last-night", "jessica"), root / "voice/voice-age-last-night.wav")
                (root / "voices/robot/voice-age-last-night.wav").unlink()
                self.assertEqual(sound_pack.age_line_path("last-night", "robot"), root / "voice/voice-age-last-night.wav")
                (root / "voice/voice-age-last-night.wav").write_bytes(b"not a wav")
                self.assertIsNone(sound_pack.age_line_path("last-night", "robot"))
                self.assertIsNone(sound_pack.age_line_path("../voice-msg-start", "robot"))
                self.assertIsNone(sound_pack.age_line_path(None, "robot"))

    def test_age_lines_are_optional_for_installed_packs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sounds"
            shutil.copytree(ROOT / "sounds", root)
            for path in [*root.glob("voices/*/voice-age-*.wav"), *root.glob("voice/voice-age-*.wav")]:
                if "buttonbox" not in path.parts:
                    path.unlink()
            sound_pack.validate_sounds(root)
            from messagebox.settings import VOICE_PACKS
            self.assertEqual(sound_pack.installed_voice_packs(root), list(VOICE_PACKS))
            self.assertIsNone(sound_pack.age_line_path("last-night", "pirate"))


class ReactionSentLineTests(unittest.TestCase):
    """Reaction cards say "You just sent ..." in every voice, offline."""

    EXPECTED = {
        "heart": "You just sent a heart!", "big-laugh": "You just sent a big laugh!",
        "applause": "You just sent applause!", "heart-eyes": "You just sent heart eyes!",
        "thumbs-up": "You just sent a thumbs up!", "fire": "You just sent fire!",
        "celebration": "You just sent a celebration!", "kiss": "You just sent a kiss!",
        "surprise": "You just sent a surprise!", "tear": "You just sent a tear!",
        "hooray": "You just sent a hooray!", "smile": "You just sent a smile!",
        "reaction": "You just sent a reaction!",
    }

    def directories(self, root=ROOT / "sounds"):
        from messagebox.settings import VOICE_PACKS
        return [root / "voice", *(root / "voices" / pack for pack in VOICE_PACKS[1:])]

    def test_every_voice_ships_every_line_with_its_manifest_transcript_and_checksum(self):
        import hashlib
        self.assertEqual(set(sound_pack.REACTION_SENT_LINES.values()) | {sound_pack.GENERIC_REACTION_SENT},
                         set(self.EXPECTED))
        for directory in self.directories():
            with self.subTest(directory=directory.name):
                rows = {row["file"]: row for row in
                        (json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["reaction_lines"]
                         if directory.name != "buttonbox" else [row for row in json.loads((directory / "manifest.json").read_text())["lines"] if row["file"].startswith("voice-sent-")])}
                self.assertEqual(set(rows), {f"voice-sent-{key}.wav" for key in self.EXPECTED})
                for key, text in self.EXPECTED.items():
                    path = directory / f"voice-sent-{key}.wav"
                    self.assertEqual(rows[path.name]["text"], text)
                    self.assertEqual(rows[path.name]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                    self.assertTrue(sound_pack._playable_wav(path))

    def test_emoji_keys_ignore_variation_selectors_and_skin_tones(self):
        self.assertEqual(sound_pack.reaction_sent_key("❤️"), "heart")
        self.assertEqual(sound_pack.reaction_sent_key("❤"), "heart")
        self.assertEqual(sound_pack.reaction_sent_key("\U0001f44d\U0001f3fd"), "thumbs-up")
        self.assertEqual(sound_pack.reaction_sent_key("\U0001f680"), "reaction")
        self.assertEqual(sound_pack.reaction_sent_key(""), "reaction")
        self.assertEqual(sound_pack.reaction_sent_key(None), "reaction")

    def test_missing_lines_fall_back_to_jessica_then_generic_then_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "sounds/voice", root / "voice")
            shutil.copytree(ROOT / "sounds/voices/pirate", root / "voices/pirate")
            with mock.patch.object(sound_pack, "SOUND_DIR", root):
                heart = "❤️"
                self.assertEqual(sound_pack.reaction_sent_path(heart, "pirate"), root / "voices/pirate/voice-sent-heart.wav")
                (root / "voices/pirate/voice-sent-heart.wav").unlink()
                self.assertEqual(sound_pack.reaction_sent_path(heart, "pirate"), root / "voice/voice-sent-heart.wav")
                (root / "voice/voice-sent-heart.wav").write_bytes(b"not a wav")
                self.assertEqual(sound_pack.reaction_sent_path(heart, "pirate"), root / "voices/pirate/voice-sent-reaction.wav")
                (root / "voices/pirate/voice-sent-reaction.wav").unlink()
                (root / "voice/voice-sent-reaction.wav").unlink()
                self.assertIsNone(sound_pack.reaction_sent_path(heart, "pirate"))
                self.assertEqual(sound_pack.reaction_sent_path("\U0001f525", "jessica"), root / "voice/voice-sent-fire.wav")

    def test_optional_lines_never_block_a_voice_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sounds"
            shutil.copytree(ROOT / "sounds", root)
            for path in root.glob("voice*/**/voice-sent-*.wav"):
                if "buttonbox" not in path.parts:
                    path.unlink()
            for path in (root / "voice").glob("voice-sent-*.wav"):
                if "buttonbox" not in path.parts:
                    path.unlink()
            sound_pack.validate_sounds(root)
            self.assertIn("pirate", sound_pack.installed_voice_packs(root))
