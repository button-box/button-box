import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from messagebox.cloud_claim import CloudClaim, CloudClaimClockError, CloudClaimError, setup_online_cue
from messagebox.cloud_device import CloudDeviceClient, atomic_json
from messagebox.onboarding.app import RequestError, watch_cloud_claim
from test_cloud_claim import FakeClient, ID, NOW
from test_cloud_device import _Response


class SetupCheckinTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = FakeClient()
        self.claim = CloudClaim(self.client, path=self.root / "claim.json", clock=lambda: NOW)
        self.result = {"claimed": False, "setup_code": "K7Q2MX",
                       "setup_url": "https://button.box/s/K7Q2MX",
                       "pending_claim": {"claim_id": ID, "expires_at": NOW + 600}}
        self.client.setup_checkin = mock.Mock(return_value=self.result)

    def test_device_checkin_uses_existing_auth_and_stable_id_without_registration(self):
        identity = {"device_id": "synthetic-device-001", "credential": "A" * 43}
        opener = mock.Mock(return_value=_Response(self.result))
        client = CloudDeviceClient("https://example.invalid/cloud-api/v1", identity, opener=opener)
        self.assertEqual(client.setup_checkin(), self.result)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.invalid/cloud-api/v1/device/setup-checkin")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + identity["credential"])
        self.assertEqual(json.loads(request.data), {"device_id": identity["device_id"]})
        self.assertEqual(opener.call_args.kwargs, {"timeout": 30})

    def test_qr_claim_uses_same_private_file_and_physical_confirmation(self):
        self.assertEqual(self.claim.setup_checkin(), self.result)
        document = json.loads(self.claim.path.read_text())
        self.assertEqual(document, {"claim_id": ID, "expires_at": NOW + 600,
                                    "whatsapp_url": "", "physical_confirmed": False})
        self.assertEqual(self.claim.path.stat().st_mode & 0o777, 0o660)
        self.assertEqual(self.client.register_calls, 0)
        self.assertTrue(self.claim.consume_press())
        self.assertEqual(self.client.confirm_calls, 1)
        self.claim.setup_checkin()
        self.assertTrue(json.loads(self.claim.path.read_text())["physical_confirmed"])
        # Restart uses the same physical-confirmation receipt.
        restarted = CloudClaim(self.client, path=self.claim.path, clock=lambda: NOW)
        self.assertTrue(restarted.consume_press())
        self.assertEqual(self.client.confirm_calls, 1)
        with self.assertRaises(CloudClaimError):
            self.claim.qr_svg()

    def test_newer_local_claim_and_cancellation_intent_are_preserved(self):
        for cancelled, expires in ((False, NOW + 601), (True, NOW + 10)):
            with self.subTest(cancelled=cancelled):
                prior = {"claim_id": "newerlocal1234567890", "expires_at": expires,
                         "whatsapp_url": "", "physical_confirmed": True,
                         "cancel_pending": cancelled}
                atomic_json(self.claim.path, prior)
                self.claim.setup_checkin()
                self.assertEqual(json.loads(self.claim.path.read_text()), prior)

    def test_browser_write_during_request_is_not_overwritten(self):
        def response():
            self.claim.start()
            return self.result
        self.client.setup_checkin.side_effect = response
        self.claim.setup_checkin()
        self.assertTrue(json.loads(self.claim.path.read_text())["whatsapp_url"])

    def test_missing_or_expired_pending_claim_does_not_create_or_clear_local_state(self):
        for pending in (None, {"claim_id": ID, "expires_at": NOW}):
            self.result["pending_claim"] = pending
            self.claim.setup_checkin()
            self.assertFalse(self.claim.path.exists())
        self.claim.start()
        original = self.claim.path.read_bytes()
        self.result["pending_claim"] = None
        self.claim.setup_checkin()
        self.assertEqual(self.claim.path.read_bytes(), original)

    def test_malformed_responses_and_unsynchronized_clock_fail_closed(self):
        for result in ({}, {"claimed": 1}, {"claimed": False}, {"claimed": False, "pending_claim": {}},
                       {"claimed": False, "pending_claim": {"claim_id": ID, "expires_at": True}},
                       {"claimed": False, "pending_claim": {**self.result["pending_claim"], "claim_token": "secret"}}):
            with self.subTest(result=result):
                self.client.setup_checkin.return_value = result
                with self.assertRaises(CloudClaimError):
                    self.claim.setup_checkin()
                self.assertFalse(self.claim.path.exists())
        self.client.setup_checkin.return_value = self.result
        self.claim.clock = lambda: NOW - 60
        with self.assertRaises(CloudClaimClockError):
            self.claim.setup_checkin()
        self.assertFalse(self.claim.path.exists())

    def test_claimed_removes_old_file_and_local_fallback_can_replace_qr_claim(self):
        self.claim.setup_checkin()
        self.assertTrue(self.claim.start()["whatsapp_url"])
        self.assertEqual(self.client.register_calls, 1)
        self.client.setup_checkin.return_value = {"claimed": True}
        self.assertEqual(self.claim.setup_checkin(), {"claimed": True})
        self.assertFalse(self.claim.path.exists())

    def test_online_receipt_is_once_per_session_across_polling_and_restart(self):
        def cue(**kwargs):
            return setup_online_cue(directory=self.root, session="session-1", **kwargs)
        cue()
        cue()
        self.assertTrue(cue(consume=True))
        cue()
        self.assertFalse(cue(consume=True))
        self.assertEqual((self.root / "setup-online.json").stat().st_mode & 0o777, 0o660)
        self.assertFalse(setup_online_cue(directory=self.root, session="session-2", consume=True))
        setup_online_cue(directory=self.root, session="session-2")
        self.assertTrue(setup_online_cue(directory=self.root, session="session-2", consume=True))

    def test_button_owner_consumes_without_reading_the_setup_marker(self):
        setup_online_cue(directory=self.root, session="session-1")
        with mock.patch("messagebox.cloud_claim.setup_session", side_effect=PermissionError("marker")):
            self.assertTrue(setup_online_cue(directory=self.root, consume=True))
            self.assertFalse(setup_online_cue(directory=self.root, consume=True))

    def test_backoff_is_capped_resets_on_success_and_stops_after_setup(self):
        request = mock.Mock(side_effect=[OSError("private")] * 4 + [False, OSError("private")])
        waits = []
        watch_cloud_claim(request, setup_pending=lambda: len(waits) < 6, sleep=waits.append)
        self.assertEqual(waits, [10, 20, 30, 30, 5, 10])
        self.assertEqual(request.call_count, 6)
        request.reset_mock()
        watch_cloud_claim(request, setup_pending=lambda: False)
        request.assert_not_called()

    def test_phase_gate_waits_at_normal_interval_without_error_backoff(self):
        request = mock.Mock(side_effect=RequestError("409 Conflict", "setup pending"))
        waits = []
        watch_cloud_claim(request, setup_pending=lambda: len(waits) < 2, sleep=waits.append)
        self.assertEqual(waits, [5, 5])


if __name__ == "__main__":
    unittest.main()
