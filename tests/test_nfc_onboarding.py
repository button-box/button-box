import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.contacts import ContactError, ContactStore
from src.nfc import (
    Announcer,
    NfcRuntime,
    main,
)
from src.nfc_state import (
    AnnouncementStore,
    EnrollmentStore,
    NfcError,
    NfcRouter,
    SelectionStore,
    active_selection,
    claim_selection,
    normalize_uid,
)


GRANDMA = "15551234567@s.whatsapp.net"
FAMILY = "120363123456789@g.us"
CARD_ONE = "04:A1:00:FF"
CARD_TWO = "04:A1:00:EE"
GRANDMA_CLIP = "/var/lib/messagebox/assets/grandma.wav"
FAMILY_CLIP = "/var/lib/messagebox/assets/family.wav"


class Clock:
    def __init__(self, value=1_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeAnnouncer:
    def __init__(self):
        self.results = []

    def announce(self, result):
        if result.announce:
            self.results.append(result)


class FailingAnnouncementStore:
    def put(self, **_kwargs):
        raise RuntimeError("audio handoff failed")


class NfcTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.contacts_path = root / "state" / "contacts.json"
        self.selection_path = root / "run" / "nfc-selection.json"
        self.enrollment_path = root / "run" / "nfc-enrollment.json"
        self.announcement_path = root / "run" / "nfc-announcement.json"
        self.clock = Clock()
        self.contacts = ContactStore(self.contacts_path, clock=self.clock)
        self.selection = SelectionStore(self.selection_path, clock=self.clock)
        self.enrollment = EnrollmentStore(self.enrollment_path, clock=self.clock)
        self.announcements = AnnouncementStore(
            self.announcement_path, clock=self.clock
        )
        self.router = NfcRouter(
            self.contacts,
            self.selection,
            self.enrollment,
            self.announcements,
        )

    def tearDown(self):
        self.directory.cleanup()

    def add_grandma(self, uid=None):
        contact = self.contacts.add_contact(
            GRANDMA,
            "Grandma",
            card_clip=GRANDMA_CLIP,
            receive_after=10,
        )
        if uid is not None:
            self.contacts.assign_card(GRANDMA, uid)
        return contact

    def add_family(self, uid=None):
        contact = self.contacts.add_contact(
            FAMILY,
            "Family",
            card_clip=FAMILY_CLIP,
            receive_after=20,
        )
        if uid is not None:
            self.contacts.assign_card(FAMILY, uid)
        return contact

    def test_uid_normalization_is_shared_with_contacts(self):
        self.assertEqual(normalize_uid(b"\x04\xa1\x00\xff"), CARD_ONE)
        self.assertEqual(normalize_uid("04-a1 00:ff"), CARD_ONE)
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, "04-a1 00:ff")
        self.assertEqual(self.contacts.resolve_card(b"\x04\xa1\x00\xff")["jid"], GRANDMA)
        for invalid in ("", "123", "not-a-card", "00:11:22", object()):
            with self.subTest(invalid=invalid):
                with self.assertRaises(NfcError):
                    normalize_uid(invalid)

    def test_zero_one_and_multi_contact_routing(self):
        unknown = self.router.card_seen(CARD_ONE)
        self.assertEqual(unknown.action, "unknown")
        self.assertTrue(unknown.announce)
        self.assertIsNone(self.selection.load())

        self.add_grandma(CARD_ONE)
        recognized = self.router.card_seen(CARD_ONE)
        self.assertEqual(recognized.action, "recognized")
        self.assertEqual(recognized.contact["jid"], GRANDMA)
        self.assertTrue(recognized.announce)
        self.assertIsNone(self.selection.load())

        # A one-contact scan cannot spring to life after configuration changes.
        self.add_family(CARD_TWO)
        self.assertIsNone(
            active_selection(
                self.contacts_path, self.selection_path, clock=self.clock
            )
        )

        selected = self.router.card_seen(CARD_ONE)
        self.assertEqual(selected.action, "selected")
        snapshot = self.selection.load()
        self.assertEqual(snapshot["uid"], CARD_ONE)
        self.assertEqual(snapshot["jid"], GRANDMA)
        self.assertEqual(
            snapshot["contacts_revision"], self.contacts.load()["revision"]
        )
        refreshed = self.router.card_seen(CARD_ONE, new_presentation=False)
        self.assertEqual(refreshed.action, "refreshed")
        self.assertFalse(refreshed.announce)

    def test_unknown_or_sole_contact_scan_clears_an_older_selection(self):
        self.add_grandma(CARD_ONE)
        self.add_family(CARD_TWO)
        self.router.card_seen(CARD_ONE)
        self.assertIsNotNone(self.selection.load())

        self.contacts.remove_contact(FAMILY)
        self.router.card_seen(CARD_ONE)
        self.assertIsNone(self.selection.load())

        self.router.card_seen("10:20:30:40")
        self.assertIsNone(self.selection.load())

    def test_claim_is_one_shot_and_same_presentation_cannot_recreate_it(self):
        self.add_grandma(CARD_ONE)
        self.add_family(CARD_TWO)
        self.router.card_seen(CARD_ONE, new_presentation=True)

        claimed = claim_selection(
            self.contacts_path,
            self.selection_path,
            max_age=30,
            clock=self.clock,
        )
        self.assertEqual(claimed["jid"], GRANDMA)
        self.assertEqual(claimed["contact"]["label"], "Grandma")
        self.assertIsNone(
            claim_selection(
                self.contacts_path,
                self.selection_path,
                max_age=30,
                clock=self.clock,
            )
        )

        held = self.router.card_seen(CARD_ONE, new_presentation=False)
        self.assertEqual(held.action, "refreshed")
        self.assertIsNone(self.router.active_contact())

        self.router.card_absent()
        presented_again = self.router.card_seen(CARD_ONE, new_presentation=True)
        self.assertEqual(presented_again.action, "selected")
        self.assertEqual(self.router.active_contact()["jid"], GRANDMA)

    def test_selection_expires_and_removal_leaves_it_latched(self):
        self.add_grandma(CARD_ONE)
        self.add_family(CARD_TWO)
        self.router.card_seen(CARD_ONE)
        self.assertEqual(self.router.card_absent().action, "removed")
        self.assertEqual(self.router.active_contact(max_age=2.5)["jid"], GRANDMA)

        self.clock.advance(2.6)
        self.assertIsNone(self.router.active_contact(max_age=2.5))

    def test_any_stale_contacts_revision_fails_closed_and_is_consumed(self):
        self.add_grandma(CARD_ONE)
        self.add_family(CARD_TWO)
        self.router.card_seen(CARD_ONE)
        self.contacts.upsert_listener("15550001@s.whatsapp.net", "Mom")

        self.assertIsNone(
            claim_selection(
                self.contacts_path,
                self.selection_path,
                clock=self.clock,
            )
        )
        self.assertFalse(self.selection_path.exists())
        self.assertFalse(self.selection.claimed_path.exists())

    def test_card_reassignment_fails_closed_instead_of_changing_recipient(self):
        self.add_grandma(CARD_ONE)
        self.add_family(CARD_TWO)
        self.router.card_seen(CARD_ONE)
        self.contacts.assign_card(FAMILY, CARD_ONE)

        self.assertIsNone(
            claim_selection(
                self.contacts_path,
                self.selection_path,
                clock=self.clock,
            )
        )
        self.assertIsNone(self.router.active_contact())

    def test_enrollment_is_locked_expires_and_cancels_conditionally(self):
        request = self.router.begin_enrollment(
            label="Grandma", jid=GRANDMA, ttl_s=10
        )
        with self.assertRaisesRegex(NfcError, "already active"):
            self.router.begin_enrollment(label="Family", jid=FAMILY, ttl_s=10)

        self.assertFalse(self.router.cancel_enrollment("different-request"))
        self.assertEqual(
            self.enrollment.active()["request_id"], request["request_id"]
        )
        self.assertTrue(self.router.cancel_enrollment(request["request_id"]))
        self.assertIsNone(self.enrollment.active())

        expired = self.router.begin_enrollment(
            label="Grandma", jid=GRANDMA, ttl_s=10
        )
        self.clock.advance(11)
        self.assertIsNone(self.enrollment.active())
        self.assertFalse(self.enrollment_path.exists())
        self.assertFalse(self.router.cancel_enrollment(expired["request_id"]))

    def test_claim_transition_is_atomic_and_completion_is_request_scoped(self):
        request = self.router.begin_enrollment(label="Grandma", jid=GRANDMA)
        claimed = self.enrollment.claim(CARD_ONE)
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["claimed_uid"], CARD_ONE)
        self.assertIsNone(self.enrollment.claim(CARD_TWO))
        self.assertFalse(self.enrollment.complete("different-request"))
        self.assertEqual(
            self.enrollment.active()["request_id"], request["request_id"]
        )

    def test_new_contact_and_card_are_one_revision_and_second_contact_is_selected(self):
        self.add_grandma(CARD_ONE)
        before = self.contacts.load()["revision"]
        self.router.begin_enrollment(
            label="Family",
            jid=FAMILY,
            card_clip=FAMILY_CLIP,
        )

        result = self.router.card_seen(CARD_TWO)

        document = self.contacts.load()
        self.assertEqual(result.action, "enrolled")
        self.assertEqual(result.contact["jid"], FAMILY)
        self.assertEqual(document["revision"], before + 1)
        self.assertEqual(document["contacts"][FAMILY]["card_uids"], [CARD_TWO])
        self.assertIsNone(self.enrollment.active())
        selection = self.selection.load()
        self.assertEqual(selection["jid"], FAMILY)
        self.assertEqual(selection["contacts_revision"], document["revision"])
        self.assertEqual(self.announcements.take()["action"], "enrolled")

    def test_first_enrolled_contact_announces_but_does_not_select(self):
        self.router.begin_enrollment(label="Grandma", jid=GRANDMA)
        result = self.router.card_seen(CARD_ONE)

        self.assertEqual(result.action, "enrolled")
        self.assertTrue(result.announce)
        self.assertEqual(self.contacts.load()["revision"], 1)
        self.assertIsNone(self.selection.load())
        self.assertEqual(self.announcements.take()["action"], "enrolled")

    def test_held_card_does_not_satisfy_new_enrollment(self):
        self.add_grandma(CARD_ONE)
        self.router.card_seen(CARD_ONE, new_presentation=True)
        self.router.begin_enrollment(label="Family", jid=FAMILY)

        held = self.router.card_seen(CARD_ONE, new_presentation=False)

        self.assertEqual(held.action, "recognized")
        self.assertIsNotNone(self.enrollment.active())
        self.assertIsNone(self.contacts.contact(FAMILY))

        enrolled = self.router.card_seen(CARD_TWO, new_presentation=True)
        self.assertEqual(enrolled.action, "enrolled")
        self.assertEqual(self.contacts.resolve_card(CARD_TWO)["jid"], FAMILY)

    def test_claimed_enrollment_recovers_after_restart_idempotently(self):
        self.add_grandma(CARD_ONE)
        request = self.router.begin_enrollment(label="Family", jid=FAMILY)
        self.enrollment.claim(CARD_TWO)
        failing_router = NfcRouter(
            self.contacts,
            self.selection,
            self.enrollment,
            FailingAnnouncementStore(),
        )

        with self.assertRaisesRegex(RuntimeError, "audio handoff"):
            failing_router.reconcile_enrollment(request["request_id"])
        revision_after_contact_write = self.contacts.load()["revision"]
        self.assertEqual(self.enrollment.active()["status"], "claimed")
        self.assertEqual(self.contacts.resolve_card(CARD_TWO)["jid"], FAMILY)

        restarted = NfcRouter(
            ContactStore(self.contacts_path, clock=self.clock),
            SelectionStore(self.selection_path, clock=self.clock),
            EnrollmentStore(self.enrollment_path, clock=self.clock),
            self.announcements,
        )
        recovered = restarted.reconcile_enrollment()

        self.assertEqual(recovered.action, "enrolled")
        self.assertEqual(recovered.contact["jid"], FAMILY)
        self.assertEqual(
            self.contacts.load()["revision"], revision_after_contact_write
        )
        self.assertIsNone(self.enrollment.active())
        self.assertEqual(restarted.active_contact()["jid"], FAMILY)
        self.assertEqual(self.announcements.take()["action"], "enrolled")

    def test_existing_contact_keeps_metadata_and_accepts_extra_cards(self):
        self.add_grandma(CARD_ONE)
        self.add_family()
        original = self.contacts.contact(GRANDMA)
        before = self.contacts.load()["revision"]
        self.router.begin_enrollment(
            label="Conflicting staged label",
            jid=GRANDMA,
            card_clip=FAMILY_CLIP,
            create_contact=False,
        )

        result = self.router.card_seen(CARD_TWO)

        current = self.contacts.contact(GRANDMA)
        self.assertEqual(result.action, "enrolled")
        self.assertEqual(current["label"], original["label"])
        self.assertEqual(current["card_clip"], original["card_clip"])
        self.assertEqual(current["receive_after"], original["receive_after"])
        self.assertEqual(current["card_uids"], [CARD_ONE, CARD_TWO])
        self.assertEqual(self.contacts.load()["revision"], before + 1)

    def test_removed_contact_is_not_recreated_by_additional_card_enrollment(self):
        self.add_grandma(CARD_ONE)
        self.router.begin_enrollment(
            label="Grandma",
            jid=GRANDMA,
            create_contact=False,
        )
        self.contacts.remove_contact(GRANDMA)

        with self.assertRaisesRegex(ContactError, "no longer exists"):
            self.router.card_seen(CARD_TWO)

        self.assertIsNone(self.contacts.contact(GRANDMA))
        self.assertIsNone(self.enrollment.active())

    def test_enrollment_reassigns_a_card_in_the_same_contact_transaction(self):
        self.add_grandma(CARD_ONE)
        self.add_family(CARD_TWO)
        before = self.contacts.load()["revision"]
        self.router.begin_enrollment(label="Ignored", jid=FAMILY)

        self.router.card_seen(CARD_ONE)

        self.assertEqual(self.contacts.resolve_card(CARD_ONE)["jid"], FAMILY)
        self.assertEqual(self.contacts.contact(GRANDMA)["card_uids"], [])
        self.assertEqual(self.contacts.load()["revision"], before + 1)

    def test_status_redacts_all_card_uids_including_claimed_enrollment(self):
        self.add_grandma(CARD_ONE)
        self.add_family(CARD_TWO)
        self.router.card_seen(CARD_ONE)
        self.router.begin_enrollment(label="Family", jid=FAMILY)
        self.enrollment.claim("10:20:30:40")

        status = self.router.status()
        rendered = json.dumps(status)
        self.assertNotIn(CARD_ONE, rendered)
        self.assertNotIn(CARD_TWO, rendered)
        self.assertNotIn("10:20:30:40", rendered)
        self.assertNotIn("claimed_uid", rendered)
        self.assertNotIn("card_uids", rendered)
        self.assertEqual(status["active"]["jid"], GRANDMA)
        self.assertEqual(status["contacts"][GRANDMA]["card_count"], 1)
        self.assertEqual(status["enrollment"]["status"], "claimed")

    def test_simulate_scan_and_status_cli_never_print_uids(self):
        self.add_grandma(CARD_ONE)
        self.add_family(CARD_TWO)
        output = io.StringIO()
        patches = (
            mock.patch("src.nfc.CONTACTS_FILE", self.contacts_path),
            mock.patch("src.nfc.NFC_SELECTION_FILE", self.selection_path),
            mock.patch("src.nfc.NFC_ENROLLMENT_FILE", self.enrollment_path),
            mock.patch("src.nfc.NFC_ANNOUNCEMENT_FILE", self.announcement_path),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["simulate-scan", CARD_ONE]), 0)
                self.assertEqual(main(["status"]), 0)

        rendered = output.getvalue()
        self.assertNotIn(CARD_ONE, rendered)
        self.assertNotIn(CARD_TWO, rendered)
        self.assertNotIn("card_uids", rendered)
        self.assertIn("Grandma", rendered)

    def test_announcement_handoff_is_single_use_expiring_and_acknowledged(self):
        self.announcements.put(
            action="selected", uid=CARD_ONE, prompt=GRANDMA_CLIP
        )
        self.assertEqual(
            self.announcements.take(),
            {"action": "selected", "uid": CARD_ONE, "prompt": GRANDMA_CLIP},
        )
        self.assertIsNone(self.announcements.take())

        self.announcements.acknowledge(CARD_ONE)
        self.assertTrue(self.announcements.is_acknowledged(CARD_ONE))
        self.announcements.put(action="unknown", uid=CARD_TWO, prompt="")
        self.assertFalse(self.announcements.is_acknowledged(CARD_ONE))
        self.clock.advance(11)
        self.assertIsNone(self.announcements.take(max_age=10))
        self.assertFalse(self.announcement_path.exists())

    def test_runtime_debounces_announcements_refreshes_and_removal(self):
        self.add_grandma(CARD_ONE)
        self.add_family(CARD_TWO)
        announcer = FakeAnnouncer()
        runtime = NfcRuntime(
            self.router, announcer, removal_grace=0.8, refresh=0.5
        )

        first = runtime.observe("04-a1-00-ff", 10.0)
        self.assertEqual(first.action, "selected")
        self.assertEqual(len(announcer.results), 1)
        self.assertIsNone(runtime.observe(CARD_ONE, 10.2))
        refreshed = runtime.observe(CARD_ONE, 10.6)
        self.assertEqual(refreshed.action, "refreshed")
        self.assertEqual(len(announcer.results), 1)
        self.assertIsNone(runtime.observe(None, 11.0))
        self.assertEqual(runtime.observe(None, 11.5).action, "removed")
        self.assertEqual(self.router.active_contact()["jid"], GRANDMA)

        self.assertEqual(runtime.observe(CARD_ONE, 12.0).action, "selected")
        self.assertEqual(len(announcer.results), 2)

    def test_runtime_requires_unknown_card_to_be_represented_for_enrollment(self):
        self.add_grandma(CARD_ONE)
        announcer = FakeAnnouncer()
        runtime = NfcRuntime(
            self.router, announcer, removal_grace=0.8, refresh=0.5
        )

        first = runtime.observe(CARD_TWO, 10.0)
        self.assertEqual(first.action, "unknown")
        self.assertEqual(len(announcer.results), 1)
        self.assertIsNone(runtime.observe(CARD_TWO, 10.6))
        self.assertEqual(len(announcer.results), 1)

        self.router.begin_enrollment(label="Family", jid=FAMILY)
        self.assertIsNone(runtime.observe(CARD_TWO, 11.2))
        self.assertEqual(runtime.observe(None, 12.1).action, "removed")
        enrolled = runtime.observe(CARD_TWO, 12.2)
        self.assertEqual(enrolled.action, "enrolled")
        self.assertEqual(enrolled.contact["label"], "Family")
        self.assertEqual(len(announcer.results), 2)
        self.assertEqual(self.router.active_contact()["jid"], FAMILY)

    def test_announcer_uses_contact_clip_and_removal_keeps_pending_audio(self):
        announcer = Announcer(self.announcements)
        self.add_grandma(CARD_ONE)
        self.add_family(CARD_TWO)

        selected = self.router.card_seen(CARD_ONE)
        announcer.announce(selected)
        self.assertTrue(self.announcement_path.exists())
        announcer.announce(self.router.card_absent())
        self.assertTrue(self.announcement_path.exists())
        self.assertEqual(self.announcements.take()["prompt"], GRANDMA_CLIP)


if __name__ == "__main__":
    unittest.main()
