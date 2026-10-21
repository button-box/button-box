"""Offline release verification; shared by downloader and privileged installer."""

import ast
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

UPDATER_VERSION = (1, 3, 0)
TAG = re.compile(r"(?=.{1,64}\Z)v[0-9]+\.[0-9]+\.[0-9]+(?:-canary\.[0-9]+)?\Z")
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_FILES = 10000
# Production boards. A release must also list the model in supported_models.
SUPPORTED_MODELS = ('Raspberry Pi 4 Model B', 'Raspberry Pi 3 Model A Plus')

# These are application-retained proof files, not importable controller modules.
CONTROLLER_DESCRIPTOR = 'messagebox/controller_descriptor.py'
CONTROLLER_SIGNATURE = 'messagebox/controller_signature.py'
CONTROLLER_MODULES = ('bounded_update.py', 'health-probe.py', 'release_verify.py',
                      'trust_update.py', 'update_controller.py', 'update_discovery.py')
RECOVERY_FILES = ('messagebox/recovery_bootstrap.py', 'messagebox/recovery_helper.py',
                  'messagebox/recovery_launcher.py')
RECOVERY_COMMAND = '/usr/bin/python3 -I /usr/lib/messagebox-recovery/launcher.py'
RECOVERY_DROPINS = {
    '/etc/systemd/system/messagebox-update-' + unit + '.service.d/messagebox.conf':
    ('[Service]\nExecStartPre=' + RECOVERY_COMMAND + (' --supervisor' if unit == 'supervisor' else '') + '\n').encode()
    for unit in ('recovery', 'supervisor')
}
UPDATER_UNITS = frozenset('messagebox-update-' + name for name in (
    'recovery.service', 'controller.service', 'install.service', 'supervisor.service',
    'supervisor.timer', 'discovery.service', 'discovery.timer'))
MAX_DESCRIPTOR = 16 * 1024
MAX_CONTROLLER_SIGNATURE = 4096
MAX_CONTROLLER_MODULE = 2 * 1024 * 1024


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


def controller_file(root, relative, limit):
    """Read one bounded file through no-follow directory/file descriptors.

    The caller supplies a trusted release/staging root. Parent checks start at
    that root; its selection and the promotion trust lock belong to the helper.
    """
    pure = PurePosixPath(relative)
    if pure.is_absolute() or '..' in pure.parts or not pure.parts or str(pure) != relative:
        raise ReleaseError('unsafe_controller_path')
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptors.append(os.open(root, flags | os.O_DIRECTORY))
        for part in pure.parts[:-1]:
            descriptors.append(os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptors[-1]))
        for descriptor in descriptors:
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise ReleaseError('unsafe_controller_file')
        descriptors.append(os.open(pure.name, flags, dir_fd=descriptors[-1]))
        info = os.fstat(descriptors[-1])
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > limit):
            raise ReleaseError('unsafe_controller_file')
        with os.fdopen(os.dup(descriptors[-1]), 'rb') as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(descriptors[-1])
        if (len(raw) > limit or len(raw) != info.st_size
                or (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ReleaseError('controller_file_changed')
        return raw, stat.S_IMODE(info.st_mode)
    except OSError as exc:
        raise ReleaseError('unsafe_controller_file') from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _controller_literal(raw, limit):
    if not isinstance(raw, bytes) or not raw or len(raw) > limit:
        raise ReleaseError('invalid_controller_literal')
    try:
        tree = ast.parse(raw.decode('utf-8'), mode='eval')
        pending, count = [(tree.body, 0)], 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > 512 or depth > 8:
                raise ValueError('literal_limit')
            if isinstance(node, ast.Dict):
                keys = [key.value for key in node.keys if isinstance(key, ast.Constant) and type(key.value) is str]
                if len(keys) != len(node.keys) or len(keys) != len(set(keys)):
                    raise ValueError('duplicate_or_invalid_keys')
                pending.extend((item, depth + 1) for item in node.values)
            elif isinstance(node, ast.List):
                pending.extend((item, depth + 1) for item in node.elts)
            elif not isinstance(node, ast.Constant) or type(node.value) not in {str, int, bool}:
                raise ValueError('not_literal_data')
        return ast.literal_eval(tree)
    except (ValueError, SyntaxError, UnicodeError, RecursionError, MemoryError) as exc:
        raise ReleaseError('invalid_controller_literal') from exc


def _controller_version(value, *, application=False):
    pattern = TAG if application else re.compile(r'(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\Z')
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ReleaseError('invalid_controller_version')
    base, _, canary = value.removeprefix('v').partition('-canary.')
    return (*map(int, base.split('.')), 0 if canary else 1, int(canary or 0))


def parse_controller_descriptor(raw):
    value = _controller_literal(raw, MAX_DESCRIPTOR)
    fields = {'schema', 'product', 'controller_version', 'controller_sequence',
              'min_application_version', 'min_launcher_protocol', 'min_helper_protocol',
              'supported_os', 'supported_arch', 'supported_models', 'capabilities',
              'modules', 'unit_changes'}
    if isinstance(value, dict) and value.get('schema') == 2:
        fields.add('recovery')
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema']) is not int
            or value['schema'] not in {1, 2} or value['product'] != 'button-box-controller'
            or type(value['controller_sequence']) is not int or not 0 < value['controller_sequence'] < 2**63
            or any(type(value[key]) is not int or not 0 < value[key] < 2**16
                   for key in ('min_launcher_protocol', 'min_helper_protocol'))
            or value['supported_os'] != ['raspios-trixie'] or value['supported_arch'] != ['aarch64']
            or value['supported_models'] != sorted(SUPPORTED_MODELS)
            or not isinstance(value['capabilities'], dict)
            or set(value['capabilities']) != {'signed_descriptor', 'self_upgrade'}
            or value['capabilities']['signed_descriptor'] is not True
            or type(value['capabilities']['self_upgrade']) is not bool
            or (value['schema'] == 1 and value['unit_changes'] != [])
            or not isinstance(value['modules'], list) or len(value['modules']) != len(CONTROLLER_MODULES)):
        raise ReleaseError('invalid_controller_descriptor')
    _controller_version(value['controller_version'])
    _controller_version(value['min_application_version'], application=True)
    expected = ['updater/' + name for name in CONTROLLER_MODULES]
    for entry, path in zip(value['modules'], expected):
        if (not isinstance(entry, dict) or set(entry) != {'path', 'size', 'mode', 'sha256'}
                or entry['path'] != path or type(entry['size']) is not int
                or not 0 < entry['size'] <= MAX_CONTROLLER_MODULE
                or type(entry['mode']) is not int or entry['mode'] != 0o644
                or not isinstance(entry['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])):
            raise ReleaseError('invalid_controller_modules')
    if value['schema'] == 2:
        units = value['unit_changes']
        if not isinstance(units, list) or len(units) > 32:
            raise ReleaseError('invalid_controller_units')
        paths = []
        for entry in units:
            if (not isinstance(entry, dict) or set(entry) != {'path', 'size', 'mode', 'sha256'}
                    or not isinstance(entry['path'], str)
                    or not entry['path'].startswith('/etc/systemd/system/')
                    or entry['path'].removeprefix('/etc/systemd/system/').removesuffix('.d/messagebox.conf') not in UPDATER_UNITS
                    or type(entry['size']) is not int or not 0 < entry['size'] <= 64 * 1024
                    or type(entry['mode']) is not int or entry['mode'] != 0o644
                    or not isinstance(entry['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])):
                raise ReleaseError('invalid_controller_units')
            paths.append(entry['path'])
        if paths != sorted(set(paths)):
            raise ReleaseError('invalid_controller_units')
        recovery = value['recovery']
        if (not isinstance(recovery, dict) or set(recovery) != {'protocol', 'helper_version', 'files'}
                or type(recovery['protocol']) is not int or recovery['protocol'] != 1
                or not isinstance(recovery['files'], list) or len(recovery['files']) != len(RECOVERY_FILES)):
            raise ReleaseError('invalid_recovery_descriptor')
        _controller_version(recovery['helper_version'])
        for entry, path in zip(recovery['files'], RECOVERY_FILES):
            if (not isinstance(entry, dict) or set(entry) != {'path', 'size', 'mode', 'sha256'}
                    or entry['path'] != path or type(entry['size']) is not int
                    or not 0 < entry['size'] <= MAX_CONTROLLER_MODULE
                    or type(entry['mode']) is not int or entry['mode'] != 0o644
                    or not isinstance(entry['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])):
                raise ReleaseError('invalid_recovery_descriptor')
    return value


def controller_signature_bytes(raw):
    value = _controller_literal(raw, MAX_CONTROLLER_SIGNATURE)
    if not isinstance(value, str) or not value.isascii() or not 0 < len(value) <= 2048:
        raise ReleaseError('invalid_controller_signature')
    return value.encode('ascii')


def verify_controller_proof(raw, signature, keys, *, run=subprocess.run):
    """Authenticate captured bytes with the current release-key generation.

    Emergency pins can replace release trust; they cannot approve controller
    code. This adds no separate controller-key role to the legacy trust model.
    """
    if not raw or len(raw) > MAX_DESCRIPTOR:
        raise ReleaseError('invalid_controller_literal')
    signature = controller_signature_bytes(signature)
    keys = Path(keys)
    emergency = {public_key_id(public_key_line(key))
                 for key in (keys.parent / 'emergency-keys').glob('*.pub')}
    keys = keys.resolve()  # Select one trust generation, never mixed pins.
    with tempfile.TemporaryDirectory(prefix='messagebox-controller-proof-') as directory:
        snapshot = Path(directory)
        document, signed, pin = [snapshot / name for name in ('descriptor', 'signature', 'key.pub')]
        document.write_bytes(raw)
        signed.write_bytes(signature)
        for key in sorted(keys.glob('*.pub')):
            key_raw, _mode = controller_file(keys, key.name, 4096)
            pin.write_bytes(key_raw)
            identity = public_key_id(public_key_line(pin))
            if identity in emergency:
                continue
            try:
                result = run(['minisign', '-V', '-H', '-m', str(document), '-x', str(signed), '-p', str(pin)],
                             capture_output=True, check=False, timeout=15)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ReleaseError('verifier_unavailable') from exc
            if result.returncode == 0:
                return {'descriptor': parse_controller_descriptor(raw),
                        'descriptor_sha256': hashlib.sha256(raw).hexdigest(), 'signing_key_id': identity}
    raise ReleaseError('bad_controller_signature')


def verify_retained_controller(release, keys, *, application_version, platform, floor=0, run=subprocess.run):
    """Read-only offline verification. No promotion, trust change or Cloud call.

    Callers must recover current trust under trust_update.trust_lock before
    verification, and repeat verification under that lock before promotion.
    Equal sequence is allowed to recheck an already accepted retained proof.
    """
    raw, mode = controller_file(release, CONTROLLER_DESCRIPTOR, MAX_DESCRIPTOR)
    signature, signature_mode = controller_file(release, CONTROLLER_SIGNATURE, MAX_CONTROLLER_SIGNATURE)
    if mode != 0o644 or signature_mode != 0o644:
        raise ReleaseError('unsafe_controller_file')
    proof = verify_controller_proof(raw, signature, keys, run=run)
    value = proof['descriptor']
    check_controller_compatibility(value, application_version, platform, floor=floor)
    for entry in value['modules'] + value.get('recovery', {}).get('files', []):
        content, mode = controller_file(release, entry['path'], MAX_CONTROLLER_MODULE)
        if mode != entry['mode'] or len(content) != entry['size'] or hashlib.sha256(content).hexdigest() != entry['sha256']:
            raise ReleaseError('controller_module_mismatch')
    return proof


def check_controller_compatibility(value, application_version, platform, *, floor=0):
    if (type(floor) is not int or floor < 0 or value['controller_sequence'] < floor
            or _controller_version(application_version, application=True)
            < _controller_version(value['min_application_version'], application=True)
            or any(platform.get(field) not in value[key] for field, key in
                   (('os', 'supported_os'), ('architecture', 'supported_arch'), ('model', 'supported_models')))):
        raise ReleaseError('incompatible_controller')


def promotion_inhibited(root=Path('/'), *, journal=True, owner=None):
    """Read root-owned admission evidence; discovery needs only the public flag."""
    root = Path(root)
    if owner is None:
        owner = 0 if root == Path('/') else os.geteuid()
    paths = ['run/messagebox-update/promotion-inhibit.json']
    if journal:
        paths.append('var/lib/button-box-controller/journal.json')
    for relative in paths:
        descriptors = []
        try:
            descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            for part in (*PurePosixPath(relative).parts[:-1], None):
                info = os.fstat(descriptors[-1])
                if info.st_uid != owner or info.st_mode & 0o022:
                    return True
                if part is not None:
                    descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                               dir_fd=descriptors[-1]))
            fd = os.open(PurePosixPath(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=descriptors[-1])
            descriptors.append(fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1
                    or info.st_mode & 0o022 or info.st_size > 32768):
                return True
            raw = os.read(fd, 32769)
            if len(raw) != info.st_size:
                return True
            value = json.loads(raw)
            if set(value) == {'generation', 'sha256', 'value'}:
                body = (json.dumps({'generation': value['generation'], 'value': value['value']}, sort_keys=True) + '\n').encode()
                if hashlib.sha256(body).hexdigest() != value['sha256']:
                    return True
                value = value['value']
            if relative.endswith('promotion-inhibit.json'):
                return True
            if value.get('kind') == 'controller' and value.get('phase') not in {
                    'committed', 'restored', 'unsupported', 'aborted'}:
                return True
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, AttributeError):
            return True
        finally:
            for fd in reversed(descriptors):
                os.close(fd)
    return False
