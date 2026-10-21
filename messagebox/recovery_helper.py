"""Signed helper protocol 1: whole-directory controller promotion and recovery.

Only this versioned, verified file owns transaction phases. The permanent
launcher stays unchanged. No application/controller module is imported here.
"""

import base64
import contextlib
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

PROTOCOL = 1
HELPER_VERSION = '1.1.0'
LAUNCHER_SHA256 = '6fee72beb953b1980df155e991cabce1f6bf9426875e631f9878d702564af7dc'
STATE = 'var/lib/button-box-controller'
ACTIVE = 'usr/lib/messagebox-update'
INHIBIT = 'run/messagebox-update/promotion-inhibit.json'
JOURNAL = STATE + '/journal.json'
MODULES = ('bounded_update.py', 'health-probe.py', 'release_verify.py', 'trust_update.py',
           'update_controller.py', 'update_discovery.py')
TIMERS = ('messagebox-update-discovery.timer', 'messagebox-update-supervisor.timer')
SERVICES = ('messagebox-update-discovery.service', 'messagebox-update-install.service',
            'messagebox-update-controller.service', 'messagebox-update-supervisor.service')
UNITS = (*TIMERS, *SERVICES)
TERMINAL = {'committed', 'restored', 'unsupported', 'aborted'}


class PromotionError(ValueError):
    pass


class ExchangeUnavailable(OSError):
    pass


def runtime(root):
    # PR3's permanent launcher is an immutable, signed dependency. Open every
    # parent without following links before importing its exact pinned bytes.
    fds = []
    try:
        fds.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for name in ('usr', 'lib', 'messagebox-recovery'):
            fds.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
        for fd in fds:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise PromotionError('unsafe_recovery_anchor')
        fd = os.open('launcher.py', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1])
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o644 or info.st_size > 65536):
                raise PromotionError('unsafe_recovery_anchor')
            raw = stream.read(65537)
        if hashlib.sha256(raw).hexdigest() != LAUNCHER_SHA256:
            raise PromotionError('recovery_anchor_mismatch')
        spec = importlib.util.spec_from_loader('permanent_recovery', loader=None)
        module = importlib.util.module_from_spec(spec)
        exec(compile(raw, '<permanent-recovery>', 'exec'), module.__dict__)
        return module
    finally:
        for fd in reversed(fds):
            os.close(fd)


def exchange_at(parent_fd, left, right):
    """Linux atomic exchange only. Unsupported means keep the old set active."""
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise ExchangeUnavailable(errno.ENOSYS, 'atomic_exchange_unavailable') from exc
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(left), parent_fd, os.fsencode(right), 2):
        code = ctypes.get_errno()
        if code in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV}:
            raise ExchangeUnavailable(code, 'atomic_exchange_unavailable')
        raise OSError(code, 'atomic_exchange_failed')


def encoded(value):
    return (json.dumps(value, sort_keys=True) + '\n').encode()


def checkpoint(event, path):
    """Injected crash boundary after the named operation."""


def record(api, root, journal, phase, step):
    journal['phase'] = phase
    if len(encoded(journal)) > 16384:
        raise PromotionError('promotion_journal_limit')
    api.save(root, JOURNAL, journal, step=step)
    step('journal_' + phase, JOURNAL)


def flush_directory(api, root, path, step):
    with api.directory(root, path) as fd:
        os.fsync(fd)
        step('directory_fsync', path)


def swap(api, root, journal, exchange, step):
    with api.directory(root, 'usr/lib') as parent:
        # The parent FD anchors both real directories on the same filesystem.
        for name in ('messagebox-update', Path(journal['stage']).name):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                api.check_dir(fd)
            finally:
                os.close(fd)
        try:
            exchange(parent, 'messagebox-update', Path(journal['stage']).name)
        except OSError as exc:
            if exc.errno in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV}:
                raise ExchangeUnavailable(exc.errno, 'atomic_exchange_unavailable') from exc
            raise
        step('exchange', 'usr/lib')
        os.fsync(parent)
        step('directory_fsync', 'usr/lib')


def identity(api, root, base, *, version=None, digest=None):
    files = []
    for name in MODULES:
        raw = api.read(root, base + '/' + name, mode=0o644)
        files.append({'path': name, 'size': len(raw), 'sha256': api.sha(raw)})
    try:
        metadata = api.read(root, base + '/controller.json', 16384, mode=0o644)
    except FileNotFoundError:
        metadata = None
    with api.directory(root, base) as fd:
        names = set(os.listdir(fd))
        if names - set(MODULES) - {'controller.json', '__pycache__'}:
            raise PromotionError('unexpected_controller_file')
    if metadata is not None:
        value = json.loads(metadata)
        if set(value) != {'schema', 'version', 'descriptor_sha256'} or value['schema'] != 1:
            raise PromotionError('invalid_controller_identity')
        version, digest = value['version'], value['descriptor_sha256']
    if version is None or digest is None:
        raise PromotionError('controller_identity_required')
    return {'version': version, 'digest': digest, 'files': files,
            'metadata': api.sha(metadata) if metadata is not None else None}


def matches(api, root, base, expected):
    try:
        return identity(api, root, base, version=expected['version'], digest=expected['digest']) == expected
    except (OSError, ValueError, KeyError, TypeError):
        return False


def validate_journal(journal):
    if (journal.get('schema') != 1 or journal.get('kind') != 'controller'
            or not re.fullmatch('[0-9a-f]{32}', journal.get('transaction', ''))
            or journal.get('stage') != 'usr/lib/messagebox-update-stage-' + journal['transaction']
            or journal.get('phase') not in {'intent', 'staged', 'exchange_intent', 'exchanged', 'activated',
                                           'committed', 'restore_intent', 'restored', 'unsupported', 'aborted'}
            or journal.get('helper_protocol') != 1):
        raise PromotionError('invalid_promotion_journal')
    for key in ('old', 'new'):
        value = journal[key]
        if ([entry['path'] for entry in value['files']] != list(MODULES)
                or any(set(entry) != {'path', 'size', 'sha256'} or type(entry['size']) is not int
                       or not 0 < entry['size'] <= 2 * 1024 * 1024
                       or not re.fullmatch('[0-9a-f]{64}', entry['sha256']) for entry in value['files'])):
            raise PromotionError('invalid_controller_identity')
    if (set(journal['services']) != set(UNITS)
            or any(set(value) != {'active', 'enabled'}
                   or value['active'] not in {'active', 'inactive', 'failed'}
                   or value['enabled'] not in {'enabled', 'disabled', 'static'}
                   for value in journal['services'].values())):
        raise PromotionError('invalid_service_snapshot')
    paths = ['etc/systemd/system/messagebox-update-' + name + '.service.d/messagebox.conf'
             for name in ('recovery', 'supervisor')]
    gate = b'[Service]\nExecStartPre=/usr/bin/python3 -I /usr/lib/messagebox-recovery/launcher.py\n'
    if ([entry['path'] for entry in journal['units']] != paths
            or any(set(entry) != {'path', 'prior', 'new', 'mode'} or entry['mode'] != 0o644
                   or base64.b64decode(entry['prior'], validate=True) != gate
                   or base64.b64decode(entry['new'], validate=True) != gate
                   for entry in journal['units'])):
        raise PromotionError('invalid_integration_snapshot')
    for key in ('old', 'new'):
        value = journal[key]
        if (set(value) != {'version', 'digest', 'files', 'metadata'}
                or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value['version'])
                or not re.fullmatch('[0-9a-f]{64}', value['digest'])
                or value['metadata'] is not None and not re.fullmatch('[0-9a-f]{64}', value['metadata'])):
            raise PromotionError('invalid_controller_identity')
    return journal


def call(run, command, *, timeout=15):
    return run(command, check=True, capture_output=True, text=True, timeout=timeout)


def states(run):
    result = {}
    for unit in UNITS:
        value = call(run, ['systemctl', 'show', unit, '--property=ActiveState,UnitFileState,MainPID'])
        fields = dict(line.split('=', 1) for line in value.stdout.splitlines() if '=' in line)
        if (fields.get('ActiveState') not in {'active', 'inactive', 'failed'}
                or fields.get('UnitFileState') not in {'enabled', 'disabled', 'static'}
                or not fields.get('MainPID', '').isdigit()):
            raise PromotionError('unsettled_updater')
        result[unit] = {'active': fields['ActiveState'], 'enabled': fields['UnitFileState']}
        if unit in {'messagebox-update-install.service', 'messagebox-update-supervisor.service'} and fields['ActiveState'] == 'active':
            raise PromotionError('worker_busy')
    return result


def quiesce(run, step, *, recovering=False):
    own = set()
    if recovering:
        unit = 'messagebox-update-supervisor.service'
        raw = call(run, ['systemctl', 'show', unit, '--property=ActiveState,MainPID,ControlPID']).stdout
        values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        # The permanent launcher is this unit's ExecStartPre. No supervisor
        # controller code has started yet. Stopping our own unit would kill
        # recovery before it could restore; exempt only this exact process.
        if values.get('ControlPID') == str(os.getpid()) and values.get('MainPID') == '0':
            own.add(unit)
    for batch in (TIMERS, tuple(unit for unit in SERVICES if unit not in own)):
        call(run, ['systemctl', 'stop', *batch], timeout=30)
        step('stop', ','.join(batch))
    jobs = call(run, ['systemctl', 'list-jobs', '--no-legend', '--plain']).stdout
    if any(unit in jobs for unit in UNITS if unit not in own):
        raise PromotionError('updater_job_pending')
    for unit in UNITS:
        if unit in own:
            continue
        value = call(run, ['systemctl', 'show', unit, '--property=ActiveState,MainPID'])
        fields = dict(line.split('=', 1) for line in value.stdout.splitlines() if '=' in line)
        if fields.get('ActiveState') not in {'inactive', 'failed'} or fields.get('MainPID') != '0':
            raise PromotionError('updater_process_alive')
    step('quiesced', 'systemd')


def restore_services(run, saved, step, *, boot=False):
    # Never wait for a consumer from inside the permanent launcher's locks.
    for unit, prior in saved.items():
        if prior['enabled'] in {'enabled', 'disabled'}:
            call(run, ['systemctl', 'enable' if prior['enabled'] == 'enabled' else 'disable', unit])
            step('enablement', unit)
    if saved['messagebox-update-controller.service']['active'] != 'active':
        call(run, ['systemctl', 'stop', 'messagebox-update-controller.service'], timeout=30)
        step('stop', 'messagebox-update-controller.service')
    active = [unit for unit, prior in saved.items() if prior['active'] == 'active'
              and unit not in {'messagebox-update-install.service', 'messagebox-update-supervisor.service'}]
    if active:
        command = ['systemctl', '--no-block', 'start', *active] if boot else ['systemctl', 'start', *active]
        call(run, command, timeout=30)
        step('start', ','.join(active))


def clear_inhibit(api, root, step):
    try:
        with api.directory(root, 'run/messagebox-update') as fd:
            try:
                os.unlink('promotion-inhibit.json', dir_fd=fd)
            except FileNotFoundError:
                pass
            else:
                step('unlink', INHIBIT)
            os.fsync(fd)
            step('directory_fsync', 'run/messagebox-update')
    except FileNotFoundError:
        # /run is volatile. Boot recovery precedes the controller unit that
        # creates RuntimeDirectory=messagebox-update. Absence is already clear.
        return


def integration(api, root, proof):
    # PR4 keeps the signed PR3 recovery integration. Later protocols can extend
    # this exact consumer contract without weakening the permanent parser.
    expected = proof['descriptor']['unit_changes']
    allowed = ['/etc/systemd/system/messagebox-update-' + name + '.service.d/messagebox.conf'
               for name in ('recovery', 'supervisor')]
    if [entry['path'] for entry in expected] != allowed:
        raise PromotionError('unsupported_unit_changes')
    result = []
    for entry in expected:
        path = entry['path'].lstrip('/')
        raw = api.read(root, path, 65536, mode=entry['mode'])
        if len(raw) != entry['size'] or api.sha(raw) != entry['sha256']:
            raise PromotionError('recovery_integration_changed')
        result.append({'path': path, 'prior': base64.b64encode(raw).decode(), 'mode': entry['mode'],
                       'new': base64.b64encode(raw).decode()})
    return result


def restore_integration(api, root, journal, step):
    for entry in journal['units']:
        if entry['prior'] is None:
            with api.directory(root, str(Path(entry['path']).parent)) as fd:
                try:
                    os.unlink(Path(entry['path']).name, dir_fd=fd)
                    step('unlink', entry['path'])
                except FileNotFoundError:
                    pass
                os.fsync(fd)
                step('directory_fsync', str(Path(entry['path']).parent))
        else:
            api.write(root, entry['path'], base64.b64decode(entry['prior'], validate=True), mode=entry['mode'], step=step)


def reconcile(api, root, journal, *, run, exchange, step, boot):
    validate_journal(journal)
    old_active = matches(api, root, ACTIVE, journal['old'])
    new_active = matches(api, root, ACTIVE, journal['new'])
    if journal['phase'] == 'committed':
        if not new_active or not matches(api, root, journal['stage'], journal['old']):
            raise PromotionError('committed_controller_invalid')
        record(api, root, journal, 'committed', step)
        clear_inhibit(api, root, step)
        restore_services(run, journal['services'], step, boot=boot)
        return 'committed'
    # A durable non-commit always returns to the exact previous set. The phase
    # cannot tell us if exchange completed; compare complete identities instead.
    known_good = matches(api, root, journal['stage'], journal['old'])
    if new_active or not old_active and known_good:
        # A failed activation can leave changed candidate bytes. The exact
        # known-good set is enough to restore it; do not execute those bytes.
        if not known_good:
            raise PromotionError('known_good_missing')
        record(api, root, journal, 'restore_intent', step)
        quiesce(run, step, recovering=boot)
        swap(api, root, journal, exchange, step)
        if not matches(api, root, ACTIVE, journal['old']):
            raise PromotionError('restore_identity_failed')
    elif not old_active:
        raise PromotionError('controller_identity_ambiguous')
    restore_integration(api, root, journal, step)
    call(run, ['systemctl', 'daemon-reload'])
    step('daemon_reload', 'systemd')
    outcome = journal['phase'] if journal['phase'] in {'unsupported', 'aborted'} else 'restored'
    record(api, root, journal, outcome, step)
    clear_inhibit(api, root, step)
    restore_services(run, journal['services'], step, boot=boot)
    return 'recovered'


def recover(context):
    journal = context['journal']
    if journal.get('kind') == 'bootstrap':
        if (journal not in ({'schema': 1, 'kind': 'bootstrap', 'phase': phase} for phase in ('prepared', 'armed'))
                or context['check_legacy']() is not True):
            raise PromotionError('unsupported_recovery_state')
        return 'legacy'
    api = runtime(context['root'])
    return reconcile(api, Path(context['root']), journal, run=subprocess.run,
                     exchange=exchange_at, step=checkpoint, boot=True)


def admitted(proof, accepted):
    value = proof['descriptor']
    def version(text):
        if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', text):
            raise PromotionError('invalid_version')
        return tuple(map(int, text.split('.')))
    result = {'sequence': value['controller_sequence'], 'digest': proof['descriptor_sha256'],
              'controller_version': value['controller_version'], 'helper_version': value['recovery']['helper_version']}
    if (value['schema'] != 2 or value['min_helper_protocol'] != 1 or value['min_launcher_protocol'] != 1
            or value['recovery']['protocol'] != 1 or version(value['recovery']['helper_version']) < (1, 1, 0)
            or result['sequence'] < accepted['sequence']
            or result['sequence'] == accepted['sequence'] and result['digest'] != accepted['digest']
            or version(result['controller_version']) < version(accepted['controller_version'])
            or version(result['helper_version']) < version(accepted['helper_version'])):
        raise PromotionError('controller_replay_or_protocol')
    return result


def committed_application(api, root, release):
    # The exact committed application is the compatibility pair for BOTH sets.
    path = str(Path(release))
    if not re.fullmatch(r'opt/messagebox-releases/[A-Za-z0-9._-]+-[0-9a-f]{40}', path):
        raise PromotionError('unsafe_release')
    with api.directory(root, 'opt') as parent:
        if os.readlink('messagebox', dir_fd=parent) != '/' + path:
            raise PromotionError('application_changed')
    raw = api.read(root, 'var/lib/button-box-update/journal.json', 2 * 1024 * 1024, mode=0o600)
    journal = json.loads(raw)
    known = json.loads(api.read(root, 'var/lib/button-box-update/known-good.json', 16384, mode=0o600))
    identity = json.loads(api.read(root, path + '/release.json', 16384, mode=0o644))
    if (journal.get('phase') != 'committed' or journal.get('new') != '/' + path
            or known.get('current') != '/' + path or journal.get('version') != identity.get('version')
            or journal.get('commit') != identity.get('commit')):
        raise PromotionError('application_not_committed')
    return {'path': path, 'version': identity['version'], 'commit': identity['commit'],
            'transaction': journal.get('transaction'), 'journal_sha256': api.sha(raw)}


def helper_bundle(api, root, source, proof, state, verify, step):
    # Before the first exchange replace BOTH PR3-only slots. A PR3 fallback
    # cannot interpret the controller journal. Retain previous copies on disk.
    accepted = admitted(proof, state['accepted'])
    files = {}
    for entry in proof['descriptor']['modules'] + proof['descriptor']['recovery']['files']:
        raw = api.read(root, source + '/' + entry['path'], mode=entry['mode'])
        if len(raw) != entry['size'] or api.sha(raw) != entry['sha256']:
            raise PromotionError('candidate_changed')
        files[entry['path']] = raw
    raw = api.read(root, source + '/' + verify.CONTROLLER_DESCRIPTOR, verify.MAX_DESCRIPTOR, mode=0o644)
    sig = api.read(root, source + '/' + verify.CONTROLLER_SIGNATURE, verify.MAX_CONTROLLER_SIGNATURE, mode=0o644)
    if api.sha(raw) != accepted['digest']:
        raise PromotionError('candidate_changed')
    verify.verify_controller_proof(raw, sig, Path(root) / 'etc/button-box-update/release-keys')
    if files['messagebox/recovery_helper.py'] != api.read(root, source + '/messagebox/recovery_helper.py', mode=0o644):
        raise PromotionError('candidate_changed')
    # The calling helper must be the candidate's own verified implementation.
    # The dispatch entry point enforces this before calling promote().
    slots = {}
    retained = None
    if state['accepted']['helper_version'] != '1.0.0':
        # On a same-digest retry keep the previous fallback, not another copy
        # of the candidate. If current is damaged, retain the verified fallback.
        roles = ('fallback', 'current') if state['accepted']['digest'] == accepted['digest'] else ('current', 'fallback')
        for role in roles:
            slot = state['helpers'][role]
            checked = {**state, 'helpers': {'current': slot, 'fallback': slot}}
            if api.select_helper(root, checked, verify)[0] == 'current':
                raw_proof = api.read(root, slot['directory'] + '/' + verify.CONTROLLER_DESCRIPTOR,
                                     verify.MAX_DESCRIPTOR, mode=0o644)
                value = verify.parse_controller_descriptor(raw_proof)
                if tuple(map(int, value['recovery']['helper_version'].split('.'))) >= (1, 1, 0):
                    retained = slot
                    break
        if retained is None:
            raise PromotionError('working_helper_fallback_required')
    for role in ('current', 'fallback'):
        if role == 'fallback' and retained is not None:
            slots[role] = retained
            continue
        base = STATE + '/helpers/' + accepted['digest'] + '/' + role
        for path, content in files.items():
            api.write(root, base + '/' + path, content, mode=0o644, step=step)
        api.write(root, base + '/' + verify.CONTROLLER_DESCRIPTOR, raw, mode=0o644, step=step)
        api.write(root, base + '/' + verify.CONTROLLER_SIGNATURE, sig, mode=0o644, step=step)
        checked = {**state, 'helpers': {'current': {'directory': base, 'digest': accepted['digest']},
                                      'fallback': {'directory': base, 'digest': accepted['digest']}}}
        if api.select_helper(root, checked, verify)[0] != 'current':
            raise PromotionError('helper_slot_invalid')
        slots[role] = {'directory': base, 'digest': accepted['digest']}
    result = {**state, 'accepted': accepted, 'helpers': slots}
    api.save(root, STATE + '/state.json', result, step=step)
    return result, files


@contextlib.contextmanager
def mode_locks(api, root):
    with api.lock(root, 'run/lock/messagebox-mode-transition.lock'):
        # Existing interaction reservation uses 0644, not the guard's 0600.
        with api.directory(root, 'run/messagebox-update', create=True) as parent:
            fd = os.open('maintenance.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o644, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o644):
                    raise PromotionError('unsafe_interaction_lock')
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield
            finally:
                os.close(fd)


def controller_handshake(root):
    path = str(Path(root) / 'run/messagebox-update/control.sock')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(path)
        connection.sendall(b'{"command":"controller_handshake"}\n')
        raw = bytearray()
        while b'\n' not in raw and len(raw) <= 16384:
            part = connection.recv(4096)
            if not part:
                break
            raw.extend(part)
        if len(raw) > 16384:
            raise PromotionError('handshake_limit')
        return json.loads(raw)


def effective_units(run):
    # Refuse to arm on masked/replaced units, other overrides, disabled recovery,
    # or ordering that differs from the frozen dependency chain.
    for unit, start in (
            ('messagebox-update-recovery.service', '/usr/lib/messagebox-update/bounded_update.py recover --boot'),
            ('messagebox-update-supervisor.service', '/usr/lib/messagebox-update/bounded_update.py supervise')):
        result = run(['systemctl', 'show', unit, '--property=ExecStartPre,ExecStart,Before,After,LoadState'],
                     check=True, capture_output=True, text=True, timeout=15)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        def commands(value):
            groups = re.findall(r'\{([^{}]*)\}', value)
            if len(groups) != value.count('{'):
                return []
            commands = []
            for group in groups:
                fields = dict(part.strip().split('=', 1) for part in group.split(';') if '=' in part)
                if fields.get('path') != '/usr/bin/python3' or fields.get('ignore_errors') != 'no':
                    return []
                commands.append(fields.get('argv[]'))
            return commands
        if (values.get('LoadState') != 'loaded' or commands(values.get('ExecStartPre', '')) != ['/usr/bin/python3 -I /usr/lib/messagebox-recovery/launcher.py']
                or commands(values.get('ExecStart', '')) != ['/usr/bin/python3 ' + start]):
            raise PromotionError('recovery_unit_mismatch')
        if unit.endswith('recovery.service'):
            if not {'messagebox.target', 'comitup.service', 'messagebox-update-controller.service'} <= set(values.get('Before', '').split()):
                raise PromotionError('recovery_order_mismatch')
        elif 'messagebox-update-recovery.service' not in values.get('After', '').split():
            raise PromotionError('recovery_order_mismatch')
    result = run(['systemctl', 'is-enabled', 'messagebox-update-recovery.service'],
                 check=True, capture_output=True, text=True, timeout=15)
    if result.stdout.strip() != 'enabled':
        raise PromotionError('recovery_not_enabled')

    starts = {
        'messagebox-update-controller.service': 'update_controller.py serve',
        'messagebox-update-discovery.service': 'update_discovery.py',
        'messagebox-update-install.service': 'update_controller.py install',
    }
    for unit in UNITS:
        raw = call(run, ['systemctl', 'show', unit, '--property=LoadState,User,TimeoutStartUSec,ExecStart,ExecStartPre']).stdout
        values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        if values.get('LoadState') != 'loaded':
            raise PromotionError('activation_unit_not_loaded')
        if unit in starts:
            expected = '/usr/bin/python3 /usr/lib/messagebox-update/' + starts[unit]
            if commands(values.get('ExecStart', '')) != [expected]:
                raise PromotionError('activation_executable_changed')
            user = 'messagebox-update' if unit.endswith('discovery.service') else ''
            if values.get('User') != user:
                raise PromotionError('activation_user_changed')
        expected = {'messagebox-update-discovery.service': '15min', 'messagebox-update-install.service': '5min 30s'}.get(unit)
        if expected and values.get('TimeoutStartUSec') != expected:
            raise PromotionError('activation_timeout_changed')


def activate(api, root, journal, run, handshake, monotonic, step):
    if monotonic() >= journal['deadline_mono']:
        raise PromotionError('activation_timeout')
    effective_units(run)
    expected = {entry['path']: entry['sha256'] for entry in journal['new']['files']}
    command = [sys.executable, '-B', str(Path(root) / ACTIVE / 'update_controller.py'), 'self-test']
    result = json.loads(call(run, command, timeout=max(1, journal['deadline_mono'] - monotonic())).stdout)
    if result != {'protocol': 1, 'version': journal['new']['version'], 'hashes': expected}:
        raise PromotionError('controller_self_test_mismatch')
    step('self_test', ACTIVE)
    call(run, ['systemctl', 'start', 'messagebox-update-controller.service'], timeout=20)
    step('controller_start', 'systemd')
    result = handshake(root)
    if result != {'protocol': 1, 'version': journal['new']['version'], 'hashes': expected}:
        raise PromotionError('controller_handshake_mismatch')
    step('handshake', ACTIVE)
    result = json.loads(call(run, ['runuser', '-u', 'messagebox-update', '--', sys.executable, '-B',
                                 str(Path(root) / ACTIVE / 'update_discovery.py'), '--self-test'], timeout=15).stdout)
    if result != {'protocol': 1, 'dry_run': True}:
        raise PromotionError('discovery_dry_run_failed')
    step('discovery_self_test', ACTIVE)
    # The committed application remains running. Its existing health probe uses
    # local evidence in wacli mode and never needs a Cloud request.
    call(run, [sys.executable, '-B', str(Path(root) / ACTIVE / 'health-probe.py'), '--current'], timeout=10)
    step('application_health', 'application')
    if monotonic() >= journal['deadline_mono'] or not matches(api, root, ACTIVE, journal['new']):
        raise PromotionError('activation_timeout_or_changed')
    return True


def reverify(api, root, source, expected, application, platform):
    verify, trust = api.anchors(root)
    trust.recover_trust(root / 'etc/button-box-update', root / 'var/lib/button-box-update')
    state = api.validate_state(api.document(root, STATE + '/state.json'))
    proof = verify.verify_retained_controller(root / source, root / 'etc/button-box-update/release-keys',
                                              application_version=application['version'], platform=platform,
                                              floor=state['accepted']['sequence'])
    if proof['descriptor_sha256'] != expected:
        raise PromotionError('candidate_changed')
    admitted(proof, state['accepted'])
    return proof


def promote(context, *, run=subprocess.run, exchange=exchange_at, step=checkpoint,
            handshake=controller_handshake, monotonic=time.monotonic):
    root, source, platform = Path(context['root']), context['release'], context['platform']
    api = runtime(root)
    journal = None
    # Canonical order for admission and both completion paths. Activation releases
    # all these locks. Durable inhibit+journal exclude every install entrant.
    with api.locks(root), mode_locks(api, root):
        prior = api.document(root, JOURNAL)
        if prior.get('kind') == 'controller' and prior.get('phase') not in TERMINAL:
            raise PromotionError('promotion_recovery_required')
        state = api.validate_state(api.document(root, STATE + '/state.json'))
        verify, trust = api.anchors(root)
        trust.recover_trust(root / 'etc/button-box-update', root / 'var/lib/button-box-update')
        application = committed_application(api, root, source)
        proof = verify.verify_retained_controller(root / source, root / 'etc/button-box-update/release-keys',
                                                 application_version=application['version'], platform=platform)
        admitted(proof, state['accepted'])
        signed_helper = next(entry for entry in proof['descriptor']['recovery']['files']
                             if entry['path'] == 'messagebox/recovery_helper.py')
        if (context.get('helper_sha256') != signed_helper['sha256']
                or context.get('descriptor_sha256') != proof['descriptor_sha256']):
            raise PromotionError('dispatched_candidate_changed')
        if (prior.get('kind') == 'controller' and prior.get('phase') == 'restored'
                and prior.get('descriptor_sha256') == proof['descriptor_sha256']):
            raise PromotionError('controller_quarantined')
        if prior.get('kind') == 'controller' and prior.get('phase') == 'committed':
            old = prior['new']
        elif prior.get('kind') == 'controller' and prior.get('phase') in {'restored', 'unsupported', 'aborted'}:
            old = prior['old']
        else:
            api.legacy_valid(root, state)
            files = state['legacy']['files']
            old = identity(api, root, ACTIVE, version='1.1.0', digest=api.sha(encoded(files)))
        if not matches(api, root, ACTIVE, old):
            raise PromotionError('active_controller_changed')
        if old['digest'] == proof['descriptor_sha256']:
            return {'state': 'already_committed'}
        services = states(run)
        units = integration(api, root, proof)
        state, contents = helper_bundle(api, root, source, proof, state, verify, step)
        transaction = uuid.uuid4().hex
        stage = 'usr/lib/messagebox-update-stage-' + transaction
        metadata = encoded({'schema': 1, 'version': proof['descriptor']['controller_version'],
                            'descriptor_sha256': proof['descriptor_sha256']})
        new = {'version': proof['descriptor']['controller_version'], 'digest': proof['descriptor_sha256'],
               'files': [{'path': Path(entry['path']).name, 'size': entry['size'], 'sha256': entry['sha256']}
                         for entry in proof['descriptor']['modules']], 'metadata': api.sha(metadata)}
        journal = {'schema': 1, 'kind': 'controller', 'phase': 'intent', 'transaction': transaction,
                   'old': old, 'new': new, 'stage': stage, 'application': application,
                   'helper_protocol': 1, 'descriptor_sha256': proof['descriptor_sha256'],
                   'signing_key_id': proof['signing_key_id'], 'sequence': proof['descriptor']['controller_sequence'],
                   'units': units, 'services': services, 'attempt': 1,
                   'deadline_mono': monotonic() + 90, 'rollout_policy_revision': None}
        record(api, root, journal, 'intent', step)
        api.write(root, INHIBIT, encoded({'transaction': transaction}), mode=0o644, step=step)
        try:
            quiesce(run, step)
            for entry in proof['descriptor']['modules']:
                api.write(root, stage + '/' + Path(entry['path']).name, contents[entry['path']], mode=0o644, step=step)
            api.write(root, stage + '/controller.json', metadata, mode=0o644, step=step)
            flush_directory(api, root, stage, step)
            if not matches(api, root, stage, new):
                raise PromotionError('staged_controller_invalid')
            record(api, root, journal, 'staged', step)
            # Recheck captured source proof under current trust immediately before exchange.
            reverify(api, root, source, proof['descriptor_sha256'], application, platform)
            record(api, root, journal, 'exchange_intent', step)
            swap(api, root, journal, exchange, step)
            record(api, root, journal, 'exchanged', step)
            for entry in units:
                api.write(root, entry['path'], base64.b64decode(entry['new']), mode=entry['mode'], step=step)
                step('integration_write', entry['path'])
            call(run, ['systemctl', 'daemon-reload'])
            step('daemon_reload', 'systemd')
        except ExchangeUnavailable:
            record(api, root, journal, 'unsupported', step)
        except Exception:
            # A power cut is injected as BaseException and intentionally bypasses
            # this live cleanup; the permanent boot launcher then reconciles it.
            record(api, root, journal, 'restore_intent', step)
    if journal['phase'] == 'exchanged':
        try:
            activate(api, root, journal, run, handshake, monotonic, step)
            # Reacquire in canonical order, never update/trust while holding guard.
            with api.locks(root), mode_locks(api, root):
                current = api.document(root, JOURNAL)
                if current['transaction'] != journal['transaction'] or current['phase'] != 'exchanged':
                    raise PromotionError('promotion_changed_during_activation')
                if committed_application(api, root, source) != application:
                    raise PromotionError('application_changed')
                reverify(api, root, source, proof['descriptor_sha256'], application, platform)
                if not matches(api, root, ACTIVE, new):
                    raise PromotionError('activation_controller_changed')
                record(api, root, journal, 'activated', step)
                record(api, root, journal, 'committed', step)
                clear_inhibit(api, root, step)
            restore_services(run, services, step)
            return {'state': 'committed', 'version': new['version'], 'transaction': transaction}
        except Exception:
            pass
    with api.locks(root), mode_locks(api, root):
        current = api.document(root, JOURNAL)
        if current['transaction'] != journal['transaction']:
            raise PromotionError('promotion_transaction_changed')
        reconcile(api, root, current, run=run, exchange=exchange, step=step, boot=True)
    # Jobs may now start: all consumer locks have been released.
    restore_services(run, journal['services'], step)
    outcome = current['phase']
    return {'state': outcome, 'version': new['version'] if outcome == 'committed' else old['version'],
            'transaction': transaction}
