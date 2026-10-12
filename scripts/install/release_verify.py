"""Offline release verification; shared by downloader and privileged installer."""

import base64
import hashlib
import json
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

UPDATER_VERSION = (1, 0, 0)
TAG = re.compile(r"(?=.{1,64}\Z)v[0-9]+\.[0-9]+\.[0-9]+(?:-canary\.[0-9]+)?\Z")
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_FILES = 10000
# Production boards. A release must also list the model in supported_models.
SUPPORTED_MODELS = ('Raspberry Pi 4 Model B', 'Raspberry Pi 3 Model A Plus')


class ReleaseError(ValueError):
    """Bounded reason code, safe to report without command output."""


def public_key_id(line):
    try:
        data = base64.b64decode(line, validate=True)
    except (ValueError, TypeError) as exc:
        raise ReleaseError('invalid_public_key') from exc
    if len(data) != 42 or data[:2] != b'Ed' or base64.b64encode(data).decode() != line:
        raise ReleaseError('invalid_public_key')
    return data[2:10][::-1].hex().upper()


def public_key_line(path):
    lines = Path(path).read_text(encoding='ascii').splitlines()
    if len(lines) != 2 or not lines[0].startswith('untrusted comment: '):
        raise ReleaseError('invalid_public_key')
    public_key_id(lines[1])
    return lines[1]


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def verify_bundle(directory, keys, *, version, channel, floor, platform, run=subprocess.run, verify_archive=True):
    directory, keys = Path(directory), Path(keys)
    # Resolve the root-owned generation once: a concurrent pointer switch may
    # reject a download, but must never mix keys from two sets.
    emergency = {public_key_id(public_key_line(key))
                 for key in (keys.parent / 'emergency-keys').glob('*.pub')}
    keys = keys.resolve()
    if not isinstance(version, str) or not TAG.fullmatch(version):
        raise ReleaseError('invalid_version')
    sums = directory / 'SHA256SUMS'
    signature = directory / 'SHA256SUMS.minisig'
    if not sums.is_file() or sums.stat().st_size > 4096 or not signature.is_file() or signature.stat().st_size > 4096:
        raise ReleaseError('incomplete_download')
    verified = False
    for key in sorted(keys.glob('*.pub')):
        if public_key_id(public_key_line(key)) in emergency:
            continue
        try:
            result = run(['minisign', '-Vm', str(sums), '-x', str(signature), '-p', str(key)],
                         capture_output=True, check=False, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseError('verifier_unavailable') from exc
        if result.returncode == 0:
            verified = True
            break
    if not verified:
        raise ReleaseError('bad_signature')
    archive_name = f'button-box-{version}.tar.gz'
    hashes = {}
    for line in sums.read_text(encoding='ascii').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9._-]+)', line)
        if not match or match[2] in hashes:
            raise ReleaseError('invalid_hashes')
        hashes[match[2]] = match[1]
    if set(hashes) != {archive_name, 'release-manifest.json'}:
        raise ReleaseError('invalid_hashes')
    for name, expected in hashes.items():
        if name == archive_name and not verify_archive:
            continue
        path = directory / name
        limit = MAX_ARCHIVE if name == archive_name else 2 * 1024 * 1024
        if not path.is_file() or path.stat().st_size > limit or digest(path) != expected:
            raise ReleaseError('hash_mismatch')
    try:
        manifest = json.loads((directory / 'release-manifest.json').read_bytes())
    except (OSError, ValueError) as exc:
        raise ReleaseError('invalid_manifest') from exc
    if (not isinstance(manifest, dict) or manifest.get('schema') != 1
            or manifest.get('product') != 'button-box' or manifest.get('version') != version or manifest.get('tag') != version
            or not isinstance(manifest.get('commit'), str) or not re.fullmatch(r'[0-9a-f]{40}', manifest['commit'])
            or type(manifest.get('release_sequence')) is not int or manifest['release_sequence'] <= floor
            or not isinstance(manifest.get('files'), list)):
        raise ReleaseError('invalid_identity_or_downgrade')
    signed_channel = manifest.get('channel')
    if (signed_channel not in {'stable', 'canary'} or channel not in {'stable', 'canary'}
            or signed_channel != ('canary' if '-canary.' in version else 'stable')
            or channel == 'stable' and signed_channel != 'stable'):
        raise ReleaseError('wrong_channel')
    minimum = manifest.get('min_updater_version')
    if (not isinstance(minimum, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', minimum)
            or tuple(map(int, minimum.split('.'))) > UPDATER_VERSION
            or platform['model'] not in SUPPORTED_MODELS
            or any(not isinstance(manifest.get(key), list) or platform[field] not in manifest[key]
                   for key, field in (('supported_os', 'os'), ('supported_arch', 'architecture'), ('supported_models', 'model')))):
        raise ReleaseError('incompatible')
    return manifest


def extract_bundle(directory, destination, version):
    """No links, devices, duplicate names or tar extraction callbacks."""
    archive = Path(directory) / f'button-box-{version}.tar.gz'
    destination = Path(destination)
    if destination.exists():
        raise ReleaseError('staging_exists')
    try:
        with tarfile.open(archive, 'r:gz') as source:
            members = []
            total, names = 0, set()
            for member in source:
                if len(members) >= MAX_FILES:
                    raise ReleaseError("archive_limit")
                members.append(member)
                path = PurePosixPath(member.name)
                if (path.is_absolute() or '..' in path.parts or str(path) != member.name.rstrip('/')
                        or not path.parts or member.name.rstrip('/') in names
                        or not (member.isfile() or member.isdir()) or member.size < 0):
                    raise ReleaseError('unsafe_archive')
                names.add(member.name.rstrip('/'))
                total += member.size
                if total > MAX_EXPANDED:
                    raise ReleaseError('archive_limit')
            destination.mkdir(mode=0o755, parents=True)
            for member in members:
                target = destination / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o755)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    with source.extractfile(member) as incoming, target.open('xb') as output:
                        while block := incoming.read(1024 * 1024):
                            output.write(block)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise ReleaseError('unsafe_archive') from exc
    return destination


def local_platform(root=Path('/')):
    root = Path(root)
    values = {}
    for line in (root / 'etc/os-release').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            values[key] = value.strip('"')
    import platform
    codename = values.get('VERSION_CODENAME', '')
    os_id = ('raspios-' + codename if (root / 'etc/rpi-issue').exists() else values['ID'] + '-' + codename)
    model = (root / 'proc/device-tree/model').read_text().rstrip('\x00\n')
    # Hardware revision suffixes are not separate supported model families.
    for family in SUPPORTED_MODELS:
        if model.startswith(family):
            model = family
    return {'os': os_id, 'architecture': platform.machine(), 'model': model}
