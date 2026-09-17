import json
import io
import types
import tempfile
import unittest
from pathlib import Path

from messagebox.onboarding.app import create_app
from messagebox.onboarding.activity import setup_activity
from messagebox.onboarding.whatsapp import _PairingHandler, WhatsAppPairingClient
from unittest import mock
from messagebox.onboarding.state import StateStore
from test_onboarding_api import WSGIHarness, FakeAdapter, FakeWhatsApp, FakeNfc


class SetupActivityTests(unittest.TestCase):
    def test_activity_rpc_has_no_caller_controlled_path_or_auth_side_effects(self):
        payload = {
            "cards": {},
            "interactions": [],
            "queue": [],
            "recently_played": [],
            "hold": [],
            "trash": [],
        }
        engine = mock.Mock()
        engine.activity_state.return_value = payload
        handler = _PairingHandler.__new__(_PairingHandler)
        handler.server = types.SimpleNamespace(engine=engine)
        handler.rfile = io.BytesIO(b'{"action":"activity_state"}\n')
        handler.wfile = io.BytesIO()
        handler.handle()
        response = json.loads(handler.wfile.getvalue())
        self.assertTrue(response["ok"])
        engine.activity_state.assert_called_once_with()
        engine.start.assert_not_called()
        handler.rfile = io.BytesIO(b'{"action":"activity_state","path":"private"}\n')
        handler.wfile = io.BytesIO()
        handler.handle()
        self.assertFalse(json.loads(handler.wfile.getvalue())["ok"])
        self.assertEqual(engine.activity_state.call_count, 1)
        client = WhatsAppPairingClient()
        with mock.patch.object(client, "_request", return_value=payload) as request:
            self.assertEqual(client.activity_state(), payload)
            request.assert_called_once_with({"action": "activity_state"})

    def test_incomplete_setup_can_read_events_without_private_fields_or_state_changes(self):
        for mode in ("HOME", "HOTSPOT"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = StateStore(root / "state.json")
                store.initialize()
                before = store.load()
                events = root / "events.jsonl"
                events.write_text(json.dumps({"type": "sent", "ts": 1000, "flow": "standalone", "target": "private", "message_id": "secret"}) + '\n{"broken":\n[]\n')
                worker = FakeWhatsApp()
                worker.activity_state = lambda: setup_activity(events)
                client = WSGIHarness(create_app(mode=mode, config={"device_id": "A7K2"}, state_store=store, adapter=FakeAdapter(), whatsapp_client=worker, nfc_client=FakeNfc()))
                response = client.request("GET", "/api/data")
                self.assertEqual(response["status"], "200 OK")
                data = json.loads(response["body"])
                self.assertEqual(data["cards"]["sent_total"], 1)
                self.assertEqual(data["interactions"][0]["outcome_label"], "Accepted for sending")
                self.assertNotIn(b"private", response["body"])
                self.assertNotIn(b"secret", response["body"])
                self.assertEqual(store.load(), before)
                self.assertEqual(data["queue"], [])
                self.assertEqual(data["recently_played"], [])

    def test_missing_events_has_empty_history(self):
        with tempfile.TemporaryDirectory() as directory:
            data = setup_activity(Path(directory) / "missing.jsonl")
        self.assertEqual(data["interactions"], [])
        self.assertEqual(data["cards"]["sent_total"], 0)
