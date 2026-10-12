"""Transactions at realistic filesystem/service crash boundaries, without a Pi."""

import copy
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_bounded_update import Fixture, FakeTime, bounded_update as update


class PowerLoss(BaseException):
    pass


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeTime()
        self.timer = mock.patch.object(update, 'time', self.clock)
        self.timer.start()
        self.addCleanup(self.timer.stop)
        boot = mock.patch.object(update, "_boot", return_value="synthetic-boot")
        boot.start()
        self.addCleanup(boot.stop)

    def prepare(self, directory):
        fixture = Fixture(directory)
        fixture.metadata.write_text(json.dumps({'version': 'v0.2.0', 'commit': 'b' * 40}))
        # Routine updates freeze dependencies and independent recovery units.
        for entry in fixture.manifest_data['files']:
            if Path(entry['installed']).name in update.FIXED_UNITS or entry['installed'].endswith('requirements-nfc.txt'):
                target = update._rooted(fixture.root, entry['installed'])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((fixture.source / entry['source']).read_bytes())
        import shutil
        for name in ("bounded_update", "release_verify", "update_controller", "update_discovery"):
            shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/install" / (name + ".py"),
                            fixture.source / "scripts/install" / (name + ".py"))
        old = update.bootstrap_layout(root=fixture.root, run=fixture.systemctl)
        fixture.manifest_data.update(version='v0.3.0-canary.1')
        fixture.write_manifest()
        return fixture, old

    def install(self, fixture, sequence=1, health=lambda *args: None):
        return update.install_transaction(fixture.source, fixture.manifest, root=fixture.root,
                                         run=fixture.systemctl, operation_id='synthetic-operation', sequence=sequence, health=health)

    def test_success_retains_current_previous_and_removes_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            self.install(fixture)
            self.assertEqual(update.controlled_release(fixture.root), update.RELEASES + '/v0.3.0-canary.1-' + 'a' * 40)
            self.assertEqual(update.read_state(fixture.root, 'known-good.json')['previous'], old)
            fixture.manifest_data.update(version='v0.3.0', commit='c' * 40)
            fixture.write_manifest()
            self.install(fixture, 2)
            children = list((fixture.root / 'opt/messagebox-releases').iterdir())
            self.assertEqual(len(children), 2)
            self.assertFalse(update._rooted(fixture.root, old).exists())
            self.assertEqual(list((fixture.root / 'var/lib/button-box-update/snapshots').iterdir()), [])
            self.assertEqual(update.read_state(fixture.root, 'sequence.json'), 2)
            fixture.assert_private_unchanged(self)

    def test_activators_stop_before_services_and_stay_suspended_through_health(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, _old = self.prepare(directory)
            path = 'messagebox-mode-reconcile.path'
            timer = 'messagebox-wifi-watchdog.timer'
            fixture.systemctl.states[path]['active'] = 'active'
            fixture.systemctl.states[timer]['active'] = 'active'
            fixture.systemctl.commands.clear()
            def health(root, journal, run):
                self.assertTrue(journal['activators_suspended'])
                self.assertEqual(fixture.systemctl.states[path]['active'], 'inactive')
                self.assertEqual(fixture.systemctl.states[timer]['active'], 'inactive')
                stops = [command for command in fixture.systemctl.commands if command[:2] == ['systemctl', 'stop']]
                self.assertEqual(set(stops[0][2:]), {path, timer})
                self.assertTrue(update._rooted(root, update.MODE_LOCK).exists())
            self.install(fixture, health=health)
            self.assertEqual(fixture.systemctl.states[path]['active'], 'active')
            self.assertEqual(fixture.systemctl.states[timer]['active'], 'active')

    def test_health_failure_restores_identity_external_files_units_and_quarantines(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            states = copy.deepcopy(fixture.systemctl.states)
            def unhealthy(*args):
                raise update.UpdateError('health_timeout')
            with self.assertRaisesRegex(update.UpdateError, 'health_timeout'):
                self.install(fixture, health=unhealthy)
            self.assertEqual(update.controlled_release(fixture.root), old)
            self.assertEqual(fixture.systemctl.states, states)
            self.assertEqual(update.read_state(fixture.root, 'result.json')['state'], 'rolled_back')
            self.assertEqual(update.read_state(fixture.root, 'sequence.json'), 1)
            self.assertTrue(update.read_state(fixture.root, 'quarantine.json'))
            self.assertFalse(update._rooted(fixture.root, update.INHIBIT).exists())
            fixture.assert_private_unchanged(self)
            with self.assertRaisesRegex(update.UpdateError, 'quarantined'):
                self.install(fixture, 2)
            update.recover_transaction(root=fixture.root, run=fixture.systemctl)
            self.assertEqual(update.controlled_release(fixture.root), old)

    def test_each_persisted_transaction_state_recovers_after_power_loss(self):
        original = update._journal
        for phase in ('prepared', 'quiesced', 'switched', 'probation', 'restoring_activators'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                fixture, old = self.prepare(directory)
                def power_cut(root, journal, observed):
                    original(root, journal, observed)
                    if observed == phase:
                        raise PowerLoss()
                with mock.patch.object(update, '_journal', side_effect=power_cut), self.assertRaises(PowerLoss):
                    self.install(fixture)
                # Boot recovery does not start dependent units while its own
                # required oneshot is still activating.
                fixture.systemctl.commands.clear()
                update.recover_transaction(root=fixture.root, run=fixture.systemctl, start=False)
                self.assertEqual(update.controlled_release(fixture.root), old)
                self.assertFalse(any('start' in command for command in fixture.systemctl.commands))
                self.assertEqual(update.read_state(fixture.root, 'journal.json')['phase'], 'rolled_back')
                fixture.assert_private_unchanged(self)

    def test_power_loss_after_an_integration_write_restores_the_recorded_external_file(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            destination = fixture.root / 'usr/lib/messagebox/audio_config.py'
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text('synthetic previous integration')
            original = update._atomic_install
            def write_then_cut(source, target, mode, **options):
                original(source, target, mode, **options)
                if target == destination:
                    raise PowerLoss()
            with mock.patch.object(update, '_atomic_install', side_effect=write_then_cut), self.assertRaises(PowerLoss):
                self.install(fixture)
            self.assertNotEqual(destination.read_text(), 'synthetic previous integration')
            update.recover_transaction(root=fixture.root, run=fixture.systemctl, start=False)
            self.assertEqual(destination.read_text(), 'synthetic previous integration')
            self.assertEqual(update.controlled_release(fixture.root), old)

    def test_power_loss_during_rollback_resumes_and_failure_keeps_inhibit(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            def power_cut(*args):
                raise PowerLoss()
            with self.assertRaises(PowerLoss):
                self.install(fixture, health=power_cut)
            original = update._external_restore
            with mock.patch.object(update, '_external_restore', side_effect=PowerLoss), self.assertRaises(PowerLoss):
                update.recover_transaction(root=fixture.root, run=fixture.systemctl)
            self.assertTrue(update._rooted(fixture.root, update.INHIBIT).exists())
            with mock.patch.object(update, '_external_restore', side_effect=OSError), self.assertRaisesRegex(update.UpdateError, 'rollback_failed'):
                update.recover_transaction(root=fixture.root, run=fixture.systemctl)
            self.assertEqual(update.read_state(fixture.root, 'result.json')['state'], 'rollback_failed')
            with mock.patch.object(update, '_external_restore', side_effect=original):
                update.recover_transaction(root=fixture.root, run=fixture.systemctl)
            self.assertEqual(update.controlled_release(fixture.root), old)
            self.assertEqual(update.read_state(fixture.root, 'result.json')['state'], 'rolled_back')

    def test_exclusive_reservation_defers_while_audio_or_press_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            lock = update._rooted(fixture.root, update.MAINTENANCE_LOCK)
            with lock.open('rb') as interaction:
                fcntl.flock(interaction, fcntl.LOCK_SH)
                with self.assertRaisesRegex(update.UpdateError, 'not_idle'):
                    self.install(fixture)
            self.assertEqual(update.controlled_release(fixture.root), old)
            self.assertIsNone(update.read_state(fixture.root, 'sequence.json'))
            with self.assertRaisesRegex(update.UpdateError, 'converted'):
                update.apply(fixture.source, fixture.manifest, fixture.backup, root=fixture.root, run=fixture.systemctl)

    def test_controlled_pointer_rejects_escape_and_low_disk_never_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            fixture.systemctl.commands.clear()
            with mock.patch.object(update.shutil, 'disk_usage', return_value=mock.Mock(free=0)):
                with self.assertRaisesRegex(update.UpdateError, 'low_disk'):
                    self.install(fixture)
            self.assertFalse(any('stop' in command for command in fixture.systemctl.commands))
            pointer = fixture.root / 'opt/messagebox'
            pointer.unlink()
            pointer.symlink_to('/tmp/untrusted')
            with self.assertRaisesRegex(update.UpdateError, 'unsafe application pointer'):
                update.controlled_release(fixture.root)

    def test_health_needs_sustained_success_and_counts_crash_loop_and_timeout(self):
        journal = {'deadline_mono': 300}
        samples = []
        def sample(*args):
            samples.append(self.clock.now)
            return self.clock.now >= 10, {'button': ('1', '0')}
        update.sustain_health(None, journal, sample=sample, monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.assertGreaterEqual(self.clock.now, 70)
        self.clock.now = 0
        def crash(*args):
            return True, {'button': ('1', str(int(self.clock.now)))}
        with self.assertRaisesRegex(update.UpdateError, 'crash_loop'):
            update.sustain_health(None, journal, sample=crash, monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.clock.now = 0
        with self.assertRaisesRegex(update.UpdateError, 'health_timeout'):
            update.sustain_health(None, {'deadline_mono': 20}, sample=lambda *args: (False, {}),
                                  monotonic=self.clock.monotonic, sleep=self.clock.sleep)

    def test_health_requires_transaction_heartbeat_gpio_pid_and_alsa_access_without_capture(self):
        import hashlib
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            new = update.RELEASES + '/v0.3.0-' + 'a' * 40
            release = update._rooted(root, new)
            release.mkdir(parents=True)
            payload = release / 'sample.py'
            payload.write_text('synthetic code')
            update.switch_release(root, new)
            journal = {'transaction': 'synthetic-transaction', 'boot': 'synthetic-boot', 'version': 'v0.3.0',
                       'commit': 'a' * 40, 'started_mono': 0, 'new': new,
                       'units': {unit: {'active': 'active'} for unit in ('messagebox-button.service', 'messagebox-poller.service')},
                       'hashes': [{'relative': 'sample.py', 'sha256': hashlib.sha256(payload.read_bytes()).hexdigest()}]}
            audio = root / 'run/messagebox-audio/audio.env'
            audio.parent.mkdir(parents=True)
            audio.write_text('MSGBOX_MIC_DEV=plughw:CARD=mic,DEV=0\nMSGBOX_SPK_DEV=plughw:CARD=Device,DEV=0\n')
            evidence = {'transaction': journal['transaction'], 'boot': journal['boot'], 'software_version': journal['version'],
                        'software_commit': journal['commit'], 'monotonic': 0, 'pid': 100, 'ready': True}
            for name in ('button', 'heartbeat'):
                update.durable_json(root / 'run/messagebox-update' / (name + '.json'), evidence, 0o644)
            commands = []
            denied = False
            def run(command, **options):
                commands.append(command)
                if command[0] == 'systemctl':
                    pid = 100 if command[2] == 'messagebox-button.service' else 101
                    return subprocess.CompletedProcess(command, 0, f'ActiveState=active\nMainPID={pid}\nNRestarts=0\n')
                if command[-1] == '-l':
                    card = '2: mic' if command[-2] == 'arecord' else '3: Device'
                    return subprocess.CompletedProcess(command, 0, f'card {card} [USB Audio], device 0: USB Audio\n')
                return subprocess.CompletedProcess(command, int(denied), '')
            self.clock.now = 60
            self.assertTrue(update.health_sample(root, journal, run)[0])
            # GPIO readiness is bound to the acquired current PID; it need not
            # expire during legitimate long playback. Heartbeat remains fresh.
            self.clock.now = 91
            self.assertFalse(update.health_sample(root, journal, run)[0])
            update.durable_json(root / 'run/messagebox-update/heartbeat.json', {**evidence, 'monotonic': 91}, 0o644)
            self.assertTrue(update.health_sample(root, journal, run)[0])
            for mutation in ({'transaction': 'old'}, {'boot': 'previous-boot'}, {'software_version': 'v0.2.0'}):
                update.durable_json(root / 'run/messagebox-update/heartbeat.json', {**evidence, 'monotonic': 91, **mutation}, 0o644)
                self.assertFalse(update.health_sample(root, journal, run)[0])
            update.durable_json(root / 'run/messagebox-update/heartbeat.json', {**evidence, 'monotonic': 91}, 0o644)
            denied = True
            self.assertFalse(update.health_sample(root, journal, run)[0])
            capture_commands = [command for command in commands if 'arecord' in command]
            self.assertTrue(capture_commands)
            self.assertTrue(all(command == ['runuser', '-u', 'messagebox', '--', 'arecord', '-l'] for command in capture_commands))

    def test_supervisor_reports_death_during_preflight_without_claiming_rollback(self):
        import hashlib
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            operation_id = 'synthetic-preflight'
            name = 'operations/' + hashlib.sha256(operation_id.encode()).hexdigest() + '.json'
            update.save_state(fixture.root, name, {'operation_id': operation_id, 'state': 'installing'})
            update.save_state(fixture.root, 'ticket.json', {'operation_id': operation_id, 'version': 'v0.3.0',
                              'boot': 'synthetic-boot', 'queued_mono': 0})
            self.clock.now = 40
            update.supervise_transaction(root=fixture.root, run=lambda command, **options: subprocess.CompletedProcess(command, 3, 'inactive\n'))
            self.assertEqual(update.read_state(fixture.root, 'result.json')['state'], 'failed')
            self.assertEqual(update.read_state(fixture.root, 'result.json')['reason'], 'updater_died')
            self.assertEqual(update.controlled_release(fixture.root), old)

    def test_independent_supervisor_recovers_dead_worker_and_restart_counter_jump(self):
        import subprocess
        for failure in ('updater_died', 'crash_loop'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                fixture, old = self.prepare(directory)
                with self.assertRaises(PowerLoss):
                    self.install(fixture, health=lambda *args: (_ for _ in ()).throw(PowerLoss()))
                journal = update.read_state(fixture.root, 'journal.json')
                journal['process_token'] = 'synthetic-token'
                update.save_state(fixture.root, 'journal.json', journal)
                def run(command, **options):
                    if command == ['systemctl', 'stop', 'messagebox-update-install.service']:
                        return subprocess.CompletedProcess(command, 0)
                    return fixture.systemctl(command, **options)
                if failure == 'updater_died':
                    with mock.patch.object(update, '_process_token', return_value=None):
                        update.supervise_transaction(root=fixture.root, run=run)
                else:
                    with mock.patch.object(update, '_process_token', return_value='synthetic-token'), \
                         mock.patch.object(update, 'health_sample', side_effect=[(True, {'button': ('1', '0')}), (True, {'button': ('5', '3')})]):
                        update.supervise_transaction(root=fixture.root, run=run)
                        update.supervise_transaction(root=fixture.root, run=run)
                self.assertEqual(update.controlled_release(fixture.root), old)
                self.assertEqual(update.read_state(fixture.root, 'result.json')['reason'], failure)

    def test_bootstrap_power_loss_and_nfc_dependency_move_recovers_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.metadata.write_text(json.dumps({'version': 'v0.2.0', 'commit': 'b' * 40}))
            nfc = fixture.root / 'opt/messagebox/venv-nfc'
            nfc.mkdir()
            (nfc / 'test-dependency').write_text('unchanged dependency')
            with mock.patch.object(update, 'switch_release', side_effect=PowerLoss), self.assertRaises(PowerLoss):
                update.bootstrap_layout(root=fixture.root, run=fixture.systemctl)
            update.recover_transaction(root=fixture.root, run=fixture.systemctl, start=False)
            self.assertFalse((fixture.root / 'opt/messagebox').is_symlink())
            self.assertEqual(os.readlink(nfc), update.STABLE_NFC)
            self.assertEqual(update._rooted(fixture.root, update.STABLE_NFC).joinpath('test-dependency').read_text(), 'unchanged dependency')


if __name__ == '__main__':
    unittest.main()
