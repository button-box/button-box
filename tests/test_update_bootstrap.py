"""Run the full bootstrap shell with a temp filesystem and explicit OS boundaries.

The shell, globs, install(1), Python key/policy code, and layout conversion are
real. Root ownership, Pi identity, accounts, and systemd are host substitutes.
"""
import importlib.util
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from field_updater import FieldBox, load_frozen, mkdir, write
from messagebox import cloud_boot_repair, controller_selector as selector, controller_slots as slots
import test_controller_descriptor as descriptors
from test_controller_slots import DurableView, Host
from test_release_tooling import build
from test_update_discovery import PLATFORM
from test_bounded_update import release_manifest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('minisign'), 'real minisign required for initial controller proof')
class BootstrapCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        descriptors.ControllerProofTests.setUpClass()
        cls.keys = descriptors.ControllerProofTests.key_pairs

    @classmethod
    def tearDownClass(cls):
        descriptors.ControllerProofTests.tearDownClass()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='first-bootstrap-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.box = box = FieldBox(self.base / 'field')
        self.root = box.root
        # Reverse the recorded completed layout to an in-place first install.
        (self.root / 'opt/messagebox').unlink()
        (self.root / box.old.lstrip('/')).rename(self.root / 'opt/messagebox')
        for name in ('usr/lib/messagebox-update', 'etc/button-box-update', 'var/lib/button-box-update'):
            shutil.rmtree(self.root / name)
        self.repo = self.base / 'checkout'
        for name in ('systemd', 'config'):
            shutil.copytree(ROOT / name, self.repo / name)
        shutil.rmtree(self.repo / 'config/release-keys')
        write(self.repo / 'config/release-keys/test.pub', self.keys[0][0].read_bytes())
        for path in (ROOT / 'scripts/install').glob('*.py'):
            write(self.repo / 'scripts/install' / path.name, path.read_bytes())
        write(self.repo / 'messagebox/cloud_boot_repair.py', (ROOT / 'messagebox/cloud_boot_repair.py').read_bytes())
        script = (ROOT / 'scripts/install/update-bootstrap.sh').read_text()
        # Redirect only fixed device paths. Keep the complete shell control flow
        # and the real current unit/drop-in glob, including its source directory.
        for prefix in ('/etc/', '/usr/', '/var/', '/run/', '/opt/'):
            script = script.replace(prefix, str(self.root) + prefix)
        self.script = write(self.repo / 'scripts/install/update-bootstrap.sh', script.encode())
        self.log = self.base / 'commands.jsonl'
        self.states = write(self.base / 'states.json', json.dumps(box.states).encode())
        shim = write(self.base / 'bin/shim', ("#!" + sys.executable + '\n' + r'''
import json,os,subprocess,sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
root=Path(os.environ['BOOTSTRAP_TEST_ROOT'])
repo=Path(os.environ['BOOTSTRAP_TEST_REPO'])
with open(os.environ['BOOTSTRAP_TEST_LOG'],'a') as log: log.write(json.dumps([name,*args])+'\n')
if name=='install':
    filtered=[]
    while args:
        option=args.pop(0)
        if option in ('-o','-g'): args.pop(0)
        else: filtered.append(option)
    raise SystemExit(subprocess.call(['/usr/bin/install',*filtered]))
if name=='id': print('0')
elif name=='timedatectl': print('UTC')
elif name=='python3':
    sys.path[:0]=[str(repo/'scripts/install'),os.environ['BOOTSTRAP_TEST_TESTS']]
    if args[0]=='-':
        code=sys.stdin.read()
        if 'SUPPORTED_MODELS' in code:
            import release_verify as v
            v.local_platform=lambda:{'os':'raspios-trixie','architecture':'aarch64','model':v.SUPPORTED_MODELS[0]}
        sys.argv=args
        exec(compile(code,'bootstrap-stdin','exec'),{'__name__':'__main__'})
    elif args[0].endswith('cloud_boot_repair.py'):
        sys.path.insert(0,str(repo))
        from messagebox.cloud_boot_repair import repair
        from field_updater import FieldBox
        box=object.__new__(FieldBox)
        box.root=root
        box.commands=[]
        repair(root,bootstrap=True,run=box.run)
    else:
        import bounded_update as u
        from field_updater import FieldBox
        box=object.__new__(FieldBox)
        box.states=json.loads(Path(os.environ['BOOTSTRAP_TEST_STATES']).read_text())
        box.commands=[]
        original=u.bootstrap_layout
        u.bootstrap_layout=lambda **k:original(root=k['root'],run=box.run)
        u._boot=lambda:'synthetic-boot'
        u.main(['--root',str(root),*args[1:]])
elif name not in ('systemctl','getent','minisign','espeak-ng'):
    raise SystemExit('unexpected host boundary: '+name)
''').encode(), 0o755)
        for name in ('id', 'timedatectl', 'install', 'python3', 'systemctl', 'getent', 'minisign', 'espeak-ng'):
            (shim.parent / name).symlink_to(shim.name)
        self.env = dict(os.environ, PATH=str(shim.parent) + os.pathsep + os.environ['PATH'],
            BOOTSTRAP_TEST_ROOT=str(self.root), BOOTSTRAP_TEST_REPO=str(self.repo),
            BOOTSTRAP_TEST_LOG=str(self.log), BOOTSTRAP_TEST_STATES=str(self.states),
            BOOTSTRAP_TEST_TESTS=str(ROOT / 'tests'))

    def run_bootstrap(self, transport):
        write(self.root / 'etc/messagebox/env', ('MSGBOX_TRANSPORT=' + transport + '\n').encode())
        return subprocess.run(['/bin/sh', str(self.script), 'auto', 'stable'], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def fresh_setup(self):
        """Run setup's real allowlists/install commands, with OS edges replaced.

        Package installation, accounts, hardware and systemd are host boundaries.
        The shell, source checks, installed bytes and sound validation are real.
        """
        shutil.rmtree(self.root)
        self.root.mkdir(mode=0o755)
        # Model membership in the shared service group on non-root test hosts.
        os.chown(self.root, -1, os.getegid())
        for name in ('etc/systemd/system', 'etc/tmpfiles.d', 'usr/local/bin', 'usr/local/sbin', 'run/lock'):
            mkdir(self.root / name)
        for name in ('messagebox', 'sounds', 'scripts', 'config', 'systemd'):
            shutil.copytree(ROOT / name, self.repo / name, dirs_exist_ok=True)
        # Restore the bootstrap script's redirected fixed paths after copying.
        script = (ROOT / 'scripts/install/update-bootstrap.sh').read_text()
        setup = (ROOT / 'scripts/setup.sh').read_text()
        for prefix in ('/etc/', '/usr/', '/var/', '/run/', '/opt/'):
            script = script.replace(prefix, str(self.root) + prefix)
            setup = setup.replace(prefix, str(self.root) + prefix)
        setup = setup.replace(str(self.root) + '/usr/bin/python3', 'python3')
        self.script.write_text(script)
        setup_path = write(self.repo / 'scripts/setup.sh', setup.encode(), 0o755)
        # These helpers install OS dependencies, not application source files.
        for name in ('wacli', 'comitup', 'nfc'):
            write(self.repo / ('scripts/install/' + name + '.sh'),
                  ('#!/bin/sh\nexec bench-os-helper ' + name + '\n').encode(), 0o755)
        shim = write(self.base / 'setup-bin/shim', ('#!' + sys.executable + '\n' + r'''
import json,os,subprocess,sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
root=Path(os.environ['BOOTSTRAP_TEST_ROOT'])
repo=Path(os.environ['BOOTSTRAP_TEST_REPO'])
with open(os.environ['BOOTSTRAP_TEST_LOG'],'a') as log: log.write(json.dumps([name,*args])+'\n')
if name=='id': print('501')
elif name=='getent': raise SystemExit(2)
elif name=='ln': os.link(args[-2],args[-1])
elif name=='sed':
    import re
    expression=args[1].split('/')
    path=Path(args[2])
    path.write_text(re.sub(expression[1],expression[2],path.read_text(),flags=re.M))
elif name=='systemctl': raise SystemExit(3 if args[0]=='is-active' else 0)
elif name=='bench-os-helper':
    if args==['nfc']:
        (root/'opt/messagebox/venv-nfc').mkdir()
        target=root/'etc/systemd/system/messagebox-nfc.service'
        target.write_bytes((repo/'systemd/messagebox-nfc.service').read_bytes())
elif name=='python3':
    if args[0].endswith('audio_config.py'):
        sys.path.insert(0,str(repo/'scripts/install'))
        from audio_config import render_environment
        print(render_environment(Path(args[1]).read_text(),'mic','plughw:CARD=mic,DEV=0',
                                 'Device','plughw:CARD=Device,DEV=0'),end='')
    elif args[0].endswith('messagebox-mode-migrate.py') or args[:2]==['-m','messagebox.wifi_watchdog']:
        pass
    else: os.execv(sys.executable,[sys.executable,*args])
elif name not in ('apt-get','groupadd','useradd','usermod','systemd-tmpfiles','systemd-analyze','journalctl'):
    raise SystemExit('unexpected setup boundary: '+name)
''').encode(), 0o755)
        for name in ('id', 'getent', 'ln', 'sed', 'systemctl', 'bench-os-helper', 'python3',
                     'apt-get', 'groupadd', 'useradd', 'usermod', 'systemd-tmpfiles',
                     'systemd-analyze', 'journalctl'):
            (shim.parent / name).symlink_to(shim.name)
        write(shim.parent / 'sudo', b'#!/bin/sh\nexec "$@"\n', 0o755)
        write(shim.parent / 'install', b'''#!/bin/bash
args=()
while (( $# )); do
  if [[ $1 = -o || $1 = -g ]]; then shift 2
  else args+=("$1"); shift; fi
done
exec /usr/bin/install "${args[@]}"
''', 0o755)
        env = dict(self.env, PATH=str(shim.parent) + os.pathsep + os.environ['PATH'])
        result = subprocess.run(['/bin/sh', str(setup_path)], env=env,
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ('controller_slots.py', 'cloud_setup.py', 'speaker.py', 'remote_access.py'):
            self.assertEqual((self.root / 'opt/messagebox/messagebox' / name).read_bytes(),
                             (ROOT / 'messagebox' / name).read_bytes())
        # A fresh box has no enabled messaging components or saved household.
        for state in self.box.states.values():
            state.update(active='inactive', enabled='static')
        for unit in ('messagebox-mode-reconcile.path', 'messagebox-wifi-change.path'):
            self.box.states[unit].update(active='active', enabled='enabled')
        for unit in ('messagebox-wifi-watchdog.timer', 'messagebox-onboarding-voice.path',
                     'messagebox-onboarding-complete.path'):
            self.box.states[unit]['enabled'] = 'enabled'
        self.states.write_text(json.dumps(self.box.states))
        # setup deliberately does not claim an updater-managed identity. The
        # bench records the exact source as a distinct, local bootstrap tree.
        write(self.root / 'opt/messagebox/release.json',
              json.dumps({'version': 'bench-install', 'commit': 'b' * 40}).encode())
        # Restore release trust; bootstrap must verify the real disposable pin.
        shutil.rmtree(self.repo / 'config/release-keys')
        write(self.repo / 'config/release-keys/test.pub', self.keys[0][0].read_bytes())

    @contextlib.contextmanager
    def installed_controller(self, slot):
        """Real installed broker, minisign, extraction, guard and transaction."""
        with mock.patch.dict(sys.modules):
            for name in ('release_verify', 'bounded_update', 'trust_update', 'update_controller'):
                spec = importlib.util.spec_from_file_location(name, self.root / slot / (name + '.py'))
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
            controller = module
            worker = controller.update
            worker.time, worker.shutil, worker._boot = self.box.worker.time, self.box.worker.shutil, self.box.worker._boot
            controller.ROOT = self.root
            controller.STATE = self.root / 'var/lib/button-box-update'
            controller.KEYS = self.root / 'etc/button-box-update/release-keys'
            controller.RUN = self.root / 'run/messagebox-update'
            controller.time = worker.time
            controller.local_platform = lambda: PLATFORM
            controller.read = lambda n,d=None: worker.read_state(self.root,n,d)
            controller.save = lambda n,v,mode=0o600: worker.save_state(self.root,n,v,mode)
            self.box.worker = worker
            transaction = worker.install_transaction
            with mock.patch.object(worker, 'install_transaction',
                    side_effect=lambda *a,**k: transaction(*a,root=self.root,run=self.box.run,
                                                         health=self.application_health,**k)):
                yield controller

    def signed_application(self, version, commit, sequence, *, controller_version='1.3.0',
                           controller_sequence=7, automatic=False, broken=False):
        source = self.box.source
        for name in (ROOT / 'scripts/release/payload.txt').read_text().splitlines():
            if name and not name.startswith('#'):
                write(source / name, (ROOT / name).read_bytes(),
                      0o755 if (ROOT / name).stat().st_mode & 0o111 else 0o644)
        verifier = source / 'scripts/install/release_verify.py'
        verifier.write_text(verifier.read_text().replace('UPDATER_VERSION = (1, 3, 0)',
            'UPDATER_VERSION = ' + str(tuple(map(int, controller_version.split('.'))))))
        if broken:
            discovery = source / 'scripts/install/update_discovery.py'
            discovery.write_text(discovery.read_text().replace("return {'protocol': 1, 'dry_run': True}",
                                                               "return {'protocol': 1, 'dry_run': False}"))
        if automatic:
            helper = source / 'messagebox/controller_slots.py'
            helper.write_text(helper.read_text().replace('            bootstrap()\n',
                "            bootstrap()\n"
                "            # Bootstrap has returned and released all locks.\n"
                "            subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),\n"
                "                            'promote'], check=True, timeout=200)\n"))
            unit = source / 'systemd/messagebox-update-bootstrap.service'
            unit.write_text(unit.read_text().replace('TimeoutStartSec=60', 'TimeoutStartSec=300'))
        with mock.patch.object(build, 'CONTROLLER_VERSION', controller_version):
            descriptor = build.controller_descriptor(source, controller_sequence, 'v0.3.13')
        proof = write(self.base / 'descriptor', descriptor)
        signature = self.base / 'signature'
        subprocess.run(['minisign','-S','-m',str(proof),'-s',str(self.keys[0][1]),'-x',str(signature)],
                       check=True,capture_output=True)
        write(source / slots.verify.CONTROLLER_DESCRIPTOR, descriptor)
        write(source / slots.verify.CONTROLLER_SIGNATURE, (repr(signature.read_text()) + '\n').encode())
        # A later application changes one harmless file, not controller bytes.
        if sequence > 1:
            write(source / 'messagebox/example.py', b"VALUE = 'later OTA'\n")
        manifest = {'schema':1,'product':'button-box','version':version,'tag':version,'commit':commit,
                    'release_sequence':sequence,'channel':'stable','min_updater_version':'1.3.0',
                    **build.SUPPORTED,
                    'files':[{'source':name,'installed':target,'sha256':build.digest(source/name)}
                             for name,target in sorted(release_manifest.installed_paths(source).items())]}
        payload = {p.relative_to(source).as_posix(): 0o755 if p.stat().st_mode & 0o111 else 0o644
                   for p in source.rglob('*') if p.is_file() and p.name != 'release-manifest.json'}
        assets = self.base / ('assets-' + str(sequence))
        build.package(source,assets,manifest,payload,0)
        subprocess.run(['minisign','-S','-m',str(assets/'SHA256SUMS'),'-s',str(self.keys[0][1]),
                        '-x',str(assets/'SHA256SUMS.minisig')],check=True,capture_output=True)
        return assets

    def install_signed_application(self, assets):
        version = json.loads((assets / 'release-manifest.json').read_text())['version']
        with self.installed_controller(selector.selected(self.root)['slot']) as controller:
            bundle = mkdir(controller.STATE / 'downloads' / ('c' * 32), 0o700)
            for path in assets.iterdir():
                write(bundle / path.name, path.read_bytes(), 0o600)
            self.box.queue_bundle(controller, bundle, version)
            with mock.patch.object(sys, 'argv', sys.argv[:]):
                exec(selector.install_program(self.root, selector.selected(self.root)['slot']), {})
            self.assertEqual(controller.read('journal.json')['phase'], 'committed')
        return os.readlink(self.root / 'opt/messagebox')

    def automatic_supervisor(self, *, slow_trial=False, fail_promotion=False, contended=False):
        """Run the installed supervisor and its real ExecStartPost/ExecStart chain.

        Only systemd job dispatch, elapsed time and hardware remain host edges.
        No manual promotion entry point is called by the test.
        """
        active = selector.selected(self.root)['slot']
        spec = importlib.util.spec_from_file_location('installed_slot_worker', self.root / active / 'bounded_update.py')
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        worker._boot = self.box.worker._boot
        transaction = worker.supervise_transaction
        with mock.patch.dict(sys.modules, {'bounded_update': worker}), \
             mock.patch.object(worker, 'supervise_transaction',
                               side_effect=lambda **k: transaction(run=self.box.run, **k)):
            exec(selector.recovery_program(self.root, active, ('bounded_update.py', 'supervise')), {})
        dropin = self.root / 'etc/systemd/system/messagebox-update-supervisor.service.d/messagebox.conf'
        post = next(line.split('=', 1)[1].split() for line in dropin.read_text().splitlines()
                    if line.startswith('ExecStartPost='))
        self.assertEqual(post, ['/usr/bin/systemctl', '--no-block', 'start', 'messagebox-update-bootstrap.service'])
        trigger = (self.root / slots.TRIGGER).read_bytes()
        self.assertEqual(trigger, slots.TRIGGER_BYTES)
        marker = self.root / slots.COMPLETE
        if marker.is_symlink() and os.readlink(marker) == os.readlink(self.root / 'opt/messagebox'):
            return False
        unit = (self.root / 'etc/systemd/system/messagebox-update-bootstrap.service').read_text()
        self.assertIn('ExecStartPre=/usr/bin/python3 -I -B /opt/messagebox/messagebox/cloud_boot_repair.py', unit)
        cloud_boot_repair.repair(self.root, run=self.box.run)
        entry = next(line.split('=', 1)[1].split() for line in unit.splitlines() if line.startswith('ExecStart='))
        budget = int(next(line.split('=', 1)[1] for line in unit.splitlines() if line.startswith('TimeoutStartSec=')))
        self.assertEqual(entry, ['/usr/bin/python3', '-B', '/opt/messagebox/messagebox/controller_slots.py', 'bootstrap'])
        self.assertGreaterEqual(budget, 60 + 200)
        release = self.root / os.readlink(self.root / 'opt/messagebox').lstrip('/')
        spec = importlib.util.spec_from_file_location('future_application_slots', release / 'messagebox/controller_slots.py')
        future = importlib.util.module_from_spec(spec)
        saved_path = sys.path[:]
        try:
            with mock.patch.dict(sys.modules):
                for name in ('release_verify', 'trust_update'):
                    sys.modules.pop(name, None)
                spec.loader.exec_module(future)
        finally:
            sys.path[:] = saved_path
        bootstrap, publish = future.bootstrap, future.publish
        def begin():
            if contended:
                raise BlockingIOError
            bootstrap(self.root, platform=PLATFORM, run=self.host)
        future.bootstrap = begin
        # The real publication acquires these exact locks again. This fails if
        # the future trigger runs inside bootstrap's critical section.
        def run(command, **kwargs):
            if command[0] == sys.executable and command[-1] == 'promote':
                if fail_promotion:
                    raise subprocess.CalledProcessError(1, 'promotion')
                self.assertEqual(kwargs['timeout'], 200)
                with mock.patch.object(sys, 'argv', [entry[2], 'promote']):
                    future.main()
            elif command[0] == 'systemd-run' and command[-1] == 'publish':
                self.assertIn('TimeoutStartSec=180', command)
                self.assertEqual(kwargs['timeout'], 200)
                with selector.lock(self.root, 'operation', blocking=False), slots.admission_lock(self.root):
                    pass
                with mock.patch.dict(os.environ, {'INVOCATION_ID': 'synthetic-systemd-job'}), \
                     mock.patch.object(sys, 'argv', [entry[2], 'publish']):
                    future.main()
            else:
                raise AssertionError(command)
            return subprocess.CompletedProcess(command, 0)
        offset = [0]
        real_monotonic = time.monotonic
        def probe(*args, **kwargs):
            if slow_trial and not offset[0]:
                offset[0] = 65
                raise OSError('simulated delayed application readiness')
            return self.host.probe(*args, **kwargs)
        future.publish = lambda: publish(self.root, platform=PLATFORM, run=self.host,
            trial_options={'execute': self.host.execute, 'probe': probe, 'duration': 75})
        future.subprocess = types.SimpleNamespace(run=run)
        with mock.patch.object(sys, 'argv', [entry[2], entry[-1]]), \
             mock.patch.object(time, 'monotonic', side_effect=lambda: real_monotonic() + offset[0]):
            started = time.monotonic()
            future.main()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, budget)
            if slow_trial:
                self.assertGreater(elapsed, 60)  # Would exceed the original unit's limit.
        # Native systemd runs this only after the whole ExecStart succeeds.
        post = next(line.split('=', 1)[1].split() for line in unit.splitlines()
                    if line.startswith('ExecStartPost='))
        self.assertEqual(post[-1], 'complete')
        future.complete_bootstrap(self.root)
        return True

    def test_signed_future_ota_automatically_replaces_controller_and_survives_failed_trial(self):
        result = self.run_bootstrap('cloud')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.commit_application()
        slots.bootstrap(self.root, platform=PLATFORM, run=self.host)
        self.application_health = lambda *a: None
        first = selector.inspect(self.root, selector.SLOTS[0])
        future = self.signed_application('v0.3.14', 'c' * 40, 2,
            controller_version='1.3.1', controller_sequence=8, automatic=True)
        self.install_signed_application(future)  # Still installed by controller 1.3.0.
        self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[0])
        self.automatic_supervisor(slow_trial=True)
        self.assertFalse(self.automatic_supervisor())
        good = selector.inspect(self.root, selector.SLOTS[1])
        self.assertEqual((good['version'], good['generation']), ('1.3.1', 8))
        self.assertNotEqual(good['files'], first['files'])
        self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[1])
        shutil.rmtree(self.root / selector.RUN)
        self.assertEqual(selector.recover(self.root, self.host, execute=self.host.execute), selector.SLOTS[1])
        bad = self.signed_application('v0.3.15', 'd' * 40, 3, controller_version='1.3.2',
                                      controller_sequence=9, automatic=True, broken=True)
        self.install_signed_application(bad)
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.automatic_supervisor()
        self.assertEqual(selector.inspect(self.root, selector.SLOTS[1]), good)
        self.assertFalse((self.root / selector.SLOTS[0] / 'GOOD').exists())
        # The already-armed independent selector timer repairs the failed
        # trial at runtime, before the next simulated reboot.
        selector.check(self.root, self.host, probe=self.host.probe, execute=self.host.execute)
        self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[1])
        self.assertFalse(selector.selected(self.root)['trial'])
        shutil.rmtree(self.root / selector.RUN)
        self.assertEqual(selector.recover(self.root, self.host, execute=self.host.execute), selector.SLOTS[1])
        last = self.signed_application('v0.3.16', 'e' * 40, 4, controller_version='1.3.3',
                                       controller_sequence=10, automatic=True)
        pointer = self.install_signed_application(last)  # New controller installs another app.
        self.assertIn('v0.3.16-' + 'e' * 40, pointer)
        self.automatic_supervisor()
        self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[0])
        self.assertEqual(selector.inspect(self.root, selector.SLOTS[0])['generation'], 10)

    def test_failed_promotion_and_contended_retry_do_not_complete_application_trigger(self):
        result = self.run_bootstrap('cloud')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.commit_application()
        slots.bootstrap(self.root, platform=PLATFORM, run=self.host)
        slots.complete_bootstrap(self.root)
        previous = os.readlink(self.root / slots.COMPLETE)
        self.application_health = lambda *a: None
        future = self.signed_application('v0.3.14', 'c' * 40, 2,
            controller_version='1.3.1', controller_sequence=8, automatic=True)
        self.install_signed_application(future)
        with self.assertRaises(subprocess.CalledProcessError):
            self.automatic_supervisor(fail_promotion=True)
        self.assertEqual(os.readlink(self.root / slots.COMPLETE), previous)
        with self.assertRaises(SystemExit) as error:
            self.automatic_supervisor(contended=True)
        self.assertEqual(error.exception.code, 75)
        self.assertEqual(os.readlink(self.root / slots.COMPLETE), previous)
        self.assertTrue(self.automatic_supervisor())
        self.assertEqual(selector.inspect(self.root, selector.SLOTS[1])['version'], '1.3.1')
        self.assertFalse(self.automatic_supervisor())

    def voice_runtime(self):
        """Import only the installed application in a separate interpreter."""
        release = self.root / os.readlink(self.root / 'opt/messagebox').lstrip('/')
        program = r'''
import sys,types
from pathlib import Path
sys.modules['gpiozero']=types.SimpleNamespace(Button=object,LED=object)
from messagebox import button_send,cloud_runtime,cloud_setup,sound_pack,speaker
from messagebox.settings import SettingsStore
root=Path(sys.argv[1]); sounds=Path(sys.argv[2])/'sounds'
settings=SettingsStore(root/'var/lib/messagebox-settings/settings.json',environ={'MSGBOX_TRANSPORT':'cloud'})
assert settings.load()[0]['voice_pack']=='buttonbox'
sound_pack.validate_sounds(sounds)
speaker.LOCK_FILE=root/'run/messagebox-speaker/speaker.lock'
guide=cloud_setup.SetupGuide(root/'var/lib/messagebox-cloud/guided-setup.json')
fresh=guide.load() is None
guide.change('start'); played=[]
def play(name):
    with speaker.reservation():
        assert (sounds/'voices/buttonbox'/('voice-'+name+'.wav')).is_file()
        played.append(name)
assert guide.speak(play)==fresh
assert played==(['setup-scan'] if fresh else [])
assert not cloud_setup.SetupGuide(guide.path).speak(play)
'''
        env = dict(os.environ, PYTHONPATH=str(release), PYTHONDONTWRITEBYTECODE='1', MSGBOX_TRANSPORT='cloud')
        result = subprocess.run([sys.executable,'-c',program,str(self.root),str(release)],
                                cwd=self.base,env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_bench_setup_to_voice_runtime_later_signed_ota_and_rollback(self):
        self.fresh_setup()
        result = self.run_bootstrap('cloud')
        self.assertEqual(result.returncode,0,result.stderr)
        self.host = Host(self.root)
        self.host.recovery_run = self.box.run
        first = self.signed_application('v0.3.13','a'*40,1)
        old_verifier = load_frozen('v0.3.12', 'scripts/install/release_verify.py')
        with self.assertRaisesRegex(ValueError, 'incompatible'):
            old_verifier.verify_bundle(first, self.root/'etc/button-box-update/release-keys',
                                       version='v0.3.13', channel='stable', floor=0, platform=PLATFORM)
        self.application_health = lambda *a: None  # Pi health is a host boundary.
        with self.installed_controller(selector.SLOTS[0]) as controller:
            bundle = mkdir(controller.STATE / 'downloads' / ('c'*32),0o700)
            for path in first.iterdir():
                write(bundle/path.name,path.read_bytes(),0o600)
            self.box.queue_bundle(controller,bundle,'v0.3.13')
            controller.worker()
            self.assertEqual(controller.read('journal.json')['phase'],'committed')
        slots.bootstrap(self.root,platform=PLATFORM,run=self.host)
        self.assertEqual(selector.selected(self.root)['slot'],selector.SLOTS[0])
        self.assertEqual(selector.inspect(self.root,selector.SLOTS[0])['version'],'1.3.0')
        self.assertIn(['systemctl','enable','--now','messagebox-update-selector.timer'],self.host.commands)
        self.voice_runtime()
        before = os.readlink(self.root/'opt/messagebox')
        routes = {p:(self.root/p).read_bytes() for p in slots.routes()}
        later = self.signed_application('v0.3.14','c'*40,2)
        # Drive a later OTA through the permanent A/B install route. Kill it
        # after pointer switch; selector recovery must restore the first app.
        def interrupted(root,journal,run):
            self.assertNotEqual(os.readlink(root/'opt/messagebox'),before)
            raise KeyboardInterrupt('power loss during application probation')
        self.application_health = interrupted
        with self.installed_controller(selector.selected(self.root)['slot']) as controller:
            bundle = mkdir(controller.STATE/'downloads'/('d'*32),0o700)
            for path in later.iterdir():
                write(bundle/path.name,path.read_bytes(),0o600)
            self.box.queue_bundle(controller,bundle,'v0.3.14')
            with mock.patch.object(sys,'argv',sys.argv[:]), self.assertRaises(KeyboardInterrupt):
                exec(selector.install_program(self.root,selector.SLOTS[0]),{})
            self.assertEqual(controller.read('journal.json')['phase'],'probation')
        shutil.rmtree(self.root/selector.RUN)
        self.assertEqual(selector.recover(self.root,self.host,execute=self.host.execute),selector.SLOTS[0])
        self.assertEqual(os.readlink(self.root/'opt/messagebox'),before)
        result = self.box.worker.read_state(self.root,'result.json')
        self.assertEqual((result['state'],result['software_version']),('rolled_back','v0.3.13'))
        self.assertFalse((self.root/'var/lib/button-box-update/install-inhibit').exists())
        self.assertEqual(self.box.worker.read_state(self.root,'sequence.json'),2)
        self.assertIn('/opt/messagebox-releases/v0.3.14-'+'c'*40,
                      self.box.worker.read_state(self.root,'quarantine.json'))
        self.assertEqual({p:(self.root/p).read_bytes() for p in routes},routes)
        self.voice_runtime()

    def commit_application(self):
        # Use the current worker actually installed by the complete shell, not
        # FieldBox's frozen worker. Only systemd, disk capacity and health are
        # host boundaries; the application transaction and retained proof are real.
        spec = importlib.util.spec_from_file_location('first_installed_worker',
            self.root / selector.SLOTS[0] / 'bounded_update.py')
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        worker._boot, worker.time, worker.shutil = self.box.worker._boot, self.box.worker.time, self.box.worker.shutil
        self.box.worker = worker
        for name in selector.MODULES:
            write(self.box.source / 'scripts/install' / name, (self.repo / 'scripts/install' / name).read_bytes())
        for name in ('controller_slots.py', 'controller_selector.py'):
            write(self.box.source / 'messagebox' / name, (ROOT / 'messagebox' / name).read_bytes())
        raw = build.controller_descriptor(self.repo, 7, 'v0.3.13')
        write(self.box.source / slots.verify.CONTROLLER_DESCRIPTOR, raw)
        document = write(self.base / 'descriptor', raw)
        signature = self.base / 'signature'
        subprocess.run(['minisign', '-S', '-m', str(document), '-s', str(self.keys[0][1]),
                        '-x', str(signature)], check=True, capture_output=True)
        write(self.box.source / slots.verify.CONTROLLER_SIGNATURE, (repr(signature.read_text()) + '\n').encode())
        self.box.refresh()
        self.box.install()
        release = self.root / os.readlink(self.root / 'opt/messagebox').lstrip('/')
        self.assertEqual(worker.read_state(self.root, 'journal.json')['kind'], 'application')
        self.assertEqual(worker.read_state(self.root, 'journal.json')['phase'], 'committed')
        self.host = Host(self.root)
        self.host.recovery_run = self.box.run
        return release

    def test_complete_first_install_commits_application_initializes_slots_and_selects(self):
        result = self.run_bootstrap('cloud')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'etc/button-box-update/installed').exists())
        self.assertTrue((self.root / 'opt/messagebox').is_symlink())
        self.assertEqual(json.loads((self.root / 'var/lib/button-box-update/journal.json').read_bytes())['phase'], 'committed')
        for source in (ROOT / 'systemd').glob('messagebox-update-*'):
            for path in ([source] if source.is_file() else source.glob('*.conf')):
                target = self.root / 'etc/systemd/system' / path.relative_to(ROOT / 'systemd')
                self.assertEqual(target.read_bytes(), path.read_bytes())
                self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        commands = [json.loads(line) for line in self.log.read_text().splitlines()]
        arm = commands.index(['systemctl','enable','--now','messagebox-update-recovery.service'])
        convert = next(i for i,c in enumerate(commands) if c[0]=='python3' and c[-1]=='bootstrap')
        self.assertLess(arm, convert)
        self.assertIn('messagebox-update-discovery.timer', commands[-2])
        for name in selector.MODULES:
            self.assertEqual((self.root / selector.SLOTS[0] / name).read_bytes(),
                             (ROOT / 'scripts/install' / name).read_bytes())
        self.commit_application()
        slots.bootstrap(self.root, platform=PLATFORM, run=self.host)
        record = selector.inspect(self.root, selector.SLOTS[0])
        self.assertFalse(record['legacy'])
        self.assertEqual((record['version'], record['generation']), ('1.3.0', 7))
        floor = selector.document(selector.read(self.root, slots.STATE + '/floor.json', mode=0o600))
        self.assertEqual(floor['value'], {'sequence': 7, 'digest': record['descriptor'], 'version': '1.3.0'})
        for path, raw in slots.routes().items():
            self.assertEqual((self.root / path).read_bytes(), raw)
        self.assertIn(['systemctl','enable','--now','messagebox-update-selector.timer'], self.host.commands)
        self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[0])
        # A reboot uses only GOOD, with release trust deliberately unavailable.
        (self.root / 'etc/button-box-update/release-keys').unlink()
        shutil.rmtree(self.root / selector.RUN)
        self.assertEqual(selector.recover(self.root, self.host, execute=self.host.execute), selector.SLOTS[0])
        self.assertEqual(self.host.recoveries, [selector.SLOTS[0]])

    def test_signed_initial_controller_rejects_unproved_modules_and_replays_before_routing(self):
        result = self.run_bootstrap('cloud')
        self.assertEqual(result.returncode, 0, result.stderr)
        release = self.commit_application()
        before = self.base / 'before-slots'
        shutil.copytree(self.root, before, symlinks=True)
        cases = ('module', 'mode', 'missing-signature', 'wrong-key', 'retained-module',
                 'replay', 'equivocation', 'downgrade', 'damaged-floor')
        for case in cases:
            with self.subTest(case=case):
                shutil.rmtree(self.root)
                shutil.copytree(before, self.root, symlinks=True)
                self.host.commands.clear()
                if case == 'module':
                    with (self.root / selector.SLOTS[0] / 'bounded_update.py').open('ab') as stream:
                        stream.write(b'\n# changed installed controller\n')
                elif case == 'mode':
                    (self.root / selector.SLOTS[0] / 'bounded_update.py').chmod(0o666)
                elif case == 'missing-signature':
                    (release / slots.verify.CONTROLLER_SIGNATURE).unlink()
                elif case == 'wrong-key':
                    keys = (self.root / 'etc/button-box-update/release-keys').resolve()
                    for key in keys.glob('*.pub'):
                        key.unlink()
                    write(keys / 'test.pub', self.keys[1][0].read_bytes())
                elif case == 'retained-module':
                    (release / 'updater/bounded_update.py').write_bytes(b'# wrong retained bytes\n')
                else:
                    raw = (release / slots.verify.CONTROLLER_DESCRIPTOR).read_bytes()
                    floor = {'sequence': 8 if case == 'replay' else 7, 'digest': selector.digest(raw), 'version': '1.3.0'}
                    if case == 'equivocation':
                        floor['digest'] = '0' * 64
                    if case == 'downgrade':
                        floor.update(sequence=6, version='1.3.1')
                    write(self.root / slots.STATE / 'floor.json',
                          b'broken' if case == 'damaged-floor' else selector.envelope(floor), 0o600)
                with self.assertRaises(ValueError):
                    slots.bootstrap(self.root, platform=PLATFORM, run=self.host)
                self.assertEqual(selector.valid(self.root), [])
                self.assertFalse(any((self.root / p).exists() for p in slots.routes()))
                self.assertNotIn(['systemctl','enable','--now','messagebox-update-selector.timer'], self.host.commands)

    def test_signed_initialization_write_cuts_retry_with_durable_floor_and_routes(self):
        result = self.run_bootstrap('cloud')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.commit_application()
        before = self.base / 'before-slots'
        shutil.copytree(self.root, before, symlinks=True)
        events = []
        slots.bootstrap(self.root, platform=PLATFORM, run=self.host, step=lambda e,p: events.append((e,p)))
        for index in range(len(events)):
            shutil.rmtree(self.root)
            shutil.copytree(before, self.root, symlinks=True)
            disk = DurableView(self.root)
            count = 0
            def cut(event, path):
                nonlocal count
                disk.step(event, path)
                count += 1
                if count == index + 1:
                    raise KeyboardInterrupt('initialization cut')
            with self.assertRaises(KeyboardInterrupt):
                slots.bootstrap(self.root, platform=PLATFORM, run=self.host, step=cut)
            # Retry after process death, then discard all unflushed writes.
            slots.bootstrap(self.root, platform=PLATFORM, run=self.host, step=disk.step)
            disk.power_loss()
            mkdir(self.root / 'run/lock')
            self.host = Host(self.root)
            self.host.recovery_run = self.box.run
            slots.bootstrap(self.root, platform=PLATFORM, run=self.host)
            record = selector.inspect(self.root, selector.SLOTS[0])
            floor = selector.document(selector.read(self.root, slots.STATE + '/floor.json', mode=0o600))
            self.assertEqual((floor['value']['sequence'], floor['value']['digest']), (7, record['descriptor']))
            self.assertEqual(selector.recover(self.root, self.host, execute=self.host.execute), selector.SLOTS[0])
        print('Signed initial-controller cuts:', len(events))

    def test_wacli_first_install_refuses_before_controller_mutation(self):
        result = self.run_bootstrap('wacli')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires a Cloud box', result.stderr)
        self.assertFalse((self.root / 'usr/lib/messagebox-update').exists())
        self.assertFalse((self.root / 'opt/messagebox').is_symlink())
