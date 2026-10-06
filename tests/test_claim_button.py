import contextlib
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from test_cloud_claim import FakeClient, NOW

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with mock.patch.dict(sys.modules, {"gpiozero": gpiozero}):
    from messagebox import button_send


cloud_claim = button_send.cloud_claim
CloudDeviceError = cloud_claim.CloudDeviceError

class ClaimButtonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = FakeClient()
        self.claim = cloud_claim.CloudClaim(self.client, path=self.root / "claim.json", clock=lambda: NOW)
        self.events = []
        confirm = self.client.confirm_claim

        def confirmed(claim_id):
            self.events.append("confirm")
            return confirm(claim_id)

        self.client.confirm_claim = confirmed
        self.cues = {name: (str(self.root / Path(button_send.BEEPS[name][0]).name),
                            *button_send.BEEPS[name][1:]) for name in ("press", "fail")}
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(mock.patch.object(cloud_claim, "CLAIM_FILE", self.claim.path))
        patches.enter_context(mock.patch.object(cloud_claim, "CloudClaim", return_value=self.claim))
        patches.enter_context(mock.patch.object(button_send, "log"))

    def audio(self, command, **kwargs):
        self.events.append(Path(command[-1]).stem.removeprefix("beep-"))
        return subprocess.CompletedProcess(command, 0)

    def test_debounced_press_plays_press_cue_before_cloud_confirmation(self):
        self.claim.start()
        reads = iter([False, True, True, True, False])
        switch = mock.Mock()
        type(switch).is_pressed = mock.PropertyMock(side_effect=lambda: next(reads))
        with mock.patch.object(button_send, "claim_beeps", return_value=self.cues), \
             mock.patch.object(button_send, "Button", return_value=switch), \
             mock.patch.object(button_send, "LED"), \
             mock.patch.object(button_send.time, "monotonic", side_effect=[0, 0.1]), \
             mock.patch.object(button_send.subprocess, "run", side_effect=self.audio) as run:
            with self.assertRaises(StopIteration):
                button_send.claim_only_loop()
        self.assertEqual(self.events, ["press", "confirm"])
        self.assertTrue(json.loads(self.claim.path.read_text())["physical_confirmed"])
        run.assert_called_once_with(["aplay", "-q", "-D", button_send.SPK_DEV, self.cues["press"][0]],
                                    check=True, timeout=2)

    def test_unrecorded_claim_outcomes_play_press_and_failure(self):
        for outcome in ("missing", "expired", "corrupt", "network", "invalid"):
            with self.subTest(outcome=outcome):
                self.claim.path.unlink(missing_ok=True)
                self.events.clear()
                if outcome != "missing":
                    self.claim.start()
                if outcome == "expired":
                    self.claim.clock = lambda: NOW + 601
                elif outcome == "corrupt":
                    self.claim.path.write_text("invalid")
                confirmation = mock.Mock()
                if outcome == "network":
                    confirmation.side_effect = CloudDeviceError("private")
                else:
                    confirmation.return_value = {"claimed": "invalid"}
                with mock.patch.object(self.client, "confirm_claim", confirmation), \
                     mock.patch.object(button_send.subprocess, "run", side_effect=self.audio):
                    button_send.claim_button_press(self.cues)
                self.assertEqual(self.events, ["press", "fail"])
                if outcome in {"network", "invalid"}:
                    self.assertFalse(json.loads(self.claim.path.read_text())["physical_confirmed"])
                self.claim.clock = lambda: NOW

    def test_accepted_and_pending_cancellation_do_not_play_failure(self):
        self.claim.start()
        with mock.patch.object(button_send.subprocess, "run", side_effect=self.audio):
            button_send.claim_button_press(self.cues)
            self.events.clear()
            button_send.claim_button_press(self.cues)
            self.assertEqual(self.events, ["press"])
            document = json.loads(self.claim.path.read_text())
            document["cancel_pending"] = True
            self.claim.path.write_text(json.dumps(document))
            self.events.clear()
            button_send.claim_button_press(self.cues)
            self.assertEqual(self.events, ["press"])
            document["cancel_pending"] = False
            document["physical_confirmed"] = False
            self.claim.path.write_text(json.dumps(document))
            self.client.claimed = True
            self.events.clear()
            button_send.claim_button_press(self.cues)
            self.assertEqual(self.events, ["press", "confirm"])
            self.assertFalse(self.claim.path.exists())

    def test_cues_use_service_directory_and_runtime_tone_definitions(self):
        for directory in (str(self.root), ""):
            with self.subTest(directory=directory), \
                 mock.patch.dict(button_send.os.environ, {"RUNTIME_DIRECTORY": directory}), \
                 mock.patch.object(button_send.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
                cues = button_send.claim_beeps()
            self.assertEqual(set(cues), {"press", "fail"})
            for name, call in zip(("press", "fail"), run.call_args_list):
                self.assertEqual(cues[name][1:], button_send.BEEPS[name][1:])
                self.assertEqual(Path(cues[name][0]).parent, Path(directory or "."))
                self.assertEqual(call.args[0][-1], cues[name][0])
                self.assertEqual(call.kwargs, {"check": True, "timeout": 5})

    def test_ffmpeg_and_aplay_failures_never_prevent_confirmation(self):
        for error in (OSError("private"), subprocess.CalledProcessError(1, "audio"),
                      subprocess.TimeoutExpired("audio", 2)):
            with self.subTest(error=type(error).__name__):
                self.claim.path.unlink(missing_ok=True)
                self.claim.start()
                with mock.patch.dict(button_send.os.environ, {"RUNTIME_DIRECTORY": str(self.root)}), \
                     mock.patch.object(button_send.subprocess, "run", side_effect=error):
                    cues = button_send.claim_beeps()
                    button_send.claim_button_press(cues)
                self.assertTrue(json.loads(self.claim.path.read_text())["physical_confirmed"])


if __name__ == "__main__":
    unittest.main()
