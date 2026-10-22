"""A/B publication cuts, real frozen controllers, and process boundaries.

Systemd and physical application health remain explicit host boundaries.
"""
import contextlib
import importlib.util
import io
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from field_updater import FieldBox, write
from messagebox import controller_selector as selector
from messagebox import controller_slots as slots
import test_controller_descriptor as descriptors
from test_release_tooling import build
from test_update_discovery import PLATFORM, verify

ROOT = Path(__file__).resolve().parents[1]


class Host:
    def __init__(self, root):
        self.root = root
        if not (root / 'proc/sys/kernel/random/boot_id').exists():
            write(root / 'proc/sys/kernel/random/boot_id', b'synthetic-boot\n', 0o444)
        stat_path = root / ('proc/' + str(os.getpid()) + '/stat')
        if not stat_path.exists():
            write(stat_path, (str(os.getpid()) + ' (test) S ' + '0 ' * 18 + '123\n').encode(), 0o444)
        self.commands = []
        self.fail_recovery = set()
        self.probes = 0
        self.not_ready = 0
        self.fresh = True
        self.self_test_timeout = None
        self.install_active = False
        self.health_observed = ''
        write(root / 'run/messagebox-audio/audio.env',
              b'MSGBOX_MIC_DEV=plughw:CARD=mic,DEV=0\nMSGBOX_SPK_DEV=plughw:CARD=Device,DEV=0\n')
        self.real_run = subprocess.run

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == 'systemctl':
            if command[1] == 'show':
                if '--property=ExecStart' in command:
                    path = 'etc/systemd/system/' + command[2] + '.d/' + slots.ROUTE
                    argument = slots.routes()[path].decode().split('ExecStart=')[-1].splitlines()[0]
                    text = 'ExecStart={ path=/usr/bin/python3 ; argv[]=' + argument + ' ; ignore_errors=no ; }'
                elif '--value' in command:
                    text = self.install_active if isinstance(self.install_active, str) else ('active' if self.install_active else 'inactive')
                elif '--property=MainPID' in command:
                    text = 'MainPID=' + str(os.getpid())
                else:
                    text = 'ActiveState=inactive\nMainPID=0\nControlPID=0\n'
            else:
                text = ''
            return subprocess.CompletedProcess(command, 0, text)
        if command[0] == 'systemd-run':
            if self.self_test_timeout and command[-1] == 'self-test':
                kwargs['timeout'] = self.self_test_timeout
            # Run the genuine offline poll/signature/extraction path. Native
            # systemd restrictions are a separate Linux acceptance check.
            return self.real_run(command[command.index('--') + 1:], **kwargs)
        return self.real_run(command, **kwargs)

    def execute(self, root, slot, arguments, timeout=180):
        if arguments[0] == 'bounded_update.py':
            if arguments[1] == 'supervise':
                return b''
            if slot in self.fail_recovery:
                raise ValueError('injected application recovery failure')
            return b''
        if arguments[0] == 'health-probe.py':
            spec = importlib.util.spec_from_file_location('slot_health', root / slot / 'health-probe.py')
            health = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(health)
            journal = json.loads((root / 'var/lib/button-box-update/journal.json').read_bytes())
            def health_run(command, **kwargs):
                if command[0] == 'systemctl':
                    state = journal['units'].get(command[2], {'active': 'active'})
                    return subprocess.CompletedProcess(command, 0, 'ActiveState=' + state['active'] +
                        '\nMainPID=' + str(os.getpid()) + '\nNRestarts=0\n')
                if command[0] == 'runuser':
                    card = '2: mic' if 'arecord' in command else '3: Device'
                    return subprocess.CompletedProcess(command, 0, 'card ' + card + ' [USB Audio], device 0: synthetic\n')
                raise AssertionError(command)
            output = io.StringIO()
            with mock.patch.object(health.update, '_boot', return_value=selector.boot_id(root)), contextlib.redirect_stdout(output):
                result = health.main(['--current'], root=root, run=health_run)
            self.health_observed = output.getvalue()
            if result:
                raise ValueError('actual health probe rejected: ' + self.health_observed)
            return self.health_observed.encode()
        return selector.execute(root, slot, arguments, timeout)

    def probe(self, root, value, run):
        self.probes += 1
        if self.probes <= self.not_ready:
            raise OSError('stale or delayed broker')
        if not self.fresh:
            return  # A success result alone must not create GOOD.
        context = selector.document(selector.read(root, 'run/messagebox-update/transaction.json'))
        # Run the actual broker evidence path after /run was cleared. Only
        # device identity and the kernel/process boundary use synthetic values.
        from test_update_approval import controller
        app = {k: context[k] for k in ('software_version', 'software_commit')}
        with mock.patch.object(controller, 'RUN', root / 'run/messagebox-update'), \
                mock.patch.object(controller, 'installed_identity', return_value=app), \
                mock.patch.object(controller.update, '_boot', return_value=context['boot']), \
                mock.patch.object(controller.update, '_process_token', return_value='123'):
            broker = controller.Controller()
            for name in ('button', 'heartbeat'):
                broker.evidence(name, app, os.getpid())



@unittest.skipUnless(shutil.which('minisign'), 'real minisign required')
class SlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        descriptors.ControllerProofTests.setUpClass()
        cls.keys = descriptors.ControllerProofTests.key_pairs

    @classmethod
    def tearDownClass(cls):
        descriptors.ControllerProofTests.tearDownClass()

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='slots-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.box = FieldBox(self.base / 'field')
        self.box.install()
        self.root = self.box.root
        write(self.root / 'proc/sys/kernel/random/boot_id', b'synthetic-boot\n', 0o444)
        self.host = Host(self.root)
        self.api = selector
        self.stage()
        slots.bootstrap(self.root, run=self.host)
        self.old = self.slot_bytes(selector.SLOTS[0])

    def stage(self, version='1.3.0', sequence=7, *, hang=False, broken_discovery=False):
        release = self.root / os.readlink(self.root / 'opt/messagebox').lstrip('/')
        source = self.base / 'candidate'
        for name in selector.MODULES:
            raw = (ROOT / 'scripts/install' / name).read_bytes()
            if name == 'release_verify.py':
                raw = raw.replace(b'UPDATER_VERSION = (1, 3, 0)', ('UPDATER_VERSION = ' + str(tuple(map(int, version.split('.'))))).encode())
            if broken_discovery and name == 'update_discovery.py':
                raw = raw.replace(b"return {'protocol': 1, 'dry_run': True}", b"return {'protocol': 1, 'dry_run': False}")
            if hang and name == 'update_controller.py':
                raw = raw.replace(b'def controller_self_test():', ("def controller_self_test():\n    Path(" + repr(str(self.root / 'run/hung-trial')) + ").write_text('hung')\n    import time; time.sleep(60)").encode())
            write(source / 'scripts/install' / name, raw)
            write(release / 'updater' / name, raw)
        with mock.patch.object(build, 'CONTROLLER_VERSION', version):
            raw = build.controller_descriptor(source, sequence, 'v0.3.13')
        write(release / verify.CONTROLLER_DESCRIPTOR, raw)
        message = write(self.base / 'descriptor', raw)
        signature = self.base / 'signature'
        subprocess.run(['minisign', '-S', '-s', str(self.keys[0][1]), '-m', str(message), '-x', str(signature)], check=True, capture_output=True)
        write(release / verify.CONTROLLER_SIGNATURE, (repr(signature.read_text()) + '\n').encode())
        write(self.root / 'etc/button-box-update/release-keys/release.pub', self.keys[0][0].read_bytes())
        emergency = self.root / 'etc/button-box-update/emergency-keys/emergency.pub'
        if not emergency.exists():
            write(emergency, self.keys[2][0].read_bytes())
        # Synthetic FieldBox commit is v0.3.13 and uses a complete format-1 journal.
        return release

    def slot_bytes(self, slot):
        return {p.relative_to(self.root / slot).as_posix(): p.read_bytes()
                for p in (self.root / slot).rglob('*') if p.is_file()}

    def publish(self, **kwargs):
        return slots.publish(self.root, run=self.host, platform=PLATFORM,
                             trial_options={'execute': self.host.execute, 'probe': self.host.probe, 'duration': 2}, **kwargs)

    def boot(self, *, excluded=()):
        shutil.rmtree(self.root / selector.RUN, ignore_errors=True)
        return selector.recover(self.root, self.host, excluded=excluded, execute=self.host.execute)

    def test_success_repeated_failures_and_generations_keep_working_proof(self):
        first = self.publish()
        active = selector.selected(self.root)['slot']
        proof = self.slot_bytes(active)
        self.assertEqual(first['version'], '1.3.0')
        for version, seq in (('1.3.1', 8), ('1.3.2', 9)):
            self.stage(version, seq)
            self.host.fresh = False
            with self.assertRaisesRegex(ValueError, 'trial failed'):
                self.publish()
            self.assertEqual(self.boot(), active)
            self.assertEqual(self.slot_bytes(active), proof)
        self.stage('1.3.3', 10)
        self.host.fresh = True
        self.assertGreater(self.publish()['generation'], first['generation'])
        self.assertNotEqual(self.boot(), active)

    def test_good_is_self_contained_without_retained_admission_proof_files(self):
        self.publish()
        for name in ('controller_descriptor.py', 'controller_signature.py'):
            (self.root / selector.SLOTS[1] / name).unlink()
        self.assertEqual(self.boot(), selector.SLOTS[1])
        selector.inspect(self.root, selector.SLOTS[1])

    def test_same_accepted_descriptor_is_idempotent_and_preserves_spare(self):
        first = self.publish()
        spare = self.slot_bytes(selector.SLOTS[0])
        self.host.fresh = False  # A repeated trial would fail and consume A.
        self.assertEqual(self.publish(), first)
        self.assertEqual(self.slot_bytes(selector.SLOTS[0]), spare)
        self.assertFalse((self.root / selector.RUN / 'trial.json').exists())

    def test_fallback_then_failed_replacement_cannot_reuse_a_commit_generation(self):
        first = self.publish()
        self.assertEqual(self.boot(excluded=(selector.SLOTS[1],)), selector.SLOTS[0])
        self.stage('1.3.1', 8)
        self.host.fresh = False
        with self.assertRaises(ValueError):
            self.publish()
        self.boot()
        self.stage('1.3.2', 9)
        self.host.fresh = True
        replacement = self.publish()
        self.assertGreater(replacement['generation'], first['generation'])
        self.assertEqual(replacement['generation'], 9)

    def test_each_bad_good_only_disables_its_slot_and_newest_generation_wins(self):
        self.publish()
        snapshot = self.base / 'snapshot'
        shutil.copytree(self.root, snapshot, symlinks=True)
        for bad in selector.SLOTS:
            for damage in ('missing', 'truncated', 'corrupt', 'hash', 'mode', 'link', 'shape', 'deep'):
                with self.subTest(bad=bad, damage=damage):
                    shutil.rmtree(self.root)
                    shutil.copytree(snapshot, self.root, symlinks=True)
                    path = self.root / bad / 'GOOD'
                    if damage == 'deep':
                        path.write_bytes(b'[' * 1100 + b'0' + b']' * 1100)
                    elif damage == 'shape':
                        value = selector.document(path.read_bytes())['value']
                        value['files'] = list(selector.MODULES)
                        path.write_bytes(selector.envelope(value))
                    elif damage == 'missing':
                        path.unlink()
                    elif damage in {'truncated', 'corrupt'}:
                        path.write_bytes(b'{' if damage == 'truncated' else b'{}')
                    elif damage == 'hash':
                        (self.root / bad / 'bounded_update.py').write_bytes(b'changed')
                    elif damage == 'mode':
                        path.chmod(0o666)
                    else:
                        path.unlink()
                        path.symlink_to(snapshot / bad / 'GOOD')
                    self.assertEqual(self.boot(), next(s for s in selector.SLOTS if s != bad))
        shutil.rmtree(self.root)
        shutil.copytree(snapshot, self.root, symlinks=True)
        self.assertEqual(self.boot(), selector.SLOTS[1])
        for s in selector.SLOTS:
            (self.root / s / 'GOOD').unlink()
        with self.assertRaisesRegex(ValueError, 'no working'):
            self.boot()

    def test_failed_trial_consumed_timer_never_retries_and_key_rotation_does_not_gate_boot(self):
        self.host.fresh = False
        with self.assertRaisesRegex(ValueError, 'trial failed'):
            self.publish()
        self.assertFalse((self.root / selector.SLOTS[1] / 'GOOD').exists())
        selector.check(self.root, self.host, probe=lambda *a: None, execute=self.host.execute)
        self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[0])
        with self.assertRaisesRegex(ValueError, 'consumed'):
            self.publish()
        (self.root / 'etc/button-box-update/release-keys').unlink()
        self.assertEqual(self.boot(), selector.SLOTS[0])
        self.assertEqual(self.slot_bytes(selector.SLOTS[0]), self.old)

    def test_boot_selection_needs_no_persistent_writes_full_blocks_or_inodes(self):
        self.publish()
        original = os.open
        def full(path, flags, *args, **kwargs):
            if flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR) and str(self.root / 'run') not in str(path):
                raise OSError(errno.ENOSPC, 'full blocks and inodes')
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(os, 'open', side_effect=full), mock.patch.object(os, 'fsync', side_effect=AssertionError('persistent fsync')):
            self.assertEqual(self.boot(), selector.SLOTS[1])
        # Application recovery was an explicit boundary: its frozen terminal
        # writes can still fail on a truly full filesystem.

    def test_floor_replay_equal_digest_and_downgrade_and_bad_floor_does_not_gate_boot(self):
        self.publish()
        for version, seq in (('1.3.1', 6), ('1.2.9', 8)):
            self.stage(version, seq)
            with self.assertRaisesRegex(ValueError, 'replay or downgrade'):
                self.publish()
        self.stage('1.3.1', 7)
        with self.assertRaisesRegex(ValueError, 'replay or downgrade'):
            self.publish()
        (self.root / slots.STATE / 'floor.json').write_bytes(b'broken')
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.boot(), selector.SLOTS[1])

    def test_both_real_controller_parsers_accept_shared_state_and_reject_incompatible_candidate(self):
        self.publish()
        for slot in selector.SLOTS:
            slots.compatible(self.root, slot, selector)
        target = self.root / selector.SLOTS[0] / 'bounded_update.py'
        target.write_bytes(target.read_bytes() + b'\ndef _validate_journal(*a): raise ValueError("broken parser")\n')
        with self.assertRaises(subprocess.CalledProcessError):
            slots.compatible(self.root, selector.SLOTS[0], selector)

    def test_recovery_failure_selects_other_and_delayed_trial_readiness_retries(self):
        self.host.not_ready = 1
        self.publish()
        self.assertGreater(self.host.probes, 1)
        self.host.fail_recovery.add(selector.SLOTS[1])
        self.assertEqual(self.boot(), selector.SLOTS[0])

    def test_admission_capacity_preserves_spare_and_accepts_sufficient_blocks_and_inodes(self):
        from types import SimpleNamespace
        self.publish()
        spare = self.slot_bytes(selector.SLOTS[0])
        self.stage('1.3.1', 8)
        for blocks, inodes in ((0, 128), (100000, 0)):
            capacity = SimpleNamespace(f_bavail=blocks, f_frsize=4096, f_favail=inodes)
            with mock.patch.object(os, 'statvfs', return_value=capacity), self.assertRaisesRegex(ValueError, 'controller capacity'):
                self.publish()
            self.assertEqual(self.slot_bytes(selector.SLOTS[0]), spare)
        with mock.patch.object(os, 'statvfs', return_value=SimpleNamespace(f_bavail=100000, f_frsize=4096, f_favail=128)):
            self.assertEqual(self.publish()['version'], '1.3.1')

    def test_io_faults_in_publication_keep_the_unchanged_working_slot(self):
        for operation in ('write', 'file_fsync', 'rename', 'directory_fsync'):
            for error in (errno.ENOSPC, errno.EIO):
                with self.subTest(operation=operation, error=error):
                    def fault(event, path):
                        if event == operation:
                            raise OSError(error, 'injected storage fault')
                    with self.assertRaises(OSError):
                        self.publish(step=fault)
                    self.assertEqual(self.boot(), selector.SLOTS[0])
                    self.assertEqual(self.slot_bytes(selector.SLOTS[0]), self.old)

    def test_bounded_child_hang_and_nonzero_exit_are_reaped(self):
        slot = selector.SLOTS[1]
        write(self.root / slot / 'hang.py', b'import time\ntime.sleep(60)\n')
        with self.assertRaises(subprocess.TimeoutExpired):
            selector.execute(self.root, slot, ('hang.py',), 0.05)
        write(self.root / slot / 'exit.py', b'import os\nos._exit(17)\n')
        with self.assertRaises(ValueError):
            selector.execute(self.root, slot, ('exit.py',), 1)

    def test_every_publication_and_trial_write_cut_keeps_an_accepted_slot(self):
        image = self.base / 'before'
        shutil.copytree(self.root, image, symlinks=True)
        events = []
        self.publish(step=lambda event, path: events.append((event, path)))
        for index in range(len(events)):
            shutil.rmtree(self.root)
            shutil.copytree(image, self.root, symlinks=True)
            count = 0
            def cut(event, path):
                nonlocal count
                count += 1
                if count == index + 1:
                    raise KeyboardInterrupt('power cut')
            with self.assertRaises(KeyboardInterrupt):
                self.publish(step=cut)
            chosen = self.boot()
            self.assertIn(chosen, selector.SLOTS)
            selector.inspect(self.root, chosen)
            # Also model a lost/torn final GOOD dirent. The retained proof
            # never changes, so its slot is usable without repair.
            if (self.root / selector.SLOTS[1] / 'GOOD').exists():
                (self.root / selector.SLOTS[1] / 'GOOD').write_bytes(b'{')
                self.assertEqual(self.boot(), selector.SLOTS[0])
            self.assertEqual(self.slot_bytes(selector.SLOTS[0]), self.old)
        self.assertTrue({'write', 'file_fsync', 'rename', 'directory_fsync', 'unlink_GOOD'} <= {e for e,p in events})
        print('A/B publication cuts:', len(events))

    def test_bootstrap_cuts_only_route_to_the_flushed_initial_slot(self):
        shutil.rmtree(self.root / 'usr/lib/messagebox-selector')
        shutil.rmtree(self.root / slots.STATE)
        shutil.rmtree(self.root / selector.RUN)
        (self.root / selector.SLOTS[0] / 'GOOD').unlink()
        for path in slots.routes():
            target = self.root / path
            target.unlink()
            if not any(target.parent.iterdir()):
                target.parent.rmdir()
        before = self.base / 'bootstrap-before'
        shutil.copytree(self.root, before, symlinks=True)
        events = []
        slots.bootstrap(self.root, run=self.host, step=lambda e,p: events.append((e,p)))
        for index in range(len(events)):
            shutil.rmtree(self.root)
            shutil.copytree(before, self.root, symlinks=True)
            count = 0
            def cut(e,p):
                nonlocal count
                count += 1
                if count == index + 1:
                    raise KeyboardInterrupt('bootstrap cut')
            with self.assertRaises(KeyboardInterrupt):
                slots.bootstrap(self.root, run=self.host, step=cut)
            if any((self.root / p).exists() for p in slots.routes()):
                self.assertTrue((self.root / selector.HOME).exists())
                self.assertEqual(selector.valid(self.root)[0][0], selector.SLOTS[0])
            slots.bootstrap(self.root, run=self.host)
            self.assertEqual(self.boot(), selector.SLOTS[0])

    def test_timer_during_live_trial_and_two_process_publication_exclusion(self):
        ready_r, ready_w = os.pipe()
        code = ('import sys,os,time;sys.path.insert(0,' + repr(str(ROOT)) + ');'
                'from messagebox import controller_selector as s;from pathlib import Path;'
                'c=s.lock(Path(' + repr(str(self.root)) + '),"operation");c.__enter__();'
                'os.write(' + str(ready_w) + ',b"ready");time.sleep(60)')
        child = subprocess.Popen([sys.executable, '-I', '-c', code], pass_fds=(ready_w,))
        os.close(ready_w)
        try:
            self.assertEqual(os.read(ready_r, 5), b'ready')
            self.assertIsNone(selector.check(self.root, self.host, probe=lambda *a: self.fail('overlapping check'), execute=self.host.execute))
            with self.assertRaises(BlockingIOError):
                self.publish()
        finally:
            child.kill()
            child.wait(timeout=5)
            os.close(ready_r)
        self.assertEqual(self.boot(), selector.SLOTS[0])

    def test_runtime_fallback_runs_frozen_app_rollback_and_restarts_app_services(self):
        self.box.data.update(version='v0.3.14', commit='b' * 40)
        self.box.save_manifest()
        def cut(*args):
            raise KeyboardInterrupt('actual install cut before health commit')
        with self.assertRaises(KeyboardInterrupt):
            self.box.worker.install_transaction(self.box.source, self.box.manifest, root=self.root,
                    run=self.box.run, sequence=2, health=cut)
        observed = []
        def execute(root, slot, arguments, timeout=180):
            observed.append(arguments)
            self.box.worker.recover_transaction(root=root, run=self.box.run, start='--boot' not in arguments)
            return b''
        with mock.patch.object(selector.time, 'monotonic', side_effect=range(0, 1000, 10)):
            selector.check(self.root, self.host, probe=mock.Mock(side_effect=OSError('dead broker')), execute=execute)
        self.assertEqual(observed, [('bounded_update.py', 'recover')])
        self.assertEqual(self.box.worker.read_state(self.root, 'journal.json')['phase'], 'rolled_back')
        for unit in ('messagebox-button.service', 'messagebox-poller.service'):
            self.assertEqual(self.box.states[unit]['active'], 'active')

    def test_timer_restart_waits_at_gate_and_resolves_only_the_new_generation(self):
        import select
        self.publish()
        started_r, started_w = os.pipe()
        resolved_r, resolved_w = os.pipe()
        children = []
        code = ("import sys,os,importlib.util\nfrom pathlib import Path\n"
                "p=Path(" + repr(str(self.root / selector.HOME)) + ")\n"
                "spec=importlib.util.spec_from_file_location('selector',p)\ns=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(s)\n"
                "os.write(" + str(started_w) + ",b'ready')\n"
                "s.os.execv=lambda binary,args:os.write(" + str(resolved_w) + ",s.canonical(args))\n"
                "s.run_consumer(Path(" + repr(str(self.root)) + "),'supervisor')\n")
        def run(command, **kwargs):
            result = self.host(command, **kwargs)
            if command[:2] == ['systemctl', 'stop'] and not children:
                children.append(subprocess.Popen([sys.executable, '-I', '-B', '-c', code], pass_fds=(started_w, resolved_w)))
                self.assertTrue(select.select([started_r], [], [], 5)[0])
                self.assertEqual(os.read(started_r, 5), b'ready')
                self.assertFalse(select.select([resolved_r], [], [], 0.1)[0])
            return result
        try:
            with selector.lock(self.root, 'operation'):
                self.assertEqual(selector.recover(self.root, run, excluded=(selector.SLOTS[1],), execute=self.host.execute), selector.SLOTS[0])
            self.assertTrue(select.select([resolved_r], [], [], 5)[0])
            arguments = json.loads(os.read(resolved_r, 8192))
            self.assertIn(str(self.root / selector.SLOTS[0] / 'bounded_update.py'), arguments)
            self.assertEqual(children[0].wait(timeout=5), 0)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
            for fd in (started_r, started_w, resolved_r, resolved_w):
                os.close(fd)

    def test_real_supervisor_start_is_independent_of_publication_and_worker_locks(self):
        import select
        ready_r, ready_w = os.pipe()
        code = ("import sys,os,importlib.util\nfrom pathlib import Path\n"
                "p=Path(" + repr(str(self.root / selector.HOME)) + ")\n"
                "spec=importlib.util.spec_from_file_location('selector',p)\ns=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(s)\n"
                "s.os.execv=lambda binary,args:os.write(" + str(ready_w) + ",s.canonical(args))\n"
                "s.run_consumer(Path(" + repr(str(self.root)) + "),'supervisor')\n")
        with selector.lock(self.root, 'operation'), slots.admission_lock(self.root):
            child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code], pass_fds=(ready_w,))
            os.close(ready_w)
            try:
                self.assertTrue(select.select([ready_r], [], [], 5)[0], 'supervisor blocked by worker locks')
                arguments = json.loads(os.read(ready_r, 8192))
                self.assertEqual(arguments[-1], 'supervise')
                self.assertIn(str(self.root / selector.SLOTS[0] / 'bounded_update.py'), arguments)
                self.assertEqual(child.wait(timeout=5), 0)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                os.close(ready_r)

    def test_timer_defers_all_live_oneshot_worker_states_without_stopping_it(self):
        for state in ('active', 'activating', 'deactivating', 'reloading'):
            self.host.install_active = state
            self.host.commands.clear()
            with slots.admission_lock(self.root), mock.patch.object(selector.time, 'monotonic', side_effect=range(0, 1000, 10)):
                selector.check(self.root, self.host, probe=mock.Mock(side_effect=OSError('busy broker')), execute=self.host.execute)
            self.assertFalse([c for c in self.host.commands if c[:2] == ['systemctl', 'stop']])
            self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[0])

    def test_timer_retries_readiness_and_restarts_the_only_valid_slot(self):
        attempts = []
        def delayed(*a):
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError('socket not bound yet')
        selector.check(self.root, self.host, probe=delayed, execute=self.host.execute)
        self.assertEqual(len(attempts), 3)
        with mock.patch.object(selector.time, 'monotonic', side_effect=range(0, 1000, 10)):
            selector.check(self.root, self.host, probe=mock.Mock(side_effect=OSError('dead broker')), execute=self.host.execute)
        self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[0])
        self.assertIn(['systemctl', '--no-block', 'start', 'messagebox-update-controller.service'], self.host.commands)

    def test_trial_blocks_all_install_paths_in_both_runtime_modes(self):
        from test_update_approval import controller
        write(self.root / selector.RUN / 'trial.json', b'{}')
        for mode in ('wacli', 'cloud'):
            with mock.patch.dict(os.environ, {'MSGBOX_TEST_TRANSPORT': mode}), mock.patch.object(controller, 'ROOT', self.root):
                with self.assertRaisesRegex(ValueError, 'promotion_inhibited'):
                    controller.Controller().dispatch({'command': 'install'}, (os.getpid(), 0, 0))
                with self.assertRaisesRegex(ValueError, 'promotion_inhibited'):
                    controller.worker()
        self.assertEqual(self.slot_bytes(selector.SLOTS[0]), self.old)

    def test_stale_bytecode_cannot_replace_verified_source_at_execution(self):
        import marshal
        import struct
        self.publish()
        path = self.root / selector.SLOTS[1] / 'release_verify.py'
        raw = path.read_bytes().replace(b'UPDATER_VERSION = (1, 3, 0)', b'UPDATER_VERSION = (0, 0, 0)')
        code = compile(raw, str(path), 'exec')
        info = path.stat()
        cache = path.parent / '__pycache__' / ('release_verify.' + sys.implementation.cache_tag + '.pyc')
        write(cache, importlib.util.MAGIC_NUMBER + struct.pack('<III', 0, int(info.st_mtime), info.st_size) + marshal.dumps(code))
        # Prove this cache would be used by a plain source-path launch.
        program = ("import sys,json;sys.pycache_prefix=None;sys.path.insert(0," + repr(str(path.parent)) +
                   ");import update_controller as c;print(json.dumps(c.controller_identity()))")
        stale = json.loads(subprocess.check_output([sys.executable, '-B', '-c', program]))
        self.assertEqual(stale['version'], '0.0.0')
        result = json.loads(selector.execute(self.root, selector.SLOTS[1], ('update_controller.py', 'self-test')))
        self.assertEqual(result['version'], '1.3.0')

    def test_bootstrap_retry_cannot_change_a_mature_running_generation(self):
        self.publish()
        path = self.root / selector.RUN / 'selected.json'
        before = path.read_bytes()
        (self.root / selector.SLOTS[1] / 'bounded_update.py').write_bytes(b'corrupt')
        slots.bootstrap(self.root, run=self.host)
        self.assertEqual(path.read_bytes(), before)

    def test_effective_route_override_rejects_before_inactive_slot_mutation(self):
        def wrong_route(command, **kwargs):
            if '--property=ExecStart' in command:
                return subprocess.CompletedProcess(command, 0, 'ExecStart={ argv[]=/usr/bin/python3 /usr/lib/messagebox-update/update_controller.py install ; }')
            return self.host(command, **kwargs)
        with self.assertRaisesRegex(ValueError, 'effective routing'):
            slots.publish(self.root, run=wrong_route, platform=PLATFORM)
        self.assertFalse((self.root / selector.SLOTS[1]).exists())
        self.assertEqual(self.slot_bytes(selector.SLOTS[0]), self.old)

    def test_cold_boot_uses_real_broker_evidence_and_full_health_probe(self):
        for name in ('transaction', 'button', 'heartbeat'):
            (self.root / ('run/messagebox-update/' + name + '.json')).unlink(missing_ok=True)
        self.publish()
        self.assertIn('PASS evidence:button:boot', self.host.health_observed)
        self.assertIn('PASS evidence:heartbeat:ready', self.host.health_observed)
        self.assertNotIn('FAIL', self.host.health_observed)

    def test_signed_discovery_with_wrong_trial_result_cannot_publish_good(self):
        self.stage(sequence=8, broken_discovery=True)
        with self.assertRaisesRegex(ValueError, 'discovery trial result'):
            self.publish()
        self.assertFalse((self.root / selector.SLOTS[1] / 'GOOD').exists())
        selector.check(self.root, self.host, execute=self.host.execute)
        self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[0])
        self.assertEqual(self.slot_bytes(selector.SLOTS[0]), self.old)

    def test_discovery_self_test_fails_when_normal_poll_or_verifier_breaks(self):
        from test_update_discovery import discovery
        for target, name in ((discovery.Discovery, 'candidate'), (discovery, 'verify_bundle')):
            with self.subTest(name=name), mock.patch.object(target, name, side_effect=ValueError('injected normal-path failure')):
                with self.assertRaises(ValueError):
                    discovery.self_test()

    def test_real_hung_signed_trial_is_killed_and_timer_selects_old(self):
        self.stage(sequence=8, hang=True)
        self.host.self_test_timeout = 0.5
        with self.assertRaises(subprocess.TimeoutExpired):
            self.publish()
        self.assertTrue((self.root / 'run/hung-trial').exists())
        self.assertFalse((self.root / selector.SLOTS[1] / 'GOOD').exists())
        selector.check(self.root, self.host, probe=lambda *a: None, execute=self.host.execute)
        self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[0])
        with self.assertRaisesRegex(ValueError, 'consumed'):
            self.publish()

    def test_real_emergency_key_rotation_preserves_both_slot_records(self):
        self.publish()
        before = [self.slot_bytes(s) for s in selector.SLOTS]
        assets = self.base / 'trust-assets'
        raw = selector.canonical({'schema': 1, 'product': 'button-box', 'trust_sequence': 1,
              'release_keys': [verify.public_key_line(self.keys[1][0])],
              'revoked_key_ids': [verify.public_key_id(verify.public_key_line(self.keys[0][0]))],
              'created': '2026-10-21T12:00:00Z'})
        message = write(assets / 'trust-update.json', raw)
        subprocess.run(['minisign', '-S', '-s', str(self.keys[2][1]), '-m', str(message),
                        '-x', str(assets / 'trust-update.json.minisig')], check=True, capture_output=True)
        slots.trust.apply_trust(assets, self.root / 'etc/button-box-update', self.root / 'var/lib/button-box-update')
        with self.assertRaisesRegex(ValueError, 'bad_controller_signature'):
            self.publish()
        self.assertEqual(self.boot(), selector.SLOTS[1])
        self.assertEqual([self.slot_bytes(s) for s in selector.SLOTS], before)

    def test_actual_syscall_errors_leave_the_working_slot_untouched(self):
        for name, error in (('fsync', errno.EIO), ('replace', errno.ENOSPC)):
            with self.subTest(name=name), mock.patch.object(os, name, side_effect=OSError(error, 'injected syscall error')):
                with self.assertRaises(OSError):
                    self.publish()
            self.assertEqual(self.boot(), selector.SLOTS[0])
            self.assertEqual(self.slot_bytes(selector.SLOTS[0]), self.old)
        original = os.open
        def no_inodes(path, flags, *args, **kwargs):
            if flags & os.O_CREAT and selector.SLOTS[1] in str(path):
                raise OSError(errno.ENOSPC, 'no inactive-slot inodes')
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(os, 'open', side_effect=no_inodes):
            with self.assertRaises(OSError):
                self.publish()
        self.assertEqual(self.boot(), selector.SLOTS[0])

    def test_repeated_timer_io_failures_then_fault_removal_recovers(self):
        with mock.patch.object(selector, 'lock', side_effect=OSError(errno.EIO, 'temporary I/O')):
            for _ in range(8):
                with self.assertRaises(OSError):
                    selector.check(self.root, self.host, execute=self.host.execute)
        (self.root / selector.RUN / 'selected.json').write_bytes(b'{}')
        selector.check(self.root, self.host, execute=self.host.execute)
        self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[0])

    def test_sigkill_during_actual_trial_then_independent_check_recovers(self):
        ready_r, ready_w = os.pipe()
        code = ("import sys,os,time\nfrom pathlib import Path\nsys.path.insert(0," + repr(str(ROOT)) + ")\n"
                "sys.path.insert(0," + repr(str(ROOT / 'tests')) + ")\n"
                "from messagebox import controller_slots as s\nfrom test_controller_slots import Host\n"
                "from test_update_discovery import PLATFORM\nroot=Path(" + repr(str(self.root)) + ")\n"
                "host=Host(root)\n"
                "def hang(*a):\n os.write(" + str(ready_w) + ",b'ready')\n time.sleep(60)\n"
                "s.publish(root,run=host,platform=PLATFORM,trial_options={'probe':hang,'execute':host.execute})\n")
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code], pass_fds=(ready_w,))
        os.close(ready_w)
        try:
            self.assertEqual(os.read(ready_r, 5), b'ready')
            selector.check(self.root, self.host, probe=lambda *a: self.fail('timer overlapped live trial'), execute=self.host.execute)
            with self.assertRaises(BlockingIOError):
                self.publish()
            child.kill()
            child.wait(timeout=5)
            selector.check(self.root, self.host, probe=lambda *a: None, execute=self.host.execute)
            self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[0])
            self.assertEqual(self.slot_bytes(selector.SLOTS[0]), self.old)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            os.close(ready_r)

    def test_standalone_selector_and_fixed_timer_contract(self):
        import ast
        import importlib.util
        import sysconfig
        standard = sysconfig.get_path('stdlib')
        def standard_module(name):
            spec = importlib.util.find_spec(name)
            return spec.origin in {'built-in', 'frozen'} or spec.origin.startswith(standard)
        tree = ast.parse((ROOT / 'messagebox/controller_selector.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue(all(standard_module(n.name) for n in node.names))
            elif isinstance(node, ast.ImportFrom):
                self.assertTrue(standard_module(node.module))
        service = (ROOT / 'systemd/messagebox-update-selector.service').read_text()
        self.assertIn('StartLimitIntervalSec=0', service)
        import inspect
        import re
        recovery = inspect.signature(selector.execute).parameters['timeout'].default
        budget = 20 + 32 + 15 + 20 + 2 * recovery + 15
        self.assertGreaterEqual(int(re.search(r'TimeoutStartSec=(\d+)', service)[1]), budget + 10)
        self.assertIn('UMask=0022', service)
        self.assertIn('TimeoutStopSec=10', service)
        self.assertNotIn('RemainAfterExit', service)
        self.assertIn('OnUnitInactiveSec=5', (ROOT / 'systemd/messagebox-update-selector.timer').read_text())
