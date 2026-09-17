import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from messagebox.identity import read_box_id


class BoxIdentityTests(unittest.TestCase):
    def test_assigned_id_is_stable_across_reads_and_missing_is_not_a_hostname(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity"
            self.assertIsNone(read_box_id(path))
            path.write_text("BOX-42\n", encoding="ascii")
            self.assertEqual(read_box_id(path), "BOX-42")
            self.assertEqual(read_box_id(path), "BOX-42")

    def test_malformed_private_or_oversized_values_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity"
            for value in (b"", b"hostname", b"BOX-0", b"BOX-01", b"BOX-1\nsecret",
                          b"BOX-1" + b" " * 40, b"BOX-1234567890", b"\xff"):
                with self.subTest(value=value):
                    path.write_bytes(value)
                    self.assertIsNone(read_box_id(path))

    def test_nonregular_symlink_and_unreadable_identity_are_unassigned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity"
            path.symlink_to(Path(directory))
            self.assertIsNone(read_box_id(path))
            path.unlink()
            os.mkfifo(path)
            self.assertIsNone(read_box_id(path))
            self.assertIsNone(read_box_id(Path(directory)))
            with patch("messagebox.identity.os.open", side_effect=PermissionError):
                self.assertIsNone(read_box_id(path))
