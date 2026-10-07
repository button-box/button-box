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
        self.states["messagebox-wifi-watchdog.timer"]["enabled"] = "disabled"
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
        self.part_of = {}
        self.comitup_portal = "messagebox-onboarding-home.service" if fixture.mode == "setup" else None

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
        if command[:4] == ["systemctl", "show", "--property=ConsistsOf", "--value"]:
            unit = command[4]
            dependents = [name for name, parent in self.part_of.items() if parent == unit]
            return subprocess.CompletedProcess(command, 0, " ".join(dependents) + "\n")
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
            stopped = list(command[2:])
            for unit in stopped:
                self.states[unit]["active"] = "inactive"
                stopped.extend(name for name, parent in self.part_of.items()
                               if parent == unit and name not in stopped)
            return subprocess.CompletedProcess(command, 0)
        if command[:3] == [
            "systemctl",
            "--job-mode=ignore-dependencies",
            "start",
        ]:
            unit = command[3]
            if (unit in {"comitup-web.service", "messagebox-onboarding-home.service"}
                    and self.states["comitup.service"]["active"] == "active"):
                # Installed ComItUp replaces these jobs during its startup.
                raise subprocess.CalledProcessError(1, command, stderr="Job canceled")
            if self.fail_start_once == unit:
                self.fail_start_once = None
                raise subprocess.CalledProcessError(1, command)
            self.states[unit]["active"] = "active"
            self.started.append(unit)
            self.ever_activated.append(unit)
            if unit == "comitup.service" and self.comitup_portal:
                self.states[self.comitup_portal]["active"] = "active"
                self.ever_activated.append(self.comitup_portal)
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


class FakeTime:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class BoundedUpdateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeTime()
        patcher = mock.patch.object(bounded_update, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retired_audio_removed_and_restored_without_touching_family_media(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            retired = [fixture.root / path.removeprefix("/") for path in bounded_update.RETIRED_SOUNDS]
            for path in retired:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"old bundled audio")
            custom = fixture.root / "opt/messagebox/sounds/nfc/family.wav"
            custom.parent.mkdir(parents=True)
            custom.write_bytes(b"synthetic family clip")
            bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                 root=fixture.root, run=fixture.systemctl)
            self.assertTrue(all(not path.exists() for path in retired))
            self.assertEqual(custom.read_bytes(), b"synthetic family clip")
            for relative in bounded_update.SOUND_SOURCES:
                self.assertEqual((fixture.root / "opt/messagebox" / relative).read_bytes(), (ROOT / relative).read_bytes())
            bounded_update.rollback(fixture.backup, root=fixture.root, run=fixture.systemctl)
            self.assertTrue(all(path.read_bytes() == b"old bundled audio" for path in retired))
            self.assertFalse((fixture.root / "opt/messagebox/sounds/cues/cue-press.wav").exists())
            self.assertEqual(custom.read_bytes(), b"synthetic family clip")

    def test_retired_symlink_rejected_before_service_or_file_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            retired = fixture.root / bounded_update.RETIRED_SOUNDS[0].removeprefix("/")
            retired.parent.mkdir(parents=True)
            retired.symlink_to(fixture.private_files[0])
            with self.assertRaisesRegex(bounded_update.UpdateError, "retired sound"):
                bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                     root=fixture.root, run=fixture.systemctl)
            self.assertFalse(fixture.backup.exists())
            self.assertFalse(fixture.systemctl.commands)
            fixture.assert_private_unchanged(self)

    def add_auxiliary(self, fixture, *, active="active", nested=False):
        unit = "example-analytics.service"
        parent = "messagebox.target"
        fixture.systemctl.part_of[unit] = parent
        fixture.systemctl.states[unit] = {"active": active, "enabled": "enabled"}
        if nested:
            fixture.systemctl.part_of["example-metrics.service"] = unit
            fixture.systemctl.states["example-metrics.service"] = {
                "active": "active", "enabled": "static",
            }
        unit_file = fixture.root / "etc/systemd/system" / unit
        unit_file.write_text("[Unit]\nPartOf=messagebox.target\n")
        link = fixture.root / "etc/systemd/system/messagebox.target.wants" / unit
        link.symlink_to(unit_file)
        return unit, unit_file, link

    def test_active_reverse_part_of_closure_restored_on_apply_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            unit, unit_file, link = self.add_auxiliary(fixture, nested=True)
            original = copy.deepcopy(fixture.systemctl.states)
            original_file = unit_file.read_bytes()
            original_link = os.readlink(link)
            bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                 root=fixture.root, run=fixture.systemctl)
            state = json.loads((fixture.backup / "state.json").read_text())
            self.assertEqual(state["format"], 2)
            self.assertEqual(state["auxiliary_units"],
                             [unit, "example-metrics.service"])
            self.assertEqual(fixture.systemctl.states[unit], original[unit])
            self.assertEqual(fixture.systemctl.states["example-metrics.service"],
                             original["example-metrics.service"])
            self.assertEqual(unit_file.read_bytes(), original_file)
            self.assertEqual(os.readlink(link), original_link)
            self.assertFalse(any(command[1] in {"enable", "disable"} and unit in command
                                 for command in fixture.systemctl.commands))
            bounded_update.rollback(fixture.backup, root=fixture.root,
                                    run=fixture.systemctl)
            self.assertEqual(fixture.systemctl.states, original)
            self.assertEqual(unit_file.read_bytes(), original_file)
            self.assertEqual(os.readlink(link), original_link)

    def test_inactive_auxiliary_is_never_started(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            unit, _, _ = self.add_auxiliary(fixture, active="inactive")
            bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                 root=fixture.root, run=fixture.systemctl)
            self.assertEqual(json.loads((fixture.backup / "state.json").read_text())
                             ["auxiliary_units"], [])
            bounded_update.rollback(fixture.backup, root=fixture.root,
                                    run=fixture.systemctl)
            self.assertEqual(fixture.systemctl.states[unit]["active"], "inactive")
            self.assertNotIn(unit, fixture.systemctl.started)

    def test_inactive_intermediate_still_discovers_active_descendant(self):
        for managed in (False, True):
            with self.subTest(managed=managed), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                unit, _, _ = self.add_auxiliary(fixture)
                if managed:
                    bridge = "messagebox-button.service"
                else:
                    bridge = "example-bridge.target"
                    fixture.systemctl.states[bridge] = {
                        "active": "inactive", "enabled": "static",
                    }
                fixture.systemctl.part_of[bridge] = "messagebox.target"
                fixture.systemctl.part_of[unit] = bridge
                fixture.original_units = copy.deepcopy(fixture.systemctl.states)

                bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                     root=fixture.root, run=fixture.systemctl)
                state = json.loads((fixture.backup / "state.json").read_text())
                self.assertEqual(state["auxiliary_units"], [unit])
                self.assertEqual(fixture.systemctl.states[bridge]["active"], "inactive")
                self.assertEqual(fixture.systemctl.states[unit]["active"], "active")
                self.assertNotIn(bridge, fixture.systemctl.started)

                bounded_update.rollback(fixture.backup, root=fixture.root,
                                        run=fixture.systemctl)
                fixture.assert_original_state(self)

    def test_failed_apply_restores_active_auxiliary(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            unit, _, _ = self.add_auxiliary(fixture)
            fixture.original_units = copy.deepcopy(fixture.systemctl.states)
            fixture.systemctl.fail_start_once = unit
            with self.assertRaisesRegex(bounded_update.UpdateError,
                                        "recorded state was restored"):
                bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                     root=fixture.root, run=fixture.systemctl)
            self.assertEqual(fixture.systemctl.states[unit]["active"], "active")
            fixture.assert_original_state(self)

    def test_failed_auxiliary_verification_triggers_automatic_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            unit, _, _ = self.add_auxiliary(fixture)
            fixture.original_units = copy.deepcopy(fixture.systemctl.states)
            failed_once = False

            def failed_health(command, **options):
                nonlocal failed_once
                if (command == ["systemctl", "is-active", unit]
                        and unit in fixture.systemctl.started and not failed_once):
                    failed_once = True
                    return subprocess.CompletedProcess(command, 3, "failed\n")
                return fixture.systemctl(command, **options)

            with self.assertRaisesRegex(bounded_update.UpdateError,
                                        "recorded state was restored"):
                bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                     root=fixture.root, run=failed_health)
            self.assertTrue(failed_once)
            fixture.assert_original_state(self)

    def test_auxiliary_discovery_rejects_transition_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            self.add_auxiliary(fixture, active="activating")
            with self.assertRaisesRegex(bounded_update.UpdateError,
                                        "auxiliary unit is still transitioning"):
                bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                     root=fixture.root, run=fixture.systemctl)
            self.assertFalse(fixture.backup.exists())
            self.assertTrue(all(command[1] in {"show", "is-active", "is-enabled"}
                                for command in fixture.systemctl.commands))

    def test_auxiliary_discovery_rejects_invalid_name_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)

            def invalid_relation(command, **options):
                if command == ["systemctl", "show", "--property=ConsistsOf",
                               "--value", "messagebox.target"]:
                    return subprocess.CompletedProcess(command, 0, "--bad.service\n")
                return fixture.systemctl(command, **options)

            with self.assertRaisesRegex(bounded_update.UpdateError,
                                        "invalid auxiliary unit name"):
                bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                     root=fixture.root, run=invalid_relation)
            self.assertFalse(fixture.backup.exists())
            self.assertTrue(all(command[1] in {"show", "is-active", "is-enabled"}
                                for command in fixture.systemctl.commands))

    def test_backup_rejects_invalid_auxiliary_names_before_mutation(self):
        invalid = ("--bad.service", "bad/name.service", "bad service",
                   "messagebox.target", "example-analytics.service", None, 7, {})
        for name in invalid:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                unit, _, _ = self.add_auxiliary(fixture)
                bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                     root=fixture.root, run=fixture.systemctl)
                state_path = fixture.backup / "state.json"
                state = json.loads(state_path.read_text())
                state["auxiliary_units"] = [unit, name] if name == unit else [name]
                state_path.write_text(json.dumps(state))
                fixture.systemctl.commands.clear()
                with self.assertRaisesRegex(bounded_update.UpdateError,
                                            "backup auxiliary unit state is invalid"):
                    bounded_update.rollback(fixture.backup, root=fixture.root,
                                            run=fixture.systemctl)
                self.assertEqual(fixture.systemctl.commands, [])

    def test_version_one_backup_remains_rollback_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                 root=fixture.root, run=fixture.systemctl)
            state_path = fixture.backup / "state.json"
            state = json.loads(state_path.read_text())
            state["format"] = 1
            del state["auxiliary_units"]
            state_path.write_text(json.dumps(state))
            bounded_update.rollback(fixture.backup, root=fixture.root,
                                    run=fixture.systemctl)
            fixture.assert_original_state(self)

    def test_restored_setup_waits_through_inactive_and_transitional_bounces(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, "setup")
            home = "messagebox-onboarding-home.service"
            voice = "messagebox-onboarding-voice-gate.service"
            fixture.systemctl.states[voice]["active"] = "active"
            expected = copy.deepcopy(fixture.systemctl.states)
            samples = [
                {home: "inactive", voice: "activating"},
                {home: "active", voice: "active"},
                {home: "deactivating", voice: "reloading"},
                {home: "active", voice: "active"},
            ]
            reads = 0

            def restored_state(command, **options):
                nonlocal reads
                if command == ["systemctl", "is-active", bounded_update.UNITS[0]]:
                    sample = samples[min(reads, len(samples) - 1)]
                    for unit, state in sample.items():
                        fixture.systemctl.states[unit]["active"] = state
                    reads += 1
                return fixture.systemctl(command, **options)

            bounded_update._verify_active(expected, restored_state)
            self.assertGreaterEqual(self.clock.now, 3.5)
            self.assertEqual(fixture.systemctl.states, expected)
            self.assertTrue(all(command[1] in {"is-active", "is-enabled"}
                                for command in fixture.systemctl.commands))

    def test_restored_state_rejects_persistent_missing_extra_and_transitioning_units(self):
        cases = [("messagebox-dash.service", "inactive"),
                 ("comitup.service", "active"),
                 ("messagebox-dash.service", "activating")]
        for unit, observed in cases:
            with self.subTest(unit=unit, observed=observed), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                fixture.systemctl.states[unit]["active"] = observed
                started = self.clock.now
                with self.assertRaises(bounded_update.UpdateError) as caught:
                    bounded_update._verify_active(fixture.original_units, fixture.systemctl)
                self.assertIn("did not settle within 30s", str(caught.exception))
                self.assertIn(unit, str(caught.exception))
                self.assertIn(f"observed {observed}", str(caught.exception))
                self.assertEqual(self.clock.now - started, bounded_update.UNIT_SETTLE_TIMEOUT)

    def test_new_failed_unit_is_rejected_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            unit = "messagebox-dash.service"
            fixture.systemctl.states[unit]["active"] = "failed"
            with self.assertRaisesRegex(bounded_update.UpdateError, f"restored managed unit failed: {unit}"):
                bounded_update._verify_active(fixture.original_units, fixture.systemctl)
            self.assertEqual(self.clock.now, 0)

    def test_transitional_initial_snapshot_is_rejected_before_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.systemctl.states["messagebox-dash.service"]["active"] = "activating"
            with self.assertRaisesRegex(bounded_update.UpdateError, "still transitioning"):
                bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                     root=fixture.root, run=fixture.systemctl)
            self.assertFalse(fixture.backup.exists())
            self.assertEqual(fixture.sample.read_text(), "VALUE = 'old'\n")
            fixture.assert_private_unchanged(self)

    def test_rollback_stops_units_mid_transition_before_restoring(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                 root=fixture.root, run=fixture.systemctl)
            unit = "messagebox-dash.service"
            fixture.systemctl.states[unit]["active"] = "activating"
            fixture.systemctl.commands.clear()
            bounded_update.rollback(fixture.backup, root=fixture.root, run=fixture.systemctl)
            self.assertTrue(any(command[:2] == ["systemctl", "stop"] and unit in command[2:]
                                for command in fixture.systemctl.commands))
            fixture.assert_original_state(self)

    def test_verification_queries_share_the_bounded_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            options_seen = []

            def slow_query(command, **options):
                options_seen.append(options)
                self.clock.now += bounded_update.UNIT_SETTLE_TIMEOUT + 1
                return fixture.systemctl(command, **options)

            with self.assertRaisesRegex(bounded_update.UpdateError, "timed out reading managed unit state"):
                bounded_update._verify_active(fixture.original_units, slow_query)
            self.assertEqual(len(options_seen), 1)
            self.assertEqual(options_seen[0]["timeout"], bounded_update.UNIT_SETTLE_TIMEOUT)

    def test_final_query_crossing_deadline_cannot_report_stable_success(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            scans = 0

            def final_query_overruns(command, **options):
                nonlocal scans
                if command == ["systemctl", "is-active", bounded_update.UNITS[0]]:
                    scans += 1
                result = fixture.systemctl(command, **options)
                if scans == 2 and command == ["systemctl", "is-enabled", bounded_update.UNITS[-1]]:
                    self.clock.now = bounded_update.UNIT_SETTLE_TIMEOUT + 0.1
                return result

            with self.assertRaisesRegex(bounded_update.UpdateError,
                                        "verification finished after its deadline"):
                bounded_update._verify_active(fixture.original_units, final_query_overruns)
            self.assertEqual(scans, 2)
            self.assertEqual(fixture.systemctl.states, fixture.original_units)

    def test_unsupported_state_and_raw_errors_do_not_leak_output(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.systemctl.states["messagebox-dash.service"]["active"] = "private unexpected output"
            with self.assertRaises(bounded_update.UpdateError) as caught:
                bounded_update._verify_active(fixture.original_units, fixture.systemctl)
            self.assertNotIn("private unexpected output", str(caught.exception))
            self.assertIn("messagebox-dash.service", str(caught.exception))
            self.assertEqual(bounded_update._failure_cause(OSError("private path")), "OSError")
            self.assertEqual(bounded_update._failure_cause(bounded_update.UpdateError("safe unit failure")),
                             "safe unit failure")

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

                for name in ("hello_piano", "sunshine", "bouncy", "sing_along", "island", "hello", "ukulele"):
                    for suffix in (".wav", ".lamp.json"):
                        installed = fixture.root / f"opt/messagebox/ringtones/{name}{suffix}"
                        self.assertEqual(installed.read_bytes(), (ROOT / f"sounds/ringtones/{name}{suffix}").read_bytes())
                        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o644)

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
                added_runtime_units = {"messagebox-wifi-watchdog.timer"} if mode == "runtime" else set()
                expected_direct_starts = expected_active | added_runtime_units
                expected_direct_starts -= (
                    {fixture.systemctl.comitup_portal} if mode == "setup" else set()
                )
                self.assertEqual(
                    set(fixture.systemctl.started), expected_direct_starts
                )
                self.assertEqual(
                    set(fixture.systemctl.ever_activated),
                    expected_active | {"messagebox-mode-reconcile.path"} | added_runtime_units,
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
                self.assertEqual(list((fixture.root / "opt/messagebox/ringtones").iterdir()), [])
                fixture.assert_original_state(self)
                self.assertEqual(set(fixture.systemctl.started), expected_direct_starts - added_runtime_units)
                self.assertEqual(
                    set(fixture.systemctl.ever_activated), expected_active
                )

    def test_comitup_exclusively_restores_home_or_hotspot_portal(self):
        portals = {"messagebox-onboarding-home.service", "comitup-web.service"}
        for portal in sorted(portals):
            with self.subTest(portal=portal), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory, "setup")
                fixture.systemctl.comitup_portal = portal
                for unit in portals:
                    fixture.systemctl.states[unit]["active"] = "active" if unit == portal else "inactive"
                fixture.original_units = copy.deepcopy(fixture.systemctl.states)
                bounded_update.apply(fixture.source, fixture.manifest, fixture.backup,
                                     root=fixture.root, run=fixture.systemctl)
                expected = {unit: state["active"] for unit, state in fixture.original_units.items()}
                expected["messagebox-mode-reconcile.path"] = "active"
                self.assertEqual({unit: state["active"] for unit, state in fixture.systemctl.states.items()},
                                 expected)
                bounded_update.rollback(fixture.backup, root=fixture.root, run=fixture.systemctl)
                fixture.assert_original_state(self)
                self.assertTrue(portals.isdisjoint(fixture.systemctl.started))
                self.assertIn(portal, fixture.systemctl.ever_activated)

    def test_comitup_owned_portals_still_require_exact_healthy_states(self):
        cases = [("messagebox-onboarding-home.service", "inactive"),
                 ("comitup-web.service", "active"),
                 ("comitup-web.service", "failed")]
        for unit, observed in cases:
            with self.subTest(unit=unit, observed=observed), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory, "setup")
                bounded_update._restore_active(fixture.original_units, fixture.systemctl)
                fixture.systemctl.states[unit]["active"] = observed
                with self.assertRaises(bounded_update.UpdateError) as caught:
                    bounded_update._verify_active(fixture.original_units, fixture.systemctl)
                self.assertIn(unit, str(caught.exception))
                fixture.assert_private_unchanged(self)

    def test_portal_without_active_comitup_retains_recorded_manual_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            portal = "messagebox-onboarding-home.service"
            fixture.systemctl.states[portal]["active"] = "active"
            expected = copy.deepcopy(fixture.systemctl.states)
            fixture.systemctl.states[portal]["active"] = "inactive"
            bounded_update._restore_active(expected, fixture.systemctl)
            self.assertIn(portal, fixture.systemctl.started)
            bounded_update._verify_active(expected, fixture.systemctl)

    def test_failure_after_install_rolls_back_files_links_and_units(self):
        for mode in ("runtime", "setup"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory, mode)
                failing_unit = (
                    "messagebox-dash.service"
                    if mode == "runtime"
                    else "comitup.service"
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
                    if command[1] not in {"show", "is-active", "is-enabled"}
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
