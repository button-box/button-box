import unittest

from scripts.dev.validate_acceptance_run import validate


MATRIX = """| ID | Required evidence | Expected result |
|---|---|---|
| BB-HOME-01 | UI+API | Ready. |
| BB-NFC-03 | PHYSICAL | Tag works. |
"""


def valid_run():
    return {
        "run_id": "RUN-1", "unit_id": "UNIT-1", "matrix_revision": "abc1234",
        "deployed_code_revision": "def5678", "tested_code_revision": "def5678",
        "unit_configuration_revision": "cfg-2", "tested_configuration_revision": "cfg-2",
        "started_at": "2026-09-14T12:00:00Z", "tester": "fixture-1",
        "features": {"nfc": True},
        "cases": [
            {"id": "BB-HOME-01", "status": "Passed", "evidence": ["UI", "API"], "evidence_ref": "evidence/home"},
            {"id": "BB-NFC-03", "status": "Passed", "evidence": ["PHYSICAL"], "evidence_ref": "evidence/nfc"},
        ],
    }


class AcceptanceRunTests(unittest.TestCase):
    def test_complete_current_run_is_ready(self):
        self.assertEqual(validate(valid_run(), MATRIX, "abc1234"), [])

    def test_rejects_failed_missing_and_stale_evidence(self):
        run = valid_run()
        run["matrix_revision"] = "old"
        run["tested_configuration_revision"] = "cfg-1"
        run["cases"][0]["evidence"] = ["API"]
        run["cases"][1]["status"] = "Blocked"
        errors = validate(run, MATRIX, "abc1234")
        self.assertIn("stale matrix revision", errors)
        self.assertIn("stale configuration revision", errors)
        self.assertIn("BB-HOME-01: missing evidence UI", errors)
        self.assertIn("BB-NFC-03: Blocked", errors)

    def test_not_fitted_is_limited_to_optional_nfc(self):
        run = valid_run()
        run["features"]["nfc"] = False
        run["cases"][1] = {"id": "BB-NFC-03", "status": "Not fitted"}
        self.assertEqual(validate(run, MATRIX, "abc1234"), [])
        run["cases"][0] = {"id": "BB-HOME-01", "status": "Not fitted"}
        self.assertIn("BB-HOME-01: Not fitted is not applicable", validate(run, MATRIX, "abc1234"))
