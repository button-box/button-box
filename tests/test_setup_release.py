import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts/install"
with mock.patch.object(sys, "path", [str(INSTALL), *sys.path]):
    spec = importlib.util.spec_from_file_location("setup_release", INSTALL / "setup_release.py")
    setup_release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup_release)


class SetupReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("messagebox", "systemd", "scripts", "sounds", "config"):
            shutil.copytree(ROOT / name, self.source / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(ROOT / "VERSION", self.source / "VERSION")
        canonical = setup_release.bounded_update._load_module(
            "setup_test_manifest", self.source / "scripts/dev/release-manifest.py"
        )
        self.manifest = {
            "version": (self.source / "VERSION").read_text().strip(),
            "commit": "1" * 40,
            "files": [
                {"source": source, "installed": target,
                 "sha256": hashlib.sha256((self.source / source).read_bytes()).hexdigest()}
                for source, target in canonical.installed_paths(self.source).items()
            ],
        }
        self.save_manifest()
        self.device = self.root / "device"
        self.device.mkdir()

    def save_manifest(self):
        (self.source / "release-manifest.json").write_text(json.dumps(self.manifest))

    def install_manifest(self):
        for item in self.manifest["files"]:
            destination = self.device / item["installed"].lstrip("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.source / item["source"], destination)
            destination.chmod(setup_release.bounded_update._install_mode(item["installed"]))

    def test_fresh_guard_rejects_private_state_symlinks_units_and_accounts(self):
        for absolute in setup_release.FRESH_PATHS:
            with self.subTest(path=absolute):
                path = self.device / absolute.lstrip("/")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(self.root / "missing")
                with self.assertRaisesRegex(RuntimeError, "fresh Pi"):
                    setup_release.check_fresh(self.device, check_accounts=False)
                self.assertTrue(path.is_symlink())
                path.unlink()
        unit = self.device / "etc/systemd/system/messagebox-button.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("existing")
        with self.assertRaisesRegex(RuntimeError, "units"):
            setup_release.check_fresh(self.device, check_accounts=False)
        unit.unlink()
        with mock.patch.object(setup_release.pwd, "getpwnam", return_value=object()):
            with self.assertRaisesRegex(RuntimeError, "account"):
                setup_release.check_fresh(self.device)

    def test_release_checks_completeness_identity_owner_and_hash(self):
        setup_release.load_release(self.source)
        with self.assertRaisesRegex(RuntimeError, "trusted"):
            setup_release.load_release(self.source, owner=os.getuid() + 1)
        original = (self.source / "VERSION").read_text()
        (self.source / "VERSION").write_text("different\n")
        with self.assertRaisesRegex(RuntimeError, "identity"):
            setup_release.load_release(self.source)
        (self.source / "VERSION").write_text(original)
        removed = self.manifest["files"].pop()
        self.save_manifest()
        with self.assertRaisesRegex(RuntimeError, "complete"):
            setup_release.load_release(self.source)
        self.manifest["files"].append(removed)
        self.save_manifest()
        (self.source / removed["source"]).write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "hash"):
            setup_release.load_release(self.source)

    def test_identity_is_recorded_only_after_all_installed_hashes_and_modes_match(self):
        self.install_manifest()
        destination = self.device / "opt/messagebox/release.json"
        setup_release.record_release(self.source, self.device)
        identity = json.loads(destination.read_text())
        self.assertEqual(identity, {
            "version": self.manifest["version"], "commit": self.manifest["commit"],
            "manifest_sha256": hashlib.sha256((self.source / "release-manifest.json").read_bytes()).hexdigest(),
        })
        original = destination.read_bytes()
        item = self.manifest["files"][0]
        installed = self.device / item["installed"].lstrip("/")
        installed.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "mode"):
            setup_release.record_release(self.source, self.device)
        self.assertEqual(destination.read_bytes(), original)
        installed.chmod(setup_release.bounded_update._install_mode(item["installed"]))
        installed.write_bytes(b"wrong")
        with self.assertRaisesRegex(RuntimeError, "hash"):
            setup_release.record_release(self.source, self.device)
        self.assertEqual(destination.read_bytes(), original)

    def test_cloud_environment_uses_audio_detection_without_claim_or_configured_markers(self):
        with (
            mock.patch.object(setup_release.audio_config, "detect_microphone", return_value=("Mic", "plughw:CARD=Mic,DEV=0")),
            mock.patch.object(setup_release.audio_config, "detect_speaker", return_value=("Speaker", "plughw:CARD=Speaker,DEV=0")),
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(setup_release.main(["cloud-environment", str(self.source / "config/env.example")]), 0)
        environment = dict(line.split("=", 1) for line in output.getvalue().splitlines() if line and not line.startswith("#"))
        self.assertEqual(environment["MSGBOX_TRANSPORT"], "cloud")
        self.assertEqual(environment["MSGBOX_MIC_CARD"], "Mic")
        self.assertEqual(environment["MSGBOX_SPEAKER_CARD"], "Speaker")
        self.assertEqual(list(self.device.iterdir()), [])

    def run_setup(self, arguments, *, with_release=True):
        """Run the actual shell orchestration with hardware/system boundaries replaced."""
        if not with_release:
            (self.source / "release-manifest.json").unlink()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        log = self.root / "operations.log"
        python = bin_dir / "python3"
        python.write_text(f'''#!{sys.executable}
import importlib.util, os, shutil, sys
from pathlib import Path
root = Path(os.environ["TEST_DEVICE"])
args = sys.argv[1:]
if args and args[0].endswith("setup_release.py"):
    sys.path.insert(0, str(Path(args[0]).parent))
    import setup_release as helper
    if args[1] == "check-fresh":
        try: helper.check_fresh(root, check_accounts=False)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr); raise SystemExit(1)
    elif args[1] == "record-release": helper.record_release(args[2], root)
    elif args[1] == "check-release": helper.load_release(args[2])
    elif args[1] == "check-install": helper.check_destinations(helper.load_release(args[2])[0], root)
    else:
        helper.audio_config.detect_microphone = lambda: ("Mic", "plughw:CARD=Mic,DEV=0")
        helper.audio_config.detect_speaker = lambda: ("Speaker", "plughw:CARD=Speaker,DEV=0")
        raise SystemExit(helper.main(args[1:]))
elif args and args[0].endswith("audio_config.py"):
    sys.path.insert(0, str(Path(args[0]).parent))
    import audio_config
    print(audio_config.render_environment(Path(args[1]).read_text(), "Mic", "mic", "Speaker", "speaker"), end="")
elif args and args[0].endswith("messagebox-mode-migrate.py") and args[1] != "--check":
    target = root / "usr/local/lib/systemd/system-generators/messagebox-mode-generator"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args[1], target); target.chmod(0o755)
elif args == ["-m", "messagebox.make_ringtones"]:
    (root / "opt/messagebox/ringtones/test.wav").touch()
''')
        python.chmod(0o755)
        sudo = bin_dir / "sudo"
        sudo.write_text(f'''#!{sys.executable}
import os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["TEST_LOG"], "a") as log: log.write(" ".join(args) + "\\n")
if args[0] in {{"apt-get", "useradd", "groupadd", "usermod", "systemd-tmpfiles", "systemd-analyze"}}:
    raise SystemExit(0)
if args[:2] == ["ln", "-T"]:
    os.link(args[2], args[3]); raise SystemExit(0)
if args[:2] == ["sed", "-i"] and sys.platform == "darwin":
    args.insert(2, "")
if args[0] == "mktemp":
    Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
if args[0] in {{"install", "ln", "chmod", "rm", "tee", "sed", "mktemp"}}:
    fixture = Path(os.environ["TEST_DEVICE"]).parent
    for arg in args[1:]:
        if arg.startswith("/") and not Path(arg).is_relative_to(fixture):
            raise RuntimeError("test operation escapes the fixture")
if args[0] == "install":
    if "-d" not in args: Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
    args = [a for i, a in enumerate(args) if a not in ("-o", "-g") and (i == 0 or args[i-1] not in ("-o", "-g"))]
raise SystemExit(subprocess.call(args))
''')
        sudo.chmod(0o755)
        mktemp = bin_dir / "mktemp"
        mktemp.write_text(f"""#!{sys.executable}
import os, sys, tempfile
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ["TEST_DEVICE"]).parent
if args and args[-1].startswith("/"):
    template = Path(args[-1]); template.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=template.name.split(".XXXXXX")[0], dir=template.parent)
    os.close(descriptor)
elif "-d" in args: name = tempfile.mkdtemp(dir=root)
else:
    descriptor, name = tempfile.mkstemp(dir=root); os.close(descriptor)
print(name)
""")
        mktemp.chmod(0o755)
        for name, body in {"id": "echo 1000", "getent": "exit 1", "systemctl": 'if [ "$1" = is-active ]; then exit 1; fi'}.items():
            command = bin_dir / name
            command.write_text("#!/bin/sh\n" + body + "\n")
            command.chmod(0o755)
        for name, body in {
            "wacli": 'echo wacli-install >>"$TEST_LOG"',
            "nfc": 'mkdir -p "$TEST_DEVICE/etc/systemd/system"\ncp "$TEST_SOURCE/systemd/messagebox-nfc.service" "$TEST_DEVICE/etc/systemd/system/"',
            "comitup": 'mkdir -p "$TEST_DEVICE/etc/systemd/system/comitup.service.d"\ncp "$TEST_SOURCE/systemd/onboarding/comitup.service.d/messagebox.conf" "$TEST_DEVICE/etc/systemd/system/comitup.service.d/"',
        }.items():
            path = self.source / f"scripts/install/{name}.sh"
            path.write_text("#!/bin/sh\n" + body + "\n")
            path.chmod(0o755)
        script = self.source / "scripts/setup.sh"
        content = script.read_text()
        content = re.sub(r"/(opt|etc|var|run|usr/(?:local|lib|share))/", lambda match: str(self.device) + match.group(), content)
        content = content.replace("/usr/bin/python3", str(python))
        script.write_text(content)
        environment = {**os.environ, "PATH": str(bin_dir) + ":" + os.environ["PATH"],
                       "TEST_DEVICE": str(self.device), "TEST_SOURCE": str(self.source), "TEST_LOG": str(log)}
        result = subprocess.run(["sh", str(script), *arguments], env=environment, text=True, capture_output=True)
        return result, log.read_text() if log.exists() else ""

    def test_cloud_shell_install_starts_with_cloud_env_skips_wacli_and_records_release(self):
        result, operations = self.run_setup(["--transport", "cloud"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MSGBOX_TRANSPORT=cloud\n", (self.device / "etc/messagebox/env").read_text())
        self.assertNotIn("wacli-install", operations)
        self.assertNotIn("DEV FLOW", result.stdout)
        self.assertTrue((self.device / "opt/messagebox/release.json").is_file())
        self.assertFalse((self.device / "etc/messagebox-onboarding/configured").exists())
        self.assertFalse((self.device / "var/lib/messagebox-cloud/device.json").exists())

    def test_default_developer_shell_install_preserves_wacli(self):
        old_identity = self.device / "opt/messagebox/release.json"
        old_identity.parent.mkdir(parents=True)
        old_identity.write_text("old identity")
        result, operations = self.run_setup([], with_release=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MSGBOX_TRANSPORT=wacli\n", (self.device / "etc/messagebox/env").read_text())
        self.assertIn("wacli-install", operations)
        self.assertIn("DEV FLOW", result.stdout)
        self.assertFalse((self.device / "opt/messagebox/release.json").exists())

    def test_existing_cloud_setup_rejected_before_any_system_mutation(self):
        private = self.device / "var/lib/messagebox-cloud"
        private.mkdir(parents=True)
        (private / "private-state").write_text("retained")
        result, operations = self.run_setup(["--transport", "cloud"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("fresh Pi", result.stderr)
        self.assertEqual(len(operations.splitlines()), 1)
        self.assertIn("check-fresh", operations)
        self.assertEqual((private / "private-state").read_text(), "retained")
        self.assertFalse((self.device / "opt/messagebox").exists())

    def test_invalid_cli_and_missing_release_fail_before_install_changes(self):
        result, operations = self.run_setup(["--transport", "cloud"], with_release=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("pinned release", result.stderr)
        self.assertEqual(len(operations.splitlines()), 1)
        self.assertEqual(list(self.device.iterdir()), [])
        for arguments in (["--transport", "business"], ["--transport"], ["unknown"]):
            result = subprocess.run(["sh", str(ROOT / "scripts/setup.sh"), *arguments], text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
