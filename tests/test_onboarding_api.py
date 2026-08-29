import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from messagebox.onboarding.app import create_app
from messagebox.onboarding.comitup_adapter import ComitupError
from messagebox.onboarding.state import PROOFS, WHATSAPP_PROOFS, StateStore
from messagebox.settings import SettingsStore


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
        self.recipient = {
            "status": "choose",
            "default": None,
            "proof": {"received": False, "played": False, "replied": False},
            "recipients": [],
        }

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

    def relink(self):
        self.calls.append(("relink",))
        if self.logout_succeeds:
            self.state.update(
                status="idle",
                phone_hint=None,
                eligible_count=0,
                safe_error=None,
            )
            self.recipient = {
                "status": "choose",
                "default": None,
                "proof": {"received": False, "played": False, "replied": False},
                "recipients": [],
            }
        else:
            self.state["safe_error"] = "UNLINK_FAILED"
        return dict(self.state)

    def recipient_state(self):
        self.calls.append(("recipient_state",))
        return json.loads(json.dumps(self.recipient))

    def recipient_list(self, refresh=False):
        self.calls.append(("recipient_list", refresh))
        return json.loads(json.dumps(self.recipient))

    def recipient_defer(self):
        self.recipient["status"] = "deferred"
        return self.recipient_state()

    def recipient_select(self, token):
        self.calls.append(("recipient_select", token))
        return self.recipient_state()

    def recipient_select_phone(self, phone):
        self.calls.append(("recipient_select_phone", phone))
        return self.recipient_state()

    def recipient_add(self, token):
        self.calls.append(("recipient_add", token))
        return self.recipient_state()

    def recipient_add_phone(self, phone):
        self.calls.append(("recipient_add_phone", phone))
        return self.recipient_state()

    def recipient_remove(self, token):
        self.calls.append(("recipient_remove", token))
        return self.recipient_state()

    def recipient_default(self, token):
        self.calls.append(("recipient_default", token))
        return self.recipient_state()


class FakeNfc:
    def __init__(self):
        self.calls = []
        self.state = {
            "status": "idle",
            "recipients": [],
            "mapped_count": 0,
            "recipient": None,
            "remove_tag": False,
            "sound_warning": False,
        }

    def status(self):
        self.calls.append(("status",))
        return json.loads(json.dumps(self.state))

    def _action(self, action, status=None):
        self.calls.append((action,))
        if status is not None:
            self.state["status"] = status
        return json.loads(json.dumps(self.state))

    def start(self):
        return self._action("start", "waiting")

    def retry(self):
        return self._action("retry", "waiting")

    def reassign(self):
        return self._action("reassign", "choose")

    def assign(self, token):
        self.calls.append(("assign", token))
        self.state["status"] = "success"
        return json.loads(json.dumps(self.state))

    def next(self):
        return self._action("next", "waiting")

    def cancel(self):
        return self._action("cancel", "idle")

    def finish(self):
        return self._action("finish", "idle")


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
        self.settings = SettingsStore(
            Path(self.directory.name) / "settings.json", environ={"TZ": "UTC"}
        )
        self.app = create_app(
            mode="HOTSPOT",
            config={"device_id": "A7K2"},
            state_store=self.store,
            adapter=self.adapter,
            connectivity_checker=self.checker,
            caregiver_settings=self.settings,
            clock=self.clock,
            sleep=self.sleeps.append,
            handoff_delay=0.25,
        )
        self.client = WSGIHarness(self.app)

    def tearDown(self):
        self.directory.cleanup()

    def home_pairing_client(
        self, whatsapp=None, nfc=None, completion_request=None, tailscale_host=None
    ):
        path = Path(self.directory.name) / "whatsapp-home.json"
        store = StateStore(path, clock=self.clock)
        store.initialize()
        store.begin_connect("Home")
        store.mark_associated(1)
        store.record_connectivity_result(PROOFS)
        worker = whatsapp or FakeWhatsApp()
        options = {}
        if completion_request is not None:
            options["completion_request"] = completion_request
        if tailscale_host is not None:
            options["tailscale_host"] = tailscale_host
        application = create_app(
            mode="HOME",
            config={"device_id": "A7K2"},
            state_store=store,
            adapter=FakeAdapter("CONNECTED"),
            connectivity_checker=self.checker,
            whatsapp_client=worker,
            nfc_client=nfc or FakeNfc(),
            caregiver_settings=self.settings,
            clock=self.clock,
            **options,
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

    def test_button_box_canonical_hostname_is_supported_and_enforced(self):
        canonical_host = "button-box-a7.local"
        application = create_app(
            mode="HOTSPOT",
            config={"device_id": "a7", "canonical_host": canonical_host},
            state_store=StateStore(
                Path(self.directory.name) / "button-box-state.json", clock=self.clock
            ),
            adapter=self.adapter,
            connectivity_checker=self.checker,
            caregiver_settings=self.settings,
            clock=self.clock,
        )
        client = WSGIHarness(application)

        accepted = client.request("GET", "/", host=canonical_host)
        redirected = client.request("GET", "/", host="message-box-a7.local")

        self.assertEqual(accepted["status"], "200 OK")
        self.assertIn(f"http://{canonical_host}/".encode(), accepted["body"])
        self.assertEqual(redirected["status"], "302 Found")
        self.assertEqual(header(redirected, "Location"), "http://10.41.0.1/")

    def test_home_dashboard_accepts_exact_tailnet_https_origin(self):
        tailnet = "message-box-a7k2.example-tailnet.ts.net"
        client, _store, _worker = self.home_pairing_client(tailscale_host=tailnet)

        loaded = client.request(
            "GET",
            "/",
            host=tailnet,
            remote_addr="127.0.0.1",
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(loaded["status"], "200 OK")
        self.assertIn(f"https://{tailnet}/".encode(), loaded["body"])

        settings = client.request(
            "GET",
            "/api/settings",
            host=tailnet,
            remote_addr="127.0.0.1",
            headers={"X-Forwarded-Proto": "https"},
        )
        document = json.loads(settings["body"])["settings"]
        candidate = {
            key: value
            for key, value in document.items()
            if key not in {"version", "revision"}
        }
        saved = client.json(
            "PUT",
            "/api/settings",
            {"revision": document["revision"], "settings": candidate},
            host=tailnet,
            remote_addr="127.0.0.1",
            headers={
                "Origin": f"https://{tailnet}",
                "X-Forwarded-Proto": "https",
            },
        )
        self.assertEqual(saved["status"], "200 OK")

    def test_home_dashboard_rejects_tailnet_header_spoofing(self):
        tailnet = "message-box-a7k2.example-tailnet.ts.net"
        client, _store, _worker = self.home_pairing_client(tailscale_host=tailnet)

        spoofed = client.request(
            "GET",
            "/api/state",
            host=tailnet,
            remote_addr="192.168.1.20",
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(spoofed["status"], "302 Found")
        self.assertEqual(header(spoofed, "Location"), f"http://{HOST}/api/state")

    def test_invalid_tailnet_configuration_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "Tailscale dashboard hostname"):
            create_app(
                mode="HOME",
                config={"device_id": "A7K2"},
                state_store=StateStore(
                    Path(self.directory.name) / "invalid-tailnet.json", clock=self.clock
                ),
                adapter=self.adapter,
                connectivity_checker=self.checker,
                caregiver_settings=self.settings,
                tailscale_host="attacker.example",
            )

    def test_settings_are_revision_checked_and_cross_site_writes_are_rejected(self):
        loaded = self.client.request("GET", "/api/settings", host="10.41.0.1")
        self.assertEqual(loaded["status"], "200 OK")
        document = json.loads(loaded["body"])["settings"]
        candidate = {
            key: value
            for key, value in document.items()
            if key not in {"version", "revision"}
        }
        candidate["max_recording_seconds"] = 120
        saved = self.client.json(
            "PUT",
            "/api/settings",
            {"revision": 0, "settings": candidate},
            host="10.41.0.1",
            headers={"Origin": "http://10.41.0.1"},
        )
        self.assertEqual(saved["status"], "200 OK")
        conflict = self.client.json(
            "PUT",
            "/api/settings",
            {"revision": 0, "settings": candidate},
            host="10.41.0.1",
            headers={"Origin": "http://10.41.0.1"},
        )
        self.assertEqual(conflict["status"], "409 Conflict")
        rejected = self.client.json(
            "PUT",
            "/api/settings",
            {"revision": 1, "settings": candidate},
            host="10.41.0.1",
            headers={"Origin": "http://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(rejected["status"], "403 Forbidden")

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
            {"mode", "phase", "safe_error", "whatsapp", "recipient_setup", "nfc_setup"},
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
        self.assertIn(("relink",), worker.calls)

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

    def test_completed_recipient_setup_can_relink_and_resets_account_state(self):
        worker = FakeWhatsApp({
            "status": "ready",
            "pairing_code": None,
            "phone_hint": "Linked account",
            "eligible_count": 1,
            "safe_error": None,
            "attempt": 1,
        })
        worker.recipient.update(
            status="complete",
            default={"token": "opaque-recipient", "label": "Default", "kind": "person"},
            proof={"received": True, "played": True, "replied": True},
        )
        client, store, _ = self.home_pairing_client(worker)
        client.request("GET", "/api/state")

        response = client.form(
            "POST",
            "/whatsapp/unlink",
            {"confirm": "unlink"},
        )

        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(store.load()["phase"], "WHATSAPP_PENDING")
        self.assertEqual(worker.recipient["status"], "choose")
        self.assertIsNone(worker.recipient["default"])
        self.assertIn(("relink",), worker.calls)

    def test_recipient_api_uses_opaque_tokens_and_same_origin_mutations(self):
        token = "recipient-token-0001"
        private_jid = "15551234567@s.whatsapp.net"
        worker = FakeWhatsApp(
            {
                "status": "ready",
                "pairing_code": None,
                "phone_hint": "WhatsApp number ending in 0123",
                "eligible_count": 1,
                "safe_error": None,
                "attempt": 1,
            }
        )
        worker.recipient["recipients"] = [
            {
                "token": token,
                "label": "Grandma",
                "kind": "person",
                "configured": False,
                "is_default": False,
                "available": True,
                "card_count": 0,
            }
        ]
        client, store, _ = self.home_pairing_client(worker)
        client.request("GET", "/api/state")

        response = client.request("GET", "/api/recipients")
        self.assertEqual(response["status"], "200 OK")
        self.assertNotIn(private_jid.encode(), response["body"])
        self.assertIn(token.encode(), response["body"])

        rejected = client.form(
            "POST",
            "/recipients/refresh",
            {},
            headers={"Origin": "http://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(rejected["status"], "403 Forbidden")
        malformed = client.form("POST", "/recipients/select", {"token": "short"})
        self.assertEqual(malformed["status"], "400 Bad Request")
        accepted = client.form("POST", "/recipients/select", {"token": token})
        self.assertEqual(accepted["status"], "200 OK")
        self.assertIn(("recipient_select", token), worker.calls)
        changed = client.form("POST", "/recipients/default", {"token": token})
        self.assertEqual(changed["status"], "200 OK")
        self.assertIn(("recipient_default", token), worker.calls)

        bad_phone = client.form(
            "POST", "/recipients/select-number", {"phone": "0555"}
        )
        self.assertEqual(bad_phone["status"], "400 Bad Request")
        denied_phone = client.form(
            "POST",
            "/recipients/add-number",
            {"phone": "+1 415 555 0199"},
            headers={"Origin": "http://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(denied_phone["status"], "403 Forbidden")
        selected_phone = client.form(
            "POST", "/recipients/select-number", {"phone": "+1 415-555-0199"}
        )
        self.assertEqual(selected_phone["status"], "200 OK")
        self.assertIn(("recipient_select_phone", "+14155550199"), worker.calls)
        added_phone = client.form(
            "POST", "/recipients/add-number", {"phone": "+44 7700 900123"}
        )
        self.assertEqual(added_phone["status"], "200 OK")
        self.assertIn(("recipient_add_phone", "+447700900123"), worker.calls)
        self.assertEqual(store.load()["phase"], "WHATSAPP_READY")

    def test_nfc_api_is_opaque_same_origin_and_completes_asynchronously(self):
        token = "recipient-token-0001"
        private_jid = "15551234567@s.whatsapp.net"
        whatsapp = FakeWhatsApp(
            {
                "status": "ready",
                "pairing_code": None,
                "phone_hint": "WhatsApp number ending in 0123",
                "eligible_count": 1,
                "safe_error": None,
                "attempt": 1,
            }
        )
        whatsapp.recipient.update(
            status="complete",
            default={"token": token, "label": "+15551234567", "kind": "person"},
        )
        whatsapp.recipient["recipients"] = [
            {
                "token": token,
                "label": "+15551234567",
                "kind": "person",
                "configured": True,
                "is_default": True,
                "available": True,
                "card_count": 0,
            }
        ]
        nfc = FakeNfc()
        nfc.state["recipients"] = [
            {
                "token": token,
                "label": "+15551234567",
                "kind": "person",
                "is_default": True,
                "card_count": 0,
            }
        ]
        completions = []
        client, _, _ = self.home_pairing_client(
            whatsapp, nfc, lambda: completions.append(True)
        )
        client.request("GET", "/api/state")

        listed = client.request("GET", "/api/nfc")
        self.assertEqual(listed["status"], "200 OK")
        self.assertNotIn(private_jid.encode(), listed["body"])
        self.assertNotIn(b"04:01:02:03", listed["body"])
        denied = client.form(
            "POST",
            "/nfc/start",
            {},
            headers={"Origin": "http://attacker.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(denied["status"], "403 Forbidden")
        self.assertEqual(client.form("POST", "/nfc/start", {})["status"], "200 OK")
        self.assertEqual(
            client.form("POST", "/nfc/assign", {"token": "short"})["status"],
            "400 Bad Request",
        )
        self.assertEqual(
            client.form("POST", "/nfc/assign", {"token": token})["status"],
            "200 OK",
        )
        completed = client.form(
            "POST", "/onboarding/complete", {"intent": "done"}
        )
        self.assertEqual(completed["status"], "202 Accepted")
        self.assertEqual(completions, [True])
        self.assertIn(("finish",), nfc.calls)

    def test_skip_can_finish_when_the_optional_nfc_worker_is_down(self):
        token = "recipient-token-0001"
        whatsapp = FakeWhatsApp(
            {
                "status": "ready",
                "pairing_code": None,
                "phone_hint": "Linked account",
                "eligible_count": 0,
                "safe_error": None,
                "attempt": 1,
            }
        )
        whatsapp.recipient.update(
            status="complete",
            default={"token": token, "label": "+15551234567", "kind": "person"},
            recipients=[
                {
                    "token": token,
                    "label": "+15551234567",
                    "kind": "person",
                    "configured": True,
                    "is_default": True,
                    "available": False,
                    "card_count": 0,
                }
            ],
        )
        nfc = FakeNfc()

        def unavailable():
            raise OSError("private socket detail")

        nfc.finish = unavailable
        completions = []
        client, _, _ = self.home_pairing_client(
            whatsapp, nfc, lambda: completions.append(True)
        )
        client.request("GET", "/api/state")
        response = client.form(
            "POST", "/onboarding/complete", {"intent": "skip"}
        )
        self.assertEqual(response["status"], "202 Accepted")
        self.assertEqual(completions, [True])

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
