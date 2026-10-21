"""An omitted Cloud voice is retained, not a request to select a new default."""
import importlib.util
import json
import types
import unittest
from pathlib import Path

from messagebox import cloud_runtime
from messagebox.cloud_device import atomic_json
from messagebox.settings import SettingsStore
import test_cloud_runtime as runtime_tests
from test_cloud_runtime import NOW, OP

HISTORICAL = Path(__file__).parent / 'fixtures/pi-v0.3.12'


class CloudSettingsVoiceTests(unittest.TestCase):
    setUp = runtime_tests.CloudRuntimeTests.setUp

    def applied_jessica(self):
        self.runtime.settings = SettingsStore(self.root / 'settings.json', environ={'MSGBOX_TRANSPORT': 'cloud'})
        document, _ = self.runtime.settings.load()
        document = {**document, 'voice_pack': 'jessica'}
        self.runtime.settings._write_pair(document)
        atomic_json(cloud_runtime.APPLIED_FILE, {'revision': document['revision'], 'settings': document})
        self.runtime.heartbeat()
        return document

    def command(self, *, explicit=False):
        item = runtime_tests.CloudRuntimeTests.settings_command(self, master_volume_percent=41)
        if explicit:
            item['payload']['settings']['voice_pack'] = 'buttonbox'
        else:
            item['payload']['settings'].pop('voice_pack')
        return {**item, 'sequence': 1, 'created_at': NOW, 'expires_at': NOW + 60}

    def old_reader(self, expected):
        spec = importlib.util.spec_from_file_location('voice_legacy_settings', HISTORICAL / 'settings.py')
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        before = self.runtime.settings.path.read_bytes()
        loaded, warning = old.SettingsStore(self.runtime.settings.path, environ={'MSGBOX_TRANSPORT': 'cloud'}).load()
        self.assertFalse(warning)
        self.assertEqual(loaded, {**expected, 'voice_pack': 'jessica'})
        self.assertEqual(self.runtime.settings.path.read_bytes(), before)
        namespace = {'json': json, 'CloudRuntimeError': RuntimeError}
        exec((HISTORICAL / 'runtime_state.py.txt').read_text(), namespace)
        state = namespace['_load'](types.SimpleNamespace(state_path=self.runtime.state_path))
        self.assertEqual(state['pending_settings'], self.runtime.state['pending_settings'])

    def test_cloud_omission_keeps_jessica_through_restart_revision_replay_and_rollback(self):
        self.applied_jessica()
        item = self.command()
        self.runtime._command(item, NOW)
        document, warning = self.runtime.settings.load()
        self.assertFalse(warning)
        self.assertEqual((document['voice_pack'], document['revision']), ('jessica', 1))
        self.assertNotIn('voice_pack', item['payload']['settings'])  # Do not mutate the Cloud input.
        self.assertEqual(self.runtime.state['pending_settings'][item['operation_id']], document)
        self.old_reader(document)

        # Recreate the crash window after the settings write but before pending-state persistence.
        self.runtime.state['pending_settings'].clear()
        self.runtime._save()
        restarted = cloud_runtime.CloudRuntime(self.client, state_path=self.runtime.state_path,
            contacts_path=self.root / 'cloud-contacts.json', queue_dir=self.root / 'queue',
            outbox_dir=self.root / 'outbox', settings_path=self.root / 'settings.json',
            clock=lambda: NOW, monotonic=lambda: self.mono[0], boot_id='restart-test')
        restarted._command(item, NOW)
        self.assertEqual(restarted.settings.load()[0], document)
        atomic_json(cloud_runtime.APPLIED_FILE, {'revision': 1, 'settings': document})
        restarted._finish_settings()
        restarted.flush_acks()
        self.assertEqual(self.client.acks[-1]['state'], 'applied')
        # A different operation for the same revision must pass the value comparison too.
        restarted._command({**item, 'operation_id': OP}, NOW)
        restarted._finish_settings()
        restarted.flush_acks()
        self.assertEqual(self.client.acks[-1]['state'], 'applied')
        self.assertEqual(restarted.settings.load()[0], document)
        self.assertFalse(any(ack.get('error_code') == 'settings_revision_conflict' for ack in self.client.acks))

    def test_explicit_cloud_buttonbox_switches_and_old_reader_keeps_revision(self):
        self.applied_jessica()
        item = self.command(explicit=True)
        self.runtime._command(item, NOW)
        document, _ = self.runtime.settings.load()
        self.assertEqual((document['voice_pack'], document['revision']), ('buttonbox', 1))
        self.old_reader(document)  # Old code uses its Jessica audio fallback, without rewriting state.
        atomic_json(cloud_runtime.APPLIED_FILE, {'revision': 1, 'settings': document})
        self.runtime._finish_settings()
        self.runtime.flush_acks()
        self.assertEqual(self.client.acks[-1]['state'], 'applied')
        self.runtime._command({**item, 'operation_id': OP}, NOW)
        self.assertEqual(self.runtime.settings.load()[0], document)

    def test_omitted_cloud_voice_uses_applied_not_unapplied_desired_voice(self):
        self.applied_jessica()
        current, _ = self.runtime.settings.load()
        candidate = {key: value for key, value in current.items() if key not in {'version', 'revision'}}
        self.runtime.settings.update({**candidate, 'voice_pack': 'pirate'}, 0)
        self.runtime._command(self.command(), NOW)
        document, _ = self.runtime.settings.load()
        self.assertEqual((document['voice_pack'], document['revision']), ('jessica', 2))
        self.assertEqual(json.loads(cloud_runtime.APPLIED_FILE.read_text())['settings']['voice_pack'], 'jessica')

    def test_missing_or_damaged_applied_snapshot_cannot_select_a_default_voice(self):
        original = self.applied_jessica()
        item = self.command()
        for index, content in enumerate((None, 'not-json', '{"settings":[]}', '{"settings":{}}')):
            with self.subTest(content=content):
                cloud_runtime.APPLIED_FILE.unlink(missing_ok=True)
                if content is not None:
                    cloud_runtime.APPLIED_FILE.write_text(content)
                self.client.items = [{**item, 'operation_id': f'invalid-snapshot-{index}'}]
                self.runtime.poll_once()
                self.assertEqual(self.client.acks[-1]['state'], 'rejected')
                self.assertEqual(self.client.acks[-1]['error_code'], 'settings_revision_invalid')
                self.assertEqual(self.runtime.settings.load()[0], original)
                self.client.items = []
