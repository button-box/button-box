"""Install each release with its executing field controller, without hardware."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = ROOT / 'tests/fixtures/pi-v0.3.12/bounded_update.py'
MODULES = ('bounded_update', 'release_verify', 'trust_update', 'update_controller', 'update_discovery', 'health-probe')


def probe(controller):
    # A fresh interpreter lets candidate validation import the actual staged code.
    from test_bounded_update import Fixture
    sys.path.insert(0, str(ROOT / 'scripts/install'))
    spec = importlib.util.spec_from_file_location('executing_controller', controller)
    update = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(update)
    update._boot = lambda: "synthetic-boot"
    with tempfile.TemporaryDirectory() as directory:
        fixture = Fixture(directory)
        fixture.metadata.write_text(json.dumps({'version': 'v0.3.12', 'commit': 'b' * 40}))
        for entry in fixture.manifest_data['files']:
            if Path(entry['installed']).name in update.FIXED_UNITS or entry['installed'].endswith('requirements-nfc.txt'):
                target = update._rooted(fixture.root, entry['installed'])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((fixture.source / entry['source']).read_bytes())
        for name in MODULES:
            shutil.copyfile(ROOT / 'scripts/install' / (name + '.py'), fixture.source / 'scripts/install' / (name + '.py'))
        old = update.bootstrap_layout(root=fixture.root, run=fixture.systemctl)
        fixture.manifest_data['version'] = 'v0.3.13-canary.1'
        fixture.write_manifest()
        update.install_transaction(fixture.source, fixture.manifest, root=fixture.root, run=fixture.systemctl,
            operation_id='synthetic-voice-transition', sequence=1, health=lambda *args: None)
        assert update.controlled_release(fixture.root) != old
        assert update.read_state(fixture.root, 'result.json')['state'] == 'installed'
        assert {path: __import__('hashlib').sha256(path.read_bytes()).hexdigest() for path in fixture.private_files} == fixture.private_hashes
        print('Full payload installed by ' + str(controller))


class VoiceReleaseTransitionTests(unittest.TestCase):
    def test_executing_field_controller_installs_this_release(self):
        # Use the integrated A/B controller, with the voice sound allowance.
        controller = ROOT / 'scripts/install/bounded_update.py' if (ROOT / 'sounds/voices/buttonbox').exists() else HISTORICAL
        result = subprocess.run([sys.executable, __file__, '--probe', str(controller)],
                                text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless((ROOT / 'sounds/voices/buttonbox').exists(), 'step-2 payload only')
    def test_v0312_controller_rejects_step2_before_install(self):
        result = subprocess.run([sys.executable, __file__, '--probe', str(HISTORICAL)],
                                text=True, capture_output=True, timeout=120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('source outside the install allowlist', result.stderr)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        probe(Path(sys.argv[2]))
    else:
        unittest.main()
