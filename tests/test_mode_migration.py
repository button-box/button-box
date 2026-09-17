import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/install/messagebox-mode-migrate.py"
GENERATOR = ROOT / "systemd/messagebox-mode-generator"
SPEC = importlib.util.spec_from_file_location("mode_migration", SCRIPT)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class Boundary(Exception):
    pass


class Systemctl:
    def __init__(self, fixture, *, fail_after=None):
        self.fixture = fixture
        self.fail_after = fail_after
        self.mutations = 0
        self.active = False

    def _mutated(self):
        self.mutations += 1
        if self.fail_after == self.mutations:
            raise Boundary

    def __call__(self, command, **kwargs):
        if command == ["systemctl", "daemon-reload"]:
            module = migration._load_generator(
                self.fixture.destination,
                trusted_uid=self.fixture.uid,
            )
            module.generate(
                self.fixture.generated.parent,
                self.fixture.marker,
                trusted_uid=self.fixture.uid,
            )
            self._mutated()
        elif command == [
            "systemctl",
            "enable",
            "--now",
            migration.RECONCILE_PATH,
        ]:
            link = self.fixture.legacy / migration.RECONCILE_PATH
            if not link.is_symlink():
                link.symlink_to("/etc/systemd/system/messagebox-mode-reconcile.path")
            self.active = True
            self._mutated()
        elif command == [
            "systemctl",
            "is-active",
            "--quiet",
            migration.RECONCILE_PATH,
        ]:
            return subprocess.CompletedProcess(command, 0 if self.active else 3)
        return subprocess.CompletedProcess(command, 0)


class Fixture:
    def __init__(self, root, mode):
        self.root = Path(root)
        self.uid = os.getuid()
        self.marker = self.root / "etc/messagebox-onboarding/enabled"
        self.marker.parent.mkdir(parents=True)
        self.marker.parent.chmod(0o750)
        if mode == "setup":
            self.marker.write_bytes(b"enabled\n")
            self.marker.chmod(0o600)
        self.lock = self.root / "run/lock/mode.lock"
        self.lock.parent.mkdir(parents=True)
        self.destination = self.root / "usr/local/lib/systemd/system-generators/messagebox-mode-generator"
        self.legacy = self.root / "etc/systemd/system/multi-user.target.wants"
        self.legacy.mkdir(parents=True)
        self.target_wants = self.root / "etc/systemd/system/messagebox.target.wants"
        self.target_wants.mkdir(parents=True)
        (self.target_wants / "messagebox-button.service").symlink_to(
            "/etc/systemd/system/messagebox-button.service"
        )
        for name, unit in migration.LEGACY.items():
            (self.legacy / unit).symlink_to(migration.LEGACY_TARGETS[name])
        self.generated = self.root / "run/systemd/generator/multi-user.target.wants"

    def migrate(self, runner):
        return migration.migrate(
            GENERATOR,
            marker=self.marker,
            lock=self.lock,
            destination=self.destination,
            legacy_dir=self.legacy,
            generated_dir=self.generated,
            target_wants=self.target_wants,
            run=runner,
            trusted_uid=self.uid,
        )

    def selected_sources(self, expected_mode):
        sources = []
        for name, unit in migration.LEGACY.items():
            if (self.legacy / unit).is_symlink():
                sources.append(name)
        if self.destination.exists():
            module = migration._load_generator(self.destination, trusted_uid=self.uid)
            output = self.root / "selection-check"
            sources.append(module.generate(output, self.marker, trusted_uid=self.uid))
        self_case = set(sources)
        if not sources or self_case != {expected_mode}:
            raise AssertionError(f"unsafe selection prefix: {sources}")


class MigrationTests(unittest.TestCase):
    def test_both_modes_migrate_once_and_repeat_without_changing_component_wants(self):
        for selected_mode in ("runtime", "setup"):
            with self.subTest(mode=selected_mode), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory, selected_mode)
                component_before = os.readlink(
                    fixture.target_wants / "messagebox-button.service"
                )
                runner = Systemctl(fixture)
                self.assertEqual(fixture.migrate(runner), selected_mode)
                self.assertFalse((fixture.legacy / "messagebox.target").exists())
                self.assertFalse((fixture.legacy / "messagebox.target").is_symlink())
                self.assertFalse((fixture.legacy / "comitup.service").exists())
                self.assertFalse((fixture.legacy / "comitup.service").is_symlink())
                self.assertTrue((fixture.legacy / migration.RECONCILE_PATH).is_symlink())
                self.assertEqual(
                    os.readlink(fixture.target_wants / "messagebox-button.service"),
                    component_before,
                )
                self.assertEqual(fixture.migrate(Systemctl(fixture)), selected_mode)

    def test_each_completed_mutation_prefix_keeps_a_correct_selector(self):
        original_remove = migration._remove_link
        original_install = migration._atomic_install
        for selected_mode in ("runtime", "setup"):
            for failpoint in range(1, 6):
                with self.subTest(mode=selected_mode, failpoint=failpoint), tempfile.TemporaryDirectory() as directory:
                    fixture = Fixture(directory, selected_mode)
                    counter = {"value": 0}

                    def boundary_after(function):
                        def wrapped(*args, **kwargs):
                            result = function(*args, **kwargs)
                            counter["value"] += 1
                            if counter["value"] == failpoint:
                                raise Boundary
                            return result

                        return wrapped

                    runner = Systemctl(fixture)
                    original_call = runner.__call__

                    def run(command, **kwargs):
                        result = original_call(command, **kwargs)
                        if command[:2] in (["systemctl", "daemon-reload"], ["systemctl", "enable"]):
                            counter["value"] += 1
                            if counter["value"] == failpoint:
                                raise Boundary
                        return result

                    with mock.patch.object(migration, "_remove_link", boundary_after(original_remove)), mock.patch.object(
                        migration, "_atomic_install", boundary_after(original_install)
                    ):
                        with self.assertRaises(Boundary):
                            fixture.migrate(run)
                    fixture.selected_sources(selected_mode)

    def test_unsafe_legacy_link_stops_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory, "runtime")
            wrong = fixture.legacy / "comitup.service"
            wrong.unlink()
            wrong.symlink_to("/tmp/comitup.service")
            with self.assertRaises(migration.MigrationError):
                fixture.migrate(Systemctl(fixture))
            self.assertFalse(fixture.destination.exists())
            self.assertEqual(os.readlink(wrong), "/tmp/comitup.service")
            self.assertTrue((fixture.legacy / "messagebox.target").is_symlink())


if __name__ == "__main__":
    unittest.main()
