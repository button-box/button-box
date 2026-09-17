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
        job = types.SimpleNamespace(audio_path="sample.wav", recipient="family@g.us", message_id="local-test", flow_kind="reply", duration=2)
        for code, completion_error in ((1, None), (0, OSError("disk")), (0, None)):
            with self.subTest(code=code, completion_error=completion_error):
                store = mock.Mock()
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
        button_send.button.is_pressed = True
        with mock.patch.object(button_send.subprocess, "Popen", return_value=process):
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
