import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from messagebox.cloud_claim import CloudClaim, CloudClaimError
from messagebox.cloud_device import CloudDeviceError

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

    def test_registration_accepts_old_and_friendly_text_only(self):
        friendly = "Hi! I'd like to register my Button Box. Its code is " + TOKEN
        cases = [("claim " + TOKEN, True), (friendly, True),
                 ("join " + TOKEN, False), (friendly + " extra", False),
                 ("Hi! I'd like to register my Button Box. Its code is other", False)]
        for text, accepted in cases:
            with self.subTest(text=text):
                self.path.unlink(missing_ok=True)
                seen = []
                def register(capabilities, text=text):
                    seen.append(capabilities)
                    return {"claim_id": ID, "claim_token": TOKEN, "expires_at": NOW + 600,
                            "whatsapp_url": "https://wa.me/12025550101?text=" + quote(text)}
                self.client.register = register
                if accepted:
                    self.assertEqual(self.claim.start()["status"], "awaiting_button")
                else:
                    with self.assertRaises(CloudClaimError):
                        self.claim.start()
                    self.assertFalse(self.path.exists())
                self.assertIs(seen[0]["natural_registration_text"], True)

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
