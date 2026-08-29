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

    def request(
        self,
        method,
        path,
        payload=None,
        headers=None,
        *,
        remote_addr="192.168.1.20",
        tailscale_host=None,
    ):
        body = json.dumps(payload).encode() if payload is not None else b""
        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.path = path
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "Host": "button-box.local",
            **(headers or {}),
        }
        handler.client_address = (remote_addr, 12345)
        handler.local_host = "button-box.local"
        handler.tailscale_host = tailscale_host
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

    def test_unrecognized_dashboard_host_is_rejected(self):
        code, payload = self.request(
            "GET", "/api/settings", headers={"Host": "attacker.example"}
        )
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"], "invalid dashboard address")

    def test_tailnet_https_same_origin_is_accepted_only_from_loopback_proxy(self):
        tailnet = "button-box-a7.example-tailnet.ts.net"
        code, loaded = self.request(
            "GET",
            "/api/settings",
            headers={"Host": tailnet, "X-Forwarded-Proto": "https"},
            remote_addr="127.0.0.1",
            tailscale_host=tailnet,
        )
        self.assertEqual(code, 200)
        document = loaded["settings"]
        candidate = {
            key: value
            for key, value in document.items()
            if key not in {"version", "revision"}
        }
        code, payload = self.request(
            "PUT",
            "/api/settings",
            {"revision": document["revision"], "settings": candidate},
            {
                "Host": tailnet,
                "Origin": f"https://{tailnet}",
                "X-Forwarded-Proto": "https",
            },
            remote_addr="127.0.0.1",
            tailscale_host=tailnet,
        )
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])

        code, payload = self.request(
            "PUT",
            "/api/settings",
            {},
            {
                "Host": tailnet,
                "Origin": f"https://{tailnet}",
                "X-Forwarded-Proto": "https",
            },
            remote_addr="192.168.1.20",
            tailscale_host=tailnet,
        )
        self.assertEqual(code, 400)
        self.assertEqual(payload["error"], "invalid dashboard address")

    def test_runtime_adds_loopback_listener_only_with_tailnet(self):
        with patch.object(dashboard, "resolve_bind", return_value="192.168.1.140"):
            self.assertEqual(
                dashboard.dashboard_bind_addresses("wlan0", None),
                ["192.168.1.140"],
            )
            self.assertEqual(
                dashboard.dashboard_bind_addresses(
                    "wlan0", "button-box-a7.example-tailnet.ts.net"
                ),
                ["192.168.1.140", "127.0.0.1"],
            )

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
