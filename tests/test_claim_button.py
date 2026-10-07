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
        self.cues = {name: button_send.CUES[name] for name in ("press", "fail")}
        consume = button_send.sound_pack.consume_moment
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(mock.patch.object(cloud_claim, "CLAIM_FILE", self.claim.path))
        patches.enter_context(mock.patch.object(cloud_claim, "CloudClaim", return_value=self.claim))
        patches.enter_context(mock.patch.object(button_send, "log"))
        patches.enter_context(mock.patch.object(button_send, "apply_master_volume"))
        patches.enter_context(mock.patch.object(button_send, "quiet_hours", return_value=False))
        patches.enter_context(mock.patch.object(button_send.sound_pack, "consume_moment", side_effect=lambda name: consume(name, self.root / "sounds.json")))

    def audio(self, command, **kwargs):
        self.events.append({ "cue-oops": "fail" }.get(Path(command[-1]).stem, Path(command[-1]).stem.removeprefix("cue-")))
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
        run.assert_called_once_with(["aplay", "-q", "-D", button_send.SPK_DEV, str(self.cues["press"])],
                                    check=True, timeout=5)

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

    def test_cues_are_bundled_and_missing_assets_are_reported_without_generation(self):
        with mock.patch.object(button_send, "validate_sounds", side_effect=ValueError), \
             mock.patch.object(button_send.subprocess, "run") as run, \
             mock.patch.object(button_send, "log") as log:
            cues = button_send.claim_beeps()
        self.assertEqual(cues, {name: button_send.CUES[name] for name in ("press", "fail", "online")})
        run.assert_not_called()
        log.assert_called_once_with("claim sound assets missing/invalid")

    def test_unknown_card_plays_oops_then_voice(self):
        with mock.patch.object(button_send, "play_moment") as moment, \
             mock.patch.object(button_send, "play_audio_ordinary") as voice, \
             mock.patch.object(button_send, "nfc_announcement_store"), \
             mock.patch.object(button_send, "log_event"):
            self.assertTrue(button_send._play_nfc_prompt("synthetic-card", "unknown", ""))
        moment.assert_called_once_with("oops")
        voice.assert_called_once_with(button_send.sound_pack.voice_path("card-unknown"))

    def test_online_cue_is_played_once_by_idle_button_owner_even_after_audio_failure(self):
        cues = {**self.cues, "online": button_send.CUES["online"]}
        queue_cue = cloud_claim.setup_online_cue
        def cue(**kwargs):
            return queue_cue(directory=self.root, session="setup-1", **kwargs)
        cue()
        with mock.patch.object(cloud_claim, "setup_online_cue", side_effect=cue), \
             mock.patch.object(button_send.subprocess, "run", side_effect=OSError("private")) as run:
            button_send.play_setup_online(cues)
            button_send.play_setup_online(cues)
        run.assert_called_once_with(["aplay", "-q", "-D", button_send.SPK_DEV, str(cues["online"])],
                                    check=True, timeout=5)
        self.assertNotEqual(cues["online"], self.cues["press"])
        self.assertNotEqual(cues["online"], self.cues["fail"])

    def test_missing_assets_and_aplay_failures_never_prevent_confirmation(self):
        for error in (OSError("private"), subprocess.CalledProcessError(1, "audio"),
                      subprocess.TimeoutExpired("audio", 2)):
            with self.subTest(error=type(error).__name__):
                self.claim.path.unlink(missing_ok=True)
                self.claim.start()
                with mock.patch.object(button_send, "validate_sounds", side_effect=ValueError), \
                     mock.patch.object(button_send.subprocess, "run", side_effect=error):
                    cues = button_send.claim_beeps()
                    button_send.claim_button_press(cues)
                self.assertTrue(json.loads(self.claim.path.read_text())["physical_confirmed"])


if __name__ == "__main__":
    unittest.main()
