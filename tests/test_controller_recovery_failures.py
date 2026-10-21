"""Failure injections for the permanent recovery installation, before shipment.

Processes, locks, sockets and filesystem operations are real. Systemd is an
explicit host boundary except in NativeDiscoverySandbox. No box is contacted.
"""

import copy
import errno
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from field_updater import write
from messagebox import recovery_bootstrap as bootstrap
from messagebox import recovery_helper as helper
from messagebox import recovery_launcher as launcher
from test_controller_promotion import DiskModel, PromotionFixture, UpdaterHost, host_exchange, restore_image
from test_update_discovery import discovery, verify


class RecoveryFailures(PromotionFixture, unittest.TestCase):
    def child(self, code, *, pass_fds=()):
        # Fresh processes also keep this suite independent of threads left by
        # earlier application tests. No test worker forks a threaded interpreter.
        source = Path(__file__).resolve().parents[1]
        prefix = ("import os,sys,time\nfrom pathlib import Path\n"
                  "sys.path.insert(0," + repr(str(source)) + ")\n"
                  "sys.path.insert(0," + repr(str(source / 'tests')) + ")\n"
                  "from test_controller_promotion import PromotionFixture,UpdaterHost\n"
                  "from messagebox import recovery_launcher as launcher\n"
                  "fixture=PromotionFixture()\nfixture.root=Path(" + repr(str(self.root)) + ")\n"
                  "fixture.host=UpdaterHost(fixture.root)\n")
        return subprocess.Popen([sys.executable, '-I', '-B', '-c', prefix + code],
                                pass_fds=pass_fds, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def cut_exchange(self):
        def cut(event, path):
            if event == 'exchange':
                raise KeyboardInterrupt('power cut')
        with self.assertRaises(KeyboardInterrupt):
            self.promote(step=cut)

    def install_helper(self, role, suffix, sequence):
        state = launcher.document(self.root, helper.STATE + '/state.json')
        source = self.root / state['helpers'][role]['directory']
        value = verify.parse_controller_descriptor((source / verify.CONTROLLER_DESCRIPTOR).read_bytes())
        value['controller_sequence'] = sequence
        raw_helper = (source / 'messagebox/recovery_helper.py').read_bytes() + suffix.encode()
        for entry in value['recovery']['files']:
            if entry['path'] == 'messagebox/recovery_helper.py':
                entry.update(size=len(raw_helper), sha256=launcher.sha(raw_helper))
        raw = (repr(value) + '\n').encode()
        signature = self.sign(raw)
        digest = launcher.sha(raw)
        base = helper.STATE + '/helpers/' + digest + '/' + role
        shutil.copytree(source, self.root / base)
        write(self.root / base / 'messagebox/recovery_helper.py', raw_helper)
        write(self.root / base / verify.CONTROLLER_DESCRIPTOR, raw)
        write(self.root / base / verify.CONTROLLER_SIGNATURE, signature)
        state['helpers'][role] = {'directory': base, 'digest': digest,
                                  'signature_sha256': launcher.sha(signature)}
        if role == 'current':
            state['accepted'].update(sequence=sequence, digest=digest)
        launcher.save(self.root, helper.STATE + '/state.json', state)

    def candidate_helper(self, suffix):
        release, _identity = bootstrap.application(self.root)
        base = self.root / release
        path = base / 'messagebox/recovery_helper.py'
        raw_helper = path.read_bytes() + suffix.encode()
        write(path, raw_helper)
        value = verify.parse_controller_descriptor((base / verify.CONTROLLER_DESCRIPTOR).read_bytes())
        value['controller_sequence'] += 1
        for entry in value['recovery']['files']:
            if entry['path'] == 'messagebox/recovery_helper.py':
                entry.update(size=len(raw_helper), sha256=launcher.sha(raw_helper))
        raw = (repr(value) + '\n').encode()
        write(base / verify.CONTROLLER_DESCRIPTOR, raw)
        write(base / verify.CONTROLLER_SIGNATURE, self.sign(raw))

    def test_unarmed_monitor_rejects_before_verified_candidate_top_level_executes(self):
        marker = self.root / 'run/candidate-executed'
        self.candidate_helper('\nPath(' + repr(str(marker)) + ").write_text('executed')\n")
        def run(command, **kwargs):
            if command == ['systemctl', 'is-active', '--quiet', launcher.MONITOR]:
                raise subprocess.CalledProcessError(3, command)
            return self.host(command, **kwargs)
        with self.assertRaises(subprocess.CalledProcessError):
            self.promote(run=run)
        self.assertFalse(marker.exists())
        self.assertEqual(self.box.fixed_bytes(), self.old)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux owner process identity required')
    def test_monitor_deadline_kills_verified_candidate_hung_at_import(self):
        marker = self.root / 'run/candidate-hung'
        self.candidate_helper('\nPath(' + repr(str(marker)) + ").write_text('entered')\nwhile True: time.sleep(1)\n")
        monitor = self.child("with fixture.host_boundaries():\n launcher.monitor(fixture.root,interval=0.02)\n")
        worker = self.child("from messagebox import recovery_bootstrap\n"
                            "real_clock=time.monotonic\nrecovery_bootstrap.time.monotonic=lambda:real_clock()-179\n"
                            "for attempt in range(200):\n"
                            " try:\n  fixture.promote()\n  break\n"
                            " except BlockingIOError:time.sleep(0.01)\n")
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            self.assertEqual(worker.wait(timeout=10), -signal.SIGKILL)
            self.assertEqual(self.box.fixed_bytes(), self.old)
        finally:
            for process in (worker, monitor):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def test_verified_raise_hang_and_exit_try_fallback_with_fresh_durable_record(self):
        # Both helpers are genuinely signed; the current helper changes durable
        # state before its injected behavioural failure. The fallback requires
        # that changed record, rather than the stale dispatch snapshot.
        fallback = """
_recover = recover
def recover(context):
    if context['journal']['phase'] != 'prepared':
        raise ValueError('stale_dispatch')
    return _recover(context)
"""
        self.install_helper('fallback', fallback, 7)
        for index, failure in enumerate(("raise RuntimeError('injected')", 'while True: time.sleep(1)', 'os._exit(23)')):
            with self.subTest(failure=failure):
                launcher.save(self.root, helper.JOURNAL, {'schema': 1, 'kind': 'bootstrap', 'phase': 'armed'})
                suffix = "\ndef recover(context):\n    api = runtime(context['root'])\n    api.save(context['root'], JOURNAL, {'schema':1,'kind':'bootstrap','phase':'prepared'})\n    " + failure + '\n'
                self.install_helper('current', suffix, 8 + index)
                before = time.monotonic()
                self.assertEqual(launcher.launch(self.root, helper_timeout=0.6), 'fallback')
                self.assertLess(time.monotonic() - before, 5)
                self.assertEqual(self.box.fixed_bytes(), self.old)

    def test_each_journal_copy_missing_truncated_or_corrupt_at_exchange_and_commit(self):
        self.cut_exchange()
        images = self.base / 'record-image'
        shutil.copytree(self.root, images, symlinks=True)
        for phase in ('exchanged', 'committed'):
            if phase == 'committed':
                self.boot()
                self.next_candidate('1.3.0', 8)
                self.assertEqual(self.promote()['state'], 'committed')
                shutil.rmtree(images)
                shutil.copytree(self.root, images, symlinks=True)
            for path in (helper.JOURNAL, helper.JOURNAL + '.copy'):
                for damage in ('missing', 'truncated', 'corrupt', 'oversized'):
                    with self.subTest(phase=phase, path=path, damage=damage):
                        shutil.rmtree(self.root)
                        shutil.copytree(images, self.root, symlinks=True)
                        target = self.root / path
                        if damage == 'missing':
                            target.unlink()
                        else:
                            target.write_bytes(b'{' if damage == 'truncated' else b'x' * (
                                launcher.RECORD_SIZE + (1 if damage == 'oversized' else 0)))
                        self.assertEqual(self.boot(), 'current')
                        journal = launcher.document(self.root, helper.JOURNAL)
                        expected = journal['new'] if phase == 'committed' else journal['old']
                        self.assertTrue(helper.matches(launcher, self.root, helper.ACTIVE, expected))
                        self.assertEqual(len(launcher.records(self.root)), 2)
                        self.assertFalse(verify.promotion_inhibited(self.root))
                        self.assertEqual(self.boot(), 'current')

    def test_damaged_committed_active_restores_old_and_damaged_spare_allows_start(self):
        self.assertEqual(self.promote()['state'], 'committed')
        journal = launcher.document(self.root, helper.JOURNAL)
        stage = self.root / journal['stage'] / 'bounded_update.py'
        original = stage.read_bytes()
        stage.write_bytes(b'corrupt spare')
        self.assertEqual(self.boot(), 'current')
        self.assertTrue(helper.matches(launcher, self.root, helper.ACTIVE, journal['new']))
        self.next_candidate('1.3.1', 8)
        with self.assertRaisesRegex(ValueError, 'redundancy_required'):
            self.promote()
        stage.write_bytes(original)
        (self.root / helper.ACTIVE / 'bounded_update.py').write_bytes(b'corrupt active')
        self.assertEqual(self.boot(), 'current')
        self.assertEqual(self.box.fixed_bytes(), self.old)
        self.assertEqual(launcher.document(self.root, helper.JOURNAL)['phase'], 'restored')

    def test_lost_both_records_keeps_independently_verified_active_and_disables_promotion(self):
        self.assertEqual(self.promote()['state'], 'committed')
        for name in (helper.JOURNAL, helper.JOURNAL + '.copy'):
            (self.root / name).write_bytes(b'corrupt')
        self.assertEqual(self.boot(), 'verified_no_journal')
        self.next_candidate('1.3.1', 8)
        with self.assertRaisesRegex(ValueError, 'no_valid_recovery_record'):
            self.promote()

    def test_missing_both_helpers_keeps_verified_terminal_controller(self):
        self.assertEqual(self.promote()['state'], 'committed')
        state = launcher.document(self.root, helper.STATE + '/state.json')
        for slot in state['helpers'].values():
            (self.root / slot['directory'] / 'messagebox/recovery_helper.py').unlink()
        self.assertEqual(self.boot(), 'verified_no_helper')
        self.assertTrue(launcher.verified_controller(self.root, state, verify))

    def test_restore_needs_no_new_inode_or_block_and_terminal_boot_is_read_only(self):
        self.cut_exchange()
        real_open, real_pwrite = os.open, os.pwrite
        def no_allocate(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                # Existing flock files do not allocate. New paths simulate both
                # disk-space and inode exhaustion, even outside STATE.
                parent = kwargs.get('dir_fd')
                try:
                    os.stat(path, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    raise OSError(errno.ENOSPC, 'injected inode exhaustion')
            return real_open(path, flags, *args, **kwargs)
        def full(*args):
            raise OSError(errno.ENOSPC, 'injected data exhaustion')
        with mock.patch.object(os, 'open', side_effect=no_allocate), mock.patch.object(os, 'pwrite', side_effect=full), \
                mock.patch('tempfile.TemporaryDirectory', side_effect=OSError(errno.ENOSPC, 'no tmp inodes')):
            self.host.io_fault = 'noalloc'
            self.assertEqual(self.boot(), 'current')
        self.host.io_fault = None
        self.assertEqual(self.box.fixed_bytes(), self.old)
        # Retry can repair diagnostics using allocated record slots only.
        self.assertEqual(self.boot(), 'current')
        with mock.patch.object(os, 'pwrite', side_effect=AssertionError('terminal write')), \
                mock.patch.object(os, 'replace', side_effect=AssertionError('terminal rename')), \
                mock.patch.object(os, 'fsync', side_effect=AssertionError('terminal fsync')):
            self.host.io_fault = 'readonly'
            self.assertEqual(self.boot(), 'current')
        self.host.io_fault = None
        self.assertIsNotNone(real_pwrite)

    def test_boot_releases_reserved_blocks_and_inode_for_frozen_app_recovery(self):
        reserve = self.root / helper.STATE / 'recovery-reserve'
        self.assertEqual(reserve.stat().st_size, 2 * 1024 * 1024)
        with mock.patch.object(os, 'statvfs', return_value=SimpleNamespace(f_bavail=0, f_frsize=4096, f_favail=0)):
            self.assertEqual(self.boot(), 'current')
        self.assertFalse(reserve.exists())
        # Continue through the actual frozen terminal application recovery, not
        # just ExecStartPre. It can now make its existing small metadata writes.
        self.box.worker.recover_transaction(root=self.root, run=self.box.run)
        self.assertEqual(self.box.fixed_bytes(), self.old)
        with mock.patch.object(os, 'statvfs', side_effect=OSError(errno.EIO, 'injected capacity query failure')):
            self.assertEqual(self.boot(), 'current')

    def test_recovery_exchange_and_fsync_eio_retry_and_restore(self):
        self.cut_exchange()
        journal = launcher.document(self.root, helper.JOURNAL)
        for operation in ('exchange', 'fsync', 'record'):
            for code in (errno.ENOSPC, errno.EIO):
                with self.subTest(operation=operation, errno=code):
                    # Put new active again with the exact old spare.
                    if helper.matches(launcher, self.root, helper.ACTIVE, journal['old']):
                        with launcher.directory(self.root, 'usr/lib') as fd:
                            host_exchange(fd, 'messagebox-update', Path(journal['stage']).name)
                    launcher.save(self.root, helper.JOURNAL, journal)
                    fired = []
                    def once(real):
                        def fail(*args, **kwargs):
                            if not fired:
                                fired.append(True)
                                raise OSError(code, 'injected syscall error')
                            return real(*args, **kwargs)
                        return fail
                    run_exchange = once(host_exchange) if operation == 'exchange' else host_exchange
                    name = 'fsync' if operation == 'fsync' else 'pwrite'
                    real = getattr(os, name)
                    with self.host_boundaries(), mock.patch.object(os, name, side_effect=once(real) if operation != 'exchange' else real):
                        try:
                            helper.reconcile(launcher, self.root, copy.deepcopy(journal), run=self.host,
                                             exchange=run_exchange, step=lambda *args: None, boot=True)
                        except OSError:
                            pass
                    self.assertTrue(fired)
                    self.assertEqual(self.boot(), 'current')
                    self.assertEqual(self.box.fixed_bytes(), self.old)

    def test_promotion_syscall_enospc_eio_and_fsync_errors_restore(self):
        snapshot = self.base / 'syscall-image'
        shutil.copytree(self.root, snapshot, symlinks=True)
        for operation in ('open', 'pwrite', 'fsync'):
            for code in (errno.ENOSPC, errno.EIO):
                with self.subTest(operation=operation, errno=code):
                    shutil.rmtree(self.root)
                    shutil.copytree(snapshot, self.root, symlinks=True)
                    exchanged, fired = [], []
                    real = getattr(os, operation)
                    def cut(event, path):
                        if event == 'exchange':
                            exchanged.append(True)
                    def fail(*args, **kwargs):
                        # open failure cuts staging before exchange; data/flush
                        # errors cut after the atomic exchange itself.
                        staging_open = operation == 'open' and 'messagebox-update-stage-' in str(args[0])
                        if not fired and (staging_open or operation != 'open' and exchanged):
                            fired.append(True)
                            raise OSError(code, 'injected promotion syscall')
                        return real(*args, **kwargs)
                    with mock.patch.object(os, operation, side_effect=fail):
                        try:
                            self.promote(step=cut)
                        except OSError:
                            pass
                    self.assertTrue(fired)
                    self.assertEqual(self.boot(), 'current')
                    self.assertEqual(self.box.fixed_bytes(), self.old)

    def test_supervisor_gate_allows_expired_installer_stop_before_locks(self):
        entered_r, entered_w = os.pipe()
        worker_process = self.child("with launcher.locks(fixture.root):\n os.write(" + str(entered_w) + ",b'1')\n time.sleep(30)\n",
                                    pass_fds=(entered_w,))
        worker = worker_process.pid
        os.close(entered_w)
        try:
            self.assertEqual(os.read(entered_r, 1), b'1')
            # The permanent ExecStartPre gate must return success/deferred while
            # the real installer holds the trust AND updater locks.
            self.assertEqual(launcher.launch(self.root, supervisor=True), 'deferred')
            path = self.root / 'var/lib/button-box-update/journal.json'
            value = json.loads(path.read_bytes())
            value.update(phase='probation', deadline_mono=-1)
            write(path, json.dumps(value).encode(), 0o600)
            stopped = []
            def run(command, **kwargs):
                if command == ['systemctl', 'stop', 'messagebox-update-install.service']:
                    os.kill(worker, signal.SIGKILL)
                    worker_process.wait(timeout=5)
                    stopped.append(True)
                return self.box.run(command, **kwargs)
            recovered = []
            def recover(**kwargs):
                with launcher.locks(self.root):
                    recovered.append(True)
            # Only the application rollback boundary is substituted: the frozen
            # deadline check and real worker stop execute before these locks.
            with mock.patch.object(self.box.worker, 'recover_transaction', side_effect=recover):
                self.box.worker.supervise_transaction(root=self.root, run=run)
            self.assertTrue(stopped)
            self.assertTrue(recovered)
            worker = None
        finally:
            if worker:
                try:
                    os.kill(worker, signal.SIGKILL)
                    worker_process.wait(timeout=5)
                except ProcessLookupError:
                    pass
            os.close(entered_r)

    def test_unlocked_or_previous_boot_owner_lease_never_kills_successful_caller(self):
        launcher.save(self.root, helper.STATE + '/owner.json', {'pid': os.getpid(), 'token': 'token',
                      'boot': 'previous-boot', 'deadline_mono': -1})
        killed = []
        with mock.patch.object(launcher, 'process_token', return_value='token'), \
                mock.patch.object(launcher, 'boot_id', return_value='current-boot'):
            launcher.expire_owner(self.root, kill=lambda *args: killed.append(args))
            with launcher.lock(self.root, helper.STATE + '/owner.lock'):
                launcher.expire_owner(self.root, kill=lambda *args: killed.append(args))
                self.assertFalse(killed)
                lease = launcher.document(self.root, helper.STATE + '/owner.json')
                lease['boot'] = 'current-boot'
                launcher.save(self.root, helper.STATE + '/owner.json', lease)
                launcher.expire_owner(self.root, kill=lambda *args: killed.append(args))
                self.assertEqual(killed, [(os.getpid(), signal.SIGKILL)])

    def test_release_key_replacement_keeps_recovery_pins_but_revokes_new_admission(self):
        self.assertEqual(self.promote()['state'], 'committed')
        config = self.root / 'etc/button-box-update'
        keys = config / 'release-keys'
        initial = config / 'release-keysets/initial'
        if not keys.is_symlink():
            initial.parent.mkdir(exist_ok=True)
            keys.rename(initial)
            keys.symlink_to('release-keysets/initial')
        for role in ('replacement', 'emergency'):
            subprocess.run(['minisign', '-G', '-W', '-p', str(self.base / (role + '.pub')),
                            '-s', str(self.base / (role + '.key'))], check=True, capture_output=True)
        write(config / 'emergency-keys/test.pub', (self.base / 'emergency.pub').read_bytes())
        bundle = self.base / 'trust-update'
        bundle.mkdir()
        value = {'schema': 1, 'product': 'button-box', 'trust_sequence': 1,
                 'release_keys': [verify.public_key_line(self.base / 'replacement.pub')],
                 'revoked_key_ids': [verify.public_key_id(verify.public_key_line(self.public))],
                 'created': '2026-01-01T00:00:00Z'}
        write(bundle / 'trust-update.json', json.dumps(value).encode())
        subprocess.run(['minisign', '-S', '-s', str(self.base / 'emergency.key'),
                        '-m', str(bundle / 'trust-update.json'), '-x', str(bundle / 'trust-update.json.minisig')],
                       check=True, capture_output=True)
        _verify, trust = launcher.anchors(self.root)
        self.assertEqual(trust.apply_trust(bundle, config, self.root / 'var/lib/button-box-update'), 1)
        self.assertEqual(self.boot(), 'current')
        self.next_candidate('1.3.1', 8)
        with self.assertRaisesRegex(ValueError, 'bad_controller_signature'):
            self.promote()
        original = self.secret
        self.secret = self.base / 'replacement.key'
        try:
            self.next_candidate('1.3.1', 9)
        finally:
            self.secret = original
        self.assertEqual(self.promote()['state'], 'committed')
        state = launcher.document(self.root, helper.STATE + '/state.json')
        write(self.root / state['helpers']['current']['directory'] / 'messagebox/recovery_helper.py', b'corrupt')
        self.assertEqual(self.boot(), 'fallback')  # previous slot's signer is revoked

    def test_two_process_owner_excludes_second_promotion_during_activation(self):
        entered_r, entered_w = os.pipe()
        release_r, release_w = os.pipe()
        process = self.child("def pause(event,path):\n if event=='controller_start':\n  os.write(" + str(entered_w)
                             + ",b'1')\n  os.read(" + str(release_r) + ",1)\n"
                             "result=fixture.promote(step=pause)\nos._exit(0 if result['state']=='committed' else 3)\n",
                             pass_fds=(entered_w, release_r))
        pid = process.pid
        os.close(entered_w)
        os.close(release_r)
        try:
            import select
            self.assertTrue(select.select([entered_r], [], [], 10)[0])
            self.assertEqual(os.read(entered_r, 1), b'1')
            with self.assertRaisesRegex(ValueError, 'recovery_busy'):
                launcher.launch(self.root, supervisor=True, lock_wait=0.01)
            with self.assertRaises(BlockingIOError):
                self.promote()
            self.assertEqual(launcher.document(self.root, helper.JOURNAL)['phase'], 'exchanged')
            os.write(release_w, b'1')
            self.assertEqual(process.wait(timeout=15), 0)
            pid = None
            self.assertEqual(launcher.document(self.root, helper.JOURNAL)['phase'], 'committed')
            self.assertFalse((self.root / helper.STATE / 'owner.json').exists())
        finally:
            if pid:
                os.kill(pid, signal.SIGKILL)
                process.wait(timeout=5)
            os.close(entered_r)
            os.close(release_w)

    def test_boot_gate_waits_for_another_recovery_owner_instead_of_admitting_startup(self):
        entered_r, entered_w = os.pipe()
        owner = self.child("with launcher.lock(fixture.root,launcher.STATE+'/owner.lock'):\n os.write("
                           + str(entered_w) + ",b'1')\n time.sleep(0.4)\n", pass_fds=(entered_w,))
        os.close(entered_w)
        try:
            self.assertEqual(os.read(entered_r, 1), b'1')
            start = time.monotonic()
            self.assertEqual(self.boot(), 'current')
            self.assertGreaterEqual(time.monotonic() - start, 0.3)
            self.assertEqual(owner.wait(timeout=5), 0)
        finally:
            os.close(entered_r)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux parent-death guard required')
    def test_sigkill_launcher_kills_its_mutating_helper_and_releases_ownership(self):
        suffix = """
def recover(context):
    Path(context['root'], 'run/hanging-helper-pid').write_text(str(os.getpid()))
    while True:
        time.sleep(1)
"""
        self.install_helper('current', suffix, 8)
        parent = self.child("launcher.launch(fixture.root)\n")
        marker = self.root / 'run/hanging-helper-pid'
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        helper_pid = int(marker.read_text())
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                status = Path('/proc/' + str(helper_pid) + '/stat').read_text().rsplit(')', 1)[1].split()[0]
            except FileNotFoundError:
                break
            if status == 'Z':
                break
            time.sleep(0.01)
        else:
            self.fail('orphan helper remained alive')
        with launcher.lock(self.root, helper.STATE + '/owner.lock'):
            pass
        self.assertEqual(launcher.launch(self.root, helper_timeout=0.4), 'fallback')

    def test_monitor_recovers_sigkill_at_each_stop_exchange_and_activation_without_reboot(self):
        snapshot = self.base / 'monitor-image'
        shutil.copytree(self.root, snapshot, symlinks=True)
        targets = [(target, 'restored') for target in (
            'stop-timers', 'stop-services', 'exchange', 'self_test', 'controller_start',
            'handshake', 'discovery_self_test', 'application_health', 'journal_activated')]
        targets += [(target, 'committed') for target in (
            'journal_committed', 'enablement', 'start', 'services_finalized', 'unlink')]
        for target, outcome in targets:
            with self.subTest(target=target):
                shutil.rmtree(self.root)
                shutil.copytree(snapshot, self.root, symlinks=True)
                monitor_process = self.child("with fixture.host_boundaries():\n launcher.monitor(fixture.root,interval=0.02)\n")
                worker = self.child("import signal\ntarget=" + repr(target) + "\n"
                                    "def kill(event,path):\n"
                                    " match=event==target or event=='stop' and target==('stop-timers' if '.timer' in path else 'stop-services')\n"
                                    " if match:os.kill(os.getpid(),signal.SIGKILL)\n"
                                    "for attempt in range(200):\n"
                                    " try:\n  fixture.promote(step=kill)\n  break\n"
                                    " except BlockingIOError:time.sleep(0.01)\n")
                try:
                    self.assertEqual(worker.wait(timeout=20), -signal.SIGKILL)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        journal = launcher.document(self.root, helper.JOURNAL)
                        if (journal.get('phase') == outcome and journal.get('services_restored')
                                and not (self.root / helper.INHIBIT).exists()):
                            break
                        time.sleep(0.02)
                    self.assertEqual(journal['phase'], outcome)
                    self.assertTrue(journal['services_restored'])
                    self.assertFalse((self.root / helper.INHIBIT).exists())
                    expected = journal['old'] if outcome == 'restored' else journal['new']
                    self.assertTrue(helper.matches(launcher, self.root, helper.ACTIVE, expected))
                    # Only the independent monitor called recovery after death.
                finally:
                    if worker.poll() is None:
                        worker.kill()
                        worker.wait(timeout=5)
                    monitor_process.kill()
                    monitor_process.wait(timeout=5)

    def test_delayed_and_stale_socket_retry_within_activation_deadline(self):
        calls = []
        def handshake(root):
            calls.append(True)
            if len(calls) == 1:
                raise FileNotFoundError('not bound yet')
            if len(calls) == 2:
                return {'protocol': 1, 'version': '0.0.0', 'hashes': {}}
            return self.host.identity()
        self.assertEqual(self.promote(handshake=handshake, sleep=lambda _: None)['state'], 'committed')
        self.assertEqual(len(calls), 3)

    def test_effective_discovery_timeout_accepts_1800_seconds(self):
        def run(command, **kwargs):
            value = self.host(command, **kwargs)
            if command[:3] == ['systemctl', 'show', 'messagebox-update-discovery.service']:
                value.stdout = value.stdout.replace('TimeoutStartUSec=15min', 'TimeoutStartUSec=30min')
            return value
        self.assertEqual(self.promote(run=run)['state'], 'committed')

    @unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0, 'Linux root peer credentials required')
    def test_real_delayed_and_stale_unix_socket_with_peer_pid_handshake(self):
        path = self.root / 'run/messagebox-update/control.sock'
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale.close()
        started, stop = threading.Event(), threading.Event()
        def serve():
            time.sleep(0.3)
            path.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                server.listen()
                server.settimeout(5)
                started.set()
                connection, _address = server.accept()
                with connection:
                    connection.recv(4096)
                    connection.sendall(json.dumps(self.host.identity()).encode() + b'\n')
            stop.set()
        thread = threading.Thread(target=serve)
        thread.start()
        def run(command, **kwargs):
            value = self.host(command, **kwargs)
            if command[:3] == ['systemctl', 'show', 'messagebox-update-controller.service']:
                value.stdout = value.stdout.replace('MainPID=123', 'MainPID=' + str(os.getpid()))
            return value
        try:
            self.assertEqual(self.promote(run=run, handshake=helper.controller_handshake)['state'], 'committed')
            self.assertTrue(started.is_set())
            self.assertTrue(stop.wait(5))
        finally:
            thread.join(6)


class RecoveryCutMatrix(PromotionFixture, unittest.TestCase):
    install_helper = RecoveryFailures.install_helper
    def test_real_built_payload_cuts_inside_every_recovery_syscall_boundary(self):
        from test_field_release_payload import BuiltFieldPayload
        def exercise(box):
            self.box, self.root = box, box.root
            self.old = box.fixed_bytes()
            self.host = UpdaterHost(self.root)
            self.cut_into_recovery()
        BuiltFieldPayload().exercise_built_payload(after_install=exercise, versions=('v0.3.12',))

    def cut_into_recovery(self):
        def interrupted(event, path):
            if event == 'exchange':
                raise KeyboardInterrupt('promotion death')
        with self.assertRaises(KeyboardInterrupt):
            self.promote(step=interrupted)
        journal = launcher.document(self.root, helper.JOURNAL)
        image = DiskModel(self.root).retained()  # deliberately omits /opt
        original_root, original_host = self.root, self.host
        boot = self.base / 'recovery-cut-root'
        boundaries = []
        try:
            restore_image(image, boot)
            self.root, self.host = boot, UpdaterHost(boot)
            helper.reconcile(launcher, boot, copy.deepcopy(journal), run=self.host,
                             exchange=host_exchange, step=lambda *args: boundaries.append(args), boot=True)
            for index, boundary in enumerate(boundaries):
                with self.subTest(index=index, boundary=boundary):
                    restore_image(image, boot)
                    self.host = UpdaterHost(boot)
                    count = []
                    def cut(event, path):
                        count.append((event, path))
                        if len(count) == index + 1:
                            raise KeyboardInterrupt('recovery death')
                    with self.assertRaises(KeyboardInterrupt):
                        helper.reconcile(launcher, boot, copy.deepcopy(journal), run=self.host,
                                         exchange=host_exchange, step=cut, boot=True)
                    self.assertEqual(self.boot(), 'current')
                    self.assertEqual((boot / helper.ACTIVE / 'bounded_update.py').read_bytes(), self.old['bounded_update'])
                    self.assertEqual(launcher.document(boot, helper.JOURNAL)['phase'], 'restored')
                    # A genuinely signed helper fails after this exact mutation.
                    # The permanent launcher must stop it and select fallback
                    # against the durable state it left behind.
                    restore_image(image, boot)
                    self.host = UpdaterHost(boot)
                    suffix = """
_original_recover = recover
_cut_count = 0
def recover(context):
    global _cut_root
    _cut_root = Path(context['root'])
    return _original_recover(context)
def checkpoint(event, path):
    global _cut_count
    _cut_count += 1
    if _cut_count == CUT:
        (_cut_root / 'run/failure-hit').write_text(str(_cut_count))
        raise RuntimeError('injected signed helper failure')
""".replace('CUT:', str(index + 1) + ':')
                    self.install_helper('current', suffix, 100 + index)
                    with self.host_boundaries():
                        self.assertEqual(launcher.launch(boot), 'fallback')
                    self.assertEqual((boot / 'run/failure-hit').read_text(), str(index + 1))
                    self.assertTrue(helper.matches(launcher, boot, helper.ACTIVE, journal['old']))
            self.assertTrue({'exchange', 'file_write', 'file_fsync', 'directory_fsync', 'journal_restore_intent',
                             'journal_restored', 'stop', 'start'} <= {event for event, path in boundaries})
            print(f'Recovery interruption matrix: {len(boundaries)} executed cuts, real built payload')
        finally:
            self.root, self.host = original_root, original_host


class SelfTestFailures(unittest.TestCase):
    def test_discovery_self_test_uses_normal_poll_and_signature_verification(self):
        with mock.patch.object(discovery.Discovery, 'candidate', side_effect=ValueError('broken normal candidate')):
            with self.assertRaises(ValueError):
                discovery.self_test()
        with mock.patch.object(discovery, 'verify_bundle', side_effect=verify.ReleaseError('broken verifier')):
            with self.assertRaises(ValueError):
                discovery.self_test()

    @unittest.skipUnless(sys.platform == 'linux' and Path('/run/systemd/system').is_dir()
                         and os.geteuid() == 0, 'native Linux systemd root required')
    def test_native_discovery_service_sandbox_runs_normal_offline_discovery(self):
        # Test images must provision the normal service user and install the
        # candidate updater under /usr/lib, just as the target box does.
        result = subprocess.run(helper.discovery_command(Path('/')), check=True, capture_output=True, text=True, timeout=40)
        self.assertEqual(json.loads(result.stdout), {'protocol': 1, 'dry_run': True})
        result = subprocess.run(helper.controller_command(Path('/')), check=True, capture_output=True, text=True, timeout=40)
        value = json.loads(result.stdout)
        self.assertEqual(value['protocol'], 1)
        self.assertEqual(set(value['hashes']), set(helper.MODULES))
