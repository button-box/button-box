"""Bounded system-clock readiness check for time-sensitive device actions."""

import subprocess


def time_synchronized(*, command_runner=subprocess.run):
    try:
        result = command_runner(
            ["/usr/bin/timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True, text=True, check=False, shell=False, timeout=2,
        )
        return result.returncode == 0 and isinstance(result.stdout, str) and result.stdout.strip() == "yes"
    except Exception:
        # Missing system services or uncertain output must keep the gate closed.
        return False
