"""Native verification of an exact, local field unit export; no OS stubs.

Export /etc/systemd/system, /usr/lib/systemd/system and generated unit links.
Do not export environment files, credentials, device data or controller state.
"""

import shutil
import os
import subprocess
import tempfile
from pathlib import Path

from messagebox import cloud_boot_repair as repair


def verify(exported):
    from test_boot_ordering import executable_substitute
    exported = Path(exported).resolve()
    vendor = exported / 'usr/lib/systemd/system'
    for name in ('comitup.service', 'comitup-web.service', 'basic.target', 'sysinit.target',
                 'paths.target', 'timers.target', 'systemd-tmpfiles-setup.service', 'multi-user.target'):
        if not (vendor / name).is_file():
            raise AssertionError('exact field OS/Comitup unit missing: ' + name)
    for mode in ('runtime', 'setup'):
        for fixed in (False, True):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for relative in ('usr/lib/systemd/system', 'etc/systemd/system',
                                 'run/systemd/generator', 'run/systemd/generator.early', 'run/systemd/generator.late'):
                    source = exported / relative
                    if source.is_dir():
                        shutil.copytree(source, root / relative, symlinks=True)
                alias = exported / 'lib'
                if alias.is_symlink():
                    target = os.readlink(alias)
                    if target not in ('usr/lib', '/usr/lib'):
                        raise AssertionError('unexpected field /lib alias')
                    (root / 'lib').symlink_to(target)
                elif any(path.is_symlink() and os.readlink(path).startswith('/lib/') for path in root.rglob('*')):
                    raise AssertionError('field export is missing its /lib alias')
                # Substitute commands only. Later Unit sections and sandbox
                # directives (including PrivateTmp's implicit edges) survive.
                for path in root.rglob('*.service'):
                    if path.is_file() and not path.is_symlink():
                        path.write_text(executable_substitute(path.read_text()))
                for path in root.rglob('*.conf'):
                    if path.is_file() and not path.is_symlink() and path.parent.name.endswith('.service.d'):
                        path.write_text(executable_substitute(path.read_text(), main=False))
                (root / repair.DROPIN).unlink(missing_ok=True)
                if fixed:
                    path = root / repair.DROPIN
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(repair.ORDERING)
                selected = 'comitup.service' if mode == 'setup' else 'messagebox.target'
                for directory in root.rglob('multi-user.target.wants'):
                    for name in ('comitup.service', 'messagebox.target'):
                        (directory / name).unlink(missing_ok=True)
                wants = root / 'etc/systemd/system/multi-user.target.wants'
                wants.mkdir(parents=True, exist_ok=True)
                target = ('/usr/lib/systemd/system/' if mode == 'setup' else '/etc/systemd/system/') + selected
                (wants / selected).symlink_to(target)
                # Require the exported always-enabled watchers, rather than
                # silently inventing an incomplete boot transaction.
                for name in ('messagebox-onboarding-complete.path', 'messagebox-onboarding-voice.path'):
                    if not any((path / name).is_symlink() for path in root.rglob('multi-user.target.wants')):
                        raise AssertionError('field enablement link missing: ' + name)
                default = root / 'etc/systemd/system/default.target'
                default.unlink(missing_ok=True)
                default.symlink_to('/usr/lib/systemd/system/multi-user.target')
                binary = root / 'bin/true'
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(b'#!/bin/sh\nexit 0\n')
                binary.chmod(0o755)
                result = subprocess.run(['systemd-analyze', 'verify', '--man=no', '--generators=no',
                                         '--root=' + str(root), 'default.target'],
                                        capture_output=True, text=True, timeout=60)
                output = result.stdout + result.stderr
                if fixed:
                    if result.returncode:
                        raise AssertionError(mode + ' repaired field graph rejected:\n' + output)
                elif 'ordering cycle' not in output.lower():
                    raise AssertionError(mode + ' did not reproduce the field ordering cycle:\n' + output)
