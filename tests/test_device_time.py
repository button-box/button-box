import subprocess
import unittest
from unittest import mock

from messagebox.device_time import time_synchronized


class DeviceTimeTests(unittest.TestCase):
    def test_fixed_bounded_command_accepts_only_explicit_sync_proof(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "yes\n", ""))
        self.assertTrue(time_synchronized(command_runner=runner))
        runner.assert_called_once_with(
            ["/usr/bin/timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True, text=True, check=False, shell=False, timeout=2,
        )

    def test_uncertain_failed_or_malformed_results_fail_closed(self):
        results = [
            subprocess.CompletedProcess([], code, output, "")
            for code, output in ((0, "no\n"), (0, ""), (0, "unknown"),
                                 (0, "yes\nno"), (0, "NTPSynchronized=yes"),
                                 (0, b"yes"), (0, None), (1, "yes\n"))
        ]
        results.append(object())
        for result in results:
            with self.subTest(result=result):
                self.assertFalse(time_synchronized(command_runner=lambda *args, **kwargs: result))

    def test_missing_command_timeout_and_runner_errors_fail_closed(self):
        for error in (FileNotFoundError(), PermissionError(),
                      subprocess.TimeoutExpired("timedatectl", 2), RuntimeError()):
            with self.subTest(error=type(error).__name__):
                runner = mock.Mock(side_effect=error)
                self.assertFalse(time_synchronized(command_runner=runner))


if __name__ == "__main__":
    unittest.main()
