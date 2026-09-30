import io
import json
import unittest
from unittest.mock import Mock, patch

import messagebox.dashboard.app as dashboard


class CloudManagementTests(unittest.TestCase):
    def request(self, method, path, payload=None):
        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.path = path
        body = json.dumps(payload or {}).encode()
        handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        handler.rfile = io.BytesIO(body)
        handler._require_same_origin = lambda: True
        handler._require_trusted_host = lambda: True
        responses = []
        handler._send = lambda code, data, *_args: responses.append((code, json.loads(data)))
        getattr(handler, f"do_{method}")()
        return responses[0]

    def test_cloud_rejects_legacy_reads_before_accessing_the_account_or_store(self):
        with patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), patch.object(
            dashboard, "pairing_engine"
        ) as pairing, patch.object(dashboard, "contact_settings") as contacts, patch.object(
            dashboard, "nfc_router"
        ) as nfc:
            for path in ("/api/whatsapp", "/api/recipients", "/api/contacts?refresh=1", "/api/nfc-runtime"):
                with self.subTest(path=path):
                    code, result = self.request("GET", path)
                    self.assertEqual(code, 409)
                    self.assertEqual(result, {
                        "error": "Manage your connection and people in Button Box Cloud",
                        "management_url": "https://button.box/dashboard",
                    })
            pairing.assert_not_called()
            contacts.assert_not_called()
            nfc.assert_not_called()

    def test_cloud_rejects_legacy_mutations_without_reading_bodies_or_changing_stores(self):
        with patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), patch.object(
            dashboard, "pairing_engine"
        ) as pairing, patch.object(dashboard, "contacts_store") as contacts, patch.object(
            dashboard, "nfc_router"
        ) as nfc, patch.object(dashboard.Handler, "_form_body") as form:
            for path in (
                "/whatsapp/pair/start", "/whatsapp/pair/cancel", "/whatsapp/unlink",
                "/recipients/refresh", "/recipients/defer", "/recipients/select-number",
                "/recipients/add-number", "/recipients/add", "/recipients/select",
                "/recipients/remove", "/recipients/default", "/nfc/enroll",
                "/nfc/cancel-runtime", "/nfc/unpair-presented", "/api/contacts", "/api/listeners",
            ):
                with self.subTest(path=path):
                    self.assertEqual(self.request("POST", path)[0], 409)
            pairing.assert_not_called()
            contacts.assert_not_called()
            nfc.assert_not_called()
            form.assert_not_called()

    def test_wacli_keeps_its_pairing_and_recipient_contract(self):
        engine = Mock()
        engine.public_state.return_value = {"status": "ready", "phone_hint": "Synthetic account"}
        engine.recipient_list.return_value = {"recipients": []}
        engine.relink.return_value = {"status": "idle"}
        with patch.dict("os.environ", {"MSGBOX_TRANSPORT": "wacli"}), patch.object(
            dashboard, "pairing_engine", return_value=engine
        ), patch.object(dashboard.Handler, "_form_body", return_value={"confirm": "unlink"}):
            self.assertEqual(self.request("GET", "/api/whatsapp"), (200, engine.public_state.return_value))
            self.assertEqual(self.request("GET", "/api/recipients"), (200, engine.recipient_list.return_value))
            self.assertEqual(self.request("POST", "/whatsapp/unlink"), (200, {
                "mode": "RUNTIME", "phase": "WHATSAPP_PENDING", "whatsapp": engine.relink.return_value,
            }))
            engine.relink.assert_called_once_with()

    def test_cloud_normal_settings_contract_is_unchanged(self):
        payload = {"ok": True, "settings": {}, "attention": False}
        with patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), patch.object(
            dashboard, "settings_payload", return_value=payload
        ) as settings:
            self.assertEqual(self.request("GET", "/api/settings"), (200, payload))
            settings.assert_called_once_with()

    def test_cloud_wifi_status_and_claim_command_paths_are_outside_the_legacy_guard(self):
        payload = {"status": "idle"}
        with patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), patch.object(
            dashboard, "wifi_change_status", return_value=payload
        ):
            self.assertEqual(self.request("GET", "/api/wifi-change"), (200, payload))
            handler = dashboard.Handler.__new__(dashboard.Handler)
            for path in ("/api/cloud-claim", "/api/cloud-claim/start", "/api/cloud-claim/qr", "/v1/commands"):
                with self.subTest(path=path):
                    self.assertFalse(handler._reject_cloud_management(path))
