"""Permanent, controller-independent recovery entry point. Protocol 1.

This file and its verifier anchors are installed once. A later application must
not replace them. Signed helpers own transaction recovery. The permanent
launcher only dispatches verified bytes through the stable protocol 1 envelope.
"""

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import stat
import sys
import time
import types
import uuid
from pathlib import Path

SUPPORT = 'usr/lib/messagebox-recovery'
STATE = 'var/lib/button-box-controller'
ACTIVE = 'usr/lib/messagebox-update'
TRUST = 'etc/button-box-update'
MODULES = ('bounded_update.py', 'health-probe.py', 'release_verify.py', 'trust_update.py',
           'update_controller.py', 'update_discovery.py')
LIMIT = 2 * 1024 * 1024
RECORD_SIZE = 32768
HELPER_TIMEOUT = 45
JOURNAL = STATE + '/journal.json'
MONITOR = 'messagebox-update-monitor.service'
MONITOR_UNIT = b'''[Unit]
Description=Permanent controller-independent recovery monitor
After=local-fs.target messagebox-update-recovery.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I /usr/lib/messagebox-recovery/launcher.py --monitor
Restart=always
RestartSec=1
NoNewPrivileges=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX

[Install]
WantedBy=multi-user.target
'''


class RecoveryError(ValueError):
    pass


def checkpoint(event, path):
    """Crash-test boundary, after the named system call. Never supplied by CLI."""


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def parts(relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts or str(path) != relative:
        raise RecoveryError('unsafe_path')
    return path.parts


def check_dir(fd, *, lock_directory=False):
    info = os.fstat(fd)
    writable = info.st_mode & 0o022
    sticky_lock = lock_directory and info.st_mode & stat.S_ISVTX
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or (writable and not sticky_lock):
        raise RecoveryError('unsafe_directory')


@contextlib.contextmanager
def directory(root, relative='', *, create=False, step=checkpoint):
    """Hold no-follow directory FDs from the caller's trusted root downward."""
    fds = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fds.append(os.open(root, flags))
        check_dir(fds[-1])
        walked = []
        for name in parts(relative) if relative else ():
            walked.append(name)
            current = '/'.join(walked)
            if create:
                try:
                    os.mkdir(name, 0o755, dir_fd=fds[-1])
                except FileExistsError:
                    pass
                else:
                    step('mkdir', current)
                    os.fsync(fds[-1])
                    step('directory_fsync', '/'.join(walked[:-1]))
            fds.append(os.open(name, flags, dir_fd=fds[-1]))
            check_dir(fds[-1], lock_directory=current == 'run/lock')
            if create:
                os.fsync(fds[-1])
                step('directory_fsync', current)
        yield fds[-1]
    finally:
        for fd in reversed(fds):
            os.close(fd)


def read(root, relative, limit=LIMIT, *, mode=None):
    names = parts(relative)
    with directory(root, '/'.join(names[:-1])) as parent:
        fd = os.open(names[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or info.st_mode & 0o022 or info.st_size > limit
                    or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
                raise RecoveryError('unsafe_file')
            raw = stream.read(limit + 1)
            after = os.fstat(fd)
            if (len(raw) != info.st_size or len(raw) > limit
                    or (info.st_mtime_ns, info.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)):
                raise RecoveryError('file_changed')
            return raw


def write(root, relative, raw, *, mode=0o600, step=checkpoint):
    names = parts(relative)
    with directory(root, '/'.join(names[:-1]), create=True, step=step) as parent:
        try:
            info = os.stat(names[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or info.st_mode & 0o022):
                raise RecoveryError('unsafe_write_target')
        temporary = '.pending-' + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fchmod(stream.fileno(), mode)
                step('file_write', '/'.join((*names[:-1], temporary)))
                os.fsync(stream.fileno())
                step('file_fsync', '/'.join((*names[:-1], temporary)))
            os.replace(temporary, names[-1], src_dir_fd=parent, dst_dir_fd=parent)
            step('replace', relative)
            os.fsync(parent)
            step('directory_fsync', '/'.join(names[:-1]))
        finally:
            # A real power cut can leave the private temporary file. It is never
            # a selectable slot or executable entry point.
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def json_document(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RecoveryError('duplicate_state_key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def records(root):
    valid = []
    for path in (JOURNAL, JOURNAL + '.copy'):
        try:
            item = json_document(read(root, path, RECORD_SIZE, mode=0o600))
            if (set(item) != {'generation', 'sha256', 'value'} or type(item['generation']) is not int
                    or item['generation'] < 1 or item['sha256'] != sha(canonical(
                        {'generation': item['generation'], 'value': item['value']}))):
                continue
            valid.append((item['generation'], path, item['value']))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return sorted(valid, reverse=True)


def canonical(value):
    return (json.dumps(value, sort_keys=True) + '\n').encode()


def document(root, relative):
    if relative == JOURNAL:
        found = records(root)
        if not found:
            if not any((Path(root) / path).exists() for path in (JOURNAL, JOURNAL + '.copy')):
                raise FileNotFoundError(relative)
            raise RecoveryError('no_valid_recovery_record')
        return found[0][2]
    return json_document(read(root, relative, 16384, mode=0o600))


def save(root, relative, value, *, step=checkpoint):
    if relative != JOURNAL:
        write(root, relative, canonical(value), step=step)
        return
    found = records(root)
    generation = found[0][0] + 1 if found else 1
    body = {'generation': generation, 'value': value}
    raw = canonical({**body, 'sha256': sha(canonical(body))})
    if len(raw) > RECORD_SIZE:
        raise RecoveryError('recovery_record_limit')
    raw = raw.ljust(RECORD_SIZE, b' ')
    # Never overwrite the only latest valid record first. Existing slots have
    # allocated blocks and inodes; restore requires neither a temp file nor rename.
    paths = [JOURNAL, JOURNAL + '.copy']
    if found and found[0][1] == paths[0]:
        paths.reverse()
    for path in paths:
        names = parts(path)
        with directory(root, '/'.join(names[:-1])) as parent:
            try:
                fd = os.open(names[-1], os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                write(root, path, raw, step=step)
                continue
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise RecoveryError('unsafe_recovery_record')
                if info.st_size != RECORD_SIZE:
                    os.ftruncate(fd, RECORD_SIZE)
                    step('truncate', path)
                if os.pwrite(fd, raw, 0) != RECORD_SIZE:
                    raise OSError('short_recovery_record_write')
                step('file_write', path)
                os.fsync(fd)
                step('file_fsync', path)
                os.fsync(parent)
                step('directory_fsync', '/'.join(names[:-1]))
            finally:
                os.close(fd)


@contextlib.contextmanager
def lock(root, relative):
    names = parts(relative)
    with directory(root, '/'.join(names[:-1])) as parent:
        fd = os.open(names[-1], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise RecoveryError('unsafe_lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)


@contextlib.contextmanager
def locks(root):
    # Existing trust -> application update order is unchanged. The new guard
    # follows update; future promotion must take mode/interaction after this.
    with lock(root, 'var/lib/button-box-update/trust.lock'), \
            lock(root, 'run/lock/messagebox-bounded-update.lock'), \
            lock(root, STATE + '/promotion.lock'):
        yield


def anchors(root):
    record = document(root, SUPPORT + '/anchors.json')
    if set(record) != {'release_verify.py', 'trust_update.py'}:
        raise RecoveryError('invalid_anchors')
    loaded = {}
    previous = sys.modules.get('release_verify')
    try:
        for name in ('release_verify.py', 'trust_update.py'):
            raw = read(root, SUPPORT + '/' + name, mode=0o644)
            if sha(raw) != record[name]:
                raise RecoveryError('anchor_mismatch')
            module = types.ModuleType(name[:-3])
            module.__file__ = str(Path(root) / SUPPORT / name)
            exec(compile(raw, module.__file__, 'exec'), module.__dict__)
            loaded[name] = module
            if name == 'release_verify.py':
                sys.modules['release_verify'] = module
    finally:
        if previous is None:
            sys.modules.pop('release_verify', None)
        else:
            sys.modules['release_verify'] = previous
    return loaded['release_verify.py'], loaded['trust_update.py']


def validate_state(value):
    if (not isinstance(value, dict) or set(value) != {'schema', 'accepted', 'helpers', 'legacy', 'application'}
            or value['schema'] != 1 or type(value['schema']) is not int
            or set(value['accepted']) != {'sequence', 'digest', 'controller_version', 'helper_version'}
            or type(value['accepted']['sequence']) is not int or value['accepted']['sequence'] < 1
            or not re.fullmatch('[0-9a-f]{64}', value['accepted']['digest'])
            or any(not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value['accepted'][key])
                   for key in ('controller_version', 'helper_version'))
            or set(value['helpers']) != {'current', 'fallback'}):
        raise RecoveryError('invalid_recovery_state')
    for slot in value['helpers'].values():
        if (set(slot) != {'directory', 'digest', 'signature_sha256'}
                or not re.fullmatch('[0-9a-f]{64}', slot['signature_sha256'])
                or not re.fullmatch('[0-9a-f]{64}', slot['digest'])
                or slot['directory'] not in {STATE + '/helpers/' + slot['digest'] + '/current',
                                             STATE + '/helpers/' + slot['digest'] + '/fallback'}):
            raise RecoveryError('invalid_helper_slot')
    if value['helpers']['current']['digest'] != value['accepted']['digest']:
        raise RecoveryError('invalid_current_identity')
    legacy = value['legacy']
    if (set(legacy) != {'directory', 'files'} or legacy['directory'] != STATE + '/controllers/legacy'
            or [entry['path'] for entry in legacy['files']] != list(MODULES)):
        raise RecoveryError('invalid_legacy_identity')
    for entry in legacy['files']:
        if (set(entry) != {'path', 'size', 'sha256'} or type(entry['size']) is not int
                or not 0 < entry['size'] <= LIMIT or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
            raise RecoveryError('invalid_legacy_identity')
    return value


def legacy_valid(root, state):
    # PR 3 cannot mutate the active controller. A safe no-op requires BOTH the
    # captured known-good set and the still-active initial set to match.
    for entry in state['legacy']['files']:
        for base in (ACTIVE, state['legacy']['directory']):
            raw = read(root, base + '/' + entry['path'], mode=0o644)
            if len(raw) != entry['size'] or sha(raw) != entry['sha256']:
                raise RecoveryError('legacy_controller_changed')
    return True


def verified_controller(root, state, verify, base=ACTIVE):
    """Independent identities survive loss of either (or both) journal copies."""
    candidates = [(state['legacy']['files'], None)]
    for slot in state['helpers'].values():
        try:
            raw = read(root, slot['directory'] + '/' + verify.CONTROLLER_DESCRIPTOR,
                       verify.MAX_DESCRIPTOR, mode=0o644)
            if sha(raw) != slot['digest']:
                continue
            value = verify.parse_controller_descriptor(raw)
            metadata = canonical({'schema': 1, 'version': value['controller_version'],
                                  'descriptor_sha256': slot['digest']})
            candidates.append(([{**entry, 'path': Path(entry['path']).name}
                                for entry in value['modules']], metadata))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    for files, metadata in candidates:
        try:
            for entry in files:
                raw = read(root, base + '/' + entry['path'], mode=0o644)
                if len(raw) != entry['size'] or sha(raw) != entry['sha256']:
                    break
            else:
                if metadata is not None and read(root, base + '/controller.json', 16384, mode=0o644) != metadata:
                    continue
                return True
        except (OSError, ValueError):
            continue
    return False


def select_helper(root, state, verify):
    for role in ('current', 'fallback'):
        slot = state['helpers'][role]
        try:
            path = Path(root) / slot['directory']
            raw = read(root, slot['directory'] + '/' + verify.CONTROLLER_DESCRIPTOR, verify.MAX_DESCRIPTOR, mode=0o644)
            sig = read(root, slot['directory'] + '/' + verify.CONTROLLER_SIGNATURE, verify.MAX_CONTROLLER_SIGNATURE, mode=0o644)
            # Recovery authorization is separate from release admission. Only
            # state-pinned, previously accepted descriptor digests can use these
            # retained public pins. Revocation still applies to every new admission.
            # Admission verifies the signature with current release trust before
            # publishing these root-owned digest pins. Recovery checks exactly
            # those accepted bytes without temporary files, new inodes, or a
            # dependence on later release-key generations.
            value = verify.parse_controller_descriptor(raw)
            if (sha(raw) != slot['digest'] or sha(sig) != slot['signature_sha256'] or value['schema'] != 2
                    or value['min_launcher_protocol'] != 1 or value['min_helper_protocol'] != 1
                    or value['recovery']['protocol'] != 1):
                raise RecoveryError('unapproved_helper')
            # Pinning the fallback digest authorizes only that prior accepted
            # helper for recovery. It is not an exception for new admission.
            helper = None
            for entry in value['recovery']['files']:
                content = read(path, entry['path'], mode=entry['mode'])
                if len(content) != entry['size'] or sha(content) != entry['sha256']:
                    raise RecoveryError('helper_mismatch')
                if entry['path'] == 'messagebox/recovery_helper.py':
                    helper = content
            return role, helper
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return None, None


def retain_recovery_keys(root, verify, *, step=checkpoint):
    for key in sorted((Path(root) / TRUST / 'release-keys').glob('*.pub')):
        line = verify.public_key_line(key)
        name = verify.public_key_id(line) + '.pub'
        write(root, SUPPORT + '/recovery-keys/' + name,
              ('untrusted comment: retained recovery authorization\n' + line + '\n').encode(),
              mode=0o644, step=step)


def process_token(pid):
    try:
        return Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return None


def expire_owner(root, *, monotonic=time.monotonic, kill=os.kill):
    try:
        with lock(root, STATE + '/owner.lock'):
            return
    except BlockingIOError:
        pass
    try:
        lease = document(root, STATE + '/owner.json')
        if (type(lease['pid']) is int and lease['pid'] > 1 and lease['token'] is not None
                and lease.get('boot') is not None and lease['boot'] == boot_id()
                and lease['token'] == process_token(lease['pid'])
                and monotonic() >= lease['deadline_mono']):
            kill(lease['pid'], signal.SIGKILL)
    except (OSError, ValueError, KeyError, TypeError):
        pass


# A fresh interpreter avoids inherited Python locks from threaded callers. The
# launcher API and helper bytes are captured before dispatch; no temporary file
# or candidate import path is needed. This code has no product test hooks.
CHILD_CODE = r"""
import base64, contextlib, ctypes, json, os, signal, sys
from pathlib import Path
payload = json.loads(sys.stdin.buffer.read(8 * 1024 * 1024 + 1))
if sys.platform == 'linux':
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0):
        os._exit(1)
    if os.getppid() != payload['parent']:
        os._exit(1)
api = {'__name__': 'recovery_child_api'}
exec(compile(base64.b64decode(payload['launcher']), '<permanent-launcher>', 'exec'), api)
namespace = {'__name__': 'verified_recovery_helper'}
try:
    with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        exec(compile(base64.b64decode(payload['helper']), '<verified-recovery-helper>', 'exec'), namespace)
        context = {'root': Path(payload['root']), 'journal': payload['journal'], 'state': payload['state'],
                   'launcher_pid': payload['parent'],
                   'check_legacy': lambda: api['legacy_valid'](Path(payload['root']), payload['state'])}
        result = namespace['recover'](context)
        if result not in {'legacy', 'recovered', 'committed', 'deferred', 'degraded'}:
            raise ValueError('invalid_helper_result')
    answer = {'result': result}
except BaseException as exc:
    answer = {'error': str(exc)[:512]}
print(json.dumps(answer), flush=True)
"""


def run_helper(raw, root, state, journal, *, timeout=HELPER_TIMEOUT):
    """Run captured, accepted bytes in a fresh, independently bounded process."""
    payload = canonical({'root': str(root), 'state': state, 'journal': journal, 'parent': os.getpid(),
                         'launcher': base64.b64encode(read(root, SUPPORT + '/launcher.py', mode=0o644)).decode(),
                         'helper': base64.b64encode(raw).decode()})
    process = subprocess.Popen([sys.executable, '-I', '-B', '-c', CHILD_CODE], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True, close_fds=True)
    try:
        output, _error = process.communicate(payload, timeout=timeout)
        if process.returncode != 0 or len(output) > 4096:
            raise RecoveryError('helper_exit_or_result_limit')
        value = json_document(output)
        if 'error' in value:
            raise RecoveryError(value['error'])
        return value['result']
    except subprocess.TimeoutExpired as exc:
        raise RecoveryError('helper_timeout') from exc
    except (ValueError, KeyError) as exc:
        raise RecoveryError('helper_failed:' + str(exc)) from exc
    finally:
        # Stop descendants before fallback, including after the leader exits.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                raise
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            stream.close()


def _launch(root, *, helper_timeout):
    root = Path(root)
    try:
        state = document(root, STATE + '/state.json')
    except FileNotFoundError:
        # Before first publication the old controller is the only entry point.
        # A promotion journal without a selection is never safe to ignore.
        for relative in (STATE + '/journal.json',
                         'etc/systemd/system/messagebox-update-recovery.service.d/messagebox.conf',
                         'etc/systemd/system/messagebox-update-supervisor.service.d/messagebox.conf'):
            try:
                read(root, relative)
            except FileNotFoundError:
                continue
            raise RecoveryError('selection_missing')
        return 'not_initialized'
    with locks(root):
        state = validate_state(document(root, STATE + '/state.json'))
        verify, _trust = anchors(root)
        # Recovery uses accepted digest pins. Finishing a release-key change
        # may allocate and is admission work; it cannot gate emergency restore.
        try:
            journal = document(root, JOURNAL)
        except (OSError, ValueError):
            if verified_controller(root, state, verify):
                # With no transaction record, there is no authority to exchange.
                # Preserve a separately verified active set and disable promotion.
                return 'verified_no_journal'
            raise
        if (not isinstance(journal, dict) or journal.get('schema') != 1
                or not isinstance(journal.get('kind'), str) or not isinstance(journal.get('phase'), str)):
            raise RecoveryError('invalid_recovery_journal')
        last = None
        for role in ('current', 'fallback'):
            # A helper can fail after a durable mutation. Never hand the next
            # helper the first helper's stale in-memory journal or selection.
            state = validate_state(document(root, STATE + '/state.json'))
            journal = document(root, JOURNAL)
            slot = state['helpers'][role]
            selected, raw = select_helper(root, {**state, 'helpers': {'current': slot, 'fallback': slot}}, verify)
            if selected is None:
                continue
            try:
                run_helper(raw, root, state, journal, timeout=helper_timeout)
                if not verified_controller(root, state, verify):
                    raise RecoveryError('helper_left_unverified_controller')
                found = records(root)
                if len(found) != 2 or found[0][0] != found[1][0]:
                    # Repair a damaged primary too: the existing admission
                    # guard reads that path. One bad copy must not freeze OTA.
                    try:
                        save(root, JOURNAL, document(root, JOURNAL))
                    except (OSError, ValueError):
                        pass
                return role
            except (OSError, ValueError) as exc:
                last = exc
        if (journal.get('kind') == 'controller'
                and journal.get('phase') in {'committed', 'restored', 'unsupported', 'aborted'}
                and verified_controller(root, state, verify)):
            return 'verified_no_helper'
        if last is not None and journal.get('kind') != 'bootstrap':
            raise RecoveryError(str(last))
        else:
            # No exchange exists in this protocol. Both independently checked
            # initial sets are intact, so old application recovery stays usable.
            if journal not in ({'schema': 1, 'kind': 'bootstrap', 'phase': phase}
                               for phase in ('prepared', 'armed')):
                raise RecoveryError('helper_required')
            legacy_valid(root, state)
            return 'legacy_no_helper'


def launch(root=Path('/'), *, owned=False, helper_timeout=HELPER_TIMEOUT,
           supervisor=False, monitoring=False, lock_wait=70):
    root = Path(root)
    deadline = time.monotonic() + lock_wait
    while True:
        try:
            if owned or not (root / STATE / 'state.json').exists():
                if owned:
                    release_space_if_needed(root)
                return _launch(root, helper_timeout=helper_timeout)
            with lock(root, STATE + '/owner.lock'):
                try:
                    if not monitoring:
                        release_space_if_needed(root)
                    return _launch(root, helper_timeout=helper_timeout)
                except BlockingIOError:
                    # An app installer owns only the inner locks; it cannot
                    # exchange controllers. Let its supervisor stop it first.
                    if supervisor or monitoring:
                        return 'deferred'
                    raise
        except BlockingIOError:
            # A controller owner can still exchange modules. Neither boot nor
            # supervisor may execute controller code until that owner finishes.
            if monitoring:
                return 'deferred'
            if time.monotonic() >= deadline:
                raise RecoveryError('recovery_busy') from None
            time.sleep(0.05)


def release_space_if_needed(root):
    # The frozen application recovery command follows the boot gate and makes
    # small atomic metadata writes even in a terminal state. Give it reserved
    # blocks and an inode under pressure; controller restore itself allocates none.
    try:
        space = os.statvfs(Path(root) / STATE)
    except OSError:
        return
    if space.f_bavail * space.f_frsize >= 1024 * 1024 and space.f_favail >= 8:
        return
    with directory(root, STATE) as fd:
        try:
            os.unlink('recovery-reserve', dir_fd=fd)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass


def monitor(root=Path('/'), *, interval=1, stop=lambda: False):
    while not stop():
        expire_owner(root)
        try:
            launch(root, monitoring=True)
        except (OSError, ValueError, KeyError, TypeError):
            # The independent service remains armed even after a failed attempt.
            pass
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--monitor', action='store_true')
    parser.add_argument('--supervisor', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, 'root_required\n')
    try:
        monitor() if args.monitor else launch(supervisor=args.supervisor)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, 'recovery_support_invalid\n')


if __name__ == '__main__':
    main()
