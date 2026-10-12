import subprocess
import unittest
from types import SimpleNamespace

from messagebox.remote_access import remote_access


def runner(active):
    def run(command, **_kwargs):
        if command[-1] == "boom":
            raise OSError
        return SimpleNamespace(returncode=0 if command[-1] in active else 3)
    return run


class RemoteAccessTests(unittest.TestCase):
    def test_both_off(self):
        self.assertEqual(remote_access(runner(set())), {"remote_access": {"ssh": "off", "tailscale": "off"}})

    def test_ssh_socket_counts_as_on(self):
        self.assertEqual(remote_access(runner({"ssh.socket"}))["remote_access"]["ssh"], "on")

    def test_tailscale_on(self):
        self.assertEqual(remote_access(runner({"tailscaled.service"}))["remote_access"]["tailscale"], "on")

    def test_unreadable_state_reports_nothing(self):
        def run(command, **_kwargs):
            raise subprocess.TimeoutExpired(command, 3)
        self.assertEqual(remote_access(run), {})


if __name__ == "__main__":
    unittest.main()
