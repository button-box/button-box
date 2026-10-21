"""Recovery bootstrap and exact verified-helper execution, without hardware."""

import copy
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from field_updater import FieldBox, write
from messagebox import recovery_bootstrap as bootstrap
from messagebox import recovery_launcher as launcher
from test_release_tooling import build
from test_update_discovery import PLATFORM, verify

ROOT = Path(__file__).resolve().parents[1]


def systemd(command, **kwargs):
    if command == ['systemctl', 'daemon-reload'] or command == ['systemctl', 'enable', '--now', launcher.MONITOR]:
        return subprocess.CompletedProcess(command, 0, '')
    if command[:2] == ['systemctl', 'is-enabled']:
        return subprocess.CompletedProcess(command, 0, 'enabled\n')
    if command[:2] == ['systemctl', 'show']:
        unit = command[2]
        start = ('recover --boot' if unit.endswith('recovery.service') else 'supervise')
        value = ('LoadState=loaded\nExecStartPre={ path=/usr/bin/python3 ; argv[]=' + bootstrap.LAUNCH + (' --supervisor' if unit.endswith('supervisor.service') else '') + ' ; ignore_errors=no ; }' +
                 '\nExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /usr/lib/messagebox-update/bounded_update.py ' + start + ' ; ignore_errors=no ; }' +
                 '\nBefore=messagebox.target comitup.service messagebox-update-controller.service\n'
                 'After=messagebox-update-recovery.service\n')
        return subprocess.CompletedProcess(command, 0, value)
    raise AssertionError(command)


@unittest.skipUnless(shutil.which('minisign'), 'minisign required for recovery proof tests')
class RecoverySupport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys_temp = tempfile.TemporaryDirectory()
        cls.keys = Path(cls.keys_temp.name).resolve()
        cls.public, cls.secret = cls.keys / 'test.pub', cls.keys / 'test.key'
        subprocess.run(['minisign', '-G', '-W', '-p', str(cls.public), '-s', str(cls.secret)],
                       check=True, capture_output=True)
        cls.public.chmod(0o644)
        cls.secret.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        cls.keys_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.box = self.candidate(self.base)
        self.root = self.box.root
        self.old = self.box.fixed_bytes()

    def sign(self, raw):
        document = write(self.keys / 'descriptor', raw)
        signed = self.keys / 'descriptor.minisig'
        subprocess.run(['minisign', '-S', '-s', str(self.secret), '-m', str(document), '-x', str(signed)],
                       check=True, capture_output=True)
        return (repr(signed.read_text()) + '\n').encode()

    def candidate(self, path, version='v0.3.12'):
        box = FieldBox(path, version)
        for name in verify.CONTROLLER_MODULES:
            write(box.source / 'scripts/install' / name, (ROOT / 'scripts/install' / name).read_bytes())
        box.add('systemd/' + launcher.MONITOR, launcher.MONITOR_UNIT)
        for name in verify.RECOVERY_FILES:
            box.add(name, (ROOT / name).read_bytes())
        raw = build.controller_descriptor(box.source, 7, 'v0.3.13')
        box.add(verify.CONTROLLER_DESCRIPTOR, raw)
        box.add(verify.CONTROLLER_SIGNATURE, self.sign(raw))
        # The old worker commits the app; fixed updater code stays frozen.
        box.install()
        write(box.installed('/etc/button-box-update/release-keys/test.pub'), self.public.read_bytes())
        # The old worker normally creates this lock before its transaction.
        write(box.installed('/var/lib/button-box-update/trust.lock'), b'', 0o600)
        return box

    def prepare(self, **kwargs):
        return bootstrap.bootstrap(self.root, platform=PLATFORM, run=systemd, **kwargs)

    def test_bootstrap_arms_independent_support_in_both_modes_without_cloud(self):
        for mode in ('cloud', 'wacli'):
            with self.subTest(mode=mode), mock.patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')):
                write(self.root / 'etc/messagebox/env', ('MSGBOX_TRANSPORT=' + mode + '\n').encode())
                state = self.prepare()
                self.assertEqual(launcher.launch(self.root), 'current')
                self.assertEqual(self.box.fixed_bytes(), self.old)
                self.assertEqual(launcher.document(self.root, launcher.STATE + '/journal.json')['phase'], 'armed')
                self.assertEqual(state['accepted']['sequence'], 7)
                self.assertNotEqual(state['helpers']['current']['directory'], state['helpers']['fallback']['directory'])
                for path, raw in bootstrap.DROPINS.items():
                    self.assertEqual(launcher.read(self.root, path, mode=0o644), raw)
                for path in (launcher.STATE + '/state.json', launcher.STATE + '/journal.json', launcher.STATE + '/promotion.lock'):
                    self.assertEqual((self.root / path).stat().st_mode & 0o777, 0o600)

    def test_corrupt_or_missing_helper_uses_verified_fallback_both_missing_keeps_old(self):
        state = self.prepare()
        current = self.root / state['helpers']['current']['directory'] / 'messagebox/recovery_helper.py'
        original = current.read_bytes()
        marker = self.base / 'unverified-executed'
        current.write_text(f"raise RuntimeError({str(marker)!r})\n")
        self.assertEqual(launcher.launch(self.root), 'fallback')
        self.assertFalse(marker.exists())
        current.unlink()
        self.assertEqual(launcher.launch(self.root), 'fallback')
        fallback = self.root / state['helpers']['fallback']['directory'] / 'messagebox/recovery_helper.py'
        fallback.unlink()
        self.assertEqual(launcher.launch(self.root), 'legacy_no_helper')
        self.assertEqual(self.box.fixed_bytes(), self.old)
        write(current, original)
        self.assertEqual(launcher.launch(self.root), 'current')

    def test_helper_executes_captured_bytes_after_hash_check(self):
        state = self.prepare()
        target = self.root / state['helpers']['current']['directory'] / 'messagebox/recovery_helper.py'
        select = launcher.select_helper
        def mutate(*args):
            result = select(*args)
            target.write_text("raise RuntimeError('changed after verification')\n")
            return result
        with mock.patch.object(launcher, 'select_helper', side_effect=mutate):
            self.assertEqual(launcher.launch(self.root), 'current')

    def test_bad_signature_or_revoked_key_never_executes_helper(self):
        state = self.prepare()
        for slot in state['helpers'].values():
            path = self.root / slot['directory'] / verify.CONTROLLER_SIGNATURE
            write(path, repr('invalid minisign signature').encode())
        self.assertEqual(launcher.launch(self.root), 'legacy_no_helper')
        self.assertEqual(self.box.fixed_bytes(), self.old)

    def test_replay_equal_digest_rule_and_version_floor_are_persistent(self):
        state = self.prepare()
        proof = {'descriptor': verify.parse_controller_descriptor((self.box.source / verify.CONTROLLER_DESCRIPTOR).read_bytes()),
                 'descriptor_sha256': state['accepted']['digest']}
        self.assertEqual(bootstrap.admission(proof, state['accepted']), state['accepted'])
        for sequence, digest, version in ((6, 'a' * 64, '1.3.0'), (7, 'a' * 64, '1.3.0'), (8, 'a' * 64, '1.2.9')):
            changed = copy.deepcopy(proof)
            changed['descriptor'].update(controller_sequence=sequence, controller_version=version)
            changed['descriptor_sha256'] = digest
            with self.subTest(sequence=sequence), self.assertRaisesRegex(launcher.RecoveryError, 'controller_replay'):
                bootstrap.admission(changed, launcher.document(self.root, launcher.STATE + '/state.json')['accepted'])
        self.assertEqual(self.prepare(), state)  # Reboot/retry reuses the accepted digest.

    def test_incomplete_application_or_unsafe_state_and_fixed_paths_fail_closed(self):
        journal_path = self.box.installed('/var/lib/button-box-update/journal.json')
        journal = json.loads(journal_path.read_bytes())
        journal['phase'] = 'probation'
        write(journal_path, json.dumps(journal).encode(), 0o600)
        with self.assertRaisesRegex(launcher.RecoveryError, 'application_not_committed'):
            self.prepare()
        self.assertFalse((self.root / launcher.SUPPORT / 'launcher.py').exists())
        journal['phase'] = 'committed'
        write(journal_path, json.dumps(journal).encode(), 0o600)
        self.prepare()
        path = self.root / launcher.STATE / 'state.json'
        raw = path.read_bytes()
        for kind in ('symlink', 'hardlink', 'writable', 'malformed'):
            with self.subTest(kind=kind):
                path.unlink()
                if kind == 'symlink':
                    target = write(self.base / 'outside', raw, 0o600)
                    path.symlink_to(target)
                elif kind == 'hardlink':
                    target = write(self.base / 'outside-hard', raw, 0o600)
                    os.link(target, path)
                else:
                    write(path, b'{}' if kind == 'malformed' else raw, 0o666 if kind == 'writable' else 0o600)
                with self.assertRaises((OSError, ValueError, KeyError, TypeError)):
                    launcher.launch(self.root)
        self.assertEqual(self.box.fixed_bytes(), self.old)

    def test_conflicting_dropin_or_wrong_effective_order_does_not_arm(self):
        write(self.root / bootstrap.RECOVERY_DROPIN, b'[Service]\nExecStart=/bad\n')
        with self.assertRaisesRegex(launcher.RecoveryError, 'permanent_recovery_changed'):
            self.prepare()
        self.assertEqual(launcher.document(self.root, launcher.STATE + '/journal.json')['phase'], 'prepared')
        write(self.root / bootstrap.RECOVERY_DROPIN, bootstrap.DROPINS[bootstrap.RECOVERY_DROPIN])
        def bad_order(command, **kwargs):
            result = systemd(command, **kwargs)
            result.stdout = result.stdout.replace('Before=messagebox.target', 'Before=')
            return result
        with self.assertRaisesRegex(launcher.RecoveryError, 'recovery_order_mismatch'):
            bootstrap.bootstrap(self.root, platform=PLATFORM, run=bad_order)
        self.assertEqual(launcher.document(self.root, launcher.STATE + '/journal.json')['phase'], 'prepared')
        self.prepare()
        self.assertEqual(launcher.launch(self.root), 'current')

    def test_signed_replays_reject_against_atomic_state_after_restart(self):
        state = self.prepare()
        release = self.root / state['application']['path']
        original = verify.parse_controller_descriptor((release / verify.CONTROLLER_DESCRIPTOR).read_bytes())
        for sequence, controller, helper in ((6, '1.3.0', '1.0.0'), (7, '1.3.1', '1.0.0'),
                                              (8, '1.2.9', '1.0.0'), (8, '1.3.0', '0.9.9')):
            value = copy.deepcopy(original)
            value.update(controller_sequence=sequence, controller_version=controller)
            value['recovery']['helper_version'] = helper
            raw = (repr(value) + '\n').encode()
            write(release / verify.CONTROLLER_DESCRIPTOR, raw)
            write(release / verify.CONTROLLER_SIGNATURE, self.sign(raw))
            with self.subTest(sequence=sequence, controller=controller, helper=helper):
                with self.assertRaisesRegex(launcher.RecoveryError, 'controller_replay'):
                    self.prepare()
                self.assertEqual(launcher.document(self.root, launcher.STATE + '/state.json'), state)
        self.assertEqual(self.box.fixed_bytes(), self.old)

    def test_revoked_signer_and_unknown_journal_cannot_bypass_recovery(self):
        state = self.prepare()
        (self.root / 'etc/button-box-update/release-keys/test.pub').unlink()
        self.assertEqual(launcher.launch(self.root), 'current')
        launcher.save(self.root, launcher.STATE + '/journal.json', {'schema': 1, 'kind': 'promotion', 'phase': 'exchange_intent'})
        with self.assertRaisesRegex(launcher.RecoveryError, 'invalid_promotion_journal'):
            launcher.launch(self.root)
        write(self.root / 'etc/button-box-update/release-keys/test.pub', self.public.read_bytes())
        # The permanent launcher dispatches this future journal to the signed
        # helper. Protocol 1's current helper then refuses it; there is no bypass.
        with self.assertRaisesRegex(ValueError, 'invalid_promotion_journal'):
            launcher.launch(self.root)
        self.assertEqual(self.box.fixed_bytes(), self.old)
        self.assertEqual(state['accepted']['helper_version'], '1.1.0')

    def test_busy_lock_and_changed_anchor_do_not_enter_helper(self):
        self.prepare()
        with launcher.lock(self.root, launcher.STATE + '/promotion.lock'):
            self.assertEqual(launcher.launch(self.root, supervisor=True), 'deferred')
        path = self.root / launcher.SUPPORT / 'release_verify.py'
        path.write_bytes(path.read_bytes() + b'\n# damaged\n')
        with self.assertRaisesRegex(launcher.RecoveryError, 'anchor_mismatch'):
            launcher.launch(self.root)
        self.assertEqual(self.box.fixed_bytes(), self.old)

    def test_power_cut_at_each_write_replace_and_flush_keeps_old_boot_path(self):
        # The model tracks file-data fsync separately from directory-entry fsync.
        # Each callback observes a prefix of the real bootstrap. Reconstructed
        # roots discard every unflushed change. The retained-write branch is
        # checked on the live prefix too. No application pointer is in the boot
        # image, so recovery cannot accidentally depend on /opt/messagebox.
        durable, flushed = {}, {}
        def tracked(relative):
            return relative != 'opt' and not relative.startswith('opt/')
        def record(path):
            if path.is_symlink():
                return ('link', os.readlink(path))
            info = path.stat()
            if path.is_dir():
                return ('directory', info.st_mode & 0o777)
            return ('file', info.st_mode & 0o777, path.read_bytes())
        for path in self.root.rglob('*'):
            relative = path.relative_to(self.root).as_posix()
            if not tracked(relative):
                continue
            durable[relative] = record(path)
            if path.is_file() and not path.is_symlink():
                flushed[path.stat().st_ino] = durable[relative]
        cuts, images = [], set()
        def boot_image(image):
            fingerprint = hash(tuple(sorted(image.items())))
            if fingerprint in images:
                return
            images.add(fingerprint)
            boot = self.base / 'boot-image'
            if boot.exists():
                shutil.rmtree(boot)
            boot.mkdir(mode=0o755)
            for relative, value in sorted(image.items()):
                path = boot / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if value[0] == 'directory':
                    path.mkdir(exist_ok=True)
                    path.chmod(value[1])
                elif value[0] == 'link':
                    path.symlink_to(value[1])
                else:
                    path.write_bytes(value[2])
                    path.chmod(value[1])
            for name, raw in self.old.items():
                self.assertEqual((boot / launcher.ACTIVE / (name + '.py')).read_bytes(), raw)
            if (boot / launcher.STATE / 'state.json').exists():
                state = launcher.validate_state(launcher.document(boot, launcher.STATE + '/state.json'))
                self.assertEqual(state['accepted']['sequence'], 7)
                anchor, _trust = launcher.anchors(boot)
                for slot in state['helpers'].values():
                    selected = {**state, 'helpers': {'current': slot, 'fallback': slot}}
                    self.assertEqual(launcher.select_helper(boot, selected, anchor)[0], 'current')
            if (boot / bootstrap.RECOVERY_DROPIN).exists():
                self.assertIn(launcher.launch(boot), ('current', 'fallback', 'legacy_no_helper'))
        def cut(event, relative):
            cuts.append((event, relative))
            path = self.root / relative
            if event == 'file_fsync':
                flushed[path.stat().st_ino] = record(path)
            elif event == 'directory_fsync':
                prefix = relative + '/' if relative else ''
                children = {child.name: child for child in path.iterdir() if tracked(prefix + child.name)}
                old = {name[len(prefix):].split('/')[0] for name in durable if name.startswith(prefix)}
                for missing in old - children.keys():
                    name = prefix + missing
                    for entry in list(durable):
                        if entry == name or entry.startswith(name + '/'):
                            del durable[entry]
                for name, child in children.items():
                    entry = prefix + name
                    if child.is_file() and not child.is_symlink():
                        durable[entry] = flushed.get(child.stat().st_ino, ('file', child.stat().st_mode & 0o777, b''))
                    else:
                        durable[entry] = record(child)
            boot_image(durable)
            # All writes retained is another permitted outcome before fsync.
            # Bootstrap owns the locks on this root; boot owns them after reset.
            if (self.root / bootstrap.RECOVERY_DROPIN).exists():
                import contextlib
                with mock.patch.object(launcher, 'locks', return_value=contextlib.nullcontext()):
                    self.assertIn(launcher.launch(self.root), ('current', 'fallback', 'legacy_no_helper'))
            self.assertEqual(self.box.fixed_bytes(), self.old)
        self.prepare(step=cut)
        self.assertTrue({'mkdir', 'file_write', 'file_fsync', 'replace', 'directory_fsync',
                         'daemon_reload', 'effective_units'} <= {event for event, path in cuts})
        self.assertGreater(len(images), 20)
        # One actual interrupted invocation, then restart from the prepared state.
        def interrupt(event, path):
            if event == 'replace' and path == launcher.STATE + '/journal.json':
                raise KeyboardInterrupt('simulated process loss')
        interrupted = self.candidate(self.base / 'interrupted')
        with self.assertRaises(KeyboardInterrupt):
            bootstrap.bootstrap(interrupted.root, platform=PLATFORM, run=systemd, step=interrupt)
        self.assertEqual(launcher.launch(interrupted.root), 'current')
        bootstrap.bootstrap(interrupted.root, platform=PLATFORM, run=systemd)
        self.assertEqual(self.box.fixed_bytes(), self.old)
        print(f'Recovery bootstrap crash matrix: {len(cuts)} boundaries; {len(images)} distinct durable images')

    def test_unknown_field_controller_is_not_enrolled_and_parent_links_reject(self):
        path = self.root / launcher.ACTIVE / 'bounded_update.py'
        path.write_bytes(path.read_bytes() + b'\n# unknown field build\n')
        with self.assertRaisesRegex(launcher.RecoveryError, 'unknown_field_controller'):
            self.prepare()
        self.assertFalse((self.root / bootstrap.RECOVERY_DROPIN).exists())
        parent = self.root / launcher.STATE
        shutil.rmtree(parent)
        outside = self.base / 'unsafe-parent'
        outside.mkdir(mode=0o755)
        parent.symlink_to(outside)
        with self.assertRaises((OSError, launcher.RecoveryError)):
            self.prepare()
        self.assertEqual(list(outside.iterdir()), [])

    def test_schema_two_signs_exact_recovery_files_and_refuses_malformed_sets(self):
        raw = (self.box.source / verify.CONTROLLER_DESCRIPTOR).read_bytes()
        original = verify.parse_controller_descriptor(raw)
        self.assertEqual(original['schema'], 2)
        self.assertEqual([entry['path'] for entry in original['recovery']['files']], list(verify.RECOVERY_FILES))
        bad = []
        for field, value in (('protocol', 2), ('helper_version', 'latest'), ('files', [])):
            changed = copy.deepcopy(original)
            changed['recovery'][field] = value
            bad.append(changed)
        for field, value in (('path', '../escape.py'), ('mode', 0o755), ('size', True), ('sha256', 'bad')):
            changed = copy.deepcopy(original)
            changed['recovery']['files'][0][field] = value
            bad.append(changed)
        for value in bad:
            with self.assertRaises(verify.ReleaseError):
                verify.parse_controller_descriptor(repr(value).encode())

    def test_installed_hooks_with_missing_state_cannot_bypass_recovery(self):
        self.prepare()
        for name in ('state.json', 'journal.json'):
            (self.root / launcher.STATE / name).unlink()
        with self.assertRaisesRegex(launcher.RecoveryError, 'selection_missing'):
            launcher.launch(self.root)

    def test_effective_unit_cannot_ignore_recovery_failure_or_substitute_executable(self):
        for source, target in (('ignore_errors=no', 'ignore_errors=yes'),
                               ('path=/usr/bin/python3', 'path=/other/python3')):
            def bad_gate(command, **kwargs):
                result = systemd(command, **kwargs)
                result.stdout = result.stdout.replace(source, target)
                return result
            with self.subTest(target=target), self.assertRaisesRegex(launcher.RecoveryError, 'recovery_unit_mismatch'):
                bootstrap.effective_units(bad_gate)

    def test_first_bootstrap_recovers_pending_trust_before_accepting_proof(self):
        import base64
        replacement = base64.b64encode(b'Ed' + b'n' * 40).decode()
        old_id = verify.public_key_id(verify.public_key_line(self.public))
        pending = {'schema': 1, 'product': 'button-box', 'trust_sequence': 1,
                   'release_keys': [replacement], 'revoked_key_ids': [old_id],
                   'created': '2026-10-21T00:00:00Z'}
        write(self.root / 'var/lib/button-box-update/trust-pending.json', json.dumps(pending).encode(), 0o600)
        with self.assertRaisesRegex(ValueError, 'bad_controller_signature'):
            self.prepare()
        self.assertFalse((self.root / launcher.SUPPORT / 'launcher.py').exists())
        state = json.loads((self.root / 'var/lib/button-box-update/trust-sequence.json').read_bytes())
        self.assertEqual(state['trust_sequence'], 1)
        self.assertIn(old_id, state['revoked_key_ids'])

    def test_signed_unit_change_must_match_bootstrap_before_support_install(self):
        release, identity = bootstrap.application(self.root)
        descriptor = self.root / release / verify.CONTROLLER_DESCRIPTOR
        value = verify.parse_controller_descriptor(descriptor.read_bytes())
        self.assertEqual(value['unit_changes'], [
            {'path': '/' + path, 'size': len(raw), 'mode': 0o644, 'sha256': launcher.sha(raw)}
            for path, raw in sorted(bootstrap.DROPINS.items())])
        value['unit_changes'][0]['sha256'] = '0' * 64
        raw = repr(value).encode()
        write(descriptor, raw)
        write(self.root / release / verify.CONTROLLER_SIGNATURE, self.sign(raw))
        with self.assertRaisesRegex(launcher.RecoveryError, 'recovery_units_not_authorized'):
            self.prepare()
        self.assertFalse((self.root / launcher.SUPPORT / 'launcher.py').exists())
        self.assertEqual(self.box.fixed_bytes(), self.old)
