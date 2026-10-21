"""Whole-controller transactions from frozen installations, without a Pi."""

import contextlib
import ctypes
import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_recovery_support as baseline
from field_updater import write
from messagebox import recovery_bootstrap as bootstrap
from messagebox import recovery_helper as helper
from messagebox import recovery_launcher as launcher
from test_update_discovery import PLATFORM, verify

ROOT = Path(__file__).resolve().parents[1]


def host_exchange(parent, left, right):
    if sys.platform == 'linux':
        return helper.exchange_at(parent, left, right)
    if sys.platform != 'darwin':
        raise unittest.SkipTest('atomic swap host adapter unavailable')
    # Test-only macOS atomic primitive. Production never uses this adapter.
    swap = ctypes.CDLL(None, use_errno=True).renameatx_np
    swap.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    swap.restype = ctypes.c_int
    if swap(parent, os.fsencode(left), parent, os.fsencode(right), 2):
        raise OSError(ctypes.get_errno(), 'test_atomic_swap_failed')


class UpdaterHost:
    def __init__(self, root):
        self.root = root
        self.units = {name: {'active': 'inactive', 'enabled': 'static'} for name in helper.UNITS}
        for name in (*helper.TIMERS, 'messagebox-update-controller.service'):
            self.units[name] = {'active': 'active', 'enabled': 'enabled'}
        self.commands = []
        self.fail = None
        self.jobs = ''
        self.supervisor_pre = False
        self.real_run = subprocess.run

    def identity(self):
        command = [sys.executable, '-B', str(self.root / helper.ACTIVE / 'update_controller.py'), 'self-test']
        return json.loads(self.real_run(command, check=True, capture_output=True, text=True).stdout)

    def handshake(self, root):
        value = self.identity()
        if self.fail == 'handshake':
            value['version'] = '0.0.0'
        return value

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == 'minisign':
            return self.real_run(command, **kwargs)
        if command[0] == 'systemctl':
            if command[1] == 'show':
                unit, prop = command[2], command[3]
                if prop == '--property=ActiveState,UnitFileState,MainPID':
                    state = self.units[unit]
                    text = f"ActiveState={state['active']}\nUnitFileState={state['enabled']}\nMainPID={123 if state['active'] == 'active' else 0}\n"
                elif prop == '--property=ActiveState,MainPID,ControlPID':
                    text = 'ActiveState=activating\nMainPID=0\nControlPID=' + str(os.getpid() if self.supervisor_pre else 0) + '\n'
                elif prop == '--property=ActiveState,MainPID':
                    state = self.units[unit]
                    text = f"ActiveState={state['active']}\nMainPID={123 if state['active'] == 'active' else 0}\n"
                elif prop == '--property=LoadState,User,TimeoutStartUSec,ExecStart,ExecStartPre':
                    timeout = {'messagebox-update-discovery.service': '15min', 'messagebox-update-install.service': '5min 30s'}.get(unit, '3min')
                    starts = {'messagebox-update-controller.service': 'update_controller.py serve',
                              'messagebox-update-discovery.service': 'update_discovery.py',
                              'messagebox-update-install.service': 'update_controller.py install'}
                    user = 'messagebox-update' if unit.endswith('discovery.service') else ''
                    start = starts.get(unit, 'bounded_update.py supervise')
                    text = ('LoadState=' + ('error' if self.fail == 'units' else 'loaded') + '\nUser=' + user + '\n'
                            'TimeoutStartUSec=' + timeout + '\nExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /usr/lib/messagebox-update/' + start + ' ; ignore_errors=no ; }\n')
                else:
                    return baseline.systemd(command, **kwargs)
                return subprocess.CompletedProcess(command, 0, text)
            if command[1] == 'stop':
                for unit in command[2:]:
                    self.units[unit]['active'] = 'inactive'
            elif command[1] in {'enable', 'disable'}:
                for unit in command[2:]:
                    self.units[unit]['enabled'] = 'enabled' if command[1] == 'enable' else 'disabled'
            elif command[1] == 'start' or command[1:3] == ['--no-block', 'start']:
                for unit in command[2:] if command[1] == 'start' else command[3:]:
                    self.units[unit]['active'] = 'active'
            elif command[1] == 'list-jobs':
                return subprocess.CompletedProcess(command, 0, self.jobs)
            elif command[1] != 'daemon-reload':
                return baseline.systemd(command, **kwargs)
            return subprocess.CompletedProcess(command, 0, '')
        if command[0] == 'runuser':
            if self.fail == 'discovery':
                raise subprocess.CalledProcessError(1, command)
            # Execute the real offline code; native restricted UID is a Pi check.
            return self.real_run(command[4:], **kwargs)
        if command[-1] == 'self-test':
            if self.fail == 'self-test':
                raise subprocess.CalledProcessError(1, command)
            return self.real_run(command, **kwargs)
        if any(part.endswith('/health-probe.py') for part in command):
            if self.fail == 'health':
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, '')
        raise AssertionError(command)


@unittest.skipUnless(shutil.which('minisign'), 'minisign required')
class PromotionFixture:
    @classmethod
    def setUpClass(cls):
        baseline.RecoverySupport.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        baseline.RecoverySupport.tearDownClass.__func__(cls)

    sign = baseline.RecoverySupport.sign

    candidate = baseline.RecoverySupport.candidate

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='controller-promotion-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.box = self.candidate(self.base)
        self.root = self.box.root
        self.old = self.box.fixed_bytes()
        bootstrap.bootstrap(self.root, platform=PLATFORM, run=baseline.systemd)
        self.host = UpdaterHost(self.root)
        self.saved = {unit: dict(value) for unit, value in self.host.units.items()}

    @contextlib.contextmanager
    def host_boundaries(self):
        # Execute the signed recovery helper with a controlled systemd boundary.
        with mock.patch.object(subprocess, 'run', side_effect=self.host):
            if sys.platform == 'darwin':
                real_cdll = ctypes.CDLL
                class LinuxLibc:
                    def __init__(self):
                        library = real_cdll(None, use_errno=True)
                        self.renameat2 = library.renameatx_np
                        self.renameatx_np = library.renameatx_np
                # Both APIs use flag 2. Only tests supply this macOS adapter.
                with mock.patch.object(ctypes, 'CDLL', side_effect=lambda *args, **kwargs: LinuxLibc()):
                    yield
            else:
                yield

    def promote(self, **kwargs):
        options = {'run': self.host, 'exchange': host_exchange, 'handshake': self.host.handshake}
        options.update(kwargs)
        with self.host_boundaries():
            return bootstrap.promote(self.root, platform=PLATFORM, **options)

    def boot(self):
        with self.host_boundaries():
            return launcher.launch(self.root)

    def next_candidate(self, version, sequence):
        relative, identity = bootstrap.application(self.root)
        release = self.root / relative
        module = release / 'updater/release_verify.py'
        raw = (ROOT / 'scripts/install/release_verify.py').read_text()
        raw = raw.replace('UPDATER_VERSION = (1, 3, 0)', 'UPDATER_VERSION = ' + repr(tuple(map(int, version.split('.')))))
        write(module, raw.encode())
        descriptor = verify.parse_controller_descriptor((release / verify.CONTROLLER_DESCRIPTOR).read_bytes())
        descriptor.update(controller_version=version, controller_sequence=sequence)
        for entry in descriptor['modules']:
            content = (release / entry['path']).read_bytes()
            entry.update(size=len(content), sha256=launcher.sha(content))
        proof = (repr(descriptor) + '\n').encode()
        write(release / verify.CONTROLLER_DESCRIPTOR, proof)
        write(release / verify.CONTROLLER_SIGNATURE, self.sign(proof))

    def install_committed_pr3_support(self):
        # Exercise the actual PR3 helper and permanent verifier, not a fake old
        # helper that already understands the new journal. Git is required by
        # the real package test; the PR1 hash tests do not depend on Git.
        commit = '2ea16b4e5efa62db371c4685d4b4aa78186cca58'
        def old(path):
            return subprocess.check_output(['git', '-C', str(ROOT), 'show', commit + ':' + path])
        state = launcher.document(self.root, helper.STATE + '/state.json')
        relative, _identity = bootstrap.application(self.root)
        descriptor = verify.parse_controller_descriptor((self.root / relative / verify.CONTROLLER_DESCRIPTOR).read_bytes())
        descriptor['controller_sequence'] = 6
        descriptor['recovery']['helper_version'] = '1.0.0'
        files = {}
        for entry in descriptor['modules'] + descriptor['recovery']['files']:
            source = 'scripts/install/' + Path(entry['path']).name if entry['path'].startswith('updater/') else entry['path']
            raw = old(source)
            files[entry['path']] = raw
            entry.update(size=len(raw), sha256=launcher.sha(raw))
        raw = (repr(descriptor) + '\n').encode()
        digest = launcher.sha(raw)
        slots = {}
        for role in ('current', 'fallback'):
            base = helper.STATE + '/helpers/' + digest + '/' + role
            for path, data in files.items():
                write(self.root / base / path, data)
            write(self.root / base / verify.CONTROLLER_DESCRIPTOR, raw)
            write(self.root / base / verify.CONTROLLER_SIGNATURE, self.sign(raw))
            slots[role] = {'directory': base, 'digest': digest}
        for name in ('release_verify.py', 'trust_update.py'):
            write(self.root / launcher.SUPPORT / name, files['updater/' + name])
        launcher.save(self.root, launcher.SUPPORT + '/anchors.json',
                      {name: launcher.sha(files['updater/' + name]) for name in ('release_verify.py', 'trust_update.py')})
        state.update(helpers=slots, accepted={'sequence': 6, 'digest': digest,
                     'controller_version': '1.3.0', 'helper_version': '1.0.0'})
        launcher.save(self.root, helper.STATE + '/state.json', state)
        self.assertEqual(launcher.launch(self.root), 'current')
        return state


class PromotionTests(PromotionFixture, unittest.TestCase):
    @unittest.skipUnless(shutil.which('git'), 'Git required for the committed PR3 support bytes')
    def test_pr3_only_helpers_are_both_replaced_before_exchange(self):
        previous = self.install_committed_pr3_support()
        def cut(event, relative):
            if event == 'exchange':
                state = launcher.document(self.root, helper.STATE + '/state.json')
                self.assertEqual(state['accepted']['helper_version'], '1.1.0')
                for slot in state['helpers'].values():
                    self.assertNotIn(slot, previous['helpers'].values())
                raise KeyboardInterrupt('cut')
        with self.assertRaises(KeyboardInterrupt):
            self.promote(step=cut)
        self.assertEqual(self.boot(), 'current')
        self.assertEqual(self.box.fixed_bytes(), self.old)

    def test_cold_boot_without_volatile_runtime_directory_recovers_before_broker(self):
        def cut(event, relative):
            if event == 'exchange':
                raise KeyboardInterrupt('cut')
        with self.assertRaises(KeyboardInterrupt):
            self.promote(step=cut)
        shutil.rmtree(self.root / 'run/messagebox-update')
        self.assertEqual(self.boot(), 'current')
        self.assertEqual(self.box.fixed_bytes(), self.old)
        self.assertEqual(launcher.document(self.root, helper.JOURNAL)['phase'], 'restored')
        self.assertEqual(self.boot(), 'current')

    def test_state_publication_retry_preserves_the_verified_prior_helper(self):
        self.assertEqual(self.promote()['state'], 'committed')
        previous = launcher.document(self.root, helper.STATE + '/state.json')['helpers']['current']
        self.next_candidate('1.3.1', 8)
        def cut(event, relative):
            if event == 'replace' and relative == helper.STATE + '/state.json':
                raise KeyboardInterrupt('cut')
        with self.assertRaises(KeyboardInterrupt):
            self.promote(step=cut)
        self.assertEqual(self.boot(), 'current')
        self.assertEqual(self.promote()['state'], 'committed')
        state = launcher.document(self.root, helper.STATE + '/state.json')
        self.assertEqual(state['helpers']['fallback'], previous)
        # Next promotion must retain the valid fallback if current is damaged.
        write(self.root / state['helpers']['current']['directory'] / 'messagebox/recovery_helper.py', b'corrupt')
        self.next_candidate('1.3.2', 9)
        self.assertEqual(self.promote()['state'], 'committed')
        self.assertEqual(launcher.document(self.root, helper.STATE + '/state.json')['helpers']['fallback'], previous)

    def test_changed_active_candidate_after_exchange_restores_exact_known_good(self):
        def corrupt(event, relative):
            if event == 'controller_start':
                path = self.root / helper.ACTIVE / 'bounded_update.py'
                write(path, path.read_bytes() + b'\n# injected candidate corruption\n')
        self.assertEqual(self.promote(step=corrupt)['state'], 'restored')
        self.assertEqual(self.box.fixed_bytes(), self.old)
        self.assertEqual(launcher.document(self.root, helper.JOURNAL)['phase'], 'restored')
        self.assertEqual(self.boot(), 'current')

    def test_promotion_entry_recovers_interrupted_exchange_before_new_admission(self):
        def cut(event, relative):
            if event == 'exchange':
                raise KeyboardInterrupt('cut')
        with self.assertRaises(KeyboardInterrupt):
            self.promote(step=cut)
        with self.assertRaisesRegex(ValueError, 'controller_quarantined'):
            self.promote()
        self.assertEqual(self.box.fixed_bytes(), self.old)
        self.assertEqual(launcher.document(self.root, helper.JOURNAL)['phase'], 'restored')

    def test_cold_boot_can_promote_committed_payload_without_another_application_install(self):
        shutil.rmtree(self.root / 'run/messagebox-update')
        (self.root / 'run/lock/messagebox-mode-transition.lock').unlink()
        self.assertEqual(self.promote()['state'], 'committed')
        lock = self.root / 'run/messagebox-update/maintenance.lock'
        self.assertEqual(lock.stat().st_mode & 0o777, 0o644)
        self.assertEqual(lock.stat().st_uid, os.geteuid())
        self.assertEqual(self.host.identity()['version'], '1.3.0')

    def test_promotes_whole_set_with_actual_import_and_discovery_self_tests(self):
        result = self.promote()
        self.assertEqual(result['state'], 'committed')
        journal = launcher.document(self.root, helper.JOURNAL)
        self.assertEqual(journal['phase'], 'committed')
        self.assertTrue(helper.matches(launcher, self.root, helper.ACTIVE, journal['new']))
        self.assertTrue(helper.matches(launcher, self.root, journal['stage'], journal['old']))
        self.assertFalse((self.root / helper.INHIBIT).exists())
        self.assertEqual(self.host.units, self.saved)
        self.assertEqual(self.boot(), 'current')
        self.assertEqual(self.host.identity()['version'], '1.3.0')

    def test_activation_failures_restore_old_set_and_service_states(self):
        for failure in ('self-test', 'handshake', 'discovery', 'units', 'health'):
            with self.subTest(failure=failure):
                if failure != 'self-test':
                    # New proof identity permits retry after a quarantined failure.
                    self.next_candidate('1.3.0', 8 + list(('self-test', 'handshake', 'discovery', 'units', 'health')).index(failure))
                self.host.fail = failure
                result = self.promote()
                self.assertEqual(result['state'], 'restored')
                self.assertEqual(self.box.fixed_bytes(), self.old)
                self.assertEqual(launcher.document(self.root, helper.JOURNAL)['phase'], 'restored')
                self.assertEqual(self.host.units, self.saved)
                self.host.fail = None
                self.assertEqual(self.boot(), 'current')

    def test_quarantined_digest_cannot_retry_and_current_helper_corruption_uses_fallback(self):
        self.assertEqual(self.promote()['state'], 'committed')
        previous = launcher.document(self.root, helper.STATE + '/state.json')['helpers']['current']
        self.next_candidate('1.3.1', 8)
        self.host.fail = 'handshake'
        self.assertEqual(self.promote()['state'], 'restored')
        self.host.fail = None
        with self.assertRaisesRegex(ValueError, 'controller_quarantined'):
            self.promote()
        state = launcher.document(self.root, helper.STATE + '/state.json')
        self.assertEqual(state['helpers']['fallback'], previous)
        write(self.root / state['helpers']['current']['directory'] / 'messagebox/recovery_helper.py', b'corrupt')
        self.assertEqual(self.boot(), 'fallback')
        self.assertEqual(self.host.identity()['version'], '1.3.0')

    def test_supervisor_pre_start_recovery_does_not_stop_itself(self):
        def cut(event, relative):
            if event == 'exchange':
                raise KeyboardInterrupt('cut')
        with self.assertRaises(KeyboardInterrupt):
            self.promote(step=cut)
        self.host.supervisor_pre = True
        self.host.jobs = '42 messagebox-update-supervisor.service start running'
        self.host.commands.clear()
        self.assertEqual(self.boot(), 'current')
        stops = [command for command in self.host.commands if command[:2] == ['systemctl', 'stop']]
        self.assertFalse(any('messagebox-update-supervisor.service' in command for command in stops))
        self.assertEqual(self.box.fixed_bytes(), self.old)

    def test_inactive_broker_is_restored_after_success(self):
        self.host.units['messagebox-update-controller.service']['active'] = 'inactive'
        self.assertEqual(self.promote()['state'], 'committed')
        self.assertEqual(self.host.units['messagebox-update-controller.service']['active'], 'inactive')

    def test_activation_deadline_and_pending_job_fail_closed(self):
        self.host.jobs = '42 messagebox-update-install.service start waiting'
        self.assertEqual(self.promote()['state'], 'restored')
        self.assertEqual(self.box.fixed_bytes(), self.old)
        self.host.jobs = ''
        self.next_candidate('1.3.0', 8)
        clock = iter((0, 91))
        self.assertEqual(self.promote(monotonic=lambda: next(clock))['state'], 'restored')
        self.assertEqual(self.box.fixed_bytes(), self.old)

    def test_effective_executable_order_enablement_and_failure_handling_are_required(self):
        mutations = (('argv[]=/usr/bin/python3 /usr/lib/messagebox-update/update_controller.py serve',
                      'argv[]=/usr/bin/python3 /other/controller.py serve'),
                     ('ignore_errors=no', 'ignore_errors=yes'),
                     ('Before=messagebox.target comitup.service messagebox-update-controller.service', 'Before='),
                     ('enabled\n', 'disabled\n'))
        for index, (source, target) in enumerate(mutations):
            with self.subTest(target=target):
                if index:
                    self.next_candidate('1.3.0', 7 + index)
                def run(command, **kwargs):
                    result = self.host(command, **kwargs)
                    if command[:2] in (['systemctl', 'show'], ['systemctl', 'is-enabled']):
                        result.stdout = result.stdout.replace(source, target)
                    return result
                result = self.promote(run=run)
                self.assertEqual(result['state'], 'restored')
                self.assertEqual(self.box.fixed_bytes(), self.old)

    def test_both_runtime_modes_use_local_transaction_without_cloud_calls(self):
        for index, mode in enumerate(('cloud', 'wacli')):
            with self.subTest(mode=mode):
                if index:
                    self.next_candidate('1.3.1', 8)
                write(self.root / 'etc/messagebox/env', ('MSGBOX_TRANSPORT=' + mode + '\n').encode())
                with mock.patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')):
                    self.assertEqual(self.promote()['state'], 'committed')
                health = [command for command in self.host.commands if any(part.endswith('/health-probe.py') for part in command)]
                self.assertTrue(health)
                self.assertEqual(health[-1][-1], '--current')

    def test_repeat_upgrades_and_one_automatic_restore_keep_monotonic_floor(self):
        self.assertEqual(self.promote()['state'], 'committed')
        first = self.host.identity()
        self.next_candidate('1.3.1', 8)
        self.assertEqual(self.promote()['state'], 'committed')
        second = self.host.identity()
        self.next_candidate('1.3.2', 9)
        self.host.fail = 'handshake'
        self.assertEqual(self.promote()['state'], 'restored')
        self.assertEqual(self.host.identity(), second)
        self.assertEqual(launcher.document(self.root, helper.STATE + '/state.json')['accepted']['sequence'], 9)
        self.host.fail = None
        self.next_candidate('1.3.2', 10)
        self.assertEqual(self.promote()['state'], 'committed')
        self.assertEqual(self.host.identity()['version'], '1.3.2')
        self.assertNotEqual(first['hashes'], second['hashes'])
        self.assertEqual(self.boot(), 'current')

    def test_unsupported_exchange_keeps_old_without_two_rename_fallback(self):
        def unsupported(*args):
            raise helper.ExchangeUnavailable(errno.ENOSYS, 'unsupported')
        result = self.promote(exchange=unsupported)
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(self.box.fixed_bytes(), self.old)
        self.assertEqual(self.host.units, self.saved)
        self.assertEqual(self.boot(), 'current')

    def test_post_exchange_process_loss_rolls_back_once_via_permanent_launcher(self):
        def cut(event, path):
            if event == 'exchange':
                raise KeyboardInterrupt('power loss')
        with self.assertRaises(KeyboardInterrupt):
            self.promote(step=cut)
        self.assertEqual(launcher.document(self.root, helper.JOURNAL)['phase'], 'exchange_intent')
        self.assertEqual(self.boot(), 'current')
        self.assertEqual(self.box.fixed_bytes(), self.old)
        self.assertEqual(launcher.document(self.root, helper.JOURNAL)['phase'], 'restored')
        self.assertEqual(self.boot(), 'current')
        self.assertEqual(self.box.fixed_bytes(), self.old)


class DiskModel:
    """Independent inode/dirent durability model; no /opt dependency at boot.

    File flushes preserve data, directory flushes preserve names. Directory
    exchange updates two names in one parent and leaves child inodes intact.
    Before a flush we test both discarded changes and retained writes.
    """
    def __init__(self, root):
        self.root = root
        self.ids = {}
        self.nodes = {}
        self.children = {}
        self.cache = {}
        self.serial = 0
        self.root_id = self.node(root)
        for path, dirs, files in os.walk(root):
            relative = Path(path).relative_to(root)
            if relative == Path('.'):
                dirs.remove('opt')
            for name in (*dirs, *files):
                self.node(Path(path) / name)
        for path, dirs, files in os.walk(root):
            if Path(path) == root:
                dirs.remove('opt')
            self.flush_dir(Path(path))

    def content(self, path):
        info = path.lstat()
        if path.is_symlink():
            return ('link', os.readlink(path))
        if path.is_dir():
            return ('directory', info.st_mode & 0o777)
        key = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if key not in self.cache:
            self.cache[key] = ('file', info.st_mode & 0o777, path.read_bytes())
        return self.cache[key]

    def node(self, path, *, fresh=False):
        inode = path.lstat().st_ino
        if fresh or inode not in self.ids:
            self.serial += 1
            self.ids[inode] = self.serial
            self.nodes[self.serial] = self.content(path)
        return self.ids[inode]

    def flush_dir(self, path):
        parent = self.node(path)
        self.children[parent] = {child.name: self.node(child) for child in path.iterdir()
                                 if not (path == self.root and child.name == 'opt')}

    def step(self, event, relative):
        path = self.root / relative
        if event in {'file_write', 'mkdir'}:
            self.node(path, fresh=True)
            if event == 'file_write':
                # Names may survive without the unflushed file contents.
                value = self.content(path)
                self.nodes[self.node(path)] = ('file', value[1], b'')
        elif event == 'file_fsync':
            self.nodes[self.node(path)] = self.content(path)
        elif event == 'directory_fsync':
            self.flush_dir(path)

    def image(self):
        result = {}
        def descend(node, prefix):
            for name, child in self.children.get(node, {}).items():
                relative = prefix + name
                value = self.nodes[child]
                result[relative] = value
                if value[0] == 'directory':
                    descend(child, relative + '/')
        descend(self.root_id, '')
        return result

    def retained(self):
        result = {}
        for path, dirs, files in os.walk(self.root):
            if Path(path) == self.root:
                dirs.remove('opt')
            for name in (*dirs, *files):
                child = Path(path) / name
                result[child.relative_to(self.root).as_posix()] = self.content(child)
        return result


def restore_image(image, root):
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o755)
    for relative, value in sorted(image.items()):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if value[0] == 'directory':
            path.mkdir(exist_ok=True)
            path.chmod(value[1])
        elif value[0] == 'link':
            path.symlink_to(value[1])
        else:
            path.write_bytes(value[2])
            path.chmod(value[1])


class PromotionCrashMatrix(PromotionFixture, unittest.TestCase):
    def test_all_syscall_prefixes_recover_with_retained_or_lost_unflushed_changes(self):
        from test_field_release_payload import BuiltFieldPayload
        def exercise(box):
            self.box, self.root = box, box.root
            self.old = box.fixed_bytes()
            self.host = UpdaterHost(self.root)
            write(self.root / 'etc/button-box-update/release-keys/pr3-test.pub', self.public.read_bytes())
            self.install_committed_pr3_support()
            failing = box.version == 'v0.3.9'
            if failing:
                self.host.fail = 'health'
            self._exercise_matrix(failing=failing)
        BuiltFieldPayload().exercise_built_payload(after_install=exercise, versions=('v0.3.12', 'v0.3.9'))

    def _exercise_matrix(self, *, failing=False):
        model = DiskModel(self.root)
        seen = set()
        cuts = []
        outcomes = set()
        boot_root = self.base / 'crash-boot'

        def check(image):
            key = hash(tuple(sorted(image.items())))
            if key in seen:
                return
            seen.add(key)
            restore_image(image, boot_root)
            # /run is tmpfs. Only the OS-provided lock directory exists before
            # recovery; the broker has not created its RuntimeDirectory yet.
            shutil.rmtree(boot_root / 'run')
            (boot_root / 'run/lock').mkdir(parents=True, mode=0o755)
            host = UpdaterHost(boot_root)
            previous_root, previous_host = self.root, self.host
            self.root, self.host = boot_root, host
            try:
                # Boot uses only permanent support and verified helper slots.
                self.assertFalse((boot_root / 'opt').exists())
                self.assertEqual(self.boot(), 'current')
                journal = launcher.document(boot_root, helper.JOURNAL)
                if journal['kind'] == 'bootstrap':
                    self.assertEqual(self.box.fixed_bytes(), self.old)
                    outcomes.add('bootstrap:old')
                else:
                    self.assertIn(journal['phase'], helper.TERMINAL)
                    expected = journal['new'] if journal['phase'] == 'committed' else journal['old']
                    self.assertTrue(helper.matches(launcher, boot_root, helper.ACTIVE, expected))
                    # One complete active set; retained known-good is inactive.
                    self.assertEqual(sum(helper.matches(launcher, boot_root, helper.ACTIVE, identity)
                                         for identity in (journal['old'], journal['new'])), 1)
                    if journal['phase'] == 'committed':
                        self.assertTrue(helper.matches(launcher, boot_root, journal['stage'], journal['old']))
                    outcomes.add(journal['phase'] + ':' + expected['version'])
                first = launcher.document(boot_root, helper.JOURNAL)
                self.assertEqual(self.boot(), 'current')
                self.assertEqual(launcher.document(boot_root, helper.JOURNAL), first)
                self.assertFalse((boot_root / helper.INHIBIT).exists())
            finally:
                self.root, self.host = previous_root, previous_host

        def cut(event, relative):
            cuts.append((event, relative))
            model.step(event, relative)
            check(model.image())
            check(model.retained())

        self.assertEqual(self.promote(step=cut, monotonic=lambda: 0)['state'], 'restored' if failing else 'committed')
        events = {event for event, relative in cuts}
        self.assertTrue({'file_write', 'file_fsync', 'replace', 'mkdir', 'directory_fsync', 'exchange',
                         'journal_intent', 'journal_staged', 'journal_exchange_intent', 'journal_exchanged',
                         'self_test', 'controller_start', 'handshake', 'discovery_self_test',
                         } <= events)
        if failing:
            self.assertTrue({'journal_restore_intent', 'journal_restored'} <= events)
        else:
            self.assertTrue({'application_health', 'journal_activated', 'journal_committed'} <= events)
        expected_outcomes = {'bootstrap:old', 'restored:1.1.0'}
        if not failing:
            expected_outcomes.add('committed:1.3.0')
        self.assertEqual(outcomes, expected_outcomes)
        self.assertGreater(len(seen), 50)
        print(f'Promotion crash matrix: {len(cuts)} boundaries; {len(seen)} boot images; {sorted(outcomes)}')


class PromotionAdmissionTests(unittest.TestCase):
    def test_private_journal_gap_and_public_inhibit_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(verify.promotion_inhibited(root))
            write(root / helper.JOURNAL, json.dumps({'kind': 'controller', 'phase': 'intent'}).encode(), 0o600)
            self.assertTrue(verify.promotion_inhibited(root))
            self.assertFalse(verify.promotion_inhibited(root, journal=False))
            write(root / helper.INHIBIT, b'{}', 0o644)
            self.assertTrue(verify.promotion_inhibited(root, journal=False))
            (root / helper.INHIBIT).unlink()
            write(root / helper.JOURNAL, json.dumps({'kind': 'controller', 'phase': 'committed'}).encode(), 0o600)
            self.assertFalse(verify.promotion_inhibited(root))
            (root / 'run/messagebox-update').chmod(0o777)
            self.assertTrue(verify.promotion_inhibited(root, journal=False))

    @unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0, 'native restricted UID needs Linux root')
    def test_real_restricted_uid_can_read_public_admission_but_not_private_journal(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            root = Path(directory)
            root.chmod(0o755)
            write(root / helper.JOURNAL, b'{"kind":"controller","phase":"committed"}', 0o600)
            script = ('import sys,os;sys.path.insert(0,sys.argv[2]);os.setgid(65534);os.setuid(65534);'
                      'from release_verify import promotion_inhibited;'
                      'print(promotion_inhibited(sys.argv[1],journal=False,owner=0))')
            command = [sys.executable, '-B', '-c', script, str(root), str(ROOT / 'scripts/install')]
            self.assertEqual(subprocess.check_output(command, text=True).strip(), 'False')
            write(root / helper.INHIBIT, b'{}', 0o644)
            self.assertEqual(subprocess.check_output(command, text=True).strip(), 'True')
            def restricted():
                os.setgid(65534)
                os.setuid(65534)
            result = subprocess.check_output([sys.executable, '-B', str(ROOT / 'scripts/install/update_discovery.py'),
                                              '--self-test'], text=True, preexec_fn=restricted)
            self.assertEqual(json.loads(result), {'protocol': 1, 'dry_run': True})

    def test_root_only_handshake_remains_available_while_mutation_is_inhibited(self):
        from test_update_approval import controller
        with mock.patch.object(controller, 'promotion_inhibited', return_value=True), \
                mock.patch.object(controller, 'controller_identity', return_value={'protocol': 1}):
            broker = controller.Controller()
            self.assertEqual(broker.dispatch({'command': 'controller_handshake'}, (42, 0, 0)), {'protocol': 1})
            with self.assertRaisesRegex(ValueError, 'unauthorized_peer'):
                broker.dispatch({'command': 'controller_handshake'}, (42, 101, 101))
            with self.assertRaisesRegex(ValueError, 'promotion_inhibited'):
                broker.dispatch({'command': 'trust_downloaded'}, (42, 101, 101))
            self.assertEqual(controller.rejection_code(ValueError('promotion_inhibited')), 'update_busy')
