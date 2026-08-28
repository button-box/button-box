import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with mock.patch.dict(sys.modules, {"gpiozero": gpiozero}):
    import messagebox.button_send as button_send  # noqa: E402
from messagebox.contacts import ContactStore  # noqa: E402
from messagebox.nfc_state import AnnouncementStore, SelectionStore  # noqa: E402


GRANDMA = "15551234567@s.whatsapp.net"
FAMILY = "120363123456789@g.us"
CARD = "04:A1:00:FF"


class FakeLed:
    def off(self):
        pass


class ButtonRoutingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.contacts_path = root / "contacts.json"
        self.selection_path = root / "nfc-selection.json"
        self.health_path = root / "nfc-health"
        self.announcement_path = root / "nfc-announcement.json"
        self.contacts = ContactStore(self.contacts_path, clock=lambda: 1000)
        self.announcements = AnnouncementStore(
            self.announcement_path, clock=lambda: 1000
        )
        self.paths = (
            mock.patch.object(button_send, "CONTACTS_FILE", str(self.contacts_path)),
            mock.patch.object(
                button_send, "NFC_SELECTION_FILE", str(self.selection_path)
            ),
            mock.patch.object(button_send, "NFC_HEALTH_FILE", self.health_path),
            mock.patch.object(
                button_send, "nfc_announcement_store", self.announcements
            ),
            mock.patch.object(button_send.time, "time", return_value=1000),
            mock.patch.object(button_send, "log"),
        )
        for patcher in self.paths:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.paths):
            patcher.stop()
        self.directory.cleanup()

    def add_grandma(self):
        return self.contacts.add_contact(
            GRANDMA,
            "Grandma",
            card_clip="/var/lib/messagebox/assets/grandma.wav",
            receive_after=0,
        )

    def mark_nfc_healthy(self):
        self.health_path.write_text("ready\n", encoding="ascii")

    def add_family(self):
        return self.contacts.add_contact(FAMILY, "Family", receive_after=0)

    def test_zero_one_and_multi_contact_routing(self):
        self.assertIsNone(button_send.current_recipient_context(claim=True))
        self.assertEqual(button_send.routing_mode(), "no_contacts")

        self.add_grandma()
        with mock.patch.object(
            button_send, "claim_selection", side_effect=AssertionError("card claimed")
        ):
            sole = button_send.current_recipient_context(claim=True)
        self.assertEqual(sole["contact"]["jid"], GRANDMA)
        self.assertFalse(sole["via_card"])
        self.assertEqual(button_send.routing_mode(), "default_recipient")

        self.contacts.assign_card(GRANDMA, CARD)
        self.mark_nfc_healthy()
        self.add_family()
        default = button_send.current_recipient_context(claim=True)
        self.assertEqual(default["contact"]["jid"], GRANDMA)
        self.assertFalse(default["via_card"])
        selection = SelectionStore(self.selection_path)
        selection.select(CARD, GRANDMA, self.contacts.load()["revision"])
        selected = button_send.current_recipient_context(claim=True)
        self.assertEqual(selected["contact"]["jid"], GRANDMA)
        self.assertTrue(selected["via_card"])
        self.assertIsNone(selection.load())
        self.assertEqual(button_send.routing_mode(), "default_recipient")

    def test_corrupt_contacts_fail_closed(self):
        self.contacts_path.write_text("not json", encoding="utf-8")
        self.assertIsNone(button_send.current_recipient_context(claim=True))
        self.assertEqual(button_send.routing_mode(), "unavailable")

    def test_cards_block_default_when_reader_or_card_state_is_unsafe(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)

        self.assertIsNone(button_send.current_recipient_context(claim=True))

        self.mark_nfc_healthy()
        self.announcements.put(action="unknown", uid="04:00:00:01", prompt="unknown.wav")
        self.assertIsNone(button_send.current_recipient_context(claim=True))

        self.announcements.clear()
        safe_default = button_send.current_recipient_context(claim=True)
        self.assertEqual(safe_default["contact"]["jid"], GRANDMA)
        self.assertFalse(safe_default["via_card"])

    def test_short_legacy_press_plays_without_resolving_or_claiming(self):
        button_send.button = types.SimpleNamespace(is_pressed=False)
        with mock.patch.object(button_send, "wait_for_stable_open"), mock.patch.object(
            button_send, "play_next_legacy"
        ) as play, mock.patch.object(
            button_send,
            "current_recipient_context",
            side_effect=AssertionError("recipient resolved on short press"),
        ):
            button_send.record_and_send_legacy()
        play.assert_called_once_with()

    def test_missing_or_corrupt_legacy_recipient_sidecar_blocks(self):
        wav = Path(self.directory.name) / "legacy.wav"
        wav.write_bytes(b"audio")
        self.assertEqual(button_send.legacy_job_recipient(str(wav)), (None, None))
        Path(str(wav) + ".json").write_text("not json", encoding="utf-8")
        self.assertEqual(button_send.legacy_job_recipient(str(wav)), (None, None))
        Path(str(wav) + ".json").write_text(
            json.dumps({"recipient": GRANDMA}), encoding="utf-8"
        )
        self.assertEqual(
            button_send.legacy_job_recipient(str(wav)), (GRANDMA, "whatsapp")
        )
        Path(str(wav) + ".json").write_text(
            json.dumps({"recipient": "+15551234567", "channel": "signal"}),
            encoding="utf-8",
        )
        self.assertEqual(
            button_send.legacy_job_recipient(str(wav)), ("+15551234567", "signal")
        )

    def test_exact_guided_reply_never_resolves_or_claims_a_card(self):
        incoming = Path(self.directory.name) / "incoming.wav"
        claim = {"path": incoming, "meta": {"chat": FAMILY}}
        captured = {}

        class FakeSession:
            def run(self, **kwargs):
                captured.update(kwargs)
                return "played"

        button_send.led = FakeLed()
        with mock.patch.object(button_send, "claim_oldest", return_value=claim), mock.patch.object(
            button_send,
            "current_recipient_context",
            side_effect=AssertionError("exact reply resolved a contact"),
        ), mock.patch.object(
            button_send, "ensure_nfc_confirmation", side_effect=AssertionError("card confirmed")
        ), mock.patch.object(
            button_send, "GuidedSession", return_value=FakeSession()
        ), mock.patch.object(
            button_send, "play_pending_listened"
        ), mock.patch.object(
            button_send, "finish_claim"
        ) as finish, mock.patch.object(
            button_send, "queued", return_value=[]
        ), mock.patch.object(
            button_send, "mark_queue_known"
        ), mock.patch.object(
            button_send, "refresh_led"
        ):
            button_send.run_guided_once()

        self.assertEqual(captured["recipient"], FAMILY)
        self.assertEqual(captured["flow_kind"], "reply")
        self.assertEqual(captured["incoming_path"], str(incoming))
        finish.assert_called_once_with(claim)


if __name__ == "__main__":
    unittest.main()
