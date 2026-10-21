import importlib.util
import json
import os
import shutil
import stat
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
    def test_payload_includes_fixed_trust_tools_without_installing_root_pins(self):
        payload = (ROOT / 'scripts/release/payload.txt').read_text().splitlines()
        self.assertEqual(len(payload), 633)
        for name in ('scripts/install/trust_update.py', 'scripts/release/trust-update.py',
                     'config/emergency-keys/README.md', 'scripts/install/health-probe.py',
                     'messagebox/recovery_bootstrap.py', 'messagebox/recovery_launcher.py',
                     'messagebox/recovery_helper.py'):
            self.assertIn(name, payload)
        # The installed program mapping remains 619 files. Fixed trust code is
        # staged separately in updater/, and public pins are bootstrap-only.
        paths = release_manifest.installed_paths(ROOT)
        self.assertEqual(len(paths), 619)
        self.assertFalse(any('/etc/button-box-update/' in target for target in paths.values()))

    def test_mapping_is_complete_for_runtime_and_excludes_private_state(self):
        paths = release_manifest.installed_paths(ROOT)
        self.assertEqual(len(paths), 619)
        for name in ("cloud-local.html", "cloud-local.js"):
            self.assertEqual(paths[f"messagebox/onboarding/static/{name}"], f"/opt/messagebox/messagebox/onboarding/static/{name}")
        for name in ('recovery_bootstrap.py', 'recovery_launcher.py', 'recovery_helper.py'):
            self.assertEqual(paths['messagebox/' + name], '/opt/messagebox/messagebox/' + name)
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
        self.assertNotIn("messagebox/business_send.py", paths)
        self.assertNotIn("messagebox/business_receive.py", paths)
        self.assertIn("messagebox/cloud_device.py", paths)
        self.assertIn("messagebox/device_http.py", paths)
        self.assertIn("messagebox/software.py", paths)
        self.assertIn("messagebox/cloud_runtime.py", paths)
        self.assertIn("messagebox/cloud_quiet.py", paths)
        self.assertEqual(paths["messagebox/audio_requests.py"], "/opt/messagebox/messagebox/audio_requests.py")
        self.assertIn("messagebox/cloud_events.py", paths)
        self.assertIn("messagebox/cloud_voice.py", paths)
        self.assertIn("sounds/cloud/reaction.wav", paths)
        self.assertIn("sounds/cloud/text-message.wav", paths)
        self.assertIn("messagebox/cloud_claim.py", paths)
        self.assertIn("messagebox/qrcodegen.py", paths)
        self.assertNotIn("sounds/feedback/sent-swoosh.wav", paths)
        self.assertIn("sounds/cues/cue-press.wav", paths)
        self.assertEqual(paths["sounds/cues/cue-deleted.wav"], "/opt/messagebox/sounds/cues/cue-deleted.wav")
        self.assertEqual(paths["sounds/cues/cue-card_saved.wav"], "/opt/messagebox/sounds/cues/cue-card_saved.wav")
        self.assertIn("sounds/voice/voice-online.wav", paths)
        self.assertIn("sounds/voice/voice-setup-online.wav", paths)
        for pack in ("voice", *(f"voices/{pack}" for pack in ("pirate", "alien", "dj", "robot", "french", "charlie", "tata"))):
            for line in ("card-prompt", "card-saved", "online-ready"):
                source = f"sounds/{pack}/voice-{line}.wav"
                self.assertEqual(paths[source], "/opt/messagebox/" + source)
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

    def test_generator_includes_additional_sounds_and_update_lines(self):
        paths = release_manifest.installed_paths(ROOT)
        for name in ('update-ready', 'auto-updates', 'update-done', 'update-reminder', 'update-installing', 'auto-updates-on'):
            source = f'sounds/cues/{name}.wav'
            self.assertEqual(paths[source], '/opt/messagebox/' + source)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            shutil.copytree(ROOT / 'sounds', source / 'sounds')
            for pack in ('cues', 'cloud', 'voice', 'voices/pirate', 'ringtones'):
                extra = source / 'sounds' / pack / 'extra.wav'
                extra.write_bytes((ROOT / 'sounds/cues/cue-press.wav').read_bytes())
                relative = extra.relative_to(source).as_posix()
                self.assertEqual(release_manifest.installed_paths(source)[relative], '/opt/messagebox/' + relative)

    def test_canonical_manifest_passes_full_bounded_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory).resolve()
            source = fixture / "source"
            device = fixture / "device"
            source.mkdir()
            marker_parent = device / "etc/messagebox-onboarding"
            marker_parent.mkdir(parents=True)
            marker_parent.chmod(0o750)
            (device / "etc/systemd/system/multi-user.target.wants").mkdir(
                parents=True
            )
            for relative in release_manifest.installed_paths(ROOT):
                candidate = source / relative
                candidate.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, candidate)
                candidate.chmod(stat.S_IMODE((ROOT / relative).stat().st_mode))
            manifest_tool = source / "scripts/dev/release-manifest.py"
            manifest_tool.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / "scripts/dev/release-manifest.py", manifest_tool)
            manifest_tool.chmod(0o644)
            manifest_path = source / "release-manifest.json"
            expected_manifest = release_manifest.manifest(ROOT)
            manifest_path.write_text(json.dumps(expected_manifest) + "\n")

            manifest, entries, manifest_hash = bounded_update.load_candidate(
                source, manifest_path, device
            )

            self.assertEqual(len(entries), 619)
            self.assertEqual(manifest["commit"], expected_manifest["commit"])
            self.assertEqual(len(manifest_hash), 64)
            generator = next(
                entry
                for entry in entries
                if entry["target"] == bounded_update.MODE_GENERATOR
            )
            self.assertEqual(generator["mode"], 0o755)
            self.assertEqual(generator["source_path"].stat().st_uid, os.getuid())
