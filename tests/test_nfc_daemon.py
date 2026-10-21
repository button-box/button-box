"""Restart contracts through the real NFC daemon, with only the reader faked."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from messagebox import nfc
from messagebox.contacts import ContactStore
from messagebox.nfc_state import SelectionStore


PERSON = "15551234567@s.whatsapp.net"
OTHER = "15551234568@s.whatsapp.net"
CARD = "04:A1:00:FF"


class ReaderStopped(BaseException):
    pass


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
