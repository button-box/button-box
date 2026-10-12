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
        with mock.patch.object(controller, 'INSTALLED', marker), \
             mock.patch.object(controller, 'os', types.SimpleNamespace(geteuid=lambda: 0)), \
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
            ipc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
