"""Real throwaway minisign keys plus bounded network/archive contract tests."""

import io
import importlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

INSTALL = Path(__file__).resolve().parents[1] / 'scripts/install'
sys.path.insert(0, str(INSTALL))
verify = importlib.import_module("release_verify")
discovery = importlib.import_module("update_discovery")
sys.path.pop(0)

PLATFORM = {'os': 'raspios-trixie', 'architecture': 'aarch64', 'model': 'Raspberry Pi 4 Model B'}


def manifest(version='v0.3.0', sequence=1):
    return {'schema': 1, 'product': 'button-box', 'version': version, 'tag': version, 'commit': 'a' * 40,
            'release_sequence': sequence, 'channel': 'canary' if '-canary.' in version else 'stable',
            'supported_os': ['raspios-trixie'], 'supported_arch': ['aarch64'],
            'supported_models': ['Raspberry Pi 4 Model B'], 'min_updater_version': '1.0.0', 'files': []}


@unittest.skipUnless(shutil.which('minisign'), 'install minisign to run real signature contract tests')
class SignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)
        cls.keys = cls.base / 'keys'
        cls.keys.mkdir()
        cls.secret = cls.base / 'throwaway.key'
        subprocess.run(['minisign', '-G', '-W', '-p', str(cls.keys / 'test.pub'), '-s', str(cls.secret)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def bundle(self, directory, metadata=None, member=None):
        directory = Path(directory)
        metadata = metadata or manifest()
        (directory / 'release-manifest.json').write_text(json.dumps(metadata))
        archive = directory / f"button-box-{metadata['version']}.tar.gz"
        with tarfile.open(archive, 'w:gz') as output:
            if member:
                output.addfile(member, io.BytesIO(b'x') if member.isfile() else None)
            else:
                entry = tarfile.TarInfo('VERSION')
                entry.size = len(metadata['version'])
                output.addfile(entry, io.BytesIO(metadata['version'].encode()))
        hashes = directory / 'SHA256SUMS'
        hashes.write_text(''.join(f'{verify.digest(directory / name)}  {name}\n' for name in sorted((archive.name, 'release-manifest.json'))))
        subprocess.run(['minisign', '-Sm', str(hashes), '-s', str(self.secret), '-x', str(directory / 'SHA256SUMS.minisig')],
                       check=True, capture_output=True)
        return metadata

    def verify(self, directory, metadata, *, floor=0, channel='stable', platform=PLATFORM, keys=None):
        return verify.verify_bundle(directory, keys or self.keys, version=metadata['version'], channel=channel,
                                    floor=floor, platform=platform)

    def test_real_signature_binds_archive_manifest_and_key(self):
        for problem in ('good', 'wrong_key', 'bad_signature', 'archive', 'manifest'):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as directory:
                metadata = self.bundle(directory)
                keys = self.keys
                if problem == 'wrong_key':
                    keys = Path(directory) / 'wrong-keys'
                    keys.mkdir()
                    subprocess.run(['minisign', '-G', '-W', '-p', str(keys / 'wrong.pub'), '-s', str(Path(directory) / 'wrong.key')],
                                   check=True, capture_output=True)
                elif problem == 'bad_signature':
                    (Path(directory) / 'SHA256SUMS.minisig').write_text('invalid signature')
                elif problem in ('archive', 'manifest'):
                    name = f"button-box-{metadata['version']}.tar.gz" if problem == 'archive' else 'release-manifest.json'
                    with (Path(directory) / name).open('ab') as output:
                        output.write(b'tampered')
                if problem == 'good':
                    self.assertEqual(self.verify(directory, metadata), metadata)
                else:
                    with self.assertRaisesRegex(verify.ReleaseError, 'bad_signature|hash_mismatch'):
                        self.verify(directory, metadata, keys=keys)

    def test_voice_release_requires_promoted_controller(self):
        old_path = Path(__file__).parent / 'fixtures/pi-v0.3.12/release_verify.py'
        spec = importlib.util.spec_from_file_location('v0312_release_verify', old_path)
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        with tempfile.TemporaryDirectory() as directory:
            metadata = manifest()
            metadata['min_updater_version'] = '1.2.0'
            self.bundle(directory, metadata)
            self.assertEqual(self.verify(directory, metadata), metadata)
            with self.assertRaisesRegex(old.ReleaseError, 'incompatible'):
                old.verify_bundle(directory, self.keys, version=metadata['version'], channel='stable',
                                  floor=0, platform=PLATFORM)

    def test_signed_downgrade_canary_to_stable_and_pi3_are_rejected(self):
        cases = [(manifest(), 1, 'stable', PLATFORM),
                 (manifest('v0.3.0-canary.1'), 0, 'stable', PLATFORM),
                 (manifest(), 0, 'stable', {**PLATFORM, 'model': 'Raspberry Pi 3 Model A Plus'})]
        for metadata, floor, channel, platform in cases:
            with self.subTest(metadata=metadata['version'], platform=platform), tempfile.TemporaryDirectory() as directory:
                self.bundle(directory, metadata)
                with self.assertRaises(verify.ReleaseError):
                    self.verify(directory, metadata, floor=floor, channel=channel, platform=platform)
        # A higher sequence stable release can replace a canary on a canary box.
        with tempfile.TemporaryDirectory() as directory:
            metadata = self.bundle(directory, manifest(sequence=4))
            self.assertEqual(self.verify(directory, metadata, channel='canary', floor=3)['release_sequence'], 4)

    def test_archive_rejects_links_traversal_and_devices_before_extraction(self):
        for name, kind in (('../escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE), ('device', tarfile.CHRTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.size = 1 if kind == tarfile.REGTYPE else 0
                entry.linkname = '/etc/passwd'
                metadata = self.bundle(directory, member=entry)
                self.verify(directory, metadata)
                with self.assertRaisesRegex(verify.ReleaseError, 'unsafe_archive'):
                    verify.extract_bundle(directory, Path(directory) / 'extracted', metadata['version'])
                self.assertFalse((Path(directory) / 'extracted').exists())


class DiscoveryTests(unittest.TestCase):
    def test_slow_download_has_deadline_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [0]
            class SlowResponse(io.BytesIO):
                def read(self, size):
                    now[0] += 750
                    return super().read(size)
            opener = mock.Mock(return_value=SlowResponse(b'x' * (64 * 1024 + 1)))
            client = discovery.Discovery(directory, opener=opener, monotonic=lambda: now[0])
            target = Path(directory) / 'archive.tar.gz'
            with self.assertRaisesRegex(discovery.DownloadError, 'download_timeout'):
                client.download('https://github.com/button-box/button-box/releases/download/v0.3.0/archive.tar.gz',
                                target, 2 * 1024 * 1024)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix('.gz.part').exists())
            self.assertEqual(opener.call_args.kwargs['timeout'], 30)
        dropin = (INSTALL.parents[1] / 'config/update-discovery-voice-ready.conf').read_text()
        self.assertIn('TimeoutStartSec=1800', dropin)
        self.assertEqual(discovery.DOWNLOAD_SECONDS, 1500)
        bootstrap = (INSTALL / 'update-bootstrap.sh').read_text()
        self.assertIn('config/update-discovery-voice-ready.conf', bootstrap)
        self.assertIn('/etc/systemd/system/messagebox-update-discovery.service.d/voice-ready.conf', bootstrap)

    def test_etag_304_and_rate_limit_are_persisted_across_boot_and_hints(self):
        with tempfile.TemporaryDirectory() as directory:
            class Response(io.BytesIO):
                headers = {'ETag': 'synthetic-etag'}
            calls = []
            def opener(request, **kwargs):
                calls.append(dict(request.header_items()))
                if len(calls) == 1:
                    return Response(b'{"synthetic":true}')
                raise urllib.error.HTTPError(request.full_url, 304, 'unchanged',
                    {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '2000'}, None)
            client = discovery.Discovery(directory, opener=opener, clock=lambda: 1000)
            self.assertEqual(client.json(discovery.API + '/latest'), {'synthetic': True})
            self.assertEqual(client.json(discovery.API + '/latest'), {'synthetic': True})
            self.assertIn('If-none-match', calls[1])
            rebooted = discovery.Discovery(directory, opener=mock.Mock(), clock=lambda: 1001)
            self.assertIsNone(rebooted.poll('stable', 0, PLATFORM, hint=True))
            rebooted.opener.assert_not_called()
            self.assertGreater(rebooted.state['backoff_until'], 2000)

    def test_network_loss_never_promotes_partial_and_cleans_abandoned_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            class Broken(io.BytesIO):
                def read(self, *args):
                    raise OSError('synthetic loss')
            client = discovery.Discovery(directory, opener=lambda *args, **kwargs: Broken())
            target = Path(directory) / 'archive.tar.gz'
            with self.assertRaises(OSError):
                client.download('https://github.com/button-box/button-box/releases/download/v0.3.0/archive.tar.gz', target, 10)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).glob('*.part')), [])
            self.assertFalse(discovery.allowed_url('https://example.org/archive'))
            self.assertFalse(discovery.allowed_url('https://github.com/another/repo/releases/download/a/b'))

    def test_canary_selection_uses_signed_sequence_and_bounded_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            client = discovery.Discovery(directory, clock=lambda: 1700000001, jitter=lambda *args: 0)
            offers = [{'tag_name': 'v0.3.9-canary.1'}, {'tag_name': 'v0.3.1'}, {'tag_name': 'v0.3.2-canary.1'}]
            values = [manifest('v0.3.9-canary.1', 5), manifest('v0.3.1', 8), manifest('v0.3.2-canary.1', 6)]
            notified = []
            with mock.patch.object(client, 'json', return_value=offers), mock.patch.object(client, 'candidate', side_effect=values), mock.patch.object(client, 'complete'):
                selected = client.poll('canary', 0, PLATFORM, notify=notified.append)
            self.assertEqual(selected['release_sequence'], 8)
            self.assertEqual(notified, ['v0.3.1'])
            self.assertIsNone(client.poll('canary', 0, PLATFORM, hint=True))


if __name__ == '__main__':
    unittest.main()


class SupportedModelTests(unittest.TestCase):
    def test_pi3_a_plus_revision_normalizes_to_supported_family(self):
        import tempfile
        from pathlib import Path
        from release_verify import SUPPORTED_MODELS, local_platform
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'etc').mkdir()
            (root / 'proc/device-tree').mkdir(parents=True)
            (root / 'etc/os-release').write_text('ID=debian\nVERSION_CODENAME=trixie\n')
            (root / 'etc/rpi-issue').write_text('')
            (root / 'proc/device-tree/model').write_text('Raspberry Pi 3 Model A Plus Rev 1.1\x00')
            model = local_platform(root)['model']
        self.assertEqual(model, 'Raspberry Pi 3 Model A Plus')
        self.assertIn(model, SUPPORTED_MODELS)
