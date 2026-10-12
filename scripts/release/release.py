#!/usr/bin/env python3
"""Offline signing, signature-first verification and attended draft uploads."""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from build import PRODUCT, ROOT, SCHEMA, SUPPORTED, ReleaseError, channel, digest, digest_stream, json_bytes

REPOSITORY = "button-box/button-box"
PUBLIC_PATTERNS = re.compile(
    rb"notion\.(?:so|site)|app\.notion\.com|collection://|view://|"
    rb"/Users/[^\s/]+/|/home/[^\s/]+/|/private/(?:tmp|var)/|"
    rb"-----BEGIN (?:[A-Z ]*PRIVATE KEY)|"
    rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})|"
    rb"(?:AKIA|ASIA)[A-Z0-9]{16}", re.IGNORECASE)


def regular_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ReleaseError("expected a regular file, not a symlink")
    return path


def public_key(path):
    path = regular_file(path).resolve()
    if path.parent != (ROOT / "config/release-keys").resolve() or path.suffix != ".pub":
        raise ReleaseError("select a pinned .pub file in config/release-keys")
    lines = path.read_text().splitlines()
    try:
        data = base64.b64decode(lines[1], validate=True)
    except (IndexError, ValueError) as exc:
        raise ReleaseError("invalid minisign public key") from exc
    if len(data) != 42 or data[:2] != b"Ed":
        raise ReleaseError("invalid minisign public key")
    return path


def checksums(directory):
    directory = Path(directory)
    data = regular_file(directory / "SHA256SUMS").read_bytes().decode("ascii")
    lines = data.splitlines(keepends=True)
    if len(lines) != 2 or any(not re.fullmatch(r"[0-9a-f]{64}  [A-Za-z0-9.-]+\n", line)
                              for line in lines):
        raise ReleaseError("SHA256SUMS must contain exactly two canonical SHA-256 entries")
    entries = {line[66:-1]: line[:64] for line in lines}
    archives = [name for name in entries if re.fullmatch(r"button-box-v0\.3\.[0-9]+(?:-canary\.[1-9][0-9]*)?\.tar\.gz", name)]
    if len(entries) != 2 or "release-manifest.json" not in entries or len(archives) != 1:
        raise ReleaseError("SHA256SUMS must name only the archive and release-manifest.json")
    if list(entries) != sorted(entries):
        raise ReleaseError("SHA256SUMS entries must be sorted")
    for name, expected in entries.items():
        if digest(regular_file(directory / name)) != expected:
            raise ReleaseError("release asset checksum mismatch")
    return archives[0]


def inspect_archive(directory, archive):
    manifest_data = (directory / "release-manifest.json").read_bytes()
    manifest = json.loads(manifest_data)
    tag = manifest.get("tag")
    if (manifest.get("schema") != SCHEMA or manifest.get("product") != PRODUCT
            or manifest.get("version") != tag or manifest.get("channel") != channel(tag)
            or archive != f"button-box-{tag}.tar.gz"
            or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("commit", ""))
            or type(manifest.get("release_sequence")) is not int
            or manifest["release_sequence"] < 1
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", manifest.get("min_updater_version", ""))
            or any(manifest.get(key) != value for key, value in SUPPORTED.items())):
        raise ReleaseError("invalid release manifest identity or compatibility")
    with tarfile.open(directory / archive, "r:gz") as tar:
        members = tar.getmembers()
        names = [member.name for member in members]
        if names != sorted(set(names)):
            raise ReleaseError("archive names must be sorted and unique")
        for member in members:
            pure = PurePosixPath(member.name)
            if (not member.isfile() or pure.is_absolute() or ".." in pure.parts
                    or str(pure) != member.name or member.uid != 0 or member.gid != 0
                    or member.uname or member.gname or member.mode not in {0o644, 0o755}):
                raise ReleaseError("archive contains an unsafe entry or noncanonical metadata")
        if len({member.mtime for member in members}) != 1:
            raise ReleaseError("archive timestamps differ")
        for name, expected in {
            "release-manifest.json": manifest_data,
            "VERSION": (tag + "\n").encode(),
            "release.json": json_bytes({"version": tag, "commit": manifest["commit"],
                                        "manifest_sha256": digest(directory / "release-manifest.json")}),
        }.items():
            if name not in names or tar.extractfile(name).read() != expected:
                raise ReleaseError("archive identity differs from external manifest")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ReleaseError("manifest lacks installed-file hashes")
        sources, installed = set(), set()
        for item in files:
            source, target = item["source"], item["installed"]
            if source in sources or target in installed or source not in names:
                raise ReleaseError("manifest contains duplicate or missing installed files")
            sources.add(source)
            installed.add(target)
            if digest_stream(tar.extractfile(source)) != item["sha256"]:
                raise ReleaseError("archive installed-file hash mismatch")
    return manifest, members


def verify(directory, key):
    directory = Path(directory)
    key = public_key(key)
    # Authenticate the exact metadata/checksum bytes before trusting filenames or extracting.
    subprocess.run(["minisign", "-V", "-H", "-m", str(regular_file(directory / "SHA256SUMS")),
                    "-x", str(regular_file(directory / "SHA256SUMS.minisig")), "-p", str(key)], check=True)
    archive = checksums(directory)
    manifest, members = inspect_archive(directory, archive)
    return archive, manifest, members


def signing_secret(directory, secret):
    if os.environ.get("CI"):
        raise ReleaseError("sign only on the operator's offline computer, never in CI")
    secret = regular_file(secret).resolve()
    if secret.is_relative_to(ROOT) or secret.is_relative_to(Path(directory).resolve()):
        raise ReleaseError("signing key must remain outside the repository and release artifacts")
    # Require the minisign password-encrypted format; never print key bytes.
    try:
        data = base64.b64decode(secret.read_bytes().splitlines()[1], validate=True)
    except (IndexError, ValueError) as exc:
        raise ReleaseError("invalid encrypted minisign secret key") from exc
    if len(data) != 158 or data[:6] != b"EdScB2":
        raise ReleaseError("require a password-encrypted minisign secret key")
    return secret


def sign(directory, secret, key):
    secret = signing_secret(directory, secret)
    key = public_key(key)
    directory = Path(directory).resolve()
    archive = checksums(directory)
    manifest, _ = inspect_archive(directory, archive)
    signature = directory / "SHA256SUMS.minisig"
    if signature.exists():
        raise ReleaseError("signature already exists; refusing to overwrite it")
    subprocess.run(["minisign", "-S", "-m", str(directory / "SHA256SUMS"), "-s", str(secret),
                    "-x", str(signature), "-c", "Button Box release signature",
                    "-t", f"{PRODUCT} {manifest['tag']} sequence {manifest['release_sequence']}"], check=True)
    verify(directory, key)
    print("Signature and both asset hashes verified. Transfer only the four release assets.")


def scan_public(data):
    if PUBLIC_PATTERNS.search(data):
        raise ReleaseError("public-output pattern scan found a possible private value; review privately")


def review(directory, archive, manifest, members, notes):
    for name in ("release-manifest.json", "SHA256SUMS", "SHA256SUMS.minisig"):
        scan_public((directory / name).read_bytes())
    scan_public(notes.read_bytes())
    with tarfile.open(directory / archive, "r:gz") as tar:
        for member in members:
            scan_public(member.name.encode())
            # WAVs require listening/metadata review by the human at the gate.
            if not member.name.endswith(".wav"):
                scan_public(tar.extractfile(member).read())
    print(f"Destination: https://github.com/{REPOSITORY} (PUBLIC, draft upload)")
    print(f"Title: Button Box {manifest['tag']}")
    print(f"Commit: {manifest['commit']}; sequence: {manifest['release_sequence']}")
    print("Archive listing (mode, bytes, path):")
    for member in members:
        print(f"{member.mode:04o} {member.size:9d} {member.name}")
    print("Release notes:\n" + notes.read_text())
    print("Review every outgoing file and its meaning, binary/audio metadata and content, tag/commit,")
    print("GitHub's automatic source archives, and any outgoing logs. Check English notes for changes,")
    print("compatibility, known limitations, release and diff links. Pattern scans are not semantic review.")
    print("Read the applicable public-output policy; stop on uncertainty. A draft is externally shared.")
    if not sys.stdin.isatty():
        raise ReleaseError("manual review requires an interactive terminal")
    if input(f"Type CREATE DRAFT {manifest['tag']} after completing review: ") != f"CREATE DRAFT {manifest['tag']}":
        raise ReleaseError("manual review declined; no upload performed")


def publish(directory, key, notes, receipt):
    directory, notes, receipt = Path(directory).resolve(), regular_file(notes), Path(receipt).resolve()
    key = public_key(key)
    if receipt.exists() or receipt.is_relative_to(ROOT) or receipt.is_relative_to(directory):
        raise ReleaseError("use a new private receipt path outside repository and release artifacts")
    # Snapshot first so bytes cannot change between human review and upload.
    with tempfile.TemporaryDirectory(prefix="button-box-draft-") as temporary:
        snapshot = Path(temporary)
        archive, _, _ = verify(directory, key)
        assets = [archive, "release-manifest.json", "SHA256SUMS", "SHA256SUMS.minisig"]
        for name in assets:
            shutil.copyfile(regular_file(directory / name), snapshot / name)
        saved_notes = snapshot / "notes.md"
        shutil.copyfile(notes, saved_notes)
        archive, manifest, members = verify(snapshot, key)
        audience = json.loads(subprocess.check_output(
            ["gh", "api", f"repos/{REPOSITORY}"], text=True))
        if audience.get("full_name") != REPOSITORY or audience.get("visibility") != "public":
            raise ReleaseError("repository identity or public audience did not match")
        tag = manifest["tag"]
        refs = subprocess.check_output(["git", "ls-remote", "https://github.com/" + REPOSITORY + ".git",
                                        "refs/tags/" + tag, "refs/tags/" + tag + "^{}"], text=True)
        remote = dict(line.split()[::-1] for line in refs.splitlines())
        commit = remote.get("refs/tags/" + tag + "^{}", remote.get("refs/tags/" + tag))
        if commit != manifest["commit"]:
            raise ReleaseError("existing GitHub tag does not match the signed commit")
        review(snapshot, archive, manifest, members, saved_notes)
        record = {"destination": f"https://github.com/{REPOSITORY}", "audience": "public",
                  "tag": tag, "commit": commit, "result": "reviewed; draft upload pending",
                  "checks": ["minisign -V -H", "both SHA-256 hashes", "archive and installed hashes",
                             "public-output pattern scan", "human semantic/audio/source/log review"],
                  "assets": {name: digest(snapshot / name) for name in assets},
                  "notes_sha256": digest(saved_notes)}
        # Write before side effects; failure still leaves a private reconciliation receipt.
        with os.fdopen(os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(json_bytes(record))
        command = ["gh", "release", "create", tag, "--repo", REPOSITORY, "--verify-tag", "--draft",
                   "--title", "Button Box " + tag, "--notes-file", str(saved_notes)]
        if manifest["channel"] == "canary":
            command.append("--prerelease")
        subprocess.run(command + [str(snapshot / name) for name in assets], check=True)
        result = json.loads(subprocess.check_output(["gh", "release", "view", tag, "--repo", REPOSITORY,
            "--json", "isDraft,isPrerelease,assets,url"], text=True))
        if (result.get("isDraft") is not True
                or result.get("isPrerelease") != (manifest["channel"] == "canary")
                or {item["name"] for item in result.get("assets", [])} != set(assets)):
            raise ReleaseError("draft read-back did not match; reconcile manually before retrying")
        record.update(result="draft created and read back; never auto-published", url=result["url"])
        receipt.write_bytes(json_bytes(record))
        print("Draft created and read back. Review and publish separately in GitHub.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("sign", "verify", "publish"):
        command = commands.add_parser(name)
        command.add_argument("directory", type=Path)
        command.add_argument("--public-key", type=Path, required=True)
        if name == "sign":
            command.add_argument("--secret-key", type=Path, required=True)
        if name == "publish":
            command.add_argument("--notes", type=Path, required=True)
            command.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "sign":
            sign(args.directory, args.secret_key, args.public_key)
        elif args.command == "verify":
            verify(args.directory, args.public_key)
            print("Signature, checksums, manifest and archive identity verified.")
        else:
            publish(args.directory, args.public_key, args.notes, args.receipt)
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError, TypeError, tarfile.TarError, EOFError) as exc:
        parser.exit(1, f"Release {args.command} failed: {exc}\n")


if __name__ == "__main__":
    main()
