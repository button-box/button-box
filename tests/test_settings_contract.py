import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from messagebox.cloud_device import capabilities
from messagebox.settings import _ROOT_KEYS, defaults, validate


REPO = Path(__file__).resolve().parents[1]
EXPORTER = REPO / "scripts" / "dev" / "settings_contract.py"


class SettingsContractTests(unittest.TestCase):
    def export(self, repo=REPO):
        return subprocess.run([sys.executable, str(EXPORTER), "--repo", str(repo)],
                              text=True, capture_output=True)

    def test_current_checkout_contract_matches_settings_and_capabilities(self):
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        contract = json.loads(result.stdout)
        self.assertEqual(contract["build"], subprocess.check_output(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], text=True).strip())
        self.assertEqual(contract["settings_version"], 1)
        self.assertEqual(contract["unknown_keys"], "ignore")
        self.assertEqual(set(contract["keys"]), _ROOT_KEYS)
        self.assertEqual(contract["keys"]["master_volume_percent"], {"type": "int", "min": 0, "max": 100})
        self.assertEqual(contract["keys"]["revision"], {"type": "int", "min": 0})
        self.assertEqual(contract["keys"]["timezone"], {"type": "string", "max": 64})
        self.assertEqual(contract["keys"]["talk_mode"], {"type": "enum", "values": ["hold", "tap"]})
        self.assertEqual(contract["keys"]["review_before_send"], {"type": "bool"})
        self.assertEqual(contract["capabilities"]["talk_modes"], ["tap", "hold"])
        self.assertIs(contract["capabilities"]["review_before_send"], True)
        for key, descriptor in contract["keys"].items():
            for value in descriptor.get("values", []):
                with self.subTest(key=key, value=value):
                    validate({**defaults({}), key: value})
        with mock.patch("messagebox.sound_pack.SOUND_DIR", REPO / "sounds"):
            self.assertEqual(contract["capabilities"], capabilities(nfc=False))
        self.assertEqual(contract["command_kinds"], sorted([
            "audio", "listened", "card_prompt", "settings", "preview_ringtone", "voice_preview",
            "nfc_enroll", "nfc_cancel", "nfc_unpair", "queue_hold", "delete_message",
        ]))

    def alternate_checkout(self, directory):
        repo = Path(directory)
        (repo / "messagebox").mkdir()
        for name in ("settings", "cloud_device", "cloud_runtime", "sound_pack"):
            target = repo / "messagebox" / f"{name}.py"
            shutil.copyfile(REPO / "messagebox" / f"{name}.py", target)
            with target.open("a") as stream:
                stream.write('\nraise RuntimeError("runtime module imported")\n')
        (repo / "sounds").symlink_to(REPO / "sounds", target_is_directory=True)
        gitdir = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "--absolute-git-dir"], text=True).strip()
        (repo / ".git").write_text(f"gitdir: {gitdir}\n")
        return repo

    def test_other_checkout_is_described_without_importing_startup_code(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.alternate_checkout(directory)
            path = repo / "messagebox" / "settings.py"
            source = path.read_text()
            # Represent the older validation policy without the new filter.
            source = source.replace('    document = normalize_recording_settings(\n        {key: value for key, value in document.items() if key in _ROOT_KEYS})\n', '    document = normalize_recording_settings(document)\n')
            path.write_text(source)
            result = self.export(repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["unknown_keys"], "reject")

    def test_undescribable_key_fails_clearly_without_emitting_json(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.alternate_checkout(directory)
            path = repo / "messagebox" / "settings.py"
            path.write_text(path.read_text().replace('    "version",', '    "future_list",\n    "version",', 1)
                            .replace('        "version": SCHEMA_VERSION,',
                                     '        "future_list": [],\n        "version": SCHEMA_VERSION,', 1))
            result = self.export(repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("cannot describe setting 'future_list'", result.stderr)
