import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from messagebox.test_report import (
    SERVICES,
    TestRigDisabled,
    TestRigError,
    build_report,
    event_transition_snapshot,
    load_test_rig_config,
    redact_text,
    report_to_markdown,
    surface_snapshot,
    test_rig_available,
)


class TestReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "test-rig.json"
        self.revision = self.root / "REVISION"
        self.run_state = self.root / "run.json"
        self.boot_id = self.root / "boot_id"
        self.config.write_text(
            json.dumps({"version": 1, "device": "button-box-003"}),
            encoding="utf-8",
        )
        self.revision.write_text("a" * 40 + "\n", encoding="utf-8")
        self.boot_id.write_text(
            "12345678-1234-1234-1234-123456789abc\n", encoding="utf-8"
        )

    def test_marker_must_be_regular_valid_and_match_exact_device(self):
        self.assertTrue(
            test_rig_available(self.config, hostname="button-box-003.local")
        )
        self.assertEqual(
            load_test_rig_config(self.config, hostname="button-box-003"),
            {"version": 1, "device": "button-box-003"},
        )
        with self.assertRaises(TestRigError):
            load_test_rig_config(self.config, hostname="button-box-004")

        missing = self.root / "missing.json"
        with self.assertRaises(TestRigDisabled):
            load_test_rig_config(missing, hostname="button-box-003")
        link = self.root / "marker-link.json"
        link.symlink_to(self.config)
        self.assertFalse(test_rig_available(link, hostname="button-box-003"))

    def test_report_allowlists_partial_onboarding_transition(self):
        self.run_state.write_text(
            json.dumps(
                {
                    "version": 1,
                    "id": "run-1",
                    "scenario": "message-loop",
                    "status": "running",
                    "started_at": "2026-09-14T10:00:00Z",
                    "updated_at": "2026-09-14T10:01:00Z",
                    "steps": {
                        "message-received": "pass",
                        "message-played": "fail",
                        "private-phone": "+1 555 123 4567",
                    },
                }
            ),
            encoding="utf-8",
        )

        def runner(command, **_kwargs):
            self.assertIn(command[2], SERVICES)
            return SimpleNamespace(
                returncode=0,
                stdout="ActiveState=active\nSubState=running\nResult=success\nNRestarts=2\n",
            )

        state = {
            "mode": "HOME",
            "phase": "WHATSAPP_READY",
            "phone_hint": "+1 555 123 4567",
            "whatsapp": {
                "status": "ready",
                "eligible_count": 2,
                "account_id": "private@s.whatsapp.net",
            },
            "recipient_setup": {
                "status": "testing",
                "configured_count": 1,
                "available_count": 1,
                "selected_jid": "private@s.whatsapp.net",
                "proof": {"received": True, "played": False, "replied": False},
            },
            "nfc_setup": {"status": "ready", "mapped_count": 1, "card_id": "secret"},
        }
        report = build_report(
            "onboarding",
            note="password=hunter2 from +1 555 123 4567 at 192.168.1.4",
            surface_state=state,
            config_path=self.config,
            revision_path=self.revision,
            run_state_path=self.run_state,
            boot_id_path=self.boot_id,
            hostname="button-box-003.local",
            runner=runner,
            clock=lambda: 0,
            path_exists=lambda path: path in {"/dev/gpiochip0", "/proc/asound/cards"},
        )

        self.assertEqual(
            report["state"]["recipient"]["proof"],
            {"received": True, "played": False, "replied": False},
        )
        self.assertEqual(
            report["test_run"]["steps"],
            {"message-received": "pass", "message-played": "fail"},
        )
        self.assertEqual(report["software"]["revision"], "a" * 40)
        self.assertEqual(report["hardware"], {"gpio": True, "i2c": False, "audio": True})
        serialized = json.dumps(report)
        for private in ("hunter2", "555", "192.168.1.4", "private@", "card_id"):
            self.assertNotIn(private, serialized)
        markdown = report_to_markdown(report)
        self.assertIn("# Button Box test report", markdown)
        self.assertIn("`message-played`: `fail`", markdown)

    def test_snapshots_drop_unknown_fields_and_invalid_values(self):
        snapshot = surface_snapshot(
            "dashboard",
            {"queue_count": -1, "hold_count": 4, "raw_events": ["private"]},
        )
        self.assertEqual(snapshot["queue_count"], 0)
        self.assertEqual(snapshot["hold_count"], 4)
        self.assertEqual(snapshot["button"]["raw_edge_at"], "unavailable")
        self.assertEqual(redact_text("token:abc person@s.whatsapp.net"), "[redacted secret] [redacted WhatsApp ID]")

    def test_event_transition_snapshot_keeps_only_allowlisted_timestamps(self):
        snapshot = event_transition_snapshot(
            [
                {"type": "received", "ts": 1, "chat": "private@s.whatsapp.net"},
                {"type": "guided_press", "ts": 2, "session_id": "private"},
                {"type": "unknown_private_event", "ts": 3},
                {"type": "played", "ts": float("nan")},
            ]
        )
        self.assertEqual(
            snapshot,
            {
                "received": "1970-01-01T00:00:01Z",
                "guided_press": "1970-01-01T00:00:02Z",
            },
        )


if __name__ == "__main__":
    unittest.main()
