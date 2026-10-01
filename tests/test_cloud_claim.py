import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from messagebox.cloud_claim import CloudClaim, CloudClaimError
from messagebox.cloud_device import CloudDeviceClient, CloudDeviceError

NOW = 1_800_000_000
ID = "claim1234567890123456"
TOKEN = "claimtoken1234567890123456"
URL = "https://wa.me/12025550101?text=" + quote("claim " + TOKEN)


class FakeClient:
    def __init__(self):
        self.register_calls = 0
        self.confirm_calls = 0
        self.claimed = False
        self.confirmed = False
        self.cancel_calls = []

    def register(self, capabilities):
        self.register_calls += 1
        return {"claim_id": ID, "claim_token": TOKEN, "whatsapp_url": URL,
                "expires_at": NOW + 600}

    def claim(self):
        return {"claimed": self.claimed}

    def confirm_claim(self, claim_id):
        assert claim_id == ID
        self.confirm_calls += 1
        self.confirmed = True
        return {"claimed": self.claimed, "box_id": "box1234567890123456"}

    def cancel_claim(self, claim_id):
        self.cancel_calls.append(claim_id)
        return {"claim_id": claim_id, "claimed": self.claimed, "cancelled": not self.claimed}


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "claim.json"
        self.client = FakeClient()
        self.clock = lambda: NOW
        self.claim = CloudClaim(self.client, path=self.path, clock=self.clock)

    def test_resumable_claim_needs_physical_press_and_local_qr(self):
        self.assertEqual(self.claim.status()["status"], "not_started")
        started = self.claim.start()
        self.assertEqual(started["status"], "awaiting_button")
        self.assertEqual(self.claim.start(), started)
        self.assertEqual(self.client.register_calls, 1)
        self.assertNotIn(TOKEN, self.path.name)
        svg = self.claim.qr_svg()
        self.assertIn(b'<svg ', svg)
        self.assertNotIn(URL.encode(), svg)
        self.assertTrue(self.claim.consume_press())
        self.assertTrue(self.client.confirmed)
        self.assertEqual(self.claim.status()["status"], "waiting_for_whatsapp")
        self.assertTrue(self.claim.consume_press())
        self.assertEqual(self.client.confirm_calls, 1)
        self.client.claimed = True
        self.assertEqual(self.claim.status()["status"], "claimed")
        self.assertFalse(self.path.exists())

    def test_optional_nfc_health_access_does_not_block_registration(self):
        for observed, expected in [(PermissionError("private runtime directory"), False),
                                   (False, False), (True, True)]:
            with self.subTest(observed=type(observed).__name__, expected=expected):
                self.path.unlink(missing_ok=True)
                health = mock.Mock()
                if isinstance(observed, Exception):
                    health.exists.side_effect = observed
                else:
                    health.exists.return_value = observed
                register = mock.Mock(wraps=self.client.register)
                with mock.patch("messagebox.cloud_claim.NFC_HEALTH_FILE", health), \
                     mock.patch.object(self.client, "register", register):
                    self.assertEqual(self.claim.start()["status"], "awaiting_button")
                self.assertEqual(register.call_args.args[0]["nfc"], expected)
                self.assertTrue(self.path.exists())

    def test_registration_and_claim_write_failures_are_not_optional(self):
        self.client.register = mock.Mock(side_effect=CloudDeviceError("unavailable"))
        with self.assertRaises(CloudClaimError):
            self.claim.start()
        self.assertFalse(self.path.exists())
        self.client.register = FakeClient().register
        with mock.patch("messagebox.cloud_claim.atomic_json", side_effect=PermissionError("private")):
            with self.assertRaises(PermissionError):
                self.claim.start()
        self.assertFalse(self.path.exists())

    def test_expired_claim_does_not_consume_button_or_display_qr(self):
        self.claim.start()
        self.claim.clock = lambda: NOW + 601
        self.assertFalse(self.claim.consume_press())
        with self.assertRaises(CloudClaimError):
            self.claim.qr_svg()
        self.assertEqual(self.claim.status()["status"], "expired")

    def test_invalid_or_cross_origin_link_is_never_saved(self):
        self.client.register = lambda _capabilities: {
            "claim_id": ID, "claim_token": TOKEN, "whatsapp_url":
            "https://example.invalid/?text=" + quote("claim " + TOKEN),
            "expires_at": NOW + 600}
        with self.assertRaises(CloudClaimError):
            self.claim.start()
        self.assertFalse(self.path.exists())

    def test_confirmation_transport_failure_keeps_claim_mode(self):
        self.claim.start()
        self.client.confirm_claim = mock.Mock(side_effect=CloudDeviceError("unavailable"))
        self.assertTrue(self.claim.consume_press())
        self.assertEqual(self.claim.status()["status"], "awaiting_button")

    def test_cancellation_before_and_after_press_removes_only_confirmed_claim(self):
        for pressed in (False, True):
            with self.subTest(pressed=pressed):
                self.claim.start()
                if pressed:
                    self.claim.consume_press()
                self.assertEqual(self.claim.cancel(ID), {"status": "cancelled"})
                self.assertFalse(self.path.exists())
                self.assertFalse(self.claim.consume_press())
                with self.assertRaises(CloudClaimError):
                    self.claim.qr_svg()
                self.assertEqual(self.claim.cancel(ID), {"status": "cancelled"})
                self.assertEqual(self.client.cancel_calls[-2:], [ID, ID])

    def test_unconfirmed_cancellation_survives_restart_expiry_and_same_id_retry(self):
        self.claim.start()
        self.client.cancel_claim = mock.Mock(side_effect=CloudDeviceError("unavailable"))
        with self.assertRaises(CloudClaimError):
            self.claim.cancel(ID)
        restarted = CloudClaim(self.client, path=self.path, clock=lambda: NOW + 601)
        self.assertEqual(restarted.status(), {"status": "cancellation_pending", "claim_id": ID})
        self.assertEqual(restarted.start(), restarted.status())
        self.assertEqual(self.client.register_calls, 1)
        self.assertTrue(restarted.consume_press())
        self.assertEqual(self.client.confirm_calls, 0)
        with self.assertRaises(CloudClaimError):
            restarted.qr_svg()
        self.client.cancel_claim = FakeClient().cancel_claim
        self.assertEqual(restarted.cancel(ID), {"status": "cancelled"})
        self.assertFalse(restarted.consume_press())

    def test_invalid_cancellation_responses_keep_local_claim(self):
        for response in ({}, {"claim_id": ID, "claimed": False, "cancelled": False},
                         {"claim_id": ID, "claimed": True, "cancelled": True},
                         {"claim_id": ID, "claimed": 0, "cancelled": True},
                         {"claim_id": "differentclaim123456", "claimed": False, "cancelled": True}):
            with self.subTest(response=response):
                self.claim.start()
                self.client.cancel_claim = mock.Mock(return_value=response)
                with self.assertRaises(CloudClaimError):
                    self.claim.cancel(ID)
                self.assertTrue(self.path.exists())
                self.assertEqual(self.claim.status()["status"], "cancellation_pending")

    def test_stale_cancel_never_targets_or_removes_newer_claim(self):
        self.claim.start()
        before = self.path.read_bytes()
        with self.assertRaises(CloudClaimError):
            self.claim.cancel("differentclaim123456")
        self.assertEqual(self.client.cancel_calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_completed_ownership_is_reported_distinctly_during_cancellation(self):
        self.claim.start()
        self.client.claimed = True
        self.assertEqual(self.claim.cancel(ID), {"status": "claimed"})
        self.assertFalse(self.path.exists())

    def test_cancellation_write_failure_prevents_remote_request(self):
        self.claim.start()
        with mock.patch("messagebox.cloud_claim.atomic_json", side_effect=PermissionError("private")):
            with self.assertRaises(PermissionError):
                self.claim.cancel(ID)
        self.assertEqual(self.client.cancel_calls, [])
        self.assertEqual(self.claim.status()["status"], "awaiting_button")

    def test_old_server_without_cancel_route_retains_claim_for_explicit_retry(self):
        self.claim.start()
        requests = []
        def unsupported(request, *, timeout):
            requests.append(request)
            raise urllib.error.HTTPError(request.full_url, 404, "unsupported", {}, None)
        client = CloudDeviceClient("https://example.invalid/cloud-api/v1",
            {"device_id": "synthetic-device-001", "credential": "x" * 43}, opener=unsupported)
        self.client.cancel_claim = client.cancel_claim
        with self.assertRaises(CloudClaimError):
            self.claim.cancel(ID)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.claim.status()["status"], "cancellation_pending")
        self.assertTrue(self.claim.consume_press())
        self.assertEqual(len(requests), 1)

    def test_confirmed_remote_cancel_with_local_cleanup_failure_retries_same_claim(self):
        self.claim.start()
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("private")):
            with self.assertRaises(PermissionError):
                self.claim.cancel(ID)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.claim.status()["status"], "cancellation_pending")
        self.assertEqual(self.claim.cancel(ID), {"status": "cancelled"})
        self.assertEqual(self.client.cancel_calls, [ID, ID])

    def test_old_status_response_cannot_remove_newer_claim_after_cancellation(self):
        self.claim.start()
        new_id = "newclaim12345678901234"
        def old_poll():
            self.assertEqual(self.claim.cancel(ID), {"status": "cancelled"})
            self.client.register = lambda _: {"claim_id": new_id, "claim_token": TOKEN,
                "whatsapp_url": URL, "expires_at": NOW + 600}
            self.claim.start()
            return {"claimed": True}
        self.client.claim = old_poll
        self.assertEqual(self.claim.status()["claim_id"], new_id)
        self.assertEqual(self.claim._read()["claim_id"], new_id)
        self.assertTrue(self.path.exists())

    def test_existing_portal_lock_needs_no_chmod_for_button_user(self):
        self.claim.start()
        lock_inode = self.claim.lock_path.stat().st_ino
        real_chmod = os.fchmod
        def deny_other_owner(descriptor, mode):
            if os.fstat(descriptor).st_ino == lock_inode:
                raise PermissionError("other owner")
            return real_chmod(descriptor, mode)
        with mock.patch("messagebox.cloud_device.os.fchmod", side_effect=deny_other_owner):
            self.assertTrue(self.claim.consume_press())


if __name__ == "__main__":
    unittest.main()
