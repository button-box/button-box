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
            self.assertEqual(capabilities()["voice_packs"], ["jessica", "pirate", "robot", "french", "charlie", "tata"])
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
            session = GuidedSession(runtime.PiGuidedIO("synthetic", "session", 60),
                                    OutboxStore(self.root / "outbox"), lambda *a, **kw: None)
            result = session.run(recipient="synthetic", flow_kind="standalone",
                                 deleted_cue_path=str(sound_pack.cue_path("deleted")))
        self.assertEqual(result, "deleted")
        self.assertEqual(order, ["cue-rec_go.wav", "mic", "cue-deleted.wav"])
        vad.start.assert_called_once_with(0)

    def test_online_voice_follows_connected_once_across_restart(self):
        with mock.patch.object(runtime.cloud_claim, "setup_online_cue", return_value=True), \
             mock.patch.object(runtime.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
            runtime.play_setup_online(runtime.claim_beeps())
            runtime.play_setup_online(runtime.claim_beeps())
        self.assertEqual([Path(call.args[0][-1]).name for call in run.call_args_list],
                         ["cue-connected.wav", "voice-online.wav", "cue-connected.wav"])
        self.assertTrue(json.loads(self.state.read_text())["online"])

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
             mock.patch.object(runtime.time, "monotonic", return_value=300), \
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
             mock.patch.object(runtime.time, "monotonic", return_value=179) as now, \
             mock.patch.object(runtime, "_offline_announced", False), \
             mock.patch.object(runtime, "_online_announced", False), \
             mock.patch.object(runtime, "play_moment") as play:
            self.assertFalse(runtime.maybe_play_connectivity())
            now.return_value = 180
            self.assertTrue(runtime.maybe_play_connectivity())
            self.assertFalse(runtime.maybe_play_connectivity())
            runtime._offline_announced = False  # Service restart, same heartbeat.
            self.assertFalse(runtime.maybe_play_connectivity())
            cloud.state["snapshot"]["verified_mono"] = 180
            self.assertTrue(runtime.maybe_play_connectivity())
            self.assertEqual(play.call_args_list, [mock.call("offline"), mock.call("connected"), mock.call(voice="online")])
            cloud.state["snapshot"] = None
            self.assertFalse(runtime.maybe_play_connectivity())

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
            self.assertFalse(runtime.capture_guided_recording("synthetic").meaningful)
        self.assertEqual(order, ["cue_finished", "arecord"])

    def test_failed_go_cue_never_opens_microphone(self):
        with mock.patch.object(runtime.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "aplay")), \
             mock.patch.object(runtime.subprocess, "Popen") as popen:
            with self.assertRaises(subprocess.CalledProcessError):
                runtime.capture_guided_recording("synthetic")
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
            result = runtime.capture_guided_recording("synthetic", max_seconds=60)
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
            result = runtime.capture_guided_recording("synthetic", max_seconds=30, talk_mode="hold")
        self.assertFalse(result.timed_out)
        with wave.open(result.path, "rb") as source:
            self.assertEqual(source.readframes(source.getnframes()), pcm)
