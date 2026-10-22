"""Real minisign trust replacement, restart boundaries, and updater integration."""

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import test_update_discovery as release_tests

INSTALL, PLATFORM = release_tests.INSTALL, release_tests.PLATFORM

sys.path.insert(0, str(INSTALL))
trust = importlib.import_module('trust_update')
verify = importlib.import_module('release_verify')
controller = importlib.import_module('update_controller')
discovery = importlib.import_module('update_discovery')
sys.path.pop(0)

ROOT = INSTALL.parents[1]
sys.path.insert(0, str(ROOT / 'scripts/release'))
spec = importlib.util.spec_from_file_location('trust_tool', ROOT / 'scripts/release/trust-update.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
sys.path.pop(0)


class PowerLoss(BaseException):
    pass


@unittest.skipUnless(shutil.which('minisign'), 'install minisign for trust contract tests')
class TrustTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        root_patch = mock.patch.object(controller, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.source = self.root / 'source'
        self.config = self.root / 'config'
        self.state = self.root / 'state'
        self.state.mkdir()
        for name in ('release-keys', 'emergency-keys'):
            (self.source / name).mkdir(parents=True)
        self.old, self.new, self.emergency = [self.generate(name) for name in ('old', 'new', 'emergency')]
        shutil.copyfile(self.old[0], self.source / 'release-keys/old.pub')
        shutil.copyfile(self.emergency[0], self.source / 'emergency-keys/emergency.pub')
        trust.bootstrap_keys(self.source, self.config)
        self.directory = self.root / 'assets'
        self.directory.mkdir()

    def generate(self, name):
        public, secret = self.root / (name + '.pub'), self.root / (name + '.key')
        subprocess.run(['minisign', '-G', '-W', '-p', str(public), '-s', str(secret)],
                       check=True, capture_output=True)
        return public, secret

    def document(self, sequence=1, keys=None, revoked=None, **changes):
        return {'schema': 1, 'product': 'button-box', 'trust_sequence': sequence,
                'release_keys': [verify.public_key_line(key) for key in (keys or [self.new[0]])],
                'revoked_key_ids': revoked or [], 'created': '2026-10-12T12:00:00Z', **changes}

    def signed(self, value=None, *, secret=None, raw=None):
        path = self.directory / 'trust-update.json'
        path.write_bytes(raw if raw is not None else json.dumps(value or self.document()).encode())
        subprocess.run(['minisign', '-S', '-m', str(path), '-s', str(secret or self.emergency[1]),
                        '-x', str(self.directory / 'trust-update.json.minisig')], check=True, capture_output=True)
        return self.directory

    def apply(self, **kwargs):
        return trust.apply_trust(self.directory, self.config, self.state, **kwargs)

    def pins(self):
        return {verify.public_key_line(path) for path in (self.config / 'release-keys').glob('*.pub')}

    def release_bundle(self, secret):
        directory = self.root / ('release-' + secret.stem)
        directory.mkdir()
        self.secret = secret
        release_tests.SignatureTests.bundle(self, directory)
        return directory

    def verify_release(self, directory):
        return verify.verify_bundle(directory, self.config / 'release-keys', version='v0.3.0',
                                    channel='stable', floor=0, platform=PLATFORM)

    def test_swap_rejects_downloaded_old_release_and_accepts_new_release(self):
        old_bundle = self.release_bundle(self.old[1])
        self.verify_release(old_bundle)
        self.signed()
        self.assertEqual(self.apply(), 1)
        self.assertEqual(self.pins(), {verify.public_key_line(self.new[0])})
        with self.assertRaisesRegex(verify.ReleaseError, 'bad_signature'):
            self.verify_release(old_bundle)
        self.verify_release(self.release_bundle(self.new[1]))
        ticket = {'operation_id': 'test', 'bundle': str(old_bundle), 'version': 'v0.3.0',
                  'manifest_sha256': verify.digest(old_bundle / 'release-manifest.json')}
        download = self.state / 'downloads' / ('a' * 32)
        download.parent.mkdir()
        shutil.move(old_bundle, download)
        ticket['bundle'] = str(download)
        with mock.patch.object(controller, 'STATE', self.state), \
                mock.patch.object(controller, 'KEYS', self.config / 'release-keys'), \
                mock.patch.object(controller, 'read', side_effect=lambda name, default=None:
                                  ticket if name == 'ticket.json' else {'operation_id': 'test'}), \
                mock.patch.object(controller, 'policy', return_value={'channel': 'stable'}), \
                mock.patch.object(controller, 'local_platform', return_value=PLATFORM), \
                mock.patch.object(controller, 'save'), mock.patch.object(controller, 'record'), \
                mock.patch.object(controller.update, 'install_transaction') as install:
            with self.assertRaisesRegex(verify.ReleaseError, 'bad_signature'):
                controller.worker()
            install.assert_not_called()

    def test_roles_are_separate_in_both_directions(self):
        self.signed(secret=self.old[1])
        with self.assertRaisesRegex(verify.ReleaseError, 'bad_trust_signature'):
            self.apply()
        # Even an erroneously copied emergency pin is never a release signer.
        shutil.copyfile(self.emergency[0], self.config / 'release-keys/emergency.pub')
        with self.assertRaisesRegex(verify.ReleaseError, 'bad_signature'):
            self.verify_release(self.release_bundle(self.emergency[1]))
        self.signed(self.document(keys=[self.emergency[0]]))
        with self.assertRaisesRegex(verify.ReleaseError, 'key_role_overlap'):
            self.apply()

    def test_sequence_and_permanent_revocation_survive_restart(self):
        revoked = verify.public_key_id(verify.public_key_line(self.old[0]))
        self.signed(self.document(sequence=4, revoked=[revoked]))
        self.apply()
        for sequence in (3, 4):
            self.signed(self.document(sequence=sequence))
            with self.assertRaisesRegex(verify.ReleaseError, 'trust_sequence_not_newer'):
                self.apply()
        self.signed(self.document(sequence=5))
        self.apply()
        self.assertEqual(trust.trust_state(self.state)['revoked_key_ids'], [revoked])
        self.signed(self.document(sequence=6, keys=[self.old[0], self.new[0]]))
        with self.assertRaisesRegex(verify.ReleaseError, 'revoked_release_key'):
            self.apply()
        self.assertEqual(trust.trust_state(self.state)['trust_sequence'], 5)

    def test_strict_bounded_documents_never_mutate_trust(self):
        values = [self.document(schema=True), self.document(trust_sequence=True), self.document(trust_sequence=0),
                  self.document(product='other'), self.document(extra=1), self.document(created='yesterday'),
                  self.document(release_keys=[]), self.document(release_keys=['not a public key']),
                  self.document(release_keys=[verify.public_key_line(self.new[0])] * 4),
                  self.document(revoked_key_ids=['bad']), self.document(revoked_key_ids=['A' * 16] * 2),
                  self.document(revoked_key_ids=[verify.public_key_id(verify.public_key_line(self.new[0]))])]
        raw_values = [json.dumps(value).encode() for value in values] + [b'{}', b'[]', b'\xff',
            b'{"schema":1,"schema":1}', b' ' * 8193, b'{' * 8000]
        original = self.pins()
        for raw in raw_values:
            with self.subTest(raw=raw[:40]):
                self.signed(raw=raw)
                with self.assertRaises(verify.ReleaseError):
                    self.apply()
                self.assertEqual(self.pins(), original)
                self.assertFalse((self.state / 'trust-sequence.json').exists())

    def test_missing_emergency_directory_is_off_and_releases_still_work(self):
        shutil.rmtree(self.config / 'emergency-keys')
        self.signed()
        with self.assertRaisesRegex(verify.ReleaseError, 'trust_updates_disabled'):
            self.apply()
        self.verify_release(self.release_bundle(self.old[1]))

    def test_crash_boundaries_always_have_complete_pins_and_roll_forward(self):
        for phase in ('intent', 'prepared', 'switched', 'recorded'):
            with self.subTest(phase=phase):
                config, state = self.root / ('config-' + phase), self.root / ('state-' + phase)
                state.mkdir()
                trust.bootstrap_keys(self.source, config)
                self.signed()
                def crash(point):
                    pins = {verify.public_key_line(key) for key in (config / 'release-keys').glob('*.pub')}
                    self.assertIn(pins, [{verify.public_key_line(self.old[0])}, {verify.public_key_line(self.new[0])}])
                    if point == phase:
                        raise PowerLoss()
                with self.assertRaises(PowerLoss):
                    trust.apply_trust(self.directory, config, state, checkpoint=crash)
                with trust.trust_lock(state):
                    trust.recover_trust(config, state)
                    trust.recover_trust(config, state)
                self.assertEqual(trust.trust_state(state)['trust_sequence'], 1)
                self.assertEqual({verify.public_key_line(key) for key in (config / 'release-keys').glob('*.pub')},
                                 {verify.public_key_line(self.new[0])})
                self.assertEqual(len(list((config / 'release-keysets').iterdir())), 1)

    def test_bootstrap_rejects_same_key_with_different_comments_before_writing(self):
        key = self.source / 'emergency-keys/emergency.pub'
        key.write_text('untrusted comment: changed comment\n' + verify.public_key_line(self.old[0]) + '\n')
        target = self.root / 'duplicate-config'
        with self.assertRaisesRegex(verify.ReleaseError, 'key_role_overlap'):
            trust.bootstrap_keys(self.source, target)
        self.assertFalse(target.exists())
        for key in (self.config / 'emergency-keys').glob('*.pub'):
            self.assertEqual(key.stat().st_mode & 0o777, 0o644)
            self.assertEqual(key.stat().st_uid, os.geteuid())

    def test_bootstrap_retry_accepts_identical_pin_and_rejects_different_before_writes(self):
        pins = list((self.config / 'emergency-keys').glob('*.pub'))
        original = pins[0].read_bytes()
        trust.bootstrap_keys(self.source, self.config)
        self.assertEqual(pins[0].read_bytes(), original)
        self.assertEqual(self.pins(), {verify.public_key_line(self.old[0])})
        # Keep the ID/filename but replace the public-key bytes.
        pins[0].write_bytes(self.new[0].read_bytes())
        previous = os.readlink(self.config / 'release-keys')
        with self.assertRaisesRegex(verify.ReleaseError, 'emergency_pin_changed'):
            trust.bootstrap_keys(self.source, self.config)
        self.assertEqual(os.readlink(self.config / 'release-keys'), previous)
        self.assertEqual(pins[0].read_bytes(), self.new[0].read_bytes())

    def test_worker_record_preserves_bounded_rollback_detail(self):
        operation = {'operation_id': 'synthetic-operation', 'version': 'v0.3.0'}
        controller.update.save_state(self.root, 'result.json', {
            'operation_id': operation['operation_id'], 'state': 'rolled_back',
            'reason': 'readiness_lost', 'detail': 'evidence:heartbeat:fresh'})
        with mock.patch.object(controller, 'read', side_effect=lambda name, default=None:
                               controller.update.read_state(self.root, name, default)), \
             mock.patch.object(controller, 'save', side_effect=lambda name, value, mode=0o600:
                               controller.update.save_state(self.root, name, value, mode)), \
             mock.patch.object(controller, 'installed_identity', return_value={}):
            controller.record(operation, 'rolled_back', 'readiness_lost')
        self.assertEqual(controller.update.read_state(self.root, 'result.json')['detail'],
                         'evidence:heartbeat:fresh')

    def test_install_lock_defers_trust_without_blocking_broker(self):
        self.signed()
        with trust.trust_lock(self.state):
            with self.assertRaisesRegex(verify.ReleaseError, 'trust_update_busy'):
                self.apply()
        self.assertFalse((self.state / 'trust-pending.json').exists())

    def test_broker_only_accepts_downloader_and_copies_bounded_regular_files(self):
        self.signed()
        cache = self.root / 'cache'
        offer = cache / 'trust-v1'
        shutil.copytree(self.directory, offer)
        broker = controller.Controller()
        payload = {'command': 'trust_downloaded', 'version': 'trust-v1'}
        with mock.patch.object(controller, 'STATE', self.state), \
                mock.patch.object(controller, 'CACHE', cache), \
                mock.patch.object(controller, 'KEYS', self.config / 'release-keys'), \
                mock.patch.object(controller, 'public_status'), \
                mock.patch.object(controller.pwd, 'getpwnam', side_effect=lambda name:
                                  types.SimpleNamespace(pw_uid=100 if name == 'messagebox' else 101)):
            with self.assertRaisesRegex(ValueError, 'not_downloader'):
                broker.dispatch(payload, (10, 100, 100))
            # A network-controlled symlink must not be followed by root.
            document = offer / 'trust-update.json'
            document.unlink()
            document.symlink_to(self.directory / 'trust-update.json')
            with self.assertRaises(OSError):
                broker.dispatch(payload, (11, 101, 101))
            document.unlink()
            shutil.copyfile(self.directory / document.name, document)
            self.assertEqual(broker.dispatch(payload, (11, 101, 101))['trust_sequence'], 1)
        self.assertEqual(list((self.state / 'downloads').iterdir()), [])

    def test_discovery_chooses_highest_signed_trust_and_honors_floor(self):
        client = discovery.Discovery(self.root / 'cache', clock=lambda: 1700000001, jitter=lambda *args: 0)
        offers = [{'tag_name': 'trust-v' + str(sequence)} for sequence in (3, 1, 2)]
        notified = []
        with mock.patch.object(client, 'json', return_value=offers), \
                mock.patch.object(client, 'trust_candidate', side_effect=[self.document(sequence=n) for n in (3, 1, 2)]):
            client.poll('stable', 0, PLATFORM, trust_enabled=True, trust_floor=2, notify_trust=notified.append)
        self.assertEqual(notified, ['trust-v3'])
        client.clock = lambda: 1700001001
        with mock.patch.object(client, 'json', return_value=offers), \
                mock.patch.object(client, 'trust_candidate', side_effect=[self.document(sequence=n) for n in (3, 1, 2)]):
            client.poll('stable', 0, PLATFORM, trust_enabled=True, trust_floor=3, hint=True, notify_trust=notified.append)
        self.assertEqual(notified, ['trust-v3'])

    def test_discovery_downloads_trust_first_even_for_requested_release(self):
        self.signed(self.document(revoked=[verify.public_key_id(verify.public_key_line(self.old[0]))]))
        cache = self.root / 'cache'
        client = discovery.Discovery(cache, clock=lambda: 1700000001, jitter=lambda *args: 0)
        trust_release = {'tag_name': 'trust-v1', 'draft': False, 'prerelease': True, 'assets': [
            {'name': path.name, 'size': path.stat().st_size,
             'browser_download_url': 'https://github.com/button-box/button-box/releases/download/trust-v1/' + path.name}
            for path in self.directory.iterdir()]}
        normal = {'tag_name': 'v0.3.0'}
        def download(url, path, limit):
            shutil.copyfile(self.directory / path.name, path)
        events = []
        def received(version):
            events.append('trust')
            trust.apply_trust(cache / version, self.config, self.state)
        def candidate(*args):
            events.append('release')
            self.assertEqual(self.pins(), {verify.public_key_line(self.new[0])})
            raise verify.ReleaseError('bad_signature')
        with mock.patch.object(discovery, 'KEYS', self.config / 'release-keys'), \
                mock.patch.object(client, 'json', side_effect=lambda url: normal if '/tags/' in url else [trust_release]), \
                mock.patch.object(client, 'download', side_effect=download), \
                mock.patch.object(client, 'candidate', side_effect=candidate):
            client.poll('stable', 0, PLATFORM, requested='v0.3.0', trust_enabled=True, notify_trust=received)
        self.assertEqual(events, ['trust', 'release'])
        self.assertEqual(list(cache.glob('trust-v*')), [])

    def test_tool_build_verify_and_signing_guards(self):
        output = self.root / 'built'
        with mock.patch.object(tool, 'ROOT', self.source):
            tool.build(output, [self.new[0]], 1, [], '2026-10-12T12:00:00Z')
        self.assertEqual(trust.parse_document((output / 'trust-update.json').read_bytes())['trust_sequence'], 1)
        with mock.patch.dict(os.environ, {'CI': '1'}), \
                self.assertRaisesRegex(ValueError, 'CI'):
            tool.sign(output, self.emergency[1], self.emergency[0])
        with mock.patch.dict(os.environ, {}, clear=True), \
                self.assertRaisesRegex(ValueError, 'encrypted'):
            tool.sign(output, self.emergency[1], self.emergency[0])
        self.signed()
        pin = self.source / 'emergency-keys/emergency.pub'
        with mock.patch.object(tool, 'ROOT', self.source.parent):
            # A pin outside config/emergency-keys is refused, even if valid.
            with self.assertRaisesRegex(ValueError, 'pinned'):
                tool.verify(self.directory, pin)
        repo = self.root / 'tool-repo'
        pins = repo / 'config/emergency-keys'
        pins.mkdir(parents=True)
        pin = pins / 'emergency.pub'
        shutil.copyfile(self.emergency[0], pin)
        with mock.patch.object(tool, 'ROOT', repo):
            self.assertEqual(tool.verify(self.directory, pin)['trust_sequence'], 1)
        secret = repo / 'private.key'
        secret.write_text('synthetic test secret\n')
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(sys.modules['release'], 'ROOT', repo.resolve()), \
                self.assertRaisesRegex(ValueError, 'outside'):
            tool.sign(output, secret, pin)
        secret = output / 'private.key'
        secret.write_text('synthetic test secret\n')
        with mock.patch.dict(os.environ, {}, clear=True), \
                self.assertRaisesRegex(ValueError, 'outside'):
            tool.sign(output, secret, pin)


class CloudTrustReportingTests(unittest.TestCase):
    def test_heartbeat_reports_sequence_only_in_cloud(self):
        from messagebox import cloud_update
        with mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT': 'cloud'}), \
                mock.patch.object(cloud_update, 'request', return_value={'installed': True, 'trust_sequence': 7}):
            self.assertEqual(cloud_update.heartbeat_fields()['software_update']['trust_sequence'], 7)
        with mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT': 'wacli'}), \
                mock.patch.object(cloud_update, 'request') as request:
            self.assertEqual(cloud_update.heartbeat_fields(), {})
            request.assert_not_called()
