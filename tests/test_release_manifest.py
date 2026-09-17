import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_manifest", ROOT / "scripts/dev/release-manifest.py")
release_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_manifest)


class ReleaseManifestTests(unittest.TestCase):
    def test_mapping_is_complete_for_runtime_and_excludes_private_state(self):
        paths = release_manifest.installed_paths(ROOT)
        self.assertEqual(len(paths.values()), len(set(paths.values())))
        self.assertEqual(paths["scripts/install/audio_config.py"], "/usr/lib/messagebox/audio_config.py")
        self.assertEqual(
            paths["scripts/install/messagebox-mode-migrate.py"],
            "/usr/lib/messagebox/messagebox-mode-migrate.py",
        )
        self.assertEqual(
            paths["systemd/messagebox-mode-generator"],
            "/usr/local/lib/systemd/system-generators/messagebox-mode-generator",
        )
        self.assertIn("systemd/messagebox-audio-detect.service", paths)
        self.assertIn("messagebox/onboarding/static/clipboard.js", paths)
        self.assertIn("sounds/feedback/sent-swoosh.wav", paths)
        self.assertNotIn("messagebox/midi_ringtone.py", paths)
        for source, target in paths.items():
            with self.subTest(source=source):
                self.assertTrue((ROOT / source).is_file())
                self.assertFalse(target.startswith(("/var/lib/", "/run/", "/etc/messagebox/")))
                self.assertNotIn("..", Path(target).parts)
