import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from messagebox.onboarding.app import create_app
from messagebox.onboarding.comitup_adapter import ComitupError
from messagebox.onboarding.state import PROOFS, WHATSAPP_PROOFS, StateStore


HOST = "message-box-A7K2.local"


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        self.value += 1
        return self.value


class FakeAdapter:
    def __init__(self, state="HOTSPOT"):
        self.state = state
        self.calls = []
        self.networks = [
            {"ssid": "Home", "security": "encrypted", "signal": 82},
            {"ssid": "Cafe", "security": "unencrypted", "signal": 40},
        ]

    def scan_networks(self):
        self.calls.append(("scan",))
        return self.networks

    def connect_once(self, ssid, password):
        self.calls.append(("connect", ssid, password))

    def get_stable_state(self):
        self.calls.append(("state",))
        return {"state": self.state, "connection": "Home" if self.state == "CONNECTED" else ""}

    def delete_active_connection_once(self):
        self.calls.append(("delete",))


class FailingDeleteAdapter(FakeAdapter):
    def delete_active_connection_once(self):
        self.calls.append(("delete",))
        raise ComitupError("delete failed")


class FakeChecker:
    def __init__(self, result=None):
        self.calls = 0
        self.result = result or {
            "ok": True,
            "proof": sorted(PROOFS),
            "error": None,
            "attempts": 1,
        }

    def check(self):
        self.calls += 1
        return self.result


class FakeWhatsApp:
    def __init__(self, state=None):
        self.state = state or {
            "status": "idle",
            "pairing_code": None,
            "phone_hint": None,
            "eligible_count": 0,
            "safe_error": None,
            "attempt": 0,
        }
        self.calls = []
        self.logout_succeeds = True

    def status(self):
        self.calls.append(("status",))
        return dict(self.state)

    def start(self, phone):
        self.calls.append(("start", phone))
        self.state.update(status="starting", pairing_code=None, safe_error=None)
        return dict(self.state)

    def cancel(self):
        self.calls.append(("cancel",))
        self.state.update(status="idle", pairing_code=None, safe_error=None)
        return dict(self.state)

    def unlink(self):
        self.calls.append(("unlink",))
        if self.logout_succeeds:
            self.state.update(
                status="idle",
                phone_hint=None,
                eligible_count=0,
                safe_error=None,
            )
        else:
            self.state["safe_error"] = "UNLINK_FAILED"
        return dict(self.state)


class WSGIHarness:
    def __init__(self, application):
        self.application = application

    def request(
        self,
        method,
        path,
        *,
        host=HOST,
        body=b"",
        content_type="",
        headers=None,
        remote_addr="10.41.0.100",
        close=True,
    ):
        if isinstance(body, str):
            body = body.encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTP_HOST": host,
            "REMOTE_ADDR": remote_addr,
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "wsgi.url_scheme": "http",
        }
        for name, value in (headers or {}).items():
            environ["HTTP_" + name.upper().replace("-", "_")] = value
        captured = {}

        def start_response(status, response_headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = response_headers

        iterable = self.application(environ, start_response)
        response_body = b"".join(iterable)
        if close and hasattr(iterable, "close"):
            iterable.close()
        captured["body"] = response_body
        captured["iterable"] = iterable
        return captured

    def form(self, method, path, fields, **kwargs):
        return self.request(
            method,
            path,
            body=urlencode(fields),
            content_type="application/x-www-form-urlencoded",
            **kwargs,
        )

    def json(self, method, path, document, **kwargs):
        return self.request(
            method,
            path,
            body=json.dumps(document),
            content_type="application/json",
            **kwargs,
        )


def header(response, name):
    for candidate, value in response["headers"]:
        if candidate.lower() == name.lower():
            return value
    return None


class OnboardingAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.state_path = Path(self.directory.name) / "state.json"
        self.store = StateStore(self.state_path, clock=self.clock)
        self.adapter = FakeAdapter()
        self.checker = FakeChecker()
        self.sleeps = []
        self.app = create_app(
            mode="HOTSPOT",
            config={"device_id": "A7K2"},
            state_store=self.store,
            adapter=self.adapter,
            connectivity_checker=self.checker,
            clock=self.clock,
            sleep=self.sleeps.append,
            handoff_delay=0.25,
        )
        self.client = WSGIHarness(self.app)

    def tearDown(self):
        self.directory.cleanup()

    def home_pairing_client(self, whatsapp=None):
        path = Path(self.directory.name) / "whatsapp-home.json"
        store = StateStore(path, clock=self.clock)
        store.initialize()
        store.begin_connect("Home")
        store.mark_associated(1)
        store.record_connectivity_result(PROOFS)
        worker = whatsapp or FakeWhatsApp()
        application = create_app(
            mode="HOME",
            config={"device_id": "A7K2"},
            state_store=store,
            adapter=FakeAdapter("CONNECTED"),
            connectivity_checker=self.checker,
            whatsapp_client=worker,
            clock=self.clock,
        )
        return WSGIHarness(application), store, worker

    def state(self):
        response = self.client.request("GET", "/api/state")
        return response, json.loads(response["body"])

    def test_root_is_local_asset_page_with_security_headers(self):
        response = self.client.request("GET", "/")
        self.assertEqual(response["status"], "200 OK")
        self.assertIn(b"Choose home Wi-Fi", response["body"])
        self.assertIn(f"http://{HOST}/".encode(), response["body"])
        self.assertNotIn(b"__MESSAGEBOX_URL__", response["body"])
        self.assertIsNone(header(response, "Set-Cookie"))
        self.assertIn("default-src 'self'", header(response, "Content-Security-Policy"))
        self.assertEqual(header(response, "X-Frame-Options"), "DENY")
        self.assertNotIn(b"<style", response["body"])
        self.assertNotIn(b"<script>", response["body"])

    def test_hotspot_ip_is_allowed_and_other_hosts_redirect_to_it(self):
        response = self.client.request("GET", "/api/state", host="10.41.0.1")
        self.assertEqual(response["status"], "200 OK")
        accepted = self.client.form(
            "POST",
            "/wifi/connect",
            {"ssid": "Cafe", "security": "open", "password": ""},
            host="10.41.0.1",
            close=False,
        )
        self.assertEqual(accepted["status"], "202 Accepted")
        accepted["iterable"].close()

        response = self.client.request("GET", "/api/state", host="captive.example")
        self.assertEqual(response["status"], "302 Found")
        self.assertEqual(header(response, "Location"), "http://10.41.0.1/api/state")
        response = self.client.form(
            "POST", "/wifi/connect", {}, host="captive.example"
        )
        self.assertEqual(response["status"], "400 Bad Request")
        self.assertIsNone(header(response, "Set-Cookie"))

    def test_captive_browser_can_submit_wifi_from_cross_site_context(self):
        accepted = self.client.form(
            "POST",
            "/wifi/connect",
            {"ssid": "Cafe", "security": "open", "password": ""},
            headers={"Origin": "http://attacker.example", "Sec-Fetch-Site": "cross-site"},
            close=False,
        )
        self.assertEqual(accepted["status"], "202 Accepted")
        accepted["iterable"].close()

    def test_network_scan_is_available_without_a_cookie(self):
        response = self.client.request("GET", "/api/networks")
        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(json.loads(response["body"])["networks"], self.adapter.networks)

    def test_state_is_public_sanitized_and_has_no_session_fields(self):
        response = self.client.request(
            "GET", "/api/state", headers={"Cookie": "messagebox_session=obsolete"}
        )
        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(
            set(json.loads(response["body"])),
            {"mode", "phase", "safe_error", "whatsapp"},
        )
        self.assertIsNone(header(response, "Set-Cookie"))

    def test_same_origin_browser_mutation_is_allowed(self):
        response = self.client.form(
            "POST",
            "/wifi/connect",
            {"ssid": "Cafe", "security": "open", "password": ""},
            headers={"Origin": f"http://{HOST}", "Sec-Fetch-Site": "same-origin"},
            close=False,
        )
        self.assertEqual(response["status"], "202 Accepted")
        response["iterable"].close()

    def test_wifi_validation_covers_ssid_open_and_protected_passwords(self):
        base = {"security": "protected", "password": "password1"}
        invalid = (
            ({**base, "ssid": ""}, "400 Bad Request"),
            ({**base, "ssid": "bad\nname"}, "400 Bad Request"),
            ({**base, "ssid": "x" * 33}, "400 Bad Request"),
            ({**base, "ssid": "Home", "password": "short"}, "400 Bad Request"),
            ({**base, "ssid": "Home", "password": "a" * 64}, "400 Bad Request"),
            ({**base, "ssid": "Cafe", "security": "open", "password": "secret123"}, "400 Bad Request"),
        )
        for fields, expected in invalid:
            with self.subTest(fields=fields):
                self.assertEqual(
                    self.client.form("POST", "/wifi/connect", fields)["status"],
                    expected,
                )
        accepted = self.client.form(
            "POST",
            "/wifi/connect",
            {"ssid": "Cafe", "security": "open", "password": ""},
            close=False,
        )
        self.assertEqual(accepted["status"], "202 Accepted")
        accepted["iterable"].close()

    def test_connect_runs_only_after_response_close_and_only_once(self):
        password = "not-in-state-123"
        fields = {
            "ssid": "Private Home",
            "security": "protected",
            "password": password,
        }
        response = self.client.form(
            "POST", "/wifi/connect", fields, close=False
        )
        self.assertEqual(response["status"], "202 Accepted")
        self.assertIn(b"messagebox-handoff", response["body"])
        self.assertIn(f'href="http://{HOST}/"'.encode(), response["body"])
        self.assertEqual(self.adapter.calls, [])
        self.assertNotIn(password, self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn(password.encode(), response["body"])
        response["iterable"].close()
        response["iterable"].close()
        self.assertEqual(self.sleeps, [0.25])
        self.assertEqual(
            self.adapter.calls, [("connect", "Private Home", password)]
        )
        self.assertEqual(self.store.load()["dispatched_generation"], 1)

    def test_duplicate_connecting_submission_has_no_second_dispatch(self):
        fields = {
            "ssid": "Home",
            "security": "protected",
            "password": "password1",
        }
        first = self.client.form(
            "POST", "/wifi/connect", fields, close=False
        )
        duplicate = self.client.form(
            "POST", "/wifi/connect", fields, close=False
        )
        duplicate["iterable"].close()
        self.assertEqual(self.adapter.calls, [])
        first["iterable"].close()
        self.assertEqual(self.adapter.calls.count(("connect", "Home", "password1")), 1)
        self.assertEqual(self.store.load()["generation"], 1)

    def test_captive_probe_routes_explicitly_redirect_to_portal(self):
        for path in (
            "/hotspot-detect.html",
            "/generate_204",
            "/gen_204",
            "/connecttest.txt",
            "/ncsi.txt",
        ):
            with self.subTest(path=path):
                response = self.client.request("GET", path, host="captive.example")
                self.assertEqual(response["status"], "302 Found")
                self.assertEqual(header(response, "Location"), "http://10.41.0.1/")

    def test_hotspot_startup_reconciles_stale_connecting_without_dbus(self):
        stale_path = Path(self.directory.name) / "stale.json"
        stale_store = StateStore(stale_path, clock=self.clock)
        stale_store.initialize()
        stale_store.begin_connect("Home")
        adapter = FakeAdapter()
        create_app(
            mode="HOTSPOT",
            config={"device_id": "A7K2"},
            state_store=stale_store,
            adapter=adapter,
            connectivity_checker=self.checker,
            clock=self.clock,
        )
        self.assertEqual(stale_store.load()["phase"], "WIFI_FAILED")
        self.assertEqual(adapter.calls, [])

    def test_hotspot_startup_reconciles_lost_associated_connection(self):
        stale_path = Path(self.directory.name) / "associated.json"
        stale_store = StateStore(stale_path, clock=self.clock)
        stale_store.initialize()
        stale_store.begin_connect("Home")
        stale_store.mark_associated(1)
        create_app(
            mode="HOTSPOT",
            config={"device_id": "A7K2"},
            state_store=stale_store,
            adapter=FakeAdapter(),
            connectivity_checker=self.checker,
            clock=self.clock,
        )
        state = stale_store.load()
        self.assertEqual(state["phase"], "WIFI_FAILED")
        self.assertEqual(state["safe_error"], "CONNECTION_LOST")

    def test_network_change_keeps_retriable_state_when_dbus_delete_fails(self):
        home_path = Path(self.directory.name) / "change.json"
        home_store = StateStore(home_path, clock=self.clock)
        home_store.initialize()
        home_store.begin_connect("Home")
        home_store.mark_associated(1)
        adapter = FailingDeleteAdapter("CONNECTED")
        checker = FakeChecker({
            "ok": False,
            "proof": ["wlan0_nm_active", "wlan0_non_ap", "wlan0_ipv4", "wlan0_default_route"],
            "error": "DNS_FAILED",
            "attempts": 3,
        })
        application = create_app(
            mode="HOME",
            config={"device_id": "A7K2"},
            state_store=home_store,
            adapter=adapter,
            connectivity_checker=checker,
            clock=self.clock,
            sleep=lambda _: None,
        )
        client = WSGIHarness(application)
        response = client.form("POST", "/wifi/change", {})
        self.assertEqual(response["status"], "202 Accepted")
        self.assertEqual(adapter.calls, [("delete",)])
        self.assertEqual(home_store.load()["phase"], "WIFI_ASSOCIATED")

    def test_home_startup_and_state_request_prove_connectivity_then_pending(self):
        home_path = Path(self.directory.name) / "home.json"
        home_store = StateStore(home_path, clock=self.clock)
        home_store.initialize()
        home_store.begin_connect("Home")
        adapter = FakeAdapter("CONNECTED")
        checker = FakeChecker()
        application = create_app(
            mode="HOME",
            config={"device_id": "A7K2"},
            state_store=home_store,
            adapter=adapter,
            connectivity_checker=checker,
            whatsapp_client=FakeWhatsApp(),
            clock=self.clock,
        )
        self.assertEqual(home_store.load()["phase"], "WHATSAPP_PENDING")
        self.assertEqual(set(home_store.load()["proofs"]), PROOFS)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(checker.calls, 1)
        client = WSGIHarness(application)
        response = client.request("GET", "/api/state")
        self.assertEqual(json.loads(response["body"])["phase"], "WHATSAPP_PENDING")
        self.assertEqual(adapter.calls, [])

    def test_whatsapp_start_is_public_in_home_mode_and_validates_phone(self):
        client, _, worker = self.home_pairing_client()
        denied = client.form(
            "POST",
            "/whatsapp/pair/start",
            {"phone": "+14155550123"},
            headers={"Origin": "http://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(denied["status"], "403 Forbidden")
        invalid = client.form(
            "POST",
            "/whatsapp/pair/start",
            {"phone": "415-555-0123"},
        )
        self.assertEqual(invalid["status"], "400 Bad Request")
        accepted = client.form(
            "POST",
            "/whatsapp/pair/start",
            {"phone": "+1 415 555 0123"},
        )
        self.assertEqual(accepted["status"], "202 Accepted")
        self.assertIn(("start", "+14155550123"), worker.calls)
        body = json.loads(accepted["body"])
        self.assertEqual(body["whatsapp"]["status"], "starting")
        self.assertNotIn("14155550123", accepted["body"].decode("utf-8"))

    def test_whatsapp_state_is_sanitized_and_resumes_with_refreshed_code(self):
        worker = FakeWhatsApp({
            "status": "code_pending",
            "pairing_code": "ABCD EFGH",
            "phone_hint": None,
            "eligible_count": 0,
            "safe_error": None,
            "attempt": 4,
            "private_command_output": "must-not-leak",
        })
        client, _, _ = self.home_pairing_client(worker)
        response = client.request("GET", "/api/state")
        body = json.loads(response["body"])
        self.assertEqual(body["whatsapp"]["pairing_code"], "ABCD EFGH")
        self.assertEqual(
            set(body["whatsapp"]),
            {"status", "pairing_code", "phone_hint", "eligible_count", "safe_error"},
        )
        self.assertNotIn(b"must-not-leak", response["body"])

        worker.state["pairing_code"] = "WXYZ 1234"
        refreshed = client.request("GET", "/api/state")
        self.assertEqual(
            json.loads(refreshed["body"])["whatsapp"]["pairing_code"],
            "WXYZ 1234",
        )

    def test_worker_ready_commits_content_free_proof_and_unlink_is_confirmed(self):
        worker = FakeWhatsApp({
            "status": "ready",
            "pairing_code": None,
            "phone_hint": "WhatsApp number ending in 0123",
            "eligible_count": 10,
            "safe_error": None,
            "attempt": 1,
        })
        client, store, _ = self.home_pairing_client(worker)
        response = client.request("GET", "/api/state")
        body = json.loads(response["body"])
        self.assertEqual(body["phase"], "WHATSAPP_READY")
        self.assertEqual(set(store.load()["proofs"]), PROOFS | WHATSAPP_PROOFS)
        durable = store.path.read_text(encoding="utf-8")
        self.assertNotIn("0123", durable)
        self.assertNotIn("ABCD", durable)

        denied = client.form(
            "POST",
            "/whatsapp/unlink",
            {"confirm": "no"},
        )
        self.assertEqual(denied["status"], "400 Bad Request")
        accepted = client.form(
            "POST",
            "/whatsapp/unlink",
            {"confirm": "unlink"},
        )
        self.assertEqual(accepted["status"], "200 OK")
        self.assertEqual(store.load()["phase"], "WHATSAPP_PENDING")

    def test_logout_failure_preserves_ready_state(self):
        worker = FakeWhatsApp({
            "status": "ready",
            "pairing_code": None,
            "phone_hint": "WhatsApp number ending in 0123",
            "eligible_count": 2,
            "safe_error": None,
            "attempt": 1,
        })
        worker.logout_succeeds = False
        client, store, _ = self.home_pairing_client(worker)
        state = client.request("GET", "/api/state")
        self.assertEqual(json.loads(state["body"])["phase"], "WHATSAPP_READY")
        response = client.form(
            "POST",
            "/whatsapp/unlink",
            {"confirm": "unlink"},
        )
        self.assertEqual(response["status"], "409 Conflict")
        self.assertEqual(store.load()["phase"], "WHATSAPP_READY")

    def test_home_captive_probes_report_success(self):
        application = create_app(
            mode="HOME",
            config={"device_id": "A7K2"},
            state_store=self.store,
            adapter=FakeAdapter("CONNECTED"),
            connectivity_checker=self.checker,
            clock=self.clock,
        )
        client = WSGIHarness(application)
        self.assertEqual(client.request("GET", "/generate_204")["status"], "204 No Content")
        self.assertEqual(client.request("GET", "/hotspot-detect.html")["status"], "200 OK")


if __name__ == "__main__":
    unittest.main()
