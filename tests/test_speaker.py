"""Use harmless child processes to test cross-process speaker ownership."""

import fcntl
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from messagebox import speaker


class SpeakerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        replacement = mock.patch.object(speaker, "LOCK_FILE", self.root / "speaker.lock")
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_another_process_must_release_before_playback_and_failure_unlocks(self):
        ready = self.root / "ready"
        script = "import fcntl,pathlib,sys,time; f=open(sys.argv[1],'a+b'); fcntl.flock(f,fcntl.LOCK_EX); pathlib.Path(sys.argv[2]).touch(); time.sleep(.25)"
        child = subprocess.Popen([sys.executable, "-c", script, str(speaker.LOCK_FILE), str(ready)])
        self.addCleanup(lambda: child.kill() if child.poll() is None else None)
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        self.assertTrue(speaker.is_busy())
        start = time.monotonic()
        speaker.run([sys.executable, "-c", "pass"], check=True, timeout=2)
        self.assertGreater(time.monotonic() - start, .1)
        child.wait(timeout=2)
        with self.assertRaises(subprocess.CalledProcessError):
            speaker.run([sys.executable, "-c", "raise SystemExit(2)"], check=True, timeout=2)
        self.assertFalse(speaker.is_busy())

    def test_short_busy_retry_and_bounded_failure_do_not_expose_driver_text(self):
        busy = subprocess.CalledProcessError(1, "aplay", stderr=b"aplay: set_params cannot set freq 48000")
        runner = mock.Mock(side_effect=[busy, subprocess.CompletedProcess([], 0)])
        with mock.patch.object(speaker.time, "sleep") as delay:
            self.assertEqual(speaker.run(["aplay"], runner=runner, check=True).returncode, 0)
            delay.assert_called_once_with(.15)
        with mock.patch.object(speaker.time, "sleep"), self.assertRaises(speaker.SpeakerBusy):
            speaker.run(["aplay"], runner=mock.Mock(side_effect=busy), check=True)
        self.assertFalse(speaker.is_busy())

    def test_wacli_nfc_sounds_do_not_write_read_only_settings(self):
        import json
        from messagebox.onboarding.nfc import TonePlayer
        from messagebox.settings import defaults, SettingsStore, SettingsReader
        settings = self.root / 'settings'
        settings.mkdir()
        self.addCleanup(lambda: settings.chmod(0o755))
        (settings / 'settings.json').write_text(json.dumps(defaults({'MSGBOX_TRANSPORT': 'wacli'})))
        settings.chmod(0o555)
        before = {p.name: p.read_bytes() for p in settings.iterdir()}
        root = Path(__file__).resolve().parents[1]
        player = TonePlayer(root / 'sounds/cues', run=mock.Mock())
        player.settings = SettingsReader(SettingsStore(settings / 'settings.json', environ={'MSGBOX_TRANSPORT': 'wacli'}))
        with mock.patch.dict('os.environ', {'MSGBOX_TRANSPORT': 'wacli'}), \
             mock.patch('messagebox.runtime_paths.SETTINGS_DIR', settings), \
             mock.patch('messagebox.sound_pack.SOUND_DIR', root / 'sounds'):
            player('read')
            player('success')
        self.assertEqual({p.name: p.read_bytes() for p in settings.iterdir()}, before)
        self.assertEqual(sum(call.args[0][0] == 'aplay' for call in player.run.call_args_list), 3)
        self.assertTrue(speaker.LOCK_FILE.is_file())
        # Deployment contract: the strict service can write /run, not settings.
        for unit in ('messagebox-onboarding-nfc', 'messagebox-onboarding-home'):
            text = (root / 'systemd/onboarding' / (unit + '.service')).read_text()
            self.assertIn('ProtectSystem=strict', text)
            self.assertIn('ReadWritePaths=/run/messagebox-speaker', text)
            self.assertTrue(any(line.startswith('SupplementaryGroups=') and 'messagebox-settings' in line
                                for line in text.splitlines()))
        tmpfiles = (root / 'systemd/messagebox.tmpfiles.conf').read_text()
        self.assertIn('d /run/messagebox-speaker 2770 root messagebox-settings -', tmpfiles)
        self.assertIn('f /run/messagebox-speaker/speaker.lock 0660 root messagebox-settings -', tmpfiles)
        detect = (root / 'systemd/messagebox-audio-detect.service').read_text()
        self.assertIn('systemd-tmpfiles --create --prefix=/run/messagebox-speaker', detect)

    def test_async_player_holds_lock_until_reaped(self):
        process = speaker.start_player([sys.executable, "-c", "import time; time.sleep(.1)"])
        self.assertTrue(speaker.is_busy())
        process.wait(timeout=2)
        self.assertTrue(process.speaker_released.wait(2))
        self.assertFalse(speaker.is_busy())
        with speaker.capture_owner():
            speaker.run([sys.executable, "-c", "pass"], check=True, timeout=2)
            with speaker.LOCK_FILE.open("r+b") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main()
