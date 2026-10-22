"""A/B publication cuts, real frozen controllers, and process boundaries.

Systemd and physical application health remain explicit host boundaries.
"""
import contextlib
import copy
import importlib.util
import io
import errno
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import socket
import threading
import time
import tracemalloc
import types
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from field_updater import FieldBox, PROVENANCE, write
from messagebox import controller_selector as selector
from messagebox import controller_slots as slots
import test_controller_descriptor as descriptors
from test_release_tooling import build
from test_update_discovery import PLATFORM, verify

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def fast_clock():
    elapsed = [0.0]
    with mock.patch.object(selector.time, 'monotonic', side_effect=lambda: elapsed[0]), \
            mock.patch.object(selector.time, 'sleep', side_effect=lambda n: elapsed.__setitem__(0,elapsed[0]+n)):
        yield


class DurableView:
    """Discard unsynced contents AND directory entries, not just stop execution.

    This models the conservative fsync contract, not ext4 writeback scheduling.
    File data follows inode identity across rename; only directory fsync commits
    namespace changes. Unflushed new inodes have no recoverable data.
    """
    def __init__(self, root):
        self.root = root
        self.files, self.dirs = {}, {}
        self.root_inode = root.stat().st_ino
        for directory, _, names in os.walk(root):
            path = Path(directory)
            self.directory(path)
            for name in names:
                file = path / name
                if file.is_file() and not file.is_symlink():
                    self.files[file.stat().st_ino] = file.read_bytes()

    def directory(self, path):
        entries = {}
        for child in path.iterdir():
            info = child.lstat()
            kind = 'link' if child.is_symlink() else 'dir' if child.is_dir() else 'file'
            entries[child.name] = (kind, info.st_ino, info.st_mode & 0o777,
                                   os.readlink(child) if kind == 'link' else None)
        self.dirs[path.stat().st_ino] = entries

    def step(self, event, name):
        path = Path(name)
        if event in {'file_fsync', 'existing_file_fsync'}:
            if event == 'file_fsync':
                path = path.with_name(path.name + '.tmp')
            self.files[path.stat().st_ino] = path.read_bytes()
        elif event == 'directory_fsync':
            self.directory(path)

    def synced(self, fd):
        """Observe actual worker/selector fsyncs, including legacy code."""
        info = os.fstat(fd)
        for directory, _, names in os.walk(self.root):
            path = Path(directory)
            if path.stat().st_ino == info.st_ino:
                self.directory(path)
                return
            for name in names:
                file = path / name
                if not file.is_symlink() and file.stat().st_ino == info.st_ino:
                    self.files[info.st_ino] = file.read_bytes()
                    return
        raise AssertionError('fsync outside modeled disk')

    def power_loss(self):
        shutil.rmtree(self.root)
        self.root.mkdir(mode=0o755)
        def restore(path, inode):
            for name, (kind, number, mode, target) in self.dirs.get(inode, {}).items():
                child = path / name
                if kind == 'link':
                    child.symlink_to(target)
                elif kind == 'dir':
                    child.mkdir(mode=mode)
                    child.chmod(mode)
                    restore(child, number)
                else:
                    child.write_bytes(self.files.get(number, b''))
                    child.chmod(mode)
        restore(self.root, self.root_inode)
        shutil.rmtree(self.root / 'run', ignore_errors=True)


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
        self.real_run = selector.bounded_run
        self.recovery_run = None
        self.recoveries = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == 'systemctl':
            if command[1] == 'show':
                if '--property=LoadState,ActiveState,Job' in command:
                    text = 'LoadState=not-found\nActiveState=inactive\nJob='
                elif '--property=ActiveState,UnitFileState,Result' in command:
                    model = getattr(self.recovery_run, '__self__', self.recovery_run)
                    state = model.states[command[2]]
                    text = ('ActiveState=' + state['active'] + '\nUnitFileState=' + state['enabled']
                            + '\nResult=' + getattr(model, 'results', {}).get(command[2], 'success'))
                elif '--property=ExecStart' in command:
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
            spec = importlib.util.spec_from_file_location('actual_slot_recovery', root / slot / 'bounded_update.py')
            worker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(worker)
            self.recoveries.append(slot)
            try:
                transaction = worker.recover_transaction
                with mock.patch.dict(sys.modules, {'bounded_update': worker}), \
                        mock.patch.object(worker, 'recover_transaction', side_effect=lambda **k: transaction(run=self.recovery_run, **k)):
                    exec(selector.recovery_program(root, slot, arguments), {})
            except (Exception, SystemExit) as exc:
                raise ValueError('actual recovery command failed') from exc
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
                result = health.main(list(arguments[1:]), root=root, run=health_run)
            self.health_observed = output.getvalue()
            if result:
                raise ValueError('actual health probe rejected: ' + self.health_observed)
            return self.health_observed.encode()
        return selector.execute(root, slot, arguments, timeout)

    def probe(self, root, value, run, **kwargs):
        self.probes += 1
        if self.probes <= self.not_ready:
            raise OSError('stale or delayed broker')
        if not self.fresh:
            return  # A success result alone must not create GOOD.
        context = selector.document(selector.read(root, 'run/messagebox-update/transaction.json'))
        # Real application producers -> AF_UNIX IPC -> broker dispatch/evidence.
        # Peer credentials/MainPID and hardware remain host boundaries.
        from test_mic_access import runtime as button_send
        software, voicepoll = (importlib.import_module('messagebox.' + name)
                               for name in ('software', 'voicepoll'))
        cloud_runtime = button_send.cloud_runtime
        slot = selector.selected(root)['slot']
        with mock.patch.dict(sys.modules):
            for name in ('release_verify', 'trust_update', 'bounded_update', 'update_controller'):
                spec = importlib.util.spec_from_file_location(name, root / slot / (name + '.py'))
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
            controller = module
        app = {k: context[k] for k in ('software_version', 'software_commit')}
        socket_path = str(root.parent / 'readiness.sock')
        errors = []
        requests = []
        with contextlib.ExitStack() as stack:
            for module, name, replacement in (
                (controller, 'RUN', root / 'run/messagebox-update'),
                (controller, 'STATE', root / 'var/lib/button-box-update'),
                (controller, 'ROOT', root),
                (controller, 'read', lambda n,d=None: controller.update.read_state(root,n,d)),
                (controller, 'save', lambda n,v,mode=0o600: controller.update.save_state(root,n,v,mode)),
                (controller, 'unit_pid', lambda unit: os.getpid()),
                (controller.pwd, 'getpwnam', lambda name: types.SimpleNamespace(pw_uid=os.geteuid())),
                (software, 'CONTROL_SOCKET', socket_path), (software, 'software_fields', lambda: app),
                (software, '_ready_at', {}),
                (button_send, 'button', types.SimpleNamespace(is_pressed=False)),
            ):
                stack.enter_context(mock.patch.object(module, name, replacement, create=True))
            stack.enter_context(mock.patch.dict(os.environ, {'MSGBOX_TRANSPORT': 'wacli'}))
            stack.enter_context(mock.patch.object(cloud_runtime, 'CloudRuntime', side_effect=AssertionError('Cloud blocked')))
            stack.enter_context(mock.patch.object(voicepoll, 'load_seen', return_value=set()))
            stack.enter_context(mock.patch.object(voicepoll, 'load_contact_authorizations', return_value={}))
            stack.enter_context(mock.patch.object(voicepoll, 'wacli', return_value=types.SimpleNamespace(stdout='{}')))
            stack.enter_context(mock.patch.object(voicepoll.time, 'sleep', side_effect=KeyboardInterrupt))
            with mock.patch.object(controller, 'installed_identity', return_value=app), \
                mock.patch.object(controller.update, '_boot', return_value=context['boot']), \
                mock.patch.object(controller.update, '_process_token', return_value='123'):
                broker = controller.Controller()
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    Path(socket_path).unlink(missing_ok=True)
                    server.bind(socket_path)
                    server.listen(2)
                    server.settimeout(5)
                    def serve():
                        try:
                            for _ in range(2):
                                connection, _ = server.accept()
                                with connection:
                                    request = json.loads(connection.recv(16384))
                                    requests.append(request.get('command'))
                                    answer = broker.dispatch(request, (os.getpid(), os.geteuid(), os.getegid()))
                                    connection.sendall(selector.canonical(answer))
                        except BaseException as exc:
                            errors.append(exc)
                    thread = threading.Thread(target=serve)
                    thread.start()
                    button_send.runtime_readiness()
                    try:
                        with contextlib.redirect_stdout(io.StringIO()):
                            voicepoll.main()
                    except KeyboardInterrupt:
                        pass
                    thread.join(6)
                    if thread.is_alive() or errors:
                        raise AssertionError((errors or 'readiness IPC hung', requests))
        Path(socket_path).unlink(missing_ok=True)



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
        self.host.recovery_run = self.box.run
        self.api = selector
        self.stage()
        for name in ('messagebox-update-selector.service', 'messagebox-update-selector.timer'):
            write(self.root / 'etc/systemd/system' / name, (ROOT / 'systemd' / name).read_bytes())
        slots.bootstrap(self.root, run=self.host)
        self.old = self.slot_bytes(selector.SLOTS[0])

    def stage(self, version='1.3.0', sequence=7, *, hang=False, broken_discovery=False, no_recovery=False, restore_defect=None, flood=None, no_start=False):
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
            if no_recovery and name == 'bounded_update.py':
                raw = raw.replace(b'def _recover_locked(root, run, *, start=True, reason=\'interrupted\'):',
                                  b'def _recover_locked(root, run, *, start=True, reason=\'interrupted\'):\n    return None')
            if no_start and name == 'bounded_update.py':
                raw = raw.replace(b'_external_restore(root, journal, run, start=start)',
                                  b'_external_restore(root, journal, run, start=False)')
            if restore_defect and name == 'bounded_update.py':
                raw += ("\noriginal_restore=_restore_record\ndef _restore_record(record,path,backup):\n"
                        + ("    return\n" if restore_defect == 'noop' else
                           "    if record['kind']!='absent': original_restore(record,path,backup)\n" if restore_defect == 'remove' else
                           "    original_restore(record,path,backup)\n    if record['kind']=='file': path.chmod(0o600)\n")).encode()
            if flood and name in {'bounded_update.py', 'update_controller.py'}:
                header = ("def _recover_locked(root, run, *, start=True, reason='interrupted'):"
                          if name == 'bounded_update.py' else 'def controller_self_test():')
                raw = raw.replace(header.encode(), (header + '\n' +
                    '\n'.join('    ' + line for line in flood.splitlines())).encode())
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
                             trial_options={'execute': self.host.execute, 'probe': self.host.probe, 'duration': 5}, **kwargs)

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

    def test_durability_model_discards_unflushed_data_and_renames(self):
        for event in ('write', 'rename', 'directory_fsync'):
            directory = self.base / ('disk-' + event)
            write(directory / 'value', b'old')
            disk = DurableView(directory)
            def cut(e, path):
                disk.step(e, path)
                if e == event and (e != 'directory_fsync' or path == str(directory)):
                    raise KeyboardInterrupt('cut before or after durability')
            with self.assertRaises(KeyboardInterrupt):
                selector.write(directory / 'value', b'new', step=cut)
            disk.power_loss()
            self.assertEqual((directory / 'value').read_bytes(), b'new' if event == 'directory_fsync' else b'old')

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
        selector.check(self.root, self.host, probe=lambda *a,**k: None, execute=self.host.execute)
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
            self.assertEqual(selector.recover(self.root, self.host, execute=lambda *a: None), selector.SLOTS[1])
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

    def test_signed_zero_exit_recovery_cannot_publish_good(self):
        self.stage(no_recovery=True)
        with self.assertRaisesRegex(ValueError, 'still inhibited|incomplete'):
            self.publish()
        self.assertFalse((self.root / selector.SLOTS[1] / 'GOOD').exists())
        self.assertEqual(self.boot(), selector.SLOTS[0])

    def test_signed_no_start_recovery_fails_runtime_trial_and_uses_other_slot(self):
        release = self.stage(no_start=True)
        with self.assertRaisesRegex(ValueError, 'service activity not restored'):
            self.publish()
        previous = os.umask(0o002)
        try:
            with self.assertRaisesRegex(ValueError, 'service activity not restored'):
                selector.recovery_trial(self.root, selector.SLOTS[1], self.host.execute)
        finally:
            os.umask(previous)
        self.assertFalse((self.root / selector.SLOTS[1] / 'GOOD').exists())
        proof = verify.verify_retained_controller(release, self.root / 'etc/button-box-update/release-keys',
            application_version='v0.3.13', platform=PLATFORM)
        identity = slots.identity(selector, self.root, selector.SLOTS[1], generation=7, version='1.3.0',
                                  descriptor=proof['descriptor_sha256'], legacy=False)
        # Model a previously accepted defect. Its signature and every hash are real.
        selector.write(self.root / selector.SLOTS[1] / 'GOOD', selector.envelope(identity))
        self.interrupted_application()
        services = selector.service_restoration(self.root)
        self.assertEqual(services['units']['messagebox-button.service']['active'], 'active')
        snapshot = self.root / self.box.worker.read_state(self.root, 'journal.json')['backup'].lstrip('/') / 'state.json'
        attempts = []
        def execute(root, slot, arguments, timeout):
            if attempts:
                self.assertTrue(snapshot.exists(), 'failed service restoration must retain proof')
            attempts.append(slot)
            return self.host.execute(root, slot, arguments, timeout)
        self.assertEqual(selector.recover(self.root, self.host, boot=False, execute=execute), selector.SLOTS[0])
        self.assertEqual(attempts, list(reversed(selector.SLOTS)))
        selector.recovered_services(self.root, services, self.host)
        self.assertFalse(snapshot.exists())

    def test_boot_rejects_wrong_enablement_before_retiring_proof(self):
        self.publish()
        self.interrupted_application()
        services = selector.service_restoration(self.root)
        attempts = []
        def execute(root, slot, arguments, timeout):
            attempts.append(slot)
            answer = self.host.execute(root, slot, arguments, timeout)
            if slot == selector.SLOTS[1]:
                state = self.box.states['messagebox.target']
                state['enabled'] = 'disabled' if state['enabled'] == 'enabled' else 'enabled'
            return answer
        self.assertEqual(selector.recover(self.root, self.host, execute=execute), selector.SLOTS[0])
        self.assertEqual(attempts, list(reversed(selector.SLOTS)))
        selector.recovered_services(self.root, services, self.host, boot=True)

    def service_model(self):
        from test_bounded_update import Fixture
        directory = self.base / 'service-model'
        directory.mkdir(mode=0o755)
        model = Fixture(directory).systemctl
        model.states = copy.deepcopy(self.box.states)
        self.host.recovery_run = model
        return model

    def test_service_rule_data_and_supported_names_match_current_and_frozen_workers(self):
        from field_updater import load_frozen
        from test_bounded_update import bounded_update as worker
        for source in [worker, *(load_frozen(v, 'scripts/install/bounded_update.py') for v in PROVENANCE['releases'])]:
            self.assertEqual(selector.UNIT_NAME.pattern, source.UNIT_NAME.pattern)
            self.assertEqual(selector.UNIT_NAME.flags, source.UNIT_NAME.flags)
            self.assertEqual(selector.LIMIT_RESULTS, source.LIMIT_RESULTS)
        model = self.service_model()
        required = {'units': {n: copy.deepcopy(model.states[n]) for n in worker.UNITS}}
        names = ['example.' + kind for kind in ('service','socket','device','mount','automount','swap',
                                               'target','path','timer','slice','scope')]
        names += [r'example\x2dchild.socket', r'example\xAFchild.service', '-unsafe.service',
                  '../escape.socket', 'two names.service', 'bad\n.service', r'bad\xZZ.socket', None]
        for unit in names:
            with self.subTest(unit=unit):
                required['auxiliary_units'] = [unit]
                if worker._valid_auxiliary_unit(unit):
                    model.states[unit] = {'active': 'active', 'enabled': 'static'}
                    selector.recovered_services(self.root, required, self.host)
                else:
                    with self.assertRaisesRegex(ValueError, 'unsafe restoration unit'):
                        selector.recovered_services(self.root, required, self.host)

    def test_service_acceptance_matches_worker_exceptions_and_rejects_required_failures(self):
        from test_bounded_update import bounded_update as worker
        model = self.service_model()
        baseline = copy.deepcopy(model.states)
        cases = [(u, 'active', 'failed', r, 'inactive', True) for u in ('messagebox-mode-reconcile.path', 'messagebox-wifi-watchdog.timer')
                 for r in selector.LIMIT_RESULTS]
        cases += [(u, 'active', 'failed', r, 'inactive', False) for u,r in (
            ('messagebox-mode-reconcile.path','resources'), ('messagebox-wifi-watchdog.timer','success'),
            ('messagebox-button.service','start-limit-hit'), ('example.path','trigger-limit-hit'))]
        cases += [('messagebox-mode-reconcile.path','inactive','failed','trigger-limit-hit','inactive',False)]
        cases += [('comitup-web.service', before, state, 'exit-code', comitup, True)
                  for before,comitup in (('inactive','inactive'), ('failed','inactive'), ('active','active'))
                  for state in ('active','inactive','failed')]
        cases += [('comitup-web.service', 'active', 'failed', 'exit-code', 'inactive', False)]
        for unit, before, active, result, comitup, accepted in cases:
            with self.subTest(unit=unit, active=active, result=result, accepted=accepted):
                model.states = copy.deepcopy(baseline)
                model.states[unit] = {'active': before, 'enabled': baseline.get(unit, {}).get('enabled', 'static')}
                model.states['comitup.service']['active'] = comitup
                required = {'units': {n: copy.deepcopy(model.states[n]) for n in worker.UNITS}}
                if unit not in required['units']:
                    required['auxiliary_units'] = [unit]
                model.states[unit]['active'] = active
                model.results = {unit: result}
                with fast_clock():
                    if accepted:
                        worker._verify_active(required['units'], model, auxiliary_units=required.get('auxiliary_units', ()))
                        selector.recovered_services(self.root, required, self.host)
                    else:
                        with self.assertRaises(worker.UpdateError):
                            worker._verify_active(required['units'], model, auxiliary_units=required.get('auxiliary_units', ()))
                        with self.assertRaisesRegex(ValueError, 'service activity not restored'):
                            selector.recovered_services(self.root, required, self.host)

    def test_runtime_recovery_accepts_relimited_watcher_and_optional_web_in_both_slots(self):
        self.publish()
        model = self.service_model()
        watcher, web = 'messagebox-mode-reconcile.path', 'comitup-web.service'
        model.states[watcher]['active'] = 'active'
        model.states[web]['active'] = 'inactive'
        model.states['comitup.service']['active'] = 'inactive'
        self.box.run = model
        self.interrupted_application()
        services = selector.service_restoration(self.root)
        initial = copy.deepcopy(model.states)
        image = self.base / 'interrupted'
        shutil.copytree(self.root, image, symlinks=True)
        def flap(states):
            states[watcher]['active'] = 'failed'
            model.results[watcher] = 'trigger-limit-hit'
            states[web]['active'] = 'failed'
        model.after_start[watcher] = flap
        for slot in selector.SLOTS:
            with self.subTest(slot=slot):
                shutil.rmtree(self.root)
                shutil.copytree(image, self.root, symlinks=True)
                model.states, model.results = copy.deepcopy(initial), {}
                self.host.commands.clear()
                self.host.recoveries.clear()
                other = next(s for s in selector.SLOTS if s != slot)
                selector.choose(self.root, other, selector.inspect(self.root, other))
                with fast_clock():
                    selector.check(self.root, self.host, execute=self.host.execute)
                self.assertEqual(self.host.recoveries, [slot])
                self.assertEqual(selector.selected(self.root)['slot'], slot)
                selector.recovered_services(self.root, services, self.host)
                self.assertIn(['systemctl','--no-block','start','messagebox-update-controller.service'], self.host.commands)
                self.assertFalse((self.root / self.box.worker.read_state(self.root,'journal.json')['backup'].lstrip('/') / 'state.json').exists())

    def test_boot_and_runtime_recover_auxiliary_socket_and_escaped_name_in_both_slots(self):
        self.publish()
        model = self.service_model()
        auxiliary = ('example.socket', r'example\x2dchild.service')
        for unit in auxiliary:
            model.states[unit] = {'active': 'active', 'enabled': 'static'}
            model.part_of[unit] = 'messagebox.target'
        self.box.run = model
        self.interrupted_application()
        services = selector.service_restoration(self.root)
        self.assertEqual(set(services['auxiliary_units']), set(auxiliary))
        initial = copy.deepcopy(model.states)
        image = self.base / 'interrupted'
        shutil.copytree(self.root, image, symlinks=True)
        for boot in (True, False):
            for slot in selector.SLOTS:
                with self.subTest(boot=boot, slot=slot):
                    shutil.rmtree(self.root)
                    shutil.copytree(image, self.root, symlinks=True)
                    model.states = copy.deepcopy(initial)
                    self.host.recoveries.clear()
                    with fast_clock():
                        self.assertEqual(selector.recover(self.root, self.host, boot=boot, preferred=(slot,),
                            execute=self.host.execute), slot)
                    self.assertEqual(self.host.recoveries, [slot])
                    selector.recovered_services(self.root, services, self.host, boot=boot)
                    for unit in auxiliary:
                        self.assertEqual(model.states[unit]['active'], 'inactive' if boot else 'active')

    def test_populated_transient_cgroup_blocks_recovery_before_any_worker_runs(self):
        unit = selector.TRIAL_UNITS['update_controller.py']
        events = write(self.root / 'sys/fs/cgroup/system.slice' / unit / 'cgroup.events', b'populated 1\n')
        for pending in (False, True):
            with self.subTest(pending=pending):
                clock = [0.0]
                if pending:
                    events.unlink()
                def run(c, **k):
                    if pending and '--property=LoadState,ActiveState,Job' in c:
                        return subprocess.CompletedProcess(c,0,'LoadState=loaded\nActiveState=inactive\nJob=9')
                    return self.host(c, **k)
                with mock.patch.object(selector.time, 'monotonic', side_effect=lambda: clock[0]), \
                        mock.patch.object(selector.time, 'sleep', side_effect=lambda n: clock.__setitem__(0,clock[0]+n)), \
                        self.assertRaisesRegex(ValueError, 'trial cgroup still populated'):
                    selector.recover(self.root, run, execute=self.host.execute)
                self.assertEqual(self.host.recoveries, [])

    def test_slow_authenticated_broker_without_newline_falls_back_within_deadline(self):
        self.publish()
        path = self.root / 'run/messagebox-update/control.sock'
        path.parent.mkdir(exist_ok=True)
        socket_type = socket.socket
        stop = threading.Event()
        class PeerSocket:
            def __init__(self, *args, **kwargs):
                self.connection = socket_type(*args, **kwargs)
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def getsockopt(self, *args):
                return struct.pack('3i', os.getpid(), 0, 0)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.connection.close()
        with socket_type(socket.AF_UNIX) as server:
            server.bind(str(path))
            server.listen(4)
            server.settimeout(0.1)
            def trickle():
                while not stop.is_set():
                    try:
                        with server.accept()[0] as connection:
                            while not stop.wait(0.1):
                                connection.sendall(b'x')
                    except (OSError, TimeoutError):
                        pass
            thread = threading.Thread(target=trickle)
            thread.start()
            try:
                started = time.monotonic()
                with mock.patch.object(socket, 'socket', PeerSocket), \
                        mock.patch.object(socket, 'SO_PEERCRED', 17, create=True):
                    selector.check(self.root, self.host, execute=self.host.execute)
                self.assertLess(time.monotonic() - started, 6)
                self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[0])
            finally:
                stop.set()
                thread.join(2)

    def interrupted_application(self):
        self.box.data.update(version='v0.3.14', commit='d' * 40)
        self.box.save_manifest()
        with self.assertRaises(KeyboardInterrupt):
            self.box.worker.install_transaction(self.box.source, self.box.manifest, root=self.root,
                run=self.box.run, sequence=2, health=mock.Mock(side_effect=KeyboardInterrupt('power loss')))

    def test_legacy_failed_install_keeps_its_restoration_proof(self):
        for name in ('messagebox-update-selector.service', 'messagebox-update-selector.timer'):
            write(self.box.source / 'systemd' / name, (self.root / 'etc/systemd/system' / name).read_bytes())
        self.box.refresh()
        self.box.data.update(version='v0.3.14', commit='d' * 40)
        self.box.save_manifest()
        with self.assertRaises(self.box.worker.UpdateError):
            def transaction(*args):
                return self.box.worker.install_transaction(*args, root=self.root, run=self.box.run, sequence=2,
                    health=mock.Mock(side_effect=self.box.worker.UpdateError('health_timeout')))
            selector.install(self.root, self.box.worker, transaction, self.box.source, self.box.manifest)
        journal = self.box.worker.read_state(self.root, 'journal.json')
        self.assertEqual(journal['phase'], 'rolled_back')
        self.assertTrue(selector.restoration(self.root))
        selector.recovered(self.root)

    def test_both_recoveries_fail_then_healthy_broker_cannot_discharge_pending_work(self):
        self.publish()
        self.interrupted_application()
        healthy = mock.Mock()
        self.host.commands.clear()
        self.host.recoveries.clear()
        with mock.patch.object(os, 'fsync', side_effect=OSError(errno.EIO, 'injected recovery storage fault')), \
                self.assertRaisesRegex(ValueError, 'no working'):
            selector.check(self.root, self.host, probe=healthy, execute=self.host.execute)
        healthy.assert_not_called()
        self.assertEqual(len(self.host.recoveries), 2)
        self.assertFalse(any(c[:3] == ['systemctl','--no-block','start'] for c in self.host.commands))
        selector.check(self.root, self.host, probe=healthy, execute=self.host.execute)
        selector.recovered(self.root)
        self.assertEqual(self.box.worker.read_state(self.root, 'journal.json')['phase'], 'rolled_back')
        count = len(self.host.recoveries)
        selector.check(self.root, self.host, probe=healthy, execute=self.host.execute)
        self.assertEqual(len(self.host.recoveries), count)
        healthy.assert_called_once()

    def test_signed_restore_defects_fail_trial_and_boot_restores_with_the_other_slot(self):
        for index, defect in enumerate(('noop', 'mode', 'remove')):
            self.stage('1.3.' + str(index), 7 + index, restore_defect=defect)
            with self.assertRaises((ValueError, OSError)):
                self.publish()
            self.assertFalse((self.root / selector.SLOTS[1] / 'GOOD').exists())
            self.boot()
        # Model a previously accepted, correctly signed defective controller.
        # Boot must reject its terminal journal and restore with the frozen A.
        proof = verify.verify_retained_controller(self.stage('1.3.3', 10, restore_defect='noop'),
            self.root / 'etc/button-box-update/release-keys', application_version='v0.3.13', platform=PLATFORM)
        slot = selector.SLOTS[1]
        release = self.root / os.readlink(self.root / 'opt/messagebox').lstrip('/')
        for name in selector.MODULES:
            write(self.root / slot / name, (release / 'updater' / name).read_bytes())
        identity = slots.identity(selector, self.root, slot, generation=10, version='1.3.3',
                                  descriptor=proof['descriptor_sha256'], legacy=False)
        selector.write(self.root / slot / 'GOOD', selector.envelope(identity))
        self.interrupted_application()
        required = selector.restoration(self.root)
        # The real frozen install changed an external unit. Corrupt both content
        # and mode and add a file whose snapshot requires removal.
        file = next(r for r in required if r['kind'] == 'file' and r['path'].endswith('.service'))
        write(self.root / file['path'].lstrip('/'), b'defective candidate\n', 0o600)
        absent = {'path': '/etc/systemd/system/messagebox-extra.service', 'kind': 'absent'}
        snapshot = self.root / self.box.worker.read_state(self.root, 'journal.json')['backup'].lstrip('/') / 'state.json'
        state = json.loads(snapshot.read_bytes())
        state['files'].append(absent)
        self.box.worker.durable_json(snapshot, state)
        required.append(absent)
        write(self.root / absent['path'].lstrip('/'), b'must be removed\n')
        self.host.recoveries.clear()
        # First run the defective controller alone: its real command returns
        # success and marks rolled_back, while the selector still rejects it.
        self.host.execute(self.root, slot, ('bounded_update.py','recover','--boot'))
        self.assertEqual(self.box.worker.read_state(self.root, 'journal.json')['phase'], 'rolled_back')
        with self.assertRaises((ValueError, OSError)):
            selector.recovered(self.root, required)
        self.host.recoveries.clear()
        self.assertEqual(self.boot(), selector.SLOTS[0])
        self.assertEqual(self.host.recoveries, [selector.SLOTS[1], selector.SLOTS[0]])
        selector.recovered(self.root, required)
        self.assertFalse(snapshot.exists())
        marker = write(self.root / 'etc/messagebox-onboarding/enabled', b'')
        self.boot()
        self.assertTrue(marker.exists(), 'a completed rollback must preserve a later setup request')

    def test_retired_proof_directory_flush_failure_stays_pending_until_retry(self):
        self.publish()
        self.interrupted_application()
        healthy = mock.Mock()
        snapshot = self.root / self.box.worker.read_state(self.root, 'journal.json')['backup'].lstrip('/')
        original = selector.flush
        def flush(path, step=selector.checkpoint):
            if path == snapshot:
                raise OSError(errno.EIO, 'proof directory flush failed')
            original(path, step)
        with mock.patch.object(selector, 'flush', side_effect=flush), self.assertRaises(ValueError):
            selector.check(self.root, self.host, probe=healthy, execute=self.host.execute)
        healthy.assert_not_called()
        self.assertTrue((snapshot / 'state.json').exists())
        selector.check(self.root, self.host, probe=healthy, execute=self.host.execute)
        selector.recovered(self.root)
        healthy.assert_called_once()

    def test_legacy_removal_is_durable_before_proof_retirement_after_failed_flush_retry(self):
        for version in PROVENANCE['releases']:
            for fault in (None, 'file', 'removal_directory', 'proof_directory'):
                with self.subTest(version=version, fault=fault):
                    box = FieldBox(self.base / (version + '-' + str(fault)), version)
                    box.install()
                    root, worker = box.root, box.worker
                    host = Host(root)
                    host.recovery_run = box.run
                    for name in ('messagebox-update-selector.service', 'messagebox-update-selector.timer'):
                        write(root / 'etc/systemd/system' / name, (ROOT / 'systemd' / name).read_bytes())
                    slots.bootstrap(root, run=host)
                    dropin = root / 'etc/systemd/system/messagebox-button.service.d/messagebox.conf'
                    self.assertFalse(dropin.exists())
                    box.add('systemd/messagebox-button.service.d/messagebox.conf', b'[Service]\nEnvironment=NEW_DROPIN=1\n')
                    box.data.update(version='v0.3.14', commit='d' * 40)
                    box.save_manifest()
                    with self.assertRaises(KeyboardInterrupt):
                        worker.install_transaction(box.source, box.manifest, root=root, run=box.run, sequence=2,
                            health=mock.Mock(side_effect=KeyboardInterrupt('interrupted application')))
                    self.assertTrue(dropin.exists())
                    required = selector.restoration(root)
                    journal = worker.read_state(root, 'journal.json')
                    proof = root / journal['backup'].lstrip('/') / 'state.json'
                    disk = DurableView(root)  # Candidate drop-in is durable at the cut baseline.
                    original = os.fsync
                    failure_inode = None
                    fired = False
                    def fsync(fd):
                        nonlocal fired
                        if failure_inode == os.fstat(fd).st_ino:
                            fired = True
                            raise OSError(errno.EIO, 'injected restoration flush failure')
                        original(fd)
                        disk.synced(fd)
                    def cut(event, path):
                        if event == 'directory_fsync' and path == str(proof.parent):
                            raise KeyboardInterrupt('proof removal durable; power lost')
                    with mock.patch.object(os, 'fsync', side_effect=fsync):
                        host.execute(root, selector.SLOTS[0], ('bounded_update.py', 'recover', '--boot'))
                        selector.recovered(root, required)
                        self.assertFalse(dropin.exists())
                        # Frozen _restore_record unlinks absent entries without a parent flush.
                        self.assertIn(dropin.name, disk.dirs[dropin.parent.stat().st_ino])
                        if fault:
                            target = (next(root / r['path'].lstrip('/') for r in required if r['kind'] == 'file')
                                      if fault == 'file' else dropin.parent if fault == 'removal_directory' else proof.parent)
                            failure_inode = target.stat().st_ino
                            with self.assertRaises(OSError):
                                selector.finish_recovery(root)
                            self.assertTrue(fired)
                            self.assertTrue(proof.exists(), 'flush failure must keep the recovery proof')
                            failure_inode = None
                        with self.assertRaises(KeyboardInterrupt):
                            selector.finish_recovery(root, step=cut)
                    disk.power_loss()  # Discard all unflushed bytes AND directory entries.
                    self.assertFalse(dropin.exists())
                    self.assertFalse(proof.exists())
                    selector.recovered(root, required)
                    host = Host(root)
                    host.recovery_run = box.run
                    self.assertEqual(selector.recover(root, host, execute=host.execute), selector.SLOTS[0])

    def test_good_rename_directory_fsync_failure_then_retry_and_power_loss(self):
        disk = DurableView(self.root)
        renamed = False
        original = os.fsync
        def step(event, path):
            nonlocal renamed
            disk.step(event, path)
            if event == 'rename' and path == str(self.root / selector.SLOTS[1] / 'GOOD'):
                renamed = True
        def fail(fd):
            if renamed and stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, 'failed directory flush after GOOD rename')
            original(fd)
        with mock.patch.object(os, 'fsync', side_effect=fail), self.assertRaises(OSError):
            self.publish(step=step)
        # The timer sees the live but unflushed GOOD after the publisher dies.
        selector.check(self.root, self.host, probe=mock.Mock(side_effect=OSError('interrupted trial')),
                       execute=self.host.execute)
        self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[1])
        self.stage('1.3.1', 8)
        # Stage is a separately committed application delivery in this model.
        # Keep the original durability map for the two controller directories.
        def cut(event, path):
            disk.step(event, path)
            if event == 'directory_fsync' and path == str(self.root / selector.SLOTS[0]):
                raise KeyboardInterrupt('spare GOOD deletion durable')
        with self.assertRaises(KeyboardInterrupt):
            self.publish(step=cut)
        disk.power_loss()
        self.host = Host(self.root)
        self.host.recovery_run = self.box.run
        self.assertFalse((self.root / selector.SLOTS[0] / 'GOOD').exists())
        self.assertEqual(self.boot(), selector.SLOTS[1])

    def test_install_route_guards_every_frozen_worker_before_mutation(self):
        from field_updater import PROVENANCE
        for version in PROVENANCE['releases']:
            box = FieldBox(self.base / ('guard-' + version), version)
            box.install()
            for name in ('messagebox-update-selector.service', 'messagebox-update-selector.timer'):
                write(box.root / 'etc/systemd/system' / name, (ROOT / 'systemd' / name).read_bytes())
                write(box.source / 'systemd' / name, (ROOT / 'systemd' / name).read_bytes())
            box.data.update(version='v0.3.14', commit='c' * 40)
            write(box.root / selector.HOME, (ROOT / 'messagebox/controller_selector.py').read_bytes())
            for name in ('messagebox-update-selector.service', 'messagebox-update-selector.timer',
                         'messagebox-update-controller.service.d/messagebox.conf',
                         'messagebox-update-selector.service.d/messagebox.conf',
                         'messagebox-update-supervisor.service.d/messagebox.conf'):
                with self.subTest(version=version, path=name):
                    target = box.source / 'systemd' / name
                    previous = target.read_bytes() if target.exists() else None
                    write(target, b'[Service]\nExecStart=\nExecStart=/bin/false\n')
                    box.refresh()
                    box.data.update(version='v0.3.14', commit='c' * 40)
                    box.save_manifest()
                    journal = box.worker.read_state(box.root, 'journal.json')
                    # Execute the actual permanent install route with the frozen
                    # broker, verifier, archive extraction and worker. Only the
                    # established FieldBox systemd/outer signature bounds remain.
                    with box.controller() as controller:
                        box.bundle(controller, sequence=2)
                        controller.trust.recover_trust(controller.KEYS.parent, controller.STATE)
                        before = box.tree()
                        with mock.patch.dict(sys.modules, {'update_controller': controller}), \
                                mock.patch.object(sys, 'argv', sys.argv[:]), \
                                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
                            exec(selector.install_program(box.root, selector.SLOTS[0]), {})
                        self.assertEqual(rejected.exception.code, 1)
                        self.assertEqual(controller.read('result.json')['state'], 'failed')
                        operation = controller.read(controller.operation_name(controller.read('ticket.json')['operation_id']))
                        self.assertEqual(operation['reason'], 'verification_failed')
                    self.assertEqual(box.tree(), before)
                    self.assertEqual(box.worker.read_state(box.root,'journal.json'), journal)
                    if previous is None:
                        target.unlink()
                    else:
                        write(target, previous)
            box.refresh()
            box.data.update(version='v0.3.14', commit='c' * 40)
            protected = selector.admit(box.root, box.source)
            backup, atomic = box.worker.create_backup, box.worker._atomic_install
            def snapshot(destination, root, entries, *a, **k):
                self.assertFalse(protected & {e['target'] for e in entries})
                return backup(destination, root, entries, *a, **k)
            def copy(source, destination, *a, **k):
                self.assertNotIn('/' + str(destination.relative_to(box.root)), protected)
                return atomic(source, destination, *a, **k)
            with box.controller() as controller, mock.patch.object(box.worker,'create_backup',side_effect=snapshot), \
                    mock.patch.object(box.worker,'_atomic_install',side_effect=copy), \
                    mock.patch.dict(sys.modules, {'update_controller': controller}), \
                    mock.patch.object(sys, 'argv', sys.argv[:]):
                box.bundle(controller, sequence=2)
                exec(selector.install_program(box.root, selector.SLOTS[0]), {})
                self.assertEqual(controller.read('journal.json')['phase'], 'committed')
                self.assertEqual(controller.read('result.json')['state'], 'installed')
        with mock.patch.object(selector.os, 'execv') as launch:
            selector.run_consumer(self.root, 'install')
        program = launch.call_args.args[1][-1]
        self.assertEqual(program, selector.install_program(self.root, selector.SLOTS[0]))
        compile(program, '<frozen install route>', 'exec')

    def test_zero_exit_incomplete_recovery_tries_the_other_slot(self):
        self.publish()
        self.interrupted_application()
        journal = self.box.worker.read_state(self.root, 'journal.json')
        journal['phase'] = 'switched'
        self.box.worker.save_state(self.root, 'journal.json', journal)
        attempted = []
        def execute(root, slot, arguments, timeout):
            if arguments[0] == '-c':
                return self.host.execute(root, slot, arguments, timeout)
            attempted.append(slot)
            if slot == selector.SLOTS[0]:
                return self.host.execute(root, slot, arguments, timeout)
            return b''  # A verified command exits zero but leaves recovery pending.
        self.assertEqual(selector.recover(self.root, self.host, execute=execute), selector.SLOTS[0])
        self.assertEqual(attempted, list(reversed(selector.SLOTS)))
        journal = self.box.worker.read_state(self.root, 'journal.json')
        target = journal['old']
        for damage in ('inhibit', 'pointer', 'known-good'):
            with self.subTest(damage=damage):
                if damage == 'inhibit':
                    write(self.root / 'var/lib/button-box-update/install-inhibit', b'{}')
                elif damage == 'pointer':
                    self.box.worker.switch_release(self.root, journal['new'])
                else:
                    self.box.worker.save_state(self.root, 'known-good.json', {'current': journal['new']})
                with self.assertRaises((ValueError, FileNotFoundError)):
                    selector.recovered(self.root)
                (self.root / 'var/lib/button-box-update/install-inhibit').unlink(missing_ok=True)
                self.box.worker.switch_release(self.root, target)
                self.box.worker.save_state(self.root, 'known-good.json', {'current': target})

    def test_runtime_prefers_alternate_then_retries_current_each_timer_run(self):
        self.publish()
        # Model an accepted retained controller with a runtime recovery bug.
        # Hashes remain valid; execute the failing function, not a Host shortcut.
        file = self.root / selector.SLOTS[0] / 'bounded_update.py'
        file.write_bytes(file.read_bytes() + b'\ndef _recover_locked(*a, **k): raise OSError("injected recovery I/O failure")\n')
        value = selector.inspect(self.root, selector.SLOTS[0], slots.identity(selector, self.root, selector.SLOTS[0],
            generation=0, version='1.1.0', descriptor='', legacy=True))
        selector.write(self.root / selector.SLOTS[0] / 'GOOD', selector.envelope(value))
        attempted = []
        def execute(root, slot, arguments, timeout):
            attempted.append((slot, timeout))
            return self.host.execute(root, slot, arguments, timeout)
        for _ in range(3):
            with fast_clock():
                selector.check(self.root, self.host, probe=mock.Mock(side_effect=OSError('broker dead')),
                               execute=execute)
            self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[1])
            self.assertEqual([s for s, _ in attempted[-2:]], list(selector.SLOTS))
            self.assertTrue(all(0 < t <= 180 for _, t in attempted))
            self.assertEqual(self.host.commands[-1], ['systemctl', '--no-block', 'start', 'messagebox-update-controller.service'])

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

    def test_infinite_trial_output_is_stream_limited_and_noisy_accepted_recovery_falls_back(self):
        marker = self.root / 'run/flood-pids'
        flood = ("\nimport subprocess,sys,os\nfrom pathlib import Path\n"
                 "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                 "Path(" + repr(str(marker)) + ").write_text(str(os.getpid())+' '+str(child.pid))\n"
                 "while True: os.write(1,b'x'*8192)\n")
        self.stage(flood=flood)
        original, reads, peaks = selector.bounded_run, [], []
        def measured(command, **kwargs):
            total = 0
            read = os.read
            def observe(fd, size):
                nonlocal total
                chunk = read(fd, size)
                total += len(chunk)
                return chunk
            tracemalloc.start()
            try:
                with mock.patch.object(os, 'read', side_effect=observe):
                    return original(command, **kwargs)
            finally:
                peaks.append(tracemalloc.get_traced_memory()[1])
                tracemalloc.stop()
                reads.append((kwargs['stdout'], total))
        def assert_killed():
            self.assertTrue(marker.exists(), 'the genuine controller process must reach the flood')
            for pid in marker.read_text().split():
                result = subprocess.run(['ps', '-o', 'stat=', '-p', pid], capture_output=True, text=True)
                self.assertTrue(not result.stdout.strip() or result.stdout.lstrip().startswith('Z'), result.stdout)
            marker.unlink()
        self.host.real_run = measured
        with self.assertRaisesRegex(ValueError, 'output limit'):
            self.publish()
        self.assertEqual(reads[-1], (subprocess.PIPE, 65537))
        self.assertFalse((self.root / selector.SLOTS[1] / 'GOOD').exists())
        assert_killed()
        # A correctly signed, previously accepted defect can also flood during
        # boot recovery. Logs are discarded; the independent deadline kills
        # its whole group and the real frozen worker completes recovery.
        release = self.root / os.readlink(self.root / 'opt/messagebox').lstrip('/')
        proof = verify.verify_retained_controller(release, self.root / 'etc/button-box-update/release-keys',
            application_version='v0.3.13', platform=PLATFORM)
        value = slots.identity(selector, self.root, selector.SLOTS[1], generation=10, version='1.3.0',
                               descriptor=proof['descriptor_sha256'], legacy=False)
        selector.write(self.root / selector.SLOTS[1] / 'GOOD', selector.envelope(value))
        attempts = []
        def execute(root, slot, arguments, timeout):
            attempts.append(slot)
            if slot == selector.SLOTS[1]:
                with mock.patch.object(selector, 'bounded_run', side_effect=measured):
                    return selector.execute(root, slot, arguments, min(timeout, 1))
            return self.host.execute(root, slot, arguments, timeout)
        started = time.monotonic()
        self.assertEqual(selector.recover(self.root, self.host, execute=execute), selector.SLOTS[0])
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(attempts, list(reversed(selector.SLOTS)))
        self.assertEqual(reads[-1], (subprocess.DEVNULL, 0))
        self.assertLess(max(peaks), 512 * 1024, 'parent output memory must stay bounded')
        assert_killed()

    def test_every_publication_and_trial_write_cut_keeps_an_accepted_slot(self):
        image = self.base / 'before'
        shutil.copytree(self.root, image, symlinks=True)
        events = []
        self.publish(step=lambda event, path: events.append((event, path)))
        for index in range(len(events)):
            shutil.rmtree(self.root)
            shutil.copytree(image, self.root, symlinks=True)
            disk = DurableView(self.root)
            count = 0
            def cut(event, path):
                nonlocal count
                disk.step(event, path)
                count += 1
                if count == index + 1:
                    raise KeyboardInterrupt('power cut')
            with self.assertRaises(KeyboardInterrupt):
                self.publish(step=cut)
            disk.power_loss()
            self.host = Host(self.root)
            self.host.recovery_run = self.box.run
            chosen = self.boot()
            self.assertIn(chosen, selector.SLOTS)
            selector.inspect(self.root, chosen)
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
            disk = DurableView(self.root)
            count = 0
            def cut(e,p):
                nonlocal count
                disk.step(e,p)
                count += 1
                if count == index + 1:
                    raise KeyboardInterrupt('bootstrap cut')
            with self.assertRaises(KeyboardInterrupt):
                slots.bootstrap(self.root, run=self.host, step=cut)
            if any((self.root / p).exists() for p in slots.routes()):
                self.assertTrue((self.root / selector.HOME).exists())
                self.assertEqual(selector.valid(self.root)[0][0], selector.SLOTS[0])
            slots.bootstrap(self.root, run=self.host)
            # A killed process is retried against the live view. Retry must
            # finish all durability work before a later power loss.
            slots.bootstrap(self.root, run=self.host, step=disk.step)
            disk.power_loss()
            self.host = Host(self.root)
            self.host.recovery_run = self.box.run
            self.assertEqual((self.root / selector.HOME).read_bytes(), (ROOT / 'messagebox/controller_selector.py').read_bytes())
            for path, raw in slots.routes().items():
                self.assertEqual((self.root / path).read_bytes(), raw)
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
            self.assertIsNone(selector.check(self.root, self.host, probe=lambda *a,**k: self.fail('overlapping check'), execute=self.host.execute))
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
        with fast_clock():
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
            self.assertEqual(arguments[-1], selector.recovery_program(self.root, selector.SLOTS[0], selector.COMMANDS['supervisor']))
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
                self.assertEqual(arguments[-1], selector.recovery_program(self.root, selector.SLOTS[0], selector.COMMANDS['supervisor']))
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
            with slots.admission_lock(self.root), fast_clock():
                selector.check(self.root, self.host, probe=mock.Mock(side_effect=OSError('busy broker')), execute=self.host.execute)
            self.assertFalse([c for c in self.host.commands if c[:2] == ['systemctl', 'stop']])
            self.assertEqual(selector.selected(self.root)['slot'], selector.SLOTS[0])

    def test_timer_retries_readiness_and_restarts_the_only_valid_slot(self):
        attempts = []
        def delayed(*a,**k):
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError('socket not bound yet')
        selector.check(self.root, self.host, probe=delayed, execute=self.host.execute)
        self.assertEqual(len(attempts), 3)
        with fast_clock():
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
        result = json.loads(selector.execute(self.root, selector.SLOTS[1], ('update_controller.py', 'self-test'), output=True))
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
        selector.check(self.root, self.host, probe=lambda *a,**k: None, execute=self.host.execute)
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
                "def hang(*a,**k):\n os.write(" + str(ready_w) + ",b'ready')\n time.sleep(60)\n"
                "s.publish(root,run=host,platform=PLATFORM,trial_options={'probe':hang,'execute':host.execute})\n")
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code], pass_fds=(ready_w,))
        os.close(ready_w)
        try:
            self.assertEqual(os.read(ready_r, 5), b'ready')
            selector.check(self.root, self.host, probe=lambda *a,**k: self.fail('timer overlapped live trial'), execute=self.host.execute)
            with self.assertRaises(BlockingIOError):
                self.publish()
            child.kill()
            child.wait(timeout=5)
            selector.check(self.root, self.host, probe=lambda *a,**k: None, execute=self.host.execute)
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
