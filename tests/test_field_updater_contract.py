"""Executable limits of the field worker, independent of current product code.

All source citations below are relative to fixtures/field-updater/shared/.
The provenance tests bind those line numbers to all four field commits.
"""

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from field_updater import CONTROLLER, FIXTURES, PROVENANCE, FieldBox, digest, load_frozen, mkdir, wav, write


class FrozenFieldSources(unittest.TestCase):
    def test_all_four_field_sets_have_identical_bytes_and_recorded_hashes(self):
        # The plan's 44-file identity claim, including the whole systemd tree.
        self.assertEqual(list(PROVENANCE["releases"]), ["v0.3.9", "v0.3.10", "v0.3.11", "v0.3.12"])
        baseline = None
        for version, release in PROVENANCE["releases"].items():
            with self.subTest(version=version):
                paths = PROVENANCE["snapshots"][release["snapshot"]]
                self.assertEqual(len(paths), 44)
                self.assertEqual(set(paths), set(release["files"]))
                actual = {}
                for relative, evidence in release["files"].items():
                    content = (FIXTURES / release["snapshot"] / relative).read_bytes()
                    self.assertEqual(hashlib.sha256(content).hexdigest(), evidence["sha256"], relative)
                    git_object = b"blob " + str(len(content)).encode() + b"\0" + content
                    self.assertEqual(hashlib.sha1(git_object).hexdigest(), evidence["git_blob"], relative)
                    actual[relative] = (content, evidence["mode"])
                if baseline is None:
                    baseline = actual
                self.assertEqual(actual, baseline)

    def test_provenance_matches_available_git_commit_objects(self):
        # Git history is not required on a source-only or shallow CI checkout.
        if shutil.which("git") is None:
            self.skipTest("Git unavailable; content hashes still checked")
        repo = Path(__file__).resolve().parents[1]
        for release in PROVENANCE["releases"].values():
            exists = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", release["commit"] + "^{commit}"],
                                    capture_output=True, check=False)
            if exists.returncode:
                self.skipTest("field commit objects unavailable; content hashes still checked")
        for version, release in PROVENANCE["releases"].items():
            for relative, evidence in release["files"].items():
                with self.subTest(version=version, path=relative):
                    blob = subprocess.check_output(["git", "-C", str(repo), "show", release["commit"] + ":" + relative])
                    self.assertEqual(blob, (FIXTURES / release["snapshot"] / relative).read_bytes())
                    tree = subprocess.check_output(["git", "-C", str(repo), "ls-tree", release["commit"], "--", relative], text=True)
                    self.assertEqual(tree.split()[:3], [evidence["mode"], "blob", evidence["git_blob"]])

    def test_absent_git_skips_only_history_comparison(self):
        # File hashes and Git blob hashes do not need a Git executable.
        with mock.patch("shutil.which", return_value=None):
            self.test_all_four_field_sets_have_identical_bytes_and_recorded_hashes()
            with self.assertRaises(unittest.SkipTest):
                self.test_provenance_matches_available_git_commit_objects()


class BootstrapContract(unittest.TestCase):
    def test_recorded_bootstrap_paths_bytes_modes_links_and_services_are_loaded(self):
        # update-bootstrap.sh:33-89; bounded_update.py:1556-1616.
        with tempfile.TemporaryDirectory() as directory:
            box = FieldBox(directory)
            for entry in box.bootstrap["entries"]:
                path = box.root / entry["path"].lstrip("/")
                with self.subTest(path=entry["path"]):
                    self.assertEqual(path.lstat().st_uid, os.geteuid())
                    if entry["kind"] == "symlink":
                        self.assertEqual(os.readlink(path), entry["target"])
                    else:
                        self.assertEqual(path.stat().st_mode & 0o777, int(entry["mode"], 8))
                        if entry["kind"] == "directory":
                            self.assertTrue(path.is_dir())
                        else:
                            expected = ((box.snapshot / entry["source"]).read_bytes()
                                        if "source" in entry else entry["text"].encode())
                            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(box.states, box.bootstrap["services"])
            self.assertEqual(box.worker.controlled_release(box.root), box.old)
            self.assertEqual(box.bootstrap_journal["kind"], "bootstrap")
            self.assertEqual(box.bootstrap_journal["phase"], "committed")
            self.assertEqual(box.worker.read_state(box.root, "policy.json")["mode"], "auto")

    def test_bootstrap_generated_dropins_units_and_enable_links_match_frozen_source(self):
        # update-bootstrap.sh:34-61,65-89; trust_update.py:132-158,202-235.
        with tempfile.TemporaryDirectory() as directory:
            box = FieldBox(directory)
            script = (box.snapshot / "scripts/install/update-bootstrap.sh").read_text()
            units = re.search(r"for unit in (messagebox.target .*?); do", script).group(1).split()
            content = re.search(r"<<'EOF'\n(.*?)EOF", script, re.S).group(1).encode()
            dropins = {e["path"] for e in box.bootstrap["entries"] if e["path"].endswith("/software-update.conf")}
            self.assertEqual(dropins, {"/etc/systemd/system/" + unit + ".d/software-update.conf" for unit in units})
            self.assertEqual(set(units), set(box.worker.UNITS))
            for path in dropins:
                self.assertEqual(box.installed(path).read_bytes(), content)
            enabled = re.search(r"systemctl enable --now (messagebox-update-recovery.service messagebox-update-controller.service .*)", script).group(1).split()
            for unit in enabled:
                source = (box.snapshot / "systemd" / unit).read_text()
                wanted = re.search(r"^WantedBy=(.+)$", source, re.M).group(1)
                link = box.installed("/etc/systemd/system/" + wanted + ".wants/" + unit)
                self.assertEqual(os.readlink(link), "/etc/systemd/system/" + unit)
                self.assertEqual(box.states[unit], {"active": "active", "enabled": "enabled"})
            # The recorded defaults match the bootstrap's explicit install -d.
            for name in ('snapshots', 'staging', 'downloads'):
                self.assertEqual(box.installed('/var/lib/button-box-update/' + name).stat().st_mode & 0o777, 0o700)
            self.assertTrue(box.bootstrap["inferred"])

    def test_recorded_layout_matches_real_frozen_bootstrap_conversion(self):
        # bounded_update.py:1556-1616. Execute the conversion, not the host shell.
        with tempfile.TemporaryDirectory() as directory:
            box = FieldBox(directory)
            pointer = box.root / "opt/messagebox"
            pointer.unlink()
            legacy = box.installed(box.old)
            (legacy / "venv-nfc").unlink()
            stable = box.installed(box.worker.STABLE_NFC)
            stable.rename(legacy / "venv-nfc")
            legacy.rename(pointer)
            box.installed('/var/lib/button-box-update/journal.json').unlink()
            box.installed('/var/lib/button-box-update/known-good.json').unlink()
            before_states = {u: dict(state) for u, state in box.states.items()}
            self.assertEqual(box.worker.bootstrap_layout(root=box.root, run=box.run), box.old)
            observed = box.worker.read_state(box.root, 'journal.json')
            observed['backup'] = box.bootstrap_journal['backup']  # Synthetic UUID only.
            self.assertEqual(observed, box.bootstrap_journal)
            self.assertEqual(box.states, before_states)
            self.assertEqual(os.readlink(pointer), box.old)
            self.assertEqual(os.readlink(box.installed(box.old) / 'venv-nfc'), box.worker.STABLE_NFC)
            self.assertTrue((stable / 'pyvenv.cfg').is_file())
            for name in ('snapshots', 'staging'):
                self.assertEqual(list(box.installed('/var/lib/button-box-update/' + name).iterdir()), [])

    def test_group_writable_host_umask_does_not_change_test_directory_modes(self):
        # bounded_update.py:1303-1319 rejects a writable controlled release.
        previous = os.umask(0o002)
        try:
            with tempfile.TemporaryDirectory() as directory:
                box = FieldBox(directory)
                for tree in (box.root, box.source):
                    for path in (tree, *tree.rglob('*')):
                        if path.is_dir() and not path.is_symlink():
                            self.assertEqual(path.stat().st_mode & 0o022, 0, str(path))
                with box.controller() as controller:
                    box.bundle(controller)
                    controller.worker()
                    self.assertEqual(controller.read('journal.json')['phase'], 'committed')
                self.assertEqual(os.umask(0o002), 0o002)
                failed = FieldBox(Path(directory) / 'failure')
                with self.assertRaisesRegex(ValueError, 'bad_signature'):
                    with failed.controller(signature_ok=False) as controller:
                        failed.bundle(controller)
                        controller.worker()
                self.assertEqual(os.umask(0o002), 0o002)
        finally:
            os.umask(previous)


class BridgeContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.box = FieldBox(temporary.name)
        self.worker = self.box.worker

    def reject(self, reason):
        before = self.box.tree()
        with self.assertRaisesRegex(self.worker.UpdateError, reason):
            self.box.install()
        self.assertEqual(self.box.tree(), before)
        self.assertEqual(self.worker.read_state(self.box.root, "journal.json"), self.box.bootstrap_journal)
        self.assertFalse(self.box.commands)

    def test_disk_capacity_exact_threshold_accepts_and_one_byte_less_defers(self):
        # bounded_update.py:1820-1822; update_controller.py:591-600,611-615.
        size = sum(entry['source_path'].stat().st_size for entry in
                   self.worker.load_candidate(self.box.source, self.box.manifest, self.box.root)[1])
        required = size * 2 + 64 * 1024 * 1024
        self.box.free_bytes = required - 1
        with self.box.controller() as controller:
            bundle = self.box.bundle(controller)
            before = self.box.tree()
            controller.worker()
            self.assertEqual(controller.read('result.json')['reason'], 'low_disk')
            self.assertEqual(controller.read('result.json')['state'], 'waiting_for_window')
            self.assertEqual(self.box.tree(), before)
            self.assertEqual(controller.read('journal.json'), self.box.bootstrap_journal)
            self.assertTrue(bundle.is_dir())
            self.assertFalse(any(c[0] == 'systemctl' for c in self.box.commands))
            self.box.free_bytes = required
            self.box.now += 60
            controller.worker()
            self.assertEqual(controller.read('result.json')['state'], 'installed')
            self.assertFalse(bundle.exists())
        self.assertEqual(self.box.disk_queries, [self.box.installed(self.worker.RELEASES)] * 2)

    def test_supervisor_dropin_installs_with_each_frozen_field_worker(self):
        # bounded_update.py:257-258,1809-1819,1882-1887;
        # release-manifest.py:30-31. Fixed basename is messagebox.conf here.
        content = b"[Service]\nExecStartPost=/usr/bin/true\n"
        relative = "systemd/messagebox-update-supervisor.service.d/messagebox.conf"
        for version in PROVENANCE["releases"]:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                box = FieldBox(directory, version)
                box.add(relative, content)
                fixed = box.fixed_bytes()
                box.install()
                target = box.installed("/etc/systemd/system/messagebox-update-supervisor.service.d/messagebox.conf")
                self.assertEqual(target.read_bytes(), content)
                self.assertEqual(target.stat().st_mode & 0o777, 0o644)
                self.assertIn(["systemctl", "daemon-reload"], box.commands)
                self.assertEqual(box.worker.read_state(box.root, "journal.json")["phase"], "committed")
                self.assertEqual(box.fixed_bytes(), fixed)

    def test_candidate_mapper_is_imported_and_called_before_first_install(self):
        # bounded_update.py:547-552,1804,1828-1833,1882-1887.
        # Instrument the CANDIDATE mapper. Do not replace the worker's loader.
        marker = self.box.source / "mapper-events"
        mapper = self.box.source / "scripts/dev/release-manifest.py"
        mapper.write_text(mapper.read_text() +
                          f"\n_probe = Path({str(marker)!r})\n_probe.write_text('import\\n')\n"
                          "_original = installed_paths\n"
                          "def installed_paths(root):\n"
                          "    _probe.write_text(_probe.read_text() + 'call\\n')\n"
                          "    return _original(root)\n")
        install = self.worker._atomic_install
        calls = []

        def observe(source, destination, mode):
            self.assertEqual(marker.read_text(), "import\ncall\n")
            calls.append(destination)
            return install(source, destination, mode)

        with mock.patch.object(self.worker, "_atomic_install", side_effect=observe):
            self.box.install()
        self.assertTrue(calls)

    def test_mapper_refusal_leaves_installed_files_and_pointer_unchanged(self):
        # bounded_update.py:547-552,1804: exception precedes staging and mutation.
        mapper = self.box.source / "scripts/dev/release-manifest.py"
        mapper.write_text(mapper.read_text() +
                          "\ndef installed_paths(root):\n    raise RuntimeError('mapper_refused')\n")
        before = self.box.tree()
        with self.assertRaisesRegex(RuntimeError, "mapper_refused"):
            self.box.install()
        self.assertEqual(self.box.tree(), before)
        self.assertEqual(self.worker.read_state(self.box.root, "journal.json"), self.box.bootstrap_journal)
        self.assertFalse(self.box.commands)

    def test_controller_modules_are_staged_in_release_not_fixed_directory(self):
        # bounded_update.py:1834-1844: all six are syntax-checked and copied.
        before = self.box.fixed_bytes()
        for name in CONTROLLER:
            path = self.box.source / f"scripts/install/{name}.py"
            path.write_bytes(path.read_bytes() + b"\n# synthetic controller candidate\n")
        self.box.install()
        pointer = self.worker.controlled_release(self.box.root)
        for name in CONTROLLER:
            candidate = self.box.installed(pointer) / f"updater/{name}.py"
            self.assertEqual(candidate.read_bytes(), (self.box.source / f"scripts/install/{name}.py").read_bytes())
            self.assertEqual(candidate.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.box.fixed_bytes(), before)

    def test_invalid_controller_syntax_rejects_and_removes_candidate(self):
        # bounded_update.py:1837-1840,1861-1863.
        write(self.box.source / "scripts/install/update_controller.py", b"def invalid(:\n")
        self.reject("invalid_candidate_controller")

    def test_application_extensions_and_new_units_install(self):
        # bounded_update.py:248-263,1828-1833,1882-1887.
        sources = [f"messagebox/bridge/nested/launcher{suffix}" for suffix in (".py", ".sh", ".html", ".js", ".css")]
        sources += ["systemd/messagebox-bridge" + suffix for suffix in (".service", ".path", ".target", ".timer")]
        sources += ["systemd/example.service.d/messagebox.conf"]
        for source in sources:
            self.box.add(source)
        self.box.install()
        for source in sources:
            target = self.worker._expected_target(source)
            self.assertEqual(self.box.installed(target).read_bytes(), (self.box.source / source).read_bytes())

    def test_disallowed_paths_and_redirected_targets_reject_before_mutation(self):
        # bounded_update.py:242-263,486-490.
        for source, target in (
            ("messagebox/bridge.json", "/opt/messagebox/messagebox/bridge.json"),
            ("scripts/install/bridge.py", "/usr/lib/messagebox-update/bridge.py"),
            ("systemd/unrelated.service", "/etc/systemd/system/unrelated.service"),
            ("systemd/messagebox-bridge.socket", "/etc/systemd/system/messagebox-bridge.socket"),
            ("systemd/messagebox-update-supervisor.service.d/other.conf", "/etc/systemd/system/messagebox-update-supervisor.service.d/other.conf"),
            ("messagebox/bridge.py", "/usr/lib/messagebox-update/bounded_update.py"),
            ("messagebox/../bridge.py", "/opt/messagebox/bridge.py"),
            ("/messagebox/bridge.py", "/opt/messagebox/messagebox/bridge.py"),
        ):
            with self.subTest(source=source):
                entry = {"source": source, "installed": target, "sha256": "0" * 64}
                self.box.data["files"].append(entry)
                self.box.save_manifest()
                self.reject("unsafe source path|outside the install allowlist")
                self.box.data["files"].pop()

    def test_changed_or_absent_fixed_units_and_dependencies_reject(self):
        # bounded_update.py:1258-1263,1815-1819. Unchanged files pass other tests.
        for unit in sorted(self.worker.FIXED_UNITS):
            for operation in ("change", "remove"):
                with self.subTest(unit=unit, operation=operation):
                    destination = self.box.installed("/etc/systemd/system/" + unit)
                    content = destination.read_bytes()
                    if operation == "change":
                        destination.write_bytes(content + b"# changed\n")
                    else:
                        destination.unlink()
                    self.reject("maintenance_release_required")
                    write(destination, content)
        path = self.box.source / "config/requirements-nfc.txt"
        path.write_bytes(path.read_bytes() + b"# changed dependency\n")
        self.box.refresh()
        self.reject("maintenance_release_required")

    def test_real_recovery_removes_new_dropin_after_failed_host_health(self):
        # bounded_update.py:1400-1415,1433-1496,1924-1927; backup includes prior absence.
        self.box.add("systemd/messagebox-update-supervisor.service.d/messagebox.conf", b"[Service]\n")
        before = self.box.tree()

        def fail(root, journal, run):
            self.assertEqual(journal["phase"], "probation")
            self.assertTrue(self.box.installed("/etc/systemd/system/messagebox-update-supervisor.service.d/messagebox.conf").is_file())
            raise self.worker.UpdateError("health_timeout")

        with self.assertRaisesRegex(self.worker.UpdateError, "health_timeout"):
            self.box.install(health=fail)
        self.assertEqual(self.worker.controlled_release(self.box.root), self.box.old)
        self.assertFalse(self.box.installed("/etc/systemd/system/messagebox-update-supervisor.service.d/messagebox.conf").exists())
        self.assertEqual(self.worker.read_state(self.box.root, "journal.json")["phase"], "rolled_back")
        # Candidate is retained for diagnosis; compare the restored active files.
        for path, value in before.items():
            self.assertEqual(self.box.tree()[path], value)


class FrozenWorkerPipeline(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.box = FieldBox(temporary.name)

    def test_frozen_worker_verifies_extracts_and_installs_supervisor_dropin(self):
        # update_controller.py:561-590,611-619; release_verify.py:50-151;
        # bounded_update.py:1790-1927. Real worker/transaction control flow;
        # health is an explicit host substitute.
        content = b"[Service]\nExecStartPost=/usr/bin/true\n"
        self.box.add("systemd/messagebox-update-supervisor.service.d/messagebox.conf", content)
        before = self.box.fixed_bytes()
        with self.box.controller() as controller:
            bundle = self.box.bundle(controller)
            controller.worker()
            self.assertEqual(controller.read("result.json")["state"], "installed")
            self.assertFalse(bundle.exists())
            self.assertEqual(list((controller.STATE / "staging").iterdir()), [])
        self.assertEqual(self.box.installed("/etc/systemd/system/messagebox-update-supervisor.service.d/messagebox.conf").read_bytes(), content)
        self.assertEqual(self.box.fixed_bytes(), before)
        self.assertEqual(self.box.worker.read_state(self.box.root, "journal.json")["phase"], "committed")

    def test_mapper_not_idle_retries_without_installed_file_mutation(self):
        # bounded_update.py:547-552,1804; update_controller.py:591-600,611-615.
        mapper = self.box.source / "scripts/dev/release-manifest.py"
        mapper.write_text(mapper.read_text() +
                          "\nfrom bounded_update import UpdateError\n"
                          "def installed_paths(root):\n    raise UpdateError('not_idle')\n")
        with self.box.controller() as controller:
            bundle = self.box.bundle(controller)
            before = self.box.tree()
            controller.worker()
            self.assertEqual(self.box.tree(), before)
            result = controller.read("result.json")
            self.assertEqual((result["state"], result["reason"]), ("waiting_for_window", "not_idle"))
            operation = controller.read(controller.operation_name(result["operation_id"]))
            self.assertEqual(operation["retry_after_mono"], self.box.now + 60)
            self.assertTrue(bundle.is_dir())
            self.assertTrue(controller.read("available.json"))
            self.assertEqual(list((controller.STATE / "staging").iterdir()), [])
            self.assertEqual(controller.read("journal.json"), self.box.bootstrap_journal)
            self.assertFalse(any(command[0] == "systemctl" for command in self.box.commands))
            # A later automatic attempt keeps the same candidate and retry path.
            self.box.now += 60
            controller.worker()
            self.assertEqual(controller.read("result.json")["state"], "waiting_for_window")
            self.assertEqual(self.box.tree(), before)

    def test_bad_signature_and_archive_manifest_mismatch_reject_before_install(self):
        # release_verify.py:63-77; update_controller.py:579-584,607-615.
        for kind in ("signature", "manifest"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                box = FieldBox(directory)
                with box.controller(signature_ok=kind != "signature") as controller:
                    bundle = box.bundle(controller, mismatched_manifest=kind == "manifest")
                    before = box.tree()
                    with self.assertRaisesRegex(ValueError, "bad_signature" if kind == "signature" else "archive_manifest_mismatch"):
                        controller.worker()
                    self.assertEqual(box.tree(), before)
                    self.assertEqual(controller.read("result.json")["reason"], "verification_failed")
                    self.assertFalse(bundle.exists())
                    self.assertFalse(controller.read("available.json"))
                    self.assertEqual(controller.read("journal.json"), box.bootstrap_journal)


class TimeContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.box = FieldBox(temporary.name)
        self.worker = self.box.worker

    def test_frozen_service_limits_remain_900_and_330_seconds(self):
        # systemd/messagebox-update-discovery.service:15;
        # systemd/messagebox-update-install.service:9. No host systemd is run.
        for name, seconds in (('discovery', 900), ('install', 330)):
            raw = self.box.installed('/etc/systemd/system/messagebox-update-' + name + '.service').read_text()
            self.assertEqual(re.findall(r'^TimeoutStartSec=(.+)$', raw, re.M), [str(seconds)])

    def test_download_sets_30_second_socket_timeout_and_cleans_partial_on_timeout(self):
        # update_discovery.py:121-140. Socket timeout, not total transfer time.
        url = 'https://github.com/button-box/button-box/releases/download/v0.3.13/payload'
        path = self.box.source / 'download.bin'
        write(path, b'previous')
        box = self.box
        for stall in (True, False):
            with self.subTest(stall=stall), box.controller():
                discovery = load_frozen(box.version, 'scripts/install/update_discovery.py')
                start = box.now

                def opener(request, *, timeout):
                    self.assertEqual(request.full_url, url)
                    self.assertEqual(timeout, 30)

                    class Response:
                        reads = 0
                        def __enter__(self):
                            return self
                        def __exit__(self, *args):
                            pass
                        def read(self, size):
                            self.reads += 1
                            if self.reads == 1:
                                return b'candidate'
                            box.sleep(timeout if stall else timeout - 1)
                            if stall:
                                raise TimeoutError('controlled socket timeout')
                            return b''
                    return Response()

                client = discovery.Discovery(mkdir(box.source / 'cache', 0o750),
                                             opener=opener, clock=lambda: box.now)
                if stall:
                    with self.assertRaisesRegex(TimeoutError, 'controlled socket timeout'):
                        client.download(url, path, 1024)
                    self.assertEqual(path.read_bytes(), b'previous')
                    self.assertEqual(box.now - start, 30)
                else:
                    client.download(url, path, 1024)
                    self.assertEqual(path.read_bytes(), b'candidate')
                    self.assertEqual(box.now - start, 29)
                self.assertFalse(path.with_suffix('.bin.part').exists())

    def sustain(self, journal, ready):
        samples = []
        def sample(*args):
            samples.append(self.box.now)
            return ready(self.box.now), {}
        self.worker.sustain_health(self.box.root, journal, self.box.run, sample=sample,
                                   monotonic=lambda: self.box.now, sleep=self.box.sleep)
        return samples

    def test_health_requires_60_continuous_seconds_and_resets_on_unready(self):
        # bounded_update.py:1253-1255,1761-1787.
        self.assertEqual(self.worker.HEALTH_STABLE, 60)
        journal = {'deadline_mono': 300}
        samples = self.sustain(journal, lambda now: True)
        self.assertEqual((samples[0], samples[-1]), (0, 60))
        self.box.now = 0
        samples = self.sustain(journal, lambda now: now != 58)
        self.assertEqual(samples[-1], 120)  # Stable again from 60 through 120.

    def test_install_records_300_second_deadline_and_rolls_back_at_boundary(self):
        # bounded_update.py:1253,1869-1870,1765-1787,1924-1927.
        self.assertEqual(self.worker.HEALTH_DEADLINE, 300)
        observed = []
        before = self.box.tree()
        def health(root, journal, run):
            observed.append((journal['started_mono'], journal['deadline_mono']))
            # Ready for the last 60 seconds only: reaching the deadline is too late.
            self.sustain(journal, lambda now: now >= journal['deadline_mono'] - 60)
        with self.assertRaisesRegex(self.worker.UpdateError, 'health_timeout'):
            self.box.install(health=health)
        self.assertEqual(observed[0][1] - observed[0][0], 300)
        self.assertGreaterEqual(self.box.now, observed[0][1])
        self.assertEqual(self.box.tree(), before)
        self.assertEqual(self.worker.read_state(self.box.root, 'journal.json')['phase'], 'rolled_back')

    def test_health_can_finish_just_before_300_second_deadline(self):
        # bounded_update.py:1779-1787: final success must precede the deadline.
        samples = self.sustain({'deadline_mono': 300}, lambda now: now >= 238)
        self.assertEqual(samples[-1], 298)


class SoundContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.box = FieldBox(temporary.name)
        self.worker = self.box.worker

    def load(self):
        return self.worker.load_candidate(self.box.source, self.box.manifest, self.box.root)

    def test_sound_paths_accept_existing_packs_and_reject_new_pack(self):
        # bounded_update.py:185-188,242-246,486-490,497-504.
        for pack in ("cues", "cloud", "ringtones", "voice", "voices/pirate", "voices/alien",
                     "voices/dj", "voices/robot", "voices/french", "voices/charlie", "voices/tata"):
            wav(self.box.source / f"sounds/{pack}/bridge.wav")
        self.box.add("sounds/cues/cues.json", b"{}")
        self.box.refresh()
        self.load()
        for source in ("sounds/voices/buttonbox/bridge.wav", "sounds/new/bridge.wav",
                       "sounds/cues/nested/bridge.wav", "sounds/cues/Upper.wav",
                       "sounds/cues/extra.json", "sounds/cues/extra.py"):
            with self.subTest(source=source):
                before = self.box.tree()
                self.box.data["files"].append({"source": source, "installed": "/opt/messagebox/" + source, "sha256": "0" * 64})
                self.box.save_manifest()
                with self.assertRaisesRegex(self.worker.UpdateError, "outside the install allowlist"):
                    self.load()
                self.assertEqual(self.box.tree(), before)
                self.box.data["files"].pop()

    def test_two_mib_per_file_boundary_and_invalid_wav(self):
        # bounded_update.py:189,372-388,504.
        self.assertEqual(self.worker.MAX_SOUND_FILE, 2 * 1024 * 1024)
        path = wav(self.box.source / "sounds/cues/bridge.wav", self.worker.MAX_SOUND_FILE)
        self.box.refresh()
        self.load()
        path.write_bytes(path.read_bytes() + b"x")
        self.box.refresh()
        with self.assertRaisesRegex(self.worker.UpdateError, "sound file exceeds size limit"):
            self.load()
        path.write_bytes(b"invalid WAV")
        self.box.refresh()
        with self.assertRaisesRegex(self.worker.UpdateError, "sound WAV is invalid"):
            self.load()

    def test_128_mib_total_boundary_uses_real_file_sizes(self):
        # bounded_update.py:190,497-503. Do not reduce the frozen limit.
        self.assertEqual(self.worker.MAX_SOUND_TOTAL, 128 * 1024 * 1024)
        current = sum((self.box.source / entry["source"]).stat().st_size
                      for entry in self.box.data["files"] if entry["source"].startswith("sounds/"))
        remaining = self.worker.MAX_SOUND_TOTAL - current
        count = 0
        while remaining:
            size = min(remaining, self.worker.MAX_SOUND_FILE)
            # JSON files need no WAV header; this also handles an odd remainder.
            path = self.box.source / f"sounds/voices/pirate/total-{count}.wav"
            if size < 46 or size % 2:
                path = self.box.source / "sounds/voices/pirate/cues.json"
                write(path, b" " * size)
            else:
                wav(path, size)
            remaining -= size
            count += 1
        self.box.refresh()
        self.load()
        wav(self.box.source / "sounds/cues/over-total.wav")
        self.box.refresh()
        with self.assertRaisesRegex(self.worker.UpdateError, "size or file count limit"):
            self.load()

    def test_2000_sound_file_boundary_uses_real_files(self):
        # bounded_update.py:191,497-503. Do not reduce the frozen limit.
        self.assertEqual(self.worker.MAX_SOUND_FILES, 2000)
        count = sum(entry["source"].startswith("sounds/") for entry in self.box.data["files"])
        for index in range(self.worker.MAX_SOUND_FILES - count):
            wav(self.box.source / f"sounds/cues/count-{index}.wav")
        self.box.refresh()
        self.load()
        wav(self.box.source / "sounds/cues/over-count.wav")
        self.box.refresh()
        with self.assertRaisesRegex(self.worker.UpdateError, "size or file count limit"):
            self.load()


class ArchiveContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.verifier = load_frozen("v0.3.12", "scripts/install/release_verify.py")
        self.version = "v0.3.13"
        self.archive = self.root / f"button-box-{self.version}.tar.gz"
        self.destination = self.root / "extracted"

    def archive_members(self, members):
        with tarfile.open(self.archive, "w:gz", compresslevel=1) as archive:
            for member, content in members:
                archive.addfile(member, io.BytesIO(content) if content is not None else None)

    def extract(self):
        return self.verifier.extract_bundle(self.root, self.destination, self.version)

    def test_normal_archive_and_10000_entry_boundary(self):
        # release_verify.py:15,118-151: directories also count as entries.
        self.assertEqual(self.verifier.MAX_FILES, 10000)
        members = []
        for index in range(10000):
            member = tarfile.TarInfo(f"directory-{index}")
            member.type = tarfile.DIRTYPE
            members.append((member, None))
        self.archive_members(members)
        self.extract()
        self.assertEqual(len(list(self.destination.iterdir())), 10000)
        import shutil
        shutil.rmtree(self.destination)
        members.append((tarfile.TarInfo("over-limit"), b""))
        self.archive_members(members)
        with self.assertRaisesRegex(self.verifier.ReleaseError, "archive_limit"):
            self.extract()
        self.assertFalse(self.destination.exists())

    def test_256_mib_expanded_boundary_with_real_compressed_archive(self):
        # release_verify.py:14,138-140. All headers are checked before writes.
        self.assertEqual(self.verifier.MAX_EXPANDED, 256 * 1024 * 1024)

        class Zeros:
            def read(self, size):
                return b"\0" * size

        member = tarfile.TarInfo("payload.bin")
        member.size = self.verifier.MAX_EXPANDED
        with tarfile.open(self.archive, "w:gz", compresslevel=1) as archive:
            archive.addfile(member, Zeros())
        self.extract()
        self.assertEqual((self.destination / member.name).stat().st_size, member.size)
        import shutil
        shutil.rmtree(self.destination)
        with tarfile.open(self.archive, "w:gz", compresslevel=1) as archive:
            archive.addfile(member, Zeros())
            extra = tarfile.TarInfo("one-byte-too-many")
            extra.size = 1
            archive.addfile(extra, io.BytesIO(b"x"))
        with self.assertRaisesRegex(self.verifier.ReleaseError, "archive_limit"):
            self.extract()
        self.assertFalse(self.destination.exists())

    def test_links_special_files_duplicates_and_traversal_reject_before_extract(self):
        # release_verify.py:130-140.
        for kind in ("symlink", "hardlink", "fifo", "character", "block", "duplicate",
                     "traversal", "absolute", "noncanonical"):
            with self.subTest(kind=kind):
                member = tarfile.TarInfo({"traversal": "../escape", "absolute": "/escape", "noncanonical": "a/./b"}.get(kind, "entry"))
                member.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                               "fifo": tarfile.FIFOTYPE, "character": tarfile.CHRTYPE,
                               "block": tarfile.BLKTYPE}.get(kind, tarfile.REGTYPE)
                member.linkname = "target" if kind in {"symlink", "hardlink"} else ""
                members = [(member, b"" if member.isfile() else None)]
                if kind == "duplicate":
                    members.append((tarfile.TarInfo("entry"), b""))
                self.archive_members(members)
                with self.assertRaisesRegex(self.verifier.ReleaseError, "unsafe_archive"):
                    self.extract()
                self.assertFalse(self.destination.exists())

    def test_128_mib_compressed_boundary_in_signed_bundle_verifier(self):
        # release_verify.py:13,50-114,85-90. Only minisign is a host substitute.
        self.assertEqual(self.verifier.MAX_ARCHIVE, 128 * 1024 * 1024)
        keys = self.root / "keys"
        key = base64.b64encode(b"Ed" + b"k" * 40).decode()
        write(keys / "synthetic.pub", ("untrusted comment: synthetic test key\n" + key + "\n").encode())
        manifest = {"schema": 1, "product": "button-box", "version": self.version,
                    "tag": self.version, "commit": "a" * 40, "release_sequence": 1,
                    "files": [], "channel": "stable", "min_updater_version": "1.1.0",
                    "supported_os": ["raspios-trixie"], "supported_arch": ["aarch64"],
                    "supported_models": ["Raspberry Pi 4 Model B"]}
        write(self.root / "release-manifest.json", json.dumps(manifest).encode())
        write(self.root / "SHA256SUMS.minisig", b"synthetic signature; not cryptographic acceptance")
        with self.archive.open("wb") as stream:
            stream.truncate(self.verifier.MAX_ARCHIVE)
        for extra in (0, 1):
            with self.subTest(extra=extra):
                with self.archive.open("r+b") as stream:
                    stream.truncate(self.verifier.MAX_ARCHIVE + extra)
                sums = "".join(f"{digest(path)}  {path.name}\n" for path in (self.archive, self.root / "release-manifest.json"))
                write(self.root / "SHA256SUMS", sums.encode())
                options = dict(version=self.version, channel="stable", floor=0,
                               platform={"model": "Raspberry Pi 4 Model B", "os": "raspios-trixie", "architecture": "aarch64"},
                               run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0))
                if extra:
                    with self.assertRaisesRegex(self.verifier.ReleaseError, "hash_mismatch"):
                        self.verifier.verify_bundle(self.root, keys, **options)
                else:
                    self.assertEqual(self.verifier.verify_bundle(self.root, keys, **options), manifest)
