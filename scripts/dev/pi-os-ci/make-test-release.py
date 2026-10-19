#!/usr/bin/env python3
"""CI only: build the four release assets from a checkout and sign them with a
throwaway minisign key, so the one-command installer can install this exact
code. The archive, manifest and SHA256SUMS come from scripts/release/build.py;
only the tag check and the offline signing ceremony are skipped.

Usage: make-test-release.py CHECKOUT OUTPUT_DIR
Writes OUTPUT_DIR/{button-box-vX.Y.Z.tar.gz, release-manifest.json, SHA256SUMS,
SHA256SUMS.minisig, test-release.pub}. The secret key is deleted afterwards.
"""
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    source, output = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(source / "scripts/release"))
    sys.path.insert(0, str(source / "scripts/install"))
    import build
    import release_verify

    tag = "v" + (source / "VERSION").read_text().strip()
    commit = build.git(source, "rev-parse", "HEAD")
    payload = build.payload_paths(source)
    minimum = ".".join(map(str, release_verify.UPDATER_VERSION))
    manifest = build.release_manifest(source, tag, commit, 1, minimum, payload)
    timestamp = int(build.git(source, "show", "-s", "--format=%ct", "HEAD"))
    build.package(source, output, manifest, payload, timestamp)
    with tempfile.TemporaryDirectory(prefix="ci-release-key-") as keys:
        secret = Path(keys) / "test-release.key"
        subprocess.run(["minisign", "-G", "-W", "-p", str(output / "test-release.pub"), "-s", str(secret)],
                       check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["minisign", "-S", "-s", str(secret), "-m", str(output / "SHA256SUMS"),
                        "-x", str(output / "SHA256SUMS.minisig"), "-t", f"CI test release {tag}"],
                       check=True, stdout=subprocess.DEVNULL)
    print(f"Signed test release {tag} ({commit[:12]}) in {output}")


if __name__ == "__main__":
    main()
