import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from messagebox.onboarding.app import create_app
from messagebox.cloud_claim import CloudClaim
from messagebox.cloud_device import CloudDeviceError
from messagebox.onboarding.state import PROOFS, StateStore
from messagebox.settings import SettingsStore

HOST = "message-box-A7K2.local"


class FakeCloudClaim:
    def __init__(self):
        self.started = 0
        self.claimed = False
        self.cancelled = []

    def status(self):
        return {"status": "claimed" if self.claimed else "not_started"}

    def start(self):
        self.started += 1
        return {"status": "awaiting_button", "whatsapp_url": "https://wa.me/12025550101?text=claim",
                "expires_at": 1_800_000_600}

    def qr_svg(self):
        return b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'

    def cancel(self, claim_id):
        self.cancelled.append(claim_id)
        return {"status": "claimed" if self.claimed else "cancelled"}


class CloudOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        state = StateStore(root / "state.json")
        state.initialize()
        state.begin_connect("Home")
        state.mark_associated(1)
        state.record_connectivity_result(PROOFS)
        self.cloud = FakeCloudClaim()
        self.completions = []
        with mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}):
            self.app = create_app(mode="HOME", config={"device_id": "A7K2"},
                state_store=state, cloud_claim=self.cloud,
                completion_request=lambda **kwargs: self.completions.append(kwargs),
                caregiver_settings=SettingsStore(root / "settings.json", environ={"TZ": "UTC"}),
                connectivity_checker=lambda: {"ok": True, "proof": sorted(PROOFS), "error": None})

    def request(self, method, path, origin=None, body=b""):
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "HTTP_HOST": HOST,
               "REMOTE_ADDR": "192.168.1.2", "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
               "wsgi.url_scheme": "http"}
        if body:
            env["CONTENT_TYPE"] = "application/x-www-form-urlencoded"
        if origin:
            env["HTTP_ORIGIN"] = origin
        captured = {}
        def started(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)
        captured["body"] = b"".join(self.app(env, started))
        return captured

    def test_claim_page_and_qr_are_local_and_start_requires_same_origin(self):
        home = self.request("GET", "/")
        self.assertIn(b"/cloud-connect", home["body"])
        page = self.request("GET", "/cloud-connect")
        self.assertIn(b"Get a connection link", page["body"])
        self.assertIn(b"cloud-connect.js", page["body"])
        self.assertEqual(self.request("GET", "/api/cloud-claim")["status"], "200 OK")
        denied = self.request("POST", "/api/cloud-claim/start", "https://other.invalid")
        self.assertNotEqual(denied["status"], "200 OK")
        self.assertEqual(self.cloud.started, 0)
        started = self.request("POST", "/api/cloud-claim/start", f"http://{HOST}")
        self.assertEqual(started["status"], "200 OK")
        self.assertEqual(json.loads(started["body"])["status"], "awaiting_button")
        self.assertEqual(self.cloud.started, 1)
        qr = self.request("GET", "/api/cloud-claim/qr")
        self.assertEqual(qr["status"], "200 OK")
        self.assertEqual(qr["headers"]["Content-Type"], "image/svg+xml; charset=utf-8")
        self.assertIn(b"<svg", qr["body"])

    def test_completion_requires_live_claim_and_preserves_legacy_setup_gate(self):
        body = b"intent=done"
        denied = self.request("POST", "/onboarding/complete", f"http://{HOST}", body)
        self.assertEqual(denied["status"], "409 Conflict")
        self.assertEqual(self.completions, [])
        self.cloud.claimed = True
        denied_origin = self.request("POST", "/onboarding/complete", "https://other.invalid", body)
        self.assertNotEqual(denied_origin["status"], "202 Accepted")
        completed = self.request("POST", "/onboarding/complete", f"http://{HOST}", body)
        self.assertEqual(completed["status"], "202 Accepted")
        self.assertEqual(self.completions, [{"transport": "cloud"}])

    def test_cancel_route_requires_same_origin_exact_form_and_post(self):
        body = b"claim_id=synthetic-claim-001"
        self.assertIn(b'Cancel connection', self.request("GET", "/cloud-connect")["body"])
        self.assertNotEqual(self.request("POST", "/api/cloud-claim/cancel", "https://other.invalid", body)["status"], "200 OK")
        self.assertEqual(self.request("GET", "/api/cloud-claim/cancel")["status"], "405 Method Not Allowed")
        self.assertEqual(self.request("POST", "/api/cloud-claim/cancel", f"http://{HOST}", b"other=value")["status"], "400 Bad Request")
        self.assertEqual(self.cloud.cancelled, [])
        cancelled = self.request("POST", "/api/cloud-claim/cancel", f"http://{HOST}", body)
        self.assertEqual(cancelled["status"], "200 OK")
        self.assertEqual(json.loads(cancelled["body"]), {"status": "cancelled"})
        self.assertEqual(self.cloud.cancelled, ["synthetic-claim-001"])
        self.cloud.claimed = True
        self.assertEqual(json.loads(self.request("POST", "/api/cloud-claim/cancel", f"http://{HOST}", body)["body"]), {"status": "claimed"})

    def test_actual_claim_cancel_route_readback_and_uncertain_retry_use_one_synthetic_claim(self):
        from tests.test_cloud_claim import FakeClient, ID, NOW
        root = Path(self.temp.name)
        client = FakeClient()
        claim = CloudClaim(client, path=root / "claim.json", clock=lambda: NOW)
        with mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}):
            self.app = create_app(mode="HOME", config={"device_id": "A7K2"},
                state_store=StateStore(root / "state.json"), cloud_claim=claim,
                completion_request=lambda **kwargs: self.completions.append(kwargs),
                caregiver_settings=SettingsStore(root / "settings.json", environ={"TZ": "UTC"}),
                connectivity_checker=lambda: {"ok": True, "proof": sorted(PROOFS), "error": None})
        started = self.request("POST", "/api/cloud-claim/start", f"http://{HOST}")
        self.assertEqual(started["status"], "200 OK")
        self.assertEqual(json.loads(started["body"])["claim_id"], ID)
        self.assertTrue(claim.consume_press())
        body = ("claim_id=" + ID).encode()
        with mock.patch.object(client, "cancel_claim", side_effect=CloudDeviceError("unavailable")):
            uncertain = self.request("POST", "/api/cloud-claim/cancel", f"http://{HOST}", body)
        self.assertEqual(uncertain["status"], "503 Service Unavailable")
        self.assertEqual(json.loads(self.request("GET", "/api/cloud-claim")["body"]),
            {"status": "cancellation_pending", "claim_id": ID})
        self.assertEqual(self.request("GET", "/api/cloud-claim/qr")["status"], "404 Not Found")
        self.assertTrue(claim.consume_press())
        cancelled = self.request("POST", "/api/cloud-claim/cancel", f"http://{HOST}", body)
        self.assertEqual(cancelled["status"], "200 OK")
        self.assertEqual(json.loads(cancelled["body"]), {"status": "cancelled"})
        self.assertEqual(json.loads(self.request("GET", "/api/cloud-claim")["body"]), {"status": "not_started"})
        self.assertFalse(claim.consume_press())
        self.assertEqual(client.register_calls, 1)
        self.assertEqual(client.confirm_calls, 1)
        self.assertEqual(client.cancel_calls, [ID])
        self.assertEqual(self.completions, [])


if __name__ == "__main__":
    unittest.main()
