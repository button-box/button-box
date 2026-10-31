"""Prepare the legacy boot hotfix source for normal offline release packaging.

This keeps the selected application's program and sounds. The repair-only
profile stops after repair; the bridge adds the existing A/B bootstrap/trial.
No commit, tag, signature, publication or device operation is performed here.
"""

import argparse
import hashlib
import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[2]
SERVICE = b'''[Unit]
Description=Persist Cloud boot ordering repair after signed application commit
After=messagebox-update-supervisor.service messagebox-update-install.service
ConditionPathExists=!/run/messagebox-boot-repair-complete
StartLimitIntervalSec=0

[Service]
Type=oneshot
UMask=0022
ExecStart=/usr/bin/python3 -I -B /opt/messagebox/messagebox/cloud_boot_repair.py
TimeoutStartSec=60
TimeoutStopSec=10
KillMode=control-group
'''
CONTROLLERS = ('bounded_update', 'health-probe', 'release_verify', 'trust_update',
               'update_controller', 'update_discovery')
APPLICATION_GUARD = '''

# The legacy capsule must never replace a newer application's program or sounds.
_boot_hotfix_mode_check = check
def check(*args, **kwargs):
    import json
    marker = Path(kwargs['marker'])
    root = marker.parent.parent.parent
    pointer = root / 'opt/messagebox'
    target = Path(os.readlink(pointer))
    if target.parent != Path('/opt/messagebox-releases'):
        raise MigrationError('legacy hotfix application pointer is unsafe')
    installed = root / str(target).lstrip('/')
    identity = json.loads((installed / 'release.json').read_bytes())
    if identity.get('version', '').removeprefix('v') not in _APPLICATIONS:
        if not _BRIDGE:
            raise MigrationError('legacy hotfix requires the ' + _BASE + ' application')
        import hashlib
        raw = (installed / 'release-manifest.json').read_bytes()
        previous = json.loads(raw)
        if (previous.get('updater_bridge_base') != _BASE
                or identity.get('manifest_sha256') != hashlib.sha256(raw).hexdigest()
                or previous.get('version') != identity.get('version')
                or previous.get('commit') != identity.get('commit')):
            raise MigrationError('legacy hotfix requires the ' + _BASE + ' application')
    mode = _boot_hotfix_mode_check(*args, **kwargs)
    # load_candidate invokes this only after outer authentication and complete
    # payload validation, under the frozen transaction's update -> mode locks.
    # This drop-in is deliberately absent from application rollback destinations.
    import importlib.util
    helper = Path(__file__).resolve().parents[2] / 'messagebox/cloud_boot_repair.py'
    spec = importlib.util.spec_from_file_location('capsule_boot_preflight', helper)
    repair = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair)
    repair.repair(root, transaction_preflight=True)
    return mode
'''


BRIDGE_APPLICATIONS = ('0.3.9', '0.3.10', '0.3.12')


def prepare(destination, *, base='v0.3.12', repo=ROOT, bridge=False):
    repo, destination = Path(repo), Path(destination).resolve()
    # Current voice releases require their compatible selected worker. Never
    # silently downgrade one to this legacy application's program or sounds.
    if base != 'v0.3.12':
        raise ValueError('legacy capsule requires base v0.3.12')
    listing = subprocess.check_output(['git', '-C', str(repo), 'show', base + ':scripts/release/payload.txt'], text=True)
    payload = {line for line in listing.splitlines() if line and not line.startswith('#')}
    archive = subprocess.check_output(['git', '-C', str(repo), 'archive', '--format=tar', base])
    destination.mkdir(mode=0o755)
    modes = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for member in source:
            if member.name not in payload:
                continue
            path = PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or '..' in path.parts:
                raise ValueError('unsafe legacy payload')
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            target.write_bytes(source.extractfile(member).read())
            modes[member.name] = 0o755 if member.mode & 0o111 else 0o644
            target.chmod(modes[member.name])
    if modes.keys() != payload:
        raise ValueError('legacy payload is incomplete')
    overlays = ['messagebox/cloud_boot_repair.py',
                'systemd/messagebox-update-selector.service', 'systemd/messagebox-update-selector.timer',
                'systemd/messagebox-update-supervisor.service.d/messagebox.conf']
    overlays += ['scripts/install/' + name + '.py' for name in CONTROLLERS]
    if bridge:
        overlays += ['messagebox/controller_selector.py', 'messagebox/controller_slots.py']
    for name in overlays:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        target.write_bytes((repo / name).read_bytes())
        target.chmod(0o644)
        modes[name] = 0o644
    target = destination / 'systemd/messagebox-update-bootstrap.service'
    service = SERVICE
    if bridge:
        service = (repo / 'systemd/messagebox-update-bootstrap.service').read_bytes()
        service = service.replace(b'ExecStartPost=',
            b'ExecStartPost=/usr/bin/python3 -B /opt/messagebox/messagebox/controller_slots.py promote\nExecStartPost=', 1)
        service = service.replace(b'TimeoutStartSec=60', b'TimeoutStartSec=300')
    target.write_bytes(service)
    target.chmod(0o644)
    modes[target.relative_to(destination).as_posix()] = 0o644
    migration = destination / 'scripts/install/messagebox-mode-migrate.py'
    migration.write_text(migration.read_text() + '\n_BASE = ' + repr(base)
                         + '\n_BRIDGE = ' + repr(bridge)
                         + '\n_APPLICATIONS = ' + repr(BRIDGE_APPLICATIONS if bridge else ('0.3.12',))
                         + APPLICATION_GUARD)
    return modes


def manifest(source, *, tag, commit, sequence, supported, base='v0.3.12', bridge=False):
    spec = importlib.util.spec_from_file_location('legacy_hotfix_mapper', source / 'scripts/dev/release-manifest.py')
    mapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mapper)
    return {'schema': 1, 'product': 'button-box', 'version': tag, 'tag': tag, 'commit': commit,
            'release_sequence': sequence, 'channel': 'canary' if '-canary.' in tag else 'stable',
            'min_updater_version': '1.1.0',
            ('updater_bridge_base' if bridge else 'boot_hotfix_base'): base, **supported,
            'files': [{'source': name, 'installed': target,
                       'sha256': hashlib.sha256((source / name).read_bytes()).hexdigest()}
                      for name, target in sorted(mapper.installed_paths(source).items())]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='v0.3.12')
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    prepare(options.output, base=options.base)


if __name__ == '__main__':
    main()
