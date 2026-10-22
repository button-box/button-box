"""Run the full bootstrap shell with a temp filesystem and explicit OS boundaries.

The shell, globs, install(1), Python key/policy code, and layout conversion are
real. Root ownership, Pi identity, accounts, and systemd are host substitutes.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from field_updater import FieldBox, write

ROOT = Path(__file__).resolve().parents[1]


class BootstrapCommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='first-bootstrap-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        box = FieldBox(self.base / 'field')
        self.root = box.root
        # Reverse the recorded completed layout to an in-place first install.
        (self.root / 'opt/messagebox').unlink()
        (self.root / box.old.lstrip('/')).rename(self.root / 'opt/messagebox')
        for name in ('usr/lib/messagebox-update', 'etc/button-box-update', 'var/lib/button-box-update'):
            shutil.rmtree(self.root / name)
        self.repo = self.base / 'checkout'
        for name in ('systemd', 'config/release-keys', 'config/emergency-keys'):
            shutil.copytree(ROOT / name, self.repo / name)
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

    def test_complete_first_install_copies_units_and_dropins_then_converts_layout(self):
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

    def test_wacli_first_install_refuses_before_controller_mutation(self):
        result = self.run_bootstrap('wacli')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires a Cloud box', result.stderr)
        self.assertFalse((self.root / 'usr/lib/messagebox-update').exists())
        self.assertFalse((self.root / 'opt/messagebox').is_symlink())
