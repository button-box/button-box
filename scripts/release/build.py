#!/usr/bin/env python3
"""Build reproducible release artifacts from an existing, stationary tag only."""
import argparse
import hashlib
import io
import json
import os
import pprint
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/install'))
from release_verify import (CONTROLLER_DESCRIPTOR, CONTROLLER_MODULES, CONTROLLER_SIGNATURE,
                            MAX_CONTROLLER_MODULE, check_controller_compatibility,
                            controller_file, parse_controller_descriptor)
sys.path.pop(0)
ROOT = Path(__file__).resolve().parents[2]
TAG_PATTERN = r"v0\.3\.(0|[1-9][0-9]*)(-canary\.[1-9][0-9]*)?"
PRODUCT = "button-box"
SCHEMA = 1
SUPPORTED = {
    "supported_os": ["raspios-trixie"],
    "supported_arch": ["aarch64"],
    "supported_models": ["Raspberry Pi 3 Model A Plus", "Raspberry Pi 4 Model B"],
}
CONTROLLER_VERSION = '1.3.0'


class ReleaseError(ValueError):
    pass


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def channel(tag):
    if not re.fullmatch(TAG_PATTERN, tag):
        raise ReleaseError("expected v0.3.x or v0.3.x-canary.N tag")
    return "canary" if "-canary." in tag else "stable"


def require_clean(repo):
    if git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ReleaseError("refusing a dirty checkout (including untracked files)")


def resolve_tag(repo, tag):
    channel(tag)
    ref = "refs/tags/" + tag
    return (git(repo, "rev-parse", "--verify", ref),
            git(repo, "rev-parse", "--verify", ref + "^{commit}"))


def require_tag(repo, tag, expected):
    if resolve_tag(repo, tag) != expected:
        raise ReleaseError("tag moved during release build")


def digest_stream(stream):
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(chunk)
    return result.hexdigest()


def digest(path):
    with Path(path).open("rb") as stream:
        return digest_stream(stream)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def controller_descriptor(root, sequence, minimum):
    modules = []
    for name in CONTROLLER_MODULES:
        raw, _mode = controller_file(root, 'scripts/install/' + name, MAX_CONTROLLER_MODULE)
        modules.append({'path': 'updater/' + name, 'size': len(raw), 'mode': 0o644,
                        'sha256': hashlib.sha256(raw).hexdigest()})
    value = {'schema': 1, 'product': 'button-box-controller',
             'controller_version': CONTROLLER_VERSION, 'controller_sequence': sequence,
             'min_application_version': minimum, 'selector_protocol': 1, 'state_format': 1,
             'capabilities': {'signed_descriptor': True, 'self_upgrade': False},
             'modules': modules, 'unit_changes': [], **SUPPORTED}
    raw = (pprint.pformat(value, sort_dicts=True, width=100) + '\n').encode('ascii')
    parse_controller_descriptor(raw)
    return raw


def controller_signature(raw, secret, key):
    # Import at call time: the offline signer reuses the existing key policy;
    # release.py also imports build's reproducible packaging primitives.
    from release import sign_controller
    return sign_controller(raw, secret, key)


def inspect_package(directory, key):
    from release import checksums, inspect_archive, public_key
    inspect_archive(directory, checksums(directory), controller_keys=public_key(key).parent)


def payload_paths(root):
    entries = [line for line in (root / "scripts/release/payload.txt").read_text().splitlines()
               if line and not line.startswith("#")]
    if not entries or entries != sorted(set(entries)):
        raise ReleaseError("payload allowlist must be nonempty, sorted and unique")
    tracked = {}
    for record in git(root, "ls-tree", "-r", "HEAD").splitlines():
        metadata, name = record.split("\t", 1)
        tracked[name] = metadata.split()[0]
    for name in entries:
        pure = PurePosixPath(name)
        if (pure.is_absolute() or ".." in pure.parts or str(pure) != name
                or tracked.get(name) not in {"100644", "100755"}):
            raise ReleaseError("payload must contain only explicitly named tracked regular files")
        source = root / name
        if source.is_symlink() or not source.is_file() or source.resolve() != source:
            raise ReleaseError("payload path is not a regular file inside the checkout")
    return {name: 0o755 if tracked[name] == "100755" else 0o644 for name in entries}


def release_manifest(root, tag, commit, sequence, minimum, payload):
    # Generate while Git metadata still exists; preserve the installed-file mapping.
    result = json.loads(subprocess.check_output(
        [sys.executable, str(root / "scripts/dev/release-manifest.py")], cwd=root))
    if result["commit"] != commit:
        raise ReleaseError("manifest commit differs from the resolved tag")
    if any(item["source"] not in payload for item in result["files"]):
        raise ReleaseError("installed file missing from the explicit payload allowlist")
    if len({item["source"] for item in result["files"]}) != len(result["files"]):
        raise ReleaseError("duplicate installed source in manifest")
    for item in result["files"]:
        if item["sha256"] != digest(root / item["source"]):
            raise ReleaseError("installed-file hash mismatch")
    result.update(schema=SCHEMA, product=PRODUCT, tag=tag, version=tag, commit=commit,
                  release_sequence=sequence, channel=channel(tag),
                  min_updater_version=minimum, **SUPPORTED)
    return result


def package(root, output, manifest, payload, timestamp):
    manifest_data = json_bytes(manifest)
    generated = {
        "VERSION": (manifest["tag"] + "\n").encode(),
        "release-manifest.json": manifest_data,
        "release.json": json_bytes({"version": manifest["tag"], "commit": manifest["commit"],
                                   "manifest_sha256": hashlib.sha256(manifest_data).hexdigest()}),
    }
    if set(generated).intersection(payload):
        raise ReleaseError("generated identity files must not be in the source allowlist")
    output.mkdir()
    (output / "release-manifest.json").write_bytes(manifest_data)
    archive = output / f"button-box-{manifest['tag']}.tar.gz"
    raw_tar = output / "payload.tar"
    with tarfile.open(raw_tar, "w", format=tarfile.USTAR_FORMAT) as tar:
        for name in sorted(set(payload) | set(generated)):
            info = tarfile.TarInfo(name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = timestamp
            info.mode = payload.get(name, 0o644)
            if name in generated:
                data = generated[name]
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                info.size = (root / name).stat().st_size
                with (root / name).open("rb") as stream:
                    tar.addfile(info, stream)
    # -n removes filename and wall-clock time; no Git metadata is archived.
    with archive.open("wb") as stream:
        subprocess.run(["gzip", "-n", "-9", "-c", str(raw_tar)], stdout=stream, check=True)
    raw_tar.unlink()
    names = sorted([archive.name, "release-manifest.json"])
    (output / "SHA256SUMS").write_text(
        "".join(f"{digest(output / name)}  {name}\n" for name in names), encoding="ascii")


def run_checks(root):
    for transport in ("cloud", "wacli"):
        subprocess.run(["make", "check"], cwd=root,
                       env={**os.environ, "MSGBOX_TEST_TRANSPORT": transport}, check=True)


def build(repo, tag, output, sequence, minimum, *, controller_sequence=None,
          min_application_version=None, controller_secret=None, controller_key=None):
    if type(sequence) is not int or sequence < 1:
        raise ReleaseError("release_sequence must be a positive integer")
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", minimum):
        raise ReleaseError("minimum updater version must be major.minor.patch")
    repo, output = Path(repo).resolve(), Path(output).resolve()
    require_clean(repo)
    expected = resolve_tag(repo, tag)
    if output.exists():
        raise ReleaseError("output already exists; use a new directory")
    if output.is_relative_to(repo):
        raise ReleaseError("output must be outside the source checkout")
    if (controller_secret is None or controller_key is None
            or type(controller_sequence) is not int or controller_sequence < 1
            or min_application_version is None):
        raise ReleaseError('controller signing key, public pin, sequence and minimum application are required')
    if Path(controller_secret).resolve().is_relative_to(repo):
        raise ReleaseError('controller signing key must remain outside the source checkout')
    proof_raw = proof_signature = None
    with tempfile.TemporaryDirectory(prefix="button-box-release-") as directory:
        temporary = Path(directory).resolve()
        for index in (1, 2):
            checkout = temporary / f"checkout-{index}"
            subprocess.run(["git", "clone", "--quiet", "--no-local", "--no-checkout",
                            str(repo), str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "checkout", "--quiet", "--detach",
                            expected[1]], check=True)
            require_tag(checkout, tag, expected)
            require_clean(checkout)
            run_checks(checkout)
            require_clean(checkout)
            require_tag(repo, tag, expected)
            payload = payload_paths(checkout)
            if any('scripts/install/' + name not in payload for name in CONTROLLER_MODULES):
                raise ReleaseError('controller module missing from the explicit payload allowlist')
            raw = controller_descriptor(checkout, controller_sequence, min_application_version)
            check_controller_compatibility(parse_controller_descriptor(raw), tag, {
                'os': 'raspios-trixie', 'architecture': 'aarch64', 'model': 'Raspberry Pi 4 Model B'})
            if proof_raw is None:
                proof_raw = raw
                proof_signature = controller_signature(raw, controller_secret, controller_key)
            elif raw != proof_raw:
                raise ReleaseError('controller payloads differ between builds')
            # The legacy mapper retains both .py data files as installed files.
            # Generate only after the clean-check and tracked payload check.
            for name, data in ((CONTROLLER_DESCRIPTOR, raw), (CONTROLLER_SIGNATURE, proof_signature)):
                if name in payload or (checkout / name).exists():
                    raise ReleaseError('controller proof paths are reserved generated files')
                (checkout / name).write_bytes(data)
                (checkout / name).chmod(0o644)
                payload[name] = 0o644
            manifest = release_manifest(checkout, tag, expected[1], sequence, minimum, payload)
            installed = {item['source']: item['installed'] for item in manifest['files']}
            if any(installed.get(name) != '/opt/messagebox/' + name
                   for name in (CONTROLLER_DESCRIPTOR, CONTROLLER_SIGNATURE)):
                raise ReleaseError('controller proof missing from the installed manifest')
            timestamp = int(git(checkout, "show", "-s", "--format=%ct", "HEAD"))
            package(checkout, temporary / f"build-{index}", manifest, payload, timestamp)
            inspect_package(temporary / f'build-{index}', controller_key)
        first, second = temporary / "build-1", temporary / "build-2"
        for asset in first.iterdir():
            if digest(asset) != digest(second / asset.name):
                raise ReleaseError("two builds differ; refusing release artifacts")
        require_clean(repo)
        require_tag(repo, tag, expected)
        shutil.copytree(first, output)
    print(f"Two builds matched: {tag} {expected[1]} (sequence {sequence})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-sequence", type=int, required=True)
    parser.add_argument("--min-updater-version", required=True)
    parser.add_argument('--controller-sequence', type=int, required=True)
    parser.add_argument('--min-application-version', required=True)
    parser.add_argument('--controller-secret-key', type=Path, required=True)
    parser.add_argument('--controller-public-key', type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.repo, args.tag, args.output, args.release_sequence, args.min_updater_version,
              controller_sequence=args.controller_sequence, min_application_version=args.min_application_version,
              controller_secret=args.controller_secret_key, controller_key=args.controller_public_key)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Release build failed: {exc}\n")


if __name__ == "__main__":
    main()
