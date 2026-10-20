"""Read-only diagnostics share the worker's health contract, including failures."""

import contextlib
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_bounded_update import bounded_update as update

SPEC = importlib.util.spec_from_file_location('health_probe', Path(__file__).resolve().parents[1] / 'scripts/install/health-probe.py')
probe = importlib.util.module_from_spec(SPEC)
with mock.patch.dict(sys.modules, {'bounded_update': update}):
    SPEC.loader.exec_module(probe)


class HealthProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.new = update.RELEASES + '/v0.3.0-' + 'a' * 40
        release = update._rooted(self.root, self.new)
        release.mkdir(parents=True)
        payload = release / 'sample.py'
        payload.write_text('synthetic program')
        (release / 'release.json').write_text(json.dumps({'version': 'v0.3.0', 'commit': 'a' * 40}))
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        (release / 'release-manifest.json').write_text(json.dumps({'files': [
            {'installed': '/opt/messagebox/sample.py', 'sha256': digest}]}))
        update.switch_release(self.root, self.new)
        self.journal = {'transaction': 'test-transaction', 'boot': 'test-boot', 'version': 'v0.3.0',
                        'commit': 'a' * 40, 'started_mono': 10, 'new': self.new,
                        'kind': 'application', 'units': {unit: {'active': 'active'} for unit in (
                            'messagebox-button.service', 'messagebox-poller.service')},
                        'hashes': [{'relative': 'sample.py', 'sha256': digest}]}
        self.evidence = {'transaction': 'test-transaction', 'boot': 'test-boot', 'software_version': 'v0.3.0',
                         'software_commit': 'a' * 40, 'monotonic': 20, 'pid': 100, 'ready': True}
        for name in ('heartbeat', 'button'):
            self.write_evidence(name, self.evidence)
        audio = self.root / 'run/messagebox-audio/audio.env'
        audio.parent.mkdir(parents=True)
        audio.write_text('MSGBOX_MIC_DEV=plughw:CARD=mic,DEV=0\nMSGBOX_SPK_DEV=plughw:CARD=speaker,DEV=0\n')
        self.microphone_card = 'mic'
        update.save_state(self.root, 'journal.json', self.journal)
        self.commands = []
        self.timer = mock.patch.object(update.time, 'monotonic', return_value=30)
        self.timer.start()
        self.addCleanup(self.timer.stop)

    def write_evidence(self, name, value):
        update.durable_json(self.root / 'run/messagebox-update' / (name + '.json'), value)

    def system_read(self, command, **options):
        self.commands.append(command)
        if command[0] == 'systemctl':
            assert command[1] == 'show', command
            return subprocess.CompletedProcess(command, 0, 'ActiveState=active\nMainPID=100\nNRestarts=0\n')
        assert command[:4] == ['runuser', '-u', 'messagebox', '--'], command
        if command[4] in {'arecord', 'aplay'}:
            assert command[5:] == ['-l'], command
            card = self.microphone_card if command[4] == 'arecord' else 'speaker'
            return subprocess.CompletedProcess(command, 0, f'card 1: {card} [Synthetic audio], device 0: synthetic\n')
        assert command[4] == 'test' and command[5] in {'-r', '-w'}, command
        return subprocess.CompletedProcess(command, 0, '')

    def test_probe_journal_and_current_leave_files_unchanged_and_never_capture(self):
        def snapshot():
            return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
                    for path in self.root.rglob('*') if path.is_file()}
        before = snapshot()
        for arguments in ([], ['--journal', str(self.root / 'var/lib/button-box-update/journal.json')], ['--current']):
            with self.subTest(arguments=arguments), contextlib.redirect_stdout(io.StringIO()) as output, \
                 mock.patch.object(probe, 'update', update), \
                 mock.patch.object(update, '_boot', return_value='test-boot'), \
                 mock.patch.object(update, 'save_state', side_effect=AssertionError('write forbidden')), \
                 mock.patch.object(update, 'durable_json', side_effect=AssertionError('write forbidden')):
                self.assertEqual(probe.main(arguments, root=self.root, run=self.system_read), 0)
                self.assertIn('PASS evidence:heartbeat:fresh', output.getvalue())
                self.assertIn('PASS audio:MSGBOX_MIC_DEV:enumerated', output.getvalue())
                self.assertIn('PASS hash:sample.py', output.getvalue())
        self.assertEqual(snapshot(), before)
        self.assertTrue(self.commands)

    def test_i2s_raw_card_passes_current_and_frozen_updater_probes_without_capture(self):
        self.microphone_card = 'sndrpigooglevoi'
        audio = self.root / 'run/messagebox-audio/audio.env'
        audio.write_text('MSGBOX_MIC_DEV=plughw:CARD=sndrpigooglevoi,DEV=0\n'
                         'MSGBOX_MIC_PCM=buttonbox_mic\n'
                         'MSGBOX_SPK_DEV=plughw:CARD=speaker,DEV=0\n')
        source_root = Path(__file__).resolve().parents[1]
        for version, directory in (
                ('1.3.0', source_root / 'scripts/install'),
                ('1.1.0', source_root / 'tests/fixtures/field-updater/shared/scripts/install')):
            with self.subTest(controller=version):
                spec = importlib.util.spec_from_file_location('i2s_worker', directory / 'bounded_update.py')
                worker = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(worker)
                spec = importlib.util.spec_from_file_location('i2s_probe', directory / 'health-probe.py')
                health = importlib.util.module_from_spec(spec)
                with mock.patch.dict(sys.modules, {'bounded_update': worker}):
                    spec.loader.exec_module(health)
                for arguments in ([], ['--current']):
                    self.commands.clear()
                    with mock.patch.object(worker, '_boot', return_value='test-boot'), \
                         contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(health.main(arguments, root=self.root, run=self.system_read), 0)
                    for name in ('configured', 'enumerated', 'readable', 'writable'):
                        self.assertIn('PASS audio:MSGBOX_MIC_DEV:' + name, output.getvalue())
                    self.assertIn(['runuser', '-u', 'messagebox', '--', 'test', '-r', '/dev/snd/pcmC1D0c'],
                                  self.commands)
                    self.assertEqual([command for command in self.commands if 'arecord' in command],
                                     [['runuser', '-u', 'messagebox', '--', 'arecord', '-l']])

    def test_failed_fields_are_bounded_codes_without_evidence_values(self):
        self.write_evidence('heartbeat', {**self.evidence, 'boot': 'private-value', 'monotonic': -1})
        self.write_evidence('button', {**self.evidence, 'pid': 999, 'ready': False})
        (update._rooted(self.root, self.new) / 'sample.py').write_text('changed')
        checks, _pids = update.health_checks(self.root, self.journal, self.system_read)
        failures = []
        self.assertFalse(update.health_sample(self.root, self.journal, self.system_read, failures=failures)[0])
        for code in ('evidence:heartbeat:boot', 'evidence:heartbeat:monotonic', 'evidence:heartbeat:fresh',
                     'evidence:button:pid', 'evidence:button:ready', 'hash:sample.py'):
            self.assertIn(code, failures)
        self.assertNotIn('private-value', str(checks))
        self.journal['hashes'] *= 100
        failures = []
        update.health_sample(self.root, self.journal, self.system_read, failures=failures)
        self.assertEqual(len(failures), update.HEALTH_FAILURE_LIMIT)
        self.assertTrue(all(len(code) <= update.HEALTH_CODE_LIMIT for code in failures))

    def test_probe_reports_bad_pointer_and_command_failures_as_codes(self):
        (self.root / 'opt/messagebox').unlink()
        with mock.patch.object(probe, 'update', update), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(probe.main([], root=self.root, run=self.system_read), 1)
        self.assertIn('FAIL pointer', output.getvalue())
        def denied(*args, **kwargs):
            raise PermissionError('private output must not be printed')
        checks, _pids = update.health_checks(self.root, self.journal, denied)
        self.assertIn(('unit:messagebox-button.service:unknown', False), checks)
        self.assertIn(('mainpid:messagebox-button.service', False), checks)
        self.assertIn(('audio:MSGBOX_MIC_DEV:probe', False), checks)
        self.assertNotIn('private output', str(checks))
