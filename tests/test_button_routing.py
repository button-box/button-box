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
        self.queue_path = root / "queue"
        self.queue_path.mkdir()
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
            mock.patch.object(button_send, "QUEUE_DIR", str(self.queue_path)),
            mock.patch.object(button_send.time, "time", return_value=1000),
            mock.patch.object(button_send, "log"),
            mock.patch.object(button_send, "log_event"),
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

    def queue_incoming(self, name="0001-message.wav"):
        wav = self.queue_path / name
        wav.write_bytes(b"incoming audio")
        sidecar = Path(str(wav) + ".json")
        sidecar.write_text(
            json.dumps({"chat": FAMILY, "msgid": "message-1"}),
            encoding="utf-8",
        )
        return wav, sidecar

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
            button_send, "beep"
        ), mock.patch.object(
            button_send, "log_event"
        ), mock.patch.object(
            button_send,
            "current_recipient_context",
            side_effect=AssertionError("recipient resolved on short press"),
        ):
            button_send.record_and_send_legacy()
        play.assert_called_once_with()

    def test_fresh_card_guided_send_preserves_queue_for_next_press(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)
        self.add_family()
        self.mark_nfc_healthy()
        selection = SelectionStore(self.selection_path)
        selection.select(CARD, GRANDMA, self.contacts.load()["revision"])
        wav, sidecar = self.queue_incoming()
        original_wav = wav.read_bytes()
        original_sidecar = sidecar.read_text(encoding="utf-8")
        sessions = []

        class FakeSession:
            def run(self, **kwargs):
                sessions.append(kwargs)
                return "sent" if kwargs["flow_kind"] == "standalone" else "played"

        button_send.led = FakeLed()
        settings = {"max_recording_seconds": 60, "after_listening": "play_only"}
        with mock.patch.object(
            button_send, "ensure_nfc_confirmation", return_value=True
        ), mock.patch.object(
            button_send, "GuidedSession", return_value=FakeSession()
        ), mock.patch.object(
            button_send, "play_pending_listened"
        ), mock.patch.object(
            button_send, "mark_queue_known"
        ), mock.patch.object(
            button_send, "refresh_led"
        ), mock.patch.object(
            button_send, "quiet_hours", return_value=False
        ):
            button_send.run_guided_once(settings)

            self.assertEqual(sessions[0]["flow_kind"], "standalone")
            self.assertEqual(sessions[0]["recipient"], GRANDMA)
            self.assertIsNone(sessions[0]["incoming_path"])
            self.assertEqual(wav.read_bytes(), original_wav)
            self.assertEqual(sidecar.read_text(encoding="utf-8"), original_sidecar)
            self.assertIsNone(selection.load())
            self.assertTrue(selection.claimed_path.exists())

            # A held card refresh cannot rearm the consumed selection. The next
            # ordinary press therefore handles the original queued message.
            self.assertFalse(
                selection.select(
                    CARD,
                    GRANDMA,
                    self.contacts.load()["revision"],
                    new_presentation=False,
                )
            )
            button_send.run_guided_once(settings)

        self.assertEqual(sessions[1]["flow_kind"], "reply")
        self.assertEqual(sessions[1]["recipient"], FAMILY)
        self.assertEqual(Path(sessions[1]["incoming_path"]).name, wav.name)
        self.assertFalse(wav.exists())
        self.assertFalse(sidecar.exists())

    def test_stale_card_selection_does_not_preempt_guided_queue(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)
        self.add_family()
        self.mark_nfc_healthy()
        SelectionStore(self.selection_path, clock=lambda: 900).select(
            CARD, GRANDMA, self.contacts.load()["revision"]
        )
        wav, _sidecar = self.queue_incoming()
        sessions = []

        class FakeSession:
            def run(self, **kwargs):
                sessions.append(kwargs)
                return "played"

        button_send.led = FakeLed()
        settings = {"max_recording_seconds": 60, "after_listening": "play_only"}
        with mock.patch.object(
            button_send, "GuidedSession", return_value=FakeSession()
        ), mock.patch.object(
            button_send, "play_pending_listened"
        ), mock.patch.object(
            button_send, "mark_queue_known"
        ), mock.patch.object(
            button_send, "refresh_led"
        ), mock.patch.object(
            button_send, "quiet_hours", return_value=False
        ):
            button_send.run_guided_once(settings)

        self.assertEqual(sessions[0]["flow_kind"], "reply")
        self.assertEqual(sessions[0]["recipient"], FAMILY)
        self.assertEqual(Path(sessions[0]["incoming_path"]).name, wav.name)

    def test_cancelled_guided_card_intent_expires_and_preserves_queue(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)
        self.add_family()
        self.mark_nfc_healthy()
        selection = SelectionStore(self.selection_path)
        selection.select(CARD, GRANDMA, self.contacts.load()["revision"])
        wav, sidecar = self.queue_incoming()

        class CancelledSession:
            def run(self, **_kwargs):
                return "deleted"

        button_send.led = FakeLed()
        with mock.patch.object(
            button_send, "ensure_nfc_confirmation", return_value=True
        ), mock.patch.object(
            button_send, "GuidedSession", return_value=CancelledSession()
        ), mock.patch.object(
            button_send, "play_pending_listened"
        ), mock.patch.object(
            button_send, "mark_queue_known"
        ), mock.patch.object(
            button_send, "refresh_led"
        ), mock.patch.object(
            button_send, "quiet_hours", return_value=False
        ), mock.patch.object(button_send, "ring_alert"):
            button_send.run_guided_once(
                {"max_recording_seconds": 60, "after_listening": "play_only"}
            )

        self.assertTrue(wav.exists())
        self.assertTrue(sidecar.exists())
        self.assertIsNone(selection.load())
        self.assertTrue(selection.claimed_path.exists())

    def test_card_claim_race_fails_closed_without_claiming_queue(self):
        self.selection_path.write_text("{}", encoding="utf-8")
        selected = {
            "contact": {"jid": GRANDMA, "label": "Grandma"},
            "uid": CARD,
            "via_card": True,
        }
        with mock.patch.object(
            button_send,
            "current_recipient_context",
            side_effect=[selected, None],
        ), mock.patch.object(
            button_send,
            "claim_oldest",
            side_effect=AssertionError("queue was claimed"),
        ), mock.patch.object(button_send, "block_unavailable_recipient") as blocked:
            button_send.run_guided_once(
                {"max_recording_seconds": 60, "after_listening": "play_only"}
            )

        blocked.assert_called_once_with()

    def test_selected_card_quick_release_never_plays_legacy_queue(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)
        self.add_family()
        self.mark_nfc_healthy()
        selection = SelectionStore(self.selection_path)
        selection.select(CARD, GRANDMA, self.contacts.load()["revision"])
        wav, sidecar = self.queue_incoming()

        with mock.patch.object(
            button_send, "acknowledge_and_classify_legacy_press", return_value="play"
        ), mock.patch.object(button_send, "wait_for_stable_open"), mock.patch.object(
            button_send, "play_next_legacy"
        ) as play:
            button_send.record_and_send_legacy({"max_recording_seconds": 60})

        play.assert_not_called()
        self.assertTrue(wav.exists())
        self.assertTrue(sidecar.exists())
        self.assertIsNone(selection.load())
        self.assertTrue(selection.claimed_path.exists())

    def test_missing_or_corrupt_legacy_recipient_sidecar_blocks(self):
        wav = Path(self.directory.name) / "legacy.wav"
        wav.write_bytes(b"audio")
        self.assertIsNone(button_send.legacy_job_recipient(str(wav)))
        Path(str(wav) + ".json").write_text("not json", encoding="utf-8")
        self.assertIsNone(button_send.legacy_job_recipient(str(wav)))
        Path(str(wav) + ".json").write_text(
            json.dumps({"recipient": GRANDMA}), encoding="utf-8"
        )
        self.assertEqual(button_send.legacy_job_recipient(str(wav)), GRANDMA)

    def test_exact_guided_reply_never_resolves_or_claims_a_card(self):
        self.contacts.add_contact(FAMILY, "Family")
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
