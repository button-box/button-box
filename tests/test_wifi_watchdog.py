import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from messagebox import wifi_watchdog as wifi


class WifiRecoveryTests(unittest.TestCase):
    def test_grace_backoff_and_recovery_reset(self):
        state, due = wifi.recovery_decision({}, now=10, runtime=True, connected=False, saved=True)
        self.assertFalse(due)
        for now, expected in ((129, False), (130, True), (249, False), (250, True), (489, False), (490, True)):
            state, due = wifi.recovery_decision(state, now=now, runtime=True, connected=False, saved=True)
            self.assertEqual(due, expected)
        for options in ({'runtime': False, 'connected': False, 'saved': True},
                        {'runtime': True, 'connected': True, 'saved': True},
                        {'runtime': True, 'connected': False, 'saved': False}):
            self.assertEqual(wifi.recovery_decision(state, now=600, **options), ({}, False))
        restarted, due = wifi.recovery_decision(state, now=1, runtime=True, connected=False, saved=True)
        self.assertFalse(due)
        self.assertEqual(restarted['since'], 1)

    def test_only_saved_infrastructure_profiles_are_configured(self):
        calls = []
        profiles = {'home': 'infrastructure\nwlan0\nyes', 'ap': 'ap\nwlan0\nyes',
                    'other': 'infrastructure\nwlan1\nyes', 'manual': 'infrastructure\nwlan0\nno', 'default': '\n\nyes'}

        def run(command, **kwargs):
            calls.append(command)
            if command[:4] == ['nmcli', '-t', '-f', 'UUID,TYPE']:
                out = '\n'.join(f'{key}:wifi' for key in profiles) + '\nwired:802-3-ethernet'
            elif command[:2] == ['nmcli', '-g']:
                out = profiles[command[-1]]
            else:
                out = ''
            return subprocess.CompletedProcess(command, 0, stdout=out)

        wifi.configure_connections(run)
        modifications = [cmd for cmd in calls if cmd[:3] == ['nmcli', 'connection', 'modify']]
        self.assertEqual(modifications, [['nmcli', 'connection', 'modify', 'uuid', 'home', 'connection.autoconnect-retries', '0'],
                                        ['nmcli', 'connection', 'modify', 'uuid', 'manual', 'connection.autoconnect-retries', '0'],
                                        ['nmcli', 'connection', 'modify', 'uuid', 'default', 'connection.autoconnect-retries', '0']])

    def test_runtime_outage_survives_oneshot_restart_and_failed_up_is_backed_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state.json'
            enabled = root / 'enabled'
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if command[:3] == ['nmcli', 'connection', 'up']:
                    raise subprocess.CalledProcessError(1, command, stderr='private network name')
                if 'GENERAL.STATE' in command:
                    out = '30 (disconnected)'
                elif command[0] == 'ip':
                    out = 'default dev eth0'
                elif 'UUID,TYPE' in command:
                    out = 'home:802-11-wireless'
                else:
                    out = 'infrastructure\nwlan0\nyes'
                return subprocess.CompletedProcess(command, 0, stdout=out)

            options = {'run': run, 'state_path': state, 'enabled_path': enabled, 'lock_path': root / 'mode.lock'}
            self.assertFalse(wifi.check_once(**options, clock=lambda: 10))
            self.assertTrue(wifi.check_once(**options, clock=lambda: 130))
            self.assertFalse(wifi.check_once(**options, clock=lambda: 160))
            ups = [cmd for cmd in calls if cmd[:3] == ['nmcli', 'connection', 'up']]
            self.assertEqual(len(ups), 1)
            self.assertEqual(json.loads(state.read_text())['next_try'], 250)
            enabled.touch()
            calls.clear()
            self.assertFalse(wifi.check_once(**options, clock=lambda: 300))
            self.assertEqual(calls, [])
            self.assertFalse(state.exists())

    def test_install_and_update_ship_timer_and_journal_config(self):
        root = Path(__file__).resolve().parents[1]
        timer = (root / 'systemd/messagebox-wifi-watchdog.timer').read_text()
        self.assertIn('OnUnitInactiveSec=30s', timer)
        self.assertIn('WantedBy=messagebox.target', timer)
        service = (root / 'systemd/messagebox-wifi-watchdog.service').read_text()
        self.assertIn('ConditionPathExists=!/etc/messagebox-onboarding/enabled', service)
        self.assertIn('RuntimeDirectoryPreserve=yes', service)
        journal = (root / 'config/journald.conf.d/messagebox.conf').read_text()
        self.assertIn('Storage=persistent', journal)
        self.assertIn('SystemMaxUse=50M', journal)
        callback = (root / 'scripts/commands/messagebox-comitup-state').read_text()
        self.assertIn('messagebox.wifi_watchdog --configure', callback)
