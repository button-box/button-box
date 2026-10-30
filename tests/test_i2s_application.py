"""I2S application admission at the service and real transaction boundaries."""

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from field_updater import FieldBox, load_frozen, write
from test_audio_config import audio_config
from test_bounded_update import FakeTime, bounded_update as current, sha256
import test_update_transaction as transactions
from test_update_discovery import verify

ROOT = Path(__file__).resolve().parents[1]
CAPABILITY = ROOT / 'messagebox/mic_capture.py'


class I2SApplicationTests(unittest.TestCase):
    def execute_guard(self, root, audio_values=None):
        # Execute the installed shell guard. Only the fixed filesystem paths and
        # Python interpreter are remapped; no app code or audio tool is started.
        guard = root / 'etc/systemd/system/messagebox-button.service.d/zz-i2s-microphone.conf'
        line = next(line.removeprefix('ExecStartPre=') for line in guard.read_text().splitlines()
                    if line.startswith('ExecStartPre='))
        command = shlex.split(line.replace('$$', '$'))
        runner = self.base / 'check-installed.py'
        runner.write_text('''import importlib.util,os,sys
from pathlib import Path
root=Path(sys.argv[1])
spec=importlib.util.spec_from_file_location('installed_audio',root/'usr/lib/messagebox/audio_config.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.I2S_CAPABILITY=root/os.readlink(root/'opt/messagebox').lstrip('/')/'messagebox/mic_capture.py'
raise SystemExit(module.main(sys.argv[2:]))
''')
        command[-1] = command[-1].replace('/usr/bin/python3 /usr/lib/messagebox/audio_config.py',
                                         shlex.join([sys.executable, str(runner), str(root)]))
        environment = os.environ.copy()
        environment.update(audio_config.read_settings(root / 'etc/messagebox/env'))
        environment.update(audio_values or {})
        return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)

    def prepare(self, worker):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        if worker is current:
            fixture, old = transactions.TransactionTests.prepare(self, self.base)
            host_run, states = fixture.systemctl, fixture.systemctl.states
        else:
            fixture = FieldBox(self.base)
            fixture.add('messagebox/mic_capture.py', CAPABILITY.read_bytes())
            fixture.manifest_data = fixture.data
            fixture.write_manifest = fixture.save_manifest
            old = fixture.old
            host_run, states = fixture.run, fixture.states
        self.fixture, self.old = fixture, old
        old_release = current._rooted(fixture.root, old)
        for name in ('mic_capture.py', 'button_send.py'):
            write(old_release / 'messagebox' / name, (ROOT / 'messagebox' / name).read_bytes())
        write(fixture.root / 'usr/lib/messagebox/audio_config.py', (ROOT / 'scripts/install/audio_config.py').read_bytes())
        env = write(fixture.root / 'etc/messagebox/env',
                    b'MSGBOX_MIC_TYPE=usb\nMSGBOX_NFC_RESET_PIN=D4\nMSGBOX_NFC_REQUEST_PIN=D27\n', 0o640)
        boot = write(fixture.root / 'boot/firmware/config.txt', b'[all]\n')
        self.guard = fixture.root / 'etc/systemd/system/messagebox-button.service.d/zz-i2s-microphone.conf'
        audio_config.set_mic_type('i2s', env_path=env, boot_config=boot,
                                  alsa_config=fixture.root / 'etc/alsa/conf.d/60-button-box-i2s-mic.conf',
                                  guard_path=self.guard, capability_file=old_release / 'messagebox/mic_capture.py')
        states['messagebox-button.service']['active'] = 'active'
        self.started = []
        self.denied = []

        def run(command, **options):
            if command[:3] == ['systemctl', '--job-mode=ignore-dependencies', 'start'] and command[3] == 'messagebox-button.service':
                result = self.execute_guard(fixture.root)
                if result.returncode:
                    self.denied.append(worker.controlled_release(fixture.root))
                    states['messagebox-button.service']['active'] = 'failed'
                    return subprocess.CompletedProcess(command, result.returncode, '', '')
                self.started.append(worker.controlled_release(fixture.root))
            return host_run(command, **options)
        self.run = run
        return fixture

    def install(self, worker, health):
        fixture = self.fixture
        with mock.patch.dict(sys.modules, {'release_verify': verify}):
            return worker.install_transaction(fixture.source, fixture.manifest, root=fixture.root,
                                              run=self.run, sequence=1, health=health)

    def test_enabling_requires_an_i2s_capable_first_application_before_any_audio_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = write(root / 'env', b'MSGBOX_NFC_RESET_PIN=D4\nMSGBOX_NFC_REQUEST_PIN=D27\n')
            boot = write(root / 'config.txt', b'[all]\n')
            with mock.patch.object(audio_config, 'write_atomic', side_effect=AssertionError('mutation')), \
                 self.assertRaisesRegex(audio_config.AudioConfigError, 'lacks I2S recording support'):
                audio_config.set_mic_type('i2s', env_path=env, boot_config=boot,
                                          capability_file=root / 'missing.py')

    def test_current_helper_refuses_missing_invalid_and_future_capabilities_without_executing_them(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'capabilities.py'
            for value in ('', 'I2S_CAPTURE_VERSION = True', 'I2S_CAPTURE_VERSION = 2',
                          'I2S_CAPTURE_VERSION = 1\nI2S_CAPTURE_VERSION = 1',
                          'raise RuntimeError("must not execute")'):
                with self.subTest(value=value):
                    marker.write_text(value)
                    with self.assertRaises(audio_config.AudioConfigError):
                        audio_config.check_i2s_application(marker)
            marker.write_bytes(CAPABILITY.read_bytes())
            audio_config.check_i2s_application(marker)

    def test_legacy_payload_cannot_start_and_transaction_restores_compatible_application(self):
        legacy_helper = subprocess.check_output(['git', '-C', str(ROOT), 'show',
                                                 'v0.3.12:scripts/install/audio_config.py'])
        legacy_button = subprocess.check_output(['git', '-C', str(ROOT), 'show',
                                                 'v0.3.12:messagebox/button_send.py'])
        legacy_unit = subprocess.check_output(['git', '-C', str(ROOT), 'show',
                                               'v0.3.12:systemd/messagebox-button.service'])
        for worker in (current, load_frozen('v0.3.12', 'scripts/install/bounded_update.py')):
            with self.subTest(worker=worker.__name__), \
                 mock.patch.object(current, 'time', FakeTime()), \
                 mock.patch.object(current, '_boot', return_value='synthetic-boot'), \
                 mock.patch.object(worker, 'time', FakeTime()), \
                 mock.patch.object(worker, '_boot', return_value='synthetic-boot'):
                fixture = self.prepare(worker)
                # The immutable candidate directory really lacks the marker;
                # the external helper and raw-only legacy recorder are real bytes.
                write(fixture.source / 'messagebox/mic_capture.py', subprocess.check_output(
                    ['git', '-C', str(ROOT), 'show', 'v0.3.12:messagebox/mic_capture.py']))
                write(fixture.source / 'scripts/install/audio_config.py', legacy_helper)
                write(fixture.source / 'messagebox/button_send.py', legacy_button)
                write(fixture.source / 'systemd/messagebox-button.service', legacy_unit)
                for source, target in (('messagebox/button_send.py', '/opt/messagebox/messagebox/button_send.py'),
                                       ('systemd/messagebox-button.service', '/etc/systemd/system/messagebox-button.service')):
                    if not any(entry['source'] == source for entry in fixture.manifest_data['files']):
                        fixture.manifest_data['files'].append({'source': source, 'installed': target})
                for entry in fixture.manifest_data['files']:
                    entry['sha256'] = sha256(fixture.source / entry['source'])
                fixture.write_manifest()
                guard_bytes = self.guard.read_bytes()
                def health(*args):
                    self.assertTrue(self.denied)
                    raise worker.UpdateError('health_timeout')
                with self.assertRaises(worker.UpdateError) as failure:
                    self.install(worker, health)
                self.assertTrue(self.denied, str(failure.exception))
                self.assertEqual(worker.controlled_release(fixture.root), self.old)
                self.assertEqual(self.execute_guard(fixture.root).returncode, 0)
                self.assertEqual(self.guard.read_bytes(), guard_bytes)
                self.assertTrue(all(release == self.old for release in self.started))
                self.assertEqual(worker.read_state(fixture.root, 'result.json')['state'], 'rolled_back')

    def test_guard_is_loaded_after_release_owned_pre_start_resets(self):
        with mock.patch.object(current, 'time', FakeTime()), \
             mock.patch.object(current, '_boot', return_value='synthetic-boot'):
            fixture = self.prepare(current)
            ordinary = write(self.guard.parent / 'messagebox.conf', b'[Service]\nExecStartPre=\n')
            self.assertEqual(current._expected_target('systemd/messagebox-button.service.d/messagebox.conf'),
                             '/etc/systemd/system/messagebox-button.service.d/messagebox.conf')
            self.assertIsNone(current._expected_target('systemd/messagebox-button.service.d/' + self.guard.name))
            self.assertEqual(sorted((self.guard, ordinary))[-1], self.guard)
            self.assertEqual(self.execute_guard(fixture.root).returncode, 0)

    def test_compatible_failed_trial_rolls_back_to_compatible_first_install_and_keeps_guard(self):
        with mock.patch.object(current, 'time', FakeTime()), \
             mock.patch.object(current, '_boot', return_value='synthetic-boot'):
            fixture = self.prepare(current)
            def health(*args):
                self.assertTrue(self.started)
                self.assertEqual(self.execute_guard(fixture.root).returncode, 0)
                raise current.UpdateError('health_timeout')
            with self.assertRaisesRegex(current.UpdateError, 'health_timeout'):
                self.install(current, health)
            self.assertEqual(current.controlled_release(fixture.root), self.old)
            self.assertEqual(self.execute_guard(fixture.root).returncode, 0)
            self.assertEqual(current.read_state(fixture.root, 'result.json')['state'], 'rolled_back')

    def test_legacy_rollback_target_is_blocked_and_usb_bypasses_i2s_guard(self):
        with mock.patch.object(current, 'time', FakeTime()), \
             mock.patch.object(current, '_boot', return_value='synthetic-boot'):
            fixture = self.prepare(current)
            marker = current._rooted(fixture.root, self.old) / 'messagebox/mic_capture.py'
            marker.write_bytes(subprocess.check_output(['git', '-C', str(ROOT), 'show',
                                                        'v0.3.12:messagebox/mic_capture.py']))
            # Model an invalid restore target introduced outside the supported
            # first-install path. Recovery must stop rather than run raw capture.
            def health(*args):
                raise current.UpdateError('health_timeout')
            with self.assertRaisesRegex(current.UpdateError, 'rollback_failed'):
                self.install(current, health)
            self.assertIn(self.old, self.denied)
            self.assertNotEqual(self.execute_guard(fixture.root).returncode, 0)
            self.assertEqual(current.read_state(fixture.root, 'result.json')['state'], 'rollback_failed')
            audio_config.set_env_value(fixture.root / 'etc/messagebox/env', 'MSGBOX_MIC_TYPE', 'usb')
            self.assertEqual(self.execute_guard(fixture.root).returncode, 0)

    def test_raw_i2s_without_processed_pcm_is_blocked_even_during_interrupted_usb_switch(self):
        with mock.patch.object(current, 'time', FakeTime()), \
             mock.patch.object(current, '_boot', return_value='synthetic-boot'):
            fixture = self.prepare(current)
            raw = {'MSGBOX_MIC_CARD': audio_config.I2S_CARD, 'MSGBOX_MIC_DEV': audio_config.I2S_DEVICE,
                   'MSGBOX_MIC_PCM': ''}
            self.assertNotEqual(self.execute_guard(fixture.root, raw).returncode, 0)
            self.assertEqual(self.execute_guard(fixture.root, {**raw, 'MSGBOX_MIC_PCM': audio_config.I2S_PCM}).returncode, 0)
            audio_config.set_env_value(fixture.root / 'etc/messagebox/env', 'MSGBOX_MIC_TYPE', 'usb')
            self.assertNotEqual(self.execute_guard(fixture.root, raw).returncode, 0)
            self.assertEqual(self.execute_guard(fixture.root, {'MSGBOX_MIC_CARD': 'mic'}).returncode, 0)
