import http.client
import json
import tempfile
import threading
import time
import unittest
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import messagebox.dashboard.app as dashboard


class HomeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a" and attributes.get("id"):
            self.links[attributes["id"]] = attributes


class DashboardHomeTests(unittest.TestCase):
    def setUp(self):
        class Handler(dashboard.Handler):
            local_host = "button-box-test.local"

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def home(self, transport):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            with patch.dict("os.environ", {"MSGBOX_TRANSPORT": transport}):
                connection.request("GET", "/", headers={"Host": "button-box-test.local"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "text/html; charset=utf-8")
                self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
                body = response.read().decode("utf-8")
        finally:
            connection.close()
        parser = HomeParser()
        parser.feed(body)
        self.assertFalse("__CLOUD_CONNECT_LINK__" in body, "cloud placeholder leaked into HTTP response")
        self.assertFalse("__MESSAGEBOX_URL__" in body, "URL placeholder leaked into HTTP response")
        if transport != "cloud":
            self.assertIn('id="setup-url">http://button-box-test.local/</code>', body)
        return body, parser.links

    def test_standalone_and_cloud_html_read_color_per_request(self):
        from messagebox.identity import BOX_COLORS

        with patch.object(dashboard, "read_box_color") as color_reader:
            for transport in ("wacli", "cloud"):
                for color in BOX_COLORS:
                    with self.subTest(transport=transport, color=color):
                        color_reader.return_value = color
                        body, _ = self.home(transport)
                        self.assertIn(f'<html lang="en" data-box-color="{color}">', body)
                        self.assertEqual(body.count("data-box-color="), 1)

    def test_cloud_runtime_opens_service_management_without_a_local_claim_link(self):
        body, links = self.home("cloud")
        link = links["home-cloud-dashboard"]
        self.assertEqual(link["href"], "https://button.box/dashboard")
        self.assertIn("Open dashboard", body)
        self.assertIn('id="cloud-wifi-form"', body)
        self.assertIn('/static/cloud-local.js', body)
        for legacy in ('id="nav-setup"', 'id="settings-form"', 'id="whatsapp-form"', 'id="listener-form"', '/static/app.js'):
            self.assertNotIn(legacy, body)
        self.assertNotIn('href="/cloud-connect"', body)

    def test_other_runtime_modes_remove_the_cloud_link_placeholder(self):
        for transport in ("wacli", "business", ""):
            with self.subTest(transport=transport):
                body, links = self.home(transport)
                self.assertNotIn("home-cloud-dashboard", links)
                self.assertNotIn('href="/cloud-connect"', body)

    def cloud_state(self, change=None, *, unclaimed=False):
        now = time.time()
        snapshot = {
            "box_id": "synthetic-box", "server_time": now,
            "verified_at": now, "verified_mono": time.monotonic(), "boot_id": "test-boot",
            "people": [{"id": "person-one"}, {"id": "person-two"}],
            "default_recipient_id": None,
            "entitlement": {"ingest": True, "send": True, "deliver": True, "until": now + 3600},
        }
        if change:
            change(snapshot)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "runtime.json"
            if not unclaimed:
                state_path.write_text(json.dumps({
                    "version": 1, "cursor": 0, "acks": {}, "seen": {}, "deleted": [],
                    "pending_nfc": {}, "pending_settings": {}, "snapshot": snapshot,
                }))
            with patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), patch.object(
                dashboard.cloud_runtime, "STATE_FILE", state_path
            ), patch.object(dashboard.cloud_runtime, "CONTACTS_FILE", root / "contacts.json"), patch.object(
                dashboard.cloud_runtime, "_current_boot_id", return_value="test-boot"
            ), patch.object(dashboard, "contacts_store") as standalone_contacts, patch.object(
                dashboard, "RecipientSetup"
            ) as standalone_proof, patch.object(
                dashboard.cloud_runtime.CloudDeviceClient, "from_environment"
            ) as identity_client, patch.object(dashboard, "read_box_id", return_value=None), patch.object(
                dashboard, "runtime_running", return_value=True
            ), patch.object(dashboard.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")) as command:
                connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
                try:
                    connection.request("GET", "/api/state", headers={"Host": "button-box-test.local"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    result = json.loads(response.read())
                finally:
                    connection.close()
                standalone_contacts.assert_not_called()
                standalone_proof.assert_not_called()
                identity_client.assert_not_called()
                self.assertFalse(any(args[0][0] == dashboard.WACLI_BIN for args, _kwargs in command.call_args_list))
            self.assertEqual(set(root.iterdir()), {state_path} if not unclaimed else set())
        self.assertNotIn("person-one", json.dumps(result))
        self.assertIsNone(result["box_id"])
        self.assertEqual(result["transport"], "cloud")
        self.assertEqual(result["setup"]["first_message"], "attention")
        return result

    def test_fresh_cloud_state_counts_current_people_and_requires_its_own_default(self):
        result = self.cloud_state()
        self.assertEqual(result["recipient_count"], 2)
        self.assertEqual(result["setup"]["whatsapp"], "complete")
        self.assertEqual(result["setup"]["recipient"], "attention")
        result = self.cloud_state(lambda snapshot: snapshot.update(default_recipient_id="person-two"))
        self.assertEqual(result["setup"]["recipient"], "complete")

    def test_cloud_stale_time_boot_and_unclaimed_never_inherit_standalone_readiness(self):
        for field, value in (
            ("verified_at", time.time() - 91),
            ("verified_mono", time.monotonic() - 91),
            ("server_time", time.time() - 91),
            ("boot_id", "previous-boot"),
        ):
            with self.subTest(field=field):
                result = self.cloud_state(lambda snapshot: snapshot.update({field: value}))
                self.assertEqual(result["recipient_count"], 0)
                self.assertEqual(result["setup"]["whatsapp"], "attention")
                self.assertEqual(result["setup"]["recipient"], "attention")
        result = self.cloud_state(unclaimed=True)
        self.assertEqual(result["recipient_count"], 0)
        self.assertEqual(result["setup"]["whatsapp"], "attention")

    def test_cloud_disabled_or_expired_entitlement_needs_attention(self):
        for change in (
            lambda snapshot: snapshot["entitlement"].update(send=False),
            lambda snapshot: snapshot["entitlement"].update(deliver=False),
            lambda snapshot: snapshot["entitlement"].update(until=time.time() - 1),
        ):
            with self.subTest(change=change):
                result = self.cloud_state(change)
                self.assertEqual(result["setup"]["whatsapp"], "attention")
                self.assertEqual(result["setup"]["recipient"], "attention")

    def test_malformed_saved_cloud_authorization_fails_closed_without_breaking_http(self):
        for field, value in (
            ("entitlement", None),
            ("entitlement", []),
            ("entitlement", {"ingest": True, "send": 1, "deliver": True}),
            ("entitlement", {"ingest": True, "send": True, "deliver": True, "until": "tomorrow"}),
            ("people", {}),
            ("people", [{"id": None}]),
            ("people", [{"id": "duplicate"}, {"id": "duplicate"}]),
            ("people", [{"id": f"person-{index}"} for index in range(101)]),
            ("default_recipient_id", "absent-person"),
            ("default_recipient_id", []),
        ):
            with self.subTest(field=field, value=value):
                result = self.cloud_state(lambda snapshot: snapshot.update({field: value}))
                self.assertEqual(result["recipient_count"], 0)
                self.assertEqual(result["setup"]["whatsapp"], "attention")
                self.assertEqual(result["setup"]["recipient"], "attention")

    def test_standalone_runtime_keeps_its_contact_pairing_and_message_proof(self):
        with patch.dict("os.environ", {"MSGBOX_TRANSPORT": "wacli"}), patch.object(
            dashboard, "contacts_store"
        ) as contacts, patch.object(dashboard, "RecipientSetup") as proof, patch.object(
            dashboard.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='{"Authenticated":true}')
        ), patch.object(dashboard, "runtime_running", return_value=True), patch.object(
            dashboard, "whatsapp_authenticated", return_value=True
        ), patch.object(dashboard.cloud_runtime, "read_snapshot") as cloud:
            contacts.return_value.public_view.return_value = {
                "contacts": {"synthetic-recipient": {}}, "default_recipient": "synthetic-recipient",
            }
            proof.return_value.public_state.return_value = {"status": "complete"}
            result = dashboard.runtime_state()
            self.assertEqual(result["recipient_count"], 1)
            self.assertEqual(result["setup"]["whatsapp"], "complete")
            self.assertEqual(result["setup"]["recipient"], "complete")
            self.assertEqual(result["setup"]["first_message"], "complete")
            cloud.assert_not_called()


if __name__ == "__main__":
    unittest.main()
