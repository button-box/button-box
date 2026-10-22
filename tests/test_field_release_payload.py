"""Use the release builder's full checkout payload, not the synthetic candidate.

This integration uses real packaging and disposable signing keys. Clean-tag,
repeat-build and production key-policy gates remain build.py's responsibility.
Later bridge PRs must also call FieldBox.run_built_release() on build() output
and assert their bridge files and retained proof after the old worker commits.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from field_updater import CONTROLLER, PROVENANCE, FieldBox, digest, write

ROOT = Path(__file__).resolve().parents[1]


class BuiltFieldPayload(unittest.TestCase):
    def test_actual_checkout_package_installs_with_all_four_frozen_workers(self):
        self.exercise_built_payload()

    def test_real_built_payload_publication_cut_matrix(self):
        def cuts(box):
            from test_controller_slots import SlotTests, Host
            fixture = SlotTests('test_every_publication_and_trial_write_cut_keeps_an_accepted_slot')
            fixture.root, fixture.base, fixture.box = box.root, box.root.parent, box
            fixture.host = Host(box.root)
            fixture.old = fixture.slot_bytes('usr/lib/messagebox-update')
            fixture.test_every_publication_and_trial_write_cut_keeps_an_accepted_slot()
        self.exercise_built_payload(after_install=cuts, versions=('v0.3.12',))

    def exercise_built_payload(self, *, after_install=None, versions=None):
        # Frozen bounded_update.py:1804-1844,1882-1923;
        # update_controller.py:579-590,611-619. Nothing in the archive is replaced
        # by FieldBox's synthetic mapper, validator, modules or unit set.
        for executable in ('git', 'minisign'):
            if shutil.which(executable) is None:
                self.skipTest(executable + ' required for the full checkout payload test')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checkout = root / 'checkout'
            subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', str(ROOT), str(checkout)], check=True)
            # Git uses the caller's umask for checkout directories. Record the
            # test source tree's modes explicitly before the safe-file reader.
            for path in (checkout, *checkout.rglob('*')):
                if path.is_dir() and not path.is_symlink() and '.git' not in path.relative_to(checkout).parts:
                    path.chmod(0o755)
            spec = importlib.util.spec_from_file_location('field_release_build', ROOT / 'scripts/release/build.py')
            build = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(build)
            # Prepare an actual temporary index for uncommitted new payload
            # files. No commit/tag is created; production build uses HEAD.
            for folder in ('messagebox', 'systemd'):
                for path in (checkout / folder).rglob('*'):
                    if path.is_file() and not (ROOT / path.relative_to(checkout)).exists():
                        path.unlink()
            listing = (ROOT / 'scripts/release/payload.txt').read_text()
            write(checkout / 'scripts/release/payload.txt', listing.encode())
            for name in listing.splitlines():
                if name and not name.startswith('#') and not (checkout / name).exists():
                    write(checkout / name, (ROOT / name).read_bytes())
            subprocess.run(['git', '-C', str(checkout), 'add', '--all'], check=True)
            tree = build.git(checkout, 'write-tree')
            real_git = build.git
            def candidate_git(repo, *args):
                return real_git(repo, 'ls-tree', '-r', tree) if args == ('ls-tree', '-r', 'HEAD') else real_git(repo, *args)
            with mock.patch.object(build, 'git', side_effect=candidate_git):
                payload = build.payload_paths(checkout)
            # Include current uncommitted candidate bytes. No commits or tags are
            # created. A production build instead enforces a clean stationary tag.
            for name, mode in payload.items():
                write(checkout / name, (ROOT / name).read_bytes(), mode)
            raw = build.controller_descriptor(checkout, 7, 'v0.3.13')
            write(checkout / build.CONTROLLER_DESCRIPTOR, raw)
            public, secret = root / 'test.pub', root / 'test.key'
            subprocess.run(['minisign', '-G', '-W', '-p', str(public), '-s', str(secret)],
                           check=True, capture_output=True)
            public.chmod(0o644)
            secret.chmod(0o600)
            descriptor = write(root / 'descriptor', raw)
            signature = root / 'descriptor.minisig'
            subprocess.run(['minisign', '-S', '-s', str(secret), '-m', str(descriptor), '-x', str(signature)],
                           check=True, capture_output=True)
            write(checkout / build.CONTROLLER_SIGNATURE, (repr(signature.read_text()) + '\n').encode('ascii'))
            payload.update({build.CONTROLLER_DESCRIPTOR: 0o644, build.CONTROLLER_SIGNATURE: 0o644})
            commit = build.git(checkout, 'rev-parse', 'HEAD')
            manifest = build.release_manifest(checkout, 'v0.3.13', commit, 1, '1.1.0', payload)
            assets = root / 'assets'
            build.package(checkout, assets, manifest, payload, 0)
            original = {p.name: digest(p) for p in assets.iterdir()}
            for version in versions or PROVENANCE['releases']:
                with self.subTest(version=version):
                    box = FieldBox(root / version, version)
                    previous = os.umask(0o002)
                    try:
                        installed = box.run_built_release(assets)
                    finally:
                        restored = os.umask(previous)
                    self.assertEqual(restored, 0o002)
                    self.assertEqual(json.loads((installed / 'release.json').read_bytes())['commit'], commit)
                    for name in CONTROLLER:
                        self.assertEqual((installed / 'updater' / (name + '.py')).read_bytes(),
                                         (checkout / 'scripts/install' / (name + '.py')).read_bytes())
                    for name in (build.CONTROLLER_DESCRIPTOR, build.CONTROLLER_SIGNATURE):
                        self.assertEqual((installed / name).read_bytes(), (checkout / name).read_bytes())
                    # Reverify real retained proof offline with the disposable pin.
                    verify = __import__('release_verify')
                    proof = verify.verify_retained_controller(
                        installed, public.parent, application_version='v0.3.13',
                        platform={'os': 'raspios-trixie', 'architecture': 'aarch64', 'model': 'Raspberry Pi 4 Model B'})
                    self.assertEqual(proof['descriptor_sha256'], digest(descriptor))
                    self.assertEqual(original, {p.name: digest(p) for p in assets.iterdir()})
                    # Frozen bounded_update.py:248-251,1828-1844: both new
                    # Python inputs and all bootstrap/timer delivery paths.
                    from messagebox import controller_slots as slots
                    from messagebox import controller_selector as selector
                    from test_controller_slots import Host
                    write(box.installed('/etc/button-box-update/release-keys/test.pub'), public.read_bytes())
                    host = Host(box.root)
                    slots.bootstrap(box.root, run=host)
                    self.assertEqual(selector.valid(box.root)[0][0], selector.SLOTS[0])
                    for path, content in slots.routes().items():
                        self.assertEqual((box.root / path).read_bytes(), content)
                    if after_install is not None:
                        after_install(box)
            # The helper must pass through the builder's SHA256SUMS; it must
            # not silently repackage or repair damaged build output.
            rejected = FieldBox(root / 'damaged')
            before = rejected.tree()
            with (assets / 'button-box-v0.3.13.tar.gz').open('ab') as stream:
                stream.write(b'changed after build')
            previous = os.umask(0o002)
            try:
                with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
                    rejected.run_built_release(assets)
            finally:
                restored = os.umask(previous)
            self.assertEqual(restored, 0o002)
            self.assertEqual(rejected.tree(), before)
            self.assertEqual(rejected.worker.read_state(rejected.root, 'journal.json'), rejected.bootstrap_journal)
