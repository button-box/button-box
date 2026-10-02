import json
import tempfile
import unittest
from pathlib import Path

from messagebox.test_report import TestRigError
from messagebox.test_runner import SCENARIOS, enable, finish_run, record_step, start_run


class TestRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "test-rig.json"
        self.state = self.root / "run.json"
        self.root_user = lambda: 0

    def test_enable_requires_root_and_exact_hostname(self):
        with self.assertRaises(TestRigError):
            enable(
                "button-box-003",
                config_path=self.config,
                hostname="button-box-003",
                geteuid=lambda: 501,
            )
        with self.assertRaises(TestRigError):
            enable(
                "button-box-004",
                config_path=self.config,
                hostname="button-box-003",
                geteuid=self.root_user,
            )
        enable(
            "button-box-003",
            config_path=self.config,
            hostname="button-box-003.local",
            geteuid=self.root_user,
        )
        self.assertEqual(
            json.loads(self.config.read_text(encoding="utf-8")),
            {"version": 1, "device": "button-box-003"},
        )

    def test_guided_run_preserves_partial_failure_and_finishes_nonpassing(self):
        run = start_run(
            "message-loop", state_path=self.state, clock=lambda: 0, geteuid=self.root_user
        )
        self.assertEqual(run["status"], "running")
        record_step(
            "message-received",
            "pass",
            state_path=self.state,
            clock=lambda: 1,
            geteuid=self.root_user,
        )
        record_step(
            "message-played",
            "fail",
            state_path=self.state,
            clock=lambda: 2,
            geteuid=self.root_user,
        )
        finished = finish_run(
            state_path=self.state, clock=lambda: 3, geteuid=self.root_user
        )
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["steps"]["message-received"], "pass")
        self.assertEqual(finished["steps"]["message-played"], "fail")
        self.assertIn("pending", set(finished["steps"].values()))

    def test_all_passed_or_skipped_steps_finish_as_passed(self):
        run = start_run(
            "message-loop", state_path=self.state, clock=lambda: 0, geteuid=self.root_user
        )
        for step in run["steps"]:
            record_step(
                step,
                "skip" if step == "recipient-received" else "pass",
                state_path=self.state,
                clock=lambda: 1,
                geteuid=self.root_user,
            )
        finished = finish_run(
            state_path=self.state, clock=lambda: 2, geteuid=self.root_user
        )
        self.assertEqual(finished["status"], "passed")

    def test_onboarding_requires_recipient_chooser_poll_stability(self):
        steps = SCENARIOS["onboarding"]
        self.assertIn("recipient-chooser-stable", steps)
        self.assertLess(
            steps.index("whatsapp-linked"), steps.index("recipient-chooser-stable")
        )
        self.assertLess(
            steps.index("recipient-chooser-stable"), steps.index("recipient-selected")
        )


if __name__ == "__main__":
    unittest.main()
