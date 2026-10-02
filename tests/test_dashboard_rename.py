import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import Mock, patch

import messagebox.dashboard.app as dashboard
from messagebox.contacts import ContactStore
from messagebox.onboarding.recipients import RecipientSetup
from messagebox.onboarding.whatsapp import PairingEngine


PERSON = "15551234567@s.whatsapp.net"
CARD = "04:A1:00:FF"


class DashboardRenameTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.contacts_path = root / "contacts.json"
        self.setup = RecipientSetup(
            state_path=root / "recipients.json", contacts_path=self.contacts_path,
            events_path=root / "events.jsonl", voice_request_path=root / "voice.json",
        )
        self.token = self.setup.select_phone("+15551234567", name="Synthetic name")["default"]["token"]
        ContactStore(self.contacts_path).assign_card(PERSON, CARD)
        # Exercise real readiness and recipient methods without starting pairing
        # recovery, subprocesses or sync service management.
        self.engine = PairingEngine.__new__(PairingEngine)
        self.engine.clock = lambda: 1000.0
        self.engine.state_path = root / "pairing.json"
        self.engine.recipients = self.setup
        state = self.engine._default_state()
        state.update(status="ready", phone_hint="Linked account", eligible_count=1)
        self.engine.state_path.write_text(json.dumps(state))
        for guard in (patch.dict(os.environ, {"MSGBOX_TRANSPORT": "wacli"}),
                      patch.object(dashboard, "pairing_engine", return_value=self.engine)):
            guard.start()
            self.addCleanup(guard.stop)

    def request(self, payload, *, host="synthetic-box.local", origin=None,
                content_type="application/x-www-form-urlencoded", raw=None):
        body = urlencode(payload).encode() if raw is None else raw
        headers = ["POST /recipients/rename HTTP/1.1", f"Host: {host}",
                   f"Content-Type: {content_type}", f"Content-Length: {len(body)}",
                   "Connection: close"]
        if origin is not None:
            headers.append(f"Origin: {origin}")
        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.rfile = io.BytesIO(("\r\n".join(headers) + "\r\n\r\n").encode() + body)
        handler.wfile = io.BytesIO()
        handler.client_address = ("192.168.1.20", 12345)
        handler.local_host = "synthetic-box.local"
        handler.tailscale_host = None
        handler.log_message = lambda *_args: None
        handler.handle_one_request()
        head, response = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
        return int(head.split(b" ", 2)[1]), json.loads(response)

    def test_http_rename_persists_normalized_name_preserving_default_identity_and_cards(self):
        before = ContactStore(self.contacts_path).load()
        code, result = self.request({"token": self.token, "name": " Cafe\u0301 "})
        self.assertEqual(code, 200)
        person = next(item for item in result["recipients"] if item["token"] == self.token)
        self.assertEqual(person["label"], "Café")
        after = ContactStore(self.contacts_path).load()
        self.assertEqual(after["default_recipient"], before["default_recipient"])
        self.assertEqual(set(after["contacts"]), set(before["contacts"]))
        self.assertEqual(after["contacts"][PERSON]["card_uids"], before["contacts"][PERSON]["card_uids"])
        reloaded = RecipientSetup(state_path=self.setup.state_path, contacts_path=self.contacts_path,
                                 events_path=self.setup.events_path, voice_request_path=self.setup.voice_request_path)
        self.assertEqual(reloaded.public_state(), result)
        self.assertNotIn(PERSON, json.dumps(result))
        self.assertNotIn(CARD, json.dumps(result))

    def test_http_blank_name_keeps_phone_label_fallback_and_routing(self):
        code, result = self.request({"token": self.token, "name": "   "})
        self.assertEqual(code, 200)
        person = next(item for item in result["recipients"] if item["token"] == self.token)
        self.assertEqual(person["label"], person["secondary_label"])
        stored = ContactStore(self.contacts_path).load()
        self.assertEqual(stored["default_recipient"], PERSON)
        self.assertEqual(stored["contacts"][PERSON]["card_uids"], [CARD])

    def test_http_invalid_name_or_stale_token_does_not_mutate_state(self):
        before = (self.contacts_path.read_bytes(), self.setup.state_path.read_bytes())
        for payload, error in [
            ({"token": self.token, "name": "x" * 81}, "recipient name is invalid"),
            ({"token": self.token, "name": "unsafe\nname"}, "recipient name is invalid"),
            ({"token": "stale-recipient-token", "name": "Synthetic renamed"},
             "recipient is no longer available; refresh and try again"),
        ]:
            with self.subTest(payload=payload):
                code, response = self.request(payload)
                self.assertEqual(code, 409)
                self.assertEqual(response["error"], error)
                self.assertEqual((self.contacts_path.read_bytes(), self.setup.state_path.read_bytes()), before)

    def test_http_exact_form_schema_and_parser_guards_remain_required(self):
        operation = Mock(wraps=self.engine.recipient_rename)
        with patch.object(self.engine, "recipient_rename", operation):
            for payload in [{"token": self.token}, {"name": "Synthetic renamed"},
                            {"token": self.token, "name": "Synthetic renamed", "extra": "field"}]:
                self.assertEqual(self.request(payload)[0], 409)
            self.assertEqual(self.request({}, content_type="application/json", raw=b"{}")[0], 415)
            self.assertEqual(self.request({}, raw=b"token=one&token=two&name=Synthetic")[0], 400)
        operation.assert_not_called()

    def test_http_cloud_and_cross_origin_guards_run_before_body_or_engine(self):
        before = (self.contacts_path.read_bytes(), self.setup.state_path.read_bytes())
        with patch.object(dashboard, "pairing_engine") as engine, \
             patch.object(dashboard.Handler, "_form_body") as form:
            with patch.dict(os.environ, {"MSGBOX_TRANSPORT": "cloud"}):
                code, response = self.request({"token": self.token, "name": "Synthetic renamed"})
                self.assertEqual(code, 409)
                self.assertEqual(response["management_url"], "https://button.box/dashboard")
            self.assertEqual(self.request({}, origin="https://different.example")[0], 403)
            self.assertEqual(self.request({}, host="untrusted.example")[0], 400)
            engine.assert_not_called()
            form.assert_not_called()
        self.assertEqual((self.contacts_path.read_bytes(), self.setup.state_path.read_bytes()), before)

    def test_http_rename_keeps_pairing_ready_and_storage_error_boundaries(self):
        before = ContactStore(self.contacts_path).load()
        self.engine.state_path.write_text(json.dumps(self.engine._default_state()))
        self.assertEqual(self.request({"token": self.token, "name": "Synthetic renamed"}),
                         (409, {"error": "whatsapp not ready"}))
        self.assertEqual(ContactStore(self.contacts_path).load(), before)
        with patch.object(dashboard, "pairing_engine", side_effect=OSError("synthetic unavailable")):
            self.assertEqual(self.request({"token": self.token, "name": "Synthetic renamed"}),
                             (503, {"error": "Recipient setup is unavailable"}))
