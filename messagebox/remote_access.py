"""Report whether anyone can log in to this box remotely (SSH or Tailscale)."""

import subprocess

UNITS = {"ssh": ("ssh.service", "ssh.socket"), "tailscale": ("tailscaled.service",)}


def _active(unit, run):
    try:
        result = run(["systemctl", "is-active", "--quiet", unit], check=False, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode == 0


def remote_access(run=subprocess.run):
    """Box-observed state only; a field is omitted when it cannot be read."""
    report = {}
    for name, units in UNITS.items():
        states = [_active(unit, run) for unit in units]
        if any(states):
            report[name] = "on"
        elif all(state is False for state in states):
            report[name] = "off"
    return {"remote_access": report} if len(report) == len(UNITS) else {}
