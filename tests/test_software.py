import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from messagebox import software


class InstalledSoftwareTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "release.json"
        self.patch = patch.object(software, "RELEASE_FILE", self.path)
        self.patch.start()
        software.installed_release.cache_clear()

    def tearDown(self):
        software.installed_release.cache_clear()
        self.patch.stop()
        self.directory.cleanup()

    def test_present_manifest_is_cached_until_restart(self):
        self.path.write_text(json.dumps({"version": "0.3.0", "commit": "abc1234" * 5,
                                         "manifest_sha256": "a" * 64}))
        self.assertEqual(software.software_fields(), {
            "software_version": "0.3.0", "software_commit": "abc1234" * 5,
        })
        self.assertIn(b"Software 0.3.0 (abc1234)", software.software_footer())
        self.path.write_text(json.dumps({"version": "0.3.1", "commit": "def5678"}))
        self.assertEqual(software.installed_release()[0], "0.3.0")
        software.installed_release.cache_clear()  # A restarted service has a new cache.
        self.assertEqual(software.software_fields(), {
            "software_version": "0.3.1", "software_commit": "def5678",
        })

    def test_missing_or_unreadable_manifest_is_omitted(self):
        self.assertEqual(software.software_fields(), {})
        self.assertEqual(software.software_footer(), b"")
        software.installed_release.cache_clear()
        with patch.object(software.os, "open", side_effect=PermissionError):
            self.assertEqual(software.software_fields(), {})

    def test_invalid_manifest_is_omitted(self):
        invalid = [b"not json", b"\xff", b"[]", b"null", b"{" * 2000, b" " * 4097]
        invalid += [json.dumps(document).encode() for document in (
            {}, {"version": "0.3.0"}, {"commit": "abc1234"},
            *({"version": version, "commit": "abc1234"} for version in (
                None, 3, "", "a" * 65, "0.3.0\n", "0.3.0 beta", "<script>", "é",
            )),
            *({"version": "0.3.0", "commit": commit} for commit in (
                None, 1234567, "abcdef", "a" * 41, "ABC1234", "abc123g", "abc1234\n",
            )),
        )]
        for payload in invalid:
            with self.subTest(payload=payload[:100]):
                self.path.write_bytes(payload)
                software.installed_release.cache_clear()
                self.assertEqual(software.software_fields(), {})
                self.assertEqual(software.software_footer(), b"")

    def test_nonregular_and_symlink_records_are_omitted(self):
        self.path.mkdir()
        self.assertEqual(software.software_fields(), {})
        self.path.rmdir()
        target = self.path.with_name("target.json")
        target.write_text('{"version":"0.3.0","commit":"abc1234"}')
        self.path.symlink_to(target)
        software.installed_release.cache_clear()
        self.assertEqual(software.software_fields(), {})

    def test_wire_format_boundaries_are_accepted(self):
        for version, commit in (("a" * 64, "a" * 40), ("0.3.0-rc.1+build_2", "abc1234")):
            self.path.write_text(json.dumps({"version": version, "commit": commit}))
            software.installed_release.cache_clear()
            self.assertEqual(software.installed_release(), (version, commit))
