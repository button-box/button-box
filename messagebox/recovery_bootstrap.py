"""Prepare independent recovery after a signed application has committed.

PR 6 will schedule this entry point over the air. Do not run the old attended
update-bootstrap.sh on a converted box. This module never exchanges controllers.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from messagebox import recovery_launcher as recovery

RECOVERY_DROPIN = 'etc/systemd/system/messagebox-update-recovery.service.d/messagebox.conf'
SUPERVISOR_DROPIN = 'etc/systemd/system/messagebox-update-supervisor.service.d/messagebox.conf'
LAUNCH = '/usr/bin/python3 -I /usr/lib/messagebox-recovery/launcher.py'
# Exact six-module baseline shared by the four supported field releases.
# Unknown installed controllers must not be silently enrolled as known-good.
LEGACY_HASHES = {'bounded_update.py': '49c285b51f745a6894ee2533df516d681e9e9992fb72c84d5017b0cb799b8425',
 'health-probe.py': 'e9079c51a8c92e495012b516ba84357a8a221d759dcedeb94d58c970a29b324a',
 'release_verify.py': 'e1aa79e5564230e0503850f6a5c1540f71a70ef17910c83a253fcddc0781a9b2',
 'trust_update.py': '99502b79d0663c3d07e2a549c3c21d18cbaa31b28363b51f2f58de2d4878ca62',
 'update_controller.py': 'd9aac78e61ba538f847ff3145443dcae2b752fc3ba1b9693d51fbfafa6321a3f',
 'update_discovery.py': 'f1dbd4e0d5eaad0fe8a99e58c9b9e31e2407868dd0f09be96acc9f730685f540'}
DROPINS = {
    RECOVERY_DROPIN: ('[Service]\nExecStartPre=' + LAUNCH + '\n').encode(),
    SUPERVISOR_DROPIN: ('[Service]\nExecStartPre=' + LAUNCH + '\n').encode(),
}


def json_read(root, path, mode=0o600):
    limit = 2 * 1024 * 1024 if path.endswith('/button-box-update/journal.json') else 16384
    return json.loads(recovery.read(root, path, limit, mode=mode))


def application(root):
    with recovery.directory(root, 'opt') as parent:
        info = os.stat('messagebox', dir_fd=parent, follow_symlinks=False)
        if info.st_uid != os.geteuid():
            raise recovery.RecoveryError('unsafe_application')
        target = os.readlink('messagebox', dir_fd=parent)
    if not re.fullmatch(r'/opt/messagebox-releases/[A-Za-z0-9._-]+-[0-9a-f]{40}', target):
        raise recovery.RecoveryError('unsafe_application')
    relative = target.lstrip('/')
    journal = json_read(root, 'var/lib/button-box-update/journal.json')
    known = json_read(root, 'var/lib/button-box-update/known-good.json')
    identity = json_read(root, relative + '/release.json', 0o644)
    if (journal.get('kind') != 'application' or journal.get('phase') != 'committed'
            or journal.get('new') != target or known.get('current') != target
            or journal.get('version') != identity.get('version')
            or journal.get('commit') != identity.get('commit')):
        raise recovery.RecoveryError('application_not_committed')
    return relative, identity


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise recovery.RecoveryError('invalid_controller_version')
    return tuple(map(int, value.split('.')))


def admission(proof, accepted=None):
    value = proof['descriptor']
    if (value['schema'] != 2 or value['min_launcher_protocol'] != 1
            or value['min_helper_protocol'] != 1 or value['recovery']['protocol'] != 1):
        raise recovery.RecoveryError('unsupported_recovery_protocol')
    current = {'sequence': value['controller_sequence'], 'digest': proof['descriptor_sha256'],
               'controller_version': value['controller_version'],
               'helper_version': value['recovery']['helper_version']}
    if accepted:
        if (current['sequence'] < accepted['sequence']
                or (current['sequence'] == accepted['sequence'] and current['digest'] != accepted['digest'])
                or version(current['controller_version']) < version(accepted['controller_version'])
                or version(current['helper_version']) < version(accepted['helper_version'])):
            raise recovery.RecoveryError('controller_replay')
    return current


def initial_verifier(root, release):
    # The old worker authenticated and committed this application. That signed
    # application is the initial root authority for the permanent verifier.
    raw = recovery.read(root, release + '/updater/release_verify.py', mode=0o644)
    spec = importlib.util.spec_from_loader('bootstrap_release_verify', loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(raw, '<installed-bootstrap-verifier>', 'exec'), module.__dict__)
    previous = sys.modules.get('release_verify')
    try:
        sys.modules['release_verify'] = module
        raw_trust = recovery.read(root, release + '/updater/trust_update.py', mode=0o644)
        spec = importlib.util.spec_from_loader('bootstrap_trust_update', loader=None)
        trust = importlib.util.module_from_spec(spec)
        exec(compile(raw_trust, '<installed-bootstrap-trust>', 'exec'), trust.__dict__)
    finally:
        if previous is None:
            sys.modules.pop('release_verify', None)
        else:
            sys.modules['release_verify'] = previous
    return module, trust


def fixed(root, relative, raw, step):
    try:
        existing = recovery.read(root, relative, mode=0o644)
    except FileNotFoundError:
        recovery.write(root, relative, raw, mode=0o644, step=step)
    else:
        if existing != raw:
            raise recovery.RecoveryError('permanent_recovery_changed')


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
        if (values.get('LoadState') != 'loaded' or commands(values.get('ExecStartPre', '')) != [LAUNCH]
                or commands(values.get('ExecStart', '')) != ['/usr/bin/python3 ' + start]):
            raise recovery.RecoveryError('recovery_unit_mismatch')
        if unit.endswith('recovery.service'):
            if not {'messagebox.target', 'comitup.service', 'messagebox-update-controller.service'} <= set(values.get('Before', '').split()):
                raise recovery.RecoveryError('recovery_order_mismatch')
        elif 'messagebox-update-recovery.service' not in values.get('After', '').split():
            raise recovery.RecoveryError('recovery_order_mismatch')
    result = run(['systemctl', 'is-enabled', 'messagebox-update-recovery.service'],
                 check=True, capture_output=True, text=True, timeout=15)
    if result.stdout.strip() != 'enabled':
        raise recovery.RecoveryError('recovery_not_enabled')


def bootstrap(root=Path('/'), *, platform=None, run=subprocess.run, step=recovery.checkpoint):
    root = Path(root)
    # State/lock directories are outside the old application rollback journal.
    with recovery.directory(root, recovery.STATE, create=True, step=step):
        pass
    lock_path = recovery.STATE + '/promotion.lock'
    try:
        recovery.read(root, lock_path, 1, mode=0o600)
    except FileNotFoundError:
        recovery.write(root, lock_path, b'', step=step)
    with recovery.locks(root):
        relative, identity = application(root)
        verify, initial_trust = initial_verifier(root, relative)
        # Recover key replacement with the installed trust implementation before
        # accepting a descriptor. Never verify against an old key generation.
        try:
            verify_anchor, trust = recovery.anchors(root)
        except FileNotFoundError:
            verify_anchor, trust = verify, initial_trust
        trust.recover_trust(root / recovery.TRUST, root / 'var/lib/button-box-update')
        proof = verify_anchor.verify_retained_controller(
            root / relative, root / recovery.TRUST / 'release-keys', application_version=identity['version'],
            platform=platform or verify_anchor.local_platform())
        try:
            state = recovery.validate_state(recovery.document(root, recovery.STATE + '/state.json'))
        except FileNotFoundError:
            state = None
        accepted = admission(proof, state['accepted'] if state else None)
        if state and accepted != state['accepted']:
            raise recovery.RecoveryError('bootstrap_already_initialized')
        value = proof['descriptor']
        expected_units = [{'path': '/' + path, 'size': len(raw), 'mode': 0o644, 'sha256': recovery.sha(raw)}
                          for path, raw in sorted(DROPINS.items())]
        if value['unit_changes'] != expected_units:
            raise recovery.RecoveryError('recovery_units_not_authorized')
        # Capture every verified byte before publishing any support entry point.
        contents = {}
        for entry in value['modules'] + value['recovery']['files']:
            raw = recovery.read(root, relative + '/' + entry['path'], mode=entry['mode'])
            if recovery.sha(raw) != entry['sha256'] or len(raw) != entry['size']:
                raise recovery.RecoveryError('bootstrap_source_changed')
            contents[entry['path']] = raw
        raw_descriptor = recovery.read(root, relative + '/' + verify.CONTROLLER_DESCRIPTOR, verify.MAX_DESCRIPTOR, mode=0o644)
        if recovery.sha(raw_descriptor) != accepted['digest']:
            raise recovery.RecoveryError('bootstrap_source_changed')
        signature = recovery.read(root, relative + '/' + verify.CONTROLLER_SIGNATURE, verify.MAX_CONTROLLER_SIGNATURE, mode=0o644)
        # A changed signature is authenticated again before it is retained.
        verify_anchor.verify_controller_proof(raw_descriptor, signature, root / recovery.TRUST / 'release-keys')
        if state is None:
            legacy = []
            for name in recovery.MODULES:
                raw = recovery.read(root, recovery.ACTIVE + '/' + name, mode=0o644)
                if recovery.sha(raw) != LEGACY_HASHES[name]:
                    raise recovery.RecoveryError('unknown_field_controller')
                legacy.append({'path': name, 'size': len(raw), 'sha256': recovery.sha(raw)})
                recovery.write(root, recovery.STATE + '/controllers/legacy/' + name, raw, mode=0o644, step=step)
            slots = {}
            for role in ('current', 'fallback'):
                base = recovery.STATE + '/helpers/' + accepted['digest'] + '/' + role
                for path, raw in contents.items():
                    recovery.write(root, base + '/' + path, raw, mode=0o644, step=step)
                recovery.write(root, base + '/' + verify.CONTROLLER_DESCRIPTOR, raw_descriptor, mode=0o644, step=step)
                recovery.write(root, base + '/' + verify.CONTROLLER_SIGNATURE, signature, mode=0o644, step=step)
                slots[role] = {'directory': base, 'digest': accepted['digest']}
            for name in ('release_verify.py', 'trust_update.py'):
                fixed(root, recovery.SUPPORT + '/' + name, contents['updater/' + name], step)
            fixed(root, recovery.SUPPORT + '/launcher.py', contents['messagebox/recovery_launcher.py'], step)
            anchor_record = {name: recovery.sha(contents['updater/' + name]) for name in ('release_verify.py', 'trust_update.py')}
            try:
                if recovery.document(root, recovery.SUPPORT + '/anchors.json') != anchor_record:
                    raise recovery.RecoveryError('permanent_recovery_changed')
            except FileNotFoundError:
                recovery.save(root, recovery.SUPPORT + '/anchors.json', anchor_record, step=step)
            state = {'schema': 1, 'accepted': accepted, 'helpers': slots,
                     'legacy': {'directory': recovery.STATE + '/controllers/legacy', 'files': legacy},
                     'application': {'path': relative, **identity}}
            # One atomic durable write publishes both selection and replay floor.
            recovery.save(root, recovery.STATE + '/state.json', state, step=step)
        recovery.legacy_valid(root, state)
        try:
            journal = recovery.document(root, recovery.STATE + '/journal.json')
        except FileNotFoundError:
            journal = None
        if journal and journal not in ({'schema': 1, 'kind': 'bootstrap', 'phase': phase} for phase in ('prepared', 'armed')):
            raise recovery.RecoveryError('unsupported_recovery_journal')
        if journal is None:
            recovery.save(root, recovery.STATE + '/journal.json', {'schema': 1, 'kind': 'bootstrap', 'phase': 'prepared'}, step=step)
        for slot in state['helpers'].values():
            checked = {**state, 'helpers': {'current': slot, 'fallback': slot}}
            if recovery.select_helper(root, checked, verify_anchor)[0] != 'current':
                raise recovery.RecoveryError('recovery_helper_unavailable')
        # Support and both independent helper copies are durable before these
        # hooks are visible. The existing enabled recovery service is preserved.
        for path, raw in DROPINS.items():
            fixed(root, path, raw, step)
        run(['systemctl', 'daemon-reload'], check=True, timeout=30)
        step('daemon_reload', 'systemd')
        effective_units(run)
        step('effective_units', 'systemd')
        recovery.save(root, recovery.STATE + '/journal.json', {'schema': 1, 'kind': 'bootstrap', 'phase': 'armed'}, step=step)
    return state
