import tempfile
import unittest
from pathlib import Path

from src.contacts import ContactStore
from src.listened_receipts import (
    AnnouncementGate,
    ReceiptStore,
    canonical_jid,
    load_listener_profiles,
    parse_wacli_send_id,
    valid_webhook_signature,
    webhook_signature,
)


class AnnouncementGateTests(unittest.TestCase):
    def test_idle_checks_are_prompt_throttled_and_busy_audio_is_skipped(self):
        gate = AnnouncementGate(poll_seconds=0.2, retry_seconds=30)
        self.assertFalse(gate.ready(busy=True, now=10))
        self.assertTrue(gate.ready(busy=False, now=10))
        self.assertFalse(gate.ready(busy=False, now=10.19))
        self.assertTrue(gate.ready(busy=False, now=10.2))

    def test_failed_audio_backs_off_until_retry_window_or_success(self):
        gate = AnnouncementGate(poll_seconds=0.2, retry_seconds=30)
        self.assertTrue(gate.ready(busy=False, now=10))
        gate.blocked(now=10)
        self.assertFalse(gate.ready(busy=False, now=39.99))
        self.assertTrue(gate.ready(busy=False, now=40))
        gate.blocked(now=40)
        gate.succeeded()
        self.assertTrue(gate.ready(busy=False, now=40.2))


class SendResponseTests(unittest.TestCase):
    def test_extracts_only_a_successful_bounded_message_id(self):
        self.assertEqual(
            parse_wacli_send_id('{"sent":true,"id":"3EB0ABC","to":"family@g.us"}'),
            "3EB0ABC",
        )
        for raw in ("", "not-json", '{"sent":false,"id":"x"}', '{"sent":true}'):
            self.assertIsNone(parse_wacli_send_id(raw))

    def test_signature_matches_wacli_sha256_contract(self):
        body = b'{"EventType":"receipt"}'
        signature = webhook_signature("private-secret", body)
        self.assertTrue(valid_webhook_signature("private-secret", body, signature))
        self.assertFalse(valid_webhook_signature("private-secret", body + b"x", signature))
        self.assertFalse(valid_webhook_signature("", body, signature))


class ListenerProfileTests(unittest.TestCase):
    def test_profiles_load_from_unified_store_and_are_isolated_from_contacts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contacts.json"
            store = ContactStore(path)
            store.add_contact(
                "15550002@s.whatsapp.net", "Grandma", receive_after=0
            )
            store.upsert_listener(
                "15550001:7@s.whatsapp.net",
                "  Mommy  ",
                listened_clip="/var/lib/messagebox/assets/mommy.wav",
            )
            profiles = load_listener_profiles(str(path))
            self.assertEqual(canonical_jid("15550001:4@s.whatsapp.net"), "15550001@s.whatsapp.net")
            self.assertEqual(
                profiles,
                {
                    "15550001@s.whatsapp.net": {
                        "name": "Mommy",
                        "clip": "/var/lib/messagebox/assets/mommy.wav",
                    }
                },
            )
            self.assertNotIn("15550002@s.whatsapp.net", profiles)

    def test_missing_and_corrupt_contacts_have_no_listener_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contacts.json"
            self.assertEqual(load_listener_profiles(str(path)), {})
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(load_listener_profiles(str(path)), {})


class ReceiptStoreTests(unittest.TestCase):
    def test_tracked_group_receipt_queues_once_per_listener_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReceiptStore(directory)
            self.assertTrue(
                store.track_sent(
                    "wa-message-1",
                    "family@g.us",
                    local_message_id="local-1",
                    flow="reply",
                    sent_at=100,
                )
            )
            payload = {
                "EventType": "receipt",
                "Chat": "family@g.us",
                "Sender": "15550001:9@s.whatsapp.net",
                "MessageIDs": ["wa-message-1", "untracked-message"],
                "Timestamp": "2026-07-31T12:00:00Z",
                "Type": "played",
                "IsFromMe": False,
            }
            profiles = {
                "15550001@s.whatsapp.net": {
                    "name": "Mommy",
                    "clip": "/private/prompts/mommy.wav",
                }
            }
            notices = store.ingest_played(
                payload, profiles, "/private/prompts/someone.wav", received_at=120
            )
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0].listener_name, "Mommy")
            self.assertEqual(store.pending_count(), 1)
            self.assertEqual(store.ingest_played(payload, profiles, "fallback.wav"), [])

            claimed = store.claim_next()
            self.assertIsNotNone(claimed)
            self.assertEqual(ReceiptStore(directory).recover_inflight(), 1)
            claimed = store.claim_next()
            store.complete(claimed, announced_at=130)
            self.assertEqual(store.pending_count(), 0)
            self.assertEqual(store.ingest_played(payload, profiles, "fallback.wav"), [])

    def test_direct_receipt_falls_back_to_chat_and_generic_clip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReceiptStore(directory)
            store.track_sent("wa-direct", "15550002@s.whatsapp.net")
            notices = store.ingest_played(
                {
                    "EventType": "receipt",
                    "Chat": "15550002@s.whatsapp.net",
                    "Sender": "",
                    "MessageIDs": ["wa-direct"],
                    "Type": "played",
                },
                {},
                "/private/prompts/someone.wav",
            )
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0].listener_name, "Someone")
            self.assertEqual(notices[0].clip, "/private/prompts/someone.wav")

    def test_nonplayed_wrong_chat_and_untracked_receipts_do_not_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReceiptStore(directory)
            store.track_sent("wa-1", "family@g.us")
            base = {
                "EventType": "receipt",
                "Chat": "family@g.us",
                "Sender": "person@s.whatsapp.net",
                "MessageIDs": ["wa-1"],
                "Type": "read",
            }
            self.assertEqual(store.ingest_played(base, {}, "fallback.wav"), [])
            base["Type"] = "played"
            base["Chat"] = "other@g.us"
            self.assertEqual(store.ingest_played(base, {}, "fallback.wav"), [])
            base["Chat"] = "family@g.us"
            base["MessageIDs"] = ["unknown"]
            self.assertEqual(store.ingest_played(base, {}, "fallback.wav"), [])


if __name__ == "__main__":
    unittest.main()
