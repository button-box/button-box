"""Bounded data parsing, real signatures, retained proof and legacy installation."""

import copy
import hashlib
import importlib
import json
import os
import pprint
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from field_updater import FieldBox, PROVENANCE, write
from test_release_tooling import build, release
from test_update_discovery import PLATFORM, verify


def literal(value):
    return (pprint.pformat(value, sort_dicts=True) + '\n').encode('ascii')


def controller_payload(root):
    for name in verify.CONTROLLER_MODULES:
        write(root / 'scripts/install' / name, ('# synthetic ' + name + '\n').encode())
    return build.controller_descriptor(root, 1, 'v0.3.13')


class DescriptorDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.raw = controller_payload(self.root)

    def test_exact_module_set_platform_capabilities_and_no_git_self_reference(self):
        value = verify.parse_controller_descriptor(self.raw)
        self.assertEqual(value['controller_version'], '1.3.0')
        self.assertEqual(value['capabilities'], {'signed_descriptor': True, 'self_upgrade': False})
        self.assertEqual(value['min_application_version'], 'v0.3.13')
        self.assertEqual(value['unit_changes'], [])
        self.assertEqual([entry['path'] for entry in value['modules']], ['updater/' + name for name in verify.CONTROLLER_MODULES])
        for entry in value['modules']:
            source = self.root / 'scripts/install' / Path(entry['path']).name
            self.assertEqual((entry['size'], entry['mode'], entry['sha256']),
                             (source.stat().st_size, 0o644, verify.digest(source)))
        self.assertNotIn('commit', value)
        for key in ('schema', 'product', 'selector_protocol', 'state_format',
                    'supported_os', 'supported_arch', 'supported_models', 'controller_sequence'):
            self.assertIn(key, value)

    def test_executable_duplicate_deep_oversized_and_malformed_data_reject_without_execution(self):
        marker = self.root / 'must-not-exist'
        bad = [b'', b'{}\n{}', b'DESCRIPTOR = {}', b'not Python',
               b"{'schema': 1, 'schema': 1}", b"{'a': {**{}}}", b'\xff',
               b'[' * 100 + b'1' + b']' * 100,
               b'[' + b','.join([b'1'] * 600) + b']',
               b' ' * (verify.MAX_DESCRIPTOR + 1),
               f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')".encode()]
        for raw in bad:
            with self.subTest(raw=raw[:40]), self.assertRaises(verify.ReleaseError):
                verify.parse_controller_descriptor(raw)
            self.assertFalse(marker.exists())

    def test_unknown_fields_and_incomplete_unsafe_or_duplicate_modules_reject(self):
        original = verify.parse_controller_descriptor(self.raw)
        cases = []
        for changes in ({'schema': True}, {'product': 'other'}, {'controller_sequence': True},
                        {'controller_sequence': 0}, {'controller_sequence': 2**63},
                        {'controller_version': 'latest'}, {'min_application_version': '1.3.0'},
                        {'state_format': False}, {'unknown': 1},
                        {'unit_changes': ['systemd/messagebox-update-install.service']},
                        {'capabilities': {'signed_descriptor': True}},
                        {'supported_arch': ['x86_64']}):
            cases.append({**copy.deepcopy(original), **changes})
        for kind in ('missing', 'extra', 'duplicate', 'path', 'mode', 'hash', 'size'):
            value = copy.deepcopy(original)
            if kind == 'missing':
                value['modules'].pop()
            elif kind == 'extra':
                value['modules'].append(dict(value['modules'][0]))
            elif kind == 'duplicate':
                value['modules'][1] = dict(value['modules'][0])
            else:
                field, content = {'path': ('path', '../escape.py'), 'mode': ('mode', 0o777),
                                  'hash': ('sha256', 'bad'), 'size': ('size', verify.MAX_CONTROLLER_MODULE + 1)}[kind]
                value['modules'][0][field] = content
            cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(verify.ReleaseError):
                verify.parse_controller_descriptor(literal(value))

    def test_source_links_hardlinks_writable_parents_and_special_files_reject(self):
        path = self.root / 'scripts/install/bounded_update.py'
        content = path.read_bytes()
        for kind in ('symlink', 'hardlink', 'fifo', 'writable', 'parent'):
            with self.subTest(kind=kind):
                path.unlink()
                if kind == 'symlink':
                    path.symlink_to(self.root / 'scripts/install/release_verify.py')
                elif kind == 'hardlink':
                    os.link(self.root / 'scripts/install/release_verify.py', path)
                elif kind == 'fifo':
                    os.mkfifo(path)
                else:
                    write(path, content, 0o666 if kind == 'writable' else 0o644)
                    if kind == 'parent':
                        path.parent.chmod(0o777)
                with self.assertRaises(verify.ReleaseError):
                    build.controller_descriptor(self.root, 1, 'v0.3.13')
                path.parent.chmod(0o755)
        path.unlink()
        write(path, content)


@unittest.skipUnless(shutil.which('minisign'), 'install minisign for real controller proof tests')
class ControllerProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys_temporary = tempfile.TemporaryDirectory()
        cls.key_root = Path(cls.keys_temporary.name)
        cls.key_pairs = []
        for name in ('release', 'wrong', 'emergency'):
            public, secret = cls.key_root / (name + '.pub'), cls.key_root / (name + '.key')
            subprocess.run(['minisign', '-G', '-W', '-p', str(public), '-s', str(secret)],
                           check=True, capture_output=True)
            cls.key_pairs.append((public, secret))

    @classmethod
    def tearDownClass(cls):
        cls.keys_temporary.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.payload = self.root / 'payload'
        self.keys = self.root / 'config/release-keys'
        write(self.keys / 'release.pub', self.key_pairs[0][0].read_bytes())
        self.raw = controller_payload(self.payload)
        self.signature = self.signed(self.raw)
        self.retained = self.root / 'release'
        self.stage_retained(self.retained)

    def signed(self, raw, pair=0):
        document = write(self.root / 'descriptor', raw)
        signature = self.root / 'descriptor.minisig'
        signature.unlink(missing_ok=True)
        subprocess.run(['minisign', '-S', '-m', str(document), '-s', str(self.key_pairs[pair][1]),
                        '-x', str(signature), '-c', 'Synthetic controller test', '-t', 'Synthetic controller test'],
                       check=True, capture_output=True)
        return literal(signature.read_text())

    def stage_retained(self, root):
        write(root / verify.CONTROLLER_DESCRIPTOR, self.raw)
        write(root / verify.CONTROLLER_SIGNATURE, self.signature)
        for name in verify.CONTROLLER_MODULES:
            write(root / 'updater' / name, (self.payload / 'scripts/install' / name).read_bytes())

    def check_retained(self, **options):
        return verify.verify_retained_controller(self.retained, self.keys, application_version='v0.3.13',
                                                 platform=PLATFORM, **options)

    def test_real_signature_and_offline_retained_module_proof(self):
        proof = self.check_retained()
        self.assertEqual(proof['descriptor_sha256'], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(proof['signing_key_id'], verify.public_key_id(verify.public_key_line(self.keys / 'release.pub')))
        self.assertEqual(proof['descriptor']['controller_version'], '1.3.0')
        self.check_retained(floor=1)  # Recheck an already accepted controller offline.
        with self.assertRaisesRegex(verify.ReleaseError, 'incompatible_controller'):
            self.check_retained(floor=2)

    def test_tampered_module_wrong_signed_hash_missing_signature_and_wrong_key_reject(self):
        module = self.retained / 'updater/bounded_update.py'
        before = module.read_bytes()
        module.write_bytes(before + b'# tampered\n')
        with self.assertRaisesRegex(verify.ReleaseError, 'module_mismatch'):
            self.check_retained()
        module.write_bytes(before)
        value = verify.parse_controller_descriptor(self.raw)
        value['modules'][0]['sha256'] = '0' * 64
        changed = literal(value)
        write(self.retained / verify.CONTROLLER_DESCRIPTOR, changed)
        write(self.retained / verify.CONTROLLER_SIGNATURE, self.signed(changed))
        with self.assertRaisesRegex(verify.ReleaseError, 'module_mismatch'):
            self.check_retained()
        self.stage_retained(self.retained)
        (self.retained / verify.CONTROLLER_SIGNATURE).unlink()
        with self.assertRaises(verify.ReleaseError):
            self.check_retained()
        self.stage_retained(self.retained)
        write(self.retained / verify.CONTROLLER_SIGNATURE, self.signed(self.raw, pair=1))
        with self.assertRaisesRegex(verify.ReleaseError, 'bad_controller_signature'):
            self.check_retained()

    def test_changed_descriptor_and_signed_malformed_data_reject(self):
        write(self.retained / verify.CONTROLLER_DESCRIPTOR, self.raw.replace(b'1.3.0', b'1.3.1'))
        with self.assertRaisesRegex(verify.ReleaseError, 'bad_controller_signature'):
            self.check_retained()
        for raw in (b"{'schema': 1}", b"__import__('os').abort()", b' ' * (verify.MAX_DESCRIPTOR + 1)):
            write(self.retained / verify.CONTROLLER_DESCRIPTOR, raw)
            write(self.retained / verify.CONTROLLER_SIGNATURE, self.signed(raw))
            with self.subTest(raw=raw[:30]), self.assertRaises(verify.ReleaseError):
                self.check_retained()

    def test_current_trust_generation_and_emergency_role_are_enforced(self):
        trust = importlib.import_module('trust_update')
        # Use the existing bootstrap, emergency signature, cumulative revocation
        # and generation switch. Build-time verification cannot pin old trust.
        self.check_retained()
        shutil.rmtree(self.keys.parent)
        source = self.root / 'bootstrap'
        write(source / 'release-keys/old.pub', self.key_pairs[0][0].read_bytes())
        write(source / 'emergency-keys/emergency.pub', self.key_pairs[2][0].read_bytes())
        state = self.root / 'trust-state'
        state.mkdir()
        trust.bootstrap_keys(source, self.keys.parent)
        with trust.trust_lock(state):
            trust.recover_trust(self.keys.parent, state)
            self.check_retained()
        value = {'schema': 1, 'product': 'button-box', 'trust_sequence': 1,
                 'release_keys': [verify.public_key_line(self.key_pairs[1][0])],
                 'revoked_key_ids': [verify.public_key_id(verify.public_key_line(self.key_pairs[0][0]))],
                 'created': '2026-10-21T12:00:00Z'}
        raw = json.dumps(value).encode()
        assets = self.root / 'trust-assets'
        write(assets / 'trust-update.json', raw)
        write(assets / 'trust-update.json.minisig', verify.controller_signature_bytes(self.signed(raw, pair=2)))
        trust.apply_trust(assets, self.keys.parent, state)
        with self.assertRaisesRegex(verify.ReleaseError, 'bad_controller_signature'):
            self.check_retained()
        write(self.retained / verify.CONTROLLER_SIGNATURE, self.signed(self.raw, pair=1))
        self.check_retained()
        write(self.keys / 'emergency.pub', self.key_pairs[2][0].read_bytes())
        write(self.keys.parent / 'emergency-keys/emergency.pub', self.key_pairs[2][0].read_bytes())
        write(self.retained / verify.CONTROLLER_SIGNATURE, self.signed(self.raw, pair=2))
        with self.assertRaisesRegex(verify.ReleaseError, 'bad_controller_signature'):
            self.check_retained()

    def test_compatibility_and_safe_retained_paths_reject_before_promotion(self):
        value = verify.parse_controller_descriptor(self.raw)
        for application, platform in (('v0.3.12', PLATFORM), ('v0.3.13-canary.1', PLATFORM),
                                      ('v0.3.13', {**PLATFORM, 'architecture': 'x86_64'})):
            with self.subTest(application=application), self.assertRaises(verify.ReleaseError):
                verify.check_controller_compatibility(value, application, platform)
        module = self.retained / 'updater/bounded_update.py'
        module.unlink()
        module.symlink_to(self.payload / 'scripts/install/bounded_update.py')
        with self.assertRaisesRegex(verify.ReleaseError, 'unsafe_controller_file'):
            self.check_retained()

    def test_frozen_workers_install_and_retain_both_proof_files_for_offline_recheck(self):
        # Frozen bounded_update.py:248-251,1828-1844,1882-1887;
        # frozen release-manifest.py:12-18. Both proof files use the old .py rule.
        for version in PROVENANCE['releases']:
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                box = FieldBox(directory, version)
                # Frozen workers must accept THIS checkout's candidate modules.
                for name in verify.CONTROLLER_MODULES:
                    source = Path(__file__).resolve().parents[1] / 'scripts/install' / name
                    write(box.source / 'scripts/install' / name, source.read_bytes())
                raw = build.controller_descriptor(box.source, 1, 'v0.3.13')
                signature = self.signed(raw)
                box.add(verify.CONTROLLER_DESCRIPTOR, raw)
                box.add(verify.CONTROLLER_SIGNATURE, signature)
                fixed = box.fixed_bytes()
                with box.controller() as controller:
                    bundle = box.bundle(controller)
                    controller.worker()
                    self.assertFalse(bundle.exists())
                    self.assertEqual(list((controller.STATE / 'staging').iterdir()), [])
                retained = box.installed(box.worker.controlled_release(box.root))
                self.assertEqual((retained / verify.CONTROLLER_DESCRIPTOR).read_bytes(), raw)
                self.assertEqual((retained / verify.CONTROLLER_SIGNATURE).read_bytes(), signature)
                proof = verify.verify_retained_controller(retained, self.keys, application_version='v0.3.13', platform=PLATFORM)
                self.assertEqual(proof['descriptor_sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(box.fixed_bytes(), fixed)

    def test_offline_signer_reuses_secret_policy_and_verifies_exact_descriptor(self):
        # Throwaway unencrypted keys exercise cryptography only. Production
        # signing_secret remains mandatory and is tested by release tooling.
        with mock.patch.object(release, 'public_key', return_value=self.keys / 'release.pub'), \
                mock.patch.object(release, 'signing_secret', return_value=self.key_pairs[0][1]):
            signature = release.sign_controller(self.raw, self.key_pairs[0][1], self.keys / 'release.pub')
        proof = verify.verify_controller_proof(self.raw, signature, self.keys)
        self.assertEqual(proof['descriptor_sha256'], hashlib.sha256(self.raw).hexdigest())

    def test_offline_signer_rejects_mismatched_selected_pin_during_key_overlap(self):
        pin = write(self.keys / 'second.pub', self.key_pairs[1][0].read_bytes())
        with mock.patch.object(release, 'public_key', return_value=pin), \
                mock.patch.object(release, 'signing_secret', return_value=self.key_pairs[0][1]), \
                self.assertRaises(subprocess.CalledProcessError):
            release.sign_controller(self.raw, self.key_pairs[0][1], pin)


class ControllerReleaseToolTests(unittest.TestCase):
    def test_signing_refuses_ci_before_a_signature_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            raw = controller_payload(root)
            with mock.patch.object(release, 'public_key', return_value=root / 'pin.pub'), \
                    mock.patch.dict(os.environ, {'CI': 'true'}), \
                    self.assertRaisesRegex(ValueError, 'CI'):
                release.sign_controller(raw, root / 'secret.key', root / 'pin.pub')
