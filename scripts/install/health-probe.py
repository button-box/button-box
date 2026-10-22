#!/usr/bin/env python3
"""Print updater health checks without writing state or capturing audio."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# A read-only probe must not create import caches on the fixed installation.
sys.dont_write_bytecode = True
import bounded_update as update  # noqa: E402


def current_journal(root):
    pointer = update.controlled_release(root)
    release = update._rooted(root, pointer)
    identity = json.loads((release / 'release.json').read_bytes())
    previous = update.read_state(root, 'journal.json', {})
    units = previous.get('units') or {unit: {'active': 'active'} for unit in (
        'messagebox-button.service', 'messagebox-poller.service',
        'messagebox-dash.service', 'messagebox-nfc.service')}
    hashes = []
    manifest = release / 'release-manifest.json'
    if manifest.is_file():
        for entry in json.loads(manifest.read_bytes())['files']:
            if entry['installed'].startswith('/opt/messagebox/'):
                hashes.append({'relative': entry['installed'].removeprefix('/opt/messagebox/'),
                               'sha256': entry['sha256']})
    else:
        # Legacy converted trees may have no manifest. Report the missing proof.
        hashes.append({'relative': 'release-manifest.json', 'sha256': ''})
    return {'new': pointer, 'version': identity['version'], 'commit': identity['commit'],
            'boot': update._boot(), 'started_mono': 0, 'units': units, 'hashes': hashes,
            'auxiliary_units': previous.get('auxiliary_units', [])}


def main(arguments=None, *, root=Path('/'), run=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--journal', type=Path, help='live journal (default: updater journal.json)')
    source.add_argument('--current', action='store_true', help='check the running release identity, without transaction binding')
    parser.add_argument('--local', action='store_true', help='controller trial: local producer readiness only')
    options = parser.parse_args(arguments)
    try:
        if options.current:
            journal = current_journal(root)
        elif options.journal:
            journal = json.loads(options.journal.read_bytes())
        else:
            journal = update.read_state(root, 'journal.json')
        if not isinstance(journal, dict) or journal.get('kind') == 'bootstrap':
            raise ValueError('no application health journal')
        if options.local:
            journal['local_readiness'] = True
        checks, _pids = update.health_checks(root, journal, run)
    except (OSError, ValueError, KeyError, TypeError, update.UpdateError):
        print('FAIL probe:inputs')
        return 1
    for name, passed in checks:
        print(('PASS ' if passed else 'FAIL ') + name)
    return int(any(not passed for _name, passed in checks))


if __name__ == '__main__':
    raise SystemExit(main())
