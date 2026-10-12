"""Emergency-only trust updates; durable generations outside application rollback."""

import fcntl
import json
import os
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from release_verify import ReleaseError, public_key_id, public_key_line

MAX_TRUST = 8192
TRUST_TAG = re.compile(r'trust-v[1-9][0-9]{0,18}\Z')


def strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ReleaseError('invalid_trust_update')
        value[key] = item
    return value


def parse_document(raw):
    if len(raw) > MAX_TRUST:
        raise ReleaseError('trust_update_limit')
    try:
        value = json.loads(raw, object_pairs_hook=strict_object)
        if (not isinstance(value, dict) or set(value) != {
                'schema', 'product', 'trust_sequence', 'release_keys', 'revoked_key_ids', 'created'}
                or type(value['schema']) is not int or value['schema'] != 1
                or value['product'] != 'button-box'
                or type(value['trust_sequence']) is not int or not 0 < value['trust_sequence'] < 2**63
                or not isinstance(value['release_keys'], list) or not 1 <= len(value['release_keys']) <= 3
                or not isinstance(value['revoked_key_ids'], list)
                or any(not isinstance(key, str) or not re.fullmatch(r'[0-9A-F]{16}', key)
                       for key in value['revoked_key_ids'])
                or len(set(value['revoked_key_ids'])) != len(value['revoked_key_ids'])
                or not isinstance(value['created'], str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})', value['created'])
                or datetime.fromisoformat(value['created'].replace('Z', '+00:00')).tzinfo is None):
            raise ReleaseError('invalid_trust_update')
        ids = [public_key_id(line) for line in value['release_keys']]
        if len(set(ids)) != len(ids) or set(ids) & set(value['revoked_key_ids']):
            raise ReleaseError('invalid_trust_update')
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ReleaseError('invalid_trust_update') from exc


def verify_trust(directory, emergency_keys):
    keys = sorted(Path(emergency_keys).glob('*.pub'))
    if not keys:
        raise ReleaseError('trust_updates_disabled')
    directory = Path(directory)
    document, signature = directory / 'trust-update.json', directory / 'trust-update.json.minisig'
    if (not document.is_file() or document.stat().st_size > MAX_TRUST
            or not signature.is_file() or signature.stat().st_size > 4096):
        raise ReleaseError('trust_update_limit')
    for key in keys:
        public_key_line(key)
        try:
            result = subprocess.run(['minisign', '-V', '-H', '-m', str(document), '-x', str(signature), '-p', str(key)],
                                    capture_output=True, check=False, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseError('verifier_unavailable') from exc
        if result.returncode == 0:
            value = parse_document(document.read_bytes())
            emergency_ids = {public_key_id(public_key_line(item)) for item in keys}
            if emergency_ids & {public_key_id(line) for line in value['release_keys']}:
                raise ReleaseError('key_role_overlap')
            return value
    raise ReleaseError('bad_trust_signature')


def fsync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_json(path, value):
    temporary = path.with_name(path.name + '.part')
    with temporary.open('w') as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, sort_keys=True)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    fsync_dir(path.parent)


@contextmanager
def trust_lock(state, *, blocking=False):
    descriptor = os.open(Path(state) / 'trust.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            # The broker must never wait while an install needs heartbeats.
            # The systemd-bounded worker may wait before any install mutation.
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise ReleaseError('trust_update_busy') from exc
        yield


def trust_state(state):
    try:
        value = json.loads((Path(state) / 'trust-sequence.json').read_bytes())
    except FileNotFoundError:
        return {'trust_sequence': 0, 'revoked_key_ids': []}
    if (not isinstance(value, dict) or type(value.get('trust_sequence')) is not int
            or value['trust_sequence'] < 0 or not isinstance(value.get('revoked_key_ids'), list)
            or any(not isinstance(key, str) or not re.fullmatch(r'[0-9A-F]{16}', key) for key in value['revoked_key_ids'])):
        raise ReleaseError('invalid_trust_state')
    return value


def switch_keys(config, value, *, checkpoint=lambda phase: None):
    """The visible *.pub set is always a complete, fsynced generation."""
    config = Path(config)
    pointer = config / 'release-keys'
    if pointer.exists() and not pointer.is_symlink():
        raise ReleaseError('trust_layout_requires_bootstrap')
    generations = config / 'release-keysets'
    generations.mkdir(mode=0o755, exist_ok=True)
    generations.chmod(0o755)
    generation = generations / uuid.uuid4().hex
    generation.mkdir(mode=0o755)
    generation.chmod(0o755)
    for line in value['release_keys']:
        path = generation / (public_key_id(line) + '.pub')
        with path.open('x') as output:
            path.chmod(0o644)
            output.write('untrusted comment: Button Box release public key\n' + line + '\n')
            output.flush()
            os.fsync(output.fileno())
    fsync_dir(generation)
    fsync_dir(generations)
    fsync_dir(config)
    checkpoint('prepared')
    temporary = config / 'release-keys.next'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(generation.relative_to(config))
    os.replace(temporary, pointer)
    fsync_dir(config)
    checkpoint('switched')
    # Retain only the active generation. No rollback may restore older trust.
    for old in generations.iterdir():
        if old != generation and old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
    fsync_dir(generations)


def recover_trust(config, state, *, checkpoint=lambda phase: None):
    """Caller holds trust.lock. A durable intent always rolls forward on restart."""
    config, state = Path(config), Path(state)
    pending = state / 'trust-pending.json'
    if not pending.exists():
        return
    value = json.loads(pending.read_bytes())
    current = trust_state(state)
    if (value['trust_sequence'] < current['trust_sequence']
            or not set(current['revoked_key_ids']) <= set(value['revoked_key_ids'])):
        raise ReleaseError('invalid_trust_state')
    switch_keys(config, value, checkpoint=checkpoint)
    durable_json(state / 'trust-sequence.json', value)
    checkpoint('recorded')
    pending.unlink()
    fsync_dir(state)
    print('software update: release trust replaced; sequence ' + str(value['trust_sequence']), flush=True)


def apply_trust(directory, config, state, *, checkpoint=lambda phase: None):
    config, state = Path(config), Path(state)
    with trust_lock(state):
        recover_trust(config, state)
        value = verify_trust(directory, config / 'emergency-keys')
        current = trust_state(state)
        if value['trust_sequence'] <= current['trust_sequence']:
            raise ReleaseError('trust_sequence_not_newer')
        revoked = set(current['revoked_key_ids']) | set(value['revoked_key_ids'])
        if revoked & {public_key_id(line) for line in value['release_keys']}:
            raise ReleaseError('revoked_release_key')
        if not (config / 'release-keys').is_symlink():
            raise ReleaseError('trust_layout_requires_bootstrap')
        value['revoked_key_ids'] = sorted(revoked)
        durable_json(state / 'trust-pending.json', value)
        checkpoint('intent')
        recover_trust(config, state, checkpoint=checkpoint)
        return value['trust_sequence']


def bootstrap_keys(source, config):
    """Validate both roles before writing any pins; no emergency pin is required."""
    source, config = Path(source), Path(config)
    release = [public_key_line(key) for key in sorted((source / 'release-keys').glob('*.pub'))]
    emergency = [public_key_line(key) for key in sorted((source / 'emergency-keys').glob('*.pub'))]
    if {public_key_id(line) for line in release} & {public_key_id(line) for line in emergency}:
        raise ReleaseError('key_role_overlap')
    if len({public_key_id(line) for line in release}) != len(release):
        raise ReleaseError('duplicate_release_key')
    # Validate existing emergency pins before changing either role. A retry
    # must not overwrite a different pin or follow an unsafe pin symlink.
    destination = config / 'emergency-keys'
    if destination.is_symlink():
        raise ReleaseError('emergency_pin_changed')
    expected = {public_key_id(line): line for line in emergency}
    for key in destination.glob('*.pub'):
        if key.is_symlink() or public_key_line(key) != expected.get(key.stem):
            raise ReleaseError('emergency_pin_changed')
    config.mkdir(mode=0o755, parents=True, exist_ok=True)
    switch_keys(config, {'release_keys': release})
    destination = config / 'emergency-keys'
    destination.mkdir(mode=0o755, exist_ok=True)
    destination.chmod(0o755)
    for line in emergency:
        key = destination / (public_key_id(line) + '.pub')
        if key.exists():
            continue  # The canonical public-key line was checked above.
        with key.open('x') as output:
            key.chmod(0o644)
            output.write('untrusted comment: Button Box emergency public key\n' + line + '\n')
            output.flush()
            os.fsync(output.fileno())
    fsync_dir(destination)
    fsync_dir(config)
