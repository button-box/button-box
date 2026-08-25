import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import messagebox.dashboard.app as dashboard
from messagebox.settings import SettingsStore


class DashboardSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SettingsStore(
            Path(self.directory.name) / "settings.json", environ={"TZ": "UTC"}
        )

    def tearDown(self):
        self.directory.cleanup()

    def request(self, method, path, payload=None, headers=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.path = path
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "Host": "button-box.local",
            **(headers or {}),
        }
        handler.rfile = io.BytesIO(body)
        responses = []
        handler._send = lambda code, data, ctype="application/json": responses.append(
            (code, json.loads(data))
        )
        with patch.object(dashboard, "settings_store", return_value=self.store), patch.object(
            dashboard, "log_event"
        ):
            getattr(handler, f"do_{method}")()
        return responses[0]

    def test_get_update_and_stale_revision_conflict(self):
        code, payload = self.request("GET", "/api/settings")
        self.assertEqual(code, 200)
        document = payload["settings"]
        candidate = {
            key: value
            for key, value in document.items()
            if key not in {"version", "revision"}
        }
        candidate["arrival_signal"] = "lamp_only"
        code, saved = self.request(
            "PUT",
            "/api/settings",
            {"revision": document["revision"], "settings": candidate},
            {"Origin": "http://button-box.local"},
        )
        self.assertEqual(code, 200)
        self.assertEqual(saved["settings"]["arrival_signal"], "lamp_only")
        code, _conflict = self.request(
            "PUT",
            "/api/settings",
            {"revision": document["revision"], "settings": candidate},
            {"Origin": "http://button-box.local"},
        )
        self.assertEqual(code, 409)

    def test_cross_site_update_is_rejected_before_reading_body(self):
        code, payload = self.request(
            "PUT",
            "/api/settings",
            {},
            {"Origin": "http://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(code, 403)
        self.assertEqual(payload["error"], "cross-site request rejected")

    def test_whatsapp_health_uses_authenticated_status(self):
        self.assertTrue(
            dashboard.whatsapp_authenticated(
                {"success": True, "data": {"authenticated": True}}
            )
        )
        self.assertFalse(
            dashboard.whatsapp_authenticated(
                {"success": True, "data": {"connected": True}}
            )
        )


if __name__ == "__main__":
    unittest.main()
