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
                with mock.patch.object(button_send.subprocess, "run", side_effect=results):
                    button_send.send_legacy_outbox_file(path.name)
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
                with mock.patch.object(button_send, "outbox_store", store), mock.patch.object(button_send.subprocess, "run", side_effect=results):
                    if completion_error:
                        with self.assertRaises(OSError):
                            button_send.send_guided_job(job)
                    else:
                        button_send.send_guided_job(job)
                self.assertEqual(self.notices.empty(), code != 0 or completion_error is not None)

    def test_cue_waits_until_idle_and_is_played_only_once(self):
        self.notices.put(button_send.time.monotonic())
        with mock.patch.object(button_send, "play_audio_ordinary") as play:
            for field in ("_recording", "_guided_active"):
                with mock.patch.object(button_send, field, True):
                    self.assertFalse(button_send.maybe_play_send_success())
            with mock.patch.object(button_send.button, "is_pressed", True):
                self.assertFalse(button_send.maybe_play_send_success())
            play.assert_not_called()
            self.assertTrue(button_send.maybe_play_send_success())
            self.assertFalse(button_send.maybe_play_send_success())
            play.assert_called_once_with(button_send.SEND_SUCCESS_WAV)

    def test_stale_and_failed_audio_cues_are_not_retried(self):
        self.notices.put(button_send.time.monotonic() - 31)
        with mock.patch.object(button_send, "play_audio_ordinary") as play:
            self.assertFalse(button_send.maybe_play_send_success())
            play.assert_not_called()
        self.notices.put(button_send.time.monotonic())
        with mock.patch.object(button_send, "play_audio_ordinary", side_effect=subprocess.CalledProcessError(1, "aplay")):
            self.assertFalse(button_send.maybe_play_send_success())
        self.assertTrue(self.notices.empty())
