import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.onboarding.whatsapp import (
    MAX_BOOTSTRAP_MESSAGES,
    MAX_ELIGIBLE_CONVERSATIONS,
    PairingEngine,
    PairingError,
    eligible_conversations,
    normalize_phone,
    pairing_command,
    pairing_environment,
    parse_event,
)


class FakeProcess:
    def __init__(self, lines):
        self.stderr = list(lines)
        self.pid = 9876
        self.returncode = None

    def wait(self):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15


class RecordingPopen:
    def __init__(self, lines):
        self.lines = lines
        self.calls = []

    def __call__(self, arguments, **kwargs):
        self.calls.append((list(arguments), kwargs))
        return FakeProcess(self.lines)


class WacliRunner:
    def __init__(self, *, authenticated=True, connected=True, logout_ok=True, chats=None):
        self.authenticated = authenticated
        self.connected = connected
        self.logout_ok = logout_ok
        self.chats = chats or []
        self.calls = []

    def __call__(self, arguments, **kwargs):
        arguments = list(arguments)
        self.calls.append((arguments, kwargs))
        if arguments[-2:] == ["auth", "status"]:
            body = {
                "success": True,
                "data": {
                    "authenticated": self.authenticated,
                    "linked_jid": "14155550123@s.whatsapp.net",
                    "phone": "14155550123",
                },
                "error": None,
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")
        if arguments[-2:] == ["doctor", "--connect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"checks": {"connected": self.connected}}),
                stderr="",
            )
        if "chats" in arguments and "list" in arguments:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"chats": self.chats}),
                stderr="",
            )
        if arguments[-2:] == ["auth", "logout"]:
            return SimpleNamespace(
                returncode=0 if self.logout_ok else 1,
                stdout="{}",
                stderr="",
            )
        raise AssertionError(f"unexpected wacli command: {arguments}")


class RecordingEngine(PairingEngine):
    def __init__(self, *args, **kwargs):
        self.history = []
        super().__init__(*args, **kwargs)

    def _set_state(self, status, **kwargs):
        result = super()._set_state(status, **kwargs)
        self.history.append(dict(result))
        return result


class WhatsAppPairingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pairing_root = self.root / "pairing"
        self.live_store = self.root / "wacli"
        self.candidates = self.live_store / "onboarding-candidates.json"

    def tearDown(self):
        self.temporary.cleanup()

    def engine(self, *, popen=None, runner=None, recover=False, cls=PairingEngine):
        return cls(
            pairing_root=self.pairing_root,
            live_store=self.live_store,
            candidates_path=self.candidates,
            popen=popen or RecordingPopen([]),
            run=runner or WacliRunner(authenticated=False),
            recover=recover,
        )

    def prepare_pair(self, engine):
        engine.stage.mkdir(mode=0o700)
        engine._set_state("starting")

    def test_phone_normalization_and_exact_auth_command(self):
        self.assertEqual(normalize_phone(" +1 415-555-0123 "), "+14155550123")
        for invalid in ("14155550123", "+012345678", "+123", "+1234567890123456", None):
            with self.subTest(invalid=invalid), self.assertRaises(PairingError):
                normalize_phone(invalid)
        self.assertEqual(
            pairing_command("+14155550123"),
            [
                "/usr/local/bin/wacli",
                "--events",
                "auth",
                "--idle-exit",
                "30s",
                "--phone",
                "+14155550123",
            ],
        )
        environment = pairing_environment("/private/staging")
        self.assertEqual(environment["WACLI_SYNC_MAX_MESSAGES"], "100")
        self.assertEqual(MAX_BOOTSTRAP_MESSAGES, 100)
        self.assertEqual(environment["WACLI_STORE_DIR"], "/private/staging")
        self.assertEqual(
            set(environment),
            {
                "HOME",
                "LANG",
                "LC_ALL",
                "PATH",
                "WACLI_STORE_DIR",
                "WACLI_SYNC_MAX_MESSAGES",
                "WACLI_SYNC_MAX_DB_SIZE",
            },
        )

    def test_ndjson_event_and_eligible_conversation_parsing(self):
        event = parse_event(
            '{"event":"pair_code","data":{"phone":"+14155550123","code":"ABCD-EFGH"}}'
        )
        self.assertEqual(event, ("pair_code", {"phone": "+14155550123", "code": "ABCD-EFGH"}))
        self.assertIsNone(parse_event("not-json"))
        rows = [
            {"jid": f"{index}@g.us", "name": f"Group {index}"}
            for index in range(12)
        ]
        rows.extend(
            [
                {"jid": "15551234567@s.whatsapp.net", "name": "DM"},
                {"jid": "status@broadcast", "name": "Status"},
                {"jid": "0@g.us", "name": "duplicate"},
            ]
        )
        result = eligible_conversations({"chats": rows})
        self.assertEqual(len(result), MAX_ELIGIBLE_CONVERSATIONS)
        self.assertEqual(len({row["jid"] for row in result}), len(result))
        self.assertNotIn("status@broadcast", {row["jid"] for row in result})

    def test_refreshed_code_auth_doctor_bootstrap_and_atomic_promotion(self):
        chats = [
            {"jid": f"{index}@g.us", "name": f"Private group {index}"}
            for index in range(12)
        ]
        process = RecordingPopen(
            [
                '{"event":"pair_code","data":{"code":"ABCD-EFGH"}}\n',
                '{"event":"pair_code","data":{"code":"WXYZ-1234"}}\n',
                '{"event":"connected","data":{}}\n',
            ]
        )
        runner = WacliRunner(chats=chats)
        engine = self.engine(popen=process, runner=runner, cls=RecordingEngine)
        self.prepare_pair(engine)
        (engine.stage / "store.db").write_text("private store", encoding="ascii")

        engine._pair("+14155550123")

        codes = [item["pairing_code"] for item in engine.history if item["status"] == "code_pending"]
        self.assertEqual(codes, ["ABCD-EFGH", "WXYZ-1234"])
        self.assertEqual(engine.public_state()["status"], "ready")
        self.assertEqual(engine.public_state()["phone_hint"], "WhatsApp number ending in 0123")
        self.assertEqual(engine.public_state()["eligible_count"], 10)
        self.assertFalse(engine.stage.exists())
        self.assertEqual((self.live_store / "store.db").read_text(encoding="ascii"), "private store")
        candidates = json.loads(self.candidates.read_text(encoding="utf-8"))
        self.assertEqual(len(candidates["conversations"]), 10)
        self.assertEqual(self.candidates.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("--download-media", process.calls[0][0])
        commands = [call[0] for call in runner.calls]
        self.assertIn(
            ["/usr/local/bin/wacli", "--read-only", "--json", "auth", "status"],
            commands,
        )
        self.assertIn(
            ["/usr/local/bin/wacli", "--json", "--timeout", "15s", "doctor", "--connect"],
            commands,
        )
        self.assertTrue(all(call[1]["env"]["WACLI_SYNC_MAX_MESSAGES"] == "100" for call in runner.calls))

    def test_unclaimed_code_expires_and_staging_is_removed(self):
        process = RecordingPopen(
            ['{"event":"pair_code","data":{"code":"ABCD-EFGH"}}\n']
        )
        engine = self.engine(popen=process, runner=WacliRunner(authenticated=False))
        self.prepare_pair(engine)
        (engine.stage / "partial.db").write_text("partial", encoding="ascii")

        engine._pair("+14155550123")

        self.assertEqual(engine.public_state()["status"], "expired")
        self.assertEqual(engine.public_state()["safe_error"], "PAIRING_INTERRUPTED")
        self.assertFalse(engine.stage.exists())
        self.assertFalse(self.live_store.exists())

    def test_duplicate_start_is_idempotent_and_other_phone_is_rejected(self):
        engine = self.engine()

        class DeferredThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        with mock.patch("src.onboarding.whatsapp.threading.Thread", DeferredThread):
            first = engine.start("+14155550123")
            duplicate = engine.start("+1 415 555 0123")
            self.assertEqual(duplicate["attempt"], first["attempt"])
            with self.assertRaisesRegex(PairingError, "pairing_already_in_progress"):
                engine.start("+442079460123")

    def test_cancel_and_worker_restart_cleanup_private_staging(self):
        engine = self.engine()
        self.prepare_pair(engine)
        (engine.stage / "partial.db").write_text("partial", encoding="ascii")
        cancelled = engine.cancel()
        self.assertEqual(cancelled["status"], "starting")
        engine._pair("+14155550123")
        self.assertEqual(engine.public_state()["status"], "idle")
        self.assertFalse(engine.stage.exists())

        engine.stage.mkdir(mode=0o700)
        (engine.stage / "partial.db").write_text("partial", encoding="ascii")
        engine._set_state("code_pending", code="ABCD-EFGH")
        recovered = self.engine(recover=True)
        self.assertEqual(recovered.public_state()["status"], "expired")
        self.assertEqual(recovered.public_state()["safe_error"], "PAIRING_INTERRUPTED")
        self.assertFalse(recovered.stage.exists())

    def test_worker_restart_recovers_an_already_promoted_verified_store(self):
        engine = self.engine()
        engine._set_state("verifying")
        self.live_store.mkdir()
        engine.backup.mkdir()
        self.candidates.write_text(
            json.dumps({
                "version": 1,
                "conversations": [{"jid": "123-456@g.us", "label": "Family"}],
            }),
            encoding="utf-8",
        )
        recovered = self.engine(runner=WacliRunner(), recover=True)
        state = recovered.public_state()
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["eligible_count"], 1)
        self.assertEqual(state["phone_hint"], "WhatsApp number ending in 0123")
        self.assertFalse(recovered.backup.exists())

    def test_worker_restart_rolls_back_interrupted_empty_store_move(self):
        engine = self.engine()
        engine._set_state("verifying")
        engine.stage.mkdir()
        (engine.stage / "partial.db").write_text("partial", encoding="ascii")
        self.live_store.mkdir()
        os.replace(self.live_store, engine.backup)

        recovered = self.engine(recover=True)

        self.assertEqual(recovered.public_state()["status"], "expired")
        self.assertTrue(self.live_store.is_dir())
        self.assertFalse(recovered.backup.exists())
        self.assertFalse(recovered.stage.exists())

    def test_corrupt_private_state_cannot_escape_through_public_status(self):
        engine = self.engine()
        document = json.loads(engine.state_path.read_text(encoding="utf-8"))
        document["pairing_code"] = "private command output"
        engine.state_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(PairingError, "pairing_state_unavailable"):
            engine.public_state()

    def test_store_conflict_fails_closed_and_removes_candidates_and_stage(self):
        self.live_store.mkdir()
        (self.live_store / "existing.db").write_text("preserve", encoding="ascii")
        process = RecordingPopen(['{"event":"connected","data":{}}\n'])
        engine = self.engine(popen=process, runner=WacliRunner(chats=[]))
        self.prepare_pair(engine)
        (engine.stage / "new.db").write_text("new", encoding="ascii")
        engine._pair("+14155550123")
        self.assertEqual(engine.public_state()["safe_error"], "STORE_CONFLICT")
        self.assertEqual((self.live_store / "existing.db").read_text(encoding="ascii"), "preserve")
        self.assertFalse(engine.stage.exists())
        self.assertFalse(self.candidates.exists())

    def test_unlink_requires_successful_logout_before_store_removal(self):
        runner = WacliRunner(logout_ok=False)
        engine = self.engine(runner=runner)
        self.live_store.mkdir()
        (self.live_store / "store.db").write_text("keep", encoding="ascii")
        engine._set_state(
            "ready",
            phone_hint="WhatsApp number ending in 0123",
            eligible_count=2,
        )
        failed = engine.unlink()
        self.assertEqual(failed["safe_error"], "UNLINK_FAILED")
        self.assertTrue((self.live_store / "store.db").exists())

        runner.logout_ok = True
        unlinked = engine.unlink()
        self.assertEqual(unlinked["status"], "idle")
        self.assertTrue(self.live_store.is_dir())
        self.assertEqual(list(self.live_store.iterdir()), [])


class WhatsAppFrontendAndServiceContractTests(unittest.TestCase):
    def test_frontend_has_all_pairing_states_accessibility_and_no_qr(self):
        root = Path(__file__).parents[1]
        html = (root / "src/onboarding/static/index.html").read_text(encoding="utf-8")
        script = (root / "src/onboarding/static/app.js").read_text(encoding="utf-8")
        for view in (
            "whatsapp-view",
            "code-view",
            "pairing-progress-view",
            "pairing-error-view",
            "ready-view",
        ):
            self.assertIn(f'id="{view}"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('tabindex="-1"', html)
        self.assertIn("whatsapp.pairing_code", script)
        self.assertIn("setTimeout(loadState, 1500)", script)
        for status in (
            'case "idle"',
            'case "code_pending"',
            'case "starting"',
            'case "bootstrapping"',
            'case "verifying"',
            'case "expired"',
            'case "failed"',
            'whatsapp.status === "ready"',
        ):
            self.assertIn(status, script)
        self.assertIn(".focus(", script)
        self.assertIn("retry-pairing", script)
        self.assertNotIn("QR code", html)
        self.assertNotIn("qr_code", script.lower())

    def test_service_separates_web_user_from_live_store_and_keeps_runtime_stopped(self):
        root = Path(__file__).parents[1]
        worker = (root / "systemd/onboarding/messagebox-whatsapp-pairing.service").read_text(
            encoding="utf-8"
        )
        web = (root / "systemd/onboarding/messagebox-onboarding-home.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("User=messagebox\n", worker)
        self.assertIn("RuntimeDirectory=messagebox-whatsapp-pairing", worker)
        self.assertIn("ReadWritePaths=/var/lib/messagebox", worker)
        self.assertNotIn("/var/lib/messagebox/wacli", web)
        self.assertIn("Requires=messagebox-whatsapp-pairing.service", web)
        self.assertNotIn("messagebox.target", worker)


if __name__ == "__main__":
    unittest.main()
