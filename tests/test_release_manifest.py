import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_manifest", ROOT / "scripts/dev/release-manifest.py")
release_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_manifest)
UPDATER_SPEC = importlib.util.spec_from_file_location(
    "bounded_update", ROOT / "scripts/install/bounded_update.py"
)
bounded_update = importlib.util.module_from_spec(UPDATER_SPEC)
UPDATER_SPEC.loader.exec_module(bounded_update)


class ReleaseManifestTests(unittest.TestCase):
    def test_mapping_is_complete_for_runtime_and_excludes_private_state(self):
        paths = release_manifest.installed_paths(ROOT)
        self.assertEqual(len(paths), 77)
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

    def test_bounded_updater_accepts_every_manifest_mapping(self):
        for source, target in release_manifest.installed_paths(ROOT).items():
            with self.subTest(source=source):
                self.assertEqual(bounded_update._expected_target(source), target)
                self.assertTrue(bounded_update._rollback_target_allowed(target))

    def test_canonical_manifest_passes_full_bounded_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            device = fixture / "device"
            marker_parent = device / "etc/messagebox-onboarding"
            marker_parent.mkdir(parents=True)
            marker_parent.chmod(0o750)
            (device / "etc/systemd/system/multi-user.target.wants").mkdir(
                parents=True
            )
            manifest_path = fixture / "release-manifest.json"
            manifest_path.write_text(json.dumps(release_manifest.manifest(ROOT)) + "\n")

            manifest, entries, manifest_hash = bounded_update.load_candidate(
                ROOT, manifest_path, device
            )

            self.assertEqual(len(entries), 77)
            self.assertEqual(manifest["commit"], release_manifest.manifest(ROOT)["commit"])
            self.assertEqual(len(manifest_hash), 64)
            generator = next(
                entry
                for entry in entries
                if entry["target"] == bounded_update.MODE_GENERATOR
            )
            self.assertEqual(generator["mode"], 0o755)
            self.assertEqual(generator["source_path"].stat().st_uid, os.getuid())
