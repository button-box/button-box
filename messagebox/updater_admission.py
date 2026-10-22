"""Root permit admission at the frozen mapper boundary. No root network calls."""
import ast
import base64
import hashlib
import importlib.util
import json
import os
import pwd
import re
import secrets
import stat
import subprocess
import time
from pathlib import Path

MODULES = ('bounded_update.py', 'health-probe.py', 'release_verify.py', 'trust_update.py',
           'update_controller.py', 'update_discovery.py')
STATE = 'var/lib/button-box-update/admission.json'
RUN = 'run/messagebox-updater-admission'
UNIT = 'messagebox-updater-admission.service'
DOMAIN = 'buttonbox-updater-permit-v1'
SHA = re.compile(r'[0-9a-f]{64}\Z')
ID = re.compile(r'[A-Za-z0-9_-]{16,128}\Z')
REQUEST_FIELDS = {'protocol', 'purpose', 'nonce', 'channel', 'release_sequence',
    'release_manifest_sha256', 'release_archive_sha256', 'controller_descriptor_sha256', 'target_controller_set_sha256'}
PAYLOAD_FIELDS = (REQUEST_FIELDS - {'protocol'}) | {'schema', 'domain', 'action', 'key_id', 'box_id',
    'device_id', 'account_scope', 'source_controller_set_sha256', 'policy_revision', 'issued_at', 'expires_at'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii') + b'\n'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def document(raw):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError('duplicate_key')
            result[name] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('number')))


def read(path, limit=65536, *, owner=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022
                or info.st_uid != (os.geteuid() if owner is None else owner) or info.st_size > limit):
            raise ValueError('unsafe_admission_file')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('admission_file_limit')
        return raw
    finally:
        os.close(fd)


def load(path):
    spec = importlib.util.spec_from_file_location('bridge_local', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cloud(root):
    lines = [line.strip() for line in read(Path(root) / 'etc/messagebox/env').decode().splitlines()]
    values = [line.split('=', 1)[1].strip().strip('\"\'') for line in lines if line.startswith('MSGBOX_TRANSPORT=')]
    if len(values) > 1 or values and values[0] not in {'cloud', 'wacli'}:
        raise ValueError('invalid_transport')
    return values == ['cloud']


def set_hash(directory):
    return digest(canonical([{'path': name, 'size': len(raw), 'sha256': digest(raw)}
        for name in MODULES for raw in [read(Path(directory) / name, 2 * 1024 * 1024)]]))


def identity_set_hash(identity):
    return digest(canonical([{'path': n, 'size': identity['files'][n][0], 'sha256': identity['files'][n][1]}
                             for n in MODULES]))


def active_path(root):
    path = root / 'usr/lib/messagebox-selector/selector.py'
    return root / load(path).selected(root)['slot'] if path.exists() else root / 'usr/lib/messagebox-update'


def rollout_keys(path):
    value = ast.literal_eval(read(path, 4096).decode('ascii'))
    if not isinstance(value, dict) or set(value) != {'schema', 'keys'} or type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('invalid_rollout_pins')
    if not isinstance(value['keys'], list) or len(value['keys']) > 8:
        raise ValueError('invalid_rollout_pins')
    keys = {}
    for line in value['keys']:
        raw = base64.b64decode(line, validate=True)
        if len(raw) != 42 or raw[:2] != b'Ed' or base64.b64encode(raw).decode() != line:
            raise ValueError('invalid_rollout_pin')
        key_id = raw[2:10][::-1].hex().upper()
        if key_id in keys:
            raise ValueError('duplicate_rollout_pin')
        keys[key_id] = line
    return keys


def subject(root):
    owner = pwd.getpwnam('messagebox').pw_uid if Path(root) == Path('/') else os.geteuid()
    identity = document(read(root / 'var/lib/messagebox-cloud/device.json', 4096, owner=owner))
    snapshot = document(read(root / 'var/lib/messagebox-cloud/runtime.json', 512 * 1024, owner=owner))['snapshot']
    value = {'device_id': identity['device_id'], 'box_id': snapshot['box_id'], 'account_scope': snapshot['account_scope']}
    if not all(isinstance(value[n], str) and ID.fullmatch(value[n]) for n in ('device_id', 'box_id')) or not SHA.fullmatch(value['account_scope']):
        raise ValueError('invalid_permit_subject')
    return value


def verify_permit(envelope, expected, keys, directory, *, now, run=subprocess.run):
    if (len(envelope) > 8192 or not keys):
        raise ValueError('permit_unavailable')
    value = document(envelope)
    if (not isinstance(value, dict) or set(value) != {'schema', 'key_id', 'payload_b64', 'signature_minisign'}
            or type(value['schema']) is not int or value['schema'] != 1 or value['key_id'] not in keys):
        raise ValueError('invalid_permit')
    raw = base64.b64decode(value['payload_b64'], validate=True)
    signature = value['signature_minisign'].encode('ascii')
    if len(raw) > 4096 or len(signature) > 512:
        raise ValueError('permit_limit')
    payload = document(raw)
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_FIELDS or canonical(payload) != raw:
        raise ValueError('noncanonical_permit')
    if (payload['schema'] != 1 or type(payload['schema']) is not int or payload['domain'] != DOMAIN
            or payload['action'] != 'permit' or payload['key_id'] != value['key_id']
            or any(payload.get(n) != v or type(payload.get(n)) is not type(v) for n,v in expected.items())
            or type(payload['policy_revision']) is not int or not 0 <= payload['policy_revision'] <= 2**53-1
            or any(type(payload[n]) is not int or not 1_700_000_000 <= payload[n] < 4_102_444_800 for n in ('issued_at','expires_at'))
            or not payload['issued_at'] <= now < payload['expires_at'] or not 0 < payload['expires_at']-payload['issued_at'] <= 300):
        raise ValueError('permit_binding')
    lines = signature.decode('ascii').splitlines()
    packet = base64.b64decode(lines[1], validate=True) if len(lines) == 4 else b''
    if (len(packet) != 74 or packet[:2] != b'Ed' or packet[2:10][::-1].hex().upper() != value['key_id']
            or lines[2] != 'trusted comment: ' + DOMAIN):
        raise ValueError('permit_signature_format')
    for name, content in (('permit',raw), ('signature',signature), ('key.pub',
            ('untrusted comment: rollout pin\n' + keys[value['key_id']] + '\n').encode())):
        (directory / name).write_bytes(content)
    result = run(['minisign', '-V', '-m', str(directory / 'permit'), '-x', str(directory / 'signature'),
                  '-p', str(directory / 'key.pub')], capture_output=True, timeout=5, check=False)
    if result.returncode:
        raise ValueError('bad_permit_signature')
    return payload


def client(root, source, request, *, run=subprocess.run):
    api = load(source / 'messagebox/controller_selector.py')
    directory = root / RUN
    api.mkdir(directory)
    api.write(directory / 'client.py', read(source / 'messagebox/cloud_updater_permit.py'), durable=False)
    api.write(directory / 'request.json', canonical(request), durable=False)
    output = directory / 'out'
    uid = pwd.getpwnam('messagebox').pw_uid if root == Path('/') else os.geteuid()
    if not output.exists():
        output.mkdir(mode=0o700)
        os.chown(output, uid, -1)
    info = output.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('unsafe_client_output')
    (output / 'response.json').unlink(missing_ok=True)
    command = ['systemd-run', '--quiet', '--wait', '--collect', '--service-type=oneshot', '--unit=' + UNIT]
    for prop in ('User=messagebox', 'Group=messagebox', 'EnvironmentFile=/etc/messagebox/env',
        'TimeoutStartSec=20', 'RuntimeMaxSec=20', 'TimeoutStopSec=3', 'KillMode=control-group',
        'UMask=0077', 'NoNewPrivileges=yes', 'ProtectSystem=strict', 'ProtectHome=yes',
        'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6', 'ReadWritePaths=/' + RUN + '/out'):
        command += ['-p', prop]
    command += ['--', '/usr/bin/python3', '-I', '-B', '/' + RUN + '/client.py']
    try:
        run(command, check=True, timeout=25, capture_output=True)
        return read(output / 'response.json', 8192, owner=uid)
    finally:
        run(['systemctl', 'stop', UNIT], check=True, timeout=5, capture_output=True)


def authorize(root, source, request, operation, source_hash, *, run=subprocess.run, fetch=client, clock=time.time):
    root, source = Path(root), Path(source)
    api = load(source / 'messagebox/controller_selector.py')
    keys = rollout_keys(source / 'messagebox/updater_rollout_keys.py')
    if not keys or run(['timedatectl','show','--property=NTPSynchronized','--value'],
            capture_output=True, text=True, check=True, timeout=3).stdout.strip() != 'yes':
        raise ValueError('permit_unavailable')
    who = subject(root)
    request = {**request, 'protocol': 1, 'nonce': secrets.token_hex(16)}
    if (set(request) != REQUEST_FIELDS or len(canonical(request)) > 1024
            or request['purpose'] not in {'install_bridge','promote_controller'}
            or request['channel'] not in {'stable','canary'}
            or type(request['release_sequence']) is not int or not 1 <= request['release_sequence'] <= 2**53-1
            or any(not isinstance(request[n], str) or not SHA.fullmatch(request[n]) for n in (
                'release_manifest_sha256','release_archive_sha256','controller_descriptor_sha256','target_controller_set_sha256'))):
        raise ValueError('invalid_permit_request')
    observation = report(root, reporter='legacy_admission', run=run)
    envelope = fetch(root, source, {'updater_permit_request': request,
        **({'updater': observation} if observation is not None else {})}, run=run)
    directory = root / RUN
    api.mkdir(directory)
    expected = {**{n:v for n,v in request.items() if n != 'protocol'}, **who,
                'source_controller_set_sha256': source_hash}
    payload = verify_permit(envelope, expected, keys, directory, now=clock(), run=run)
    if subject(root) != who:
        raise ValueError('permit_subject_changed')
    try:
        accepted = document(api.read(root, STATE, mode=0o600))
    except FileNotFoundError:
        accepted = {'policy_revision': 0, 'operations': {}}
    if (type(accepted['policy_revision']) is not int or payload['policy_revision'] < accepted['policy_revision']
            or any(r['nonce'] == request['nonce'] and (p != request['purpose'] or r['operation'] != operation)
                   for p,r in accepted['operations'].items())):
        raise ValueError('permit_replay')
    accepted['policy_revision'] = payload['policy_revision']
    accepted['operations'][request['purpose']] = {'operation': operation, 'nonce': request['nonce'],
        'request': request, 'source': source_hash, 'expires_at': payload['expires_at']}
    api.write(root / STATE, canonical(accepted), mode=0o600)
    if not payload['issued_at'] <= clock() < payload['expires_at']:
        raise ValueError('permit_expired')
    return payload


def installed_capability(root):
    api = load(root / 'usr/lib/messagebox-selector/selector.py')
    selected = api.selected(root)
    record = api.inspect(root, selected['slot'])
    if selected['trial'] or record['legacy'] or tuple(map(int, record['version'].split('.'))) < (1,3,0):
        raise ValueError('controller_slots_required')


def protect_legacy_writes(worker):
    # The field snapshot flushes its contents but not the new snapshot entry
    # in snapshots/. Add that missing barrier in THIS worker process only.
    if worker.get('_bridge_durable'):
        return
    backup, install = worker['create_backup'], worker['_atomic_install']
    def durable_backup(directory, *args, **kwargs):
        result = backup(directory, *args, **kwargs)
        worker['_sync_tree'](Path(directory))
        return result
    def durable_install(source, destination, *args, **kwargs):
        destination = Path(destination)
        missing, parent = [], destination.parent
        while not parent.exists():
            missing.append(parent.parent)
            parent = parent.parent
        result = install(source, destination, *args, **kwargs)
        with destination.open('rb') as stream:
            os.fsync(stream.fileno())  # The field code chmods after its fsync.
        for parent in missing:
            worker['_fsync_directory'](parent)
        return result
    worker['create_backup'], worker['_atomic_install'] = durable_backup, durable_install
    worker['_bridge_durable'] = True


def legacy_admission(source, root, *, manifest_path=None, run=subprocess.run, fetch=client):
    source, root = Path(source), Path(root)
    manifest_raw = read(manifest_path or source / 'release-manifest.json', 2 * 1024 * 1024)
    manifest = document(manifest_raw)
    if manifest.get('required_updater_capabilities'):
        if manifest['required_updater_capabilities'] != ['controller_self_upgrade_v1']:
            raise ValueError('unsupported_updater_capability')
        installed_capability(root)
    if not cloud(root):
        return  # Local signed-feed verification and physical policy remain in the worker.
    ticket = document(read(root / 'var/lib/button-box-update/ticket.json', 16384))
    if ticket['manifest_sha256'] != digest(manifest_raw):
        raise ValueError('permit_ticket_mismatch')
    policy = document(read(root / 'var/lib/button-box-update/policy.json', 4096))
    bundle = Path(ticket['bundle'])
    if bundle.parent != root / 'var/lib/button-box-update/downloads' or not re.fullmatch(r'[0-9a-f]{32}', bundle.name):
        raise ValueError('unsafe_permit_bundle')
    archive = bundle / ('button-box-' + manifest['version'] + '.tar.gz')
    with archive.open('rb') as stream:
        archive_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    request = {'purpose': 'install_bridge', 'channel': policy['channel'], 'release_sequence': manifest['release_sequence'],
        'release_manifest_sha256': digest(manifest_raw), 'release_archive_sha256': archive_hash,
        'controller_descriptor_sha256': digest(read(source / 'messagebox/controller_descriptor.py', 16384)),
        'target_controller_set_sha256': set_hash(source / 'scripts/install')}
    authorize(root, source, request, ticket['operation_id'], set_hash(active_path(root)), run=run, fetch=fetch)


def admit_promotion(root, source, app, *, run=subprocess.run, fetch=client):
    root, source = Path(root), Path(source)
    if not cloud(root):
        return
    accepted = document(read(root / STATE))['operations']['install_bridge']
    if accepted['request']['release_manifest_sha256'] != app['manifest_sha256']:
        raise ValueError('promotion_application_mismatch')
    api = load(root / 'usr/lib/messagebox-selector/selector.py')
    selected = api.selected(root)
    request = {**accepted['request'], 'purpose': 'promote_controller'}
    request.pop('protocol')
    request.pop('nonce')
    if (request['controller_descriptor_sha256'] != digest(read(source / 'messagebox/controller_descriptor.py', 16384))
            or request['target_controller_set_sha256'] != set_hash(source / 'updater')):
        raise ValueError('promotion_target_mismatch')
    authorize(root, source, request, app['manifest_sha256'], set_hash(root / selected['slot']), run=run, fetch=fetch)


def candidate_identity(root, slot, api):
    raw = api.read(root, slot + '/controller_descriptor.py', 16384)
    if document(api.read(root, slot + '/TRIED')) != {'descriptor': digest(raw)}:
        raise ValueError('unknown_trial_identity')
    source = Path(__file__).resolve().parents[1]
    verifier = source / 'updater/release_verify.py'
    if not verifier.exists():
        verifier = source / 'scripts/install/release_verify.py'
    value = load(verifier).parse_controller_descriptor(raw)
    identity = {'generation': value['controller_sequence'], 'version': value['controller_version'],
        'state_format': value['state_format'], 'descriptor': digest(raw), 'legacy': False,
        'files': {Path(e['path']).name: [e['size'], e['sha256']] for e in value['modules']}}
    return api.inspect(root, slot, identity)


def report(root=Path('/'), *, reporter='runtime', active=None, run=subprocess.run):
    # Schema 1 identifies an installed selector, never a staged bridge. The
    # initial permit instead binds the registry's expected legacy set hash.
    root = Path(root)
    api_path = root / 'usr/lib/messagebox-selector/selector.py'
    if not api_path.exists():
        return None
    if digest(read(api_path, 65536)) != '17c7a181bb39aedfd9c6ed8d89cedf85273d6d7a2966b7e484d302fd68f318e4':
        raise ValueError('unknown_selector')
    api = load(api_path)
    selection = api.read(root, api.RUN + '/selected.json')
    selected = api.selected(root)
    if active is not None and Path(active) != root / selected['slot']:
        return None
    slots, identities = {}, {}
    for name, path in zip(('a','b'), api.SLOTS):
        identity, state = None, 'none'
        try:
            identity, state = api.inspect(root, path), 'GOOD'
        except (OSError, ValueError, KeyError, TypeError):
            try:
                identity, state = candidate_identity(root, path, api), 'TRIED'
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                pass
        if identity:
            identities[name] = identity
        slots[name] = {'slot_id': name, 'generation': identity['generation'] if identity else 0,
            'state': state, 'controller_version': identity['version'] if identity else None,
            'set_sha256': identity_set_hash(identity) if identity else None}
    active_id = ('a','b')[api.SLOTS.index(selected['slot'])]
    identity = api.inspect(root, selected['slot'], selected['identity'])
    current = slots[active_id]
    if (current['state'] == 'none' or current['generation'] != identity['generation']
            or current['controller_version'] != identity['version']):
        return None
    try:
        result = document(api.read(root, 'var/lib/button-box-controller/trial-result.json'))
        last = {n: result[n] for n in ('slot_id','generation','result','reason_code')}
    except FileNotFoundError:
        last = None
    journal = document(read(root / 'var/lib/button-box-update/journal.json', 1024 * 1024))
    target = os.readlink(root / 'opt/messagebox')
    if not target.startswith('/opt/messagebox-releases/') or '..' in Path(target).parts:
        raise ValueError('unsafe_application_pointer')
    app = document(read(root / target.lstrip('/') / 'release.json', 4096))
    application_state = 'unknown'
    try:
        api.recovered(root)
        if journal['phase'] == 'committed':
            application_state = 'committed'
        elif journal['phase'] == 'rolled_back':
            result = document(read(root / 'var/lib/button-box-update/result.json'))
            if (result.get('state') == 'rolled_back' and result.get('operation_id') == journal.get('operation_id')
                    and result.get('software_version') == app['version'] and result.get('software_commit') == app['commit']):
                application_state = 'rolled_back'
    except (OSError, ValueError, KeyError, TypeError):
        if journal.get('phase') == 'rollback_failed':
            result = document(read(root / 'var/lib/button-box-update/result.json'))
            if (result.get('state') == 'rollback_failed' and result.get('operation_id') == journal.get('operation_id')
                    and result.get('software_version') == app['version'] and result.get('software_commit') == app['commit']):
                application_state = 'rollback_failed'
    timeout = run(['systemctl','show','messagebox-update-discovery.service','--property=TimeoutStartUSec','--value'],
                  check=True, text=True, capture_output=True, timeout=3).stdout.strip()
    if not re.fullmatch(r'[0-9]+(?:min|s)', timeout):
        raise ValueError('unknown_discovery_timeout')
    seconds = int(timeout.removesuffix('min').removesuffix('s')) * (60 if timeout.endswith('min') else 1)
    for name, observed in identities.items():
        path = api.SLOTS[('a','b').index(name)]
        checked = api.inspect(root,path) if slots[name]['state'] == 'GOOD' else candidate_identity(root,path,api)
        if checked != observed:
            return None
    if api.read(root, api.RUN + '/selected.json') != selection:
        return None
    value = {'schema': 1, 'reporter': reporter, 'controller_version': identity['version'],
        'selector_version': '1.0.0', 'active_slot': active_id, 'active_generation': identity['generation'],
        'slots': slots, 'last_trial': last, 'discovery_timeout_seconds': seconds,
        'application': {'version': app['version'], 'commit': app['commit'], 'state': application_state},
        'capabilities': {'permit_v1': True, 'controller_self_upgrade_v1': True}}
    return validate_report(value)


def publish_report(root=Path('/'), *, run=subprocess.run):
    # The field broker cannot grow a status method. Use the existing root
    # supervisor hook, not a new timer, and measure the actual running script.
    root = Path(root)
    if not cloud(root):
        return
    command = ['systemctl','show','messagebox-update-controller.service','--property=MainPID','--value']
    pid = run(command, check=True, text=True, capture_output=True, timeout=3).stdout.strip()
    if not pid.isdecimal() or int(pid) <= 1:
        raise ValueError('controller_not_running')
    args = read(root / 'proc' / pid / 'cmdline', 4096).split(b'\0')
    active = next((root / slot for slot in ('usr/lib/messagebox-update','usr/lib/messagebox-update-b')
                   if ('/' + slot + '/update_controller.py').encode() in args), None)
    if active is None or run(command, check=True, text=True, capture_output=True, timeout=3).stdout.strip() != pid:
        raise ValueError('controller_identity_changed')
    observation = report(root, active=active, run=run)
    if observation is None:
        return
    value = {'boot': (root / 'proc/sys/kernel/random/boot_id').read_text().strip(),
        'monotonic': time.monotonic(), 'updater': observation}
    load(Path(__file__).with_name('controller_selector.py')).write(
        root / 'var/lib/button-box-update/updater-status.json', canonical(value))


def cached_report(root=Path('/')):
    root = Path(root)
    value = document(read(root / 'var/lib/button-box-update/updater-status.json', 4608,
                          owner=0 if root == Path('/') else os.geteuid()))
    if (value['boot'] != (root / 'proc/sys/kernel/random/boot_id').read_text().strip()
            or type(value['monotonic']) not in (int,float) or not 0 <= time.monotonic()-value['monotonic'] <= 120):
        raise ValueError('stale_updater_report')
    selected = document(read(root / 'run/messagebox-selector/selected.json', owner=0 if root == Path('/') else os.geteuid()))
    report = value['updater']
    paths = {'a':'usr/lib/messagebox-update','b':'usr/lib/messagebox-update-b'}
    if (selected['slot'] != paths[report['active_slot']]
            or selected['identity']['generation'] != report['active_generation']
            or identity_set_hash(selected['identity']) != report['slots'][report['active_slot']]['set_sha256']):
        raise ValueError('stale_updater_selection')
    return validate_report(value['updater'])


def validate_report(value):
    # Match the schema-1 receiver, including budgets on unknown extensions.
    if (not isinstance(value, dict)
            or len(json.dumps(value,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')) > 4096):
        raise ValueError('updater_report_limit')
    pending, properties = [(value, 1)], 0
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict,list)) and depth > 4:
            raise ValueError('updater_report_depth')
        if isinstance(item, dict):
            properties += len(item)
            pending.extend((v,depth+1) for v in item.values())
        elif isinstance(item, list):
            properties += len(item)
            pending.extend((v,depth+1) for v in item)
    if properties > 48:
        raise ValueError('updater_report_properties')
    def number(n):
        return type(n) is int and 0 <= n <= 2**53-1
    def version(v):
        return isinstance(v,str) and len(v) <= 32 and re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)',v)
    names = ('schema','reporter','controller_version','selector_version','active_slot','active_generation',
             'slots','last_trial','discovery_timeout_seconds','application','capabilities')
    value = {n:value[n] for n in names}
    value['slots'] = {s:{n:value['slots'][s][n] for n in
        ('slot_id','generation','state','controller_version','set_sha256')} for s in ('a','b')}
    value['application'] = {n:value['application'][n] for n in ('version','commit','state')}
    value['capabilities'] = {n:value['capabilities'][n] for n in ('permit_v1','controller_self_upgrade_v1')}
    if value['last_trial'] is not None:
        value['last_trial'] = {n:value['last_trial'][n] for n in ('slot_id','generation','result','reason_code')}
    if (type(value['schema']) is not int or value['schema'] != 1 or value['reporter'] not in {'runtime','legacy_admission'}
            or not version(value['controller_version']) or not version(value['selector_version'])
            or value['active_slot'] not in {'a','b'} or not number(value['active_generation'])
            or type(value['discovery_timeout_seconds']) is not int or not 1 <= value['discovery_timeout_seconds'] <= 3600
            or not isinstance(value['application']['version'],str) or not re.fullmatch(r'[A-Za-z0-9._+-]{1,64}',value['application']['version'])
            or not isinstance(value['application']['commit'],str) or not re.fullmatch(r'[0-9a-f]{7,40}',value['application']['commit'])
            or value['application']['state'] not in {'unknown','committed','rolled_back','rollback_failed'}
            or any(type(v) is not bool for v in value['capabilities'].values())
            or value['capabilities']['controller_self_upgrade_v1'] and not value['capabilities']['permit_v1']):
        raise ValueError('invalid_updater_report')
    for name, slot in value['slots'].items():
        if slot['slot_id'] != name or not number(slot['generation']) or slot['state'] not in {'GOOD','TRIED','none'}:
            raise ValueError('invalid_slot')
        if slot['state'] == 'none':
            if slot['generation'] != 0 or slot['controller_version'] is not None or slot['set_sha256'] is not None:
                raise ValueError('invalid_empty_slot')
        elif not version(slot['controller_version']) or not isinstance(slot['set_sha256'],str) or not SHA.fullmatch(slot['set_sha256']):
            raise ValueError('invalid_slot_identity')
    active, other = value['slots'][value['active_slot']], value['slots']['b' if value['active_slot']=='a' else 'a']
    if (active['state'] == 'none' or active['generation'] != value['active_generation']
            or active['controller_version'] != value['controller_version']
            or active['state'] == 'TRIED' and (other['state'] != 'GOOD' or other['generation'] >= active['generation'])):
        raise ValueError('invalid_active_identity')
    trial = value['last_trial']
    if trial is not None:
        reasons = {'activation_failed','identity_mismatch','recovery_failed','interrupted','permit_denied','timeout','other'}
        if (trial['slot_id'] not in {'a','b'} or not number(trial['generation'])
                or trial['result'] not in {'promoted','failed','rolled_back','none'}
                or trial['result'] in {'promoted','none'} and trial['reason_code'] is not None
                or trial['result'] in {'failed','rolled_back'} and trial['reason_code'] not in reasons):
            raise ValueError('invalid_trial_summary')
        slot = value['slots'][trial['slot_id']]
        if (trial['result'] == 'none' and (trial['slot_id'] != value['active_slot'] or slot['state'] != 'TRIED'
                                         or trial['generation'] != slot['generation'])
                or slot['state'] != 'none' and trial['generation'] > slot['generation']
                or trial['generation'] == slot['generation'] and (
                    trial['result'] == 'promoted' and slot['state'] != 'GOOD'
                    or trial['result'] in {'failed','rolled_back'} and (trial['slot_id'] == value['active_slot'] or slot['state'] == 'GOOD'))):
            raise ValueError('invalid_trial_generation')
    return value
