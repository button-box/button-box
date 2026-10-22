"""Frozen controller A/B selector. Standard library only; never upgraded."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SLOTS = ('usr/lib/messagebox-update', 'usr/lib/messagebox-update-b')
HOME = 'usr/lib/messagebox-selector/selector.py'
RUN = 'run/messagebox-selector'
MODULES = ('bounded_update.py', 'health-probe.py', 'release_verify.py', 'trust_update.py',
           'update_controller.py', 'update_discovery.py')
CONSUMERS = tuple('messagebox-update-' + n + '.service' for n in ('discovery', 'install', 'controller', 'supervisor'))
COMMANDS = {'controller': ('update_controller.py', 'serve'), 'discovery': ('update_discovery.py',),
            'install': ('update_controller.py', 'install'), 'supervisor': ('bounded_update.py', 'supervise')}


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def document(raw):
    def pairs(items):
        value = dict(items)
        if len(value) != len(items):
            raise ValueError('duplicate keys')
        return value
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except RecursionError as exc:
        raise ValueError('JSON depth') from exc


def read(root, relative, limit=16384, mode=0o644, *, minimum=1):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or '..' in parts:
        raise ValueError('unsafe path')
    fds = [os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        for part in parts[:-1]:
            fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
        if any(os.fstat(fd).st_uid != (0 if Path(root) == Path('/') else os.geteuid()) or os.fstat(fd).st_mode & 0o022 for fd in fds):
            raise ValueError('unsafe directory')
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1])
        fds.append(fd)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != (0 if Path(root) == Path('/') else os.geteuid()) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != mode or not minimum <= info.st_size <= limit):
            raise ValueError('unsafe file')
        raw = os.read(fd, limit + 1)
        if len(raw) != info.st_size:
            raise ValueError('short read')
        return raw
    finally:
        for fd in reversed(fds):
            os.close(fd)


def checkpoint(event, path):
    pass


def flush(path, step=checkpoint):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
        step('directory_fsync', str(path))
    finally:
        os.close(fd)


def settle(root, relative, names, step=checkpoint):
    # Equality proves identity, not durability after an interrupted rename.
    for name in names:
        path = Path(root) / relative / name
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
            step('existing_file_fsync', str(path))
        finally:
            os.close(fd)
    path = Path(root) / relative
    while path != Path(root):
        flush(path, step)
        path = path.parent


def mkdir(path, step=checkpoint):
    if not path.exists():
        mkdir(path.parent, step)
        path.mkdir(mode=0o755)
        step('mkdir', str(path))
        flush(path.parent, step)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError('unsafe directory')


def write(path, raw, *, mode=0o644, durable=True, step=checkpoint):
    mkdir(path.parent, step)
    if durable:
        flush(path.parent.parent, step)  # Also durable after an interrupted mkdir/retry.
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(os.dup(fd), 'wb') as stream:
            stream.write(raw)
            stream.flush()
            step('write', str(path))
        if durable:
            os.fsync(fd)
            step('file_fsync', str(path))
    finally:
        os.close(fd)
    os.replace(temporary, path)
    step('rename', str(path))
    if durable:
        flush(path.parent, step)


@contextlib.contextmanager
def lock(root, name, *, shared=False, blocking=True):
    path = Path(root) / RUN
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd = os.open(path / name, (os.O_RDONLY if shared else os.O_RDWR | os.O_CREAT) | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def envelope(value):
    return canonical({'value': value, 'sha256': digest(canonical(value))})


def inspect(root, slot, value=None):
    if slot not in SLOTS:
        raise ValueError('unknown slot')
    if value is None:
        record = document(read(root, slot + '/GOOD'))
        value = record['value']
        if set(record) != {'value', 'sha256'} or digest(canonical(value)) != record['sha256']:
            raise ValueError('bad GOOD integrity')
    if (set(value) != {'generation', 'version', 'state_format', 'files', 'descriptor', 'legacy'}
            or type(value['generation']) is not int or value['generation'] < 0
            or type(value['state_format']) is not int or value['state_format'] != 1
            or type(value['legacy']) is not bool or not isinstance(value['files'], dict)
            or set(value['files']) != set(MODULES)):
        raise ValueError('incompatible GOOD')
    for name, identity in value['files'].items():
        raw = read(root, slot + '/' + name, 2 * 1024 * 1024)
        if identity != [len(raw), digest(raw)]:
            raise ValueError('bad module')
    return value


def valid(root):
    result = []
    for slot in SLOTS:
        try:
            result.append((slot, inspect(root, slot)))
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            continue
    return sorted(result, key=lambda item: (item[1]['generation'], item[0]), reverse=True)


def kernel_text(root, relative):
    # procfs reports size zero for these kernel-authored identity files.
    with (Path(root) / 'proc' / relative).open('rb') as stream:
        return stream.read(4096).decode().strip()


def boot_id(root):
    return kernel_text(root, 'sys/kernel/random/boot_id')


def process_token(root, pid):
    if not str(pid).isdigit() or int(pid) <= 0:
        raise ValueError('invalid process')
    return kernel_text(root, str(pid) + '/stat').rsplit(')', 1)[1].split()[19]


def choose(root, slot, value, *, trial=False, step=checkpoint):
    write(Path(root) / RUN / 'selected.json', canonical({'slot': slot, 'identity': value,
          'boot': boot_id(root), 'trial': trial}), durable=False, step=step)


def selected(root):
    value = document(read(root, RUN + '/selected.json'))
    if value['boot'] != boot_id(root):
        raise ValueError('previous boot')
    inspect(root, value['slot'], value['identity'] if value['trial'] else None)
    if not value['trial'] and inspect(root, value['slot']) != value['identity']:
        raise ValueError('generation changed')
    return value


def call(run, command, timeout=15):
    return run(command, check=True, capture_output=True, text=True, timeout=timeout)


def stop(root, run):
    # The gate is held. systemd completes the old cgroup stop; timer starts
    # after that wait at the gate and cannot resolve a slot until selection ends.
    call(run, ['systemctl', 'stop', *CONSUMERS], 20)


def python_command(root, slot, arguments, python=sys.executable):
    # -B alone still reads old .pyc files. Use an unwritten volatile cache
    # prefix so accepted source hashes identify the bytes Python executes.
    return [python, '-B', '-X', 'pycache_prefix=' + str(Path(root) / RUN / 'unused'),
            *(arguments if arguments[0] == '-c' else (str(Path(root) / slot / arguments[0]), *arguments[1:]))]


def execute(root, slot, arguments, timeout=180):
    if arguments[:2] == ('bounded_update.py', 'recover'):
        arguments = ('-c', recovery_program(root, slot, arguments))
    process = subprocess.Popen(python_command(root, slot, arguments),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
        if process.returncode or len(output) > 65536:
            raise ValueError('controller command failed')
        return output
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()


def restoration(root):
    journal = document(read(root, 'var/lib/button-box-update/journal.json', 1024 * 1024, 0o600))
    if journal['phase'] == 'committed':
        return []
    backup = journal['backup'].lstrip('/')
    try:
        records = document(read(root, backup + '/state.json', 2 * 1024 * 1024, 0o600))['files']
    except FileNotFoundError:
        if journal['phase'] == 'rolled_back':
            return []  # Completed field rollbacks predate retained snapshots.
        raise
    for record in records:
        if not Path(record['path']).is_absolute() or '..' in Path(record['path']).parts:
            raise ValueError('unsafe restoration path')
        if record['kind'] == 'file' and digest(read(root, backup + '/files/' + record['backup'], 2 * 1024 * 1024, 0o600, minimum=0)) != record['sha256']:
            raise ValueError('damaged restoration proof')
    return records


def recovered(root, required=None):
    """Read-only boot gate: terminal journal, exact pointer, no install inhibit."""
    state = 'var/lib/button-box-update/'
    if os.path.lexists(Path(root) / state / 'install-inhibit'):
        raise ValueError('application still inhibited')
    journal = document(read(root, state + 'journal.json', 1024 * 1024, 0o600))
    if (not isinstance(journal, dict) or journal.get('format') != 1 or journal.get('kind') not in {'application', 'bootstrap'}
            or journal.get('phase') not in {'committed', 'rolled_back'}):
        raise ValueError('application recovery incomplete')
    expected = journal.get('new' if journal['phase'] == 'committed' else 'old')
    if not isinstance(expected, str) or not re.fullmatch(r'/opt/messagebox-releases/[A-Za-z0-9._-]+-[0-9a-f]{40}', expected):
        raise ValueError('unsafe recovered pointer')
    pointer = Path(root) / 'opt/messagebox'
    if journal['kind'] == 'bootstrap' and journal['phase'] == 'rolled_back':
        if pointer.is_symlink() or not pointer.is_dir():
            raise ValueError('bootstrap restore incomplete')
    else:
        if os.readlink(pointer) != expected:
            raise ValueError('application pointer mismatch')
        read(root, expected.lstrip('/') + '/release.json')
        known = document(read(root, state + 'known-good.json', mode=0o600))
        if not isinstance(known, dict) or known.get('current') != expected:
            raise ValueError('known-good pointer mismatch')
    for record in restoration(root) if required is None else required:
        path = Path(root) / record['path'].lstrip('/')
        if record['kind'] == 'absent':
            if os.path.lexists(path):
                raise ValueError('restoration removal incomplete')
        elif record['kind'] == 'symlink':
            if not path.is_symlink() or os.readlink(path) != record['target']:
                raise ValueError('restoration link incomplete')
        else:
            if digest(read(root, record['path'].lstrip('/'), 2 * 1024 * 1024, record['mode'], minimum=0)) != record['sha256']:
                raise ValueError('restoration contents incomplete')
            info = path.lstat()
            if (info.st_uid, info.st_gid) != (record['uid'], record['gid']):
                raise ValueError('restoration ownership incomplete')


def recovery_program(root, slot, arguments):
    # Retain the existing snapshot across legacy pruning and failed attempts.
    # A terminal-but-incomplete rollback must run restoration again.
    return ("import sys;from pathlib import Path;sys.path.insert(0," + repr(str(Path(root) / slot)) + ");"
            "import bounded_update as u;root=Path(" + repr(str(root)) + ");"
            "u.prune_releases=lambda r:None;j=u.read_state(root,'journal.json');"
            "replay=" + repr(arguments[1] == 'recover') + " and j and j['phase']=='rolled_back' and (root/j['backup'].lstrip('/')/'state.json').exists();"
            "j.update(phase='rolling_back') if replay else None;u.save_state(root,'journal.json',j) if replay else None;"
            "u.main(" + repr(['--root', str(root), *arguments[1:]]) + ")")


def finish_recovery(root, slot, execute):
    journal = document(read(root, 'var/lib/button-box-update/journal.json', 1024 * 1024, 0o600))
    if not re.fullmatch(r'/var/lib/button-box-update/snapshots/[0-9a-f]{32}', journal['backup']):
        raise ValueError('unsafe recovery snapshot')
    directory = Path(root) / journal['backup'].lstrip('/')
    if journal['phase'] == 'rolled_back' and directory.exists():
        # Retire proof only after independent checks. Flush even if a prior
        # attempt unlinked it and failed its directory flush. Later legitimate
        # mode changes must not be undone by an already completed rollback.
        program = ("import os;fd=os.open(" + repr(str(directory)) + ",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);"
                   "\nif os.fstat(fd).st_uid!=os.geteuid() or os.fstat(fd).st_mode&0o777!=0o700: raise ValueError('unsafe snapshot')\n"
                   "\ntry: os.unlink('state.json',dir_fd=fd)\nexcept FileNotFoundError: pass\n"
                   "os.fsync(fd);os.close(fd)")
        execute(root, slot, ('-c', program), 15)


def recover(root, run, *, excluded=(), preferred=(), execute=execute, boot=True, deadline=None):
    deadline = deadline or time.monotonic() + 380
    with lock(root, 'gate'):
        stop(root, run)
        required = restoration(root)
        choices = sorted(valid(root), key=lambda item: item[0] not in preferred)
        for slot, value in choices:
            if slot in excluded:
                continue
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                execute(root, slot, ('bounded_update.py', 'recover', *(('--boot',) if boot else ())), min(180, remaining))
                recovered(root, required)
                finish_recovery(root, slot, execute)
                choose(root, slot, value)
                (Path(root) / RUN / 'trial.json').unlink(missing_ok=True)
                return slot
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
    raise ValueError('no working accepted controller')


def probe(root, value, run):
    before = call(run, ['systemctl', 'show', 'messagebox-update-controller.service', '--property=MainPID']).stdout
    pid = int(before.strip().split('=')[1])
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(2)
        connection.connect(str(Path(root) / 'run/messagebox-update/control.sock'))
        peer, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != 0 or peer != pid or pid <= 0:
            raise ValueError('wrong broker process')
        connection.sendall(b'{"command":"controller_handshake"}\n')
        raw = bytearray()
        while b'\n' not in raw and len(raw) <= 16384:
            chunk = connection.recv(4096)
            if not chunk:
                break
            raw.extend(chunk)
    answer = document(raw)
    if not value['legacy'] and answer != {'protocol': 1, 'version': value['version'],
                                         'hashes': {n: v[1] for n, v in value['files'].items()}}:
        raise ValueError('wrong broker identity')
    if before != call(run, ['systemctl', 'show', 'messagebox-update-controller.service', '--property=MainPID']).stdout:
        raise ValueError('broker changed')


def check(root, run=subprocess.run, *, probe=probe, execute=execute):
    try:
        with lock(root, 'operation', blocking=False):
            try:
                current = selected(root)
                if current['trial']:
                    raise ValueError('interrupted trial')
                recovered(root)  # A healthy broker cannot discharge recovery.
                finish_recovery(root, current['slot'], execute)
                deadline = time.monotonic() + 5
                while True:
                    try:
                        probe(root, current['identity'], run)
                        return
                    except (OSError, ValueError, subprocess.SubprocessError):
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.2)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                pass
            # Do not interrupt an ordinary application install. Its independent
            # deadline above and fixed worker timeout remain effective.
            if call(run, ['systemctl', 'show', 'messagebox-update-install.service', '--property=ActiveState', '--value']).stdout.strip() in {'active', 'activating', 'deactivating', 'reloading'}:
                return
            preferred = (tuple(s for s, _ in valid(root) if s != current['slot'])
                         if 'current' in locals() and not current['trial'] else ())
            recover(root, run, preferred=preferred, execute=execute, boot=False,
                    deadline=time.monotonic() + 380)
            call(run, ['systemctl', '--no-block', 'start', 'messagebox-update-controller.service'])
    except BlockingIOError:
        return  # The bounded publication service owns a live trial.


def sandbox(root, slot, name, argument):
    properties = ['NoNewPrivileges=yes', 'PrivateTmp=yes', 'ProtectSystem=strict',
                  'ProtectHome=yes', 'RuntimeMaxSec=20']
    if name == 'update_discovery.py':
        properties += ['User=messagebox-update', 'Group=messagebox-update',
                       'SupplementaryGroups=messagebox-settings', 'PrivateDevices=yes', 'UMask=0027',
                       'ReadWritePaths=/var/cache/button-box-update', 'CapabilityBoundingSet=',
                       'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6']
    else:
        properties += ['SupplementaryGroups=messagebox-update', 'UMask=0022',
                       'ReadWritePaths=/var/lib/button-box-update /run/messagebox-update /etc/button-box-update',
                       'CapabilityBoundingSet=CAP_CHOWN', 'RestrictAddressFamilies=AF_UNIX']
    command = ['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--service-type=exec']
    for prop in properties:
        command += ['-p', prop]
    return [*command, '--', *python_command(root, slot, (name, argument), '/usr/bin/python3')]


def trial(root, slot, value, application, run=subprocess.run, *, probe=probe, execute=execute,
          step=checkpoint, duration=75):
    inspect(root, slot, value)
    tried = document(read(root, slot + '/TRIED'))
    if tried != {'descriptor': value['descriptor']}:
        raise ValueError('trial not consumed')
    with lock(root, 'gate'):
        stop(root, run)
        write(Path(root) / RUN / 'trial.json', canonical({'descriptor': value['descriptor']}), durable=False, step=step)
        choose(root, slot, value, trial=True, step=step)
    started, deadline = time.monotonic(), time.monotonic() + duration
    context = {'transaction': os.urandom(16).hex(), 'boot': boot_id(root),
               'software_version': application['version'], 'software_commit': application['commit'],
               'controller_digest': value['descriptor']}
    write(Path(root) / 'run/messagebox-update/transaction.json', canonical(context), durable=False, step=step)
    for name in ('button', 'local'):
        (Path(root) / ('run/messagebox-update/' + name + '.json')).unlink(missing_ok=True)
        step('unlink_evidence', name)
    output = document(call(run, sandbox(root, slot, 'update_controller.py', 'self-test'), 25).stdout)
    if output != {'protocol': 1, 'version': value['version'], 'hashes': {n: v[1] for n, v in value['files'].items()}}:
        raise ValueError('trial self-test identity')
    discovery = document(call(run, sandbox(root, slot, 'update_discovery.py', '--self-test'), 25).stdout)
    if discovery != {'protocol': 1, 'dry_run': True}:
        raise ValueError('discovery trial result')
    recovery_trial(root, slot, execute)
    call(run, ['systemctl', 'start', 'messagebox-update-controller.service'])
    while time.monotonic() < deadline:
        try:
            probe(root, value, run)
            for name, unit in (('button', 'messagebox-button.service'), ('local', 'messagebox-poller.service')):
                evidence = document(read(root, 'run/messagebox-update/' + name + '.json'))
                pid = call(run, ['systemctl', 'show', unit, '--property=MainPID']).stdout.strip().split('=')[1]
                if (any(evidence.get(k) != v for k, v in context.items()) or str(evidence.get('pid')) != pid
                        or evidence.get('process_token') != process_token(root, pid)
                        or not evidence.get('ready') or not started <= evidence.get('monotonic', 0) <= time.monotonic()):
                    raise ValueError('no fresh process evidence')
            execute(root, slot, ('health-probe.py', '--current', '--local'), 15)
            inspect(root, slot, value)
        except (OSError, ValueError, subprocess.SubprocessError):
            time.sleep(0.2)
            continue
        write(Path(root) / slot / 'GOOD', envelope(value), step=step)
        choose(root, slot, value, step=step)
        (Path(root) / RUN / 'trial.json').unlink(missing_ok=True)
        step('trial_complete', slot)
        return
    raise ValueError('trial failed or timed out')


def recovery_trial(root, slot, execute):
    # Exercise the real candidate CLI and filesystem rollback on a staged
    # format-1 interrupted journal. Only systemctl is substituted in the child;
    # it must never operate on live services from this isolated root.
    journal = document(read(root, 'var/lib/button-box-update/journal.json', 1024 * 1024, 0o600))
    with tempfile.TemporaryDirectory(prefix='recovery-', dir=Path(root) / RUN) as directory:
        stage = Path(directory)
        journal.update(phase='switched', kind='application')
        for name in ('old', 'new'):
            write(stage / journal[name].lstrip('/') / 'release.json', b'{}\n', durable=False)
        (stage / 'opt/messagebox').symlink_to(journal['new'])
        state = stage / 'var/lib/button-box-update'
        write(state / 'journal.json', canonical(journal), mode=0o600, durable=False)
        write(state / 'install-inhibit', b'{}\n', durable=False)
        snapshot = stage / journal['backup'].lstrip('/')
        mkdir(snapshot / 'files')
        (snapshot / 'files').chmod(0o700)
        units = {n: {'active': 'inactive', 'enabled': 'disabled'} for n in journal['units']}
        content = b'[Service]\nExecStart=/usr/bin/true\n'
        write(snapshot / 'files/unit', content, mode=0o600, durable=False)
        write(snapshot / 'files/empty', b'', mode=0o600, durable=False)
        records = [{'path': '/etc/systemd/system/messagebox-button.service', 'kind': 'file', 'backup': 'unit',
                    'sha256': digest(content), 'mode': 0o644, 'uid': os.geteuid(), 'gid': os.getegid()},
                   {'path': '/etc/systemd/system/messagebox-poller.service.d/messagebox.conf', 'kind': 'file', 'backup': 'empty',
                    'sha256': digest(b''), 'mode': 0o644, 'uid': os.geteuid(), 'gid': os.getegid()},
                   {'path': '/etc/systemd/system/messagebox-button.service.d/messagebox.conf', 'kind': 'absent'}]
        for record in records:
            write(stage / record['path'].lstrip('/'), b'candidate integration\n', mode=0o600, durable=False)
            if record['kind'] == 'file':
                record['gid'] = (stage / record['path'].lstrip('/')).stat().st_gid
        write(snapshot / 'state.json', canonical({'format': 1, 'files': records, 'units': units}), mode=0o600, durable=False)
        required = restoration(stage)
        program = ("import sys,subprocess;sys.path.insert(0,sys.argv[1]);import bounded_update as u;"
                   "service=lambda c,**k:subprocess.CompletedProcess(c,0,"
                   "'inactive\\n' if c[1]=='is-active' else 'disabled\\n' if c[1]=='is-enabled' else '');"
                   "original=u.recover_transaction;u.recover_transaction=lambda **k:original(run=service,**k);"
                   "u.main(['--root',sys.argv[2],'recover','--boot'])")
        execute(root, slot, ('-c', program, str(Path(root) / slot), str(stage)), 25)
        recovered(stage, required)


def admit(root, source):
    """Protect the permanent integration even when the selected worker is old."""
    units = (*CONSUMERS, 'messagebox-update-recovery.service',
             'messagebox-update-selector.service', 'messagebox-update-selector.timer',
             'messagebox-update-supervisor.timer', 'messagebox-update-discovery.timer')
    protected = {'/etc/systemd/system/' + name for name in units}
    for name in ('messagebox-update-selector.service', 'messagebox-update-selector.timer'):
        if (Path(source) / 'systemd' / name).read_bytes() != read(root, 'etc/systemd/system/' + name):
            raise ValueError('fixed selector unit changed')
    for path in (Path(source) / 'systemd').rglob('*'):
        if path.is_file() and path.parent.name in {u + '.d' for u in units}:
            if path.read_bytes() != read(root, 'etc/systemd/system/' + path.parent.name + '/' + path.name):
                raise ValueError('fixed controller route changed')
            protected.add('/etc/systemd/system/' + path.parent.name + '/' + path.name)
    return protected


def install(root, worker, transaction, source, *args, **kwargs):
    protected = admit(root, source)
    backup, atomic, prune = worker.create_backup, worker._atomic_install, worker.prune_releases
    worker.prune_releases = lambda r: prune(r) if worker.read_state(r, 'journal.json')['phase'] == 'committed' else None
    # Fixed bytes need neither rewriting nor rollback. In particular, legacy
    # rollback unlinks a file before restore; keep the permanent gate out of it.
    worker.create_backup = lambda destination,r,entries,*a,**k: backup(destination,r,
        [e for e in entries if e['target'] not in protected],*a,**k)
    worker._atomic_install = lambda source,destination,*a,**k: (None if '/' + str(destination.relative_to(root)) in protected
                                                              else atomic(source,destination,*a,**k))
    try:
        return transaction(source, *args, **kwargs)
    finally:
        worker.create_backup, worker._atomic_install, worker.prune_releases = backup, atomic, prune


def install_program(root, slot):
    # Only the verified child imports controller code. Its signature/extraction
    # and transaction paths remain unchanged; admission runs before mutation.
    return ("import sys,runpy;from pathlib import Path;sys.path.insert(0," + repr(str(Path(root) / slot)) + ");"
            "import update_controller as c;guard=runpy.run_path(" + repr(str(Path(root) / HOME)) + ")['install'];"
            "original=c.update.install_transaction;"
            "c.update.install_transaction=lambda source,*a,**k:guard(Path(" + repr(str(root)) + "),c.update,original,source,*a,**k);"
            "sys.argv=['update_controller.py','install'];c.main()")


def run_consumer(root, command):
    with lock(root, 'gate', shared=True):
        value = selected(root)
        arguments = ('-c', install_program(root, value['slot'])) if command == 'install' else COMMANDS[command]
        if command == 'supervisor':
            arguments = ('-c', recovery_program(root, value['slot'], arguments))
    # Resolve once; changes stop this service's cgroup before slot reuse.
    os.execv(sys.executable, python_command(root, value['slot'], arguments))


def main():
    root = Path('/')
    command = sys.argv[1]
    if command == 'boot':
        with lock(root, 'operation'):
            recover(root, subprocess.run)
    elif command == 'check':
        check(root)
    else:
        run_consumer(root, command)


if __name__ == '__main__':
    main()
