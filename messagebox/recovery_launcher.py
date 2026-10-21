"""Permanent, controller-independent recovery entry point. Protocol 1.

This file and its verifier anchors are installed once. A later application must
not replace them. Signed helpers own transaction recovery. The permanent
launcher only dispatches verified bytes through the stable protocol 1 envelope.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
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


def document(root, relative):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RecoveryError('duplicate_state_key')
            result[key] = value
        return result
    return json.loads(read(root, relative, 16384, mode=0o600), object_pairs_hook=unique)


def save(root, relative, value, *, step=checkpoint):
    write(root, relative, (json.dumps(value, sort_keys=True) + '\n').encode(), step=step)


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
        if (set(slot) != {'directory', 'digest'} or not re.fullmatch('[0-9a-f]{64}', slot['digest'])
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


def select_helper(root, state, verify):
    for role in ('current', 'fallback'):
        slot = state['helpers'][role]
        try:
            path = Path(root) / slot['directory']
            raw = read(root, slot['directory'] + '/' + verify.CONTROLLER_DESCRIPTOR, verify.MAX_DESCRIPTOR, mode=0o644)
            sig = read(root, slot['directory'] + '/' + verify.CONTROLLER_SIGNATURE, verify.MAX_CONTROLLER_SIGNATURE, mode=0o644)
            proof = verify.verify_controller_proof(raw, sig, Path(root) / TRUST / 'release-keys')
            value = proof['descriptor']
            if (proof['descriptor_sha256'] != slot['digest'] or value['schema'] != 2
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


def launch(root=Path('/')):
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
        verify, trust = anchors(root)
        trust.recover_trust(root / TRUST, root / 'var/lib/button-box-update')
        journal = document(root, STATE + '/journal.json')
        if (not isinstance(journal, dict) or journal.get('schema') != 1
                or not isinstance(journal.get('kind'), str) or not isinstance(journal.get('phase'), str)):
            raise RecoveryError('invalid_recovery_journal')
        role, raw = select_helper(root, state, verify)
        if raw is None:
            # No exchange exists in this protocol. Both independently checked
            # initial sets are intact, so old application recovery stays usable.
            if journal not in ({'schema': 1, 'kind': 'bootstrap', 'phase': phase}
                               for phase in ('prepared', 'armed')):
                raise RecoveryError('helper_required')
            legacy_valid(root, state)
            return 'legacy_no_helper'
        namespace = {'__name__': 'verified_recovery_helper'}
        exec(compile(raw, '<verified-recovery-helper>', 'exec'), namespace)
        # Helpers receive the fixed root and bounded state. A future signed
        # helper can recover controller transactions without replacing this file.
        def checked_legacy():
            return legacy_valid(root, state)
        result = namespace['recover']({'journal': journal, 'root': root, 'state': state,
                                       'check_legacy': checked_legacy})
        if result not in {'legacy', 'recovered', 'committed', 'deferred'}:
            raise RecoveryError('invalid_helper_result')
        return role


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, 'root_required\n')
    try:
        launch()
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, 'recovery_support_invalid\n')


if __name__ == '__main__':
    main()
