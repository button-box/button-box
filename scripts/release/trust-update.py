#!/usr/bin/env python3
"""Build, manually sign, or verify emergency release-key replacement assets."""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from release import ROOT, ReleaseError, regular_file, signing_secret

sys.path.insert(0, str(ROOT / 'scripts/install'))
from release_verify import public_key_id, public_key_line
from trust_update import parse_document, verify_trust


def emergency_pin(path):
    path = regular_file(path).resolve()
    if path.parent != (ROOT / 'config/emergency-keys').resolve() or path.suffix != '.pub':
        raise ReleaseError('select a pinned .pub file in config/emergency-keys')
    public_key_line(path)
    return path


def verify(directory, pin):
    pin = emergency_pin(pin)
    # Verify only the selected reviewed pin, not candidate-supplied keys.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='button-box-emergency-pin-') as temporary:
        key = Path(temporary) / pin.name
        key.write_bytes(pin.read_bytes())
        return verify_trust(directory, temporary)


def build(directory, keys, sequence, revoked, created):
    directory = Path(directory).resolve()
    if directory.is_relative_to(ROOT):
        raise ReleaseError('trust artifacts must remain outside the repository')
    value = {'schema': 1, 'product': 'button-box', 'trust_sequence': sequence,
             'release_keys': [public_key_line(regular_file(key)) for key in keys],
             'revoked_key_ids': revoked, 'created': created}
    raw = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    parse_document(raw)
    emergency_ids = {public_key_id(public_key_line(key)) for key in (ROOT / 'config/emergency-keys').glob('*.pub')}
    if emergency_ids & {public_key_id(line) for line in value['release_keys']}:
        raise ReleaseError('key_role_overlap')
    directory.mkdir(mode=0o700)
    (directory / 'trust-update.json').write_bytes(raw)


def sign(directory, secret, pin):
    directory = Path(directory).resolve()
    secret = signing_secret(directory, secret)
    pin = emergency_pin(pin)
    document = regular_file(directory / 'trust-update.json')
    value = parse_document(document.read_bytes())
    if public_key_id(public_key_line(pin)) in {public_key_id(line) for line in value['release_keys']}:
        raise ReleaseError('key_role_overlap')
    signature = directory / 'trust-update.json.minisig'
    if signature.exists():
        raise ReleaseError('signature already exists; refusing to overwrite it')
    subprocess.run(['minisign', '-S', '-m', str(document), '-s', str(secret), '-x', str(signature),
                    '-c', 'Button Box trust update signature',
                    '-t', 'button-box trust sequence ' + str(value['trust_sequence'])], check=True)
    verify(directory, pin)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('build')
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--release-key', type=Path, action='append', required=True)
    command.add_argument('--trust-sequence', type=int, required=True)
    command.add_argument('--revoked-key-id', action='append', default=[])
    command.add_argument('--created', default=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    for name in ('sign', 'verify'):
        command = commands.add_parser(name)
        command.add_argument('directory', type=Path)
        command.add_argument('--public-key', type=Path, required=True)
        if name == 'sign':
            command.add_argument('--secret-key', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'build':
            build(args.output, args.release_key, args.trust_sequence, args.revoked_key_id, args.created)
        elif args.command == 'sign':
            sign(args.directory, args.secret_key, args.public_key)
        else:
            verify(args.directory, args.public_key)
        print('Trust update ' + args.command + ' complete; no upload performed.')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, 'Trust update failed: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
