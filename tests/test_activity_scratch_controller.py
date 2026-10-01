import importlib.util
import io
import json
from email.message import Message
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import wave

from messagebox.dashboard import app as dashboard
from messagebox.played_history import archive_played_file


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/dev/simulate-activity-dashboard.py"
SPEC = importlib.util.spec_from_file_location("activity_scratch_controller", SCRIPT)
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class NoopGuard:
    def __init__(self):
        self.calls = 0

    def verify(self):
        self.calls += 1


class PostGuardFailure(NoopGuard):
    def verify(self):
        super().verify()
        if self.calls == 2:
            raise controller.SimulationError("simulated production drift")


class ActivityScratchControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.queue = self.root / "queue"
        self.queue.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.contacts = self.state / "contacts.json"
        self.contacts.write_text(json.dumps({
            "version": 2, "revision": 1, "default_recipient": "12025550101@s.whatsapp.net",
            "contacts": {
                "12025550101@s.whatsapp.net": {
                    "label": "Test counterpart", "kind": "person", "receive_after": 1,
                    "card_uids": [], "card_clip": "",
                }
            },
            "listeners": {},
        }))
        self.originals = {
            name: getattr(dashboard, name) for name in (
                "QUEUE_DIR", "HOLD_DIR", "TRASH_DIR", "PLAYED_DIR", "OUTBOX_DIR",
                "EVENTS_FILE", "LISTENED_DIR", "CONTACTS_FILE", "RING_REQUEST_FILE",
            )
        }
        dashboard.QUEUE_DIR = str(self.queue)
        dashboard.HOLD_DIR = str(self.queue / ".hold")
        dashboard.TRASH_DIR = str(self.queue / ".trash")
        dashboard.PLAYED_DIR = str(self.queue / ".played")
        dashboard.OUTBOX_DIR = str(self.root / "outbox")
        dashboard.EVENTS_FILE = str(self.state / "events.jsonl")
        dashboard.LISTENED_DIR = str(self.state / "listened")
        dashboard.CONTACTS_FILE = self.contacts
        dashboard.RING_REQUEST_FILE = str(self.root / "runtime" / "ring-request")
        dashboard.PUBLIC_MESSAGES.clear()
        dashboard.PUBLIC_MESSAGE_REVERSE.clear()

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(dashboard, name, value)
        dashboard.PUBLIC_MESSAGES.clear()
        dashboard.PUBLIC_MESSAGE_REVERSE.clear()
        self.temporary.cleanup()

    def make_message(self, name="1000-message.wav"):
        path = self.queue / name
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * 800)
        Path(str(path) + ".json").write_text(json.dumps({
            "version": 1, "chat": "12025550101@s.whatsapp.net", "msgid": "test-message",
            "sender_jid": "12025550101@s.whatsapp.net", "media_type": "audio",
        }))
        return path

    def request(
        self, method, path, *, host="127.0.0.1:18080", expected_host=None,
        client="127.0.0.1",
    ):
        ledger, guard = [], NoopGuard()
        handler_type = controller.guarded_handler(
            dashboard, guard, expected_host or host, ledger,
        )
        handler = handler_type.__new__(handler_type)
        handler.path = path
        handler.command = method
        handler.headers = {"Host": host, "Origin": "http://" + host}
        handler.client_address = (client, 12345)
        response = {}

        def send(code, body, ctype="application/json"):
            response.update(code=code, body=body, ctype=ctype)

        handler._send = send
        getattr(handler, "do_" + method)()
        response["guard_calls"] = guard.calls
        return response

    def test_real_handler_activity_transitions_remain_inside_scratch(self):
        source = self.make_message()
        archive_played_file(self.queue, source, played_at=2_000_000_000)
        data = dashboard.build_data()
        replay = self.request("POST", "/api/requeue?f=" + data["recently_played"][0]["token"])
        self.assertEqual(replay["code"], 200)
        self.assertEqual(replay["guard_calls"], 2)

        queue = dashboard.build_data()["queue"]
        self.assertEqual(self.request("POST", "/api/hold?f=" + queue[0]["token"])["code"], 200)
        held = dashboard.build_data()["hold"]
        self.assertEqual(self.request("POST", "/api/resume?f=" + held[0]["token"])["code"], 200)
        queue = dashboard.build_data()["queue"]
        self.assertEqual(self.request("POST", "/api/delete?f=" + queue[0]["token"])["code"], 200)
        trash = dashboard.build_data()["trash"]
        self.assertEqual(self.request("POST", "/api/reinstate?f=" + trash[0]["token"])["code"], 200)
        self.assertEqual(len(list(self.queue.glob("*.wav"))), 1)

    def test_post_guard_failure_latches_without_corrupting_committed_response(self):
        source = self.make_message()
        token = dashboard.public_message_token("queue", source.name)
        ledger, guard, state = [], PostGuardFailure(), controller.ControllerState()
        handler_type = controller.guarded_handler(
            dashboard, guard, "127.0.0.1:18080", ledger, state,
        )

        def request(path):
            handler = handler_type.__new__(handler_type)
            handler.path = path
            handler.command = "POST"
            handler.request_version = "HTTP/1.1"
            handler.requestline = "POST " + path + " HTTP/1.1"
            handler.headers = Message()
            handler.headers["Host"] = "127.0.0.1:18080"
            handler.headers["Origin"] = "http://127.0.0.1:18080"
            handler.client_address = ("127.0.0.1", 12345)
            handler.wfile = io.BytesIO()
            handler.close_connection = False
            handler.do_POST()
            return handler.wfile.getvalue()

        first = request("/api/hold?f=" + token)
        self.assertEqual(first.count(b"HTTP/1.0 200 OK"), 1)
        self.assertNotIn(b"503", first)
        self.assertTrue(state.failed.is_set())
        self.assertTrue((self.queue / ".hold" / source.name).is_file())
        self.assertFalse(source.exists())

        second = request("/api/delete?f=" + token)
        self.assertIn(b"HTTP/1.0 503 Service Unavailable", second)
        self.assertEqual(guard.calls, 2)
        self.assertTrue((self.queue / ".hold" / source.name).is_file())

    def test_failure_latch_stops_server_loop_after_current_request(self):
        state, guard = controller.ControllerState(), NoopGuard()

        class Server:
            timeout = 0.25

            def __init__(self):
                self.calls = 0
                self.closed = False

            def handle_request(self):
                self.calls += 1
                state.fail()

            def server_close(self):
                self.closed = True

        server = Server()
        controller._serve_loop(server, state, guard, 10)
        self.assertEqual(server.calls, 1)
        self.assertTrue(server.closed)
        self.assertTrue(state.failed.is_set())
        self.assertEqual(guard.calls, 1)

    def test_gate_allows_only_activity_page_and_exact_controls(self):
        allowed = ["/", "/static/app.js", "/api/state", "/api/data"]
        with mock.patch.object(dashboard.Handler, "do_GET", autospec=True) as delegate:
            for path in allowed:
                self.request("GET", path)
            self.assertGreaterEqual(delegate.call_count, len(allowed))
        for path in (
            "/api/contacts", "/api/settings", "/api/wifi-change", "/audio/token",
            "/api/data?extra=1", "//outside/api/data",
        ):
            self.assertEqual(self.request("GET", path)["code"], 404)
        for path in ("/api/ring", "/api/hold", "/api/hold?f=", "/api/hold?f=a&f=b"):
            self.assertEqual(self.request("POST", path)["code"], 404)
        self.assertEqual(self.request("GET", "/api/data", client="192.0.2.10")["code"], 400)
        self.assertEqual(self.request(
            "GET", "/api/data", host="localhost:18080", expected_host="127.0.0.1:18080",
        )["code"], 400)

        ledger, guard = [], NoopGuard()
        handler_type = controller.guarded_handler(dashboard, guard, "127.0.0.1:18080", ledger)
        handler = handler_type.__new__(handler_type)
        handler.path = "/api/data"
        handler.command = "GET"
        handler.headers = {"Host": "127.0.0.1:18080", "X-Forwarded-For": "127.0.0.1"}
        handler.client_address = ("127.0.0.1", 12345)
        response = {}
        handler._send = lambda code, body, ctype="application/json": response.update(code=code)
        handler.do_GET()
        self.assertEqual(response["code"], 400)

        handler = handler_type.__new__(handler_type)
        handler.path = "/api/hold?f=token"
        handler.command = "POST"
        handler.headers = {"Host": "127.0.0.1:18080"}
        handler.client_address = ("127.0.0.1", 12345)
        response = {}
        handler._send = lambda code, body, ctype="application/json": response.update(code=code)
        handler.do_POST()
        self.assertEqual(response["code"], 400)

        handler = handler_type.__new__(handler_type)
        handler.path = "/private-name?secret=value"
        handler.command = "GET"
        with mock.patch.object(dashboard.Handler, "_send", return_value=None):
            handler._send(404, "{}")
        self.assertEqual(ledger[-1], {"method": "GET", "path": "blocked", "status": 404})

    def test_read_only_subprocess_guard_rejects_send_audio_and_service_changes(self):
        seen = []

        def run(args, *positional, **kwargs):
            seen.append(tuple(args))
            return subprocess.CompletedProcess(args, 0, "active\n", "")

        guarded = controller.ReadOnlySubprocess(run)
        guarded(["systemctl", "is-active", "messagebox-sync.service"])
        guarded(["/usr/local/bin/wacli", "--read-only", "--json", "auth", "status"])
        self.assertEqual(len(seen), 2)
        for command in (
            ["aplay", "file.wav"], ["arecord", "file.wav"],
            ["/usr/local/bin/wacli", "send", "text"],
            ["systemctl", "stop", "messagebox-dash.service"],
        ):
            with self.assertRaises(controller.SimulationError):
                guarded(command)

    def test_production_guard_detects_settings_outside_state(self):
        production = self._production_fixture()
        receipt = {"sync_mutable_paths": ["wacli.db", "wacli.db-shm", "wacli.db-wal"]}
        manifest = {"settings_sha256": "0" * 64}
        with mock.patch.object(controller.receiver, "_verify_service_state"), \
                mock.patch.object(controller, "_dashboard_inactive"), \
                mock.patch.object(controller.receiver, "_verify_production_nfc_clear"), \
                mock.patch.object(controller.receiver, "_verify_live_binding"):
            guard = controller.ProductionGuard(production, manifest, receipt)
            guard.verify()
            Path(production["settings"]).write_text("changed")
            with self.assertRaisesRegex(controller.SimulationError, "settings changed"):
                guard.verify()

    def _production_fixture(self):
        production = {}
        for name in ("queue", "outbox", "state", "sync_store"):
            path = self.root / ("production-" + name)
            path.mkdir()
            production[name] = path
        (production["sync_store"] / "wacli.db").write_bytes(b"db")
        production["seen"] = production["state"] / "seen.json"
        production["seen"].write_text("[]")
        production["contacts"] = production["state"] / "contacts.json"
        production["contacts"].write_text("{}")
        settings_dir = self.root / "production-settings"
        settings_dir.mkdir()
        production["settings"] = settings_dir / "settings.json"
        production["settings"].write_text("settings")
        for name in (
            "nfc_enrollment", "nfc_selection", "nfc_selection_claimed", "nfc_unknown",
            "nfc_announcement", "nfc_announcement_claimed",
            "nfc_announcement_acknowledged", "nfc_health",
        ):
            production[name] = self.root / ("missing-" + name)
        return production

    def test_fresh_import_audits_every_activity_mutable_path_under_scratch(self):
        scratch = self.root / "fresh"
        scratch.mkdir()
        code = f"""
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('activity_fresh', {str(SCRIPT)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
dashboard = module.import_scratch_dashboard(Path({str(scratch)!r}))
print(dashboard.QUEUE_DIR)
"""
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=SCRIPT.parents[2], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(Path(result.stdout.strip()).is_relative_to(scratch.resolve()))

    def test_scratch_dashboard_never_prunes_a_production_history_tree(self):
        production = self.root / "production-queue"
        production.mkdir()
        old = production / ".played"
        old.mkdir()
        (old / "old.wav").write_bytes(b"protected")
        (old / "old.wav.json").write_text(json.dumps({"played_at": 1, "chat": "x"}))
        before = controller.receiver._tree(production)
        source = self.make_message()
        archive_played_file(self.queue, source, played_at=2_000_000_000)

        dashboard.build_data()

        self.assertEqual(controller.receiver._tree(production), before)


if __name__ == "__main__":
    unittest.main()
