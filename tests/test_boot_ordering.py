"""Boot graph contracts, including the unchanged drop-ins already in the field."""

import re
import shutil
import subprocess
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest import mock

from field_updater import FIXTURES, PROVENANCE, FieldBox, digest, service_environment, write
from messagebox import controller_selector as selector, controller_slots as slots
from test_bounded_update import bounded_update, release_manifest
from test_release_tooling import build


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


def ordering_cycle(units):
    """Model shipped ordering and the relevant systemd default boot edges."""
    edges = defaultdict(set)
    edges['local-fs.target'].add('sysinit.target')
    for prerequisite in ('sysinit.target', 'paths.target', 'timers.target', 'sockets.target'):
        edges[prerequisite].add('basic.target')
    for name, raw in units.items():
        settings = unit_settings(raw)
        defaults = settings.get(('Unit', 'DefaultDependencies'), ['yes'])[-1] != 'no'
        for key in ('After', 'Before'):
            for value in settings.get(('Unit', key), []):
                for dependency in value.split():
                    first, second = (dependency, name) if key == 'After' else (name, dependency)
                    edges[first].add(second)
        if not defaults:
            continue
        if name.endswith(('.service', '.path', '.timer')):
            edges['sysinit.target'].add(name)
            edges[name].add('shutdown.target')
        if name.endswith('.service'):
            edges['basic.target'].add(name)
        if name.endswith('.path'):
            edges[name].add('paths.target')
        if name.endswith('.timer'):
            edges[name].add('timers.target')
        if name.endswith('.target'):
            for key in ('Wants', 'Requires'):
                for value in settings.get(('Unit', key), []):
                    for dependency in value.split():
                        edges[dependency].add(name)
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

    for name in sorted(list(edges)):
        cycle = visit(name)
        if cycle:
            return cycle
    return None


class BootOrderingTests(unittest.TestCase):
    def test_all_shipped_units_and_generated_dropins_have_no_cycle(self):
        units = shipped_units(ROOT / 'systemd')
        script = (ROOT / 'scripts/install/update-bootstrap.sh').read_text()
        self.assertIsNone(ordering_cycle(units), 'wacli graph')
        self.assertIsNone(ordering_cycle(with_bootstrap(units, script)), 'Cloud graph')
        # Permanent selector routes replace ExecStart only; include them too.
        for path, raw in slots.routes().items():
            name = Path(path).parent.name.removesuffix('.d')
            units[name] += '\n' + raw.decode()
        self.assertIsNone(ordering_cycle(with_bootstrap(units, script)), 'selector graph')

    def test_frozen_graph_reproduces_cycle_and_each_activator_can_reintroduce_it(self):
        frozen = FIXTURES / 'shared'
        script = (frozen / 'scripts/install/update-bootstrap.sh').read_text()
        self.assertIn(RECOVERY, ordering_cycle(with_bootstrap(shipped_units(frozen / 'systemd'), script)))
        units = with_bootstrap(shipped_units(ROOT / 'systemd'), script)
        self.assertIsNone(ordering_cycle(units), 'must work with old drop-ins unchanged')
        for name in ACTIVATORS:
            with self.subTest(unit=name):
                broken = dict(units)
                broken[name] = broken[name].replace('DefaultDependencies=no', 'DefaultDependencies=yes').replace('After=basic.target\n', '')
                cycle = ordering_cycle(broken)
                self.assertIn(name, cycle)
                self.assertIn(RECOVERY, cycle)
                self.assertIn('basic.target', cycle)

    def test_wacli_triggers_selection_and_shutdown_semantics_are_preserved(self):
        old = shipped_units(FIXTURES / 'shared/systemd')
        current = shipped_units(ROOT / 'systemd')
        for name in ACTIVATORS:
            with self.subTest(unit=name):
                before, after = unit_settings(old[name]), unit_settings(current[name])
                for key, values in before.items():
                    self.assertEqual(after[key], values)
                self.assertNotIn(RECOVERY, current[name])
                self.assertEqual(after['Unit', 'Requires'], ['sysinit.target'])
                self.assertEqual(after['Unit', 'After'], ['basic.target'])
                self.assertEqual(after['Unit', 'Conflicts'], ['shutdown.target'])
                self.assertEqual(after['Unit', 'Before'], ['shutdown.target'])
        for name in (RECOVERY, 'messagebox-update-controller.service', 'messagebox-update-supervisor.service'):
            self.assertEqual(current[name].split('\n[Service]')[0], old[name].split('\n[Service]')[0])

    def test_settled_boot_discovery_retry_can_pull_in_recovery_without_path_jobs(self):
        units = shipped_units(FIXTURES / 'shared/systemd')
        controller = 'messagebox-update-controller.service'
        discovery = 'messagebox-update-discovery.service'
        self.assertIn(RECOVERY, unit_settings(units[controller])['Unit', 'Requires'])
        self.assertIn(controller, unit_settings(units[discovery])['Unit', 'Requires'])
        # After= does not enqueue an already-settled application activator.
        self.assertIsNone(ordering_cycle({name: units[name] for name in (RECOVERY, controller, discovery)}))

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze unavailable; static graph checked')
    def test_native_systemd_dependency_graph(self):
        # Isolated graph verification needs no running manager or Pi paths.
        # Retain real Unit/Path/Timer sections, substitute service executables.
        units = with_bootstrap(shipped_units(ROOT / 'systemd'),
                               (ROOT / 'scripts/install/update-bootstrap.sh').read_text())
        units.update({
            'basic.target': '[Unit]\nRequires=sysinit.target paths.target timers.target sockets.target\n'
                            'After=sysinit.target paths.target timers.target sockets.target\n',
            'sysinit.target': '[Unit]\nAfter=local-fs.target\n',
        })
        # Verify one combined boot transaction as well as each unit separately.
        units['default.target'] = '[Unit]\nAfter=basic.target\nWants=' + ' '.join(sorted(units)) + '\n'
        dependencies = {'local-fs.target', 'paths.target', 'timers.target', 'sockets.target', 'shutdown.target'}
        for raw in units.values():
            settings = unit_settings(raw)
            for key in ('After', 'Before', 'Wants', 'Requires', 'Conflicts', 'PartOf'):
                for value in settings.get(('Unit', key), []):
                    dependencies.update(value.split())
        for name in dependencies - units.keys():
            units[name] = '[Unit]\nDescription=OS dependency stub\n'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / 'etc/systemd/system'
            directory.mkdir(parents=True)
            write(root / 'bin/true', b'#!/bin/sh\nexit 0\n', 0o755)
            paths = []
            for name, raw in units.items():
                if name.endswith('.service'):
                    raw = raw.split('[Service]')[0] + '[Service]\nExecStart=/bin/true\n'
                paths.append(str(write(directory / name, raw.encode())))
            result = subprocess.run(['systemd-analyze', 'verify', '--man=no', '--root=' + str(root), *paths],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class BootRepairDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.assets = Path(cls.temporary.name).resolve() / 'assets'
        payload = {name: 0o755 if (ROOT / name).stat().st_mode & 0o111 else 0o644
                   for name in (ROOT / 'scripts/release/payload.txt').read_text().splitlines()
                   if name and not name.startswith('#')}
        files = [{'source': name, 'installed': target, 'sha256': digest(ROOT / name)}
                 for name, target in sorted(release_manifest.installed_paths(ROOT).items())]
        if any(item['source'] not in payload for item in files):
            raise AssertionError('installed source missing from release payload')
        manifest = {'schema': 1, 'product': 'button-box', 'version': 'v0.3.16', 'tag': 'v0.3.16',
                    'commit': 'e' * 40, 'release_sequence': 1, 'channel': 'stable',
                    'min_updater_version': '1.1.0', **build.SUPPORTED, 'files': files}
        build.package(ROOT, cls.assets, manifest, payload, 0)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_repair_delta_reaches_frozen_workers_with_and_without_selector(self):
        for version in PROVENANCE['releases']:
            for routed in (False, True):
                with self.subTest(version=version, selector=routed), tempfile.TemporaryDirectory() as directory:
                    box = FieldBox(directory, version)
                    routes = slots.routes() if routed else {}
                    if routed:
                        for name in ('messagebox-update-selector.service', 'messagebox-update-selector.timer'):
                            write(box.root / 'etc/systemd/system' / name, (ROOT / 'systemd' / name).read_bytes())
                        relative = 'systemd/messagebox-update-supervisor.service.d/messagebox.conf'
                        write(box.root / 'etc' / relative, (ROOT / relative).read_bytes())
                        for path, raw in routes.items():
                            write(box.root / path, raw)
                    for name in ACTIVATORS:
                        path = next((ROOT / 'systemd').rglob(name))
                        box.add(path.relative_to(ROOT).as_posix(), path.read_bytes())
                    if routed:
                        for name in ('messagebox-update-selector.service', 'messagebox-update-selector.timer'):
                            box.add('systemd/' + name, (ROOT / 'systemd' / name).read_bytes())
                    gates = {name: box.installed('/etc/systemd/system/' + name + '.d/software-update.conf').read_bytes()
                             for name in ACTIVATORS}
                    recovery = box.installed('/etc/systemd/system/' + RECOVERY).read_bytes()
                    with box.controller() as controller:
                        transaction = controller.update.install_transaction
                        def guarded(source, *args, **kwargs):
                            return selector.install(box.root, box.worker, transaction, source, *args, **kwargs)
                        box.bundle(controller)
                        if routed:
                            with mock.patch.object(controller.update, 'install_transaction', side_effect=guarded):
                                controller.worker()
                        else:
                            controller.worker()
                        self.assertEqual(controller.read('journal.json')['phase'], 'committed')
                    installed = shipped_units(box.root / 'etc/systemd/system')
                    self.assertIsNone(ordering_cycle(installed))
                    for name in ACTIVATORS:
                        path = box.installed('/etc/systemd/system/' + name)
                        self.assertEqual(path.read_bytes(), next((ROOT / 'systemd').rglob(name)).read_bytes())
                        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
                        self.assertEqual(box.installed('/etc/systemd/system/' + name + '.d/software-update.conf').read_bytes(), gates[name])
                    self.assertEqual(box.installed('/etc/systemd/system/' + RECOVERY).read_bytes(), recovery)
                    for path, raw in routes.items():
                        self.assertEqual((box.root / path).read_bytes(), raw)

    def test_current_complete_payload_installs_with_current_worker(self):
        # A current controller accepts the complete current application contract.
        # Systemd and health are host substitutes, as in the field harness.
        with tempfile.TemporaryDirectory() as directory:
            box = FieldBox(directory)
            source = Path(directory).resolve() / 'extracted'
            with service_environment():
                slots.verify.extract_bundle(self.assets, source, 'v0.3.16')
            for name in ('messagebox-update-selector.service', 'messagebox-update-selector.timer'):
                write(box.root / 'etc/systemd/system' / name, (ROOT / 'systemd' / name).read_bytes())
            with mock.patch.dict('sys.modules', {'release_verify': slots.verify}), \
                 mock.patch.object(bounded_update, '_boot', return_value='synthetic-boot'), \
                 mock.patch.object(bounded_update, 'time', box.worker.time):
                bounded_update.install_transaction(source, source / 'release-manifest.json',
                    root=box.root, run=box.run, health=lambda *args: None, sequence=1)
            self.assertEqual(bounded_update.read_state(box.root, 'journal.json')['phase'], 'committed')
            self.assertIsNone(ordering_cycle(shipped_units(box.root / 'etc/systemd/system')))

    def test_legacy_worker_requires_controller_bridge_for_current_complete_payload(self):
        # Existing buttonbox sound paths predate this hotfix and are outside the
        # legacy worker's sound allowlist. Do not mistake a delta proof for full
        # current-release compatibility with that worker.
        with tempfile.TemporaryDirectory() as directory:
            box = FieldBox(directory)
            before = box.tree()
            with self.assertRaisesRegex(box.worker.UpdateError, 'outside the install allowlist'):
                box.run_built_release(self.assets)
            self.assertEqual(box.tree(), before)

    def test_failed_install_restores_old_activators_and_dropins(self):
        with tempfile.TemporaryDirectory() as directory:
            box = FieldBox(directory)
            before = box.tree()
            for name in ACTIVATORS:
                path = next((ROOT / 'systemd').rglob(name))
                box.add(path.relative_to(ROOT).as_posix(), path.read_bytes())
            def fail(*args):
                raise box.worker.UpdateError('health_timeout')
            with self.assertRaises(box.worker.UpdateError):
                box.install(health=fail)
            self.assertEqual(box.worker.read_state(box.root, 'journal.json')['phase'], 'rolled_back')
            self.assertEqual(box.tree(), before)
            self.assertFalse(box.installed(box.worker.INHIBIT).exists())

    def test_interrupted_partial_unit_repair_uses_existing_boot_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            box = FieldBox(directory)
            before = box.tree()
            for name in ACTIVATORS:
                path = next((ROOT / 'systemd').rglob(name))
                box.add(path.relative_to(ROOT).as_posix(), path.read_bytes())
            install, changed = box.worker._atomic_install, []
            def cut(source, destination, mode):
                if destination.name in ACTIVATORS:
                    changed.append(destination.name)
                    if len(changed) == 3:
                        raise KeyboardInterrupt('simulated interruption between unit writes')
                return install(source, destination, mode)
            with mock.patch.object(box.worker, '_atomic_install', side_effect=cut), self.assertRaises(KeyboardInterrupt):
                box.install()
            self.assertEqual(len(changed), 3)
            self.assertTrue(box.installed(box.worker.INHIBIT).exists())
            box.worker.recover_transaction(root=box.root, run=box.run, start=False)
            self.assertEqual(box.worker.read_state(box.root, 'journal.json')['phase'], 'rolled_back')
            self.assertEqual(box.tree(), before)
            self.assertFalse(box.installed(box.worker.INHIBIT).exists())

    def test_dead_button_blocks_frozen_automatic_install_even_after_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            box = FieldBox(directory)
            with box.controller() as controller:
                controller.save('available.json', {'version': 'v0.3.16'})
                write(controller.RUN / 'confirmation-path.json', b'{}')
                with mock.patch.object(controller, 'unit_pid', return_value=0), \
                     mock.patch.object(Path, 'is_file', return_value=True), \
                     mock.patch.object(controller.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                     mock.patch.object(controller, 'start_install') as start:
                    self.assertFalse(controller.installed())
                    controller.Controller().automatic_if_ready(None)
                start.assert_not_called()
