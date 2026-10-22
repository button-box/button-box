"""Bounded runtime Wi-Fi recovery; never enters onboarding or changes the AP."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from messagebox.onboarding.connectivity import _has_default_route
from messagebox.onboarding.mode import transition_lock
from messagebox.onboarding.paths import ONBOARDING_ENABLED_PATH, MODE_TRANSITION_LOCK_PATH

STATE_PATH = Path('/run/messagebox-wifi-watchdog/state.json')


def recovery_decision(state, *, now, runtime, connected, saved):
    """Return the next outage state and whether one reconnect attempt is due."""
    if not runtime or connected or not saved:
        return {}, False
    since = state.get('since', now)
    if not isinstance(since, (int, float)) or since > now:
        since = now
    attempts = state.get('attempts', 0)
    if type(attempts) is not int or attempts < 0:
        attempts = 0
    next_try = state.get('next_try', since + 120)
    if not isinstance(next_try, (int, float)) or next_try > now + 900:
        next_try = since + 120
    result = {'since': since, 'attempts': attempts, 'next_try': next_try}
    due = now - since >= 120 and now >= next_try
    if due:
        result['attempts'] += 1
        result['next_try'] = now + min(900, 120 * 2 ** min(attempts, 3))
    return result, due


def command(args, run):
    return run(args, check=True, capture_output=True, text=True, timeout=20).stdout.rstrip("\n")


def saved_connections(run=subprocess.run, *, autoconnect_only=True):
    """Select autoconnect infrastructure profiles applicable to the built-in Wi-Fi."""
    rows = command(['nmcli', '-t', '-f', 'UUID,TYPE', 'connection', 'show'], run)
    saved = []
    for row in rows.splitlines():
        uuid, _, kind = row.partition(':')
        if kind not in {'802-11-wireless', 'wifi'}:
            continue
        values = command(['nmcli', '-g', '802-11-wireless.mode,connection.interface-name,connection.autoconnect',
                          'connection', 'show', 'uuid', uuid], run).splitlines()
        if len(values) == 3 and values[0] in ('', 'infrastructure') and values[1] in ('', 'wlan0') and (not autoconnect_only or values[2] == 'yes'):
            saved.append(uuid)
    return saved


def configure_connections(run=subprocess.run):
    for uuid in saved_connections(run, autoconnect_only=False):
        command(['nmcli', 'connection', 'modify', 'uuid', uuid,
                 'connection.autoconnect-retries', '0'], run)


def check_once(*, run=subprocess.run, state_path=STATE_PATH, enabled_path=ONBOARDING_ENABLED_PATH,
               clock=time.monotonic, lock_path=MODE_TRANSITION_LOCK_PATH):
    state_path = Path(state_path)
    if Path(enabled_path).exists():
        state_path.unlink(missing_ok=True)
        return False
    try:
        state = json.loads(state_path.read_text())
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    # A default route on another interface does not prove home Wi-Fi recovered.
    nm = command(['nmcli', '-g', 'GENERAL.STATE', 'device', 'show', 'wlan0'], run)
    routes = command(['ip', 'route', 'show', 'default'], run)
    connected = nm.startswith('100') and _has_default_route(routes, 'wlan0')
    saved = saved_connections(run) if not connected else []
    state, due = recovery_decision(state, now=clock(), runtime=True, connected=connected, saved=bool(saved))
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = state_path.with_suffix('.tmp')
    with temporary.open('w') as handle:
        os.chmod(temporary, 0o600)
        json.dump(state, handle)
    temporary.replace(state_path)
    if due:
        # Serialize the mutation with setup/reset, without holding up observations.
        with transition_lock(lock_path):
            if Path(enabled_path).exists():
                return False
            print('Wi-Fi unavailable for two minutes; attempting saved infrastructure connection', flush=True)
            uuid = saved[(state['attempts'] - 1) % len(saved)]
            try:
                command(['nmcli', 'connection', 'up', 'uuid', uuid, 'ifname', 'wlan0'], run)
                print('Wi-Fi reconnect request accepted', flush=True)
            except (OSError, subprocess.SubprocessError):
                print('Wi-Fi reconnect failed; retry is backed off', flush=True)
    return due


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configure', action='store_true')
    args = parser.parse_args()
    try:
        if args.configure:
            configure_connections()
        else:
            check_once()
    except (OSError, subprocess.SubprocessError):
        print('Wi-Fi recovery check unavailable', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
