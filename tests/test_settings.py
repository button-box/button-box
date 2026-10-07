import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from messagebox.settings import LEGACY_RINGTONES, RINGTONES, RevisionConflict, SettingsError, SettingsStore, defaults, validate


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "settings.json"

    def tearDown(self):
        self.directory.cleanup()

    def candidate(self, document, **changes):
        candidate = {
            key: value
            for key, value in document.items()
            if key not in {"version", "revision"}
        }
        candidate.update(changes)
        return candidate

    def test_migrates_supported_installed_environment_once(self):
        store = SettingsStore(
            self.path,
            environ={
                "MSGBOX_MAX_SECONDS": "120",
                "MSGBOX_RING_WAV": "/opt/messagebox/ringtones/ring4.wav",
                "MSGBOX_SPEAKER_VOLUME": "65%",
                "MSGBOX_QUIET_START_H": "21",
                "MSGBOX_QUIET_END_H": "6",
                "MSGBOX_NFC_DETECTION_BEEP": "0",
                "TZ": "Asia/Jerusalem",
            },
        )
        document, warning = store.load()
        self.assertFalse(warning)
        self.assertEqual(document["recording_mode"], "hold_release")
        self.assertEqual(document["after_listening"], "play_only")
        self.assertEqual(document["max_recording_seconds"], 120)
        self.assertEqual(document["ringtone_id"], "hello_piano")
        self.assertEqual(document["master_volume_percent"], 65)
        self.assertEqual(document["quiet_hours"], {"enabled": True, "start": "21:00", "end": "06:00"})
        self.assertFalse(document["nfc_confirmation_beep"])
        self.assertTrue(document["swoosh_sound_enabled"])

    def test_fresh_store_uses_new_defaults_and_saved_choices_are_unchanged(self):
        store = SettingsStore(self.path, environ={"TZ": "UTC"})
        fresh, warning = store.load()
        self.assertFalse(warning)
        self.assertEqual((fresh["recording_mode"], fresh["after_listening"]),
                         ("hold_release", "play_only"))
        saved = {**fresh, "revision": 7, "recording_mode": "tap_review",
                 "after_listening": "invite_reply"}
        self.path.write_text(json.dumps(saved), encoding="utf-8")
        before = self.path.read_bytes()
        loaded, warning = SettingsStore(self.path, environ={"TZ": "UTC"}).load()
        self.assertFalse(warning)
        self.assertEqual(loaded, saved)
        self.assertEqual(self.path.read_bytes(), before)

    def test_old_settings_document_keeps_existing_send_cue_enabled(self):
        initial = defaults({"TZ": "UTC"})
        initial.pop("swoosh_sound_enabled")
        self.path.write_text(json.dumps(initial), encoding="utf-8")
        document, warning = SettingsStore(self.path, environ={"TZ": "UTC"}).load()
        self.assertFalse(warning)
        self.assertTrue(document["swoosh_sound_enabled"])
        self.assertEqual(document["revision"], initial["revision"])

    def test_each_legacy_ringtone_loads_and_cloud_update_maps_without_revision_loss(self):
        for old_id in LEGACY_RINGTONES:
            with self.subTest(old_id=old_id):
                saved = {**defaults({"TZ": "UTC"}), "revision": 7,
                         "ringtone_id": old_id, "master_volume_percent": 63}
                self.path.write_text(json.dumps(saved))
                store = SettingsStore(self.path, environ={"TZ": "UTC"})
                loaded, warning = store.load()
                self.assertFalse(warning)
                self.assertEqual(loaded, {**saved, "ringtone_id": "hello_piano"})
                updated = store.update(self.candidate(loaded, ringtone_id=old_id),
                                       7, desired_revision=9)
                self.assertEqual(updated["ringtone_id"], "hello_piano")
                self.assertEqual(updated["revision"], 9)
                self.assertEqual(SettingsStore(self.path).load(), (updated, False))

    def test_each_new_ringtone_round_trips_and_unknown_id_is_rejected(self):
        store = SettingsStore(self.path, environ={"TZ": "UTC"})
        for ringtone_id in RINGTONES:
            current, _warning = store.load()
            saved = store.update(self.candidate(current, ringtone_id=ringtone_id),
                                 current["revision"])
            self.assertEqual(SettingsStore(self.path).load(), (saved, False))
        with self.assertRaises(SettingsError):
            validate({**saved, "ringtone_id": "unknown"})

    def test_swoosh_setting_requires_boolean(self):
        document = defaults({"TZ": "UTC"})
        with self.assertRaises(SettingsError):
            validate({**document, "swoosh_sound_enabled": 1})

    def test_revision_conflict_never_overwrites_newer_settings(self):
        store = SettingsStore(self.path, environ={"TZ": "UTC"})
        initial, _warning = store.load()
        saved = store.update(self.candidate(initial, max_recording_seconds=30), 0)
        with self.assertRaises(RevisionConflict):
            store.update(self.candidate(initial, max_recording_seconds=120), 0)
        current, _warning = store.load()
        self.assertEqual(current, saved)
        self.assertEqual(current["max_recording_seconds"], 30)

    def test_existing_shared_lock_does_not_require_file_ownership(self):
        store = SettingsStore(self.path, environ={"TZ": "UTC"})
        initial, _warning = store.load()
        store.lock_path.write_bytes(b"")
        store.lock_path.chmod(0o660)
        lock_inode = store.lock_path.stat().st_ino
        real_fchmod = os.fchmod

        def reject_owner_only_lock_change(descriptor, mode):
            if os.fstat(descriptor).st_ino == lock_inode:
                raise PermissionError(1, "Operation not permitted")
            return real_fchmod(descriptor, mode)

        with mock.patch(
            "messagebox.settings.os.fchmod",
            side_effect=reject_owner_only_lock_change,
        ):
            saved = store.update(self.candidate(initial, master_volume_percent=70), 0)

        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["master_volume_percent"], 70)

    def test_new_lock_is_group_writable_under_restrictive_umask(self):
        store = SettingsStore(self.path, environ={"TZ": "UTC"})
        initial, _warning = store.load()
        previous_umask = os.umask(0o077)
        try:
            store.update(self.candidate(initial, master_volume_percent=70), 0)
        finally:
            os.umask(previous_umask)

        self.assertEqual(stat.S_IMODE(store.lock_path.stat().st_mode), 0o660)

    def test_corrupt_primary_uses_last_valid_snapshot_and_attention(self):
        store = SettingsStore(self.path, environ={"TZ": "UTC"})
        initial, _warning = store.load()
        saved = store.update(self.candidate(initial, ringtone_id="gentle_music_box"), 0)
        self.path.write_text("not json", encoding="utf-8")
        recovered, warning = store.load()
        self.assertTrue(warning)
        self.assertEqual(recovered, saved)

    def test_invalid_candidate_does_not_change_document(self):
        store = SettingsStore(self.path, environ={"TZ": "UTC"})
        initial, _warning = store.load()
        with self.assertRaises(SettingsError):
            store.update(self.candidate(initial, max_recording_seconds=45), 0)
        current, _warning = store.load()
        self.assertEqual(current, initial)

    def test_safe_defaults_match_caregiver_contract(self):
        document = defaults({"TZ": "UTC"})
        self.assertEqual(document["recording_mode"], "hold_release")
        self.assertEqual(document["after_listening"], "play_only")
        self.assertEqual(document["max_recording_seconds"], 60)
        self.assertEqual(document["ringtone_id"], "hello_piano")
        self.assertEqual(document["arrival_signal"], "ring_and_lamp")
        self.assertEqual(document["quiet_hours"], {"enabled": True, "start": "22:00", "end": "07:00"})


if __name__ == "__main__":
    unittest.main()
