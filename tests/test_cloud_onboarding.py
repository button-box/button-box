import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from messagebox.onboarding.app import create_app, _LazyApplication, post_worker_init
from messagebox.cloud_claim import CloudClaim, CloudClaimClockError, CloudClaimError
from messagebox.cloud_device import CloudDeviceError
from messagebox.onboarding.state import PROOFS, WHATSAPP_PROOFS, StateStore
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
        self.state = state
        self.cloud = FakeCloudClaim()
        self.whatsapp = mock.Mock()
        self.nfc = mock.Mock()
        self.adapter = mock.Mock()
        self.checker = mock.Mock(spec=[], return_value={"ok": True, "proof": sorted(PROOFS), "error": None})
        self.completions = []
        with mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}):
            self.app = create_app(mode="HOME", config={"device_id": "A7K2"},
                state_store=state, cloud_claim=self.cloud,
                whatsapp_client=self.whatsapp, nfc_client=self.nfc, adapter=self.adapter,
                completion_request=lambda **kwargs: self.completions.append(kwargs),
                caregiver_settings=SettingsStore(root / "settings.json", environ={"TZ": "UTC"}),
                connectivity_checker=self.checker, sleep=lambda seconds: None)

    def request(self, method, path, origin=None, body=b"", input_stream=None):
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "HTTP_HOST": HOST,
               "REMOTE_ADDR": "192.168.1.2", "CONTENT_LENGTH": str(len(body)),
               "wsgi.input": input_stream if input_stream is not None else io.BytesIO(body),
               "wsgi.url_scheme": "http"}
        if body or method == "POST":
            env["CONTENT_TYPE"] = "application/x-www-form-urlencoded"
        if origin:
            env["HTTP_ORIGIN"] = origin
        captured = {}
        def started(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)
        response = self.app(env, started)
        try:
            captured["body"] = b"".join(response)
        finally:
            if hasattr(response, "close"):
                response.close()
        return captured

    def run_watcher(self, *, claim=None, setup_pending=lambda: True, sleep=lambda _: None,
                    mode="HOME", transport="cloud"):
        from messagebox.onboarding.completion import request_completion
        path = Path(self.temp.name) / "completion-request.json"
        write = mock.Mock(side_effect=lambda **kwargs: request_completion(path, **kwargs))
        with mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": transport}), \
             mock.patch("messagebox.onboarding.app.threading.Thread") as thread:
            create_app(mode=mode, config={"device_id": "A7K2"},
                       state_store=self.state, cloud_claim=claim or self.cloud,
                       completion_request=write, start_claim_watcher=True,
                       setup_pending=setup_pending, sleep=sleep)
        if thread.called:
            thread.return_value.start.assert_called_once_with()
            self.assertTrue(thread.call_args.kwargs["daemon"])
            args = thread.call_args.kwargs
            args["target"](**args["kwargs"])
        return write, path, thread

    def test_watcher_claimed_without_local_claim_or_browser_writes_once(self):
        from test_cloud_claim import FakeClient, NOW
        client = FakeClient()
        client.claimed = True
        claim = CloudClaim(client, path=Path(self.temp.name) / "absent-claim.json", clock=lambda: NOW)
        pending = [True]
        with mock.patch.object(client, "claim", wraps=client.claim) as status:
            write, path, _ = self.run_watcher(claim=claim, setup_pending=lambda: pending[0],
                                            sleep=lambda _: pending.__setitem__(0, False))
        write.assert_called_once_with(transport="cloud")
        self.assertEqual(json.loads(path.read_text()), {"version": 1, "complete": True, "transport": "cloud"})
        status.assert_called_once_with()

    def test_watcher_waiting_and_status_errors_keep_polling_without_completion(self):
        for result in ({"status": "not_started"}, CloudClaimError("private"), OSError("private")):
            with self.subTest(result=type(result).__name__):
                self.cloud.status = mock.Mock(side_effect=result if isinstance(result, Exception) else None,
                                              return_value=result)
                pending = [True]
                write, path, _ = self.run_watcher(setup_pending=lambda: pending[0],
                                                sleep=lambda _: pending.__setitem__(0, False))
                write.assert_not_called()
                self.assertFalse(path.exists())
                self.cloud.status.assert_called_once_with()

    def test_watcher_retries_are_bounded_and_restart_before_completion_requests_again(self):
        self.cloud.claimed = True
        waits = []
        write, path, _ = self.run_watcher(sleep=waits.append)
        self.assertEqual(write.call_count, 3)
        self.assertEqual(waits, [60, 60])
        pending = [True]
        write, _, _ = self.run_watcher(setup_pending=lambda: pending[0],
                                     sleep=lambda _: pending.__setitem__(0, False))
        write.assert_called_once_with(transport="cloud")
        path.unlink()
        write, _, _ = self.run_watcher(setup_pending=lambda: False)
        write.assert_not_called()
        self.assertFalse(path.exists())
        self.state.reconcile_hotspot()
        pending = [True]
        with mock.patch.object(self.cloud, "status") as status:
            write, _, _ = self.run_watcher(setup_pending=lambda: pending[0],
                                         sleep=lambda _: pending.__setitem__(0, False))
        write.assert_not_called()
        status.assert_not_called()

    def test_watcher_is_opt_in_and_only_runs_in_cloud_home_mode(self):
        with mock.patch("messagebox.onboarding.app.threading.Thread") as thread:
            with mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}):
                create_app(mode="HOME", config={"device_id": "A7K2"}, state_store=self.state)
            thread.assert_not_called()
        for mode, transport in (("HOTSPOT", "cloud"), ("HOME", "wacli")):
            _, _, thread = self.run_watcher(mode=mode, transport=transport)
            thread.assert_not_called()

    def test_worker_hook_initializes_lazy_app_once_without_http(self):
        lazy = _LazyApplication()
        with mock.patch("messagebox.onboarding.app.app", lazy), \
             mock.patch("messagebox.onboarding.app.create_app") as create:
            post_worker_init(mock.Mock(wsgi=object()))
            create.assert_not_called()
            post_worker_init(mock.Mock(wsgi=lazy))
            post_worker_init(mock.Mock(wsgi=lazy))
        create.assert_called_once_with(start_claim_watcher=True)

    def test_claim_page_and_qr_are_local_and_start_requires_same_origin(self):
        home = self.request("GET", "/")
        self.assertEqual(home["status"], "302 Found")
        self.assertEqual(home["headers"]["Location"], "/cloud-connect")
        page = self.request("GET", "/cloud-connect")
        self.assertIn(b"Connect WhatsApp", page["body"])
        self.assertIn(b"cloud-connect.js", page["body"])
        self.assertIn(b'action="/wifi/change" method="post"', page["body"])
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

    def test_state_and_root_do_not_query_legacy_workers(self):
        for ready in (False, True):
            with self.subTest(ready=ready):
                if ready:
                    self.state.mark_whatsapp_ready(WHATSAPP_PROOFS)
                response = self.request("GET", "/api/state")
                self.assertEqual(response["status"], "200 OK")
                document = json.loads(response["body"])
                self.assertEqual(document["transport"], "cloud")
                self.assertEqual(set(document), {"phase", "box_id", "safe_error", "mode", "transport"})
                self.assertEqual(self.request("GET", "/")["headers"]["Location"], "/cloud-connect")
        self.assertEqual(self.whatsapp.mock_calls, [])
        self.assertEqual(self.nfc.mock_calls, [])
        self.checker.assert_not_called()

    def test_cloud_legacy_routes_reject_without_reading_body_or_mutating_state(self):
        reads = ("/api/data", "/api/recipients", "/api/nfc")
        writes = (
            "/whatsapp/pair/start", "/whatsapp/pair/cancel", "/whatsapp/unlink",
            "/recipients/refresh", "/recipients/select", "/recipients/select-number",
            "/recipients/add", "/recipients/add-number", "/recipients/remove",
            "/recipients/default", "/recipients/rename", "/recipients/defer",
            "/nfc/start", "/nfc/retry", "/nfc/reassign", "/nfc/assign", "/nfc/next", "/nfc/cancel",
        )
        stream = mock.Mock()
        stream.read.side_effect = AssertionError("Legacy Cloud route read a request body")
        before = self.state.load()
        for method, paths in (("GET", reads), ("POST", writes)):
            for path in paths:
                with self.subTest(method=method, path=path):
                    response = self.request(method, path, f"http://{HOST}", b"intent=done", stream)
                    self.assertEqual(response["status"], "409 Conflict")
        self.assertEqual(self.state.load(), before)
        self.assertEqual(self.whatsapp.mock_calls, [])
        self.assertEqual(self.nfc.mock_calls, [])
        stream.read.assert_not_called()
        self.assertEqual(self.completions, [])

    def test_home_root_waits_for_durable_wifi_proof_without_connectivity_probe(self):
        self.state.reconcile_hotspot()
        self.state.begin_connect("Home")
        self.state.mark_associated(self.state.load()["generation"])
        root = self.request("GET", "/")
        self.assertEqual(root["status"], "200 OK")
        self.assertIn(b"/static/app.js", root["body"])
        self.checker.assert_not_called()
        state = self.request("GET", "/api/state")
        self.assertEqual(json.loads(state["body"])["phase"], "WHATSAPP_PENDING")
        self.assertEqual(self.request("GET", "/")["headers"]["Location"], "/cloud-connect")

    def test_cloud_wifi_change_keeps_origin_gate_and_network_action(self):
        self.assertEqual(self.request("POST", "/wifi/change", "https://other.invalid")["status"], "403 Forbidden")
        self.adapter.delete_active_connection_once.assert_not_called()
        response = self.request("POST", "/wifi/change", f"http://{HOST}")
        self.assertEqual(response["status"], "202 Accepted")
        self.adapter.delete_active_connection_once.assert_called_once_with()
        self.assertEqual(self.whatsapp.mock_calls, [])

    def test_cloud_hotspot_still_serves_wifi_setup_and_scanning(self):
        with mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}):
            self.app = create_app(mode="HOTSPOT", config={"device_id": "A7K2"},
                state_store=self.state, adapter=self.adapter,
                whatsapp_client=self.whatsapp, nfc_client=self.nfc)
        self.adapter.scan_networks.return_value = [{"ssid": "Synthetic Wi-Fi", "security": "encrypted", "signal": 80}]
        root = self.request("GET", "/")
        self.assertEqual(root["status"], "200 OK")
        self.assertNotIn("Location", root["headers"])
        response = self.request("GET", "/api/networks")
        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(json.loads(response["body"])["networks"][0]["ssid"], "Synthetic Wi-Fi")
        self.assertEqual(json.loads(self.request("GET", "/api/state")["body"])["transport"], "cloud")
        self.assertEqual(self.whatsapp.mock_calls, [])
        self.assertEqual(self.nfc.mock_calls, [])

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

    def test_unsynchronized_clock_returns_safe_retry_reason_without_completing_setup(self):
        with mock.patch.object(self.cloud, "start", side_effect=CloudClaimClockError("cloud clock is not ready")):
            response = self.request("POST", "/api/cloud-claim/start", f"http://{HOST}")
        self.assertEqual(response["status"], "503 Service Unavailable")
        self.assertEqual(json.loads(response["body"]), {"error": "clock_not_ready"})
        self.assertEqual(self.completions, [])

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
        from test_cloud_claim import FakeClient, ID, NOW
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
