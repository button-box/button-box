import copy
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/install/bounded_update.py"
MIGRATION = ROOT / "scripts/install/messagebox-mode-migrate.py"
GENERATOR = ROOT / "systemd/messagebox-mode-generator"
RELEASE_MANIFEST_SCRIPT = ROOT / "scripts/dev/release-manifest.py"
SPEC = importlib.util.spec_from_file_location("bounded_update", SCRIPT)
bounded_update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bounded_update)
MANIFEST_SPEC = importlib.util.spec_from_file_location(
    "release_manifest_for_update_tests", RELEASE_MANIFEST_SCRIPT
)
release_manifest = importlib.util.module_from_spec(MANIFEST_SPEC)
MANIFEST_SPEC.loader.exec_module(release_manifest)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Systemctl:
    def __init__(self, fixture):
        self.fixture = fixture
        self.states = {
            unit: {"active": "inactive", "enabled": "static"}
            for unit in bounded_update.UNITS
        }
        if fixture.mode == "runtime":
            self.states["messagebox.target"] = {"active": "active", "enabled": "enabled"}
            self.states["messagebox-dash.service"] = {
                "active": "active",
                "enabled": "static",
            }
            self.states["comitup.service"]["enabled"] = "disabled"
        else:
            self.states["comitup.service"] = {"active": "active", "enabled": "enabled"}
            self.states["messagebox-onboarding-home.service"] = {
                "active": "active",
                "enabled": "static",
            }
            self.states["messagebox.target"]["enabled"] = "disabled"
        self.states["messagebox-mode-reconcile.path"]["enabled"] = "disabled"
        self.commands = []
        self.started = []
        self.ever_activated = []
        self.fail_start_once = None
        self.fail_operation_once = None

    def _reload_generator(self):
        if self.fixture.generated.exists():
            shutil.rmtree(self.fixture.generated)
        generator = self.fixture.generator
        if not generator.exists():
            return
        loader = importlib.machinery.SourceFileLoader(
            f"fixture_generator_{len(self.commands)}", str(generator)
        )
        specification = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        module.generate(
            self.fixture.generated.parent,
            self.fixture.marker,
            trusted_uid=os.getuid(),
        )

    def __call__(self, command, **unused):
        self.commands.append(command[:])
        if self.fail_operation_once == command[1]:
            self.fail_operation_once = None
            raise subprocess.CalledProcessError(1, command)
        if command[:2] == ["systemctl", "is-active"]:
            if command[2] == "--quiet":
                unit = command[3]
                return subprocess.CompletedProcess(
                    command, 0 if self.states[unit]["active"] == "active" else 3
                )
            unit = command[2]
            value = self.states[unit]["active"]
            return subprocess.CompletedProcess(command, 0 if value == "active" else 3, value + "\n")
        if command[:2] == ["systemctl", "is-enabled"]:
            unit = command[2]
            value = self.states[unit]["enabled"]
            return subprocess.CompletedProcess(command, 0 if value == "enabled" else 1, value + "\n")
        if command == ["systemctl", "daemon-reload"]:
            self._reload_generator()
            return subprocess.CompletedProcess(command, 0)
        if command[:3] == ["systemctl", "enable", "--now"]:
            unit = command[3]
            self.states[unit] = {"active": "active", "enabled": "enabled"}
            self.ever_activated.append(unit)
            link = self.fixture.legacy / unit
            link.unlink(missing_ok=True)
            link.symlink_to(f"/etc/systemd/system/{unit}")
            return subprocess.CompletedProcess(command, 0)
        if command[:2] == ["systemctl", "stop"]:
            for unit in command[2:]:
                self.states[unit]["active"] = "inactive"
            return subprocess.CompletedProcess(command, 0)
        if command[:3] == [
            "systemctl",
            "--job-mode=ignore-dependencies",
            "start",
        ]:
            unit = command[3]
            if self.fail_start_once == unit:
                self.fail_start_once = None
                raise subprocess.CalledProcessError(1, command)
            self.states[unit]["active"] = "active"
            self.started.append(unit)
            self.ever_activated.append(unit)
            return subprocess.CompletedProcess(command, 0)
        if command[:2] == ["systemctl", "start"]:
            units = command[2:]
            if self.fail_start_once and self.fail_start_once in units:
                self.fail_start_once = None
                raise subprocess.CalledProcessError(1, command)
            for unit in units:
                self.states[unit]["active"] = "active"
                self.ever_activated.append(unit)
                if unit == "messagebox.target":
                    self.states["messagebox-button.service"]["active"] = "active"
                    self.ever_activated.append("messagebox-button.service")
                elif unit == "comitup.service":
                    self.states["comitup-web.service"]["active"] = "active"
                    self.ever_activated.append("comitup-web.service")
            self.started.extend(units)
            return subprocess.CompletedProcess(command, 0)
        if command[:2] in (["systemctl", "enable"], ["systemctl", "disable"]):
            operation = command[1]
            offset = 3 if command[2] == "--runtime" else 2
            for unit in command[offset:]:
                self.states[unit]["enabled"] = (
                    f"enabled{'-runtime' if offset == 3 else ''}"
                    if operation == "enable"
                    else "disabled"
                )
                link = self.fixture.legacy / unit
                if operation == "disable":
                    link.unlink(missing_ok=True)
                elif unit in {
                    "comitup.service",
                    "messagebox.target",
                    "messagebox-mode-reconcile.path",
                }:
                    link.unlink(missing_ok=True)
                    target = (
                        "/usr/lib/systemd/system/comitup.service"
                        if unit == "comitup.service"
                        else f"/etc/systemd/system/{unit}"
                    )
                    link.symlink_to(target)
            return subprocess.CompletedProcess(command, 0)
        if command[:2] == ["systemctl", "mask"]:
            offset = 3 if command[2] == "--runtime" else 2
            for unit in command[offset:]:
                self.states[unit]["enabled"] = (
                    "masked-runtime" if offset == 3 else "masked"
                )
            return subprocess.CompletedProcess(command, 0)
        raise AssertionError(f"unexpected command: {command}")


class Fixture:
    def __init__(self, directory, mode="runtime"):
        self.base = Path(directory).resolve()
        self.mode = mode
        self.source = self.base / "source"
        self.root = self.base / "device"
        self.backup = self.base / "backups/release"
        self.manifest = self.source / "manifest.json"
        self.marker = self.root / "etc/messagebox-onboarding/enabled"
        self.legacy = self.root / "etc/systemd/system/multi-user.target.wants"
        self.generated = self.root / "run/systemd/generator/multi-user.target.wants"
        self.generator = self.root / bounded_update.MODE_GENERATOR.removeprefix("/")
        self.sample = self.root / "opt/messagebox/messagebox/__init__.py"
        self.metadata = self.root / bounded_update.RELEASE_METADATA.removeprefix("/")
        self.private_files = [
            self.root / "etc/messagebox/env",
            self.root / "var/lib/messagebox/state/messages.sqlite",
            self.root / "var/lib/messagebox-onboarding/session.json",
        ]
        self.source.mkdir()
        self.root.mkdir()
        self.marker.parent.mkdir(parents=True)
        self.marker.parent.chmod(0o750)
        if mode == "setup":
            self.marker.write_bytes(b"enabled\n")
            self.marker.chmod(0o600)
        self.legacy.mkdir(parents=True)
        selected = "messagebox.target" if mode == "runtime" else "comitup.service"
        selected_target = (
            "/etc/systemd/system/messagebox.target"
            if mode == "runtime"
            else "/usr/lib/systemd/system/comitup.service"
        )
        (self.legacy / selected).symlink_to(selected_target)
        (self.root / "etc/systemd/system/messagebox.target.wants").mkdir(parents=True)
        (self.root / "run/lock").mkdir(parents=True)
        self.sample.parent.mkdir(parents=True)
        self.sample.write_text("VALUE = 'old'\n")
        self.sample.chmod(0o600)
        if mode == "runtime":
            self.metadata.write_text('{"version":"old"}\n')
        for index, path in enumerate(self.private_files):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"private fixture {index}\n")
        self.private_hashes = {path: sha256(path) for path in self.private_files}

        canonical_paths = release_manifest.installed_paths(ROOT)
        entries = []
        for source, installed in sorted(canonical_paths.items()):
            content = (ROOT / source).read_bytes()
            if source == "messagebox/__init__.py":
                content = b'"""Synthetic updated package."""\n'
            path = self.source / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(stat.S_IMODE((ROOT / source).stat().st_mode))
            entries.append(
                {
                    "source": source,
                    "installed": installed,
                    "sha256": sha256(path),
                }
            )
        manifest_tool = self.source / "scripts/dev/release-manifest.py"
        manifest_tool.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(RELEASE_MANIFEST_SCRIPT, manifest_tool)
        manifest_tool.chmod(0o644)
        self.manifest_data = {
            "version": "0.1.0-test.1",
            "commit": "a" * 40,
            "scope": "Synthetic test package.",
            "files": entries,
        }
        self.write_manifest()
        self.systemctl = Systemctl(self)
        self.original_units = copy.deepcopy(self.systemctl.states)
        self.original_link = (selected, selected_target)
        self.original_metadata = self.metadata.read_bytes() if self.metadata.exists() else None

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.manifest_data, indent=2) + "\n")

    def assert_private_unchanged(self, test_case):
        test_case.assertEqual(
            {path: sha256(path) for path in self.private_files}, self.private_hashes
        )

    def assert_original_state(self, test_case):
        test_case.assertEqual(self.sample.read_text(), "VALUE = 'old'\n")
        test_case.assertEqual(stat.S_IMODE(self.sample.stat().st_mode), 0o600)
        test_case.assertFalse(self.generator.exists())
        test_case.assertEqual(self.systemctl.states, self.original_units)
        selected, target = self.original_link
        test_case.assertTrue((self.legacy / selected).is_symlink())
        test_case.assertEqual(os.readlink(self.legacy / selected), target)
        other = "comitup.service" if selected == "messagebox.target" else "messagebox.target"
        test_case.assertFalse((self.legacy / other).is_symlink())
        test_case.assertFalse((self.legacy / "messagebox-mode-reconcile.path").is_symlink())
        test_case.assertEqual(self.marker.exists(), self.mode == "setup")
        if self.mode == "setup":
            test_case.assertEqual(self.marker.read_bytes(), b"enabled\n")
            test_case.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)
        if self.original_metadata is None:
            test_case.assertFalse(self.metadata.exists())
        else:
            test_case.assertEqual(self.metadata.read_bytes(), self.original_metadata)
        self.assert_private_unchanged(test_case)


class BoundedUpdateTests(unittest.TestCase):
    def test_apply_and_rollback_restore_both_boot_modes(self):
        for mode in ("runtime", "setup"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory, mode)
                expected_active = {
                    unit
                    for unit, state in fixture.original_units.items()
                    if state["active"] == "active"
                }
                bounded_update.apply(
                    fixture.source,
                    fixture.manifest,
                    fixture.backup,
                    root=fixture.root,
                    run=fixture.systemctl,
                )

                self.assertEqual(
                    fixture.sample.read_text(), '"""Synthetic updated package."""\n'
                )
                self.assertEqual(stat.S_IMODE(fixture.generator.stat().st_mode), 0o755)
                self.assertEqual(
                    json.loads(fixture.metadata.read_text())["commit"], "a" * 40
                )
                self.assertEqual(
                    json.loads(fixture.metadata.read_text())["manifest_sha256"],
                    sha256(fixture.manifest),
                )
                self.assertEqual(
                    stat.S_IMODE(fixture.backup.stat().st_mode), 0o700
                )
                self.assertEqual(
                    stat.S_IMODE((fixture.backup / "state.json").stat().st_mode),
                    0o600,
                )
                self.assertEqual(
                    set(fixture.systemctl.started), expected_active
                )
                self.assertEqual(
                    set(fixture.systemctl.ever_activated),
                    expected_active | {"messagebox-mode-reconcile.path"},
                )
                direct_start_commands = [
                    command
                    for command in fixture.systemctl.commands
                    if "start" in command
                ]
                self.assertTrue(direct_start_commands)
                self.assertTrue(
                    all(
                        command[1:3]
                        == ["--job-mode=ignore-dependencies", "start"]
                        for command in direct_start_commands
                    )
                )
                self.assertTrue(
                    (fixture.legacy / "messagebox-mode-reconcile.path").is_symlink()
                )
                self.assertFalse((fixture.legacy / "messagebox.target").is_symlink())
                self.assertFalse((fixture.legacy / "comitup.service").is_symlink())
                self.assertEqual(fixture.marker.exists(), mode == "setup")
                if mode == "setup":
                    self.assertEqual(fixture.marker.read_bytes(), b"enabled\n")
                    self.assertEqual(stat.S_IMODE(fixture.marker.stat().st_mode), 0o600)
                fixture.assert_private_unchanged(self)

                fixture.systemctl.started.clear()
                fixture.systemctl.ever_activated.clear()
                bounded_update.rollback(
                    fixture.backup,
                    root=fixture.root,
                    run=fixture.systemctl,
                )
                fixture.assert_original_state(self)
                self.assertEqual(set(fixture.systemctl.started), expected_active)
                self.assertEqual(
                    set(fixture.systemctl.ever_activated), expected_active
                )

    def test_failure_after_install_rolls_back_files_links_and_units(self):
        for mode in ("runtime", "setup"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory, mode)
                failing_unit = (
                    "messagebox-dash.service"
                    if mode == "runtime"
                    else "messagebox-onboarding-home.service"
                )
                fixture.systemctl.fail_start_once = failing_unit
                with self.assertRaisesRegex(
                    bounded_update.UpdateError, "recorded state was restored"
                ):
                    bounded_update.apply(
                        fixture.source,
                        fixture.manifest,
                        fixture.backup,
                        root=fixture.root,
                        run=fixture.systemctl,
                    )
                fixture.assert_original_state(self)

    def test_each_transaction_prefix_automatically_rolls_back(self):
        cases = ("stop", "install", "daemon-reload", "enable")
        original_install = bounded_update._atomic_install
        for failure in cases:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                install_calls = 0

                def install_then_fail(*arguments, **keywords):
                    nonlocal install_calls
                    install_calls += 1
                    if failure == "install" and install_calls == 3:
                        raise OSError("synthetic install boundary")
                    return original_install(*arguments, **keywords)

                if failure != "install":
                    fixture.systemctl.fail_operation_once = failure
                with mock.patch.object(
                    bounded_update, "_atomic_install", side_effect=install_then_fail
                ):
                    with self.assertRaisesRegex(
                        bounded_update.UpdateError, "recorded state was restored"
                    ):
                        bounded_update.apply(
                            fixture.source,
                            fixture.manifest,
                            fixture.backup,
                            root=fixture.root,
                            run=fixture.systemctl,
                        )
                fixture.assert_original_state(self)

    def test_hash_path_and_migration_checks_precede_mutation(self):
        cases = (
            "hash",
            "path",
            "partial",
            "generator-mode",
            "staging-mode",
            "legacy-link",
        )
        for problem in cases:
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                if problem == "hash":
                    fixture.manifest_data["files"][0]["sha256"] = "0" * 64
                    fixture.write_manifest()
                elif problem == "path":
                    fixture.manifest_data["files"][0]["installed"] = "/etc/unsafe.conf"
                    fixture.write_manifest()
                elif problem == "partial":
                    fixture.manifest_data["files"].pop()
                    fixture.write_manifest()
                elif problem == "generator-mode":
                    (fixture.source / "systemd/messagebox-mode-generator").chmod(0o644)
                elif problem == "staging-mode":
                    fixture.source.chmod(0o775)
                else:
                    link = fixture.legacy / "comitup.service"
                    link.symlink_to("/tmp/untrusted.service")
                with self.assertRaises((bounded_update.UpdateError, OSError)):
                    bounded_update.apply(
                        fixture.source,
                        fixture.manifest,
                        fixture.backup,
                        root=fixture.root,
                        run=fixture.systemctl,
                    )
                self.assertFalse(fixture.backup.exists())
                self.assertEqual(fixture.sample.read_text(), "VALUE = 'old'\n")
                fixture.assert_private_unchanged(self)
                mutating = [
                    command
                    for command in fixture.systemctl.commands
                    if command[1] not in {"is-active", "is-enabled"}
                ]
                self.assertEqual(mutating, [])

    def test_outer_lock_rejects_overlapping_apply_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            with bounded_update._update_lock(fixture.root):
                with self.assertRaisesRegex(
                    bounded_update.UpdateError,
                    "another bounded update operation is active",
                ):
                    bounded_update.apply(
                        fixture.source,
                        fixture.manifest,
                        fixture.backup,
                        root=fixture.root,
                        run=fixture.systemctl,
                    )
                with self.assertRaisesRegex(
                    bounded_update.UpdateError,
                    "another bounded update operation is active",
                ):
                    bounded_update.rollback(
                        fixture.backup,
                        root=fixture.root,
                        run=fixture.systemctl,
                    )

            self.assertFalse(fixture.backup.exists())
            self.assertEqual(fixture.systemctl.commands, [])
            fixture.assert_original_state(self)


if __name__ == "__main__":
    unittest.main()
