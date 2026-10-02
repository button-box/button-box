import os
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest import mock

from messagebox.dashboard import app


class CloudLocalPageTests(unittest.TestCase):
    def test_cloud_state_preserves_transport_without_legacy_discovery(self):
        with mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": "cloud"}), mock.patch.object(app, "contacts_store") as contacts, mock.patch.object(app, "RecipientSetup") as recipients, mock.patch.object(app.subprocess, "run") as command:
            state = app.runtime_state()
            self.assertEqual(state["mode"], "RUNTIME")
            self.assertEqual(state["transport"], "cloud")
            contacts.assert_not_called()
            recipients.assert_not_called()
            command.assert_not_called()

    def test_cloud_has_one_management_destination_and_standalone_keeps_its_dashboard(self):
        class TestHandler(app.Handler):
            local_host = "button-box-test.local"
            tailscale_host = ""

        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for transport in ["cloud", "wacli"]:
                with self.subTest(transport=transport), mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": transport}):
                    request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/", headers={"Host": "button-box-test.local"})
                    with urllib.request.urlopen(request) as response:
                        body = response.read()
                    if transport == "cloud":
                        self.assertIn(b"cloud-local.js", body)
                        self.assertIn(b"https://button.box/dashboard", body)
                        self.assertIn(b"Change Wi-Fi", body)
                        self.assertNotIn(b"primary-nav", body)
                        self.assertNotIn(b"whatsapp-phone", body)
                        with mock.patch.object(app, "pairing_engine") as pairing:
                            for method, path in [("GET", "/api/whatsapp"), ("POST", "/whatsapp/unlink"), ("GET", "/api/recipients")]:
                                request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=b"" if method == "POST" else None, method=method, headers={"Host": "button-box-test.local", "Origin": "http://button-box-test.local"})
                                with self.assertRaises(urllib.error.HTTPError) as error:
                                    urllib.request.urlopen(request)
                                self.assertEqual(error.exception.code, 409)
                            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/whatsapp/unlink", data=b"", headers={"Host": "button-box-test.local", "Origin": "https://other.invalid"})
                            with self.assertRaises(urllib.error.HTTPError) as error:
                                urllib.request.urlopen(request)
                            self.assertEqual(error.exception.code, 403)
                            pairing.assert_not_called()
                    else:
                        self.assertIn(b"primary-nav", body)
                        self.assertIn(b"whatsapp-phone", body)
                        self.assertNotIn(b"cloud-local.js", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
