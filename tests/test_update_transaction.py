"""Transactions at realistic filesystem/service crash boundaries, without a Pi."""

import copy
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_bounded_update import Fixture, FakeTime, add_sound, sha256, bounded_update as update


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

    def prepare(self, directory, mode='runtime'):
        fixture = Fixture(directory, mode)
        fixture.metadata.write_text(json.dumps({'version': 'v0.2.0', 'commit': 'b' * 40}))
        # Routine updates freeze dependencies and independent recovery units.
        for entry in fixture.manifest_data['files']:
            if Path(entry['installed']).name in update.FIXED_UNITS or entry['installed'].endswith('requirements-nfc.txt'):
                target = update._rooted(fixture.root, entry['installed'])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((fixture.source / entry['source']).read_bytes())
        import shutil
        for name in ("bounded_update", "release_verify", "trust_update", "update_controller", "update_discovery", "health-probe"):
            shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/install" / (name + ".py"),
                            fixture.source / "scripts/install" / (name + ".py"))
        old = update.bootstrap_layout(root=fixture.root, run=fixture.systemctl)
        fixture.manifest_data.update(version='v0.3.0-canary.1')
        fixture.write_manifest()
        return fixture, old

    def install(self, fixture, sequence=1, health=lambda *args: None, **options):
        return update.install_transaction(fixture.source, fixture.manifest, root=fixture.root,
                                         run=fixture.systemctl, operation_id='synthetic-operation', sequence=sequence,
                                         health=health, **options)

    @staticmethod
    def health_run(fixture):
        """Fake systemd show/ALSA for health_checks on top of the fixture systemctl."""
        import subprocess

        def run(command, **options):
            if command[:2] == ['systemctl', 'show'] and command[3].startswith('--property=ActiveState'):
                active = fixture.systemctl.states[command[2]]['active']
                return subprocess.CompletedProcess(command, 0, f'ActiveState={active}\nMainPID=0\nNRestarts=0\n')
            if command[0] == 'runuser':
                return subprocess.CompletedProcess(command, 1, '')
            return fixture.systemctl(command, **options)
        return run

    def test_attended_install_in_setup_mode_keeps_marker_and_uses_setup_health(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory, 'setup')
            # Owner and automatic installs still defer to onboarding.
            with self.assertRaisesRegex(update.UpdateError, 'onboarding_active'):
                self.install(fixture)
            self.assertEqual(update.controlled_release(fixture.root), old)
            observed = {}

            def health(root, journal, run):
                self.assertEqual(journal['mode'], 'setup')
                self.assertTrue(fixture.marker.exists())
                checks, _pids = update.health_checks(root, journal, self.health_run(fixture))
                observed.update(checks)
            self.install(fixture, health=health, allow_setup=True)
            self.assertTrue(fixture.marker.exists())
            self.assertEqual(update.controlled_release(fixture.root), update.RELEASES + '/v0.3.0-canary.1-' + 'a' * 40)
            # No household app runs in setup mode: no heartbeat/button evidence.
            self.assertFalse([name for name in observed if name.startswith('evidence:')])
            self.assertTrue(observed['setup:marker'])
            self.assertTrue(observed['unit:comitup.service:active'])
            self.assertTrue(observed['pointer'])
            self.assertTrue(all(passed for name, passed in observed.items() if not name.startswith('audio:')))

    def test_setup_mode_install_refuses_without_comitup_and_fails_if_mode_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory, 'setup')
            fixture.systemctl.states['comitup.service']['active'] = 'failed'
            with self.assertRaisesRegex(update.UpdateError, 'setup_not_ready'):
                self.install(fixture, allow_setup=True)
            self.assertEqual(update.controlled_release(fixture.root), old)
            self.assertEqual(update.read_state(fixture.root, 'journal.json')['kind'], 'bootstrap')
            fixture.systemctl.states['comitup.service']['active'] = 'active'

            def health(root, journal, run):
                fixture.marker.unlink()
                checks = dict(update.health_checks(root, journal, self.health_run(fixture))[0])
                self.assertFalse(checks['setup:marker'])
                fixture.marker.write_bytes(b'enabled\n')
                fixture.marker.chmod(0o600)
                raise update.UpdateError('health_timeout')
            with self.assertRaisesRegex(update.UpdateError, 'health_timeout'):
                self.install(fixture, health=health, allow_setup=True)
            self.assertEqual(update.controlled_release(fixture.root), old)
            self.assertEqual(update.read_state(fixture.root, 'result.json')['state'], 'rolled_back')
            self.assertTrue(fixture.marker.exists())

    def test_rate_limited_old_watcher_does_not_fail_rollback_but_a_failed_service_does(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            path = 'messagebox-onboarding-voice.path'
            reconcile = 'messagebox-mode-reconcile.path'
            # The old watcher was already rate-limited before the update.
            fixture.systemctl.states[path] = {'active': 'failed', 'enabled': 'enabled'}
            fixture.systemctl.results[path] = 'unit-start-limit-hit'
            fixture.systemctl.states[reconcile]['active'] = 'active'
            starts = []

            def flap(states):
                starts.append(reconcile)
                if len(starts) > 1:  # the restored watcher trips its trigger limit again
                    states[reconcile]['active'] = 'failed'
                    fixture.systemctl.results[reconcile] = 'trigger-limit-hit'
            fixture.systemctl.after_start[reconcile] = flap

            def unhealthy(root, journal, run):
                checks = dict(update.health_checks(root, journal, self.health_run(fixture))[0])
                # Not running before the update: inactive or failed is acceptable.
                self.assertTrue(checks['unit:messagebox-onboarding-voice.path:failed'])
                raise update.UpdateError('health_timeout')
            fixture.systemctl.states['messagebox-mode-reconcile.path']['active'] = 'active'
            starts.append('initial')
            with self.assertRaisesRegex(update.UpdateError, 'health_timeout'):
                self.install(fixture, health=unhealthy)
            self.assertEqual(update.read_state(fixture.root, 'result.json')['state'], 'rolled_back')
            self.assertFalse(update._rooted(fixture.root, update.INHIBIT).exists())
            self.assertIn(['systemctl', 'reset-failed', path], fixture.systemctl.commands)
            self.assertEqual(fixture.systemctl.states[path]['active'], 'inactive')

        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            service = 'messagebox-dash.service'

            def crash(states):
                states[service]['active'] = 'failed'
                fixture.systemctl.results[service] = 'start-limit-hit'

            def unhealthy(*args):
                # The restored old service then crashes: a real restore failure.
                fixture.systemctl.after_start[service] = crash
                raise update.UpdateError('health_timeout')
            with self.assertRaisesRegex(update.UpdateError, 'rollback_failed'):
                self.install(fixture, health=unhealthy)
            self.assertEqual(update.read_state(fixture.root, 'result.json')['state'], 'rollback_failed')
            self.assertTrue(update._rooted(fixture.root, update.INHIBIT).exists())
            self.assertNotIn(['systemctl', 'reset-failed', service], fixture.systemctl.commands)
            self.assertIn(service, update.read_state(fixture.root, 'journal.json')['restore_error'])

    def test_regenerated_mode_link_after_out_of_band_marker_change_is_not_a_restore_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            # The marker was removed without a daemon-reload: the snapshot still
            # records setup-mode generator output for the runtime target.
            fixture.systemctl.states['messagebox.target'] = {'active': 'active', 'enabled': 'disabled'}
            fixture.systemctl.generator_enables = True

            def unhealthy(*args):
                raise update.UpdateError('health_timeout')
            with self.assertRaisesRegex(update.UpdateError, 'health_timeout'):
                self.install(fixture, health=unhealthy)
            self.assertEqual(fixture.systemctl.states['messagebox.target']['enabled'], 'enabled-runtime')
            self.assertEqual(update.read_state(fixture.root, 'result.json')['state'], 'rolled_back')
            self.assertEqual(update.controlled_release(fixture.root), old)

    def test_each_release_owns_sounds_and_failed_upgrade_restores_previous_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, _old = self.prepare(directory)
            path, entry = add_sound(fixture, 'sounds/cloud/extra.wav')
            previous_bytes = path.read_bytes()
            self.install(fixture)
            previous = update.controlled_release(fixture.root)
            previous_sound = update._rooted(fixture.root, previous) / 'sounds/cloud/extra.wav'
            fixture.manifest_data.update(version='v0.3.0', commit='c' * 40)
            path.write_bytes((fixture.source / 'sounds/cues/cue-press.wav').read_bytes())
            entry['sha256'] = sha256(path)
            add_sound(fixture, 'sounds/cues/next-only.wav')
            def unhealthy(root, journal, run):
                current_sound = update._rooted(root, '/opt/messagebox/sounds/cloud/extra.wav')
                self.assertEqual(current_sound.read_bytes(), path.read_bytes())
                self.assertEqual(previous_sound.read_bytes(), previous_bytes)
                self.assertNotEqual(current_sound.stat().st_ino, previous_sound.stat().st_ino)
                raise update.UpdateError('health_timeout')
            with self.assertRaisesRegex(update.UpdateError, 'health_timeout'):
                self.install(fixture, 2, health=unhealthy)
            self.assertEqual(update.controlled_release(fixture.root), previous)
            self.assertEqual(update._rooted(fixture.root, '/opt/messagebox/sounds/cloud/extra.wav').read_bytes(), previous_bytes)
            self.assertFalse(update._rooted(fixture.root, '/opt/messagebox/sounds/cues/next-only.wav').exists())

    def test_converted_box_cannot_omit_entire_required_cloud_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            import shutil
            shutil.rmtree(fixture.source / 'sounds/cloud')
            fixture.manifest_data['files'] = [entry for entry in fixture.manifest_data['files']
                                              if not entry['source'].startswith('sounds/cloud/')]
            fixture.write_manifest()
            with self.assertRaisesRegex(update.UpdateError, 'sound pack is incomplete'):
                self.install(fixture)
            self.assertEqual(update.controlled_release(fixture.root), old)

    @staticmethod
    def transitioning(fixture, unit, times):
        """Report a timer-started oneshot as activating for the first scans."""
        import subprocess
        seen = []

        def run(command, **options):
            if command == ['systemctl', 'is-active', unit] and (times is None or len(seen) < times):
                seen.append(command)
                fixture.systemctl.commands.append(command[:])
                return subprocess.CompletedProcess(command, 3, 'activating\n')
            return fixture.systemctl(command, **options)
        run.seen = seen
        return run

    def test_preflight_waits_for_a_briefly_activating_oneshot(self):
        unit = 'messagebox-wifi-watchdog.service'
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            run = self.transitioning(fixture, unit, 1)
            start = self.clock.now
            update.install_transaction(fixture.source, fixture.manifest, root=fixture.root, run=run,
                                       operation_id='synthetic-operation', sequence=1, health=lambda *args: None)
            self.assertEqual(len(run.seen), 1)
            self.assertGreaterEqual(self.clock.now - start, update.UNIT_SETTLE_POLL)
            self.assertEqual(update.controlled_release(fixture.root), update.RELEASES + '/v0.3.0-canary.1-' + 'a' * 40)
            self.assertEqual(update.read_state(fixture.root, 'result.json')['state'], 'installed')

    def test_preflight_fails_before_mutation_when_a_unit_stays_activating(self):
        unit = 'messagebox-wifi-watchdog.service'
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            run = self.transitioning(fixture, unit, None)
            start = self.clock.now
            fixture.systemctl.commands.clear()
            with self.assertRaisesRegex(update.UpdateError,
                                        rf'still transitioning: {unit} \(activating\); did not settle within 30s'):
                update.install_transaction(fixture.source, fixture.manifest, root=fixture.root, run=run,
                                           operation_id='synthetic-operation', sequence=1, health=lambda *args: None)
            self.assertGreaterEqual(self.clock.now - start, update.UNIT_SETTLE_TIMEOUT)
            self.assertGreater(len(run.seen), 2)
            self.assertEqual(update.controlled_release(fixture.root), old)
            self.assertFalse(any(command[1] in {'stop', 'start', '--job-mode=ignore-dependencies'}
                                 for command in fixture.systemctl.commands))
            self.assertEqual(update.read_state(fixture.root, 'journal.json')['kind'], 'bootstrap')

    def test_bootstrap_waits_for_a_briefly_activating_oneshot(self):
        unit = 'messagebox-wifi-watchdog.service'
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.metadata.write_text(json.dumps({'version': 'v0.2.0', 'commit': 'b' * 40}))
            run = self.transitioning(fixture, unit, 1)
            old = update.bootstrap_layout(root=fixture.root, run=run)
            self.assertEqual(len(run.seen), 1)
            self.assertEqual(update.controlled_release(fixture.root), old)

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

    def test_app_evidence_is_required_only_from_selected_producers_outside_setup_mode(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            new = update.RELEASES + '/v0.3.0-' + 'a' * 40
            update._rooted(root, new).mkdir(parents=True)
            update.switch_release(root, new)

            def run(command, **options):
                if command[0] == 'systemctl':
                    return subprocess.CompletedProcess(command, 0, 'ActiveState=inactive\nMainPID=0\nNRestarts=0\n')
                return subprocess.CompletedProcess(command, 1, '')

            def names(units, **extra):
                journal = {'transaction': 't', 'boot': 'b', 'version': 'v0.3.0', 'commit': 'a' * 40,
                           'started_mono': 0, 'new': new, 'hashes': [], 'units': units, **extra}
                return {name for name, _passed in update.health_checks(root, journal, run)[0]}
            unclaimed = {unit: {'active': 'inactive', 'enabled': 'disabled'}
                         for unit in ('messagebox-button.service', 'messagebox-poller.service')}
            # An unclaimed Cloud box never selected the app: nothing to prove.
            self.assertFalse({name for name in names(unclaimed) if name.startswith('evidence:')})
            # A selected but crashed producer still has to prove itself.
            crashed = {**unclaimed, 'messagebox-poller.service': {'active': 'failed', 'enabled': 'enabled'}}
            evidence = {name for name in names(crashed) if name.startswith('evidence:')}
            self.assertIn('evidence:heartbeat:ready', evidence)
            self.assertNotIn('evidence:button:ready', evidence)
            # Legacy journals without enablement keep the strict default.
            self.assertIn('evidence:button:ready', names({}))
            # Setup mode: never app evidence, and the marker must remain.
            setup = names(crashed, mode='setup')
            self.assertFalse({name for name in setup if name.startswith('evidence:')})
            self.assertIn('setup:marker', setup)

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
        for failure in ('updater_died', 'crash_loop', 'health_timeout'):
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
                elif failure == 'health_timeout':
                    self.clock.now = journal['deadline_mono']
                    with mock.patch.object(update, '_process_token', return_value='synthetic-token'):
                        update.supervise_transaction(root=fixture.root, run=run)
                else:
                    with mock.patch.object(update, '_process_token', return_value='synthetic-token'), \
                         mock.patch.object(update, 'health_sample', side_effect=[(True, {'button': ('1', '0')}), (True, {'button': ('5', '3')})]):
                        update.supervise_transaction(root=fixture.root, run=run)
                        update.supervise_transaction(root=fixture.root, run=run)
                self.assertEqual(update.controlled_release(fixture.root), old)
                self.assertEqual(update.read_state(fixture.root, 'result.json')['reason'], failure)
                self.assertEqual(update.read_state(fixture.root, 'result.json')['detail'], failure)

    def test_supervisor_tolerates_transient_and_resets_persisted_unready_clock(self):
        import subprocess
        for sustained in (False, True):
            with self.subTest(sustained=sustained), tempfile.TemporaryDirectory() as directory:
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
                ready = True
                def sample(*args, failures):
                    if not ready:
                        failures.append('evidence:heartbeat:fresh')
                    return ready, {}
                with mock.patch.object(update, '_process_token', return_value='synthetic-token'), \
                     mock.patch.object(update, 'health_sample', side_effect=sample):
                    self.clock.now = 10
                    update.supervise_transaction(root=fixture.root, run=run)
                    ready = False
                    for now in (20, 39):
                        self.clock.now = now
                        update.supervise_transaction(root=fixture.root, run=run)
                        self.assertEqual(update.controlled_release(fixture.root), journal['new'])
                        self.assertEqual(update.read_state(fixture.root, 'supervisor.json')['first_unready_mono'], 20)
                    ready = True
                    self.clock.now = 40
                    update.supervise_transaction(root=fixture.root, run=run)
                    monitor = update.read_state(fixture.root, 'supervisor.json')
                    self.assertNotIn('first_unready_mono', monitor)
                    self.assertEqual(monitor['last_failures'], ['evidence:heartbeat:fresh'])
                    ready = False
                    self.clock.now = 41
                    update.supervise_transaction(root=fixture.root, run=run)
                    self.clock.now = 61 if sustained else 60
                    update.supervise_transaction(root=fixture.root, run=run)
                if sustained:
                    self.assertEqual(update.controlled_release(fixture.root), old)
                    result = update.read_state(fixture.root, 'result.json')
                    self.assertEqual(result['state'], 'rolled_back')
                    self.assertEqual(result['reason'], 'readiness_lost')
                    self.assertEqual(result['detail'], 'evidence:heartbeat:fresh')
                else:
                    self.assertEqual(update.controlled_release(fixture.root), journal['new'])

    def test_terminal_rollback_receipt_survives_next_supervisor_tick(self):
        import hashlib
        for state in ('rolled_back',):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                fixture, _old = self.prepare(directory)
                operation_id = 'synthetic-operation'
                name = 'operations/' + hashlib.sha256(operation_id.encode()).hexdigest() + '.json'
                update.save_state(fixture.root, name, {'operation_id': operation_id, 'state': 'installing'})
                update.save_state(fixture.root, 'ticket.json', {'operation_id': operation_id,
                                  'boot': 'synthetic-boot', 'queued_mono': 0})
                result = {'operation_id': operation_id, 'state': state, 'reason': 'readiness_lost',
                          'detail': 'evidence:heartbeat:fresh'}
                update.save_state(fixture.root, 'result.json', result, 0o644)
                journal = update.read_state(fixture.root, 'journal.json')
                journal['phase'] = state
                update.save_state(fixture.root, 'journal.json', journal)
                self.clock.now = 100
                with mock.patch.object(update, '_run', side_effect=AssertionError('no service mutation')):
                    update.supervise_transaction(root=fixture.root, run=fixture.systemctl)
                    update.supervise_transaction(root=fixture.root, run=fixture.systemctl)
                self.assertEqual(update.read_state(fixture.root, 'result.json'), result)
                operation = update.read_state(fixture.root, name)
                self.assertEqual(operation['state'], state)
                self.assertEqual(operation['reason'], 'readiness_lost')
                self.assertEqual(operation['detail'], 'evidence:heartbeat:fresh')

    def test_supervisor_retries_a_real_rollback_failed_after_storage_fault_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, old = self.prepare(directory)
            with mock.patch.object(update, '_restore_record', side_effect=OSError('injected restore I/O failure')), \
                    self.assertRaisesRegex(update.UpdateError, 'rollback_failed'):
                self.install(fixture, health=mock.Mock(side_effect=update.UpdateError('health_timeout')))
            self.assertEqual(update.read_state(fixture.root, 'journal.json')['phase'], 'rollback_failed')
            fixture.systemctl.states['messagebox-update-install.service'] = {'active': 'failed', 'enabled': 'static'}
            update.supervise_transaction(root=fixture.root, run=fixture.systemctl)
            self.assertEqual(update.read_state(fixture.root, 'journal.json')['phase'], 'rolled_back')
            self.assertEqual(update.controlled_release(fixture.root), old)
            self.assertFalse(update._rooted(fixture.root, update.INHIBIT).exists())

    def test_bootstrap_only_removes_unheld_top_level_lgpio_fifos(self):
        for kind in ('unused', 'held', 'other', 'nested', 'symlink', 'unreadable'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(directory)
                fixture.metadata.write_text(json.dumps({'version': 'v0.2.0', 'commit': 'b' * 40}))
                pointer = fixture.root / 'opt/messagebox'
                fifo = pointer / ('.lgd-nfy-test' if kind != 'other' else 'other-fifo')
                if kind == 'nested':
                    fifo = pointer / 'nested' / fifo.name
                    fifo.parent.mkdir()
                if kind == 'symlink':
                    fifo.symlink_to('release.json')
                else:
                    os.mkfifo(fifo)
                proc = fixture.root / 'proc'
                proc.mkdir()
                if kind == 'held':
                    descriptors = proc / '123/fd'
                    descriptors.mkdir(parents=True)
                    (descriptors / '7').symlink_to(fifo)
                scanner = update._fifo_held
                def held(path):
                    if kind == 'unreadable':
                        raise PermissionError()
                    return scanner(path, proc=proc)
                with mock.patch.object(update, '_fifo_held', side_effect=held):
                    if kind == 'unused':
                        old = update.bootstrap_layout(root=fixture.root, run=fixture.systemctl)
                        self.assertFalse((update._rooted(fixture.root, old) / fifo.name).exists())
                    else:
                        with self.assertRaisesRegex(update.UpdateError, 'unsafe_legacy_tree'):
                            update.bootstrap_layout(root=fixture.root, run=fixture.systemctl)
                        self.assertTrue(fifo.exists())
                        self.assertFalse(pointer.is_symlink())

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


class EvidenceClockTests(unittest.TestCase):
    def test_evidence_written_during_a_slow_sample_is_not_future_dated(self):
        import importlib
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path
        from unittest import mock as _mock
        bounded = importlib.import_module('bounded_update')
        with _tempfile.TemporaryDirectory() as tmp:
            root = _Path(tmp)
            run_dir = root / 'run/messagebox-update'
            run_dir.mkdir(parents=True)
            journal = {'started_mono': 100.0, 'units': {}, 'hashes': [], 'transaction': 't', 'boot': 'b',
                       'version': 'v1', 'commit': 'c' * 40, 'new': '/x'}
            # The button writes evidence at 205 while the slow systemctl calls run; the check reads 206.
            for name in ('button', 'heartbeat'):
                (run_dir / (name + '.json')).write_text(_json.dumps({'transaction': 't', 'boot': 'b', 'software_version': 'v1',
                    'software_commit': 'c' * 40, 'monotonic': 205.0, 'pid': 1, 'ready': True}))
            with _mock.patch.object(bounded.time, 'monotonic', lambda: 206.0), \
                 _mock.patch.object(bounded, 'controlled_release', return_value='/x'):
                checks = bounded.health_checks(root, journal, run=lambda *a, **k: _mock.Mock(stdout='', returncode=1))[0]
        names = dict(checks)
        self.assertTrue(names['evidence:button:monotonic'])
        self.assertTrue(names['evidence:heartbeat:monotonic'])
