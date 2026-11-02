"""I2S application admission at the service and real transaction boundaries."""

import contextlib
import errno
import importlib.util
import io
import json
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
        environment.update({"MSGBOX_MIC_CARD": audio_config.I2S_CARD, "MSGBOX_MIC_PCM": audio_config.I2S_PCM}
                           if audio_values is None else audio_values)
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
        write(fixture.root / 'boot/firmware/config.txt', b'[all]\n')
        self.guard = fixture.root / 'etc/systemd/system/messagebox-button.service.d/zz-i2s-microphone.conf'
        audio_config.set_mic_type('i2s', env_path=env,
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

    def test_ota_activates_i2s_or_restores_real_usb_helper_with_current_and_frozen_workers(self):
        legacy = subprocess.check_output(['git', '-C', str(ROOT), 'show',
                                          'v0.3.17:scripts/install/audio_config.py'])
        legacy_marker = subprocess.check_output(['git', '-C', str(ROOT), 'show',
                                                 'v0.3.17:messagebox/mic_capture.py'])
        for worker in (current, load_frozen('v0.3.12', 'scripts/install/bounded_update.py')):
            for scenario in ("cold", "configured", "missing"):
                available = scenario != "missing"
                configured = scenario == "configured"
                with self.subTest(worker=worker.__name__, scenario=scenario), \
                     mock.patch.object(worker, 'time', FakeTime()), \
                     mock.patch.object(worker, '_boot', return_value='synthetic-boot'):
                    fixture = self.prepare(worker)
                    root = fixture.root
                    states = fixture.systemctl.states if worker is current else fixture.states
                    host = fixture.systemctl if worker is current else fixture.run
                    old_release = worker._rooted(root, self.old)
                    write(old_release / 'messagebox/mic_capture.py', legacy_marker)
                    helper = root / 'usr/lib/messagebox/audio_config.py'
                    write(helper, legacy)
                    boot = write(root / 'boot/firmware/config.txt', b'[all]\n')
                    alsa = root / 'etc/alsa/conf.d/60-button-box-i2s-mic.conf'
                    if not configured:
                        alsa.unlink()
                        self.guard.unlink()
                    guards = [self.guard, self.guard.parent.with_name(
                        'messagebox-onboarding-button.service.d') / self.guard.name]
                    guard_before = [(p.stat().st_ino, p.read_bytes()) for p in guards] if configured else []
                    # Candidate helper and capability are installed by the real worker.
                    for source, target in (('scripts/install/audio_config.py', '/usr/lib/messagebox/audio_config.py'),
                                           ('messagebox/mic_capture.py', '/opt/messagebox/messagebox/mic_capture.py')):
                        write(fixture.source / source, (ROOT / source).read_bytes())
                        entry = next((e for e in fixture.manifest_data['files'] if e['source'] == source), None)
                        if entry is None:
                            entry = {'source': source, 'installed': target}
                            fixture.manifest_data['files'].append(entry)
                        entry['sha256'] = sha256(fixture.source / source)
                    fixture.write_manifest()
                    states['messagebox-audio-detect.service']['active'] = 'active'
                    write(root / 'run/messagebox-audio/audio.env', b'MSGBOX_MIC_CARD=mic\nMSGBOX_MIC_DEV=plughw:CARD=mic,DEV=0\n')
                    loads = []

                    def detector():
                        spec = importlib.util.spec_from_file_location('installed_audio_trial', helper)
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        for name, path in (('CONFIG_ENV', 'etc/messagebox/env'),
                                           ('I2S_ALSA_CONFIG', 'etc/alsa/conf.d/60-button-box-i2s-mic.conf'),
                                           ('I2S_GUARD', 'etc/systemd/system/messagebox-button.service.d/zz-i2s-microphone.conf')):
                            setattr(module, name, root / path)
                        module.I2S_CAPABILITY = worker._rooted(root, worker.controlled_release(root)) / 'messagebox/mic_capture.py'
                        loaded = [configured]
                        def detect(**options):
                            if options.get('kind') == 'i2s':
                                if not loaded[0]:
                                    raise module.AudioConfigError('not enumerated')
                                return audio_config.I2S_CARD, audio_config.I2S_DEVICE
                            return 'mic', 'plughw:CARD=mic,DEV=0'
                        def load(command, **options):
                            loads.append(command)
                            if command[0].endswith('dtoverlay'):
                                loaded[0] = available
                        module.detect_microphone = detect
                        module.detect_speaker = lambda: ('speaker', 'plughw:CARD=speaker,DEV=0')
                        if hasattr(module, 'activate_i2s'):
                            module.configured_mic_type = lambda: module.mic_type(module.read_settings(root / 'etc/messagebox/env'))
                            activation = module.activate_i2s
                            module.activate_i2s = lambda: activation(run=load, sleep=worker.time.sleep, monotonic=worker.time.monotonic)
                        real_mkstemp = module.tempfile.mkstemp
                        def no_guard_space(*args, **kwargs):
                            if configured and Path(kwargs['dir']) in [p.parent for p in guards]:
                                raise OSError(errno.ENOSPC, 'guard storage full')
                            return real_mkstemp(*args, **kwargs)
                        with contextlib.redirect_stderr(io.StringIO()), \
                             mock.patch.object(module.tempfile, 'mkstemp', side_effect=no_guard_space):
                            return module.main(['--runtime-output', str(root / 'run/messagebox-audio/audio.env')])

                    def run(command, **options):
                        if command[:3] == ['systemctl', '--job-mode=ignore-dependencies', 'start']:
                            unit = command[3]
                            if unit == 'messagebox-audio-detect.service':
                                result = detector()
                                states[unit]['active'] = 'failed' if result else 'active'
                                if result and options.get('check'):
                                    checks = dict(worker.health_checks(root, worker.read_state(root, 'journal.json'), run)[0])
                                    self.assertFalse(checks['unit:messagebox-audio-detect.service:failed'])
                                    raise subprocess.CalledProcessError(result, command)
                                return subprocess.CompletedProcess(command, result, '', '')
                            if unit == 'messagebox-button.service':
                                values = audio_config.read_settings(root / 'run/messagebox-audio/audio.env')
                                denied = states['messagebox-audio-detect.service']['active'] != 'active'
                                denied = denied or self.execute_guard(root, values).returncode != 0
                                if denied:
                                    states[unit]['active'] = 'failed'
                                    return subprocess.CompletedProcess(command, 1, '', '')
                                # Model the button's hardware idle boundary only after
                                # detection and the actual installed start guard pass.
                                journal = worker.read_state(root, 'journal.json')
                                evidence = {key: journal[key] for key in ('transaction', 'boot')}
                                evidence.update(software_version=journal['version'],
                                                software_commit=journal['commit'], pid=100,
                                                ready=True, monotonic=worker.time.monotonic())
                                write(root / 'run/messagebox-update/button.json', json.dumps(evidence).encode())
                        if command[:2] == ['systemctl', 'show'] and command[3] == '--property=ActiveState,MainPID,NRestarts':
                            state = states[command[2]]['active']
                            return subprocess.CompletedProcess(command, 0, 'ActiveState=' + state + '\nMainPID=100\nNRestarts=0\n')
                        if command[0] == 'runuser':
                            return subprocess.CompletedProcess(command, 1, '', '')
                        return host(command, **options)
                    self.run = run
                    def health(root, journal, run):
                        # Hardware/ALSA are synthetic; evidence validation and
                        # failed-service health use the actual updater checks.
                        checks = dict(worker.health_checks(root, journal, run)[0])
                        self.assertTrue(checks['unit:messagebox-audio-detect.service:active'])
                        if not available:
                            self.assertFalse(checks['audio:MSGBOX_MIC_DEV:enumerated'])
                        if available:
                            self.assertTrue(checks['unit:messagebox-button.service:active'])
                            button = {k: v for k, v in checks.items() if k.startswith('evidence:button:')}
                            self.assertTrue(button)
                            self.assertTrue(all(button.values()), button)
                        if not available:
                            raise worker.UpdateError('health_timeout')
                    if available:
                        self.install(worker, health)
                        self.assertEqual(worker.read_state(root, 'result.json')['state'], 'installed')
                        self.assertIn('MSGBOX_MIC_PCM=buttonbox_mic', (root / 'run/messagebox-audio/audio.env').read_text())
                    else:
                        with self.assertRaisesRegex(worker.UpdateError, 'health_timeout'):
                            self.install(worker, health)
                        self.assertEqual(worker.controlled_release(root), self.old)
                        self.assertEqual(helper.read_bytes(), legacy)
                        self.assertEqual(worker.read_state(root, 'result.json')['state'], 'rolled_back')
                        self.assertEqual(states['messagebox-button.service']['active'], 'active')
                        self.assertNotIn('MSGBOX_MIC_PCM', (root / 'run/messagebox-audio/audio.env').read_text())
                    self.assertEqual(bool(loads), not configured)
                    self.assertEqual(boot.read_bytes(), b'[all]\n')
                    if configured:
                        self.assertEqual([(p.stat().st_ino, p.read_bytes()) for p in guards], guard_before)

    def test_enabling_requires_an_i2s_capable_first_application_before_any_audio_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = write(root / 'env', b'MSGBOX_NFC_RESET_PIN=D4\nMSGBOX_NFC_REQUEST_PIN=D27\n')
            write(root / 'config.txt', b'[all]\n')
            with mock.patch.object(audio_config, 'write_atomic', side_effect=AssertionError('mutation')), \
                 self.assertRaisesRegex(audio_config.AudioConfigError, 'lacks I2S recording support'):
                audio_config.set_mic_type('i2s', env_path=env,
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
            self.assertEqual(self.execute_guard(fixture.root, {"MSGBOX_MIC_CARD": "mic"}).returncode, 0)

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
