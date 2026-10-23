"""Boot graph contracts, including the unchanged drop-ins already in the field."""

import re
import contextlib
import importlib.util
import json
import os
import sys
import shutil
import subprocess
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest import mock

from field_updater import FIXTURES, PROVENANCE, FieldBox, digest, write
from messagebox import cloud_boot_repair as repair, controller_selector as selector, controller_slots as slots
from test_bounded_update import bounded_update
from test_controller_slots import DurableView
from test_release_tooling import build, release


ROOT = Path(__file__).resolve().parents[1]
ACTIVATORS = (
    'messagebox-mode-reconcile.path', 'messagebox-wifi-change.path',
    'messagebox-wifi-watchdog.timer', 'messagebox-onboarding-complete.path',
    'messagebox-onboarding-voice.path',
)
RECOVERY = 'messagebox-update-recovery.service'


def unit_settings(raw):
    """Keep repeated directives; dependency lists accumulate across drop-ins."""
    result = defaultdict(list)
    section = ''
    for line in raw.replace('\\\n', ' ').splitlines():
        line = line.strip()
        if line.startswith('['):
            section = line[1:-1]
        elif line and not line.startswith(('#', ';')) and '=' in line:
            key, value = line.split('=', 1)
            result[section, key].append(value)
    return result


def shipped_units(tree):
    units = {}
    for path in tree.rglob('*'):
        if path.is_file() and path.suffix in {'.service', '.path', '.timer', '.target'}:
            units[path.name] = path.read_text()
    for path in sorted(tree.rglob('*.conf')):
        if path.parent.name.endswith('.d'):
            name = path.parent.name.removesuffix('.d')
            units[name] = units.get(name, '') + '\n' + path.read_text()
    return units


def bootstrap_dropins(script):
    # Parse the actual generated template and unit loop, not a duplicated list.
    match = re.search(r'for unit in (messagebox.target [^;]+); do\n'
                      r'.*?cat > .*? <<\x27EOF\x27\n(.*?)\nEOF', script, re.S)
    if match is None:
        raise AssertionError('bootstrap recovery gate template not found')
    return dict.fromkeys(match[1].split(), match[2] + '\n')


def with_bootstrap(units, script):
    result = dict(units)
    for name, raw in bootstrap_dropins(script).items():
        result[name] = result.get(name, '') + '\n' + raw
    return result


def executable_substitute(raw, *, main=True):
    """Keep ALL Unit sections and sandbox directives, replace only commands."""
    section, result = '', []
    for line in raw.replace('\\\n', ' ').splitlines():
        stripped = line.strip()
        if stripped.startswith('['):
            section = stripped[1:-1]
        if section == 'Service' and stripped.startswith(('ExecStart=', 'ExecStartPre=', 'ExecStartPost=',
                                                    'ExecStop=', 'ExecStopPost=', 'ExecReload=', 'ExecCondition=')):
            continue
        result.append(line)
    return '\n'.join(result) + ('\n[Service]\nExecStart=/bin/true\n' if main else '\n')


def boot_links(units, mode):
    """Bootstrap links plus setup's unconditional activators and selected mode."""
    links = defaultdict(set)
    baseline = json.loads((FIXTURES / 'bootstrap-state.json').read_text())
    for entry in baseline['entries']:
        if entry['kind'] == 'symlink' and '.wants/' in entry['path']:
            parent, unit = entry['path'].rsplit('.wants/', 1)
            links[Path(parent).name].add(unit)
    enabled = {name for name, state in baseline['services'].items() if state['enabled'] == 'enabled'}
    # The fixture's inferred runtime enablement is replaced by the boot-mode
    # generator's one selected link, not carried into the setup transaction.
    enabled.difference_update({'messagebox.target', 'comitup.service'})
    # These are enabled by setup independently of the selected boot mode.
    script = (ROOT / 'scripts/setup.sh').read_text().replace('\\\n', ' ')
    for line in script.splitlines():
        if 'systemctl enable' in line:
            enabled.update(word for word in line.split() if word.endswith(('.path', '.timer')) and word in units)
    enabled.update({'messagebox-mode-reconcile.path', 'messagebox-wifi-reset.service'})
    for name in enabled:
        for value in unit_settings(units[name]).get(('Install', 'WantedBy'), []):
            for parent in value.split():
                links[parent].add(name)
    links['multi-user.target'].add('comitup.service' if mode == 'setup' else 'messagebox.target')
    return links


def boot_model(units):
    # Explicit, limited OS model for portable contract tests. This is not an
    # exported field OS and is never used for the native field certification.
    return {
        'multi-user.target': '[Unit]\nRequires=basic.target\nAfter=basic.target\n',
        'basic.target': '[Unit]\nDefaultDependencies=no\nRequires=sysinit.target paths.target timers.target\n'
                        'After=sysinit.target paths.target timers.target\n',
        'sysinit.target': '[Unit]\nDefaultDependencies=no\nAfter=local-fs.target\n',
        'paths.target': '[Unit]\nDefaultDependencies=no\n',
        'timers.target': '[Unit]\nDefaultDependencies=no\n',
        **units,
    }


def ordering_cycle(units, *, links=None, initial=None):
    settings = {name: unit_settings(raw) for name, raw in units.items()}
    links = links or {}
    def values(name, key):
        result = set()
        for value in settings.get(name, {}).get(('Unit', key), []):
            result.update(value.split())
        return result
    def defaults(name):
        return settings.get(name, {}).get(('Unit', 'DefaultDependencies'), ['yes'])[-1] != 'no'
    pending, queued = list(initial or units), set()
    while pending:
        name = pending.pop()
        if name in queued:
            continue
        queued.add(name)
        pending += list(values(name, 'Wants') | values(name, 'Requires') | set(links.get(name, ())))
        if defaults(name) and name.endswith(('.service', '.path', '.timer')):
            pending.append('sysinit.target')
    edges = defaultdict(set)
    def edge(first, second):
        if first in queued and second in queued:
            edges[first].add(second)
    for name in queued:
        for dependency in values(name, 'After'):
            edge(dependency, name)
        for dependency in values(name, 'Before'):
            edge(name, dependency)
        if defaults(name):
            if name.endswith(('.service', '.path', '.timer')):
                edge('sysinit.target', name)
            if name.endswith('.service'):
                edge('basic.target', name)
            if name.endswith('.path'):
                edge(name, 'paths.target')
            if name.endswith('.timer'):
                edge(name, 'timers.target')
            if name.endswith('.target'):
                for dependency in values(name, 'Wants') | values(name, 'Requires') | set(links.get(name, ())):
                    if defaults(dependency):
                        edge(dependency, name)
        if name.endswith(('.path', '.timer')):
            section = 'Path' if name.endswith('.path') else 'Timer'
            trigger = settings.get(name, {}).get((section, 'Unit'), [name.rsplit('.', 1)[0] + '.service'])[-1]
            edge(name, trigger)
    visited, stack = set(), []
    def visit(name):
        if name in stack:
            return stack[stack.index(name):] + [name]
        if name in visited:
            return None
        stack.append(name)
        for successor in sorted(edges[name]):
            cycle = visit(successor)
            if cycle:
                return cycle
        stack.pop()
        visited.add(name)
        return None
    for name in sorted(queued):
        cycle = visit(name)
        if cycle:
            return cycle
    return None


def app_graph(repaired=False):
    units = with_bootstrap(shipped_units(ROOT / 'systemd'),
                           (ROOT / 'scripts/install/update-bootstrap.sh').read_text())
    if repaired:
        units[RECOVERY] += '\n' + repair.ORDERING.decode()
    return boot_model(units)


class BootOrderingTests(unittest.TestCase):
    def test_both_modes_cycle_before_repair_and_are_acyclic_after_single_switch(self):
        for mode in ('runtime', 'setup'):
            for fixed in (False, True):
                with self.subTest(mode=mode, repaired=fixed):
                    units = app_graph(fixed)
                    cycle = ordering_cycle(units, links=boot_links(units, mode), initial=['multi-user.target'])
                    if fixed:
                        self.assertIsNone(cycle)
                    else:
                        self.assertIn(RECOVERY, cycle)
                    links = boot_links(units, mode)
                    self.assertTrue({'messagebox-onboarding-complete.path', 'messagebox-onboarding-voice.path'}
                                    <= links['multi-user.target'])
                    selected = 'comitup.service' if mode == 'setup' else 'messagebox.target'
                    other = 'messagebox.target' if mode == 'setup' else 'comitup.service'
                    self.assertIn(selected, links['multi-user.target'])
                    self.assertNotIn(other, links['multi-user.target'])

    def test_executable_substitution_keeps_appended_unit_and_comitup_gates(self):
        units = app_graph()
        for name in ('messagebox-button.service', 'messagebox-mode-reconcile.service',
                     'comitup.service', 'comitup-web.service'):
            with self.subTest(unit=name):
                before, after = unit_settings(units[name]), unit_settings(executable_substitute(units[name]))
                self.assertEqual({k: v for k, v in before.items() if k[0] == 'Unit'},
                                 {k: v for k, v in after.items() if k[0] == 'Unit'})
                self.assertIn(RECOVERY, after['Unit', 'Requires'])

    def test_dead_boot_gates_the_reset_gesture_and_setup_completion(self):
        units = app_graph()
        reset = unit_settings(units['messagebox-wifi-reset.service'])
        self.assertIn(RECOVERY, reset['Unit', 'Requires'])
        self.assertIn('messagebox.onboarding.reset', units['messagebox-wifi-reset.service'])
        for name in ('comitup.service', 'messagebox-onboarding-complete.service'):
            self.assertIn(RECOVERY, unit_settings(units[name])['Unit', 'Requires'])
        from messagebox.onboarding import reset as gesture
        self.assertEqual(gesture.HOLD_SECONDS, 10)

    def test_wacli_activators_remain_byte_identical_to_pre_hotfix(self):
        units = shipped_units(ROOT / 'systemd')
        original = shipped_units(FIXTURES / 'shared/systemd')
        for name in ACTIVATORS:
            self.assertEqual(units[name], original[name])

    def test_exact_field_export_in_both_boot_modes(self):
        # No synthetic vendor units: without the exact export, this check is
        # explicitly unavailable rather than a false field-graph certificate.
        exported = os.environ.get('MSGBOX_BOOT_GRAPH_ROOT')
        if not exported or not shutil.which('systemd-analyze'):
            self.skipTest('requires systemd-analyze and exact field unit export MSGBOX_BOOT_GRAPH_ROOT')
        from boot_graph_export import verify
        verify(Path(exported))


class AtomicRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.box = FieldBox(self.temporary.name)
        journal = dict(self.box.bootstrap_journal, kind='application')
        self.box.worker.save_state(self.box.root, 'journal.json', journal)

    def test_atomic_switch_is_durable_idempotent_and_survives_application_rollback(self):
        box = self.box
        repair.repair(box.root, run=box.run)
        inode = (box.root / repair.DROPIN).stat().st_ino
        repair.repair(box.root, run=box.run)
        self.assertEqual((box.root / repair.DROPIN).stat().st_ino, inode)
        self.assertEqual((box.root / repair.DROPIN).read_bytes(), repair.ORDERING)
        def fail(*args):
            raise box.worker.UpdateError('health_timeout')
        with self.assertRaises(box.worker.UpdateError):
            box.install(health=fail)
        self.assertEqual((box.root / repair.DROPIN).read_bytes(), repair.ORDERING)

    def test_cut_at_every_write_boundary_keeps_old_or_complete_graph_and_retry_repairs(self):
        events = []
        repair.repair(self.box.root, run=self.box.run, step=lambda event, path: events.append((event, path)))
        for cut in range(len(events)):
            with self.subTest(boundary=cut), tempfile.TemporaryDirectory() as temporary:
                box = FieldBox(temporary)
                box.worker.save_state(box.root, 'journal.json', dict(box.bootstrap_journal, kind='application'))
                disk = DurableView(box.root)
                actual_fsync = os.fsync
                count = [0]
                def fsync(fd):
                    actual_fsync(fd)
                    disk.synced(fd)
                def stop(event, path):
                    count[0] += 1
                    if count[0] == cut + 1:
                        raise KeyboardInterrupt
                with mock.patch.object(os, 'fsync', side_effect=fsync), self.assertRaises(KeyboardInterrupt):
                    repair.repair(box.root, run=box.run, step=stop)
                disk.power_loss()
                path = box.root / repair.DROPIN
                if path.exists():
                    self.assertEqual(path.read_bytes(), repair.ORDERING)
                    for mode in ('runtime', 'setup'):
                        units = app_graph(True)
                        self.assertIsNone(ordering_cycle(units, links=boot_links(units, mode), initial=['multi-user.target']))
                repair.repair(box.root, run=box.run)
                self.assertEqual(path.read_bytes(), repair.ORDERING)

    def test_wacli_install_and_probation_locks_or_nonterminal_journal_do_not_mutate(self):
        box = self.box
        write(box.root / 'etc/messagebox/env', b'MSGBOX_TRANSPORT=wacli\n')
        self.assertFalse(repair.repair(box.root, run=box.run))
        self.assertFalse((box.root / repair.DROPIN).exists())
        write(box.root / 'etc/messagebox/env', b'MSGBOX_TRANSPORT=cloud\n')
        for phase in ('prepared', 'quiesced', 'switched', 'probation', 'restoring_activators', 'rollback_failed'):
            box.worker.save_state(box.root, 'journal.json', dict(box.bootstrap_journal, kind='application', phase=phase))
            with self.assertRaises(BlockingIOError):
                repair.repair(box.root, run=box.run)
        box.worker.save_state(box.root, 'journal.json', dict(box.bootstrap_journal, kind='application'))
        with box.worker.transaction_locks(box.root):
            with self.assertRaises(BlockingIOError):
                repair.repair(box.root, run=box.run)
        self.assertFalse((box.root / repair.DROPIN).exists())

    def test_reentry_after_terminal_rollback_requires_the_restored_known_good_pointer(self):
        box = self.box
        box.worker.save_state(box.root, 'journal.json', dict(box.bootstrap_journal, kind='application', phase='rolled_back'))
        box.worker.save_state(box.root, 'known-good.json', {'current': '/wrong'})
        with self.assertRaises(BlockingIOError):
            repair.repair(box.root, run=box.run)
        box.worker.save_state(box.root, 'known-good.json', {'current': box.old})
        repair.repair(box.root, run=box.run)
        self.assertTrue((box.root / repair.COMPLETE).exists())

    def test_reload_failure_keeps_repair_and_does_not_acknowledge_completion(self):
        def fail(*args, **kwargs):
            raise subprocess.CalledProcessError(1, 'systemctl')
        with self.assertRaises(subprocess.CalledProcessError):
            repair.repair(self.box.root, run=fail)
        self.assertEqual((self.box.root / repair.DROPIN).read_bytes(), repair.ORDERING)
        self.assertFalse((self.box.root / repair.COMPLETE).exists())
        repair.repair(self.box.root, run=self.box.run)

    def test_later_override_in_loaded_unit_prevents_completion_receipt(self):
        def wrong(command, **kwargs):
            if command[1] == 'show':
                return subprocess.CompletedProcess(command, 0, 'DefaultDependencies=yes\nAfter=basic.target\n')
            return self.box.run(command, **kwargs)
        with self.assertRaisesRegex(ValueError, 'loaded recovery ordering'):
            repair.repair(self.box.root, run=wrong)
        self.assertFalse((self.box.root / repair.COMPLETE).exists())

    def test_standard_sticky_run_lock_is_allowed_but_nonsticky_writable_directory_is_not(self):
        directory = self.box.root / 'run/lock'
        directory.mkdir(exist_ok=True)
        directory.chmod(0o777)
        with self.assertRaisesRegex(ValueError, 'unsafe boot repair directory'):
            repair.repair(self.box.root, run=self.box.run)
        directory.chmod(0o1777)
        repair.repair(self.box.root, run=self.box.run)
        self.assertEqual((self.box.root / repair.DROPIN).read_bytes(), repair.ORDERING)


@unittest.skipUnless(shutil.which('minisign'), 'real minisign required for signed hotfix delivery')
class HotfixDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.public, cls.secret = cls.root / 'test.pub', cls.root / 'test.key'
        subprocess.run(['minisign', '-G', '-W', '-p', str(cls.public), '-s', str(cls.secret)], check=True, capture_output=True)
        spec = importlib.util.spec_from_file_location('boot_hotfix_builder', ROOT / 'scripts/dev/build_boot_hotfix.py')
        cls.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.builder)
        cls.source = cls.root / 'source'
        cls.payload = cls.builder.prepare(cls.source)
        cls.assets = cls.package(cls.source, cls.payload, cls.root / 'assets', minimum='1.1.0')

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def package(cls, source, payload, assets, *, minimum, version='v0.3.16'):
        descriptor = build.controller_descriptor(source, 7 if minimum == '1.1.0' else 8, version)
        message = write(cls.root / 'descriptor', descriptor)
        signature = cls.root / 'descriptor.minisig'
        subprocess.run(['minisign', '-S', '-s', str(cls.secret), '-m', str(message), '-x', str(signature)], check=True, capture_output=True)
        payload = dict(payload)
        for name, raw in ((build.CONTROLLER_DESCRIPTOR, descriptor),
                          (build.CONTROLLER_SIGNATURE, (repr(signature.read_text()) + '\n').encode())):
            write(source / name, raw)
            payload[name] = 0o644
        mapper_spec = importlib.util.spec_from_file_location('hotfix_mapper', source / 'scripts/dev/release-manifest.py')
        mapper = importlib.util.module_from_spec(mapper_spec)
        mapper_spec.loader.exec_module(mapper)
        manifest = {'schema': 1, 'product': 'button-box', 'version': version, 'tag': version,
                    'commit': 'e' * 40, 'release_sequence': 1 if minimum == '1.1.0' else 2, 'channel': 'stable',
                    'min_updater_version': minimum, **build.SUPPORTED,
                    'files': [{'source': name, 'installed': target, 'sha256': digest(source / name)}
                              for name, target in sorted(mapper.installed_paths(source).items())]}
        if minimum == '1.1.0':
            manifest = cls.builder.manifest(source, tag=version, commit='e' * 40,
                                            sequence=1, supported=build.SUPPORTED)
        build.package(source, assets, manifest, payload, 0)
        release.inspect_archive(assets, 'button-box-' + version + '.tar.gz', controller_keys=cls.public.parent)
        subprocess.run(['minisign', '-S', '-s', str(cls.secret), '-m', str(assets / 'SHA256SUMS'),
                        '-x', str(assets / 'SHA256SUMS.minisig')], check=True, capture_output=True)
        return assets

    def deliver(self, box, assets, *, routed=False):
        # Real outer signature, hashes, extraction, frozen/current transaction.
        # The selected worker and systemd process identity remain host boundaries.
        with box.controller() as controller:
            bundle = box.root / 'var/lib/button-box-update/downloads' / ('c' * 32)
            bundle.mkdir(mode=0o700)
            for path in assets.iterdir():
                write(bundle / path.name, path.read_bytes(), 0o600)
            for path in controller.KEYS.glob('*.pub'):
                path.unlink()
            write(controller.KEYS / 'test.pub', self.public.read_bytes())
            version = json.loads((assets / 'release-manifest.json').read_bytes())['version']
            box.queue_bundle(controller, bundle, version)
            # FieldBox substitutes the outer verifier result by default; restore
            # the real verifier with this disposable pin for this forcing test.
            spec = importlib.util.spec_from_file_location('real_hotfix_verify', box.snapshot / 'scripts/install/release_verify.py')
            verify = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(verify)
            if box.worker is bounded_update:
                verify = slots.verify
            controller.verify_bundle = lambda *a, **k: verify.verify_bundle(*a, **k)
            transaction = controller.update.install_transaction
            def guarded(source, *args, **kwargs):
                return selector.install(box.root, box.worker, transaction, source, *args, **kwargs)
            with contextlib.ExitStack() as stack:
                if routed:
                    stack.enter_context(mock.patch.object(controller.update, 'install_transaction', side_effect=guarded))
                stack.enter_context(mock.patch.dict(sys.modules, {'release_verify': slots.verify}))
                for name in list(sys.modules):
                    if name == 'messagebox' or name.startswith('messagebox.'):
                        del sys.modules[name]
                controller.worker()
            self.assertEqual(controller.read('journal.json')['phase'], 'committed')
        source = box.installed(box.worker.controlled_release(box.root))
        spec = importlib.util.spec_from_file_location('delivered_boot_repair', source / 'messagebox/cloud_boot_repair.py')
        installed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installed)
        # Execute the actual delivered bootstrap command as the manager would.
        unit = (box.root / 'etc/systemd/system/messagebox-update-bootstrap.service').read_text()
        self.assertIn('/opt/messagebox/messagebox/cloud_boot_repair.py', unit)
        installed.repair(box.root, run=box.run)
        self.assertEqual((box.root / repair.DROPIN).read_bytes(), repair.ORDERING)
        return source

    def application_version(self, box, version):
        previous = box.old
        box.old = '/opt/messagebox-releases/' + version + '-' + 'b' * 40
        box.installed(previous).rename(box.root / box.old.lstrip('/'))
        pointer = box.root / 'opt/messagebox'
        pointer.unlink()
        pointer.symlink_to(box.old)
        write(box.installed('/opt/messagebox/release.json'),
              json.dumps({'version': version, 'commit': 'b' * 40}).encode())
        box.bootstrap_journal.update(old=box.old, new=box.old, version=version)
        box.worker.save_state(box.root, 'journal.json', box.bootstrap_journal)
        box.worker.save_state(box.root, 'known-good.json', {'current': box.old, 'previous': None})

    def test_exact_signed_legacy_capsule_through_every_frozen_worker_and_selector_guard(self):
        for version in (*PROVENANCE['releases'], 'current'):
            for routed in (False, True):
                with self.subTest(worker=version, selector=routed), tempfile.TemporaryDirectory() as directory:
                    box = FieldBox(directory, 'v0.3.12' if version == 'current' else version)
                    self.application_version(box, 'v0.3.12')
                    fixed = box.fixed_bytes()
                    if routed or version == 'current':
                        for name in ('messagebox-update-selector.service', 'messagebox-update-selector.timer'):
                            write(box.root / 'etc/systemd/system' / name, (ROOT / 'systemd' / name).read_bytes())
                    if routed:
                        path = 'systemd/messagebox-update-supervisor.service.d/messagebox.conf'
                        write(box.root / 'etc/systemd/system' / Path(path).relative_to('systemd'), (ROOT / path).read_bytes())
                        for path, raw in slots.routes().items():
                            write(box.root / path, raw)
                    with contextlib.ExitStack() as stack:
                        if version == 'current':
                            clock = box.worker.time
                            box.worker = bounded_update
                            stack.enter_context(mock.patch.object(bounded_update, 'time', clock))
                            stack.enter_context(mock.patch.object(bounded_update, '_boot', return_value='synthetic-boot'))
                        source = self.deliver(box, self.assets, routed=routed)
                    self.assertEqual(box.fixed_bytes(), fixed)
                    # The real legacy program and sound assets are preserved.
                    self.assertEqual((source / 'messagebox/button_send.py').read_bytes(),
                                     (self.source / 'messagebox/button_send.py').read_bytes())

    def test_current_complete_payload_through_current_worker(self):
        source = self.root / 'current'
        source.mkdir()
        payload = {}
        for name in (ROOT / 'scripts/release/payload.txt').read_text().splitlines():
            if not name or name.startswith('#'):
                continue
            mode = 0o755 if (ROOT / name).stat().st_mode & 0o111 else 0o644
            write(source / name, (ROOT / name).read_bytes(), mode)
            payload[name] = mode
        assets = self.package(source, payload, self.root / 'current-assets', minimum='1.3.0', version='v0.3.17')
        with tempfile.TemporaryDirectory() as directory:
            box = FieldBox(directory)
            self.application_version(box, 'v0.3.15')
            for name in ('messagebox-update-selector.service', 'messagebox-update-selector.timer'):
                write(box.root / 'etc/systemd/system' / name, (ROOT / 'systemd' / name).read_bytes())
            old_time = box.worker.time
            box.worker = bounded_update
            with mock.patch.object(bounded_update, 'time', old_time), \
                 mock.patch.object(bounded_update, '_boot', return_value='synthetic-boot'):
                self.deliver(box, assets)

    def test_legacy_capsule_refuses_newer_application_before_any_installed_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            box = FieldBox(directory)
            self.application_version(box, 'v0.3.15')
            keys = box.root / 'etc/button-box-update/release-keys'
            for path in keys.glob('*.pub'):
                path.unlink()
            write(keys / 'test.pub', self.public.read_bytes())
            before = box.tree()
            with self.assertRaisesRegex(box.worker.UpdateError, 'staged mode migration check failed'):
                self.deliver(box, self.assets)
            self.assertEqual(box.tree(), before)
