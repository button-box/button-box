import json
import os
import tempfile
import unittest
from pathlib import Path

from src.onboarding.state import PROOFS, WHATSAPP_PROOFS, StateError, StateStore


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        self.value += 1
        return self.value


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "nested" / "onboarding.json"
        self.store = StateStore(self.path, clock=Clock())

    def tearDown(self):
        self.directory.cleanup()

    def authenticated_store(self):
        return self.store.initialize()

    def test_initialize_is_private_and_idempotent(self):
        state = self.store.initialize()
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["phase"], "WIFI_SELECT")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.store.lock_path).st_mode & 0o777, 0o600)
        self.assertEqual(self.store.initialize(), state)

    def test_happy_path_has_generations_dispatch_and_connectivity_proofs(self):
        self.authenticated_store()
        connecting = self.store.begin_connect("Home Wi-Fi")
        self.assertEqual(connecting["generation"], 1)
        self.assertEqual(connecting["phase"], "WIFI_CONNECTING")
        self.assertTrue(self.store.mark_dispatched(1))
        self.assertFalse(self.store.mark_dispatched(1))
        associated = self.store.mark_associated(1)
        self.assertEqual(associated["phase"], "WIFI_ASSOCIATED")
        state = self.store.record_connectivity_result(PROOFS)
        self.assertEqual(state["phase"], "WHATSAPP_PENDING")
        self.assertEqual(set(state["proofs"]), PROOFS)

        ready = self.store.mark_whatsapp_ready(WHATSAPP_PROOFS)
        self.assertEqual(ready["phase"], "WHATSAPP_READY")
        self.assertEqual(set(ready["proofs"]), PROOFS | WHATSAPP_PROOFS)

        pending = self.store.mark_whatsapp_unlinked()
        self.assertEqual(pending["phase"], "WHATSAPP_PENDING")
        self.assertEqual(set(pending["proofs"]), PROOFS)

    def test_whatsapp_transitions_require_exact_content_free_proofs(self):
        self.authenticated_store()
        self.store.begin_connect("Home")
        self.store.mark_associated(1)
        self.store.record_connectivity_result(PROOFS)
        for invalid in (set(), {"whatsapp_authenticated"}, {"private_identity"}):
            with self.subTest(invalid=invalid), self.assertRaises(StateError):
                self.store.mark_whatsapp_ready(invalid)

    def test_stale_generation_fails_closed(self):
        self.authenticated_store()
        self.store.begin_connect("Home")
        with self.assertRaises(StateError):
            self.store.mark_dispatched(0)

    def test_failure_is_safe_and_can_retry(self):
        self.authenticated_store()
        self.store.begin_connect("Home")
        with self.assertRaises(StateError):
            self.store.fail("password was super-secret")
        failed = self.store.fail("ASSOCIATION_FAILED")
        self.assertEqual(failed["phase"], "WIFI_FAILED")
        self.assertEqual(failed["safe_error"], "ASSOCIATION_FAILED")
        retry = self.store.begin_connect("Other")
        self.assertEqual(retry["generation"], 2)
        self.assertIsNone(retry["safe_error"])

    def test_reconcile_hotspot_fails_only_an_interrupted_attempt(self):
        self.authenticated_store()
        self.assertEqual(self.store.reconcile_hotspot()["phase"], "WIFI_SELECT")
        self.store.begin_connect("Home")
        state = self.store.reconcile_hotspot()
        self.assertEqual(state["phase"], "WIFI_FAILED")
        self.assertEqual(state["safe_error"], "ASSOCIATION_FAILED")

    def test_reset_removes_attempt_data_and_history(self):
        self.authenticated_store()
        self.store.begin_connect("Private SSID")
        state = self.store.reset()
        self.assertEqual(state["phase"], "WIFI_SELECT")
        self.assertEqual(state["generation"], 2)
        self.assertEqual(len(state["transitions"]), 1)

    def test_recreate_reset_recovers_missing_or_corrupt_state(self):
        self.path.parent.mkdir(parents=True)
        recreated = self.store.reset(recreate=True)
        self.assertEqual(recreated["phase"], "WIFI_SELECT")
        self.path.write_text("not-json", encoding="utf-8")
        recreated = self.store.reset(recreate=True)
        self.assertEqual(recreated["phase"], "WIFI_SELECT")
        self.assertEqual(recreated["generation"], 0)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires O_NOFOLLOW")
    def test_lock_file_symlink_is_rejected(self):
        self.path.parent.mkdir(parents=True)
        target = self.path.parent / "target"
        target.write_text("unchanged", encoding="ascii")
        self.store.lock_path.symlink_to(target)
        with self.assertRaises(OSError):
            self.store.initialize()
        self.assertEqual(target.read_text(encoding="ascii"), "unchanged")

    def test_connectivity_observation_replaces_stale_proofs_atomically(self):
        self.authenticated_store()
        self.store.begin_connect("Home")
        self.store.mark_associated(1)
        partial = {"wlan0_nm_active", "wlan0_non_ap"}
        state = self.store.record_connectivity_result(partial, "WLAN0_NO_IPV4")
        self.assertEqual(state["phase"], "WIFI_ASSOCIATED")
        self.assertEqual(set(state["proofs"]), partial)
        self.assertEqual(state["safe_error"], "WLAN0_NO_IPV4")

        state = self.store.record_connectivity_result(PROOFS)
        self.assertEqual(state["phase"], "WHATSAPP_PENDING")

    def test_malformed_unknown_version_and_unknown_fields_fail_closed(self):
        self.path.parent.mkdir(parents=True)
        for content in ("not-json", "[]", '{"version":2}', '{"version":1,"extra":true}'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(StateError):
                    self.store.load()

if __name__ == "__main__":
    unittest.main()
