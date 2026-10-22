"""Run the full bootstrap shell with a temp filesystem and explicit OS boundaries.

The shell, globs, install(1), Python key/policy code, and layout conversion are
real. Root ownership, Pi identity, accounts, and systemd are host substitutes.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from field_updater import FieldBox, mkdir, write
from messagebox import controller_selector as selector, controller_slots as slots
import test_controller_descriptor as descriptors
from test_controller_slots import DurableView, Host
from test_release_tooling import build
from test_update_discovery import PLATFORM

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
        for name in ('systemd', 'config/release-keys', 'config/emergency-keys'):
            shutil.copytree(ROOT / name, self.repo / name)
        shutil.rmtree(self.repo / 'config/release-keys')
        write(self.repo / 'config/release-keys/test.pub', self.keys[0][0].read_bytes())
        for path in (ROOT / 'scripts/install').glob('*.py'):
            write(self.repo / 'scripts/install' / path.name, path.read_bytes())
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
