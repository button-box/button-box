"""Real base capsules -> frozen OTA -> installed bootstrap -> signed full OTA.

Disposable keys only. PID 1, peer credentials, platform, elapsed time, network
heartbeat success and physical health are host boundaries, not Pi acceptance.
"""
import contextlib
import hashlib
import importlib.util
import importlib.machinery
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from field_updater import FieldBox, digest, load_frozen, mkdir, write
from messagebox import cloud_boot_repair as repair, controller_selector as selector, controller_slots as slots
from test_boot_ordering import boot_links, boot_model, ordering_cycle, ordering_graph, shipped_units, unit_settings, with_bootstrap
from test_controller_slots import DurableView, Host, fast_clock
from test_release_tooling import build, release
from test_update_discovery import PLATFORM

ROOT = Path(__file__).resolve().parents[1]
BASES = ('v0.3.9', 'v0.3.10', 'v0.3.12')


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class LegacyHost(Host):
    def probe(self, root, value, run, **kwargs):
        """Use the delivered legacy producers, never the current application.

        Cloud's successful network heartbeat is supplied by the host. Its actual
        installed IPC producer and button idle path talk to the selected broker.
        The broker authors the fresh transaction-bound evidence itself.
        """
        self.probes += 1
        if self.not_ready:
            raise OSError('injected readiness failure')
        installed = root / os.readlink(root / 'opt/messagebox').lstrip('/')
        with mock.patch.dict(sys.modules), contextlib.ExitStack() as stack:
            for name in ('release_verify', 'bounded_update', 'trust_update', 'update_controller'):
                sys.modules[name] = broker = module(name, root / selector.selected(root)['slot'] / (name + '.py'))
            software = module('legacy_software', installed / 'messagebox/software.py')
            stack.enter_context(mock.patch.dict(sys.modules, {'messagebox.software': software}))
            producer = module('legacy_cloud_update', installed / 'messagebox/cloud_update.py')
            app = json.loads((installed / 'release.json').read_bytes())
            socket_path = root.parent / 'bridge-ready.sock'
            for target, name, replacement in (
                (software, 'RELEASE_FILE', installed / 'release.json'),
                (producer, 'SOCKET', str(socket_path)),
                (broker, 'ROOT', root), (broker, 'RUN', root / 'run/messagebox-update'),
                (broker, 'STATE', root / 'var/lib/button-box-update'),
                (broker, 'installed_identity', lambda: {'software_version': app['version'], 'software_commit': app['commit']}),
                (broker, 'read', lambda n,d=None: broker.update.read_state(root,n,d)),
                (broker, 'save', lambda n,v,mode=0o600: broker.update.save_state(root,n,v,mode)),
                (broker, 'unit_pid', lambda unit: os.getpid()),
                (broker.pwd, 'getpwnam', lambda name: types.SimpleNamespace(pw_uid=os.geteuid())),
                (broker.update, '_boot', lambda: selector.boot_id(root)),
                (broker.update, '_process_token', lambda pid: '123'),
            ):
                stack.enter_context(mock.patch.object(target, name, replacement))
            stack.enter_context(mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT': 'cloud'}))
            server = socket.socket(socket.AF_UNIX)
            stack.callback(server.close)
            socket_path.unlink(missing_ok=True)
            stack.callback(lambda: socket_path.unlink(missing_ok=True))
            server.bind(str(socket_path))
            server.listen(2)
            server.settimeout(3)
            errors, requests = [], []
            control = broker.Controller()
            def serve():
                try:
                    for _ in range(2):
                        connection, _ = server.accept()
                        with connection:
                            request = json.loads(connection.recv(16384))
                            requests.append(request['command'])
                            response = control.dispatch(request, (os.getpid(), os.geteuid(), os.getegid()))
                            connection.sendall(selector.canonical(response))
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=serve)
            thread.start()
            # Both paths exist in each shipped base; readiness is not fabricated
            # by the bootstrap helper or by copying old evidence files.
            producer.Confirmation().idle(types.SimpleNamespace(is_pressed=False), lambda *a: None)
            producer.heartbeat_succeeded()
            thread.join(4)
            if thread.is_alive() or errors or requests != ['button', 'heartbeat']:
                raise AssertionError((errors, requests))


@unittest.skipUnless(shutil.which('minisign'), 'real disposable minisign signatures required')
class UpdaterBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='updater-bridge-')
        cls.base = Path(cls.temporary.name).resolve()
        cls.public, cls.secret = cls.base / 'test.pub', cls.base / 'test.key'
        subprocess.run(['minisign', '-G', '-W', '-p', str(cls.public), '-s', str(cls.secret)],
                       check=True, capture_output=True)
        cls.profile = module('updater_bridge_profile', ROOT / 'scripts/dev/build_boot_hotfix.py')
        source = cls.base / 'bridge'
        payload = cls.profile.prepare(source, bridge=True)
        assets = cls.package(source, payload, cls.base / 'bridge-assets',
                             tag='v0.3.17', sequence=10, base='v0.3.12')
        cls.sources = dict.fromkeys(BASES, source)
        cls.artifacts = dict.fromkeys(BASES, assets)
        corrective = cls.base / 'corrective'
        shutil.copytree(source, corrective)
        cls.corrective = cls.package(corrective, payload, cls.base / 'corrective-assets',
            tag='v0.3.18', sequence=11, base='v0.3.12', controller_sequence=8)
        archive = cls.base / 'base.tar'
        with archive.open('wb') as output:
            subprocess.run(['git', '-C', str(ROOT), 'archive', 'v0.3.12', 'messagebox', 'sounds'],
                           check=True, stdout=output)
        with tarfile.open(archive) as original:
            cls.base_hashes = {entry.name: hashlib.sha256(original.extractfile(entry).read()).hexdigest()
                               for entry in original if entry.isfile()}
        archive.unlink()
        source = cls.base / 'full'
        payload = build.payload_paths(ROOT)
        for name, mode in payload.items():
            write(source / name, (ROOT / name).read_bytes(), mode)
        cls.full = cls.package(source, payload, cls.base / 'full-assets', tag='v0.3.20', sequence=20)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def package(cls, source, payload, output, *, tag, sequence, base=None, controller_sequence=None):
        descriptor = build.controller_descriptor(source, controller_sequence or (7 if base else 8), 'v0.3.9')
        message = write(cls.base / 'descriptor', descriptor)
        signature = cls.base / 'descriptor.minisig'
        subprocess.run(['minisign', '-S', '-s', str(cls.secret), '-m', str(message), '-x', str(signature)],
                       check=True, capture_output=True)
        payload = dict(payload)
        for name, raw in ((build.CONTROLLER_DESCRIPTOR, descriptor),
                          (build.CONTROLLER_SIGNATURE, (repr(signature.read_text()) + '\n').encode())):
            write(source / name, raw)
            payload[name] = 0o644
        if base:
            manifest = cls.profile.manifest(source, tag=tag, commit='e' * 40, sequence=sequence,
                                           supported=build.SUPPORTED, base=base, bridge=True)
        else:
            mapper = module('full_bridge_mapper', source / 'scripts/dev/release-manifest.py')
            manifest = dict(schema=1, product='button-box', version=tag, tag=tag, commit='f' * 40,
                release_sequence=sequence, channel='stable', min_updater_version='1.3.0', **build.SUPPORTED,
                files=[dict(source=n, installed=p, sha256=digest(source / n))
                       for n,p in sorted(mapper.installed_paths(source).items())])
        build.package(source, output, manifest, payload, 0)
        release.inspect_archive(output, 'button-box-' + tag + '.tar.gz', controller_keys=cls.public.parent)
        subprocess.run(['minisign', '-S', '-s', str(cls.secret), '-m', str(output / 'SHA256SUMS'),
                        '-x', str(output / 'SHA256SUMS.minisig')], check=True, capture_output=True)
        return output

    def new_box(self, directory, base):
        box = FieldBox(directory, base)
        # The shared transaction fixture records updater units and drop-ins;
        # boot assertions also need the original application activators, even
        # after rollback removes every file introduced by the capsule.
        for path in (box.snapshot / 'systemd').rglob('*'):
            if path.is_file() and path.suffix in {'.service', '.target', '.path', '.timer'}:
                target = box.root / 'etc/systemd/system' / path.name
                if not target.exists():
                    write(target, path.read_bytes())
        # Model the running fixture's selected components as target wants. The
        # original fixture's "static" values are placeholders, not boot links.
        wants = mkdir(box.root / 'etc/systemd/system/messagebox.target.wants')
        for unit, state in box.states.items():
            path = box.root / 'etc/systemd/system' / unit
            if state['active'] == 'active' and path.is_file():
                parents = unit_settings(path.read_text())['Install', 'WantedBy']
                if 'messagebox.target' in ' '.join(parents).split():
                    (wants / unit).symlink_to('/etc/systemd/system/' + unit)
                    state['enabled'] = 'enabled'
        # Recorded fixed/bootstrap fixture is shared; installed app identity is
        # variable data. Use the actual selected base, never silently v0.3.12.
        write(box.installed('/opt/messagebox/release.json'),
              json.dumps({'version': base, 'commit': 'b' * 40}).encode())
        box.bootstrap_journal['version'] = base
        box.worker.save_state(box.root, 'journal.json', box.bootstrap_journal)
        for pin in box.installed('/etc/button-box-update/release-keys').glob('*.pub'):
            pin.unlink()
        write(box.installed('/etc/button-box-update/release-keys/test.pub'), self.public.read_bytes())
        return box

    def deliver(self, box, assets, *, health=None, installed=False, preflight_step=None):
        with contextlib.ExitStack() as stack:
            if preflight_step is not None:
                execute = importlib.machinery.SourceFileLoader.exec_module
                def loaded(loader, target):
                    execute(loader, target)
                    if target.__name__ == 'capsule_boot_preflight':
                        target.repair.__kwdefaults__['step'] = preflight_step
                stack.enter_context(mock.patch.object(importlib.machinery.SourceFileLoader, 'exec_module', loaded))
            # Staged signed preflight executes inside the frozen process; only
            # its systemd boundary is redirected, never its repair implementation.
            native_run = subprocess.run
            stack.enter_context(mock.patch.object(subprocess, 'run', side_effect=lambda c, **k:
                box.run(c, **k) if c[0] == 'systemctl' else native_run(c, **k)))
            if installed:
                # Reuse the real installed-controller boundary from the bootstrap
                # integration; its verifier and worker come from the selected slot.
                from test_update_bootstrap import BootstrapCommandTests
                fixture = types.SimpleNamespace(root=box.root, box=box, application_health=health or (lambda *a: None))
                control = stack.enter_context(BootstrapCommandTests.installed_controller(fixture, selector.selected(box.root)['slot']))
            else:
                control = stack.enter_context(box.controller(signature_ok=None, health=health))
            bundle = mkdir(control.STATE / 'downloads' / ('c' * 32), 0o700)
            for path in assets.iterdir():
                write(bundle / path.name, path.read_bytes(), 0o600)
            version = json.loads((assets / 'release-manifest.json').read_bytes())['version']
            box.queue_bundle(control, bundle, version)
            stack.enter_context(mock.patch.dict(sys.modules))
            for name in list(sys.modules):
                if name == 'messagebox' or name.startswith('messagebox.'):
                    del sys.modules[name]
            if installed:
                with mock.patch.object(sys, 'argv', sys.argv[:]):
                    exec(selector.install_program(box.root, selector.selected(box.root)['slot']), {})
            else:
                control.worker()
            self.assertEqual(control.read('journal.json')['phase'], 'committed')
        return box.installed(box.worker.controlled_release(box.root))

    def hook(self, box, *, step=lambda e,p: None, fail_health=False):
        """Run delivered service commands in their real order with root redirected.

        Supervisor job dispatch and systemd-run are replaced, not the bootstrap,
        publisher, selector, proof verification or trial executables.
        """
        supervisor = box.root / 'etc/systemd/system/messagebox-update-supervisor.service.d/messagebox.conf'
        self.assertIn('ExecStartPost=/usr/bin/systemctl --no-block start messagebox-update-bootstrap.service', supervisor.read_text())
        box.worker.supervise_transaction(root=box.root, run=box.run)
        installed = box.installed(box.worker.controlled_release(box.root))
        source = installed / 'messagebox'
        boot = module('delivered_bridge_repair', source / 'cloud_boot_repair.py')
        saved = sys.path[:]
        try:
            with mock.patch.dict(sys.modules):
                for name in ('release_verify', 'trust_update'):
                    sys.modules.pop(name, None)
                api = module('delivered_bridge_slots', source / 'controller_slots.py')
        finally:
            sys.path[:] = saved
        host = LegacyHost(box.root)
        self.last_host = host
        host.recovery_run = box.run
        host.not_ready = int(fail_health)
        load_selector = api.selector
        def selected_api(path):
            loaded = load_selector(path)
            offset = [0.0]
            # Keep subprocess/socket clocks real. Accelerate only the selector's
            # readiness retry sleep; patching time.sleep globally also advances
            # subprocess.wait's polling and can exhaust a trial before it starts.
            loaded.time = types.SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0],
                sleep=lambda n: offset.__setitem__(0, offset[0] + n))
            return loaded
        api.selector = selected_api
        original = api.bootstrap, api.publish, api.complete_bootstrap
        api.bootstrap = lambda: original[0](box.root, platform=PLATFORM, run=host, step=step)
        api.publish = lambda: original[1](box.root, platform=PLATFORM, run=host, step=step,
            trial_options={'execute': host.execute, 'probe': host.probe, 'duration': 75})
        api.complete_bootstrap = lambda: original[2](box.root, step=step)
        def dispatch(command, **kwargs):
            self.assertEqual(command[0], 'systemd-run')
            self.assertEqual(command[-1], 'publish')
            self.assertIn('TimeoutStartSec=180', command)
            self.assertEqual(kwargs['timeout'], 200)
            # Promotion must begin after bootstrap has dropped all its locks.
            with selector.lock(box.root, 'operation', blocking=False), api.admission_lock(box.root):
                pass
            with mock.patch.dict(os.environ, {'INVOCATION_ID': 'synthetic-systemd-job'}), \
                    mock.patch.object(sys, 'argv', ['controller_slots.py', 'publish']):
                api.main()
            return subprocess.CompletedProcess(command, 0)
        api.subprocess = types.SimpleNamespace(run=dispatch)
        unit = (box.root / 'etc/systemd/system/messagebox-update-bootstrap.service').read_text()
        self.assertIn('TimeoutStartSec=300', unit)
        commands = [shlex.split(line.split('=', 1)[1]) for line in unit.splitlines()
                    if line.startswith(('ExecStartPre=', 'ExecStart=', 'ExecStartPost='))]
        self.assertEqual([c[-1] for c in commands],
                         ['/opt/messagebox/messagebox/cloud_boot_repair.py', 'bootstrap', 'promote', 'complete'])
        boot.repair(box.root, run=box.run, step=step)
        for command in commands[1:]:
            with mock.patch.object(sys, 'argv', ['controller_slots.py', command[-1]]):
                api.main()
        return host

    def restart(self, box, *, durations=None):
        """Cold boot executes only jobs reachable and ordered by installed units.

        PID 1, service process liveness and elapsed time remain host substitutes.
        Cycles, failed prerequisites, failed gates and start timeouts are failures;
        recovery cannot be called as a repair outside its scheduled boot job.
        """
        units = shipped_units(box.root / 'etc/systemd/system')
        units = with_bootstrap(units, (ROOT / 'scripts/install/update-bootstrap.sh').read_text())
        def dependencies(mode):
            links = boot_links(units, mode)
            for directory in (box.root / 'etc/systemd/system').glob('*.wants'):
                links[directory.name.removesuffix('.wants')].update(
                    path.name for path in directory.iterdir() if path.is_symlink())
            return links
        for mode in ('runtime', 'setup'):
            cycle = ordering_cycle(boot_model(units), links=dependencies(mode), initial=['multi-user.target'])
            self.assertIsNone(cycle, ('unreachable boot', mode, cycle))
        shutil.rmtree(box.root / 'run', ignore_errors=True)
        mkdir(box.root / 'run/lock')
        for state in box.states.values():
            state['active'] = 'inactive'
        host = LegacyHost(box.root)
        host.recovery_run = box.run
        queued, edges = ordering_graph(boot_model(units), links=dependencies('runtime'),
                                       initial=['multi-user.target'])
        predecessors = {unit: {before for before, after in edges.items() if unit in after} for unit in queued}
        completed = set()
        while queued - completed:
            ready = sorted(u for u in queued - completed if predecessors[u] <= completed)
            self.assertTrue(ready, 'boot jobs cannot make progress')
            for unit in ready:
                seconds = (durations or {}).get(unit, 0)
                if unit.endswith('.service'):
                    self.assertLess(seconds, host.service_timeout(unit), ('service start timeout', unit))
                if unit == 'messagebox-update-recovery.service':
                    route = host.service_setting(unit, 'ExecStart')
                    if '/usr/lib/messagebox-selector/selector.py boot' in route:
                        chosen = selector.recover(box.root, host, execute=host.execute)
                        selector.inspect(box.root, chosen)
                    else:
                        self.assertIn('/usr/lib/messagebox-update/bounded_update.py recover --boot', route)
                        box.worker.recover_transaction(root=box.root, run=box.run, start=False)
                    selector.recovered(box.root)
                if unit in box.states:
                    requires = {v for raw in unit_settings(units.get(unit, ''))['Unit', 'Requires'] for v in raw.split()}
                    self.assertTrue(requires <= completed, ('prerequisite has not started', unit, requires - completed))
                    box.run(['systemctl', '--job-mode=ignore-dependencies', 'start', unit])
                completed.add(unit)
        self.assertIn('messagebox-update-recovery.service', completed)
        for unit in ('messagebox.target', 'messagebox-button.service', 'messagebox-update-controller.service'):
            self.assertIn(unit, completed)
            self.assertEqual(box.states[unit]['active'], 'active', unit)
        self.assertTrue({'messagebox-update-discovery.timer', 'messagebox-update-supervisor.timer'} <= completed)
        return host

    def test_each_base_steps_to_legacy_app_promotes_and_takes_full_application(self):
        for base in BASES:
            with self.subTest(base=base), tempfile.TemporaryDirectory() as directory:
                box = self.new_box(directory, base)
                fixed = box.fixed_bytes()
                installed = self.deliver(box, self.artifacts[base])
                for entry in json.loads((installed / 'release-manifest.json').read_bytes())['files']:
                    if entry['source'].startswith(('messagebox/', 'sounds/')) and entry['source'] not in {
                            'messagebox/controller_selector.py', 'messagebox/controller_slots.py', 'messagebox/cloud_boot_repair.py',
                            build.CONTROLLER_DESCRIPTOR, build.CONTROLLER_SIGNATURE}:
                        self.assertEqual(digest(self.sources[base] / entry['source']), self.base_hashes[entry['source']])
                host = self.hook(box)
                self.assertGreater(host.probes, 0)
                self.assertEqual(selector.selected(box.root)['slot'], selector.SLOTS[1])
                self.assertEqual(selector.inspect(box.root, selector.SLOTS[1])['version'], '1.3.0')
                self.assertEqual(box.fixed_bytes(), fixed)
                self.restart(box)
                self.deliver(box, self.full, installed=True)
                self.restart(box)
                self.assertEqual(box.fixed_bytes(), fixed)
                self.assertEqual(box.worker.read_state(box.root, 'sequence.json'), 20)

    def test_bad_outer_signature_sequence_and_newer_app_do_not_mutate(self):
        for base in BASES:
            for failure in ('signature', 'sequence', 'newer_app'):
                with self.subTest(base=base, failure=failure), tempfile.TemporaryDirectory() as directory:
                    box = self.new_box(directory, base)
                    assets = self.artifacts[base]
                    if failure == 'signature':
                        assets = Path(directory) / 'bad-assets'
                        shutil.copytree(self.artifacts[base], assets)
                        write(assets / 'SHA256SUMS.minisig', b'invalid signature\n')
                    elif failure == 'sequence':
                        box.worker.save_state(box.root, 'sequence.json', 100)
                    else:
                        write(box.installed('/opt/messagebox/release.json'),
                              json.dumps({'version': 'v0.3.15', 'commit': 'b' * 40}).encode())
                    before = box.tree()
                    with self.assertRaises((ValueError, box.worker.UpdateError)):
                        self.deliver(box, assets)
                    self.assertEqual(box.tree(), before)

    def test_real_capsule_keeps_local_ask_confirmation_and_auto_window(self):
        for base in BASES:
            for mode in ('ask', 'auto'):
                with self.subTest(base=base, mode=mode), tempfile.TemporaryDirectory() as directory:
                    box = self.new_box(directory, base)
                    with box.controller(signature_ok=None) as control, contextlib.ExitStack() as stack:
                        assets = self.artifacts[base]
                        manifest = json.loads((assets / 'release-manifest.json').read_bytes())
                        # The ordinary verifier authenticates this candidate;
                        # the normal broker then owns permission and ticket creation.
                        control.verify_bundle(assets, control.KEYS, version=manifest['version'],
                            channel='stable', floor=0, platform=PLATFORM)
                        available = {'version': manifest['version'], 'sequence': manifest['release_sequence'],
                                     'manifest_sha256': digest(assets / 'release-manifest.json'), 'bundle': str(assets)}
                        control.save('available.json', available)
                        control.save('policy.json', {'mode': mode, 'channel': 'stable', 'timezone': 'UTC', 'jitter_minutes': 0})
                        for target, name, value in ((control, 'ROOT', box.root),
                            (control, 'installed', lambda: True), (control, 'local_time_ready', lambda: True),
                            (control, 'unit_pid', lambda unit: 700),
                            (control, 'auto_window', lambda *a: True),
                            (control.pwd, 'getpwnam', lambda name: types.SimpleNamespace(pw_uid=100))):
                            stack.enter_context(mock.patch.object(target, name, value, create=True))
                        stack.enter_context(mock.patch.object(control, 'subprocess', types.SimpleNamespace(run=lambda *a,**k: None)))
                        broker = control.Controller()
                        with mock.patch.object(control, 'auto_window', return_value=False):
                            broker.automatic_if_ready(None)
                        self.assertIsNone(control.read('ticket.json'))
                        broker.automatic_if_ready(control.active_operation())
                        if mode == 'ask':
                            # No request (including Not now) means no root ticket.
                            self.assertIsNone(control.read('ticket.json'))
                            def send(command, **fields):
                                return broker.dispatch({'command': command, **fields}, (700,100,100))
                            operation = send('install', operation_id='synthetic-owner', version=manifest['version'], remaining=300)
                            self.assertEqual(operation['state'], 'waiting_for_button')
                            self.assertIsNone(control.read('ticket.json'))
                            binding = operation['binding']
                            send('arm', binding=binding, released=True)
                            with self.assertRaisesRegex(ValueError, 'short_or_expired'):
                                send('confirm', binding=binding, released=True)
                            send('button', released=False)
                            box.now += 3.1
                            send('confirm', binding=binding, released=True)
                            with self.assertRaises(ValueError):
                                send('confirm', binding=binding, released=True)
                        ticket = control.read('ticket.json')
                        self.assertEqual(ticket['version'], manifest['version'])
                        self.assertEqual(ticket['manifest_sha256'], available['manifest_sha256'])
                        self.assertEqual(control.policy()['mode'], mode)

    def test_bad_inner_signature_and_controller_sequence_keep_working_slot(self):
        for base in BASES:
            for failure in ('signature', 'sequence'):
                with self.subTest(base=base, failure=failure), tempfile.TemporaryDirectory() as directory:
                    box = self.new_box(directory, base)
                    installed = self.deliver(box, self.artifacts[base])
                    fixed = box.fixed_bytes()
                    if failure == 'signature':
                        write(installed / build.CONTROLLER_SIGNATURE, repr('invalid inner signature').encode())
                    else:
                        write(box.root / slots.STATE / 'floor.json', selector.envelope({
                            'sequence': 100, 'digest': '0' * 64, 'version': '1.3.0'}), 0o600)
                    with self.assertRaises(ValueError):
                        self.hook(box)
                    self.assertFalse((box.root / selector.SLOTS[1] / 'GOOD').exists())
                    self.assertEqual(box.fixed_bytes(), fixed)
                    self.restart(box)

    def test_app_health_failure_rolls_back_and_controller_health_failure_retains_A(self):
        for base in BASES:
            with self.subTest(base=base), tempfile.TemporaryDirectory() as directory:
                box = self.new_box(directory, base)
                fixed, before = box.fixed_bytes(), os.readlink(box.root / 'opt/messagebox')
                def fail(*args):
                    raise box.worker.UpdateError('health_timeout')
                with self.assertRaises(box.worker.UpdateError):
                    self.deliver(box, self.artifacts[base], health=fail)
                self.assertEqual(os.readlink(box.root / 'opt/messagebox'), before)
                self.assertEqual(box.fixed_bytes(), fixed)
                self.restart(box)
            with self.subTest(base=base, controller=True), tempfile.TemporaryDirectory() as directory:
                box = self.new_box(directory, base)
                fixed = box.fixed_bytes()
                self.deliver(box, self.artifacts[base])
                with self.assertRaisesRegex(ValueError, 'trial failed'):
                    self.hook(box, fail_health=True)
                self.assertFalse((box.root / slots.COMPLETE).exists())
                self.assertFalse((box.root / selector.SLOTS[1] / 'GOOD').exists())
                host = LegacyHost(box.root)
                host.recovery_run = box.run
                with fast_clock():
                    selector.check(box.root, host, probe=host.probe, execute=host.execute)
                self.assertEqual(selector.selected(box.root)['slot'], selector.SLOTS[0])
                self.assertEqual(box.fixed_bytes(), fixed)
                self.restart(box)
                slot_a = {path.name: (path.lstat().st_mode, path.read_bytes()) for path in
                          (box.root / selector.SLOTS[0]).iterdir()}

                # A failed descriptor is one use, but a fresh signed correction
                # must traverse the permanent selector's admission/rollback guard.
                self.deliver(box, self.corrective, installed=True)
                self.hook(box)
                self.assertEqual(selector.selected(box.root)['slot'], selector.SLOTS[1])
                self.restart(box)
                selected = selector.selected(box.root)
                self.assertEqual(selected['slot'], selector.SLOTS[1])
                self.assertFalse(selected['trial'])
                self.assertEqual(selector.inspect(box.root, selected['slot'])['generation'], 8)
                self.assertEqual(box.worker.read_state(box.root, 'sequence.json'), 11)
                self.assertEqual({path.name: (path.lstat().st_mode, path.read_bytes()) for path in
                                  (box.root / selector.SLOTS[0]).iterdir()}, slot_a)
                self.assertEqual(box.fixed_bytes(), fixed)

    def test_power_loss_at_each_capsule_journal_step_uses_frozen_recovery(self):
        for base in BASES:
            for phase in ('prepared', 'quiesced', 'switched', 'probation', 'restoring_activators', 'committed'):
                with self.subTest(base=base, phase=phase), tempfile.TemporaryDirectory() as directory:
                    box = self.new_box(directory, base)
                    fixed = box.fixed_bytes()
                    original = box.worker._journal
                    def cut(root, journal, current):
                        self.assertEqual((root / repair.DROPIN).read_bytes(), repair.ORDERING)
                        original(root, journal, current)
                        if current == phase:
                            raise KeyboardInterrupt('power loss')
                    with mock.patch.object(box.worker, '_journal', side_effect=cut), self.assertRaises(KeyboardInterrupt):
                        self.deliver(box, self.artifacts[base])
                    self.restart(box)
                    self.assertIn(box.worker.read_state(box.root, 'journal.json')['phase'], ('rolled_back', 'committed'))
                    self.assertEqual(box.fixed_bytes(), fixed)

    def test_authenticated_preflight_precedes_mutation_and_cuts_are_classified_honestly(self):
        for base in BASES:
            with self.subTest(base=base), tempfile.TemporaryDirectory() as directory:
                reference = self.new_box(Path(directory) / 'reference', base)
                events = []
                self.deliver(reference, self.artifacts[base], preflight_step=lambda e,p: events.append((e,p)))
                reachable, inherited = 0, 0
                for index in range(len(events)):
                    with self.subTest(cut=index, event=events[index][0]), tempfile.TemporaryDirectory(dir=directory) as selected:
                        box = self.new_box(selected, base)
                        before = os.readlink(box.root / 'opt/messagebox')
                        journal = box.worker.read_state(box.root, 'journal.json')
                        releases = sorted((box.root / 'opt/messagebox-releases').iterdir())
                        fixed = box.fixed_bytes()
                        disk = DurableView(box.root)
                        count = 0
                        def cut(event, path):
                            nonlocal count
                            # Preflight runs under the real transaction locks.
                            with self.assertRaises(BlockingIOError):
                                with repair.locks(box.root, os.geteuid()):
                                    pass
                            self.assertEqual(os.readlink(box.root / 'opt/messagebox'), before)
                            self.assertEqual(box.worker.read_state(box.root, 'journal.json'), journal)
                            self.assertEqual(sorted((box.root / 'opt/messagebox-releases').iterdir()), releases)
                            self.assertFalse(any(c[:2] == ['systemctl', 'stop'] for c in box.commands))
                            path = Path(path)
                            if event == 'file_fsync':
                                disk.files[path.stat().st_ino] = path.read_bytes()
                            else:
                                disk.step(event, str(path))
                            count += 1
                            if count == index + 1:
                                raise KeyboardInterrupt('preflight power loss')
                        with self.assertRaises(KeyboardInterrupt):
                            self.deliver(box, self.artifacts[base], preflight_step=cut)
                        disk.power_loss()
                        if (box.root / repair.DROPIN).exists():
                            self.restart(box)
                            reachable += 1
                        else:
                            # This is an uncovered original-fleet failure, never
                            # a passed boot or a direct offline recovery shortcut.
                            with self.assertRaisesRegex(AssertionError, 'unreachable boot'):
                                self.restart(box)
                            inherited += 1
                        self.assertEqual(box.fixed_bytes(), fixed)
                        self.assertEqual(os.readlink(box.root / 'opt/messagebox'), before)
                self.assertGreater(reachable, 0)
                self.assertGreater(inherited, 0)
                print('Bridge preflight cuts:', base, 'reachable', reachable, 'inherited uncovered', inherited)

    def test_boot_model_rejects_cycle_and_service_timeouts(self):
        with tempfile.TemporaryDirectory() as directory:
            box = self.new_box(directory, 'v0.3.12')
            with self.assertRaisesRegex(AssertionError, 'unreachable boot'):
                self.restart(box)
            self.deliver(box, self.artifacts['v0.3.12'])
            with self.assertRaisesRegex(AssertionError, 'service start timeout'):
                self.restart(box, durations={'messagebox-update-recovery.service': 181})
            self.restart(box, durations={'messagebox-update-recovery.service': 179})
            self.hook(box)
            with self.assertRaisesRegex(AssertionError, 'service start timeout'):
                self.restart(box, durations={'messagebox-update-recovery.service': 401})
            self.restart(box, durations={'messagebox-update-recovery.service': 399})

    def test_prepared_candidate_and_external_writes_already_have_durable_repair(self):
        for base in BASES:
            for boundary in ('candidate', 'snapshot', 'external'):
                with self.subTest(base=base, boundary=boundary), tempfile.TemporaryDirectory() as directory:
                    box = self.new_box(directory, base)
                    original = box.worker.create_backup if boundary == 'snapshot' else box.worker._atomic_install
                    def cut(*args, **kwargs):
                        original(*args, **kwargs)
                        destination = Path(args[1]) if boundary != 'snapshot' else None
                        if (boundary == 'snapshot' or
                                boundary == 'candidate' and destination.is_relative_to(box.root / 'opt/messagebox-releases') or
                                boundary == 'external' and destination.is_relative_to(box.root / 'etc')):
                            self.assertEqual((box.root / repair.DROPIN).read_bytes(), repair.ORDERING)
                            raise KeyboardInterrupt('application mutation cut')
                    name = 'create_backup' if boundary == 'snapshot' else '_atomic_install'
                    with mock.patch.object(box.worker, name, side_effect=cut), self.assertRaises(KeyboardInterrupt):
                        self.deliver(box, self.artifacts[base])
                    self.restart(box)

    def test_preflight_reload_failure_leaves_running_application_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            box = self.new_box(directory, 'v0.3.12')
            before = os.readlink(box.root / 'opt/messagebox')
            original = box.run
            def fail(command, **kwargs):
                if command == ['systemctl', 'daemon-reload']:
                    raise subprocess.CalledProcessError(1, command)
                return original(command, **kwargs)
            box.run = fail
            with self.assertRaises(box.worker.UpdateError):
                self.deliver(box, self.artifacts[box.version])
            self.assertEqual(os.readlink(box.root / 'opt/messagebox'), before)
            self.assertEqual(len(list((box.root / 'opt/messagebox-releases').iterdir())), 1)
            self.assertEqual(box.states['messagebox-button.service']['active'], 'active')
            self.assertFalse(any(c[:2] == ['systemctl', 'stop'] for c in box.commands))
            box.run = original
            self.restart(box)  # Durable bytes protect the next cold boot despite reload failure.

    def discover(self, box, *, pinned=False, latest='bridge', requested=None,
                 selected=False, transfer_seconds=0, service_budget=None):
        """Actual frozen/selected downloader against a local GitHub API substitute."""
        paths, releases = {}, {}
        for name, assets in (('bridge', self.artifacts[box.version]), ('full', self.full)):
            version = json.loads((assets / 'release-manifest.json').read_bytes())['version']
            entries = []
            for path in assets.iterdir():
                url = 'https://github.com/button-box/button-box/releases/download/' + version + '/' + path.name
                paths[url] = path
                entries.append({'name': path.name, 'size': path.stat().st_size, 'browser_download_url': url})
            releases[name] = {'tag_name': version, 'draft': False, 'prerelease': False, 'assets': entries}
        elapsed, urls = [0.0], []
        class Response:
            headers = {}
            def __init__(self, stream, size=0):
                self.stream, self.size = stream, size
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def read(self, size):
                block = self.stream.read(size)
                if self.size and block:
                    elapsed[0] += transfer_seconds * len(block) / self.size
                    if service_budget and elapsed[0] >= service_budget:
                        raise subprocess.TimeoutExpired('messagebox-update-discovery.service', service_budget)
                return block
        def opener(request, **kwargs):
            url = request.full_url
            urls.append(url)
            if url in paths:
                path = paths[url]
                return Response(path.open('rb'), path.stat().st_size if path.name.endswith('.tar.gz') else 0)
            if url.endswith('/latest'):
                value = releases[latest]
            elif '?per_page=100&page=1' in url:
                value = [releases['full'], releases['bridge']]
            elif '/tags/' in url:
                value = next(r for r in releases.values() if r['tag_name'] == url.rsplit('/', 1)[-1])
            else:
                raise AssertionError(url)
            return Response(io.BytesIO(json.dumps(value).encode()))
        if selected:
            path = box.root / selector.selected(box.root)['slot']
            verifier = module('selected_discovery_verifier', path / 'release_verify.py')
            trust = module('selected_discovery_trust', path / 'trust_update.py')
            with mock.patch.dict(sys.modules, {'release_verify': verifier, 'trust_update': trust}):
                discovery = module('selected_discovery', path / 'update_discovery.py')
        else:
            verifier = load_frozen(box.version, 'scripts/install/release_verify.py')
            trust = load_frozen(box.version, 'scripts/install/trust_update.py')
            with mock.patch.dict(sys.modules, {'release_verify': verifier, 'trust_update': trust}):
                discovery = load_frozen(box.version, 'scripts/install/update_discovery.py')
        discovery.KEYS = box.root / 'etc/button-box-update/release-keys'
        with tempfile.TemporaryDirectory(dir=box.root.parent) as directory:
            client = discovery.Discovery(directory, opener=opener, clock=lambda: 2000000000,
                **({'monotonic': lambda: elapsed[0]} if selected else {}))
            result = client.poll('stable', 0, PLATFORM, trust_enabled=pinned, requested=requested, hint=True)
        return result, urls, elapsed[0]

    def test_frozen_discovery_cohorts_with_both_releases_and_permanent_latest_hold(self):
        for base in BASES:
            with self.subTest(base=base), tempfile.TemporaryDirectory() as directory:
                box = self.new_box(directory, base)
                for pinned in (False, True):
                    result, urls, _ = self.discover(box, pinned=pinned)
                    self.assertEqual(result['version'], 'v0.3.17')
                    self.assertTrue(any('?per_page=' in u for u in urls) if pinned else any(u.endswith('/latest') for u in urls))
                    result, _, _ = self.discover(box, pinned=pinned, latest='full')
                    self.assertEqual(result['version'] if result else None, 'v0.3.17' if pinned else None)
                    if not pinned:
                        result, urls, _ = self.discover(box, latest='full', requested='v0.3.17')
                        self.assertEqual(result['version'], 'v0.3.17')
                        self.assertTrue(any(u.endswith('/tags/v0.3.17') for u in urls))
                    else:
                        # Frozen pinned discovery appends the requested release
                        # to history without deduplication, then prunes its own
                        # selected directory. History alone is the working route.
                        with self.assertRaisesRegex(ValueError, 'discovery_failed'):
                            self.discover(box, pinned=True, latest='full', requested='v0.3.17')

    def test_integrated_discovery_timeout_allows_real_download_over_900_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            box = self.new_box(directory, 'v0.3.12')
            self.deliver(box, self.artifacts[box.version])
            host = self.hook(box)
            self.assertEqual(host.service_timeout('messagebox-update-discovery.service'), 1800)
            with self.assertRaises(subprocess.TimeoutExpired):
                self.discover(box, selected=True, latest='full', transfer_seconds=1000, service_budget=900)
            result, _, elapsed = self.discover(box, selected=True, latest='full', transfer_seconds=1000,
                service_budget=host.service_timeout('messagebox-update-discovery.service'))
            self.assertEqual(result['version'], 'v0.3.20')
            self.assertGreater(elapsed, 900)
            with self.assertRaisesRegex(ValueError, 'discovery_failed'):
                self.discover(box, selected=True, latest='full', transfer_seconds=1600,
                    service_budget=host.service_timeout('messagebox-update-discovery.service'))
            # The downloader's own 1500-second limit remains.
            # A later manager override cannot be accepted just because routing bytes match.
            write(box.root / 'etc/systemd/system/messagebox-update-discovery.service.d/zz-timeout.conf',
                  b'[Service]\nTimeoutStartSec=900\n')
            with self.assertRaisesRegex(ValueError, 'effective discovery timeout differs'):
                slots.effective_routes(box.root, selector, host)

    def test_power_loss_at_every_repair_bootstrap_slot_and_completion_boundary(self):
        for base in BASES:
            with self.subTest(base=base), tempfile.TemporaryDirectory() as directory:
                box = self.new_box(directory, base)
                self.deliver(box, self.artifacts[base])
                fixed = box.fixed_bytes()
                image = Path(directory) / 'before-hook'
                shutil.copytree(box.root, image, symlinks=True)
                events = []
                self.hook(box, step=lambda e,p: events.append((e,p)))
                for index in range(len(events)):
                    with self.subTest(cut=index, event=events[index][0]):
                        shutil.rmtree(box.root)
                        shutil.copytree(image, box.root, symlinks=True)
                        disk = DurableView(box.root)
                        count = 0
                        def cut(event, path):
                            nonlocal count
                            # The repair helper reports its unique temp path;
                            # the selector reports the final path and uses .tmp.
                            # Both callbacks follow the actual fsync. Observe
                            # those writes directly, as the A/B cut tests do,
                            # instead of rescanning immutable sounds per fsync.
                            path = Path(path)
                            if event == 'file_fsync' and not path.with_name(path.name + '.tmp').exists():
                                disk.files[path.stat().st_ino] = path.read_bytes()
                            else:
                                disk.step(event, str(path))
                            count += 1
                            if count == index + 1:
                                raise KeyboardInterrupt('power loss')
                        with self.assertRaises(KeyboardInterrupt):
                            self.hook(box, step=cut)
                        disk.power_loss()
                        self.restart(box)
                        self.assertEqual(box.fixed_bytes(), fixed)
                        # A consumed failed trial is not automatically retried;
                        # nevertheless the old updater/runtime must be available.
                        try:
                            self.hook(box)
                        except ValueError as exc:
                            self.assertEqual(str(exc), 'trial already consumed')
                        self.restart(box)
                self.assertTrue({'write', 'file_fsync', 'rename', 'directory_fsync',
                                 'unlink_GOOD', 'trial_complete', 'completion_rename'} <= {e for e,p in events})
                print('Bridge durable hook boundaries:', base, len(events))
