import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "messagebox_device_dashboard", ROOT / "src" / "dashboard.py"
)
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)

from src.listened_receipts import webhook_signature  # noqa: E402


class GuidedOutboxDashboardTests(unittest.TestCase):
    def test_signed_played_receipt_is_correlated_queued_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            originals = {
                name: getattr(dashboard, name)
                for name in (
                    "EVENTS_FILE",
                    "LISTENED_DIR",
                    "CONTACTS_FILE",
                    "LISTENED_FALLBACK_WAV",
                    "WACLI_WEBHOOK_SECRET",
                )
            }
            try:
                dashboard.EVENTS_FILE = str(root / "events.jsonl")
                dashboard.LISTENED_DIR = str(root / "receipts")
                dashboard.CONTACTS_FILE = root / "contacts.json"
                dashboard.LISTENED_FALLBACK_WAV = "/private/prompts/someone.wav"
                dashboard.WACLI_WEBHOOK_SECRET = "private-secret"
                dashboard.contacts_store().upsert_listener(
                    "15550001@s.whatsapp.net",
                    "Mommy",
                    listened_clip="/var/lib/messagebox/assets/prompts/mommy.wav",
                )
                dashboard.listened_store().track_sent("wa-message", "family@g.us")
                body = json.dumps(
                    {
                        "EventType": "receipt",
                        "Chat": "family@g.us",
                        "Sender": "15550001@s.whatsapp.net",
                        "MessageIDs": ["wa-message"],
                        "Timestamp": "2026-07-31T12:00:00Z",
                        "Type": "played",
                        "IsFromMe": False,
                    },
                    separators=(",", ":"),
                ).encode()

                def post(signature):
                    handler = dashboard.Handler.__new__(dashboard.Handler)
                    handler.path = "/api/wacli-receipt"
                    handler.headers = {
                        "Content-Length": str(len(body)),
                        "X-Wacli-Signature": signature,
                    }
                    handler.rfile = io.BytesIO(body)
                    responses = []
                    handler._send = (
                        lambda code, payload, ctype="application/json": responses.append(
                            (code, json.loads(payload))
                        )
                    )
                    handler.do_POST()
                    return responses[0]

                self.assertEqual(post("sha256=wrong")[0], 401)
                signature = webhook_signature("private-secret", body)
                self.assertEqual(post(signature), (202, {"ok": True, "queued": 1}))
                self.assertEqual(post(signature), (202, {"ok": True, "queued": 0}))
                self.assertEqual(dashboard.listened_store().pending_count(), 1)
                events = dashboard.load_events()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["listener"], "Mommy")
            finally:
                for name, value in originals.items():
                    setattr(dashboard, name, value)

    def test_partial_event_line_does_not_break_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            original = dashboard.EVENTS_FILE
            dashboard.EVENTS_FILE = str(Path(directory) / "events.jsonl")
            try:
                Path(dashboard.EVENTS_FILE).write_text(
                    '{"type":"guided_session_started","ts":1}\n'
                    '{"type":"guided_review_played"',
                    encoding="utf-8",
                )
                self.assertEqual(
                    dashboard.load_events(),
                    [{"type": "guided_session_started", "ts": 1}],
                )
            finally:
                dashboard.EVENTS_FILE = original

    def test_counts_only_delivery_state_and_never_exposes_recipient(self):
        with tempfile.TemporaryDirectory() as directory:
            dashboard.OUTBOX_DIR = directory
            for name, state, recipient in (
                ("one", "pending", "private-person@s.whatsapp.net"),
                ("two", "failed", "private-family@g.us"),
                ("three", "uncertain", "another-private-person@s.whatsapp.net"),
            ):
                path = Path(directory) / f"{name}.job"
                path.mkdir()
                (path / "job.json").write_text(
                    json.dumps({"state": state, "recipient": recipient}),
                    encoding="utf-8",
                )
            counts = dashboard.guided_outbox_counts()
            self.assertEqual(counts, {"pending": 1, "attention": 2})
            rendered = json.dumps(counts)
            self.assertNotIn("whatsapp", rendered)
            self.assertNotIn("private", rendered)

    def test_behavior_funnel_and_timeline_correlate_without_raw_identifiers(self):
        events = [
            {
                "type": "received",
                "ts": 100,
                "file": "100-private-message-id.wav",
                "chat": "private-family@g.us",
                "sender": "Rachel",
            },
            {
                "type": "guided_session_started",
                "ts": 110,
                "session_id": "private-session-one",
                "flow": "reply",
                "source_file": "100-private-message-id.wav",
            },
            {
                "type": "guided_inbound_played",
                "ts": 130,
                "session_id": "private-session-one",
            },
            {
                "type": "guided_press",
                "ts": 136,
                "session_id": "private-session-one",
                "action": "stop_recording",
            },
            {
                "type": "guided_review_played",
                "ts": 140,
                "session_id": "private-session-one",
                "duration": 4.2,
            },
            {
                "type": "guided_approved",
                "ts": 145,
                "session_id": "private-session-one",
                "message_id": "private-outbox-one",
                "duration": 4.2,
            },
            {
                "type": "sent",
                "ts": 146,
                "message_id": "private-outbox-one",
                "whatsapp_id": "private-whatsapp-one",
                "flow": "reply",
            },
            {
                "type": "listen_receipt",
                "ts": 170,
                "whatsapp_id": "private-whatsapp-one",
                "listener": "Mommy",
            },
            {
                "type": "guided_session_started",
                "ts": 200,
                "session_id": "private-session-two",
                "flow": "standalone",
            },
            {
                "type": "guided_review_played",
                "ts": 220,
                "session_id": "private-session-two",
                "duration": 3.1,
            },
            {
                "type": "guided_deleted",
                "ts": 240,
                "session_id": "private-session-two",
                "flow": "standalone",
            },
        ]
        result = dashboard.build_guided_observability(
            events,
            {"private-family@g.us": "Family"},
            now=300,
        )
        behavior = result["behavior"]
        self.assertEqual(behavior["reply_sessions"], 1)
        self.assertEqual(behavior["reply_approved"], 1)
        self.assertEqual(behavior["reply_rate"], 100)
        self.assertEqual(behavior["review_send_rate"], 50)
        self.assertEqual(behavior["not_sent"], 1)
        self.assertEqual(behavior["standalone_sessions"], 1)
        self.assertEqual(behavior["avg_wait_to_play_s"], 30)

        reply = result["interactions"][1]
        self.assertEqual(reply["sender"], "Rachel")
        self.assertEqual(reply["chat"], "Family")
        self.assertEqual(reply["outcome"], "delivered")
        self.assertEqual(
            reply["stages"],
            [
                {"label": "Received", "state": "done"},
                {"label": "Played", "state": "done"},
                {"label": "Recorded", "state": "done"},
                {"label": "Reviewed", "state": "done"},
                {"label": "Approved", "state": "done"},
                {"label": "Sent", "state": "done"},
            ],
        )
        self.assertIn("Stop pressed", reply["journey"])
        self.assertIn("Delivered", reply["journey"])
        self.assertIn("Mommy listened", reply["journey"])
        self.assertEqual(reply["listeners"], ["Mommy"])
        rendered = json.dumps(result)
        for private_value in (
            "private-family@g.us",
            "private-session-one",
            "private-outbox-one",
            "private-whatsapp-one",
            "100-private-message-id.wav",
        ):
            self.assertNotIn(private_value, rendered)

    def test_fifo_history_and_pending_outbox_remain_explainable(self):
        events = [
            {
                "type": "received",
                "ts": 300,
                "file": "old.wav",
                "chat": "origin@g.us",
                "sender": "Anael",
            },
            {
                "type": "guided_session_started",
                "ts": 310,
                "session_id": "old-session",
                "flow": "reply",
            },
            {
                "type": "guided_inbound_played",
                "ts": 330,
                "session_id": "old-session",
            },
            {
                "type": "guided_approved",
                "ts": 350,
                "session_id": "old-session",
                "message_id": "waiting-message",
                "duration": 2.5,
            },
        ]
        result = dashboard.build_guided_observability(
            events,
            {"origin@g.us": "Family"},
            {"waiting-message": "pending"},
            now=360,
        )
        interaction = result["interactions"][0]
        self.assertEqual(interaction["sender"], "Anael")
        self.assertEqual(interaction["source_confidence"], "oldest_first")
        self.assertEqual(interaction["outcome"], "queued")
        self.assertEqual(interaction["outcome_label"], "Approved · waiting")
        self.assertEqual(interaction["stages"][-1], {"label": "Waiting", "state": "current"})

    def test_playback_only_is_complete_and_excluded_from_reply_rate(self):
        events = [
            {
                "type": "received",
                "ts": 100,
                "file": "incoming.wav",
                "chat": "origin@g.us",
                "sender": "Rachel",
            },
            {
                "type": "guided_session_started",
                "ts": 110,
                "session_id": "playback-session",
                "flow": "reply",
                "source_file": "incoming.wav",
            },
            {
                "type": "guided_inbound_played",
                "ts": 120,
                "session_id": "playback-session",
            },
            {
                "type": "guided_playback_only",
                "ts": 121,
                "session_id": "playback-session",
            },
        ]
        result = dashboard.build_guided_observability(
            events, {"origin@g.us": "Family"}, now=400
        )
        self.assertEqual(result["behavior"]["reply_sessions"], 0)
        self.assertIsNone(result["behavior"]["reply_rate"])
        interaction = result["interactions"][0]
        self.assertEqual(interaction["outcome"], "played_only")
        self.assertEqual(interaction["outcome_label"], "Played")
        self.assertIn("Playback complete", interaction["journey"])


if __name__ == "__main__":
    unittest.main()
