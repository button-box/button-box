"""Local policy, one-use GPIO confirmation, mic boundary and unprivileged peers."""

import contextlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from test_update_discovery import INSTALL
sys.path.insert(0, str(INSTALL))
import update_controller as controller
sys.path.pop(0)
from messagebox import cloud_update
from test_mic_access import runtime


class Clock:
    now = 10.0
    def monotonic(self):
        return self.now
    def sleep(self, value):
        self.now += value


class UpdateCueTests(unittest.TestCase):
    def test_recordings_selected_for_install_and_auto(self):
        with mock.patch.object(cloud_update.sound_pack, 'SOUND_DIR', Path(__file__).resolve().parents[1] / 'sounds'), \
             mock.patch.object(cloud_update.subprocess, 'run') as synthesize:
            for text, name in ((cloud_update.CUE, 'update-ready.wav'), (cloud_update.AUTO_CUE, 'auto-updates.wav')):
                play = mock.Mock()
                cloud_update._voice(text, play)
                self.assertEqual(play.call_args.args[0].name, name)
            synthesize.assert_not_called()

    def test_missing_invalid_or_symlink_recording_uses_espeak_and_completion_fallback(self):
        for kind in ('missing', 'invalid', 'symlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'cues').mkdir()
                for name in ('update-ready.wav', 'auto-updates.wav', 'update-done.wav'):
                    path = root / 'cues' / name
                    if kind == 'invalid':
                        path.write_bytes(b'invalid WAV')
                    elif kind == 'symlink':
                        path.symlink_to(Path(__file__).resolve().parents[1] / 'sounds/cues/cue-press.wav')
                with mock.patch.object(cloud_update.sound_pack, 'SOUND_DIR', root), \
                     mock.patch.object(cloud_update.subprocess, 'run') as synthesize:
                    for text in (cloud_update.CUE, cloud_update.AUTO_CUE):
                        cloud_update._voice(text, mock.Mock())
                        self.assertEqual(synthesize.call_args.args[0][0], 'espeak-ng')
                        self.assertEqual(synthesize.call_args.args[0][-1], text)
                    self.assertEqual(cloud_update.installed_cue(), root / 'cues/cue-all_set.wav')


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = Clock()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(controller, 'read', side_effect=lambda name, default=None: controller.update.read_state(self.root, name, default)))
        self.stack.enter_context(mock.patch.object(controller, 'save', side_effect=lambda name, value, mode=0o600: controller.update.save_state(self.root, name, value, mode)))
        self.stack.enter_context(mock.patch.object(controller.time, 'monotonic', side_effect=self.clock.monotonic))
        self.stack.enter_context(mock.patch.object(controller.update, '_boot', return_value='boot-one'))
        self.stack.enter_context(mock.patch.object(controller.pwd, 'getpwnam', side_effect=lambda name: types.SimpleNamespace(pw_uid=100 if name == 'messagebox' else 101)))
        self.stack.enter_context(mock.patch.object(controller, 'unit_pid', return_value=700))
        self.stack.enter_context(mock.patch.object(controller, 'installed_identity', return_value={'software_version': 'v0.2.0', 'software_commit': 'b' * 40}))
        self.stack.enter_context(mock.patch.object(controller, 'installed', return_value=True))
        self.stack.enter_context(mock.patch.object(controller, "RUN", self.root / "run/messagebox-update"))
        self.stack.enter_context(mock.patch.object(controller, "STATE", self.root / "var/lib/button-box-update"))
        self.stack.enter_context(mock.patch.object(controller, "wake_discovery"))
        self.broker = controller.Controller()
        controller.save('policy.json', {'mode': 'ask', 'channel': 'stable', 'timezone': 'Europe/Lisbon', 'jitter_minutes': 0})

    def send(self, command, *, pid=700, uid=100, **fields):
        return self.broker.dispatch({'command': command, **fields}, (pid, uid, 100))

    def test_cloud_ask_to_auto_requires_button_peer_fresh_edge_and_same_binding(self):
        result = self.send('mode', operation_id='synthetic-mode', mode='auto', remaining=300)
        self.assertEqual(result['state'], 'waiting_for_button')
        self.assertEqual(controller.policy()['mode'], 'ask')
        binding = result['binding']
        with self.assertRaisesRegex(ValueError, 'not_button_owner'):
            self.send('arm', pid=701, binding=binding, released=True)
        self.send('arm', binding=binding, released=True)
        with self.assertRaisesRegex(ValueError, 'short_or_expired'):
            self.send('confirm', binding=binding, released=True, held_seconds=1000)
        self.send('button', released=False)
        self.clock.now += 2.9
        with self.assertRaisesRegex(ValueError, 'short_or_expired'):
            self.send('confirm', binding=binding, released=True)
        # A short hold followed by release resets the root monotonic start.
        self.send('button', released=True)
        self.send('button', released=False)
        self.clock.now += 3.1
        with self.assertRaisesRegex(ValueError, 'approval_expired'):
            self.send('confirm', binding='different-candidate', released=True)
        self.send('confirm', binding=binding, released=True)
        self.assertEqual(controller.policy()['mode'], 'auto')
        with self.assertRaises(ValueError):
            self.send('confirm', binding=binding, released=True)
        self.assertEqual(self.send('mode', operation_id='synthetic-mode', mode='auto', remaining=300)['state'], 'installed')

    def test_expiry_reboot_duplicates_cannot_rearm_or_change_candidate(self):
        result = self.send('mode', operation_id='synthetic-mode', mode='auto', remaining=300)
        binding = result['binding']
        self.send('arm', binding=binding, released=True)
        self.clock.now += 121
        self.assertIsNone(self.send('button', released=False)['approval'])
        duplicate = self.send('mode', operation_id='synthetic-mode', mode='auto', remaining=300)
        self.assertEqual(duplicate['state'], 'expired')
        self.assertEqual(controller.policy()['mode'], 'ask')
        second = self.send('mode', operation_id='synthetic-reboot', mode='auto', remaining=300)
        with mock.patch.object(controller.update, '_boot', return_value='boot-two'):
            self.assertEqual(self.send('mode', operation_id='synthetic-reboot', mode='auto', remaining=300)['state'], 'expired')
        with self.assertRaisesRegex(ValueError, 'operation_conflict'):
            self.send('mode', operation_id='synthetic-reboot', mode='ask', remaining=300)
        self.assertNotEqual(second['operation_id'], duplicate['operation_id'])

    def test_exact_install_is_idempotent_and_always_asks_even_in_auto(self):
        configuration = controller.policy()
        configuration['mode'] = 'auto'
        controller.save('policy.json', configuration)
        controller.save('available.json', {'version': 'v0.3.0', 'manifest_sha256': 'a' * 64, 'sequence': 1})
        result = self.send('install', operation_id='synthetic-install', version='v0.3.0', remaining=300)
        self.assertEqual(result['state'], 'waiting_for_button')
        duplicate = self.send('install', operation_id='synthetic-install', version='v0.3.0', remaining=5000)
        self.assertEqual(result['binding'], duplicate['binding'])
        self.assertEqual(result['expires_mono'], duplicate['expires_mono'])
        with self.assertRaisesRegex(ValueError, 'operation_conflict'):
            self.send('install', operation_id='synthetic-install', version='v0.3.1', remaining=300)
        for version in ('latest', 'https://example.org/install', 'v0.3.0;reboot', '../../v0.3.0'):
            with self.assertRaises(ValueError):
                self.send('install', operation_id='other', version=version, remaining=300)

    def test_attended_first_application_still_verifies_before_queueing_and_has_no_cloud_command(self):
        import shutil
        bundle = self.root / 'attended-assets'
        bundle.mkdir()
        for name in ('SHA256SUMS', 'SHA256SUMS.minisig', 'release-manifest.json', 'button-box-v0.3.0.tar.gz'):
            (bundle / name).write_text('synthetic bundle bytes')
        marker = self.root / 'installed'
        marker.touch()
        stale = self.auto_waiting('v0.3.0', 'b')
        with mock.patch.object(controller, 'INSTALLED', marker), \
             mock.patch.object(controller, 'os', types.SimpleNamespace(geteuid=lambda: 0, open=os.open, fdopen=os.fdopen, O_WRONLY=os.O_WRONLY, O_CREAT=os.O_CREAT, O_NOFOLLOW=os.O_NOFOLLOW)), \
             mock.patch.object(controller, "local_platform", return_value={"os": "raspios-trixie", "architecture": "aarch64", "model": "Raspberry Pi 4 Model B"}), \
             mock.patch.object(controller.update, '_validate_staged_file'), \
             mock.patch.object(controller.update, '_atomic_install', side_effect=lambda source, target, mode: shutil.copyfile(source, target)), \
             mock.patch.object(controller, 'verify_bundle', return_value={'commit': 'a' * 40, 'release_sequence': 7}) as verify, \
             mock.patch.object(controller, 'start_install') as start:
            controller.attended_install(bundle, 'v0.3.0')
            verify.assert_called_once()
            self.assertEqual(verify.call_args.args[1], controller.KEYS)
            self.assertEqual(start.call_args.args[0]['action'], 'attended')
            self.assertEqual(start.call_args.args[1]['version'], 'v0.3.0')
            # The attended install owns the slot; the waiting automatic one is closed.
            self.assertEqual(controller.read('active.json'), start.call_args.args[0]['operation_id'])
            closed = controller.read(controller.operation_name(stale))
            self.assertEqual((closed['state'], closed['reason']), ('expired', 'superseded'))
            start.reset_mock()
            verify.side_effect = controller.ReleaseError('bad_signature')
            with self.assertRaises(controller.ReleaseError):
                controller.attended_install(bundle, 'v0.3.0')
            start.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'unsupported_command'):
            self.send('attended-install', version='v0.3.0')

    def test_exact_requested_candidate_is_reserved_against_newer_discovery(self):
        downloads = controller.STATE / 'downloads'
        downloads.mkdir(parents=True)
        old = downloads / ('b' * 32)
        old.mkdir()
        controller.save('available.json', {'version': 'v0.3.1', 'sequence': 10, 'manifest_sha256': 'b' * 64, 'bundle': str(old)})
        result = self.send('install', operation_id='synthetic-exact', version='v0.3.0', remaining=300)
        self.assertEqual(result['state'], 'downloading')
        requested = downloads / ('a' * 32)
        requested.mkdir()
        candidate = {'version': 'v0.3.0', 'sequence': 7, 'manifest_sha256': 'a' * 64, 'bundle': str(requested)}
        with mock.patch.object(controller, 'receive_bundle', return_value=candidate):
            self.send('downloaded', uid=101, version='v0.3.0')
        self.assertEqual(controller.read('available.json')['version'], 'v0.3.0')
        reserved = self.send('result', operation_id='synthetic-exact')
        self.assertEqual(reserved['state'], 'waiting_for_button')
        with mock.patch.object(controller, 'receive_bundle') as receive, self.assertRaisesRegex(ValueError, 'reserved'):
            self.send('downloaded', uid=101, version='v0.3.2')
        receive.assert_not_called()

    def auto_waiting(self, version='v0.3.0', manifest='a'):
        configuration = controller.policy()
        configuration['mode'] = 'auto'
        controller.save('policy.json', configuration)
        controller.save('available.json', {'version': version, 'manifest_sha256': manifest * 64, 'sequence': 4})
        with mock.patch.object(controller, 'auto_window', return_value=False):
            self.send('button', released=True)
        operation_id = 'auto:' + manifest * 64
        self.assertEqual(controller.read('active.json'), operation_id)
        self.assertEqual(controller.read(controller.operation_name(operation_id))['state'], 'waiting_for_window')
        return operation_id

    def test_installed_target_closes_stale_automatic_operation_and_owner_install_proceeds(self):
        # Regression: an attended install of the same candidate finished while an
        # automatic operation still waited; a newer explicit install was rejected.
        stale = self.auto_waiting('v0.3.6-canary.4', 'a')
        controller.save('result.json', {'operation_id': 'bootstrap:' + 'a' * 64, 'state': 'installed',
                                        'reason': 'healthy', 'version': 'v0.3.6-canary.4'}, 0o644)
        controller.save('available.json', {'version': 'v0.3.6', 'manifest_sha256': 'b' * 64, 'sequence': 5})
        with mock.patch.object(controller, 'installed_identity',
                               return_value={'software_version': 'v0.3.6-canary.4', 'software_commit': 'c' * 40}), \
             mock.patch.object(controller, 'public_status', return_value={}):
            self.send('status', uid=101)
            closed = controller.read(controller.operation_name(stale))
            self.assertEqual((closed['state'], closed['reason']), ('installed', 'already_installed'))
            # Closing the stale receipt never replaces the reported transaction result.
            self.assertEqual(controller.read('result.json')['operation_id'], 'bootstrap:' + 'a' * 64)
            result = self.send('install', operation_id='synthetic-owner', version='v0.3.6', remaining=300)
        self.assertEqual(result['state'], 'waiting_for_button')

    def test_explicit_install_replaces_unstarted_automatic_but_keeps_one_use_approval(self):
        stale = self.auto_waiting('v0.3.0', 'a')
        with self.assertRaisesRegex(ValueError, 'update_busy'):
            self.send('install', operation_id='synthetic-older', version='v0.2.9', remaining=300)
        with self.assertRaisesRegex(ValueError, 'update_busy'):
            self.send('mode', operation_id='synthetic-mode', mode='ask', remaining=300)
        result = self.send('install', operation_id='synthetic-owner', version='v0.3.0', remaining=300)
        self.assertEqual(result['state'], 'waiting_for_button')
        self.assertTrue(result['cue_pending'])
        closed = controller.read(controller.operation_name(stale))
        self.assertEqual((closed['state'], closed['reason']), ('expired', 'superseded_by_owner'))
        # The owner request now holds the slot; no automatic operation can take it back.
        with mock.patch.object(controller, 'auto_window', return_value=True), \
             mock.patch.object(controller, 'start_install') as start:
            self.send('button', released=True)
            start.assert_not_called()
        self.assertEqual(controller.read('active.json'), 'synthetic-owner')
        # Installing (started) automatic work still blocks owner requests.
        operation = controller.read(controller.operation_name('synthetic-owner'))
        controller.record(operation, 'expired', 'button_window_expired')
        started = self.auto_waiting('v0.3.0', 'a')
        operation = controller.read(controller.operation_name(started))
        controller.record(operation, 'installing')
        with self.assertRaisesRegex(ValueError, 'update_busy'):
            self.send('install', operation_id='synthetic-later', version='v0.3.0', remaining=300)

    def test_newer_candidate_replaces_waiting_automatic_operation(self):
        stale = self.auto_waiting('v0.3.0', 'a')
        controller.save('available.json', {'version': 'v0.3.1', 'manifest_sha256': 'b' * 64, 'sequence': 5})
        with mock.patch.object(controller, 'auto_window', return_value=True), \
             mock.patch.object(controller, 'local_time_ready', return_value=True), \
             mock.patch.object(controller, 'start_install') as start:
            self.send('button', released=True)
        closed = controller.read(controller.operation_name(stale))
        self.assertEqual((closed['state'], closed['reason']), ('expired', 'superseded'))
        operation, available = start.call_args.args
        self.assertEqual(operation['operation_id'], 'auto:' + 'b' * 64)
        self.assertEqual((operation['version'], available['version']), ('v0.3.1', 'v0.3.1'))

    def test_completed_install_closes_automatic_operation_left_by_a_race(self):
        stale = self.auto_waiting('v0.3.6-canary.4', 'a')
        bundle = controller.STATE / 'downloads' / ('c' * 32)
        bundle.mkdir(parents=True)
        operation = {'operation_id': 'bootstrap:' + 'a' * 64, 'action': 'attended', 'version': 'v0.3.6-canary.4',
                     'boot': 'boot-one', 'expires_mono': 1000}
        controller.record(operation, 'installing')
        controller.save('ticket.json', {'operation_id': operation['operation_id'], 'version': 'v0.3.6-canary.4',
                                        'manifest_sha256': 'a' * 64, 'bundle': str(bundle)})
        controller.save('active.json', stale)  # the broker raced the attended pointer write
        identity = {'software_version': 'v0.3.6-canary.4', 'software_commit': 'c' * 40}
        with mock.patch.object(controller, 'digest', return_value='a' * 64), \
             mock.patch.object(controller, 'verify_bundle', return_value={'release_sequence': 4}), \
             mock.patch.object(controller, 'extract_bundle'), \
             mock.patch.object(controller, 'local_platform', return_value={}), \
             mock.patch.object(controller.update, 'install_transaction'), \
             mock.patch.object(controller, 'installed_identity', return_value=identity):
            controller._worker()
        self.assertEqual(controller.read('result.json')['state'], 'installed')
        closed = controller.read(controller.operation_name(stale))
        self.assertEqual((closed['state'], closed['reason']), ('installed', 'already_installed'))

    def test_only_the_attended_worker_may_install_in_setup_mode(self):
        for action, allowed in (('attended', True), ('explicit', False), ('automatic', False)):
            with self.subTest(action=action):
                bundle = controller.STATE / 'downloads' / ('d' * 32)
                bundle.mkdir(parents=True, exist_ok=True)
                operation = {'operation_id': f'{action}:' + 'a' * 64, 'action': action, 'version': 'v0.3.6',
                             'boot': 'boot-one', 'expires_mono': 1000}
                controller.record(operation, 'installing')
                controller.save('ticket.json', {'operation_id': operation['operation_id'], 'version': 'v0.3.6',
                                                'manifest_sha256': 'a' * 64, 'bundle': str(bundle)})
                with mock.patch.object(controller, 'digest', return_value='a' * 64), \
                     mock.patch.object(controller, 'verify_bundle', return_value={'release_sequence': 4}), \
                     mock.patch.object(controller, 'extract_bundle'), \
                     mock.patch.object(controller, 'local_platform', return_value={}), \
                     mock.patch.object(controller.update, 'install_transaction') as install:
                    controller._worker()
                self.assertIs(install.call_args.kwargs['allow_setup'], allowed)

    def test_rejections_carry_only_bounded_codes(self):
        self.assertEqual(controller.rejection_code(ValueError('update_busy')), 'update_busy')
        self.assertEqual(controller.rejection_code(controller.ReleaseError('trust_update_busy')), 'trust_update_busy')
        self.assertEqual(controller.rejection_code(ValueError('/var/lib/secret')), 'request_rejected')
        self.assertEqual(controller.rejection_code(OSError('update_busy')), 'request_rejected')
        self.assertLessEqual(controller.REJECTION_CODES, cloud_update.REJECTION_CODES)
        self.assertEqual(controller.version_key('v0.3.6-canary.4') < controller.version_key('v0.3.6'), True)
        self.assertEqual(controller.version_key('0.3.5') < controller.version_key('v0.3.6-canary.1'), True)
        self.assertIsNone(controller.version_key('latest'))

    def test_dowloader_and_runtime_peers_cannot_forge_button_or_heartbeat_evidence(self):
        for command in ('arm', 'confirm', 'button', 'heartbeat'):
            for uid, pid in ((101, 700), (100, 701)):
                with self.subTest(command=command, uid=uid, pid=pid), self.assertRaises(ValueError):
                    self.send(command, uid=uid, pid=pid)
        with self.assertRaises(ValueError):
            self.send('downloaded', version='v0.3.0')

    def test_auto_requires_valid_box_timezone_quiet_window(self):
        from datetime import datetime, timezone
        def timestamp(value):
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
        self.assertFalse(controller.auto_window(0, 'Europe/Lisbon', 0))
        self.assertFalse(controller.auto_window(timestamp('2026-01-12T01:59:00'), 'Europe/Lisbon', 0))
        self.assertTrue(controller.auto_window(timestamp('2026-01-12T02:20:00'), 'Europe/Lisbon', 10))
        self.assertFalse(controller.auto_window(timestamp('2026-01-12T04:00:00'), 'Europe/Lisbon', 0))
        self.assertFalse(controller.auto_window(timestamp('2026-01-12T02:00:00'), 'Invalid/Timezone', 0))


class ButtonBoundaryTests(unittest.TestCase):
    def test_short_long_expired_and_already_held_confirmation_never_mint_capture(self):
        for duration, expired, cue_pending in ((1, False, False), (3.2, False, False), (3.2, True, False), (3.2, False, True)):
            with self.subTest(duration=duration, expired=expired, cue_pending=cue_pending):
                clock = Clock()
                deadline = clock.now - 1 if expired else clock.now + 120
                approval = {'binding': 'operation-manifest-version-boot', 'deadline_mono': deadline, 'cue_pending': cue_pending}
                ipc = mock.Mock(return_value={'approval': approval})
                button = types.SimpleNamespace(is_pressed=True)
                def sleep(seconds):
                    clock.sleep(seconds)
                    if clock.now >= 10 + duration:
                        button.is_pressed = False
                confirmation = cloud_update.Confirmation(ipc=ipc, monotonic=clock.monotonic, sleep=sleep)
                with mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT': 'cloud'}), \
                     mock.patch.object(cloud_update, 'confirmation', confirmation), \
                     mock.patch.object(runtime, 'button', button, create=True), \
                     mock.patch.object(runtime, 'transport_mode', return_value='cloud'), \
                     mock.patch.object(runtime, 'capture_session') as capture, \
                     mock.patch.object(runtime.subprocess, 'Popen') as audio:
                    self.assertTrue(runtime.handle_confirmed_press(clock.now))
                    capture.assert_not_called()
                    audio.assert_not_called()
                confirmed = [call for call in ipc.call_args_list if call.args[0] == 'confirm']
                self.assertEqual(len(confirmed), int(duration >= 3 and not expired and not cue_pending))

    def run_hold(self, duration, *, expired=False, cue_pending=False, already_held=False):
        clock = Clock()
        deadline = clock.now - 1 if expired else clock.now + 120
        approval = {'binding': 'operation-manifest-version-boot', 'deadline_mono': deadline, 'cue_pending': cue_pending}
        events = []
        def ipc(command, **fields):
            events.append((command, clock.now))
            return {'approval': None if already_held else approval}
        button = types.SimpleNamespace(is_pressed=True)
        def sleep(seconds):
            clock.sleep(seconds)
            if clock.now >= 10 + duration:
                button.is_pressed = False
        def on_hold():
            events.append(('cue', clock.now, button.is_pressed))
            def wait(timeout):
                events.append(('cue_wait', timeout))
            return wait
        confirmation = cloud_update.Confirmation(ipc=ipc, monotonic=clock.monotonic, sleep=sleep, on_hold=on_hold)
        if already_held:
            confirmation.consume_release = True
        with mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT': 'cloud'}):
            self.assertTrue(confirmation.intercept(button))
        return events

    def test_hold_cue_fires_once_at_threshold_while_held_and_confirm_waits_for_it(self):
        events = self.run_hold(5)
        cues = [event for event in events if event[0] == 'cue']
        self.assertEqual(len(cues), 1)
        self.assertGreaterEqual(cues[0][1], 13)
        self.assertLess(cues[0][1], 13.1)
        self.assertTrue(cues[0][2])
        names = [event[0] for event in events]
        self.assertEqual(names[-2:], ['cue_wait', 'confirm'])
        self.assertEqual(events[-2][1], cloud_update.HOLD_CUE_WAIT_S)

    def test_hold_cue_never_for_short_expired_cue_pending_or_already_held_presses(self):
        for kwargs in ({'duration': 1}, {'duration': 2.9}, {'duration': 5, 'expired': True},
                       {'duration': 5, 'cue_pending': True}, {'duration': 5, 'already_held': True}):
            with self.subTest(**kwargs):
                events = self.run_hold(**kwargs)
                self.assertFalse([event for event in events if event[0] in {'cue', 'cue_wait', 'confirm'}])

    def test_hold_cue_failure_still_confirms(self):
        clock = Clock()
        approval = {'binding': 'b', 'deadline_mono': clock.now + 120, 'cue_pending': False}
        ipc = mock.Mock(return_value={'approval': approval})
        button = types.SimpleNamespace(is_pressed=True)
        def sleep(seconds):
            clock.sleep(seconds)
            button.is_pressed = clock.now < 14
        confirmation = cloud_update.Confirmation(ipc=ipc, monotonic=clock.monotonic, sleep=sleep,
                                                 on_hold=mock.Mock(side_effect=OSError('no speaker')))
        with mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT': 'cloud'}):
            confirmation.intercept(button)
        self.assertEqual([call.args[0] for call in ipc.call_args_list], ['button', 'confirm'])

    def test_runtime_hold_cue_waits_for_player_and_reservation(self):
        released = mock.Mock()
        process = mock.Mock(reservation_released=released)
        with mock.patch.object(runtime, 'start_player', return_value=process) as start:
            wait = runtime.start_update_hold_cue()
            self.assertIn('cue-card_saved.wav', start.call_args.args[0][-1])
            process.wait.assert_not_called()
            wait(1.5)
        process.wait.assert_called_once_with(timeout=1.5)
        released.wait.assert_called_once()
        self.assertLessEqual(released.wait.call_args.args[0], 1.5)

    def test_window_begins_after_fixed_voice_cue_and_observed_release(self):
        clock = Clock()
        button = types.SimpleNamespace(is_pressed=False)
        approval = {'action': 'install', 'binding': 'synthetic-binding', 'cue_pending': True}
        events = []
        def ipc(command, **fields):
            events.append(command)
            return {'approval': approval if command == 'button' else {**approval, 'cue_pending': False, 'deadline_mono': clock.now + 120}}
        def voice(text, play):
            events.append(text)
            button.is_pressed = True
        def sleep(seconds):
            clock.sleep(seconds)
            button.is_pressed = False
        confirmation = cloud_update.Confirmation(ipc=ipc, voice=voice, monotonic=clock.monotonic, sleep=sleep)
        with mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT': 'cloud'}):
            confirmation.idle(button, mock.Mock())
        self.assertEqual(events, ['button', cloud_update.CUE, 'arm'])
        self.assertFalse(button.is_pressed)
        self.assertGreater(confirmation.binding['deadline_mono'], 130)

    def test_wacli_bypasses_updater_and_maintenance_keeps_physical_capture_path(self):
        with mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT': 'wacli'}), \
             mock.patch.object(runtime, 'transport_mode', return_value='wacli'), \
             mock.patch.object(runtime, '_capture_confirmed_press', return_value=True) as flow, \
             mock.patch.object(cloud_update, 'request') as ipc:
            self.assertTrue(runtime.handle_confirmed_press(5, card_prompt_uid='synthetic-card'))
            flow.assert_called_once_with(5, card_prompt_uid='synthetic-card')
            cloud_update.confirmation.idle(types.SimpleNamespace(is_pressed=False), mock.Mock())
            self.assertEqual(cloud_update.capability(), {})
            self.assertEqual(cloud_update.heartbeat_fields(), {})
            with mock.patch.object(cloud_update.install_announcer, 'take') as take, \
                 mock.patch.object(runtime, 'play_idle_sound') as play:
                runtime.announce_update_installed()
                take.assert_not_called()
                play.assert_not_called()
            ipc.assert_not_called()



class InstallAnnouncerTests(unittest.TestCase):
    FIELDS = {'software_version': 'v0.9.0', 'software_commit': 'a' * 40}

    def result(self, **changes):
        return {'operation_id': 'owner-op-1', 'version': 'v0.9.0', 'state': 'installed', 'reason': 'healthy',
                'software_version': 'v0.9.0', 'software_commit': 'a' * 40, **changes}

    def announcer(self, results):
        clock = Clock()
        sequence = list(results)
        polls = []
        def get_status():
            polls.append(clock.now)
            return {'last_result': sequence.pop(0) if len(sequence) > 1 else sequence[0]}
        announcer = cloud_update.InstallAnnouncer(get_status=get_status, fields=lambda: self.FIELDS,
                                                  monotonic=clock.monotonic, sleep=clock.sleep)
        return announcer, polls, clock

    def test_owner_install_announces_once_after_health(self):
        announcer, polls, _ = self.announcer([self.result(state='installing', reason='none'), self.result()])
        announcer.run()
        self.assertEqual(len(polls), 2)
        self.assertEqual(polls[1] - polls[0], cloud_update.INSTALL_POLL_S)
        self.assertEqual(announcer.take(), 'owner-op-1')
        self.assertIsNone(announcer.take())

    def test_never_for_auto_bootstrap_rollback_failure_mode_change_or_other_release(self):
        for result in (self.result(operation_id='auto:' + 'b' * 64), self.result(operation_id='bootstrap:' + 'c' * 64),
                       self.result(state='rolled_back', reason='health_failed'), self.result(state='failed', reason='x'),
                       self.result(reason='mode_changed'), self.result(software_version='v0.8.0'),
                       self.result(software_commit='b' * 40)):
            with self.subTest(result=result):
                announcer, polls, _ = self.announcer([result])
                announcer.run()
                self.assertEqual(len(polls), 1)
                self.assertIsNone(announcer.take())

    def test_polling_stops_after_window(self):
        announcer, polls, clock = self.announcer([self.result(state='installing', reason='none')])
        announcer.run()
        self.assertGreaterEqual(clock.now - 10, cloud_update.INSTALL_WINDOW_S)
        self.assertEqual(len(polls), cloud_update.INSTALL_WINDOW_S // cloud_update.INSTALL_POLL_S)
        self.assertIsNone(announcer.take())

    def test_pending_expires_with_window(self):
        announcer, _, clock = self.announcer([self.result()])
        announcer.run()
        clock.now += cloud_update.INSTALL_WINDOW_S
        self.assertIsNone(announcer.take())

    def test_unavailable_updater_and_wacli_stay_silent(self):
        with mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT': 'wacli'}), \
             mock.patch.object(cloud_update, 'request') as ipc:
            clock = Clock()
            announcer = cloud_update.InstallAnnouncer(fields=lambda: self.FIELDS, monotonic=clock.monotonic, sleep=clock.sleep)
            announcer.run()
            self.assertIsNone(announcer.take())
            ipc.assert_not_called()

    def runtime_announce(self, operation_id, *, quiet=False, pressed=False, state=None):
        announcer = mock.Mock()
        announcer.take.return_value = operation_id
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'sound-state.json'
            consume = runtime.sound_pack.consume_moment
            with mock.patch.object(runtime, 'transport_mode', return_value='cloud'), \
                 mock.patch.object(cloud_update.sound_pack, 'SOUND_DIR', Path(__file__).resolve().parents[1] / 'sounds'), \
                 mock.patch.object(runtime, 'quiet_hours', return_value=quiet), \
                 mock.patch.object(runtime, 'button', types.SimpleNamespace(is_pressed=pressed), create=True), \
                 mock.patch.object(runtime, 'log_event'), \
                 mock.patch.object(cloud_update, 'install_announcer', announcer), \
                 mock.patch.object(runtime.sound_pack, 'consume_moment',
                                   side_effect=lambda name, **kw: consume(name, state, **kw)), \
                 mock.patch.object(runtime, 'play_idle_sound', return_value=True) as play:
                runtime.announce_update_installed()
                runtime.announce_update_installed()
                announcer.take.return_value = operation_id
                runtime.announce_update_installed()
            return play, announcer

    def test_runtime_plays_update_done_once_per_operation(self):
        play, _ = self.runtime_announce('owner-op-1')
        play.assert_called_once()
        self.assertTrue(str(play.call_args.args[0]).endswith('update-done.wav'))

    def test_runtime_completion_falls_back_to_all_set(self):
        with mock.patch.object(cloud_update, 'bundled_cue', return_value=None):
            play, _ = self.runtime_announce('owner-op-1')
        self.assertTrue(str(play.call_args.args[0]).endswith('cue-all_set.wav'))

    def test_runtime_respects_quiet_mute_and_press(self):
        for kwargs in ({'quiet': True}, {'pressed': True}):
            with self.subTest(**kwargs):
                play, announcer = self.runtime_announce('owner-op-1', **kwargs)
                play.assert_not_called()
                announcer.take.assert_not_called()

    def test_runtime_no_pending_operation_is_silent(self):
        play, _ = self.runtime_announce(None)
        play.assert_not_called()

if __name__ == '__main__':
    unittest.main()
