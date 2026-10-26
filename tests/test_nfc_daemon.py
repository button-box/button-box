"""Restart contracts through the real NFC daemon, with only the reader faked."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from messagebox import nfc
from messagebox.contacts import ContactStore
from messagebox.nfc_state import AnnouncementStore, EnrollmentStore, SelectionStore


PERSON = "15551234567@s.whatsapp.net"
OTHER = "15551234568@s.whatsapp.net"
CARD = "04:A1:00:FF"
OTHER_CARD = "04:A1:00:EE"


class ReaderStopped(BaseException):
    pass


class NfcAnnouncementTests(unittest.TestCase):
    @contextlib.contextmanager
    def box(self, mode, *, contact_count=2, cooldown=60):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            tick = [0.0]
            contacts = ContactStore(root / "contacts.json")
            contacts.add_contact(PERSON, "Card person")
            contacts.assign_card(PERSON, CARD)
            if contact_count == 2:
                contacts.add_contact(OTHER, "Another person")
                contacts.assign_card(OTHER, OTHER_CARD)
            for name, value in {
                "CONTACTS_FILE": contacts.path, "CLOUD_CONTACTS_FILE": root / "cloud-contacts.json",
                "NFC_SELECTION_FILE": root / "selection.json", "NFC_ENROLLMENT_FILE": root / "enrollment.json",
            }.items():
                stack.enter_context(mock.patch.object(nfc, name, value))
            if mode == "cloud":
                contacts.path.replace(nfc.CLOUD_CONTACTS_FILE)
            stack.enter_context(mock.patch.dict(nfc.os.environ, {"MSGBOX_TRANSPORT": mode}))
            stack.enter_context(mock.patch.object(nfc, "SelectionStore", side_effect=lambda path: SelectionStore(path, clock=lambda: 1000 + tick[0])))
            stack.enter_context(mock.patch.object(nfc, "EnrollmentStore", side_effect=lambda path: EnrollmentStore(path, clock=lambda: 1000 + tick[0])))
            stack.enter_context(mock.patch("messagebox.cloud_device.CloudDeviceClient.from_environment", side_effect=AssertionError("unexpected Cloud call")))
            announcements = AnnouncementStore(root / "announcement.json", clock=lambda: 1000 + tick[0])
            router = nfc.router(announcements)
            reader = nfc.NfcRuntime(router, nfc.Announcer(announcements), reannounce_cooldown=cooldown)

            def observe(uid, at):
                tick[0] = at
                result = reader.observe(uid, at)
                return result, announcements.take()

            yield router, observe

    def test_removal_and_wiggle_do_not_repeat_or_slide_the_cooldown(self):
        for mode in ("wacli", "cloud"):
            for gap in (0.4, 1.0):
                for count in (1, 2):
                    with self.subTest(mode=mode, gap=gap, contacts=count), self.box(mode, contact_count=count) as (router, observe):
                        _, line = observe(CARD, 0)
                        self.assertEqual(line["uid"], CARD)
                        selected = router.selection.load()
                        _, line = observe(None, gap)
                        self.assertIsNone(line)
                        if count == 2:
                            self.assertEqual(router.selection.load()["selected_at"], selected["selected_at"])
                        result, line = observe("04-a1-00-ff", gap + 0.1)
                        self.assertIsNone(line)
                        if result is not None:
                            self.assertFalse(result.announce)
                        for at in (20, 40, 59):
                            self.assertIsNone(observe(None, at)[1])
                            self.assertIsNone(observe(CARD, at + 0.1)[1])
                        self.assertIsNone(observe(None, 60)[1])
                        result, line = observe(CARD, 61)
                        self.assertTrue(result.announce)
                        self.assertEqual(line["uid"], CARD)

    def test_different_card_announces_and_selects_immediately(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.box(mode) as (router, observe):
                self.assertEqual(observe(CARD, 0)[1]["uid"], CARD)
                self.assertEqual(observe(OTHER_CARD, 0.1)[1]["uid"], OTHER_CARD)
                self.assertEqual(router.active_contact()["jid"], OTHER)
                self.assertEqual(observe(CARD, 0.2)[1]["uid"], CARD)
                self.assertEqual(router.active_contact()["jid"], PERSON)

    def test_configured_cooldown_and_unknown_card_feedback(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.box(mode, cooldown=5) as (_router, observe):
                observe(CARD, 0)
                observe(None, 1)
                self.assertIsNone(observe(CARD, 4.9)[1])
                observe(None, 6)
                self.assertEqual(observe(CARD, 6.1)[1]["uid"], CARD)
                unknown = "04:00:00:00"
                self.assertEqual(observe(unknown, 7)[1]["action"], "unknown")
                observe(None, 8)
                self.assertEqual(observe(unknown, 9)[1]["action"], "unknown")

    def test_cloud_reaction_taps_keep_their_send_handoff(self):
        with self.box("cloud") as (router, observe):
            router.contacts.add_contact("box:reaction_heart", "Heart card")
            router.contacts.assign_card("box:reaction_heart", CARD)
            self.assertEqual(observe(CARD, 0)[1]["uid"], CARD)
            self.assertIsNone(observe(None, 1)[1])
            self.assertEqual(observe(CARD, 2)[1]["uid"], CARD)

    def test_silent_replacement_rearms_selection_but_held_card_still_expires(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.box(mode) as (router, observe):
                observe(CARD, 0)
                original = router.selection.load()
                self.assertIsNone(observe(None, 1)[1])
                self.assertIsNone(observe(CARD, 2)[1])
                replacement = router.selection.load()
                self.assertNotEqual(original["selection_id"], replacement["selection_id"])
                self.assertEqual(replacement["selected_at"], 1002)
                self.assertIsNone(observe(CARD, 31)[1])
                self.assertEqual(router.selection.load()["selected_at"], 1002)
                self.assertIsNone(observe(CARD, 33)[1])
                self.assertIsNone(router.active_contact())

    def test_enrollment_feedback_bypasses_recent_selection_cooldown(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.box(mode) as (router, observe):
                observe(CARD, 0)
                router.begin_enrollment(label="Card person", jid=PERSON)
                self.assertIsNone(observe(CARD, 1)[1])
                self.assertIsNotNone(router.enrollment.active())
                self.assertIsNone(observe(None, 2)[1])
                result, line = observe(CARD, 3)
                self.assertEqual(result.action, "enrolled")
                self.assertEqual(line["action"], "enrolled")
                self.assertIsNone(router.enrollment.active())
                self.assertIsNone(observe(None, 4)[1])
                self.assertIsNone(observe(CARD, 5)[1])


class NfcDaemonRestartTests(unittest.TestCase):
    @contextlib.contextmanager
    def box(self, mode):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            wall, mono = [1000.0], [0.0]
            contacts = ContactStore(root / "contacts.json")
            contacts.add_contact(PERSON, "Card person")
            contacts.add_contact(OTHER, "Another person")
            contacts.assign_card(PERSON, CARD)
            selection = SelectionStore(root / "selection.json", clock=lambda: wall[0])
            selection.select(CARD, PERSON, contacts.load()["revision"])
            for name, value in {
                "CONTACTS_FILE": contacts.path, "CLOUD_CONTACTS_FILE": contacts.path,
                "NFC_SELECTION_FILE": selection.path, "NFC_ENROLLMENT_FILE": root / "enrollment.json",
                "NFC_ANNOUNCEMENT_FILE": root / "announcement.json", "NFC_HEALTH_FILE": root / "health",
            }.items():
                stack.enter_context(mock.patch.object(nfc, name, value))
            stack.enter_context(mock.patch.dict(nfc.os.environ, {"MSGBOX_TRANSPORT": mode}))
            stack.enter_context(mock.patch.object(nfc, "SelectionStore", side_effect=lambda path: SelectionStore(path, clock=lambda: wall[0])))
            stack.enter_context(mock.patch.object(nfc.time, "monotonic", side_effect=lambda: mono[0]))
            stack.enter_context(mock.patch("messagebox.cloud_device.CloudDeviceClient.from_environment", side_effect=AssertionError("unexpected Cloud call")))

            def run(readings):
                steps = iter(readings)

                def read():
                    try:
                        uid, at, tick = next(steps)
                    except StopIteration:
                        raise ReaderStopped()
                    wall[0], mono[0] = at, tick
                    return bytes.fromhex(uid.replace(":", "")) if uid else None

                with mock.patch.object(nfc, "hardware_reader", return_value=mock.Mock(read=read)), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(ReaderStopped):
                        nfc.run_daemon()
                self.assertFalse(nfc.NFC_HEALTH_FILE.exists())

            yield selection, contacts, wall, run

    def test_real_daemon_startup_with_removed_card_keeps_choice_and_expiry(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.box(mode) as (selection, contacts, wall, run):
                original = selection.load()
                # Two empty reads confirm absence, without deleting the choice.
                run([(None, 1005, 10), (None, 1006, 11)])
                self.assertEqual(selection.load(), original)
                run([(None, 1029, 20)])
                self.assertEqual(selection.load(), original)
                wall[0] = 1031
                self.assertEqual(selection.resolve(contacts.load())[0], "none")
                self.assertIsNone(selection.load())
                self.assertTrue(selection.claimed_path.exists())

    def test_real_daemon_startup_with_held_card_cannot_rearm_or_renew(self):
        for mode in ("wacli", "cloud"):
            for consumed in (False, True):
                with self.subTest(mode=mode, consumed=consumed), self.box(mode) as (selection, _contacts, wall, run):
                    original = selection.load()
                    if consumed:
                        selection.consume(original)
                    # A crash before the removal grace ends proves no removal.
                    run([(None, 1005, 10)])
                    run([(CARD, 1029, 20)])
                    if consumed:
                        self.assertIsNone(selection.load())
                    else:
                        self.assertEqual(selection.load()["selection_id"], original["selection_id"])
                        self.assertEqual(selection.load()["selected_at"], 1000)
                    run([(CARD, 1031, 30), (CARD, 1032, 31)])
                    self.assertIsNone(selection.load())
                    path = selection.claimed_path if consumed else selection.path
                    self.assertTrue(path.exists())
                    # Only confirmed removal followed by a new presentation rearms.
                    run([(None, 1033, 40), (None, 1034, 41), (CARD, 1035, 42)])
                    replacement = selection.load()
                    self.assertEqual(replacement["selected_at"], 1035)
                    self.assertNotEqual(replacement["selection_id"], original["selection_id"])
                    self.assertEqual(wall[0], 1035)


if __name__ == "__main__":
    unittest.main()
