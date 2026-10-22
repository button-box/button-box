"""First A/B installation and authenticated inactive-slot publication.

The frozen selector owns selection and GOOD publication. This code runs only
from a bounded root systemd operation, after application commit and worker exit.
"""
import contextlib
import fcntl
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'updater'))
if not (Path(__file__).resolve().parents[1] / 'updater').is_dir():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/install'))
import release_verify as verify  # noqa: E402
import trust_update as trust  # noqa: E402

STATE = 'var/lib/button-box-controller'
SELECTOR = 'usr/lib/messagebox-selector/selector.py'
ROUTE = '90-controller-slot.conf'
LEGACY = {'bounded_update.py': '49c285b51f745a6894ee2533df516d681e9e9992fb72c84d5017b0cb799b8425', 'health-probe.py': 'e9079c51a8c92e495012b516ba84357a8a221d759dcedeb94d58c970a29b324a', 'release_verify.py': 'e1aa79e5564230e0503850f6a5c1540f71a70ef17910c83a253fcddc0781a9b2', 'trust_update.py': '99502b79d0663c3d07e2a549c3c21d18cbaa31b28363b51f2f58de2d4878ca62', 'update_controller.py': 'd9aac78e61ba538f847ff3145443dcae2b752fc3ba1b9693d51fbfafa6321a3f', 'update_discovery.py': 'f1dbd4e0d5eaad0fe8a99e58c9b9e31e2407868dd0f09be96acc9f730685f540'}


def selector(path):
    spec = importlib.util.spec_from_file_location('frozen_selector', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def routes():
    commands = {'recovery': 'boot', 'controller': 'controller', 'discovery': 'discovery',
                'install': 'install', 'supervisor': 'supervisor'}
    return {'etc/systemd/system/messagebox-update-' + name + '.service.d/' + ROUTE:
            ('[Service]\nExecStart=\nExecStart=/usr/bin/python3 -I /' + SELECTOR + ' ' + command + ('\nTimeoutStartSec=400\n' if name == 'recovery' else '\n')).encode()
            for name, command in commands.items()}


def effective_routes(root, api, run):
    for path, raw in routes().items():
        if api.read(root, path) != raw:
            raise ValueError('routing incomplete')
        unit = Path(path).parent.name.removesuffix('.d')
        expected = raw.decode().split('ExecStart=')[-1].splitlines()[0]
        result = api.call(run, ['systemctl', 'show', unit, '--property=ExecStart']).stdout
        if re.findall(r'argv\[\]=(.*?)\s*;', result) != [expected]:
            raise ValueError('effective routing differs')


@contextlib.contextmanager
def admission_lock(root):
    # Match the installed worker's trust/update lock order. Supervision never
    # takes these locks before inspecting its worker deadline.
    with trust.trust_lock(Path(root) / 'var/lib/button-box-update'):
        path = Path(root) / 'run/lock/messagebox-bounded-update.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)


def application(root, api):
    target = os.readlink(Path(root) / 'opt/messagebox')
    if not target.startswith('/opt/messagebox-releases/') or '..' in Path(target).parts:
        raise ValueError('unsafe application pointer')
    relative = target.lstrip('/')
    journal = api.document(api.read(root, 'var/lib/button-box-update/journal.json', 1024 * 1024, 0o600))
    identity = api.document(api.read(root, relative + '/release.json'))
    known = api.document(api.read(root, 'var/lib/button-box-update/known-good.json', mode=0o600))
    if (journal.get('phase') != 'committed' or journal.get('kind') != 'application'
            or journal.get('new') != target or known.get('current') != target
            or journal.get('commit') != identity['commit'] or journal.get('version') != identity['version']):
        raise ValueError('application not committed')
    return relative, identity


def identity(api, root, slot, *, generation, version, descriptor, legacy):
    return {'generation': generation, 'version': version, 'descriptor': descriptor, 'legacy': legacy,
            'state_format': 1, 'files': {n: [len(raw), api.digest(raw)] for n in api.MODULES
                                      for raw in [api.read(root, slot + '/' + n, 2 * 1024 * 1024)]}}


def bootstrap(root=Path('/'), *, run=subprocess.run, step=lambda event, path: None):
    root = Path(root)
    source = Path(__file__).with_name('controller_selector.py')
    initial = selector(source)
    # No command entry point changes before the exact selector and A's GOOD
    # have reached stable storage. Re-entry never replaces frozen bytes.
    initial.mkdir(root / STATE, step)
    with initial.lock(root, 'operation', blocking=False), admission_lock(root):
        application(root, initial)
        raw = source.read_bytes()
        try:
            if initial.read(root, SELECTOR, 65536) != raw:
                raise ValueError('frozen selector changed')
        except FileNotFoundError:
            initial.write(root / SELECTOR, raw, step=step)
        api = selector(root / SELECTOR)
        if not api.valid(root):
            for name, expected in LEGACY.items():
                if api.digest(api.read(root, api.SLOTS[0] + '/' + name, 1024 * 1024)) != expected:
                    raise ValueError('unknown initial controller')
            record = identity(api, root, api.SLOTS[0], generation=0, version='1.1.0', descriptor='', legacy=True)
            api.write(root / api.SLOTS[0] / 'GOOD', api.envelope(record), step=step)
        with api.lock(root, 'gate'):
            if (not any((root / p).exists() for p in routes())
                    and len(api.valid(root)) == 1 and api.valid(root)[0][1]['legacy']):
                api.choose(root, *api.valid(root)[0])
        for path, content in routes().items():
            try:
                old = api.read(root, path)
            except FileNotFoundError:
                old = None
            if old != content:
                api.write(root / path, content, step=step)
        api.call(run, ['systemctl', 'daemon-reload'])
        effective_routes(root, api, run)
        api.call(run, ['systemctl', 'enable', '--now', 'messagebox-update-selector.timer'])


def compatible(root, slot, api, *, run=subprocess.run):
    # Execute each controller's real journal parser in an isolated read-only
    # process. Version 1 is the shared format; no migration is permitted here.
    program = ("import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
               "import bounded_update as u;root=Path(sys.argv[2]);"
               "j=u.read_state(root,'journal.json');u._validate_journal(root,j);"
               "[u.read_state(root,n) for n in ('known-good.json','result.json','available.json','quarantine.json','active.json')];"
               "import update_controller as c;c.read=lambda n,d=None:u.read_state(root,n,d);c.policy()")
    run([sys.executable, '-I', '-B', '-X', 'pycache_prefix=' + str(Path(root) / api.RUN / 'unused'), '-c', program, str(Path(root) / slot), str(root)],
        check=True, capture_output=True, timeout=10)


def publish(root=Path('/'), *, platform=None, run=subprocess.run, step=lambda event, path: None,
            trial_options=None):
    root = Path(root)
    api = selector(root / SELECTOR)
    with api.lock(root, 'operation', blocking=False), admission_lock(root):
        release, app = application(root, api)
        effective_routes(root, api, run)
        api.call(run, ['systemctl', 'is-active', '--quiet', 'messagebox-update-selector.timer'])
        trust.recover_trust(root / 'etc/button-box-update', root / 'var/lib/button-box-update')
        proof = verify.verify_retained_controller(root / release, root / 'etc/button-box-update/release-keys',
                application_version=app['version'], platform=platform or verify.local_platform())
        value, digest = proof['descriptor'], proof['descriptor_sha256']
        try:
            record = api.document(api.read(root, STATE + '/floor.json', mode=0o600))
            floor = record['value']
            if record['sha256'] != api.digest(api.canonical(floor)):
                raise ValueError('damaged admission floor')
            if (value['controller_sequence'] < floor['sequence']
                    or value['controller_sequence'] == floor['sequence'] and digest != floor['digest']
                    or verify._controller_version(value['controller_version']) < verify._controller_version(floor['version'])):
                raise ValueError('controller replay or downgrade')
        except FileNotFoundError:
            if any(not v['legacy'] for _, v in api.valid(root)):
                raise ValueError('admission floor missing')
        current = api.selected(root)
        if current['trial']:
            raise ValueError('trial already active')
        working = api.inspect(root, current['slot'])
        if verify._controller_version(value['controller_version']) < verify._controller_version(working['version']):
            raise ValueError('controller downgrade')
        if working['descriptor'] == digest:
            return working  # Rechecking accepted proof must not consume the spare.
        compatible(root, current['slot'], api, run=run)
        space = os.statvfs(root / 'usr/lib')
        required = sum(entry['size'] for entry in value['modules']) + 32 * 1024 * 1024
        if space.f_bavail * space.f_frsize < required or space.f_favail < 128:
            raise ValueError('controller capacity')
        inactive = next(s for s in api.SLOTS if s != current['slot'])
        try:
            tried = api.document(api.read(root, inactive + '/TRIED'))
            if tried['descriptor'] == digest:
                raise ValueError('trial already consumed')
        except FileNotFoundError:
            pass
        api.mkdir(root / inactive, step)
        (root / inactive / 'GOOD').unlink(missing_ok=True)
        step('unlink_GOOD', inactive)
        api.flush(root / inactive, step)
        for entry in value['modules']:
            raw, mode = verify.controller_file(root / release, entry['path'], verify.MAX_CONTROLLER_MODULE)
            if len(raw) != entry['size'] or api.digest(raw) != entry['sha256']:
                raise ValueError('candidate changed')
            api.write(root / inactive / Path(entry['path']).name, raw, mode=mode, step=step)
        for original, name in ((verify.CONTROLLER_DESCRIPTOR, 'controller_descriptor.py'),
                               (verify.CONTROLLER_SIGNATURE, 'controller_signature.py')):
            raw, _ = verify.controller_file(root / release, original, verify.MAX_DESCRIPTOR)
            if original == verify.CONTROLLER_DESCRIPTOR and api.digest(raw) != digest:
                raise ValueError('retained descriptor changed')
            api.write(root / inactive / name, raw, step=step)
        candidate = identity(api, root, inactive, generation=value['controller_sequence'],
                             version=value['controller_version'], descriptor=digest, legacy=False)
        api.inspect(root, inactive, candidate)
        compatible(root, inactive, api, run=run)
        api.write(root / STATE / 'floor.json', api.envelope({'sequence': value['controller_sequence'],
                  'digest': digest, 'version': value['controller_version']}), mode=0o600, step=step)
        api.write(root / inactive / 'TRIED', api.canonical({'descriptor': digest}), step=step)
        # The selector, not a candidate result string, observes health and owns
        # GOOD publication. The unchanged working slot is still usable on cuts.
        api.trial(root, inactive, candidate, app, run=run, step=step, **(trial_options or {}))
        return candidate


def main():
    command = sys.argv[1]
    if command == 'bootstrap':
        try:
            bootstrap()
        except BlockingIOError:
            pass  # Supervisor queues another attempt after the worker exits.
    elif command == 'promote':
        subprocess.run(['systemd-run', '--quiet', '--wait', '--collect', '--service-type=oneshot',
            '--unit=messagebox-update-slot', '-p', 'TimeoutStartSec=180', '-p', 'TimeoutStopSec=10',
            '-p', 'KillMode=control-group', '-p', 'UMask=0022', '-p', 'StartLimitIntervalSec=0', '--',
            '/usr/bin/python3', '-B', str(Path(__file__).resolve()), 'publish'], check=True, timeout=200)
    elif command == 'publish':
        if not os.environ.get('INVOCATION_ID'):
            raise ValueError('publication requires bounded systemd operation')
        publish()


if __name__ == '__main__':
    main()
