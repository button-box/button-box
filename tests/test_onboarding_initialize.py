import fcntl
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from messagebox.onboarding import initialize
from messagebox.onboarding.paths import InitializerPaths
from messagebox.onboarding.state import StateStore


class TTY(io.StringIO):
    def isatty(self):
        return True


class InspectingTTY(TTY):
    def __init__(self, value, inspect):
        super().__init__(value)
        self.inspect = inspect

    def readline(self, *args, **kwargs):
        self.inspect()
        return super().readline(*args, **kwargs)


class InitializeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.paths = InitializerPaths(
            config_dir=self.root / "etc/messagebox-onboarding",
            config=self.root / "etc/messagebox-onboarding/config.json",
            configured=self.root / "etc/messagebox-onboarding/configured",
            enabled=self.root / "etc/messagebox-onboarding/enabled",
            state_dir=self.root / "var/lib/messagebox-onboarding",
            state=self.root / "var/lib/messagebox-onboarding/state.json",
            obsolete_session_key=self.root
            / "var/lib/messagebox-onboarding/session.key",
            template=self.root / "usr/share/messagebox/onboarding/comitup.conf.template",
            comitup_config=self.root / "etc/comitup.conf",
            comitup_boot_config=self.root / "boot/comitup.conf",
            comitup_firmware_config=self.root / "boot/firmware/comitup.conf",
            run_dir=self.root / "run",
            lock=self.root / "run-lock/messagebox-init-wifi-onboarding.lock",
        )
        self.paths.template.parent.mkdir(parents=True)
        self.paths.comitup_config.parent.mkdir(parents=True)
        self.paths.run_dir.mkdir(parents=True)
        self.paths.lock.parent.mkdir(parents=True)
        self.paths.template.write_text(
            "ap_name: Button Box\nap_password: @HOTSPOT_PASSWORD@\n",
            encoding="ascii",
        )

        self.events = []
        self.commands = []
        self.active_unit = None
        self.fail_check = None
        self.fail_replace = None
        self.fail_after_replace = None
        self.observe_handler_after_replace = None
        self.signal_during_check = None
        self.mutate_config_during_check = None
        self.handlers = {
            signal.SIGHUP: signal.SIG_DFL,
            signal.SIGINT: signal.SIG_DFL,
            signal.SIGTERM: signal.SIG_DFL,
        }

        def run(arguments, **kwargs):
            arguments = list(arguments)
            self.commands.append((arguments, kwargs))
            self.events.append(("run", tuple(arguments)))
            if arguments[:3] == ["systemctl", "is-active", "--quiet"]:
                return SimpleNamespace(
                    returncode=0 if arguments[-1] == self.active_unit else 3
                )
            if arguments == [initialize.COMITUP_BIN, "--check"]:
                if self.mutate_config_during_check is not None:
                    self.paths.comitup_config.write_bytes(
                        self.mutate_config_during_check
                    )
                if self.signal_during_check is not None:
                    self.handlers[self.signal_during_check](
                        self.signal_during_check, None
                    )
                if self.fail_check is not None:
                    raise subprocess.CalledProcessError(
                        1, arguments, stderr=self.fail_check
                    )
                return SimpleNamespace(returncode=0)
            raise AssertionError(f"unexpected command: {arguments}")

        def chown(path, uid, gid):
            self.events.append(("chown", str(path), uid, gid))

        def fchown(descriptor, uid, gid):
            self.events.append(("fchown", uid, gid))

        def replace_file(source, destination):
            destination = Path(destination)
            self.events.append(("replace", str(destination)))
            if self.fail_replace == destination:
                self.fail_replace = None
                raise OSError("injected install failure")
            os.replace(source, destination)
            if self.observe_handler_after_replace == destination:
                self.events.append(
                    ("commit-handler", self.handlers[signal.SIGTERM])
                )
            if self.fail_after_replace == destination:
                self.fail_after_replace = None
                raise OSError("injected post-replace failure")

        def copy2(source, destination):
            self.events.append(("copy2", str(source), str(destination)))
            return shutil.copy2(source, destination)

        def set_signal(signum, handler):
            previous = self.handlers[signum]
            self.handlers[signum] = handler
            return previous

        def fstat(descriptor):
            metadata = os.fstat(descriptor)
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_uid=0,
                st_nlink=metadata.st_nlink,
            )

        self.dependencies = initialize.Dependencies(
            geteuid=lambda: 0,
            gethostname=lambda: "message-box-a7",
            getpwnam=lambda name: SimpleNamespace(pw_uid=501),
            getgrnam=lambda name: SimpleNamespace(gr_gid=502),
            choice=lambda alphabet: next(char for char in alphabet if char.isalpha()),
            run=run,
            access=os.access,
            chown=chown,
            fchown=fchown,
            replace=replace_file,
            copy2=copy2,
            mkdtemp=tempfile.mkdtemp,
            rmtree=shutil.rmtree,
            set_signal=set_signal,
            state_store=StateStore,
            fstat=fstat,
        )

    def tearDown(self):
        self.directory.cleanup()

    def call_main(self, confirmation="y\n", *, dependencies=None, stdin=None):
        output = TTY()
        error = io.StringIO()
        code = initialize.main(
            [],
            paths=self.paths,
            dependencies=dependencies or self.dependencies,
            stdin=stdin or TTY(confirmation),
            stdout=output,
            stderr=error,
        )
        return code, output.getvalue(), error.getvalue()

    def assert_temporary_material_removed(self):
        self.assertEqual(list(self.paths.run_dir.iterdir()), [])

    def test_help_works_without_root_or_tty_and_has_no_side_effects(self):
        dependencies = replace(
            self.dependencies,
            geteuid=lambda: (_ for _ in ()).throw(AssertionError("root checked")),
        )
        output = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            initialize.main(
                ["--help"],
                paths=self.paths,
                dependencies=dependencies,
                stdin=io.StringIO(),
                stdout=output,
            )
        self.assertEqual(raised.exception.code, 0)
        self.assertTrue(
            output.getvalue().startswith(
                "usage: messagebox-init-wifi-onboarding [-h]"
            )
        )
        self.assertEqual(self.commands, [])

    def test_real_lock_contention_is_rejected_before_preflight(self):
        descriptor = os.open(self.paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, _, error = self.call_main()
        finally:
            os.close(descriptor)

        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("already running", error)
        self.assertEqual(self.commands, [])

    def test_unsafe_and_nonregular_lock_paths_are_rejected(self):
        self.paths.lock.symlink_to("missing")
        code, _, error = self.call_main()
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("lock path", error)
        self.paths.lock.unlink()

        self.paths.lock.mkdir()
        code, _, error = self.call_main()
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("lock path", error)
        self.paths.lock.rmdir()

        self.paths.lock.write_bytes(b"")
        unsafe_fstat = replace(
            self.dependencies,
            fstat=lambda descriptor: SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600,
                st_uid=123,
                st_nlink=1,
            ),
        )
        code, _, error = self.call_main(dependencies=unsafe_fstat)
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("lock path", error)
        self.assertEqual(self.commands, [])

    def test_root_and_real_tty_are_required_before_preflight(self):
        not_root = replace(self.dependencies, geteuid=lambda: 1000)
        code, _, error = self.call_main(dependencies=not_root)
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("sudo", error)

        output = TTY()
        error_stream = io.StringIO()
        code = initialize.main(
            [],
            paths=self.paths,
            dependencies=self.dependencies,
                stdin=io.StringIO("y\n"),
            stdout=output,
            stderr=error_stream,
        )
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("interactive terminal", error_stream.getvalue())
        self.assertEqual(self.commands, [])
        self.assert_temporary_material_removed()

    def test_existing_and_symlinked_markers_are_rejected(self):
        cases = (
            (self.paths.enabled, False),
            (self.paths.enabled, True),
            (self.paths.configured, False),
            (self.paths.configured, True),
        )
        for marker, symlinked in cases:
            with self.subTest(marker=marker.name, symlinked=symlinked):
                marker.parent.mkdir(parents=True, exist_ok=True)
                if symlinked:
                    marker.symlink_to("missing")
                else:
                    marker.write_text("set\n", encoding="ascii")
                self.commands.clear()
                code, _, error = self.call_main()
                self.assertEqual(code, initialize.EXIT_FAILED)
                self.assertIn("already", error)
                self.assertEqual(self.commands, [])
                marker.unlink()

    def test_regular_and_dangling_boot_overrides_are_rejected(self):
        for path in (
            self.paths.comitup_boot_config,
            self.paths.comitup_firmware_config,
        ):
            for symlinked in (False, True):
                with self.subTest(path=path, symlinked=symlinked):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if symlinked:
                        path.symlink_to("missing")
                    else:
                        path.write_bytes(b"override\n")
                    self.commands.clear()

                    code, _, error = self.call_main()

                    self.assertEqual(code, initialize.EXIT_FAILED)
                    self.assertIn("boot Comitup", error)
                    self.assertEqual(self.commands, [])
                    path.unlink()

    def test_active_onboarding_service_is_rejected(self):
        self.active_unit = "messagebox-onboarding-home.service"
        code, _, error = self.call_main()
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn(self.active_unit, error)
        queried = [call[0][-1] for call in self.commands]
        self.assertEqual(
            queried,
            list(initialize.ONBOARDING_SERVICES)[:3],
        )
        self.assert_temporary_material_removed()

    def test_all_credential_and_comitup_symlinks_are_rejected(self):
        paths = (
            self.paths.config,
            self.paths.state,
            self.paths.obsolete_session_key,
            self.paths.comitup_config,
        )
        for path in paths:
            with self.subTest(path=path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to("missing")
                code, _, error = self.call_main()
                self.assertEqual(code, initialize.EXIT_FAILED)
                self.assertIn("symlink", error.lower())
                self.assertTrue(path.is_symlink())
                path.unlink()

    def test_all_preflight_gates_run_before_residue_is_removed(self):
        self.paths.config_dir.mkdir(parents=True)
        self.paths.config.write_bytes(b"residue")

        def missing_account(name):
            raise KeyError(name)

        dependencies = replace(self.dependencies, getpwnam=missing_account)
        code, _, error = self.call_main(dependencies=dependencies)
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("account", error)
        self.assertEqual(self.paths.config.read_bytes(), b"residue")

    def test_hostname_is_lowercased_and_strictly_validated(self):
        for hostname in (
            "messagebox-a7",
            "message-box-",
            "message-box-under_score",
            "message-box-" + "a" * 33,
            "buttonbox-a7",
            "button-box-",
            "button-box-under_score",
            "button-box-" + "a" * 33,
        ):
            with self.subTest(hostname=hostname):
                dependencies = replace(
                    self.dependencies, gethostname=lambda value=hostname: value
                )
                code, _, error = self.call_main(dependencies=dependencies)
                self.assertEqual(code, initialize.EXIT_FAILED)
                self.assertIn("hostname", error)
                self.assertFalse(self.paths.comitup_config.exists())
                self.assert_temporary_material_removed()

        dependencies = replace(
            self.dependencies, gethostname=lambda: "BUTTON-BOX-A7"
        )
        code, output, error = self.call_main(dependencies=dependencies)
        self.assertEqual((code, error), (initialize.EXIT_OK, ""))
        self.assertIn("Device ID:        a7", output)
        self.assertIn("Hotspot:          button-box-a7", output)

    def test_confirmation_declined_installs_nothing(self):
        self.paths.comitup_config.write_bytes(b"old config\n")
        code, output, error = self.call_main("n\n")
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("aaaa-aaaa", output)
        self.assertIn("declined", error)
        self.assertEqual(self.paths.comitup_config.read_bytes(), b"old config\n")
        self.assertFalse(self.paths.config.exists())
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.configured.exists())
        self.assert_temporary_material_removed()

    def test_success_installs_compact_metadata_state_and_exact_permissions(self):
        observed_card = {}

        def inspect_card():
            temporary = next(self.paths.run_dir.iterdir())
            card = temporary / "card"
            observed_card["mode"] = stat.S_IMODE(card.stat().st_mode)
            observed_card["content"] = card.read_text(encoding="ascii")

        stdin = InspectingTTY("yes\n", inspect_card)
        code, output, error = self.call_main(stdin=stdin)
        self.assertEqual((code, error), (initialize.EXIT_OK, ""))
        self.assertIn("configured but not started", output)
        self.assertEqual(observed_card["mode"], 0o600)
        self.assertEqual(
            observed_card["content"],
            "a7\nmessage-box-a7\naaaa-aaaa\n",
        )

        expected_metadata = {
            "canonical_host": "message-box-a7.local",
            "device_id": "a7",
            "version": 1,
        }
        self.assertEqual(json.loads(self.paths.config.read_text()), expected_metadata)
        self.assertEqual(
            self.paths.config.read_bytes(),
            b'{"canonical_host":"message-box-a7.local","device_id":"a7","version":1}\n',
        )
        self.assertNotIn(b"aaaa-aaaa", self.paths.config.read_bytes())
        self.assertEqual(StateStore(self.paths.state).load()["phase"], "WIFI_SELECT")
        self.assertEqual(
            self.paths.comitup_config.read_text(encoding="ascii").count(
                "aaaa-aaaa"
            ),
            1,
        )
        self.assertEqual(self.paths.configured.read_bytes(), b"configured\n")
        self.assertFalse(self.paths.enabled.exists())

        expected_modes = {
            self.paths.comitup_config: 0o600,
            self.paths.config_dir: 0o750,
            self.paths.state_dir: 0o700,
            self.paths.config: 0o640,
            self.paths.state: 0o600,
            self.paths.configured: 0o640,
        }
        for path, mode in expected_modes.items():
            with self.subTest(path=path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)

        self.assertIn(
            ("chown", str(self.paths.config_dir), 0, 502), self.events
        )
        self.assertIn(
            ("chown", str(self.paths.state_dir), 501, 502), self.events
        )
        fchowns = [event[1:] for event in self.events if event[0] == "fchown"]
        self.assertEqual(
            fchowns,
            [(0, 0), (0, 502), (501, 502), (0, 502)],
        )

        destinations = [
            event[1] for event in self.events if event[0] == "replace"
        ]
        self.assertEqual(
            destinations,
            [
                str(self.paths.comitup_config),
                str(self.paths.config),
                str(self.paths.state),
                str(self.paths.configured),
            ],
        )
        check_event = ("run", (initialize.COMITUP_BIN, "--check"))
        self.assertLess(
            self.events.index(("replace", str(self.paths.comitup_config))),
            self.events.index(check_event),
        )
        self.assertLess(
            self.events.index(check_event),
            self.events.index(("replace", str(self.paths.config))),
        )
        self.assertEqual(destinations[-1], str(self.paths.configured))
        self.assertFalse(
            any("start" in command for command, _ in self.commands)
        )
        check_kwargs = next(
            kwargs
            for command, kwargs in self.commands
            if command == [initialize.COMITUP_BIN, "--check"]
        )
        self.assertEqual(check_kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(check_kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(check_kwargs["stderr"], subprocess.PIPE)
        self.assertIs(check_kwargs["check"], True)
        self.assert_temporary_material_removed()

        lock_descriptor = os.open(self.paths.lock, os.O_RDWR)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock_descriptor)

    def test_comitup_check_failure_restores_prior_config_and_metadata(self):
        old = b"old: configuration\n"
        self.paths.comitup_config.write_bytes(old)
        os.chmod(self.paths.comitup_config, 0o640)
        os.utime(self.paths.comitup_config, (1_600_000_000, 1_600_000_001))
        before = self.paths.comitup_config.stat()
        self.fail_check = "secret command diagnostic"

        code, _, error = self.call_main()
        after = self.paths.comitup_config.stat()
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertNotIn("secret", error)
        self.assertEqual(self.paths.comitup_config.read_bytes(), old)
        self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(self.paths.configured.exists())
        self.assert_temporary_material_removed()

    def test_comitup_check_failure_removes_new_config(self):
        self.fail_check = "check failed"
        code, _, _ = self.call_main()
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertFalse(self.paths.comitup_config.exists())
        self.assertFalse(self.paths.configured.exists())
        self.assert_temporary_material_removed()

    def test_comitup_check_mutation_is_detected_and_rolled_back(self):
        previous = b"previous configuration\n"
        self.paths.comitup_config.write_bytes(previous)
        self.mutate_config_during_check = b"changed by comitup\n"

        code, _, error = self.call_main()

        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("changed /etc/comitup.conf", error)
        self.assertEqual(self.paths.comitup_config.read_bytes(), previous)
        self.assertFalse(self.paths.config.exists())
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.configured.exists())
        self.assert_temporary_material_removed()

    def test_rollback_failure_uses_safe_non_guaranteeing_error(self):
        self.paths.comitup_config.write_bytes(b"previous\n")
        self.fail_check = "check output containing a secret"

        def fail_restore(source, destination):
            if Path(source).name == "comitup.conf.previous":
                raise OSError("rollback detail containing a secret")
            return shutil.copy2(source, destination)

        dependencies = replace(self.dependencies, copy2=fail_restore)
        code, _, error = self.call_main(dependencies=dependencies)

        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("Verify /etc/comitup.conf before retrying", error)
        self.assertNotIn("no credentials", error)
        self.assertNotIn("secret", error)
        self.assertFalse(self.paths.configured.exists())
        self.assert_temporary_material_removed()

    def test_install_failure_after_check_rolls_back_comitup(self):
        self.paths.comitup_config.write_bytes(b"previous\n")
        self.fail_replace = self.paths.config
        code, _, error = self.call_main()
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("failed", error)
        self.assertEqual(self.paths.comitup_config.read_bytes(), b"previous\n")
        self.assertFalse(self.paths.configured.exists())
        self.assert_temporary_material_removed()

    def test_comitup_post_replace_failure_restores_prior_config(self):
        self.paths.comitup_config.write_bytes(b"previous\n")
        self.fail_after_replace = self.paths.comitup_config
        code, _, error = self.call_main()
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("failed", error)
        self.assertEqual(self.paths.comitup_config.read_bytes(), b"previous\n")
        self.assertFalse(self.paths.configured.exists())
        self.assert_temporary_material_removed()

    def test_configured_marker_install_failure_rolls_back_comitup(self):
        self.paths.comitup_config.write_bytes(b"previous\n")
        self.fail_replace = self.paths.configured
        code, _, error = self.call_main()
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("failed", error)
        self.assertEqual(self.paths.comitup_config.read_bytes(), b"previous\n")
        self.assertFalse(self.paths.configured.exists())
        self.assert_temporary_material_removed()

    def test_missing_or_unresolved_template_placeholder_is_rejected(self):
        templates = (
            "ap_password: missing\n",
            "ap_password: @HOTSPOT_PASSWORD@\nother: @UNKNOWN@\n",
            "@HOTSPOT_PASSWORD@ @HOTSPOT_PASSWORD@\n",
        )
        for template in templates:
            with self.subTest(template=template):
                self.paths.template.write_text(template, encoding="ascii")
                code, _, error = self.call_main()
                self.assertEqual(code, initialize.EXIT_FAILED)
                self.assertIn("placeholder", error)
                self.assertFalse(self.paths.comitup_config.exists())
                self.assert_temporary_material_removed()

    def test_sigterm_during_check_uses_the_same_rollback_path(self):
        self.paths.comitup_config.write_bytes(b"previous\n")
        self.signal_during_check = signal.SIGTERM
        code, _, error = self.call_main()
        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("interrupted", error)
        self.assertEqual(self.paths.comitup_config.read_bytes(), b"previous\n")
        self.assertIs(self.handlers[signal.SIGINT], signal.SIG_DFL)
        self.assertIs(self.handlers[signal.SIGTERM], signal.SIG_DFL)
        self.assertFalse(self.paths.configured.exists())
        self.assert_temporary_material_removed()

    def test_sighup_during_check_uses_the_same_rollback_path(self):
        self.paths.comitup_config.write_bytes(b"previous\n")
        self.signal_during_check = signal.SIGHUP

        code, _, error = self.call_main()

        self.assertEqual(code, initialize.EXIT_FAILED)
        self.assertIn("interrupted", error)
        self.assertEqual(self.paths.comitup_config.read_bytes(), b"previous\n")
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            self.assertIs(self.handlers[signum], signal.SIG_DFL)
        self.assertFalse(self.paths.configured.exists())
        self.assert_temporary_material_removed()

    def test_commit_marker_transition_ignores_interrupts(self):
        self.observe_handler_after_replace = self.paths.configured

        code, _, error = self.call_main()

        self.assertEqual((code, error), (initialize.EXIT_OK, ""))
        self.assertIn(("commit-handler", signal.SIG_IGN), self.events)
        self.assertTrue(self.paths.configured.exists())
        self.assertTrue(self.paths.comitup_config.exists())
        for signum in initialize.INTERRUPT_SIGNALS:
            self.assertIs(self.handlers[signum], signal.SIG_DFL)


if __name__ == "__main__":
    unittest.main()
