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

    def test_prompt_takes_rotate_durably_and_review_precedes_child_audio(self):
        names = [sound_pack.next_send_prompt(self.state).name for _ in range(5)]
        self.assertEqual(names, [f"voice-ask-send-{n}.wav" for n in (1, 2, 3, 1, 2)])
        order = []
        with mock.patch.object(runtime, "play_moment", side_effect=lambda *a, **kw: order.append(kw["voice"])), \
             mock.patch.object(runtime, "play_audio_for_approval", side_effect=lambda path, *a, **kw: order.append(path)):
            runtime.PiGuidedIO("recipient", "session", 60).play_review_for_approval("child.wav")
        self.assertEqual(order, ["review", "child.wav"])

    def test_incoming_message_bookends_precede_countdown(self):
        io = FakeIO(recordings=[RecordingResult(None, 0, False)])
        with tempfile.TemporaryDirectory() as directory:
            session = GuidedSession(io, OutboxStore(directory), lambda *a, **kw: None)
            session.run(recipient="synthetic", flow_kind="reply", countdown_path="countdown", send_prompt_path="send",
                        delete_warning_path="warning", not_sent_path="not-sent", incoming_path="message",
                        incoming_cue_path="start", incoming_voice_path="voice", incoming_end_path="end")
        self.assertEqual(io.calls[:5], [("ordinary", p) for p in ("start", "voice", "message", "end", "countdown")])

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

    def test_limit_warning_at_five_seconds_for_all_limits_and_pcm_exclusion(self):
        for maximum in (30, 60, 120):
            with self.subTest(maximum=maximum), mock.patch.object(runtime.subprocess, "Popen") as popen:
                process = popen.return_value
                process.poll.side_effect = [None, 0]
                cue = runtime.RecordingLimitCue(10, maximum)
                cue.update(10 + maximum - 5.01)
                popen.assert_not_called()
                cue.update(10 + maximum - 5)
                cue.update(10 + maximum - 4.45)
                self.assertEqual(cue.intervals[0][0], maximum - 5)
                self.assertAlmostEqual(cue.intervals[0][1], maximum - 4.45)
                rate = 16000
                pcm = struct.pack("<h", 5000) * (maximum * rate)
                muted = sound_pack.mute_pcm(pcm, rate, cue.intervals)
                self.assertEqual(len(muted), len(pcm))
                self.assertEqual(muted[(maximum - 5) * rate * 2:int((maximum - 4.45) * rate) * 2],
                                 bytes(int(0.55 * rate) * 2))
                self.assertEqual(muted[:rate * 2], pcm[:rate * 2])
                self.assertEqual(muted[-rate * 2:], pcm[-rate * 2:])

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

    def test_guided_saved_wav_excludes_warning_before_trim_and_approval(self):
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
             mock.patch.object(runtime, "play_moment"), \
             mock.patch.object(runtime.subprocess, "Popen", side_effect=[recorder, warning]), \
             mock.patch.object(runtime.time, "monotonic", side_effect=[0, 55, 55.55, 60, 60, 60]), \
             mock.patch.object(runtime.select, "select", return_value=([], [], [])), \
             mock.patch.object(runtime, "EnergyVAD", return_value=vad):
            result = runtime.capture_guided_recording("synthetic", max_seconds=60)
        with wave.open(result.path, "rb") as saved:
            actual = saved.readframes(saved.getnframes())
        span = actual[55 * 32000:int(55.55 * 32000)]
        self.assertEqual(span, bytes(len(span)))
        self.assertEqual(actual[:32000], pcm[:32000])
        self.assertEqual(actual[-32000:], pcm[-32000:])
        self.assertEqual(len(actual), len(pcm))
        self.assertEqual(vad.start.call_count, 2)  # Re-evaluated without warning audio.

    def test_hold_release_saved_outbox_wav_excludes_warning(self):
        pcm = struct.pack("<h", 5000) * (30 * 48000)
        recorder = mock.Mock()
        recorder.poll.return_value = 0
        warning = mock.Mock()
        warning.poll.side_effect = [None, 0]
        def popen(command, **kwargs):
            if command[0] == "arecord":
                with wave.open(command[-1], "wb") as output:
                    output.setparams((1, 2, 48000, 0, "NONE", ""))
                    output.writeframes(pcm)
                return recorder
            return warning
        context = {"contact": {"jid": "synthetic"}, "via_card": False}
        with mock.patch.object(runtime, "button", types.SimpleNamespace(is_pressed=True)), \
             mock.patch.object(runtime, "OUTBOX_DIR", str(self.root)), \
             mock.patch.object(runtime, "claim_fresh_card_intent", return_value=("none", None)), \
             mock.patch.object(runtime, "acknowledge_and_classify_legacy_press", return_value="record"), \
             mock.patch.object(runtime, "recording_recipient_context", return_value=context), \
             mock.patch.object(runtime, "transport_mode", return_value="wacli"), \
             mock.patch.object(runtime, "bind_legacy_job_recipient"), \
             mock.patch.object(runtime.time, "monotonic", side_effect=[0, 25, 25.55, 30, 30, 30, 30]), \
             mock.patch.object(runtime.time, "sleep"), \
             mock.patch.object(runtime.subprocess, "Popen", side_effect=popen):
            runtime.record_and_send_legacy({"max_recording_seconds": 30})
        saved = next(self.root.glob("*.wav"))
        with wave.open(str(saved), "rb") as source:
            actual = source.readframes(source.getnframes())
        span = actual[25 * 96000:int(25.55 * 96000)]
        self.assertEqual(span, bytes(len(span)))
        self.assertEqual(actual[:96000], pcm[:96000])
        self.assertEqual(actual[-96000:], pcm[-96000:])
        self.assertEqual(len(actual), len(pcm))
