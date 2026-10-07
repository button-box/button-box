import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from messagebox.cloud_device import capabilities
from messagebox.cloud_runtime import _ringtone_preview_timeout
from messagebox.settings import RINGTONES

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "sounds/ringtones"
SPEC = importlib.util.spec_from_file_location("install_ringtones", ROOT / "scripts/install/ringtones.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class RingtonePackTests(unittest.TestCase):
    def test_pack_manifest_picker_and_capabilities_have_the_approved_order(self):
        installer.validate_ringtones(ASSETS)
        manifest = json.loads((ASSETS / "manifest.json").read_text())
        rows = manifest["ringtones"]
        expected = [(row["id"], row["label"]) for row in rows]
        self.assertEqual([id for id, _label in expected], list(RINGTONES))
        self.assertEqual(capabilities()["ringtones"], list(RINGTONES))
        html = (ROOT / "messagebox/onboarding/static/index.html").read_text()
        picker = re.search(r'<select id="ringtone"[^>]*>(.*?)</select>', html).group(1)
        self.assertEqual(re.findall(r'<option value="([^"]+)">([^<]+)</option>', picker), expected)
        # The local dashboard and setup page serve this same static picker.
        from messagebox.dashboard.app import DASHBOARD_STATIC
        self.assertIn(picker.encode(), DASHBOARD_STATIC["/"][0])

    def test_every_bundled_preview_has_enough_time_for_complete_audio(self):
        import wave
        for filename in RINGTONES.values():
            with self.subTest(filename=filename), wave.open(str(ASSETS / filename)) as sound:
                duration = sound.getnframes() / sound.getframerate()
                timeout = _ringtone_preview_timeout(ASSETS / filename)
                self.assertGreaterEqual(timeout, 17)
                self.assertGreaterEqual(timeout, duration + 5)

    def test_missing_empty_stereo_truncated_and_bad_lamp_or_manifest_fail_validation(self):
        import wave
        for fault in ("missing", "empty", "stereo", "truncated", "lamp", "manifest", "symlink"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                pack = Path(directory) / "ringtones"
                shutil.copytree(ASSETS, pack, ignore=shutil.ignore_patterns("midi"))
                sound = pack / "sunshine.wav"
                if fault == "missing":
                    sound.unlink()
                elif fault == "empty":
                    sound.write_bytes(b"")
                elif fault == "stereo":
                    with wave.open(str(sound), "wb") as output:
                        output.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                        output.writeframes(b"\0" * 40)
                elif fault == "truncated":
                    sound.write_bytes(sound.read_bytes()[:100])
                elif fault == "lamp":
                    (pack / "sunshine.lamp.json").write_text('{"ringtone_id":"sunshine","seconds":15.5,"lamp_on":[[1,0]]}')
                elif fault == "manifest":
                    (pack / "manifest.json").write_text('{}')
                else:
                    sound.unlink()
                    sound.symlink_to(ASSETS / "sunshine.wav")
                with self.assertRaisesRegex(ValueError, "Missing or invalid ringtones"):
                    installer.validate_ringtones(pack)

    def test_install_entry_points_reject_a_broken_pack_before_external_side_effects(self):
        for script in ("setup.sh", "provision.sh"):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                repository = Path(directory) / "repo"
                for name in ("messagebox", "scripts", "sounds", "config", "systemd"):
                    shutil.copytree(ROOT / name, repository / name,
                                    ignore=shutil.ignore_patterns("__pycache__"))
                (repository / "sounds/ringtones/island.wav").write_bytes(b"")
                commands = Path(directory) / "bin"
                commands.mkdir()
                log = Path(directory) / "external.log"
                for name in ("ssh", "rsync", "sudo"):
                    guard = commands / name
                    guard.write_text('#!/bin/sh\nprintf "called\\n" >> "$EXTERNAL_LOG"\nexit 99\n')
                    guard.chmod(0o755)
                args = [str(repository / "scripts" / script)]
                if script == "provision.sh":
                    args.append("admin@example.invalid")
                result = subprocess.run(args, env={**os.environ, "PATH": f"{commands}:{os.environ['PATH']}",
                                        "EXTERNAL_LOG": str(log)}, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Missing or invalid ringtones: island", result.stderr)
                self.assertFalse(log.exists())
