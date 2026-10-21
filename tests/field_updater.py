"""Host harness whose worker always comes from the frozen field sources.

The default payload and recorded device baseline use synthetic variable data.
Real build output can replace the default payload. systemd and health are
explicit host substitutes. File validation, locks, writes, journals, switch
and recovery run through the frozen worker. This does not simulate power loss or boot a Pi.
"""

import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


FIXTURES = Path(__file__).resolve().parent / "fixtures/field-updater"
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_text())
CONTROLLER = ("bounded_update", "release_verify", "trust_update", "update_controller",
              "update_discovery", "health-probe")


def load_frozen(version, relative):
    path = FIXTURES / PROVENANCE["releases"][version]["snapshot"] / relative
    spec = importlib.util.spec_from_file_location("field_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


@contextlib.contextmanager
def service_environment():
    # Model the systemd process boundary, not the invoking test shell. Frozen
    # extraction creates intermediate directories with the process umask.
    # controller.service explicitly sets 0022; install.service inherits the
    # systemd manager default (inferred as 0022, not observed on a field box).
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def mkdir(path, mode=0o755):
    """Set every new parent mode explicitly, independent of the host umask."""
    if not path.exists():
        mkdir(path.parent)
        path.mkdir(mode=mode)
        path.chmod(mode)
    return path


def write(path, content, mode=0o644):
    mkdir(path.parent)
    path.write_bytes(content)
    path.chmod(mode)
    return path


def wav(path, size=46):
    """Write a valid mono, 16-bit, 48 kHz WAV of the requested even size."""
    mkdir(path.parent)
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0" * (size - 44))
    path.chmod(0o644)
    return path


class FieldBox:
    def __init__(self, directory, version="v0.3.12", *, free_bytes=4 * 1024**3):
        self.version = version
        self.root = Path(directory).resolve() / "device"
        self.source = Path(directory).resolve() / "payload"
        self.snapshot = FIXTURES / PROVENANCE["releases"][version]["snapshot"]
        self.worker = load_frozen(version, "scripts/install/bounded_update.py")
        self.mapper = load_frozen(version, "scripts/dev/release-manifest.py")
        self.manifest = self.source / "release-manifest.json"
        self.now = 0.0
        # Only replace the worker's clock, not the process-wide time module.
        self.worker.time = SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep)
        self.worker._boot = lambda: "synthetic-boot"
        self.commands = []
        self.free_bytes = free_bytes
        self.disk_queries = []
        # Give this frozen module its own shutil namespace; never patch the
        # process-wide shutil module or change the worker's disk threshold.
        self.worker.shutil = SimpleNamespace(**vars(shutil))
        self.worker.shutil.disk_usage = self.disk_usage
        self.bootstrap = json.loads((FIXTURES / "bootstrap-state.json").read_text())
        self.states = {unit: dict(state) for unit, state in self.bootstrap["services"].items()}
        self.old = self.bootstrap["old_release"]
        mkdir(self.root)
        mkdir(self.source)
        self.load_bootstrap()
        self.bootstrap_journal = self.worker.read_state(self.root, "journal.json")

        # Seed only frozen sources and synthetic data. Future product changes
        # must not silently change the field baseline or its payload floor.
        for relative in ("scripts/dev/release-manifest.py",
                         "scripts/install/messagebox-mode-migrate.py",
                         "systemd/messagebox-mode-generator"):
            write(self.source / relative, (self.snapshot / relative).read_bytes(),
                  0o755 if relative.endswith("messagebox-mode-generator") else 0o644)
        for name in CONTROLLER:
            content = (self.snapshot / f"scripts/install/{name}.py").read_bytes()
            write(self.source / f"scripts/install/{name}.py", content)
        for relative in sorted(self.worker.EXPLICIT_TARGETS):
            path = self.source / relative
            if path.exists():
                continue
            if path.suffix == ".wav":
                wav(path)
            else:
                write(path, b"{}\n" if path.suffix == ".json" else b"# synthetic payload\n")
        # Candidate NFC dependencies must match the existing field baseline.
        dependency = self.worker.EXPLICIT_TARGETS["config/requirements-nfc.txt"]
        write(self.source / "config/requirements-nfc.txt", self.installed(dependency).read_bytes())
        for unit in sorted(self.worker.FIXED_UNITS):
            relative = "systemd/" + unit
            content = (self.snapshot / relative).read_bytes()
            write(self.source / relative, content)
        # This candidate validator is deliberately minimal. The frozen worker's
        # WAV/path/count/byte checks remain real; product sound semantics are not
        # covered by this harness.
        write(self.source / "messagebox/sound_pack.py",
              b"def validate_sounds(root):\n    return None\n")
        write(self.source / "messagebox/example.py", b"VALUE = 'candidate'\n")
        for pack in ("cloud", "cues", "voice", "voices", "ringtones", "voices/pirate",
                     "voices/alien", "voices/dj", "voices/robot", "voices/french",
                     "voices/charlie", "voices/tata"):
            mkdir(self.source / "sounds" / pack)
        self.refresh()

    def load_bootstrap(self):
        # Absolute links stay device-relative records. The frozen worker's
        # _rooted() redirects the application pointer inside the temp root.
        for entry in self.bootstrap["entries"]:
            path = self.root / entry["path"].lstrip("/")
            if entry["kind"] == "directory":
                mkdir(path, int(entry["mode"], 8))
                path.chmod(int(entry["mode"], 8))
            elif entry["kind"] == "file":
                content = ((self.snapshot / entry["source"]).read_bytes()
                           if "source" in entry else entry["text"].encode())
                write(path, content, int(entry["mode"], 8))
            elif entry["kind"] == "symlink":
                mkdir(path.parent)
                path.symlink_to(entry["target"])
            else:
                raise AssertionError("unknown bootstrap fixture entry")

    def disk_usage(self, path):
        if path != self.installed(self.worker.RELEASES):
            raise AssertionError(f"unexpected disk boundary: {path}")
        self.disk_queries.append(path)
        return SimpleNamespace(total=8 * 1024**3, used=8 * 1024**3 - self.free_bytes,
                               free=self.free_bytes)

    def installed(self, absolute):
        return self.worker._rooted(self.root, absolute)

    def sleep(self, seconds):
        self.now += seconds

    def refresh(self):
        self.data = {"version": "v0.3.13", "commit": "a" * 40,
                     "files": [{"source": source, "installed": target,
                                "sha256": digest(self.source / source)}
                               for source, target in sorted(self.mapper.installed_paths(self.source).items())]}
        self.save_manifest()

    def save_manifest(self):
        write(self.manifest, json.dumps(self.data).encode())

    def add(self, relative, content=b"# candidate\n"):
        path = write(self.source / relative, content)
        self.refresh()
        return path

    def run(self, command, **options):
        """Small systemd model. Reject every unexpected external command."""
        self.commands.append(command[:])
        if command[:2] == ["systemctl", "is-active"]:
            value = self.states[command[2]]["active"]
        elif command[:2] == ["systemctl", "is-enabled"]:
            value = self.states[command[2]]["enabled"]
        elif command[:4] == ["systemctl", "show", "--property=ConsistsOf", "--value"]:
            value = ""
        elif command == ["systemctl", "daemon-reload"]:
            value = ""
        elif command[:2] == ["systemctl", "stop"]:
            for unit in command[2:]:
                self.states[unit]["active"] = "inactive"
            value = ""
        elif command[:3] == ["systemctl", "--job-mode=ignore-dependencies", "start"]:
            self.states[command[3]]["active"] = "active"
            value = ""
        elif command[:2] == ["systemctl", "reset-failed"]:
            value = ""
        elif command[:2] in (["systemctl", "enable"], ["systemctl", "disable"]):
            for unit in command[2:]:
                self.states[unit]["enabled"] = "enabled" if command[1] == "enable" else "disabled"
            value = ""
        else:
            raise AssertionError(f"unexpected host boundary: {command}")
        return subprocess.CompletedProcess(command, 0, value + "\n")

    def install(self, *, health=None):
        # Successful host health is an explicit substitute, never Pi acceptance.
        with service_environment():
            return self.worker.install_transaction(
                self.source, self.manifest, root=self.root, run=self.run,
                sequence=1, health=health or (lambda *args: None))

    def tree(self):
        """Installed bytes/modes/links, excluding worker locks and state files."""
        result = {}
        for top in ("etc", "opt", "usr"):
            for path in sorted((self.root / top).rglob("*")):
                relative = path.relative_to(self.root).as_posix()
                if path.is_symlink():
                    result[relative] = ("link", os.readlink(path))
                elif path.is_file():
                    result[relative] = ("file", path.stat().st_mode & 0o777, digest(path))
        return result

    def fixed_bytes(self):
        return {name: self.installed(f"/usr/lib/messagebox-update/{name}.py").read_bytes()
                for name in CONTROLLER}

    @contextlib.contextmanager
    def controller(self, *, signature_ok=True):
        """Run the frozen worker pipeline with all filesystem paths redirected.

        Only platform identity, minisign, installed identity, disk, systemd and health
        cross host boundaries. Verifier, extraction, trust lock and worker code
        remain frozen. This is not a signature or live OTA acceptance test.
        """
        verifier = load_frozen(self.version, "scripts/install/release_verify.py")
        with mock.patch.dict(sys.modules, {"bounded_update": self.worker, "release_verify": verifier}):
            trust = load_frozen(self.version, "scripts/install/trust_update.py")
            with mock.patch.dict(sys.modules, {"trust_update": trust}):
                controller = load_frozen(self.version, "scripts/install/update_controller.py")
        controller.STATE = self.installed(self.worker.UPDATE_STATE)
        controller.KEYS = self.installed("/etc/button-box-update/release-keys")
        controller.RUN = self.installed("/run/messagebox-update")
        controller.time = self.worker.time
        controller.read = lambda name, default=None: self.worker.read_state(self.root, name, default)
        controller.save = lambda name, value, mode=0o600: self.worker.save_state(self.root, name, value, mode)
        controller.local_platform = lambda: {"model": "Raspberry Pi 4 Model B", "os": "raspios-trixie", "architecture": "aarch64"}

        def identity():
            value = json.loads(self.installed("/opt/messagebox/release.json").read_bytes())
            return {"software_version": value["version"], "software_commit": value["commit"]}

        def signature(command, **options):
            if command[0] != "minisign":
                raise AssertionError(f"unexpected verifier boundary: {command}")
            self.commands.append(command[:])
            return subprocess.CompletedProcess(command, 0 if signature_ok else 1)

        controller.installed_identity = identity
        controller.verify_bundle = lambda *args, **kwargs: verifier.verify_bundle(*args, run=signature, **kwargs)
        transaction = self.worker.install_transaction

        def install(*args, **kwargs):
            return transaction(*args, root=self.root, run=self.run, health=lambda *args: None, **kwargs)

        with mock.patch.dict(sys.modules, {"bounded_update": self.worker, "release_verify": verifier,
                                           "trust_update": trust}), \
                mock.patch.object(self.worker, "install_transaction", side_effect=install), service_environment():
            yield controller

    def bundle(self, controller, *, mismatched_manifest=False):
        """Build a synthetic signed-envelope candidate for controller.worker()."""
        self.data.update(schema=1, product="button-box", tag=self.data["version"],
                         release_sequence=1, channel="stable", min_updater_version="1.1.0",
                         supported_os=["raspios-trixie"], supported_arch=["aarch64"],
                         supported_models=["Raspberry Pi 4 Model B"])
        self.save_manifest()
        bundle = controller.STATE / "downloads" / ("c" * 32)
        mkdir(bundle, 0o700)
        archive = bundle / f"button-box-{self.data['version']}.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for path in sorted(self.source.rglob("*")):
                if path.is_file():
                    output.add(path, arcname=path.relative_to(self.source).as_posix(), recursive=False)
        # The archive already contains its own copy for the mismatch test.
        external = dict(self.data)
        if mismatched_manifest:
            external["commit"] = "d" * 40
        write(bundle / "release-manifest.json", json.dumps(external).encode())
        sums = "".join(f"{digest(path)}  {path.name}\n" for path in (archive, bundle / "release-manifest.json"))
        write(bundle / "SHA256SUMS", sums.encode())
        write(bundle / "SHA256SUMS.minisig", b"synthetic host signature")
        key = base64.b64encode(b"Ed" + b"k" * 40).decode()
        write(controller.KEYS / "synthetic.pub", ("untrusted comment: synthetic test key\n" + key + "\n").encode())
        self.queue_bundle(controller, bundle, self.data["version"])
        return bundle

    def queue_bundle(self, controller, bundle, version):
        ticket = {"operation_id": "auto:" + digest(bundle / "release-manifest.json"),
                  "bundle": str(bundle), "version": version,
                  "manifest_sha256": digest(bundle / "release-manifest.json")}
        controller.save("ticket.json", ticket)
        controller.save("available.json", ticket)
        controller.save("active.json", ticket["operation_id"])
        controller.save(controller.operation_name(ticket["operation_id"]), {
            "operation_id": ticket["operation_id"], "action": "automatic", "state": "installing",
            "version": ticket["version"], "boot": "synthetic-boot", "expires_mono": 10000})

    def run_built_release(self, assets):
        """Install an unchanged build.py output through the frozen worker.

        Later bridge PRs must use this entry point with their actual release
        assets, then check bridge-specific installed state. No synthetic mapper,
        validator, module or manifest replaces the built archive. Only the outer
        minisign result and the documented host boundaries are substituted.
        This accepts unsigned build output; production signing is a separate gate.
        """
        assets = Path(assets)
        manifest = json.loads((assets / "release-manifest.json").read_bytes())
        version = manifest["version"]
        with self.controller() as controller:
            bundle = mkdir(controller.STATE / "downloads" / ("c" * 32), 0o700)
            names = (f"button-box-{version}.tar.gz", "release-manifest.json", "SHA256SUMS")
            for name in names:
                write(bundle / name, (assets / name).read_bytes())
            # build.py emits unsigned application assets. The frozen verifier
            # still checks every SHA256SUMS entry and the archive manifest.
            write(bundle / "SHA256SUMS.minisig", b"synthetic host signature")
            self.queue_bundle(controller, bundle, version)
            before = self.fixed_bytes()
            controller.worker()
            result = controller.read("result.json")
            journal = controller.read("journal.json")
            if (result["state"] != "installed" or journal["phase"] != "committed"
                    or self.worker.controlled_release(self.root) != journal["new"]
                    or self.fixed_bytes() != before or bundle.exists()):
                raise AssertionError("built release did not complete the frozen worker contract")
            return self.installed(journal["new"])
