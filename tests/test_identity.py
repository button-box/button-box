import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from messagebox.identity import BOX_COLORS, BOX_COLOR_PATH, read_box_color, read_box_id


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


class BoxColorTests(unittest.TestCase):
    def test_missing_defaults_to_yellow_and_all_values_round_trip(self):
        self.assertEqual(BOX_COLOR_PATH, Path("/etc/messagebox-box-color"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "color"
            self.assertEqual(read_box_color(path), "yellow")
            for color in BOX_COLORS:
                with self.subTest(color=color):
                    path.write_text(color + "\n", encoding="ascii")
                    self.assertEqual(read_box_color(path), color)
                    self.assertEqual(read_box_color(path), color)
            path.write_text(" \tpink-red\n", encoding="ascii")
            self.assertEqual(read_box_color(path), "pink-red")

    def test_invalid_oversized_and_nonascii_default_without_exposing_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "color"
            for value in (b"", b"unknown", b"BLUE", b"pink-red\nprivate",
                          b"blue" + b" " * 29, b"blue\xff"):
                with self.subTest(value=value):
                    path.write_bytes(value)
                    self.assertEqual(read_box_color(path), "yellow")

    def test_symlink_nonregular_and_unreadable_default_to_yellow(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "real-color"
            target.write_text("blue\n", encoding="ascii")
            path = Path(directory) / "color"
            path.symlink_to(target)
            self.assertEqual(read_box_color(path), "yellow")
            path.unlink()
            os.mkfifo(path)
            self.assertEqual(read_box_color(path), "yellow")
            self.assertEqual(read_box_color(Path(directory)), "yellow")
            with patch("messagebox.identity.os.open", side_effect=PermissionError):
                self.assertEqual(read_box_color(target), "yellow")

    def test_operator_enum_matches_reader_and_rejects_invalid_arguments(self):
        script = Path(__file__).resolve().parents[1] / "scripts/messageboxctl"
        block = script.read_text().split("  set-color)\n", 1)[1].split("  reset-wifi)", 1)[0]
        enum = re.search(r"^      ([a-z|\-]+)\) ;;$", block, re.MULTILINE).group(1)
        self.assertEqual(tuple(enum.split("|")), BOX_COLORS)
        for args in ([], ["invalid"], ["Blue"], ["blue", "green"], ["blue\n"]):
            with self.subTest(args=args):
                result = subprocess.run(["sh", str(script), "set-color", *args],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn("set-color COLOR", result.stderr)

    def test_palette_has_exact_shell_and_button_colors_and_fixed_status_colors(self):
        css = (Path(__file__).resolve().parents[1] / "messagebox/onboarding/static/styles.css").read_text()
        palette = dict(zip(BOX_COLORS, ("#ffc94d", "#ff6b8b", "#4cc38a", "#5ea7ff",
                                        "#ffffff", "#1a1a1a", "#e8d2b0")))
        for color, value in palette.items():
            with self.subTest(color=color):
                block = re.search(r':root\[data-box-color="' + color + r'"\] \{([^}]+)\}', css).group(1)
                properties = dict(re.findall(r"(--[a-z-]+): (#[a-f0-9]+);", block))
                self.assertEqual(properties, {
                    "--box-color": value,
                    "--action": "#1a1a1a" if color in ("white", "black") else value,
                    "--action-text": "#ffffff" if color in ("white", "black") else "#1a1a1a",
                })
        for declaration in ("--danger: #a13326;", "--warning: #ffc94d;",
                            "--yellow-soft: #fff3c4;", "--green-soft: #dff8e8;",
                            "--green-dark: #128c4a;"):
            self.assertIn(declaration, css)
        for selector in (".brand-mark", ".home-hero::after"):
            block = re.search(re.escape(selector) + r" \{([^}]+)\}", css).group(1)
            self.assertIn("background: var(--box-color);", block)
