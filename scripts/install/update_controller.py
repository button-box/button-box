#!/usr/bin/env python3
"""Fixed, local updater broker. No root network, audio, GPIO or arbitrary commands."""

import argparse
import hashlib
import json
import math
import os
import pwd
import re
import shutil
import socket
import stat
import struct
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import bounded_update as update
import trust_update as trust
from release_verify import MAX_ARCHIVE, TAG, ReleaseError, digest, extract_bundle, local_platform, verify_bundle

STATE = Path(update.UPDATE_STATE)
CACHE = Path('/var/cache/button-box-update')
KEYS = Path('/etc/button-box-update/release-keys')
RUN = Path('/run/messagebox-update')
SOCKET = RUN / 'control.sock'
INSTALLED = Path('/etc/button-box-update/installed')
TERMINAL = {'installed', 'failed', 'expired', 'rolled_back', 'rollback_failed'}
ID = re.compile(r'[A-Za-z0-9:._-]{1,240}\Z')


def auto_window(now, timezone, jitter):
    if not 1700000000 <= now < 4102444800:
        return False
    try:
        local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    except (ValueError, KeyError):
        return False
    minute = local.hour * 60 + local.minute
    return 120 + jitter <= minute < 240


def local_time_ready():
    try:
        result = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized", "--value"],
                                capture_output=True, text=True, check=False, timeout=3)
        return result.returncode == 0 and result.stdout.strip() == "yes"
    except (OSError, subprocess.SubprocessError):
        return False


def unit_pid(unit):
    result = subprocess.run(['systemctl', 'show', unit, '--property=MainPID', '--value'],
                            check=True, capture_output=True, text=True, timeout=3)
    return int(result.stdout.strip())


def read(name, default=None):
    return update.read_state(Path('/'), name, default)


def save(name, value, mode=0o600):
    update.save_state(Path('/'), name, value, mode)


def operation_name(operation_id):
    return 'operations/' + hashlib.sha256(operation_id.encode()).hexdigest() + '.json'


def policy():
    value = read('policy.json', {'mode': 'auto', 'channel': 'stable', 'timezone': 'UTC', 'jitter_minutes': 0})
    if (value.get('mode') not in {'auto', 'ask'} or value.get('channel') not in {'stable', 'canary'}
            or not isinstance(value.get('timezone'), str) or type(value.get('jitter_minutes')) is not int
            or not 0 <= value['jitter_minutes'] <= 30):
        raise ValueError('invalid_policy')
    return value


def installed_identity():
    try:
        value = json.loads(Path('/opt/messagebox/release.json').read_bytes())
        return {'software_version': value['version'], 'software_commit': value['commit']}
    except (OSError, ValueError, KeyError):
        return {}


def record(operation, state, reason='none'):
    if state in {'rolled_back', 'rollback_failed'}:
        prior = read('result.json', {})
        if prior.get('operation_id') == operation['operation_id'] and prior.get('detail'):
            operation['detail'] = prior['detail'][:update.HEALTH_CODE_LIMIT]
    operation.update(state=state, reason=reason, **installed_identity())
    save(operation_name(operation['operation_id']), operation)
    result = {key: operation.get(key) for key in
              ('operation_id', 'version', 'state', 'reason', 'software_version', 'software_commit', 'detail')
              if key != 'detail' or operation.get(key)}
    save('result.json', result, 0o644)
    return result


def installed():
    try:
        proof = json.loads((RUN / 'confirmation-path.json').read_bytes())
        pid = unit_pid('messagebox-button.service')
        recovery = subprocess.run(['systemctl', 'is-active', 'messagebox-update-recovery.service'],
                                  capture_output=True, text=True, check=False, timeout=3)
        return (INSTALLED.is_file()
                and Path('/usr/lib/messagebox-update/bounded_update.py').is_file()
                and Path('/etc/systemd/system/messagebox-update-recovery.service').is_file()
                and recovery.returncode == 0 and pid > 0 and proof.get('pid') == pid
                and proof.get('boot') == update._boot()
                and proof.get('process_token') is not None and proof.get('process_token') == update._process_token(pid)
                and proof.get('software_version') == installed_identity().get('software_version'))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def public_status():
    configuration = policy()
    available = read('available.json', {})
    result = read('result.json', {})
    value = {'installed': installed(), 'effective_mode': configuration['mode'],
             'channel': configuration['channel'], 'available_version': available.get('version'),
             'last_result': result, 'sequence_floor': read('sequence.json', 0),
             'trust_sequence': trust.trust_state(STATE)['trust_sequence'],
             'trust_updates_enabled': any((KEYS.parent / 'emergency-keys').glob('*.pub')),
             **installed_identity()}
    save('status.json', value, 0o644)
    return value


def remaining_valid(operation):
    if operation.get('action') == 'automatic' and operation.get('state') == 'waiting_for_window':
        return True
    return (operation.get('boot') == update._boot()
            and time.monotonic() < operation['expires_mono'])


def active_operation():
    operation_id = read('active.json')
    if not operation_id:
        return None
    operation = read(operation_name(operation_id))
    transaction_result = read('result.json', {})
    if (operation and operation['state'] not in TERMINAL and transaction_result.get('operation_id') == operation_id
            and transaction_result.get('state') in TERMINAL):
        record(operation, transaction_result['state'], transaction_result['reason'])
    if operation and operation['state'] not in TERMINAL and not remaining_valid(operation):
        journal = read('journal.json')
        if not journal or journal.get('operation_id') != operation_id or journal['phase'] in {'committed', 'rolled_back'}:
            record(operation, 'expired', 'request_expired')
    return operation


def bind_approval(operation, available=None):
    manifest_digest = available.get('manifest_sha256') if available else 'local-policy'
    operation.update(binding=hashlib.sha256(json.dumps([
        operation['operation_id'], manifest_digest, operation['version'], operation['boot'],
    ]).encode()).hexdigest(), manifest_sha256=manifest_digest, cue_pending=True)
    record(operation, 'waiting_for_button')


def receive_bundle(version, *, trust_only=False):
    """Copy untrusted downloads with no-follow FDs before root verification."""
    pattern = trust.TRUST_TAG if trust_only else TAG
    if not isinstance(version, str) or not pattern.fullmatch(version):
        raise ReleaseError('invalid_version')
    source = CACHE / version
    if source.is_symlink() or not source.is_dir():
        raise ReleaseError('incomplete_download')
    destination = STATE / 'downloads' / uuid.uuid4().hex
    destination.mkdir(parents=True, mode=0o700)
    names = (('trust-update.json', 'trust-update.json.minisig') if trust_only else
             ('SHA256SUMS', 'SHA256SUMS.minisig', 'release-manifest.json', f'button-box-{version}.tar.gz'))
    try:
        parent = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in names:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(descriptor, 'rb') as incoming:
                    info = os.fstat(incoming.fileno())
                    limit = (trust.MAX_TRUST if name == 'trust-update.json' else 4096) if trust_only else (
                        MAX_ARCHIVE if name.endswith('.tar.gz') else 2 * 1024 * 1024)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                        raise ReleaseError('unsafe_download')
                    with (destination / name).open('xb') as output:
                        count = 0
                        while block := incoming.read(1024 * 1024):
                            count += len(block)
                            if count > limit:
                                raise ReleaseError('archive_limit')
                            output.write(block)
                        output.flush()
                        os.fsync(output.fileno())
        finally:
            os.close(parent)
        if trust_only:
            try:
                return trust.apply_trust(destination, KEYS.parent, STATE)
            finally:
                shutil.rmtree(destination)
        manifest = verify_bundle(destination, KEYS, version=version, channel=policy()['channel'],
                                 floor=read('sequence.json', 0), platform=local_platform())
        pointer = update.RELEASES + '/' + version + '-' + manifest['commit']
        if pointer in read('quarantine.json', []):
            raise ReleaseError('quarantined')
        available = {'version': version, 'commit': manifest['commit'], 'sequence': manifest['release_sequence'],
                     'manifest_sha256': digest(destination / 'release-manifest.json'), 'bundle': str(destination)}
        return available
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def wake_discovery():
    subprocess.run(["systemctl", "start", "--no-block", "messagebox-update-discovery.service"],
                   check=True, timeout=5)


def start_install(operation, available):
    # This root-owned ticket is the only input the fixed worker consumes.
    ticket = {'operation_id': operation['operation_id'], 'version': available['version'],
              'manifest_sha256': available['manifest_sha256'], 'bundle': available['bundle'],
              'queued_mono': time.monotonic(), 'boot': update._boot()}
    save('ticket.json', ticket)
    record(operation, 'installing')
    subprocess.run(['systemctl', 'start', '--no-block', 'messagebox-update-install.service'],
                   check=True, timeout=5)


class Controller:
    def __init__(self):
        self.lock = threading.RLock()
        self.pressed_at = None
        self.pressed_binding = None
        self.time_checked = None
        self.time_ready = False

    def dispatch(self, data, peer):
        pid, uid, _gid = peer
        runtime_uid = pwd.getpwnam('messagebox').pw_uid
        discovery_uid = pwd.getpwnam('messagebox-update').pw_uid
        command = data.get('command')
        if command in {'status', 'result', 'install', 'mode', 'hint'} and uid != runtime_uid:
            if not (command == 'status' and uid == discovery_uid):
                raise ValueError('unauthorized_peer')
        if command in {'button', 'arm', 'confirm'} and (uid != runtime_uid or pid != unit_pid('messagebox-button.service')):
            raise ValueError('not_button_owner')
        if command == 'heartbeat' and (uid != runtime_uid or pid != unit_pid('messagebox-poller.service')):
            raise ValueError('not_heartbeat_owner')
        if command in {'downloaded', 'download_failed', 'trust_downloaded'} and uid != discovery_uid:
            raise ValueError('not_downloader')
        with self.lock:
            if command == 'status':
                value = public_status()
                operation = active_operation()
                value['requested_version'] = operation.get('version') if operation and operation['state'] == 'downloading' else None
                hint = read('hint.json', {})
                value['discovery_hint'] = hint.get('at', 0) > time.time() - 600
                return value
            if command == 'result':
                operation_id = data.get('operation_id')
                if not isinstance(operation_id, str) or not ID.fullmatch(operation_id):
                    raise ValueError('invalid_operation')
                active_operation()
                operation = read(operation_name(operation_id), {})
                transaction_result = read('result.json', {})
                if transaction_result.get('operation_id') == operation_id and transaction_result.get('state') in TERMINAL:
                    if operation and operation['state'] not in TERMINAL:
                        record(operation, transaction_result['state'], transaction_result['reason'])
                        operation = read(operation_name(operation_id))
                return {key: operation.get(key) for key in
                        ('operation_id', 'version', 'state', 'reason', 'software_version', 'software_commit', 'detail')
                        if key != 'detail' or operation.get(key)}
            if command in {'install', 'mode'}:
                return self.new_operation(data)
            if command == 'hint':
                # Timer coalesces hints under persisted API cooldown; no root network.
                save('hint.json', {'at': time.time()})
                wake_discovery()
                return {'accepted': True}
            if command == 'trust_downloaded':
                if set(data) != {'command', 'version'}:
                    raise ValueError('invalid_trust_request')
                sequence = receive_bundle(data.get('version'), trust_only=True)
                public_status()
                return {'accepted': True, 'trust_sequence': sequence}
            if command == 'download_failed':
                operation = active_operation()
                reason = data.get('reason')
                if reason not in {'download_failed', 'bad_signature', 'hash_mismatch', 'wrong_channel', 'incompatible',
                                  'invalid_identity_or_downgrade', 'invalid_hashes', 'incomplete_release',
                                  'invalid_release', 'download_limit', 'verifier_unavailable', 'incomplete_download'}:
                    reason = 'verification_failed'
                if operation and operation['state'] == 'downloading' and operation['version'] == data.get('version'):
                    record(operation, 'failed', reason)
                return {'accepted': True}
            if command == 'downloaded':
                operation = active_operation()
                if operation and operation['state'] == 'installing':
                    raise ValueError('update_busy')
                if (operation and operation['action'] == 'install' and operation['state'] == 'waiting_for_button'
                        and operation['version'] != data.get('version')):
                    raise ValueError('approval_candidate_reserved')
                try:
                    available = receive_bundle(data.get('version'))
                except ReleaseError as exc:
                    operation = active_operation()
                    if operation and operation['state'] == 'downloading' and operation['version'] == data.get('version'):
                        record(operation, 'failed', str(exc))
                    raise
                previous = read('available.json', {})
                requested = (operation and operation['action'] == 'install'
                             and operation['state'] == 'downloading' and operation['version'] == available['version'])
                if (operation and operation['state'] == 'waiting_for_button'
                        and operation.get('manifest_sha256') != available['manifest_sha256']):
                    shutil.rmtree(available['bundle'])
                    raise ValueError('candidate_changed')
                if not requested and available['sequence'] < previous.get('sequence', 0):
                    shutil.rmtree(available['bundle'])
                    return {'accepted': False}
                save('available.json', available)
                for abandoned in (STATE / 'downloads').iterdir():
                    if abandoned.name != Path(available['bundle']).name and abandoned.is_dir() and not abandoned.is_symlink():
                        shutil.rmtree(abandoned)
                if previous.get('bundle') and previous['bundle'] != available['bundle']:
                    shutil.rmtree(previous['bundle'], ignore_errors=True)
                operation = active_operation()
                if operation and operation['state'] == 'downloading' and operation['version'] == available['version']:
                    bind_approval(operation, available)
                return {'accepted': True}
            if command in {'button', 'heartbeat'}:
                self.evidence(command, data, pid)
                operation = active_operation()
                if command == 'button':
                    released = data.get('released') is True
                    if released:
                        # A fresh edge is mandatory after arming; already-held
                        # presses never qualify. Short presses reset this clock.
                        self.pressed_at = None
                        self.pressed_binding = None
                        self.automatic_if_ready(operation)
                    elif operation and operation['state'] == 'waiting_for_button' and not operation.get('cue_pending'):
                        self.pressed_at = self.pressed_at or time.monotonic()
                        self.pressed_binding = operation['binding']
                    return {'approval': self.approval(operation)}
                return {'accepted': True}
            if command in {'arm', 'confirm'}:
                operation = active_operation()
                if (not operation or operation['state'] != 'waiting_for_button'
                        or data.get('binding') != operation.get('binding') or not remaining_valid(operation)
                        or data.get('released') is not True):
                    raise ValueError('approval_expired')
                if command == 'arm':
                    if operation.get('cue_pending') is not True:
                        return {'approval': self.approval(operation)}
                    operation.update(cue_pending=False, deadline_mono=min(time.monotonic() + 120, operation['expires_mono']))
                    save(operation_name(operation['operation_id']), operation)
                    self.pressed_at = None
                    self.pressed_binding = None
                    return {'approval': self.approval(operation)}
                if (self.pressed_binding != operation['binding'] or self.pressed_at is None
                        or time.monotonic() - self.pressed_at < 3 or time.monotonic() >= operation['deadline_mono']):
                    raise ValueError('short_or_expired_hold')
                if operation['action'] == 'enable_auto':
                    configuration = policy()
                    configuration['mode'] = 'auto'
                    save('policy.json', configuration)
                    record(operation, 'installed', 'mode_changed')
                else:
                    available = read('available.json', {})
                    if available.get('manifest_sha256') != operation['manifest_sha256'] or available.get('version') != operation['version']:
                        raise ValueError('candidate_changed')
                    start_install(operation, available)
                self.pressed_at = self.pressed_binding = None
                return {'accepted': True}
            raise ValueError('unsupported_command')

    def new_operation(self, data):
        operation_id = data.get('operation_id')
        if not isinstance(operation_id, str) or not ID.fullmatch(operation_id):
            raise ValueError('invalid_operation')
        command = data['command']
        version = data.get('version') if command == 'install' else None
        if command == 'install' and (set(data) != {'command', 'operation_id', 'version', 'remaining'}
                or not isinstance(version, str) or not TAG.fullmatch(version)):
            raise ValueError('invalid_version')
        if command == 'mode' and (set(data) != {'command', 'operation_id', 'mode', 'remaining'} or data.get('mode') not in {'ask', 'auto'}):
            raise ValueError('invalid_mode')
        prior = read(operation_name(operation_id))
        action = 'install' if command == 'install' else ('enable_auto' if data['mode'] == 'auto' else 'enable_ask')
        if prior:
            if prior['action'] != action or prior['version'] != version:
                raise ValueError('operation_conflict')
            active_operation()
            return read(operation_name(operation_id))
        remaining = data.get('remaining')
        if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining <= 0:
            raise ValueError('request_expired')
        current = active_operation()
        if current and current['state'] not in TERMINAL:
            raise ValueError('update_busy')
        operation = {'operation_id': operation_id, 'action': action, 'version': version,
                     'boot': update._boot(), 'expires_mono': time.monotonic() + min(remaining, 86400)}
        save('active.json', operation_id)
        if action == 'enable_ask':
            configuration = policy()
            configuration['mode'] = 'ask'
            save('policy.json', configuration)
            return record(operation, 'installed', 'mode_changed')
        if action == 'enable_auto':
            if policy()['mode'] == 'auto':
                return record(operation, 'installed', 'already_effective')
            bind_approval(operation)
        elif installed_identity().get('software_version') == version:
            return record(operation, 'installed', 'already_installed')
        else:
            available = read('available.json', {})
            if available.get('version') == version:
                bind_approval(operation, available)
            else:
                record(operation, 'downloading')
                save('hint.json', {'at': time.time()})
                wake_discovery()
        return read(operation_name(operation_id))

    def approval(self, operation):
        if not operation or operation['state'] != 'waiting_for_button':
            return None
        if not operation.get('cue_pending') and time.monotonic() >= operation['deadline_mono']:
            record(operation, 'expired', 'button_window_expired')
            return None
        return {key: operation.get(key) for key in ('action', 'binding', 'cue_pending', 'deadline_mono')}

    def automatic_if_ready(self, operation):
        available = read('available.json', {})
        configuration = policy()
        if configuration['mode'] != 'auto' or not available or not installed() or Path(update.MODE_MARKER).exists():
            return
        if operation and operation['state'] not in TERMINAL and operation.get('action') != 'automatic':
            return
        if installed_identity().get('software_version') == available['version']:
            return
        if not operation or operation['state'] in TERMINAL:
            operation = {'operation_id': 'auto:' + available['manifest_sha256'], 'action': 'automatic',
                         'version': available['version'], 'boot': update._boot(), 'expires_mono': time.monotonic() + 86400}
            # Persisted terminal receipts prevent automatic retries after rollback.
            if read(operation_name(operation['operation_id'])):
                return
            save('active.json', operation['operation_id'])
            record(operation, 'waiting_for_window')
        if (time.monotonic() >= operation.get('retry_after_mono', 0)
                and auto_window(time.time(), configuration['timezone'], configuration['jitter_minutes'])):
            now = time.monotonic()
            if self.time_checked is None or now - self.time_checked >= 60:
                self.time_ready = local_time_ready()
                self.time_checked = now
            if not self.time_ready:
                return
            # The worker atomically acquires the exclusive reservation and checks
            # onboarding again. Never infer idle from an old heartbeat file.
            start_install(operation, available)

    def evidence(self, command, data, pid):
        if command == 'button' and all(data.get(key) == value for key, value in installed_identity().items()):
            update.durable_json(RUN / 'confirmation-path.json', {
                **installed_identity(), 'pid': pid, 'boot': update._boot(),
                'process_token': update._process_token(pid),
            }, 0o644)
        try:
            transaction = json.loads((RUN / 'transaction.json').read_bytes())
        except (OSError, ValueError):
            return
        if (data.get('software_version') != transaction.get('software_version')
                or data.get('software_commit') != transaction.get('software_commit')):
            return
        name = 'button' if command == 'button' else 'heartbeat'
        update.durable_json(RUN / (name + '.json'), {
            **transaction, 'pid': pid, 'monotonic': time.monotonic(), 'ready': True,
        }, 0o644)


def worker():
    # Serialize the signature check through transaction completion with trust
    # replacement. A queued candidate is always checked against current pins.
    with trust.trust_lock(STATE, blocking=True):
        trust.recover_trust(KEYS.parent, STATE)
        _worker()


def _worker():
    ticket = read('ticket.json')
    operation = read(operation_name(ticket['operation_id']))
    bundle = Path(ticket['bundle'])
    # Root tickets only reference one immediate root-owned downloads child.
    if bundle.parent != STATE / 'downloads' or not re.fullmatch(r'[0-9a-f]{32}', bundle.name):
        raise ValueError('unsafe_ticket')
    source = STATE / 'staging' / uuid.uuid4().hex
    keep_available = False
    try:
        if digest(bundle / 'release-manifest.json') != ticket['manifest_sha256']:
            raise ValueError('candidate_changed')
        manifest = verify_bundle(bundle, KEYS, version=ticket['version'], channel=policy()['channel'],
                                 floor=read('sequence.json', 0), platform=local_platform())
        extract_bundle(bundle, source, ticket['version'])
        if digest(source / 'release-manifest.json') != ticket['manifest_sha256']:
            raise ReleaseError('archive_manifest_mismatch')
        update.install_transaction(source, source / 'release-manifest.json', operation_id=ticket['operation_id'],
                                   sequence=manifest['release_sequence'])
    except update.UpdateError as exc:
        result = read('result.json', {})
        if result.get('operation_id') == ticket['operation_id'] and result.get('state') in {'rolled_back', 'rollback_failed'}:
            record(operation, result['state'], result['reason'])
        elif str(exc) in {'not_idle', 'onboarding_active', 'low_disk'} and operation['action'] == 'automatic':
            operation['retry_after_mono'] = time.monotonic() + 60
            record(operation, 'waiting_for_window', 'not_idle' if str(exc) != 'low_disk' else 'low_disk')
            keep_available = True
            return
        elif str(exc) in {'not_idle', 'onboarding_active'}:
            # Explicit approval is one use: never save it for a later interaction.
            record(operation, 'failed', str(exc))
        else:
            record(operation, 'failed', 'install_failed')
        raise
    except (OSError, ValueError):
        record(operation, 'failed', 'verification_failed')
        raise
    finally:
        shutil.rmtree(source, ignore_errors=True)
        if not keep_available:
            shutil.rmtree(bundle, ignore_errors=True)
            save('available.json', {})
    record(operation, 'installed', 'healthy')


def attended_install(directory, version):
    """One-time SSH/bootstrap path; root still needs an eligible signed bundle.

    The old B71 application has no confirmation path. Install fixed recovery
    and convert its tree first, then let this controller own the first B72
    application transaction. Never fall back to legacy in-place apply.
    """
    if os.geteuid() != 0 or not INSTALLED.is_file():
        raise ValueError('attended_bootstrap_required')
    directory = Path(directory).absolute()
    if not isinstance(version, str) or not TAG.fullmatch(version):
        raise ValueError('invalid_version')
    names = ('SHA256SUMS', 'SHA256SUMS.minisig', 'release-manifest.json', f'button-box-{version}.tar.gz')
    for name in names:
        update._validate_staged_file(directory / name, directory, 0)
    manifest = verify_bundle(directory, KEYS, version=version, channel=policy()['channel'],
                             floor=read('sequence.json', 0), platform=local_platform())
    destination = STATE / 'downloads' / uuid.uuid4().hex
    destination.mkdir(mode=0o700, parents=True)
    for name in names:
        update._atomic_install(directory / name, destination / name, 0o600)
    operation_id = 'bootstrap:' + digest(destination / 'release-manifest.json')
    available = {'version': version, 'commit': manifest['commit'], 'sequence': manifest['release_sequence'],
                 'manifest_sha256': digest(destination / 'release-manifest.json'), 'bundle': str(destination)}
    operation = {'operation_id': operation_id, 'action': 'attended', 'version': version,
                 'boot': update._boot(), 'expires_mono': time.monotonic() + 86400}
    prior = read(operation_name(operation_id))
    if prior:
        shutil.rmtree(destination)
        raise ValueError('bootstrap_operation_already_attempted')
    save('available.json', available)
    save('active.json', operation_id)
    start_install(operation, available)


def serve():
    if (STATE / 'trust-pending.json').exists():
        with trust.trust_lock(STATE):
            trust.recover_trust(KEYS.parent, STATE)
    RUN.mkdir(mode=0o755, parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    controller = Controller()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(SOCKET))
        os.chmod(SOCKET, 0o660)
        import grp
        os.chown(SOCKET, 0, grp.getgrnam('messagebox-settings').gr_gid)
        server.listen(8)
        while True:
            connection, _address = server.accept()
            with connection:
                connection.settimeout(3)
                peer = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                try:
                    data = bytearray()
                    while b'\n' not in data and len(data) <= 16384:
                        block = connection.recv(4096)
                        if not block:
                            break
                        data.extend(block)
                    payload = json.loads(data)
                    if not isinstance(payload, dict):
                        raise ValueError('invalid_request')
                    result = controller.dispatch(payload, peer)
                except Exception:
                    # Never return network strings, paths or subprocess output.
                    result = {'error': 'request_rejected'}
                try:
                    connection.sendall(json.dumps(result).encode() + b'\n')
                except OSError:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('serve', 'install', 'attended-install'))
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--version')
    args = parser.parse_args()
    try:
        if args.command == 'attended-install':
            if not args.bundle or not args.version:
                raise ValueError('bootstrap_bundle_required')
            attended_install(args.bundle, args.version)
        elif args.command == 'install':
            worker()
        else:
            serve()
    except Exception:
        parser.exit(1, 'software update: operation failed; inspect the bounded result code.\n')


if __name__ == '__main__':
    main()
