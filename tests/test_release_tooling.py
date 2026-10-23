import base64
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_build", ROOT / "scripts/release/build.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)
SPEC = importlib.util.spec_from_file_location("release_tools", ROOT / "scripts/release/release.py")
release = importlib.util.module_from_spec(SPEC)
with mock.patch.dict(sys.modules, {"build": build}):
    SPEC.loader.exec_module(release)

COMMIT = "a" * 40
TAG = "v0.3.0-canary.1"


class ReleaseToolingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "scripts/release").mkdir(parents=True)
        (self.repo / "scripts/dev").mkdir()
        (self.repo / "messagebox").mkdir()
        (self.repo / "messagebox/example.py").write_text("print('public example')\n")
        # A synthetic tag checkout without creating commits/tags in any repository.
        (self.repo / "scripts/dev/release-manifest.py").write_text(
            "import hashlib, json\nfrom pathlib import Path\n"
            "print(json.dumps({'version': 'development', 'commit': '" + COMMIT + "', "
            "'files': [{'source': str(p), 'installed': '/opt/messagebox/' + str(p), "
            "'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} "
            "for p in sorted(Path('messagebox').glob('*.py'))]}))\n"
        )
        (self.repo / 'scripts/install').mkdir()
        self.tracked = ['messagebox/example.py', 'scripts/dev/release-manifest.py']
        for name in build.CONTROLLER_MODULES:
            relative = 'scripts/install/' + name
            (self.repo / relative).write_text('# synthetic controller\n')
            self.tracked.append(relative)
        (self.repo / "scripts/release/payload.txt").write_text('\n'.join(sorted(self.tracked)) + '\n')
        (self.root / 'pin.pub').write_text('untrusted comment: synthetic public key\n' +
                                         base64.b64encode(b'Ed' + b'k' * 40).decode() + '\n')
        (self.repo / "VERSION").write_text("development\n")
        (self.repo / ".env").write_text("private ignored file\n")
        self.dirty = False
        self.moved = False
        self.commands = []
        self.real_run = subprocess.run

    def git(self, repo, *args):
        if args[0] == "status":
            return " M VERSION" if self.dirty else ""
        if args[0] == "rev-parse":
            return "b" * 40 if self.moved else COMMIT
        if args[0] == "ls-tree":
            return '\n'.join(('100755' if name == 'scripts/dev/release-manifest.py' else '100644') +
                             ' blob ' + COMMIT + '\t' + name for name in self.tracked)
        if args[0] == "show":
            return "1700000000"
        raise AssertionError(args)

    def command_run(self, command, *args, **kwargs):
        self.commands.append(command)
        if command[:2] == ["git", "clone"]:
            shutil.copytree(self.repo, command[-1])
            return subprocess.CompletedProcess(command, 0)
        if command[0] == "git":
            self.assertEqual(command[-2:], ["--detach", COMMIT])
            return subprocess.CompletedProcess(command, 0)
        if command[0] == "make":
            return subprocess.CompletedProcess(command, 0)
        return self.real_run(command, *args, **kwargs)

    def make_build(self, output=None, *, boot_hotfix_base=None):
        output = output or self.root / "output"
        with mock.patch.object(build, "git", side_effect=self.git), \
                mock.patch.object(build, 'controller_signature', return_value=b"'synthetic signature'\n"), \
                mock.patch.object(build, 'inspect_package', side_effect=lambda path, key: release.inspect_archive(path, release.checksums(path))), \
                mock.patch.object(build.subprocess, "run", side_effect=self.command_run):
            build.build(self.repo, TAG, output, 7, "1.1.0" if boot_hotfix_base else "1.0.0", controller_sequence=1,
                        min_application_version=TAG, controller_secret=self.root / 'offline.key',
                        controller_key=self.root / 'pin.pub', boot_hotfix_base=boot_hotfix_base)
        return output

    def test_boot_hotfix_profile_uses_normal_repeat_build_checks_and_packaging(self):
        profile = 'scripts/dev/build_boot_hotfix.py'
        (self.repo / profile).write_text('''import hashlib,json,shutil
from pathlib import Path
def prepare(destination, *, base, repo):
    assert base == 'v0.3.12'
    shutil.copytree(repo,destination)
    example=destination/'messagebox/example.py'
    example.write_text(example.read_text()+'# legacy profile\\n')
    return {name:0o644 for name in (destination/'scripts/release/payload.txt').read_text().splitlines()}
def manifest(source, *, tag, commit, sequence, supported):
    return dict(schema=1,product='button-box',version=tag,tag=tag,commit=commit,
                release_sequence=sequence,channel='canary',min_updater_version='1.1.0',
                boot_hotfix_base='v0.3.12',**supported,
                files=[dict(source=str(p.relative_to(source)),installed='/opt/messagebox/'+str(p.relative_to(source)),
                            sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                       for p in sorted((source/'messagebox').glob('*.py'))])
''')
        self.tracked.append(profile)
        (self.repo / 'scripts/release/payload.txt').write_text('\n'.join(sorted(self.tracked)) + '\n')
        assets = self.make_build(boot_hotfix_base='v0.3.12')
        manifest = json.loads((assets / 'release-manifest.json').read_bytes())
        self.assertEqual(manifest['boot_hotfix_base'], 'v0.3.12')
        with tarfile.open(assets / ('button-box-' + TAG + '.tar.gz')) as archive:
            self.assertTrue(archive.extractfile('messagebox/example.py').read().endswith(b'# legacy profile\n'))
        self.assertEqual(len([command for command in self.commands if command[0] == 'make']), 4)
        self.assertNotIn('legacy profile', (self.repo / 'messagebox/example.py').read_text())

    def test_manifest_deterministic_archive_checksums_and_allowlist(self):
        first = self.make_build(self.root / "first")
        second = self.make_build(self.root / "second")
        self.assertEqual(sorted(p.name for p in first.iterdir()),
                         ["SHA256SUMS", f"button-box-{TAG}.tar.gz", "release-manifest.json"])
        for asset in first.iterdir():
            self.assertEqual(asset.read_bytes(), (second / asset.name).read_bytes())
        manifest = json.loads((first / "release-manifest.json").read_text())
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["product"], "button-box")
        self.assertEqual(manifest["tag"], TAG)
        self.assertEqual(manifest["version"], TAG)
        self.assertEqual(manifest["commit"], COMMIT)
        self.assertEqual(manifest["release_sequence"], 7)
        self.assertEqual(manifest["channel"], "canary")
        self.assertEqual(manifest["supported_os"], ["raspios-trixie"])
        self.assertEqual(manifest["supported_arch"], ["aarch64"])
        self.assertEqual(manifest["supported_models"],
                         ["Raspberry Pi 3 Model A Plus", "Raspberry Pi 4 Model B"])
        self.assertEqual(manifest["min_updater_version"], "1.0.0")
        self.assertEqual(next(item['sha256'] for item in manifest['files'] if item['source'] == 'messagebox/example.py'),
                         build.digest(self.repo / "messagebox/example.py"))
        archive = release.checksums(first)
        release.inspect_archive(first, archive)
        with tarfile.open(first / archive) as tar:
            members = tar.getmembers()
            self.assertEqual([m.name for m in members], sorted(m.name for m in members))
            self.assertNotIn(".env", tar.getnames())
            for member in members:
                self.assertEqual((member.uid, member.gid, member.uname, member.gname, member.mtime),
                                 (0, 0, "", "", 1700000000))
                expected_mode = 0o755 if member.name == "scripts/dev/release-manifest.py" else 0o644
                self.assertEqual(member.mode, expected_mode)
            self.assertEqual(tar.extractfile("VERSION").read(), (TAG + "\n").encode())
            self.assertEqual(tar.extractfile("release-manifest.json").read(),
                             (first / "release-manifest.json").read_bytes())
        header = (first / archive).read_bytes()[:10]
        self.assertEqual(header[3], 0)  # no optional filename/comment fields
        self.assertEqual(header[4:8], b"\0" * 4)
        self.assertEqual((self.repo / "VERSION").read_text(), "development\n")
        checks = [c for c in self.commands if c[0] == "make"]
        self.assertEqual(len(checks), 8)  # two checkouts, two transports, twice

    def test_dirty_checkout_refused_before_clone_or_output(self):
        self.dirty = True
        with self.assertRaisesRegex(build.ReleaseError, "dirty"):
            self.make_build()
        self.assertFalse(self.commands)
        self.assertFalse((self.root / "output").exists())

    def test_moving_tag_refused_before_output(self):
        def checks(_):
            self.moved = True
        with mock.patch.object(build, "run_checks", side_effect=checks):
            with self.assertRaisesRegex(build.ReleaseError, "moved"):
                self.make_build()
        self.assertFalse((self.root / "output").exists())

    def test_checks_mutating_checkout_are_refused(self):
        with mock.patch.object(build, "run_checks", side_effect=lambda _: setattr(self, "dirty", True)):
            with self.assertRaisesRegex(build.ReleaseError, "dirty"):
                self.make_build()
        self.assertFalse((self.root / "output").exists())

    def test_non_tag_and_bad_release_metadata_refused(self):
        for tag in ("main", COMMIT, "v0.2.0-rc.1", "v0.3.01", "v0.3.0-canary.0", "--help"):
            with self.subTest(tag=tag), self.assertRaises(build.ReleaseError):
                build.channel(tag)
        self.assertEqual(build.channel("v0.3.2"), "stable")
        for sequence, minimum in ((0, "1.0.0"), (-1, "1.0.0"), (True, "1.0.0"), (1, "latest")):
            with self.assertRaises(build.ReleaseError):
                build.build(self.repo, TAG, self.root / "output", sequence, minimum)

    def test_payload_refuses_symlink_untracked_file_and_missing_installed_file(self):
        with mock.patch.object(build, "git", side_effect=self.git):
            path = self.repo / "messagebox/example.py"
            path.unlink()
            path.symlink_to(self.repo / "VERSION")
            with self.assertRaisesRegex(build.ReleaseError, "regular"):
                build.payload_paths(self.repo)
            path.unlink()
            path.write_text("print('public example')\n")
            listing = self.repo / "scripts/release/payload.txt"
            listing.write_text(".env\n")
            with self.assertRaisesRegex(build.ReleaseError, "tracked"):
                build.payload_paths(self.repo)
            listing.write_text("scripts/dev/release-manifest.py\n")
            with self.assertRaisesRegex(build.ReleaseError, "missing"):
                build.release_manifest(self.repo, TAG, COMMIT, 7, "1.0.0", build.payload_paths(self.repo))

    def test_two_different_builds_refused(self):
        original = build.package
        def different(root, output, manifest, payload, timestamp):
            original(root, output, manifest, payload, timestamp + (output.name == 'build-2'))
        with mock.patch.object(build, "package", side_effect=different):
            with self.assertRaisesRegex(build.ReleaseError, "differ"):
                self.make_build()
        self.assertFalse((self.root / "output").exists())

    def test_sum_format_and_tampering_are_refused(self):
        output = self.make_build()
        sums = output / "SHA256SUMS"
        original = sums.read_text()
        for invalid in (original.replace("  ", " "), original + original.splitlines()[0] + "\n",
                        original.replace("release-manifest.json", "../release-manifest.json"),
                        original.replace("\n", "\r\n"), original.rstrip("\n")):
            with self.subTest(invalid=invalid[:10]):
                sums.write_bytes(invalid.encode())
                with self.assertRaises(build.ReleaseError):
                    release.checksums(output)
        sums.write_text(original)
        (output / "release-manifest.json").write_text("tampered\n")
        with self.assertRaisesRegex(build.ReleaseError, "checksum"):
            release.checksums(output)

    def test_signature_is_verified_before_checksums(self):
        output = self.make_build()
        (output / "SHA256SUMS.minisig").write_text("mock signature\n")
        with mock.patch.object(release, "public_key", return_value=self.root / "pin.pub"), \
                mock.patch.object(release.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "minisign")), \
                mock.patch.object(release, "checksums") as sums:
            with self.assertRaises(subprocess.CalledProcessError):
                release.verify(output, self.root / "pin.pub")
            sums.assert_not_called()

    def test_review_requires_attended_approval(self):
        output = self.make_build()
        (output / "SHA256SUMS.minisig").write_text("mock signature\n")
        notes = self.root / "notes.md"
        notes.write_text("Public release notes in English.\n")
        archive = release.checksums(output)
        manifest, members = release.inspect_archive(output, archive)
        with mock.patch.object(release.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="no"):
            with self.assertRaisesRegex(build.ReleaseError, "declined"):
                release.review(output, archive, manifest, members, notes)
        with mock.patch.object(release.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(build.ReleaseError, "interactive"):
                release.review(output, archive, manifest, members, notes)

    def test_publish_only_draft_four_assets_prerelease_and_private_receipt(self):
        output = self.make_build()
        (output / "SHA256SUMS.minisig").write_text("mock signature\n")
        notes = self.root / "notes.md"
        notes.write_text("Public release notes in English.\n")
        receipt = self.root / "receipt.json"
        assets = sorted(p.name for p in output.iterdir())
        def capture(command, **kwargs):
            if command[:2] == ["gh", "api"]:
                return json.dumps({"full_name": release.REPOSITORY, "visibility": "public"})
            if command[:2] == ["git", "ls-remote"]:
                return COMMIT + "\trefs/tags/" + TAG + "\n"
            if command[:3] == ["gh", "release", "view"]:
                return json.dumps({"isDraft": True, "isPrerelease": True,
                                   "assets": [{"name": n} for n in assets], "url": "https://github.com/example"})
            raise AssertionError(command)
        with mock.patch.object(release, "public_key", return_value=self.root / "pin.pub"), \
                mock.patch.object(release.subprocess, "check_output", side_effect=capture), \
                mock.patch.object(release.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                mock.patch.object(release.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="CREATE DRAFT " + TAG):
            release.publish(output, self.root / "pin.pub", notes, receipt)
        self.assertEqual(run.call_count, 5)  # app/controller proof in original and snapshot, then draft creation
        verify_command, _, _, _, create_command = [call.args[0] for call in run.call_args_list]
        self.assertEqual(verify_command[:3], ["minisign", "-V", "-H"])
        self.assertEqual(create_command[:4], ["gh", "release", "create", TAG])
        self.assertIn("--draft", create_command)
        self.assertIn("--verify-tag", create_command)
        self.assertIn("--prerelease", create_command)
        self.assertEqual(sorted(Path(arg).name for arg in create_command[-4:]), assets)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        record = json.loads(receipt.read_text())
        self.assertEqual(record["assets"], {name: build.digest(output / name) for name in assets})
        self.assertIn("never auto-published", record["result"])

    def test_sign_uses_explicit_encrypted_key_then_verifies(self):
        output = self.make_build()
        secret = self.root / "format-fixture.key"
        # Format-only dummy bytes for the mocked process; no cryptographic key is generated.
        secret.write_bytes(b"untrusted comment: test fixture\n" + base64.b64encode(b"EdScB2" + b"\0" * 152) + b"\n")
        calls = []
        def minisign(command, **kwargs):
            calls.append(command)
            if "-S" in command:
                (output / "SHA256SUMS.minisig").write_text("mock signature\n")
            return subprocess.CompletedProcess(command, 0)
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(release, "public_key", return_value=self.root / "pin.pub"), \
                mock.patch.object(release.subprocess, "run", side_effect=minisign):
            release.sign(output, secret, self.root / "pin.pub")
            with self.assertRaisesRegex(build.ReleaseError, "overwrite"):
                release.sign(output, secret, self.root / "pin.pub")
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1][:2], ["minisign", "-S"])
        self.assertEqual(calls[1][calls[1].index("-s") + 1], str(secret))
        self.assertNotIn("-W", calls[1])
        self.assertEqual(calls[2][:3], ["minisign", "-V", "-H"])

    def test_ci_and_unencrypted_key_refused_without_signing(self):
        secret = self.root / "secret.key"
        secret.write_text("not a signing key\n")
        with mock.patch.dict(os.environ, {"CI": "true"}), \
                self.assertRaisesRegex(build.ReleaseError, "CI"):
            release.sign(self.root, secret, self.root / "pin.pub")
        with mock.patch.dict(os.environ, {}, clear=True), \
                self.assertRaisesRegex(build.ReleaseError, "encrypted"):
            release.sign(self.root / "artifacts", secret, self.root / "pin.pub")

    def test_public_patterns_and_pin_location_fail_closed(self):
        for data in (b"https://notion.so/example", b"/Users/example/private", b"collection://example",
                     b"-----BEGIN PRIVATE KEY-----"):
            with self.assertRaises(build.ReleaseError):
                release.scan_public(data)
        key = self.root / "untrusted.pub"
        key.write_text("untrusted key\n")
        with self.assertRaisesRegex(build.ReleaseError, "pinned"):
            release.public_key(key)

    def test_checked_in_payload_covers_installed_mapping(self):
        spec = importlib.util.spec_from_file_location("installed_manifest", ROOT / "scripts/dev/release-manifest.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        names = [line for line in (ROOT / "scripts/release/payload.txt").read_text().splitlines()
                 if line and not line.startswith("#")]
        self.assertEqual(names, sorted(set(names)))
        self.assertTrue(set(module.installed_paths(ROOT)).issubset(names))
        self.assertIn("scripts/install/bounded_update.py", names)
        self.assertIn("scripts/dev/release-manifest.py", names)
        for name in names:
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertFalse(any(name.startswith((".git/", "tests/", "hardware/")) for name in names))

    def test_generated_controller_proof_matches_packaged_modules_and_is_retained(self):
        output = self.make_build()
        archive = release.checksums(output)
        with tarfile.open(output / archive) as tar:
            raw = tar.extractfile(build.CONTROLLER_DESCRIPTOR).read()
            value = build.parse_controller_descriptor(raw)
            self.assertEqual(value['controller_version'], '1.3.0')
            self.assertEqual(value['min_application_version'], TAG)
            for entry in value['modules']:
                source = 'scripts/install/' + Path(entry['path']).name
                self.assertEqual(build.digest_stream(tar.extractfile(source)), entry['sha256'])
                self.assertEqual(tar.getmember(source).size, entry['size'])
            self.assertEqual(tar.getmember(build.CONTROLLER_DESCRIPTOR).mode, 0o644)
            self.assertEqual(tar.getmember(build.CONTROLLER_SIGNATURE).mode, 0o644)
        data = json.loads((output / 'release-manifest.json').read_bytes())
        mapping = {item['source']: item['installed'] for item in data['files']}
        for path in (build.CONTROLLER_DESCRIPTOR, build.CONTROLLER_SIGNATURE):
            self.assertEqual(mapping[path], '/opt/messagebox/' + path)
            self.assertFalse((self.repo / path).exists())

    def test_builder_requires_the_complete_controller_payload(self):
        listing = self.repo / 'scripts/release/payload.txt'
        listing.write_text(listing.read_text().replace('scripts/install/update_controller.py\n', ''))
        with self.assertRaisesRegex(ValueError, 'controller module missing'):
            self.make_build()
        self.assertFalse((self.root / 'output').exists())

    def test_controller_mismatch_or_missing_proof_blocks_publish_before_any_github_call(self):
        output = self.make_build()
        archive = release.checksums(output)
        original = (output / archive).read_bytes()
        notes = self.root / 'notes.md'
        notes.write_text('Synthetic release notes.\n')
        (output / 'SHA256SUMS.minisig').write_text('synthetic app signature\n')
        for case in ('module', 'missing_signature', 'malformed_descriptor', 'oversized_descriptor'):
            with self.subTest(case=case):
                (output / archive).write_bytes(original)
                with tarfile.open(output / archive) as tar:
                    members = [(member, tar.extractfile(member).read()) for member in tar.getmembers()]
                with tarfile.open(output / archive, 'w:gz') as tar:
                    for member, data in members:
                        if case == 'missing_signature' and member.name == build.CONTROLLER_SIGNATURE:
                            continue
                        if case == 'module' and member.name == 'scripts/install/bounded_update.py':
                            data += b'# tampered\n'
                        if member.name == build.CONTROLLER_DESCRIPTOR:
                            if case == 'malformed_descriptor':
                                data = b"__import__('os').abort()\n"
                            elif case == 'oversized_descriptor':
                                data = b' ' * 20000
                        member.size = len(data)
                        tar.addfile(member, io.BytesIO(data))
                (output / 'SHA256SUMS').write_text(''.join(
                    f'{build.digest(output / name)}  {name}\n' for name in sorted((archive, 'release-manifest.json'))))
                receipt = self.root / (case + '-receipt.json')
                with mock.patch.object(release, 'public_key', return_value=self.root / 'pin.pub'), \
                        mock.patch.object(release.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                        mock.patch.object(release.subprocess, 'check_output') as network:
                    with self.assertRaises(ValueError):
                        release.publish(output, self.root / 'pin.pub', notes, receipt)
                    network.assert_not_called()
                self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
