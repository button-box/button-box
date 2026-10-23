"""Persist the Cloud updater boot-order repair outside application rollback.

Run as root by the existing post-commit bootstrap service. No application or
controller imports: this must also work with the original frozen updater.
"""

import argparse
import contextlib
import fcntl
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


DROPIN = 'etc/systemd/system/messagebox-update-recovery.service.d/80-boot-ordering.conf'
COMPLETE = 'run/messagebox-boot-repair-complete'
ORDERING = b'''[Unit]
# Recovery needs initialized local storage, but must precede application activators.
DefaultDependencies=no
Requires=sysinit.target
After=local-fs.target sysinit.target
Before=basic.target shutdown.target
Conflicts=shutdown.target
RequiresMountsFor=/usr /var /opt /run
'''
LOCKS = ('run/lock/messagebox-bounded-update.lock', 'run/lock/messagebox-mode-transition.lock')


def directory(path, root, uid, *, shared_lock=False):
    selected = root
    for part in path.relative_to(root).parts:
        root = root / part
        try:
            root.mkdir(mode=0o755)
        except FileExistsError:
            pass
        info = root.lstat()
        # Debian may provide a root-owned sticky /run/lock. Sticky ownership
        # protects the root lock inode; other writable parents remain unsafe.
        sticky_lock = shared_lock and root == selected / 'run/lock' and info.st_mode & stat.S_ISVTX
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
                or info.st_mode & 0o022 and not sticky_lock):
            raise ValueError('unsafe boot repair directory')


def read(path, root, uid):
    # Existing parents must be safe; never create configuration while reading it.
    parent = root
    for part in path.relative_to(root).parts[:-1]:
        parent = parent / part
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
            raise ValueError('unsafe boot repair directory')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid or info.st_mode & 0o022:
            raise ValueError('unsafe boot repair file')
        raw = os.read(fd, 128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError('oversized boot repair file')
        return raw
    finally:
        os.close(fd)


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(path, raw, root, uid, step):
    directory(path.parent, root, uid)
    try:
        existing = read(path, root, uid)
    except FileNotFoundError:
        existing = None
    if existing is not None and existing != raw:
        raise ValueError('boot repair file differs; maintenance required')
    if existing is None:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.boot-ordering-', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            try:
                os.fchmod(output.fileno(), 0o644)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
                step('file_fsync', temporary)
                os.replace(temporary, path)
                step('rename', path)
            finally:
                temporary.unlink(missing_ok=True)
    # Re-entry after rename also flushes the existing inode and every parent.
    sync(path)
    step('existing_file_fsync', path)
    parent = path.parent
    while parent != root:
        sync(parent)
        step('directory_fsync', parent)
        parent = parent.parent


@contextlib.contextmanager
def locks(root, uid):
    descriptors = []
    try:
        for name in LOCKS:
            path = root / name
            directory(path.parent, root, uid, shared_lock=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            descriptors.append(fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError('unsafe boot repair lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def repair(root=Path('/'), *, run=subprocess.run, bootstrap=False, step=lambda event, path: None):
    root = Path(root).resolve()
    if root == Path('/') and os.geteuid() != 0:
        raise ValueError('boot repair requires root')
    uid = 0 if root == Path('/') else root.stat().st_uid
    values = []
    for line in read(root / 'etc/messagebox/env', root, uid).decode().splitlines():
        key, separator, value = line.strip().partition('=')
        if separator and key == 'MSGBOX_TRANSPORT':
            values.append(shlex.split(value, comments=True))
    if values == [['wacli']]:
        return False
    if values != [['cloud']]:
        raise ValueError('Cloud boot repair configuration is unavailable')
    with locks(root, uid):
        if (root / 'var/lib/button-box-update/install-inhibit').exists():
            raise BlockingIOError('installation in progress')
        if not bootstrap:
            journal = json.loads(read(root / 'var/lib/button-box-update/journal.json', root, uid))
            pointer = root / 'opt/messagebox'
            phase = journal.get('phase')
            expected = journal.get('new') if phase == 'committed' else journal.get('old') if phase == 'rolled_back' else None
            if (journal.get('kind') != 'application' or expected is None
                    or not pointer.is_symlink() or os.readlink(pointer) != expected):
                raise BlockingIOError('application has not committed')
            if phase == 'rolled_back':
                known = json.loads(read(root / 'var/lib/button-box-update/known-good.json', root, uid))
                if known.get('current') != expected:
                    raise BlockingIOError('application rollback has not settled')
        read(root / 'etc/systemd/system/messagebox-update-recovery.service', root, uid)
        publish(root / DROPIN, ORDERING, root, uid, step)
        run(['systemctl', 'daemon-reload'], check=True, timeout=30)
        step('daemon_reload', root / DROPIN)
        loaded = run(['systemctl', 'show', 'messagebox-update-recovery.service',
                      '--property=DefaultDependencies,After,Before,Conflicts'],
                     check=True, capture_output=True, text=True, timeout=5)
        properties = dict(line.split('=', 1) for line in loaded.stdout.splitlines() if '=' in line)
        after = set(properties.get('After', '').split())
        if (properties.get('DefaultDependencies') != 'no' or 'basic.target' in after
                or not {'sysinit.target', 'local-fs.target'} <= after
                or not {'basic.target', 'shutdown.target'} <= set(properties.get('Before', '').split())
                or 'shutdown.target' not in properties.get('Conflicts', '').split()):
            raise ValueError('loaded recovery ordering does not match repair')
        # The legacy hotfix's service condition suppresses repeated bootstrap
        # jobs only after the durable repair and successful reload.
        publish(root / COMPLETE, b'complete\n', root, uid, step)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bootstrap', action='store_true')
    options = parser.parse_args()
    try:
        repair(bootstrap=options.bootstrap)
    except BlockingIOError:
        return 75
    except (OSError, ValueError, subprocess.SubprocessError):
        print('Cloud boot ordering repair deferred or rejected.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
