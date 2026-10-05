import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with mock.patch.dict(sys.modules, {"gpiozero": gpiozero}):
    from messagebox import button_send


class SendSuccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.notices = button_send.queue.SimpleQueue()
        for name, value in {
            "send_success_notices": self.notices,
            "OUTBOX_DIR": self.directory.name,
            "TEMP_DIR": self.directory.name,
            "_recording": False,
            "_guided_active": False,
            "button": types.SimpleNamespace(is_pressed=False),
        }.items():
            patcher = mock.patch.object(button_send, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("log", "log_event", "track_sent_for_receipts"):
            patcher = mock.patch.object(button_send, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_legacy_cue_only_after_successful_send_and_cleanup(self):
        for code in (1, 0):
            with self.subTest(returncode=code):
                path = Path(self.directory.name) / "1000-2.0.wav"
                path.write_bytes(b"audio")
                button_send.bind_legacy_job_recipient(str(path), "family@g.us")
                results = [types.SimpleNamespace(returncode=0), types.SimpleNamespace(returncode=code, stdout="", stderr="")]
                with mock.patch.object(button_send.subprocess, "run", side_effect=results) as run:
                    button_send.send_legacy_outbox_file(path.name)
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("--lock-wait") + 1], "0s")
                self.assertEqual(self.notices.empty(), code != 0)
                self.assertEqual(path.exists(), code != 0)

    def test_guided_cue_only_after_success_and_outbox_completion(self):
        job = types.SimpleNamespace(
            audio_path="sample.wav", recipient="family@g.us", message_id="local-test",
            flow_kind="reply", duration=2, transport="wacli", path=Path("local-test.job"),
        )
        for code, completion_error in ((1, None), (0, OSError("disk")), (0, None)):
            with self.subTest(code=code, completion_error=completion_error):
                store = mock.Mock()
                store.load.return_value = job
                store.set_state.return_value = job
                store.complete.side_effect = completion_error
                results = [types.SimpleNamespace(returncode=0), types.SimpleNamespace(returncode=code, stdout="", stderr="")]
                with mock.patch.object(button_send, "outbox_store", store), mock.patch.object(button_send.subprocess, "run", side_effect=results) as run:
                    if completion_error:
                        with self.assertRaises(OSError):
                            button_send.send_guided_job(job)
                    else:
                        button_send.send_guided_job(job)
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("--lock-wait") + 1], "0s")
                self.assertEqual(self.notices.empty(), code != 0 or completion_error is not None)

    def test_presence_and_played_reactions_delegate_without_waiting_for_sync(self):
        with mock.patch.object(button_send.subprocess, "Popen") as spawn:
            button_send.presence("recording", "120363000001@g.us")
            button_send.presence("paused", "120363000001@g.us")
            button_send.react_played({"chat": "120363000001@g.us", "msgid": "synthetic"})
        self.assertEqual(spawn.call_count, 3)
        for call in spawn.call_args_list:
            command = call.args[0]
            self.assertEqual(command[command.index("--lock-wait") + 1], "0s")

    def test_cue_waits_until_idle_and_is_played_only_once(self):
        self.notices.put(button_send.time.monotonic())
        with mock.patch.object(button_send, "play_send_success_cue") as play:
            for field in ("_recording", "_guided_active"):
                with mock.patch.object(button_send, field, True):
                    self.assertFalse(button_send.maybe_play_send_success())
            with mock.patch.object(button_send.button, "is_pressed", True):
                self.assertFalse(button_send.maybe_play_send_success())
            play.assert_not_called()
            self.assertTrue(button_send.maybe_play_send_success())
            self.assertFalse(button_send.maybe_play_send_success())
            play.assert_called_once_with()

    def test_stale_and_failed_audio_cues_are_not_retried(self):
        self.notices.put(button_send.time.monotonic() - 31)
        with mock.patch.object(button_send, "play_send_success_cue") as play:
            self.assertFalse(button_send.maybe_play_send_success())
            play.assert_not_called()
        self.notices.put(button_send.time.monotonic())
        with mock.patch.object(button_send, "play_send_success_cue", side_effect=subprocess.CalledProcessError(1, "aplay")):
            self.assertFalse(button_send.maybe_play_send_success())
        self.assertTrue(self.notices.empty())

    def test_cloud_success_is_durable_scoped_and_consumed_by_idle_owner_once(self):
        store = button_send.AudioRequests(Path(self.directory.name) / "audio-requests", clock=lambda: 100)
        key = button_send.success_key("a" * 64, "local-test")
        store.enqueue(key, "success", "a" * 64, 130)
        with mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "cloud_audio_requests", store), \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send, "play_send_success_cue", return_value=True) as play:
            for flag in ("_recording", "_guided_active"):
                with mock.patch.object(button_send, flag, True):
                    self.assertFalse(button_send.maybe_play_cloud_sound())
            self.assertEqual(store.outcome(key), "pending")
            self.assertTrue(button_send.maybe_play_cloud_sound())
            # A new process and repeated enqueue cannot replay the cue.
            restarted = button_send.AudioRequests(store.directory, clock=lambda: 101)
            restarted.enqueue(key, "success", "a" * 64, 131)
            with mock.patch.object(button_send, "cloud_audio_requests", restarted):
                self.assertFalse(button_send.maybe_play_cloud_sound())
            play.assert_called_once_with()
        self.assertEqual(store.outcome(key), "played")

    def test_cloud_swoosh_off_consumes_success_cue_without_audio(self):
        store = button_send.AudioRequests(Path(self.directory.name) / "audio-off", clock=lambda: 100)
        key = button_send.success_key("a" * 64, "local-test")
        store.enqueue(key, "success", "a" * 64, 130)
        with mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "cloud_audio_requests", store), \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send, "caregiver_settings", return_value={"swoosh_sound_enabled": False}), \
             mock.patch.object(button_send, "play_idle_sound") as play:
            self.assertFalse(button_send.maybe_play_cloud_sound())
        play.assert_not_called()
        self.assertEqual(store.outcome(key), "rejected")

    def test_cloud_stale_foreign_or_crash_claimed_cues_never_play(self):
        for case in ("expired", "foreign", "crash"):
            with self.subTest(case=case):
                store = button_send.AudioRequests(Path(self.directory.name) / case, clock=lambda: 100)
                key = button_send.success_key("a" * 64, "local-test")
                store.enqueue(key, "success", "a" * 64, 99 if case == "expired" else 130)
                if case == "crash":
                    with store.owner():
                        self.assertIsNotNone(store.claim_next("a" * 64))
                with mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
                     mock.patch.object(button_send, "cloud_audio_requests", store), \
                     mock.patch.object(button_send.cloud_runtime, "account_scope", return_value=("b" if case == "foreign" else "a") * 64), \
                     mock.patch.object(button_send, "play_send_success_cue") as play:
                    self.assertFalse(button_send.maybe_play_cloud_sound())
                    play.assert_not_called()
                self.assertEqual(store.outcome(key), {"expired": "expired", "foreign": "rejected", "crash": "unknown"}[case])

    def test_new_press_interrupts_success_cue_without_being_consumed(self):
        self.notices.put(button_send.time.monotonic())
        process = mock.Mock()
        process.poll.return_value = None

        def press(_seconds):
            button_send.button.is_pressed = True

        with mock.patch.object(button_send.subprocess, "Popen", return_value=process) as spawn, mock.patch.object(button_send.time, "sleep", side_effect=press), mock.patch.object(button_send, "wait_for_stable_open") as discard:
            self.assertTrue(button_send.maybe_play_send_success())
        spawn.assert_called_once_with(["aplay", "-q", "-D", button_send.SPK_DEV, button_send.SEND_SUCCESS_WAV])
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=0.2)
        discard.assert_not_called()
        self.assertTrue(button_send.button.is_pressed)
        self.assertTrue(self.notices.empty())

    def test_success_cue_releases_speaker_even_if_termination_stalls(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("aplay", 0.2), 0]
        with mock.patch.object(button_send.subprocess, "Popen", side_effect=lambda *_: (setattr(button_send.button, "is_pressed", True), process)[1]):
            button_send.play_send_success_cue()
        self.assertEqual(process.method_calls, [
            mock.call.poll(), mock.call.poll(), mock.call.terminate(),
            mock.call.wait(timeout=0.2), mock.call.kill(), mock.call.wait(timeout=0.2),
        ])

    def test_success_cue_finishes_normally_without_killing_player(self):
        process = mock.Mock()
        process.poll.side_effect = [None, 0, 0]
        with mock.patch.object(button_send.subprocess, "Popen", return_value=process), mock.patch.object(button_send.time, "sleep"):
            button_send.play_send_success_cue()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_swoosh_off_suppresses_only_the_send_success_cue(self):
        with mock.patch.object(button_send, "caregiver_settings", return_value={"swoosh_sound_enabled": False}), \
             mock.patch.object(button_send, "play_idle_sound") as play:
            self.assertFalse(button_send.play_send_success_cue())
        play.assert_not_called()

    def test_missing_swoosh_setting_preserves_legacy_default(self):
        with mock.patch.object(button_send, "caregiver_settings", return_value={}), \
             mock.patch.object(button_send, "play_idle_sound", return_value=True) as play:
            self.assertTrue(button_send.play_send_success_cue())
        play.assert_called_once_with(button_send.SEND_SUCCESS_WAV, 5)

    def test_success_cue_player_failure_or_timeout_does_not_retry_send(self):
        for status in (1, None):
            with self.subTest(status=status):
                self.notices.put(100)
                process = mock.Mock()
                process.poll.return_value = status
                with mock.patch.object(button_send.subprocess, "Popen", return_value=process), mock.patch.object(button_send.time, "monotonic", side_effect=[100, 100, 106]):
                    self.assertFalse(button_send.maybe_play_send_success())
                self.assertTrue(self.notices.empty())
                if status is None:
                    process.terminate.assert_called_once_with()
                    process.wait.assert_called_once_with(timeout=0.2)
