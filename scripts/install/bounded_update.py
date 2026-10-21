#!/usr/bin/env python3
"""Install or roll back one manifest-bounded Button Box release."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import sys
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import wave
from pathlib import Path, PurePosixPath


RELEASE_METADATA = "/opt/messagebox/release.json"
MODE_MARKER = "/etc/messagebox-onboarding/enabled"
LEGACY_WANTS = "/etc/systemd/system/multi-user.target.wants"
SELECTOR_PATHS = (
    MODE_MARKER,
    "/etc/systemd/system/messagebox.target.wants/messagebox-wifi-watchdog.timer",
    f"{LEGACY_WANTS}/messagebox.target",
    f"{LEGACY_WANTS}/comitup.service",
    f"{LEGACY_WANTS}/messagebox-mode-reconcile.path",
)
MODE_GENERATOR = "/usr/local/lib/systemd/system-generators/messagebox-mode-generator"
MODE_MIGRATION = "/usr/lib/messagebox/messagebox-mode-migrate.py"
UPDATE_LOCK = "/run/lock/messagebox-bounded-update.lock"
BACKUP_FORMAT = 2
UNIT_SETTLE_TIMEOUT = 30.0
UNIT_SETTLE_STABLE = 2.0
UNIT_SETTLE_POLL = 0.5
TRANSITIONAL_ACTIVE_STATES = {"activating", "deactivating", "reloading"}
UNIT_NAME = re.compile(
    r"(?:[A-Za-z0-9:_.@-]|\\x[0-9a-fA-F]{2})+"
    r"\.(?:service|socket|device|mount|automount|swap|target|path|timer|slice|scope)\Z"
)

UNITS = (
    "messagebox.target",
    "messagebox-audio-detect.service",
    "messagebox-button.service",
    "messagebox-sync.service",
    "messagebox-poller.service",
    "messagebox-dash.service",
    "messagebox-nfc.service",
    "messagebox-wifi-change.service",
    "messagebox-wifi-change.path",
    "messagebox-wifi-watchdog.service",
    "messagebox-wifi-watchdog.timer",
    "messagebox-mode-reconcile.service",
    "messagebox-mode-reconcile.path",
    "comitup.service",
    "comitup-web.service",
    "messagebox-onboarding-home.service",
    "messagebox-onboarding-nfc.service",
    "messagebox-onboarding-complete.service",
    "messagebox-onboarding-complete.path",
    "messagebox-onboarding-button.service",
    "messagebox-onboarding-voice-gate.service",
    "messagebox-onboarding-voice.path",
    "messagebox-onboarding-voice.target",
    "messagebox-whatsapp-pairing.service",
    "messagebox-wifi-reset.service",
)
START_ORDER = (
    "messagebox-audio-detect.service",
    "messagebox-sync.service",
    "messagebox-poller.service",
    "messagebox-nfc.service",
    "messagebox-dash.service",
    "messagebox-button.service",
    "comitup.service",
    "messagebox-whatsapp-pairing.service",
    "messagebox-onboarding-nfc.service",
    "comitup-web.service",
    "messagebox-onboarding-home.service",
    "messagebox-onboarding-button.service",
    "messagebox-onboarding-voice-gate.service",
    "messagebox-onboarding-voice.target",
    "messagebox-onboarding-complete.service",
    "messagebox-wifi-reset.service",
    "messagebox-wifi-change.service",
    "messagebox-mode-reconcile.service",
    "messagebox-onboarding-complete.path",
    "messagebox-onboarding-voice.path",
    "messagebox-wifi-change.path",
    "messagebox-wifi-watchdog.service",
    "messagebox-wifi-watchdog.timer",
    "messagebox-mode-reconcile.path",
    "messagebox.target",
)

EXPLICIT_TARGETS = {
    "config/journald.conf.d/messagebox.conf": "/etc/systemd/journald.conf.d/messagebox.conf",
    "scripts/install/audio_config.py": "/usr/lib/messagebox/audio_config.py",
    "scripts/install/messagebox-mode-migrate.py": MODE_MIGRATION,
    "scripts/messageboxctl": "/usr/local/bin/messageboxctl",
    "scripts/commands/messagebox-contact": "/usr/local/bin/messagebox-contact",
    "scripts/commands/messagebox-comitup-state": "/usr/local/sbin/messagebox-comitup-state",
    "scripts/commands/messagebox-init-wifi-onboarding": "/usr/local/sbin/messagebox-init-wifi-onboarding",
    "scripts/dev/onboard.sh": "/usr/local/bin/messagebox-dev-onboard",
    "scripts/dev/hardware-test.sh": "/opt/messagebox/dev/hardware-test.sh",
    "config/requirements-nfc.txt": "/opt/messagebox/config/requirements-nfc.txt",
}
for name in ("hello_piano", "sunshine", "bouncy", "sing_along", "island", "hello", "ukulele"):
    for suffix in (".wav", ".lamp.json"):
        EXPLICIT_TARGETS[f"sounds/ringtones/{name}{suffix}"] = f"/opt/messagebox/ringtones/{name}{suffix}"
EXPLICIT_TARGETS["sounds/ringtones/manifest.json"] = "/opt/messagebox/ringtones/manifest.json"

SOUND_SOURCES = (
    "sounds/cues/README.md",
    "sounds/cues/cue-all_set.wav",
    "sounds/cues/cue-card.wav",
    "sounds/cues/cue-card_saved.wav",
    "sounds/cues/cue-connected.wav",
    "sounds/cues/cue-deleted.wav",
    "sounds/cues/cue-listened.wav",
    "sounds/cues/cue-msg_end.wav",
    "sounds/cues/cue-msg_start.wav",
    "sounds/cues/cue-offline.wav",
    "sounds/cues/cue-oops.wav",
    "sounds/cues/cue-press.wav",
    "sounds/cues/cue-ready.wav",
    "sounds/cues/cue-rec_go.wav",
    "sounds/cues/cue-rec_limit.wav",
    "sounds/cues/cue-sent.wav",
    "sounds/cues/cue-still_trying.wav",
    "sounds/cues/cues.json",
    "sounds/cues/manifest.json",
    "sounds/voice/README.md",
    "sounds/voice/manifest.json",
    "sounds/voice/voice-all-set.wav",
    "sounds/voice/voice-ask-send-1.wav",
    "sounds/voice/voice-ask-send-2.wav",
    "sounds/voice/voice-ask-send-3.wav",
    "sounds/voice/voice-card-needed.wav",
    "sounds/voice/voice-card-prompt.wav",
    "sounds/voice/voice-card-saved.wav",
    "sounds/voice/voice-card-unknown.wav",
    "sounds/voice/voice-count-new.wav",
    "sounds/voice/voice-count-reply.wav",
    "sounds/voice/voice-empty.wav",
    "sounds/voice/voice-fail.wav",
    "sounds/voice/voice-last-chance.wav",
    "sounds/voice/voice-listened.wav",
    "sounds/voice/voice-msg-start.wav",
    "sounds/voice/voice-not-sent.wav",
    "sounds/voice/voice-online.wav",
    "sounds/voice/voice-online-ready.wav",
    "sounds/voice/voice-setup-online.wav",
    "sounds/voice/voice-review.wav",
    "sounds/voice/voice-stuck.wav",
)
SOUND_SOURCES += ("sounds/voices/README.md",) + tuple(
    f"sounds/voices/{pack}/{name}"
    for pack in ("pirate", "alien", "dj", "robot", "french", "charlie", "tata")
    for name in ("manifest.json",) + tuple(
        Path(source).name for source in SOUND_SOURCES
        if source.startswith("sounds/voice/") and source.endswith(".wav")
        and Path(source).name != "voice-setup-online.wav"
    )
)
CLOUD_SOUND_SOURCES = ("sounds/cloud/manifest.json", "sounds/cloud/reaction.wav",
                  "sounds/cloud/text-message.wav", "sounds/cloud/nothing.wav") + tuple(
    f"sounds/cloud/more-{count}.wav" for count in range(1, 98)
)
SOUND_SOURCES += CLOUD_SOUND_SOURCES
# The original shipped files are a floor, not a ceiling. Existing ringtone
# destinations remain stable for the shared runtime and legacy installations.
REQUIRED_SOUND_SOURCES = frozenset(SOUND_SOURCES) | frozenset(
    source for source in EXPLICIT_TARGETS if source.startswith("sounds/")
)
for source in SOUND_SOURCES:
    EXPLICIT_TARGETS[source] = "/opt/messagebox/" + source
SOUND_PATH = re.compile(
    r"sounds/(?:cues|cloud|ringtones|voice|voices/(?:pirate|alien|dj|robot|french|charlie|tata|buttonbox))/"
    r"(?:[a-z0-9][a-z0-9._-]{0,80}\.wav|manifest\.json|cues\.json)\Z"
)
MAX_SOUND_FILE = 2 * 1024 * 1024
MAX_SOUND_TOTAL = 160 * 1024 * 1024
MAX_SOUND_FILES = 2000
RETIRED_SOUNDS = tuple(
    "/opt/messagebox/sounds/guided-reply/" + name + ".wav"
    for name in ("reply-countdown", "standalone-countdown", "press-to-send", "delete-warning", "not-sent")
) + ("/opt/messagebox/sounds/feedback/sent-swoosh.wav",)

EXECUTABLE_TARGETS = {
    MODE_GENERATOR,
    "/usr/local/bin/messageboxctl",
    "/usr/local/bin/messagebox-contact",
    "/usr/local/bin/messagebox-dev-onboard",
    "/usr/local/sbin/messagebox-comitup-state",
    "/usr/local/sbin/messagebox-init-wifi-onboarding",
    "/opt/messagebox/dev/hardware-test.sh",
    "/opt/messagebox/messagebox/syncloop.sh",
}
ENABLED_STATES = {"enabled"}
DISABLED_STATES = {"disabled"}
PASSIVE_ENABLED_STATES = {"alias", "generated", "indirect", "not-found", "static", "transient"}
# systemd refuses further activation after these results. For a .path/.timer
# activator this is a rate limit on an old watcher, not a broken restoration.
LIMIT_RESULTS = {"start-limit-hit", "unit-start-limit-hit", "trigger-limit-hit"}
# The boot-mode generator adds one runtime wants link from the marker on every
# daemon-reload; systemctl reports that unit as enabled-runtime.
GENERATED_MODE_UNITS = {"setup": "comitup.service", "runtime": "messagebox.target"}


class UpdateError(RuntimeError):
    pass


class UnitTransitioning(UpdateError):
    """A unit is between states; a later scan can see it settled."""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _expected_target(source):
    if source in EXPLICIT_TARGETS:
        return EXPLICIT_TARGETS[source]
    if SOUND_PATH.fullmatch(source):
        return "/opt/messagebox/" + source
    path = PurePosixPath(source)
    if len(path.parts) >= 2 and path.parts[0] == "messagebox":
        if path.suffix not in {".css", ".html", ".js", ".py", ".sh"}:
            return None
        return "/opt/messagebox/" + source
    if len(path.parts) >= 2 and path.parts[0] == "systemd":
        if path.name == "messagebox.tmpfiles.conf":
            return "/etc/tmpfiles.d/messagebox.conf"
        if path.name == "messagebox-mode-generator":
            return MODE_GENERATOR
        if path.name == "messagebox.conf" and path.parent.name.endswith(".service.d"):
            return f"/etc/systemd/system/{path.parent.name}/messagebox.conf"
        if (
            path.name == "messagebox.target" or path.name.startswith("messagebox-")
        ) and path.suffix in {".path", ".service", ".target", ".timer"}:
            return "/etc/systemd/system/" + path.name
    return None


def _rollback_target_allowed(absolute):
    exact = (
        set(SELECTOR_PATHS)
        | {RELEASE_METADATA, MODE_GENERATOR, "/etc/tmpfiles.d/messagebox.conf"}
        | set(EXPLICIT_TARGETS.values())
        | set(RETIRED_SOUNDS)
        | {
            "/etc/systemd/system/messagebox.target",
            "/etc/systemd/system/comitup.service.d/messagebox.conf",
            "/etc/systemd/system/comitup-web.service.d/messagebox.conf",
        }
    )
    sound_source = absolute.removeprefix("/opt/messagebox/")
    return (SOUND_PATH.fullmatch(sound_source) and _expected_target(sound_source) == absolute) or absolute in exact or (
        absolute.startswith("/opt/messagebox/messagebox/")
        or absolute.startswith("/opt/messagebox/sounds/guided-reply/")
        or absolute.startswith("/etc/systemd/system/messagebox-")
    )


def _install_mode(target):
    return 0o755 if target in EXECUTABLE_TARGETS else 0o644


def _rooted(root, absolute):
    pure = PurePosixPath(absolute)
    if not pure.is_absolute() or ".." in pure.parts:
        raise UpdateError("manifest contains an unsafe installed path")
    selected = Path(root)
    pointer = selected / "opt/messagebox"
    if selected != Path("/") and str(pure).startswith("/opt/messagebox/") and pointer.is_symlink():
        controlled = controlled_release(selected)
        return selected.joinpath(controlled.lstrip("/"), str(pure).removeprefix("/opt/messagebox/"))
    return selected.joinpath(*pure.parts[1:])


def _check_parents(path, root):
    root = Path(root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise UpdateError("path escapes the selected root") from exc
    current = root
    for part in relative.parts[:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) and current == _rooted(root, "/opt/messagebox"):
            controlled_release(root)
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise UpdateError("destination has an unsafe parent")


def _check_absolute_parents(path):
    path = Path(path)
    if not path.is_absolute():
        raise UpdateError("backup path must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise UpdateError("backup path has an unsafe parent")


def _validate_staged_file(path, source_root, trusted_uid):
    source_root = Path(source_root)
    path = Path(path)
    try:
        relative = path.relative_to(source_root)
    except ValueError as exc:
        raise UpdateError("release input is outside the staged source") from exc
    current = source_root
    directories = [source_root]
    for part in relative.parts[:-1]:
        current /= part
        directories.append(current)
    for directory in directories:
        try:
            metadata = directory.lstat()
        except FileNotFoundError as exc:
            raise UpdateError("release source is incomplete") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != trusted_uid
            or metadata.st_mode & 0o022
        ):
            raise UpdateError("release source directory is not trusted")
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise UpdateError("release source is incomplete") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != trusted_uid
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
    ):
        raise UpdateError("release source file is not trusted")
    return metadata


def _validate_sound(path, metadata):
    if metadata.st_size > MAX_SOUND_FILE:
        raise UpdateError("release sound file exceeds size limit")
    if path.suffix != ".wav":
        return
    try:
        with wave.open(str(path), "rb") as sound:
            frames = sound.getnframes()
            if (sound.getnchannels() != 1 or sound.getsampwidth() != 2
                    or sound.getframerate() != 48000 or sound.getcomptype() != "NONE"
                    or frames <= 0 or frames * 2 > MAX_SOUND_FILE
                    or len(sound.readframes(frames)) != frames * 2):
                raise ValueError("unsupported or truncated WAV")
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        raise UpdateError("release sound WAV is invalid") from exc


def _load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    specification = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _candidate_sound_pack(source_root):
    """Load the staged validator with the staged package importable, then forget it.

    The updater runs from the staging tree, where `messagebox` is not on sys.path.
    """
    saved_path = list(sys.path)
    saved = {name: module for name, module in sys.modules.items()
             if name == "messagebox" or name.startswith("messagebox.")}
    sys.path.insert(0, str(source_root))
    try:
        return _load_module("messagebox_candidate_sound_pack", source_root / "messagebox/sound_pack.py")
    finally:
        sys.path[:] = saved_path
        for name in [name for name in sys.modules if name == "messagebox" or name.startswith("messagebox.")]:
            del sys.modules[name]
        sys.modules.update(saved)


@contextlib.contextmanager
def _update_lock(root):
    root = Path(root)
    trusted_uid = 0 if root == Path("/") else os.geteuid()
    path = _rooted(root, UPDATE_LOCK)
    _check_parents(path, root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise UpdateError("bounded update lock is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != trusted_uid
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise UpdateError("bounded update lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("another bounded update operation is active") from exc
        yield
    finally:
        os.close(descriptor)


def load_candidate(source_root, manifest_path, root):
    source_root = Path(source_root).resolve()
    manifest_path = Path(manifest_path).absolute()
    trusted_uid = 0 if Path(root) == Path("/") else os.geteuid()
    _validate_staged_file(manifest_path, source_root, trusted_uid)
    canonical_script = source_root / "scripts/dev/release-manifest.py"
    _validate_staged_file(canonical_script, source_root, trusted_uid)
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError("release manifest is unavailable or invalid") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise UpdateError("release manifest has an invalid schema")
    version = manifest.get("version")
    commit = manifest.get("commit")
    if not isinstance(version, str) or not version or not isinstance(commit, str):
        raise UpdateError("release manifest lacks release identity")
    if len(version) > 64 or any(
        character not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.+_-"
        for character in version
    ):
        raise UpdateError("release manifest version is invalid")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise UpdateError("release manifest commit is invalid")

    entries = []
    sources = set()
    targets = set()
    sound_total = sound_count = 0
    for raw in manifest["files"]:
        if not isinstance(raw, dict):
            raise UpdateError("release manifest has an invalid file entry")
        source = raw.get("source")
        target = raw.get("installed")
        expected_hash = raw.get("sha256")
        if not all(isinstance(value, str) for value in (source, target, expected_hash)):
            raise UpdateError("release manifest has an invalid file entry")
        source_pure = PurePosixPath(source)
        if source_pure.is_absolute() or ".." in source_pure.parts or str(source_pure) != source:
            raise UpdateError("release manifest contains an unsafe source path")
        if _expected_target(source) != target:
            raise UpdateError("release manifest contains a source outside the install allowlist")
        if source in sources or target in targets:
            raise UpdateError("release manifest contains a duplicate source or destination")
        if len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash):
            raise UpdateError("release manifest contains an invalid hash")
        source_path = source_root.joinpath(*source_pure.parts)
        metadata = _validate_staged_file(source_path, source_root, trusted_uid)
        if source.startswith("sounds/"):
            sound_total += metadata.st_size
            sound_count += 1
            if sound_total > MAX_SOUND_TOTAL or sound_count > MAX_SOUND_FILES:
                raise UpdateError("release sound pack exceeds size or file count limit")
            _validate_sound(source_path, metadata)
        if _sha256(source_path) != expected_hash:
            raise UpdateError("release source hash does not match the manifest")
        destination = _rooted(root, target)
        _check_parents(destination, root)
        try:
            current = destination.lstat()
        except FileNotFoundError:
            current = None
        if current is not None and (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
            raise UpdateError("installed destination is not a regular file")
        entries.append(
            {
                "source": source,
                "source_path": source_path,
                "target": target,
                "destination": destination,
                "sha256": expected_hash,
                "mode": _install_mode(target),
            }
        )
        sources.add(source)
        targets.add(target)
    # A converted Cloud box always needs the complete floor, including Cloud
    # fallbacks. Preserve the older non-Cloud operator release contract.
    sound_entries = {source for source in sources if source.startswith("sounds/")}
    required_sounds = set(REQUIRED_SOUND_SOURCES)
    if not _rooted(root, "/opt/messagebox").is_symlink() and not any(
            source.startswith("sounds/cloud/") for source in sound_entries):
        required_sounds.difference_update(CLOUD_SOUND_SOURCES)
    if not required_sounds.issubset(sound_entries):
        raise UpdateError("release sound pack is incomplete")
    try:
        _candidate_sound_pack(source_root).validate_sounds(source_root / "sounds")
    except (OSError, ValueError, ImportError) as exc:
        raise UpdateError("release sound assets are invalid") from exc
    for absolute in RETIRED_SOUNDS:
        destination = _rooted(root, absolute)
        _check_parents(destination, root)
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UpdateError("retired sound is not a regular file")
    canonical = _load_module(
        "messagebox_bounded_update_release_manifest", canonical_script
    ).installed_paths(source_root)
    declared = {entry["source"]: entry["target"] for entry in entries}
    if declared != canonical:
        raise UpdateError("release manifest is not the complete installed release")
    if MODE_GENERATOR not in targets or MODE_MIGRATION not in targets:
        raise UpdateError("release manifest lacks the checked mode migration inputs")

    migration_entry = next(item for item in entries if item["target"] == MODE_MIGRATION)
    generator_entry = next(item for item in entries if item["target"] == MODE_GENERATOR)
    if stat.S_IMODE(generator_entry["source_path"].stat().st_mode) != 0o755:
        raise UpdateError("staged mode generator is not executable")
    try:
        migration = _load_module(
            "messagebox_bounded_update_migration_check", migration_entry["source_path"]
        )
        migration.check(
            generator_entry["source_path"],
            marker=_rooted(root, MODE_MARKER),
            legacy_dir=_rooted(root, LEGACY_WANTS),
            trusted_uid=trusted_uid,
        )
        reconcile_link = _rooted(
            root, f"{LEGACY_WANTS}/messagebox-mode-reconcile.path"
        )
        if reconcile_link.is_symlink() and os.readlink(reconcile_link) != (
            "/etc/systemd/system/messagebox-mode-reconcile.path"
        ):
            raise UpdateError("existing mode reconciliation link is unsafe")
        if reconcile_link.exists() and not reconcile_link.is_symlink():
            raise UpdateError("existing mode reconciliation link is unsafe")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise UpdateError("staged mode migration check failed") from exc
    return manifest, entries, hashlib.sha256(manifest_bytes).hexdigest()


def _record_path(path, absolute, backup_files, index, *, allow_symlink=False):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"path": absolute, "kind": "absent"}
    if stat.S_ISLNK(metadata.st_mode):
        if not allow_symlink:
            raise UpdateError("cannot back up an unexpected symbolic link")
        return {"path": absolute, "kind": "symlink", "target": os.readlink(path)}
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise UpdateError("cannot back up an unsafe filesystem object")
    stored = backup_files / f"{index:04d}"
    with open(path, "rb") as source, open(stored, "xb") as destination:
        shutil.copyfileobj(source, destination)
        destination.flush()
        os.fsync(destination.fileno())
        os.fchmod(destination.fileno(), 0o600)
    return {
        "path": absolute,
        "kind": "file",
        "backup": stored.name,
        "sha256": _sha256(stored),
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
    }


def _run(command, run, **kwargs):
    try:
        return run(command, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"command failed: {command[0]}") from exc


def _unit_states(run, *, allow_transitional=False, deadline=None):
    def query(operation, unit):
        options = {"check": False, "capture_output": True, "text": True}
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpdateError(f"timed out reading managed unit state: {unit}")
            options["timeout"] = remaining
        return _run(["systemctl", operation, unit], run, **options).stdout.strip()

    states = {}
    for unit in UNITS:
        active = query("is-active", unit)
        enabled = query("is-enabled", unit)
        if active in TRANSITIONAL_ACTIVE_STATES and not allow_transitional:
            raise UnitTransitioning(f"managed unit is still transitioning: {unit} ({active})")
        known_active = {"active", "inactive", "failed"}
        if allow_transitional:
            known_active |= TRANSITIONAL_ACTIVE_STATES
        if active not in known_active:
            raise UpdateError(f"managed unit has an unsupported active state: {unit}")
        known_enabled = ENABLED_STATES | DISABLED_STATES | PASSIVE_ENABLED_STATES | {
            "enabled-runtime",
            "masked",
            "masked-runtime",
        }
        if enabled not in known_enabled:
            raise UpdateError(f"managed unit has an unsupported enabled state: {unit}")
        states[unit] = {"active": active, "enabled": enabled}
    return states


def _valid_auxiliary_unit(unit):
    return (
        isinstance(unit, str)
        and not unit.startswith("-")
        and UNIT_NAME.fullmatch(unit) is not None
        and unit not in UNITS
    )


def _active_auxiliary_units(states, run):
    # ConsistsOf is systemd's reverse PartOf relation. Follow only units whose
    # stop can propagate from an active managed unit. An inactive intermediate
    # can still pass a stop to an active descendant.
    pending = [unit for unit in UNITS if states[unit]["active"] == "active"]
    seen = set(pending)
    active = []
    while pending:
        unit = pending.pop(0)
        result = _run(
            ["systemctl", "show", "--property=ConsistsOf", "--value", unit],
            run, check=True, capture_output=True, text=True,
        )
        if not isinstance(result.stdout, str):
            raise UpdateError("cannot read auxiliary unit dependencies")
        for dependent in result.stdout.split():
            if dependent in seen:
                continue
            if dependent not in UNITS and not _valid_auxiliary_unit(dependent):
                raise UpdateError("systemd returned an invalid auxiliary unit name")
            seen.add(dependent)
            if dependent in UNITS:
                pending.append(dependent)
                continue
            result = _run(
                ["systemctl", "is-active", dependent], run,
                check=False, capture_output=True, text=True,
            )
            state = result.stdout.strip() if isinstance(result.stdout, str) else None
            if state in TRANSITIONAL_ACTIVE_STATES:
                raise UnitTransitioning(f"auxiliary unit is still transitioning: {dependent} ({state})")
            if state not in {"active", "inactive", "failed"}:
                raise UpdateError("auxiliary unit has an unsupported active state")
            if state == "active":
                active.append(dependent)
            pending.append(dependent)
    return active


def _settled_unit_scan(run, *, auxiliary=True):
    """Return settled managed states and active auxiliary units.

    Timer-started oneshots pass through "activating" for a moment on every
    tick. Poll until no scanned unit is transitioning before failing.
    """
    deadline = time.monotonic() + UNIT_SETTLE_TIMEOUT
    while True:
        try:
            states = _unit_states(run)
            return states, (_active_auxiliary_units(states, run) if auxiliary else [])
        except UnitTransitioning as exc:
            now = time.monotonic()
            if now >= deadline:
                raise UpdateError(f"{exc}; did not settle within {UNIT_SETTLE_TIMEOUT:g}s") from exc
            time.sleep(min(UNIT_SETTLE_POLL, deadline - now))


def create_backup(backup_dir, root, entries, unit_states, auxiliary_units=(), *, external_only=False):
    backup_dir = Path(backup_dir)
    _check_absolute_parents(backup_dir)
    if backup_dir.exists() or backup_dir.is_symlink():
        raise UpdateError("backup directory must not already exist")
    backup_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_metadata = backup_dir.parent.lstat()
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise UpdateError("backup parent is unsafe")
    backup_dir.mkdir(mode=0o700)
    backup_files = backup_dir / "files"
    backup_files.mkdir(mode=0o700)
    records = []
    targets = [item["target"] for item in entries]
    if not external_only:
        targets.extend(RETIRED_SOUNDS)
        targets.append(RELEASE_METADATA)
    for absolute in targets:
        records.append(
            _record_path(_rooted(root, absolute), absolute, backup_files, len(records))
        )
    for absolute in SELECTOR_PATHS:
        records.append(
            _record_path(
                _rooted(root, absolute),
                absolute,
                backup_files,
                len(records),
                allow_symlink=absolute != MODE_MARKER,
            )
        )
    state = {
        "format": BACKUP_FORMAT, "files": records, "units": unit_states,
        "auxiliary_units": list(auxiliary_units),
    }
    state_path = backup_dir / "state.json"
    with open(state_path, "x", encoding="utf-8") as output:
        output.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
        os.fchmod(output.fileno(), 0o600)
    _fsync_directory(backup_files)
    _fsync_directory(backup_dir)
    return state


def _atomic_install(source, destination, mode, *, uid=0, gid=0):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = None
    try:
        with open(source, "rb") as input_file, tempfile.NamedTemporaryFile(
            "wb", dir=destination.parent, prefix=".messagebox-update.", delete=False
        ) as output:
            temporary = Path(output.name)
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
            if os.geteuid() == 0:
                os.fchown(output.fileno(), uid, gid)
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _stop_active(states, run, *, auxiliary_units=()):
    active = [
        unit for unit in reversed(UNITS)
        if states[unit]["active"] in {"active"} | TRANSITIONAL_ACTIVE_STATES
    ]
    activators = [unit for unit in [*active, *auxiliary_units] if unit.endswith((".path", ".timer"))]
    services = [unit for unit in active if unit not in activators]
    for batch in (activators, services):
        if batch:
            _run(["systemctl", "stop", *batch], run, check=True, timeout=30)


def _restore_enabled(states, run):
    for unit in UNITS:
        enabled = states[unit]["enabled"]
        if enabled in ENABLED_STATES:
            _run(["systemctl", "enable", unit], run, check=True)
        elif enabled == "enabled-runtime":
            _run(["systemctl", "enable", "--runtime", unit], run, check=True)
        elif enabled in DISABLED_STATES:
            _run(["systemctl", "disable", unit], run, check=True)
        elif enabled == "masked":
            _run(["systemctl", "mask", unit], run, check=True)
        elif enabled == "masked-runtime":
            _run(["systemctl", "mask", "--runtime", unit], run, check=True)


def _restore_active(states, run, *, also_active=(), skip=()):
    if set(START_ORDER) != set(UNITS) or len(START_ORDER) != len(UNITS):
        raise UpdateError("managed unit start order is incomplete")
    # ComItUp replaces portal jobs while moving HOTSPOT -> CONNECTED. It must
    # own those starts; a simultaneous manual start can be canceled by it.
    comitup_owns_portals = states["comitup.service"]["active"] == "active"
    for unit in START_ORDER:
        if comitup_owns_portals and unit in {
            "comitup-web.service", "messagebox-onboarding-home.service"
        }:
            continue
        if states[unit]["active"] == "active" and unit not in also_active and unit not in skip:
            _run(
                ["systemctl", "--job-mode=ignore-dependencies", "start", unit],
                run,
                check=True,
            )


def _is_activator(unit):
    return unit.endswith((".path", ".timer"))


def _limit_hit(unit, run, *, timeout=5):
    result = _run(
        ["systemctl", "show", "--property=Result", "--value", unit], run,
        check=False, capture_output=True, text=True, timeout=timeout,
    )
    return isinstance(result.stdout, str) and result.stdout.strip() in LIMIT_RESULTS


def _reset_limited_activators(run, units):
    """Clear only rate-limited activators so systemd may start them again.

    Services and activators that failed for any other reason keep their failed
    state and still fail verification when they were expected to run.
    """
    reset = []
    for unit in units:
        if not _is_activator(unit):
            continue
        observed = _run(["systemctl", "is-active", unit], run,
                        check=False, capture_output=True, text=True, timeout=5)
        state = observed.stdout.strip() if isinstance(observed.stdout, str) else None
        if state == "failed" and _limit_hit(unit, run):
            reset.append(unit)
    if reset:
        _run(["systemctl", "reset-failed", *reset], run, check=True, timeout=30)
    return reset


def _restore_auxiliary_active(auxiliary_units, run, *, skip=()):
    for unit in auxiliary_units:
        if unit in skip:
            continue
        _run(
            ["systemctl", "--job-mode=ignore-dependencies", "start", unit],
            run, check=True,
        )


def _verify_active(states, run, *, also_active=(), auxiliary_units=()):
    expected = {
        unit: "active" if unit in also_active else states[unit]["active"]
        for unit in UNITS
    }
    # ComItUp owns this optional web UI and may stop it on Wi-Fi connection,
    # including leaving a failed state after canceling its startup. A manually
    # running portal without ComItUp still has to be restored truthfully.
    optional_web = (states["comitup-web.service"]["active"] != "active"
                    or states["comitup.service"]["active"] == "active")

    limited = set()

    def matches(unit, observed):
        if unit == "comitup-web.service" and optional_web:
            return observed in {"active", "inactive", "failed"}
        if expected[unit] == "failed":
            # A unit that was failed before the update was not running. The
            # restoration does not start it; a reset-failed unit is inactive.
            return observed in {"inactive", "failed"}
        if observed == "failed" and unit in limited:
            return True
        return observed == expected[unit]

    deadline = time.monotonic() + UNIT_SETTLE_TIMEOUT
    stable_since = None
    differences = []
    while True:
        if time.monotonic() >= deadline:
            detail = "; ".join(differences) or "the restored state did not remain stable"
            raise UpdateError(f"managed unit state did not settle within {UNIT_SETTLE_TIMEOUT:g}s: {detail}")
        current = _unit_states(run, allow_transitional=True, deadline=deadline)
        for unit in UNITS:
            # The restored release's own activator hit systemd's rate limit.
            # Accept it only for .path/.timer units that were meant to run.
            if (current[unit]["active"] == "failed" and unit not in limited
                    and _is_activator(unit) and expected[unit] == "active"
                    and _limit_hit(unit, run, timeout=max(0.1, deadline - time.monotonic()))):
                limited.add(unit)
        differences = [
            f"{unit} expected {expected[unit]}, observed {current[unit]['active']}"
            for unit in UNITS if not matches(unit, current[unit]["active"])
        ]
        newly_failed = [
            unit for unit in UNITS
            if current[unit]["active"] == "failed" and not matches(unit, "failed")
        ]
        if newly_failed:
            raise UpdateError("restored managed unit failed: " + ", ".join(newly_failed))
        for unit in auxiliary_units:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpdateError("timed out reading auxiliary unit state")
            result = _run(
                ["systemctl", "is-active", unit], run,
                check=False, capture_output=True, text=True, timeout=remaining,
            )
            observed = result.stdout.strip() if isinstance(result.stdout, str) else None
            if observed == "failed":
                raise UpdateError("restored auxiliary unit failed")
            if observed not in {"active", "inactive"} | TRANSITIONAL_ACTIVE_STATES:
                raise UpdateError("auxiliary unit has an unsupported active state")
            if observed != "active":
                differences.append("auxiliary unit is not active")
        now = time.monotonic()
        if now >= deadline:
            detail = "; ".join(differences) or "verification finished after its deadline"
            raise UpdateError(f"managed unit state did not settle within {UNIT_SETTLE_TIMEOUT:g}s: {detail}")
        if differences:
            stable_since = None
        elif stable_since is None:
            stable_since = now
        elif now - stable_since >= UNIT_SETTLE_STABLE:
            return
        time.sleep(min(UNIT_SETTLE_POLL, max(0, deadline - now)))


def _verify_enabled(states, run, *, root=None):
    actual = _unit_states(
        run, allow_transitional=True, deadline=time.monotonic() + UNIT_SETTLE_TIMEOUT
    )
    generated = None
    if root is not None:
        # daemon-reload reruns the mode generator from the restored marker. A
        # snapshot taken after the marker changed without a reload recorded the
        # previous generator output; the regenerated link is the correct state.
        mode = "setup" if _rooted(root, MODE_MARKER).exists() else "runtime"
        generated = GENERATED_MODE_UNITS[mode]
    for unit in UNITS:
        observed = actual[unit]["enabled"]
        if observed == states[unit]["enabled"]:
            continue
        if unit == generated and observed == "enabled-runtime":
            continue
        raise UpdateError(f"managed enabled unit state does not match the recorded state: {unit}")


def _failure_cause(error):
    # Only updater-authored errors are safe to print; OS/library errors can
    # contain private paths or subprocess output. The exception chain is kept.
    return str(error) if isinstance(error, UpdateError) else type(error).__name__


def _validate_backup_state(state, backup_dir, root):
    if not isinstance(state, dict) or state.get("format") not in {1, BACKUP_FORMAT}:
        raise UpdateError("backup state has an unsupported format")
    if not isinstance(state.get("files"), list) or not isinstance(state.get("units"), dict):
        raise UpdateError("backup state is incomplete")
    records = []
    seen = set()
    trusted_uid = 0 if Path(root) == Path("/") else os.geteuid()
    files_metadata = (backup_dir / "files").lstat()
    if (
        not stat.S_ISDIR(files_metadata.st_mode)
        or files_metadata.st_uid != trusted_uid
        or stat.S_IMODE(files_metadata.st_mode) != 0o700
    ):
        raise UpdateError("backup file directory is unsafe")
    for record in state["files"]:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise UpdateError("backup contains an invalid file record")
        absolute = record["path"]
        if not _rollback_target_allowed(absolute):
            raise UpdateError("backup contains a path outside the rollback allowlist")
        if absolute in seen:
            raise UpdateError("backup contains a duplicate path")
        seen.add(absolute)
        path = _rooted(root, absolute)
        _check_parents(path, root)
        kind = record.get("kind")
        if kind == "file":
            stored_name = record.get("backup")
            if not isinstance(stored_name, str) or PurePosixPath(stored_name).name != stored_name:
                raise UpdateError("backup file reference is unsafe")
            stored = backup_dir / "files" / stored_name
            try:
                stored_metadata = stored.lstat()
            except FileNotFoundError as exc:
                raise UpdateError("backup file is missing") from exc
            if (
                not stat.S_ISREG(stored_metadata.st_mode)
                or stored_metadata.st_uid != trusted_uid
                or stored_metadata.st_nlink != 1
                or stat.S_IMODE(stored_metadata.st_mode) != 0o600
                or _sha256(stored) != record.get("sha256")
            ):
                raise UpdateError("backup file hash does not match its record")
            if (
                not isinstance(record.get("mode"), int)
                or not 0 <= record["mode"] <= 0o777
                or not isinstance(record.get("uid"), int)
                or record["uid"] < 0
                or not isinstance(record.get("gid"), int)
                or record["gid"] < 0
            ):
                raise UpdateError("backup file metadata is invalid")
        elif kind == "symlink":
            if absolute not in SELECTOR_PATHS or absolute == MODE_MARKER:
                raise UpdateError("backup contains an unexpected symbolic link")
            if not isinstance(record.get("target"), str):
                raise UpdateError("backup symbolic link target is invalid")
        elif kind != "absent":
            raise UpdateError("backup contains an invalid file kind")
        records.append((record, path))
    if set(state["units"]) != set(UNITS):
        raise UpdateError("backup unit state is incomplete")
    for unit in UNITS:
        unit_state = state["units"][unit]
        if not isinstance(unit_state, dict):
            raise UpdateError("backup unit state is invalid")
        if unit_state.get("active") not in {"active", "failed", "inactive"}:
            raise UpdateError("backup active unit state is invalid")
        enabled = unit_state.get("enabled")
        if enabled not in (
            ENABLED_STATES
            | DISABLED_STATES
            | PASSIVE_ENABLED_STATES
            | {"enabled-runtime", "masked", "masked-runtime"}
        ):
            raise UpdateError("backup enabled unit state is invalid")
    if state["format"] == 1:
        if "auxiliary_units" in state:
            raise UpdateError("version-one backup has unexpected auxiliary state")
    else:
        auxiliary = state.get("auxiliary_units")
        if (
            not isinstance(auxiliary, list)
            or any(not _valid_auxiliary_unit(unit) for unit in auxiliary)
            or len(set(auxiliary)) != len(auxiliary)
        ):
            raise UpdateError("backup auxiliary unit state is invalid")
    return records


def _restore_record(record, path, backup_dir):
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    if current is not None:
        if stat.S_ISDIR(current.st_mode):
            raise UpdateError("rollback destination is an unexpected directory")
        path.unlink()
    if record["kind"] == "absent":
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if record["kind"] == "symlink":
        path.symlink_to(record["target"])
        _fsync_directory(path.parent)
        return
    _atomic_install(
        backup_dir / "files" / record["backup"],
        path,
        record["mode"],
        uid=record["uid"],
        gid=record["gid"],
    )


def _rollback_locked(backup_dir, *, root, run):
    backup_dir = Path(backup_dir)
    _check_absolute_parents(backup_dir)
    try:
        backup_metadata = backup_dir.lstat()
        state_path = backup_dir / "state.json"
        state_metadata = state_path.lstat()
        state = json.loads(state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise UpdateError("backup state is unavailable or invalid") from exc
    if stat.S_ISLNK(backup_metadata.st_mode) or not stat.S_ISDIR(backup_metadata.st_mode):
        raise UpdateError("backup directory is unsafe")
    if stat.S_ISLNK(state_metadata.st_mode) or not stat.S_ISREG(state_metadata.st_mode):
        raise UpdateError("backup state is unsafe")
    trusted_uid = 0 if root == Path("/") else os.geteuid()
    if (
        backup_metadata.st_uid != trusted_uid
        or stat.S_IMODE(backup_metadata.st_mode) != 0o700
        or state_metadata.st_uid != trusted_uid
        or state_metadata.st_nlink != 1
        or stat.S_IMODE(state_metadata.st_mode) != 0o600
    ):
        raise UpdateError("backup permissions or ownership are unsafe")
    records = _validate_backup_state(state, backup_dir, root)
    auxiliary_units = state.get("auxiliary_units", ())

    current = _unit_states(
        run, allow_transitional=True, deadline=time.monotonic() + UNIT_SETTLE_TIMEOUT
    )
    _stop_active(current, run)
    for record, path in records:
        _restore_record(record, path, backup_dir)
    _run(["systemctl", "daemon-reload"], run, check=True)
    _restore_enabled(state["units"], run)
    _reset_limited_activators(run, [*UNITS, *auxiliary_units])
    _restore_active(state["units"], run)
    _restore_auxiliary_active(auxiliary_units, run)
    _verify_enabled(state["units"], run, root=root)
    _verify_active(state["units"], run, auxiliary_units=auxiliary_units)
    return state


def rollback(backup_dir, *, root=Path("/"), run=subprocess.run):
    root = Path(root).resolve()
    if root == Path("/") and os.geteuid() != 0:
        raise UpdateError("bounded rollback requires root")
    with _update_lock(root):
        return _rollback_locked(backup_dir, root=root, run=run)


def _apply_locked(source_root, manifest_path, backup_dir, *, root, run):
    manifest, entries, manifest_hash = load_candidate(source_root, manifest_path, root)
    states = _unit_states(run)
    auxiliary_units = _active_auxiliary_units(states, run)
    create_backup(backup_dir, root, entries, states, auxiliary_units)
    try:
        _stop_active(states, run)
        generator_entry = next(item for item in entries if item["target"] == MODE_GENERATOR)
        migration_entry = next(item for item in entries if item["target"] == MODE_MIGRATION)
        for entry in entries:
            if entry is generator_entry:
                continue
            _atomic_install(entry["source_path"], entry["destination"], entry["mode"])

        for absolute in RETIRED_SOUNDS:
            destination = _rooted(root, absolute)
            _check_parents(destination, root)
            try:
                current = destination.lstat()
            except FileNotFoundError:
                current = None
            if current is not None and (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
                raise UpdateError("retired sound changed after preflight")
            destination.unlink(missing_ok=True)
            if destination.parent.exists():
                _fsync_directory(destination.parent)

        migration = _load_module("messagebox_bounded_update_migration", migration_entry["source_path"])
        trusted_uid = 0 if root == Path("/") else root.stat().st_uid
        migration.migrate(
            generator_entry["source_path"],
            marker=_rooted(root, MODE_MARKER),
            lock=_rooted(root, "/run/lock/messagebox-mode-transition.lock"),
            destination=_rooted(root, MODE_GENERATOR),
            legacy_dir=_rooted(root, LEGACY_WANTS),
            generated_dir=_rooted(root, "/run/systemd/generator/multi-user.target.wants"),
            target_wants=_rooted(root, "/etc/systemd/system/messagebox.target.wants"),
            run=run,
            trusted_uid=trusted_uid,
        )
        _run(["systemctl", "enable", "messagebox-wifi-watchdog.timer"], run, check=True)
        if root == Path("/"):
            _run(["env", "PYTHONPATH=/opt/messagebox", "python3", "-m",
                  "messagebox.wifi_watchdog", "--configure"], run, check=True)
            Path("/var/log/journal").mkdir(mode=0o755, parents=True, exist_ok=True)
            _run(["systemctl", "restart", "systemd-journald.service"], run, check=True)
            _run(["journalctl", "--flush"], run, check=True)
        metadata = {
            "commit": manifest["commit"],
            "manifest_sha256": manifest_hash,
            "version": manifest["version"],
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as output:
            json.dump(metadata, output, indent=2, sort_keys=True)
            output.write("\n")
            metadata_source = Path(output.name)
        try:
            _atomic_install(metadata_source, _rooted(root, RELEASE_METADATA), 0o644)
        finally:
            metadata_source.unlink(missing_ok=True)
        for entry in entries:
            if _sha256(entry["destination"]) != entry["sha256"]:
                raise UpdateError("installed file hash does not match the manifest")
            if stat.S_IMODE(entry["destination"].stat().st_mode) != entry["mode"]:
                raise UpdateError("installed file mode does not match policy")
        _restore_active(
            states, run, also_active={"messagebox-mode-reconcile.path"} | (
                {"messagebox-wifi-watchdog.timer"} if not _rooted(root, MODE_MARKER).exists() else set())
        )
        if not _rooted(root, MODE_MARKER).exists():
            _run(["systemctl", "--job-mode=ignore-dependencies", "start", "messagebox-wifi-watchdog.timer"], run, check=True)
        _restore_auxiliary_active(auxiliary_units, run)
        _verify_active(
            states, run, also_active={"messagebox-mode-reconcile.path"} | (
                {"messagebox-wifi-watchdog.timer"} if not _rooted(root, MODE_MARKER).exists() else set()),
            auxiliary_units=auxiliary_units,
        )
    except BaseException as update_error:
        try:
            _rollback_locked(backup_dir, root=root, run=run)
        except BaseException as rollback_error:
            raise UpdateError(
                f"update failed ({_failure_cause(update_error)}); "
                f"automatic rollback also failed: {_failure_cause(rollback_error)}"
            ) from update_error
        raise UpdateError(
            f"update failed ({_failure_cause(update_error)}); the recorded state was restored"
        ) from update_error
    return manifest


def apply(source_root, manifest_path, backup_dir, *, root=Path("/"), run=subprocess.run):
    root = Path(root).resolve()
    if root == Path("/") and os.geteuid() != 0:
        raise UpdateError("bounded update requires root")
    if _rooted(root, "/opt/messagebox").is_symlink():
        raise UpdateError("converted installations require a signed transaction")
    with _update_lock(root):
        return _apply_locked(
            source_root, manifest_path, backup_dir, root=root, run=run
        )


# The fixed controller installs this module outside /opt/messagebox. A candidate
# cannot replace its own recovery code or the executing installer.
UPDATE_STATE = '/var/lib/button-box-update'
RELEASES = '/opt/messagebox-releases'
STABLE_NFC = '/opt/messagebox-deps/venv-nfc'
INHIBIT = UPDATE_STATE + '/install-inhibit'
MODE_LOCK = '/run/lock/messagebox-mode-transition.lock'
MAINTENANCE_LOCK = '/run/messagebox-update/maintenance.lock'
HEALTH_DEADLINE = 300
HEALTH_STABLE = 60
HEALTH_UNREADY = 20
HEALTH_FAILURE_LIMIT = 32
HEALTH_CODE_LIMIT = 160
FIXED_UNITS = {
    'messagebox-update-recovery.service', 'messagebox-update-controller.service',
    'messagebox-update-install.service', 'messagebox-update-supervisor.service',
    'messagebox-update-supervisor.timer', 'messagebox-update-discovery.service',
    'messagebox-update-discovery.timer',
}


def durable_json(path, document, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.' + path.name, delete=False) as output:
        temporary = Path(output.name)
        os.fchmod(output.fileno(), mode)
        json.dump(document, output, sort_keys=True)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_state(root, name, default=None):
    path = _rooted(root, UPDATE_STATE + '/' + name)
    try:
        _check_parents(path, root)
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid != (0 if Path(root) == Path('/') else os.geteuid())
                or metadata.st_mode & 0o022 or metadata.st_size > 2 * 1024 * 1024):
            raise UpdateError('unsafe updater state')
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise UpdateError('invalid updater state') from exc


def save_state(root, name, document, mode=0o600):
    path = _rooted(root, UPDATE_STATE + '/' + name)
    _check_parents(path, root)
    durable_json(path, document, mode)


def controlled_release(root):
    pointer = _rooted(root, '/opt/messagebox')
    metadata = pointer.lstat()
    trusted = 0 if Path(root) == Path('/') else os.geteuid()
    target = os.readlink(pointer) if stat.S_ISLNK(metadata.st_mode) else ''
    pure = PurePosixPath(target)
    if (metadata.st_uid != trusted or pure.parent != PurePosixPath(RELEASES)
            or not re.fullmatch(r'[A-Za-z0-9._-]+-[0-9a-f]{40}', pure.name)):
        raise UpdateError('unsafe application pointer')
    directory = _rooted(root, target)
    _check_parents(directory, root)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != trusted or info.st_mode & 0o022:
        raise UpdateError('unsafe release directory')
    return target


def switch_release(root, target):
    pure = PurePosixPath(target)
    if (pure.parent != PurePosixPath(RELEASES)
            or not re.fullmatch(r'[A-Za-z0-9._-]+-[0-9a-f]{40}', pure.name)):
        raise UpdateError('unsafe application pointer')
    directory = _rooted(root, target)
    _check_parents(directory, root)
    if not directory.is_dir() or directory.is_symlink():
        raise UpdateError('release directory unavailable')
    pointer = _rooted(root, '/opt/messagebox')
    _check_parents(pointer, root)
    temporary = pointer.with_name('.messagebox-switch')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, pointer)
    _fsync_directory(pointer.parent)


def _inhibit(root, enabled):
    path = _rooted(root, INHIBIT)
    if enabled:
        durable_json(path, {'inhibited': True}, 0o644)
    else:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)


@contextlib.contextmanager
def transaction_locks(root, *, maintenance=True):
    # Global order: update -> mode transition -> audio/interaction reservation.
    # Reconciliation rechecks inhibit after acquiring the mode lock.
    with _update_lock(root):
        descriptors = []
        try:
            for absolute, mode in ((MODE_LOCK, 0o600), (MAINTENANCE_LOCK, 0o644)):
                if not maintenance and absolute == MAINTENANCE_LOCK:
                    continue
                path = _rooted(root, absolute)
                _check_parents(path, root)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, mode)
                descriptors.append(descriptor)
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != (0 if Path(root) == Path('/') else os.geteuid())
                        or stat.S_IMODE(info.st_mode) != mode):
                    raise UpdateError('unsafe transaction lock')
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise UpdateError('not_idle') from exc
            yield descriptors
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


def _journal(root, journal, phase):
    journal['phase'] = phase
    save_state(root, 'journal.json', journal)


def _result(root, journal, state, reason):
    identity = {}
    try:
        pointer = controlled_release(root)
        metadata = json.loads((_rooted(root, pointer) / 'release.json').read_bytes())
        identity = {'software_version': metadata['version'], 'software_commit': metadata['commit']}
    except (OSError, ValueError, KeyError, UpdateError):
        pass
    detail = journal.get('detail', reason) if state in {'rolled_back', 'rollback_failed'} else None
    save_state(root, 'result.json', {
        'operation_id': journal.get('operation_id'), 'version': journal.get('version'),
        'state': state, 'reason': reason,
        **({'detail': detail[:HEALTH_CODE_LIMIT]} if detail else {}), **identity,
    }, 0o644)


def _external_restore(root, journal, run, *, start):
    backup = _rooted(root, journal['backup'])
    state = json.loads((backup / 'state.json').read_bytes())
    records = _validate_backup_state(state, backup, root)
    for record, path in records:
        _restore_record(record, path, backup)
    _run(['systemctl', 'daemon-reload'], run, check=True, timeout=30)
    _restore_enabled(state['units'], run)
    _inhibit(root, False)
    if start:
        _reset_limited_activators(run, [*UNITS, *state.get('auxiliary_units', ())])
        _restore_active(state['units'], run)
        _restore_auxiliary_active(state.get('auxiliary_units', ()), run)
        _verify_enabled(state['units'], run, root=root)
        _verify_active(state['units'], run, auxiliary_units=state.get('auxiliary_units', ()))


def _validate_journal(root, journal):
    if (not isinstance(journal, dict) or journal.get('format') != 1
            or journal.get('phase') not in {'prepared', 'quiesced', 'switched', 'probation',
                'restoring_activators', 'committed', 'rolling_back', 'rolled_back', 'rollback_failed'}
            or journal.get('kind') not in {'application', 'bootstrap'}):
        raise UpdateError('invalid update journal')
    for key in ('old', 'new'):
        pure = PurePosixPath(journal.get(key, ''))
        if (pure.parent != PurePosixPath(RELEASES)
                or not re.fullmatch(r'[A-Za-z0-9._-]+-[0-9a-f]{40}', pure.name)):
            raise UpdateError('invalid journal pointer')
    pure = PurePosixPath(journal.get('backup', ''))
    if pure.parent != PurePosixPath(UPDATE_STATE + '/snapshots') or not re.fullmatch(r'[0-9a-f]{32}', pure.name):
        raise UpdateError('invalid journal snapshot')
    return journal


def _recover_locked(root, run, *, start=True, reason='interrupted'):
    journal = read_state(root, 'journal.json')
    if journal is None:
        prune_releases(root)
        return None
    _validate_journal(root, journal)
    if journal['phase'] in {'committed', 'rolled_back'}:
        if journal['phase'] == 'committed' and journal['kind'] == 'application':
            if controlled_release(root) != journal['new']:
                raise UpdateError('committed_pointer_mismatch')
            save_state(root, 'known-good.json', {'current': journal['new'], 'previous': journal['old']})
            _result(root, journal, 'installed', 'healthy')
        prune_releases(root)
        return journal
    # Persist the initiating cause before restoration so recovery after a cut
    # cannot replace it with the generic interruption reason.
    monitor = read_state(root, 'supervisor.json', {})
    if monitor.get('transaction') == journal.get('transaction'):
        reason = monitor.get('rollback_reason', reason)
        if monitor.get('last_failures'):
            journal['detail'] = monitor['last_failures'][0][:HEALTH_CODE_LIMIT]
    reason = journal.setdefault('rollback_reason', reason)
    _journal(root, journal, 'rolling_back')
    _inhibit(root, True)
    try:
        current = _unit_states(run, allow_transitional=True, deadline=time.monotonic() + 30)
        _stop_active(current, run, auxiliary_units=journal.get('auxiliary_units', ()))
        if journal['kind'] == 'bootstrap':
            pointer = _rooted(root, '/opt/messagebox')
            old = _rooted(root, journal['old'])
            if pointer.is_symlink():
                pointer.unlink()
            if old.exists() and not pointer.exists():
                nfc = old / 'venv-nfc'
                if not nfc.exists() and not nfc.is_symlink() and _rooted(root, STABLE_NFC).exists():
                    nfc.symlink_to(STABLE_NFC)
                    _fsync_directory(old)
                os.replace(old, pointer)
                _fsync_directory(pointer.parent)
        else:
            switch_release(root, journal['old'])
        _external_restore(root, journal, run, start=start)
        quarantine = read_state(root, 'quarantine.json', [])
        if journal['kind'] == 'application' and journal['new'] not in quarantine:
            quarantine.append(journal['new'])
            save_state(root, 'quarantine.json', quarantine)
        _result(root, journal, 'rolled_back', reason)
        _journal(root, journal, 'rolled_back')
        if journal['kind'] == 'application':
            known = read_state(root, 'known-good.json', {})
            save_state(root, 'known-good.json', {'current': journal['old'], 'previous': journal.get('previous_good', known.get('previous'))})
        prune_releases(root)
    except Exception as exc:
        _inhibit(root, True)
        # The result keeps the initiating health code; the root-only journal
        # also names the restoration step that failed (updater text only).
        journal['restore_error'] = _failure_cause(exc)[:HEALTH_CODE_LIMIT]
        _result(root, journal, 'rollback_failed', 'restoration_failed')
        _journal(root, journal, 'rollback_failed')
        raise UpdateError('rollback_failed') from exc
    return journal


def recover_transaction(*, root=Path('/'), run=subprocess.run, start=True, reason='interrupted'):
    root = Path(root).resolve()
    with transaction_locks(root, maintenance=False):
        return _recover_locked(root, run, start=start, reason=reason)


def _sync_tree(directory):
    for path in sorted(directory.rglob('*'), reverse=True):
        if path.is_symlink():
            continue
        if path.is_dir():
            _fsync_directory(path)
        else:
            with path.open('rb') as file:
                os.fsync(file.fileno())
    _fsync_directory(directory)
    _fsync_directory(directory.parent)


def _fifo_held(path, *, proc=Path('/proc')):
    """Fail closed if we cannot inspect a process; disappearing FDs are normal."""
    identity = path.lstat()
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            for descriptor in (process / 'fd').iterdir():
                try:
                    link = os.readlink(descriptor)
                    if link == str(path):
                        return True
                    # An open descriptor may refer to the FIFO via another path.
                    observed = descriptor.stat()
                    if (observed.st_dev, observed.st_ino) == (identity.st_dev, identity.st_ino):
                        return True
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            continue
    return False


def _remove_unused_lgpio_fifos(pointer):
    for path in pointer.glob('.lgd-nfy*'):
        info = path.lstat()
        if stat.S_ISFIFO(info.st_mode):
            try:
                held = _fifo_held(path)
            except OSError as exc:
                raise UpdateError('unsafe_legacy_tree') from exc
            if not held:
                # Recheck identity before unlinking; never remove a replacement.
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise UpdateError('unsafe_legacy_tree')
                path.unlink()
                _fsync_directory(pointer)


def bootstrap_layout(*, root=Path('/'), run=subprocess.run):
    import uuid
    root = Path(root).resolve()
    with transaction_locks(root):
        pointer = _rooted(root, '/opt/messagebox')
        if pointer.is_symlink():
            return controlled_release(root)
        pending = read_state(root, 'journal.json')
        if pending and pending['phase'] not in {'committed', 'rolled_back'}:
            raise UpdateError('recovery_required')
        metadata = json.loads((pointer / 'release.json').read_bytes())
        version, commit = metadata['version'], metadata['commit']
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', version) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise UpdateError('installed_identity_required')
        old = RELEASES + '/' + version + '-' + commit
        releases = _rooted(root, RELEASES)
        _check_parents(releases, root)
        releases.mkdir(parents=True, exist_ok=True, mode=0o755)
        if _rooted(root, old).exists():
            raise UpdateError('release_exists')
        _remove_unused_lgpio_fifos(pointer)
        # Legacy venvs may contain Python symlinks; only this dependency subtree
        # is exempt from the no-symlink rule. App trees must be root-owned.
        for path in (pointer, *pointer.rglob('*')):
            if 'venv-nfc' in path.relative_to(pointer).parts:
                continue
            info = path.lstat()
            if (info.st_uid != (0 if root == Path('/') else os.geteuid()) or info.st_mode & 0o022
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
                raise UpdateError('unsafe_legacy_tree')
        states, _auxiliary = _settled_unit_scan(run, auxiliary=False)
        backup = UPDATE_STATE + '/snapshots/' + uuid.uuid4().hex
        create_backup(_rooted(root, backup), root, [], states, external_only=True)
        journal = {'format': 1, 'kind': 'bootstrap', 'old': old, 'new': old,
                   'backup': backup, 'version': version, 'operation_id': None}
        _journal(root, journal, 'prepared')
        try:
            _inhibit(root, True)
            _stop_active(states, run)
            os.replace(pointer, _rooted(root, old))
            _fsync_directory(pointer.parent)
            _fsync_directory(releases)
            nfc = _rooted(root, old) / 'venv-nfc'
            stable = _rooted(root, STABLE_NFC)
            stable.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if nfc.exists() and not nfc.is_symlink():
                if stable.exists():
                    raise UpdateError('stable_dependency_exists')
                os.replace(nfc, stable)
                _fsync_directory(stable.parent)
            if not nfc.exists() and not nfc.is_symlink() and stable.exists():
                nfc.symlink_to(STABLE_NFC)
                _fsync_directory(nfc.parent)
            switch_release(root, old)
            _journal(root, journal, 'switched')
            _inhibit(root, False)
            _restore_active(states, run)
            _verify_active(states, run)
            _journal(root, journal, 'committed')
            save_state(root, 'known-good.json', {'current': old, 'previous': None})
            prune_releases(root)
        except Exception:
            _recover_locked(root, run, reason='bootstrap_failed')
            raise
        return old


def _process_token(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _boot():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def health_checks(root, journal, run=subprocess.run):
    """Named booleans only: no evidence values, command output or audio capture."""
    checks, pids = [], {}

    def check(name, passed):
        checks.append((name[:HEALTH_CODE_LIMIT], bool(passed)))

    expected_units = {**journal['units'], **{unit: {'active': 'active'} for unit in journal.get('auxiliary_units', [])}}
    setup = journal.get('mode') == 'setup'
    for unit, state in expected_units.items():
        expected = state['active']
        if journal.get('activators_suspended') and unit.endswith(('.path', '.timer')):
            expected = 'inactive' if expected != 'failed' else 'failed'
        # Only fixed/validated unit names become diagnostic codes.
        label = unit if unit in UNITS or _valid_auxiliary_unit(unit) else 'unknown'
        values = {}
        try:
            result = _run(['systemctl', 'show', unit, '--property=ActiveState,MainPID,NRestarts'],
                          run, capture_output=True, text=True, check=True, timeout=5)
            values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        except (OSError, UpdateError, subprocess.SubprocessError):
            pass
        active = values.get('ActiveState')
        code = active if active in {'active', 'inactive', 'failed', 'activating', 'deactivating', 'reloading'} else 'unknown'
        if unit == 'comitup-web.service' and (state['active'] != 'active'
                or journal['units'].get('comitup.service', {}).get('active') == 'active'):
            passed = active in {'active', 'inactive', 'failed'}
        elif expected == 'failed':
            # Not running before the update (for example a rate-limited old
            # watcher); the candidate need not reproduce that failure.
            passed = active in {'inactive', 'failed'}
        else:
            passed = active == expected
        check('unit:' + label + ':' + code, passed)
        if expected == 'active' and unit in {'messagebox-button.service', 'messagebox-poller.service',
                'messagebox-dash.service', 'messagebox-nfc.service', 'messagebox-sync.service'}:
            pids[unit] = (values.get('MainPID'), values.get('NRestarts'))
            check('mainpid:' + label, values.get('MainPID') not in {None, '0'})
    if setup:
        # Setup mode at transaction start: the household application is not
        # running, so it cannot author evidence. The recorded setup units above
        # (ComItUp, setup portal and guided button) carry readiness instead,
        # and the box must still be in setup mode.
        check('setup:marker', _rooted(root, MODE_MARKER).exists())
    for name, producer in (('heartbeat', 'messagebox-poller.service'), ('button', 'messagebox-button.service')):
        recorded = journal['units'].get(producer)
        # Evidence is required unless setup mode was recorded, or the producing
        # unit was neither running nor enabled (an unclaimed Cloud box). A
        # crashed but selected unit still has to prove itself.
        if setup or (recorded is not None and recorded.get('active') != 'active'
                     and recorded.get('enabled') not in ENABLED_STATES | {'enabled-runtime'}):
            continue
        try:
            evidence = json.loads(_rooted(root, '/run/messagebox-update/' + name + '.json').read_bytes())
            if not isinstance(evidence, dict):
                raise ValueError('invalid evidence')
        except (OSError, ValueError):
            evidence = {}
        for field, key in (('transaction', 'transaction'), ('boot', 'boot'),
                           ('software_version', 'version'), ('software_commit', 'commit')):
            # --current is identity/boot-bound, without a pending transaction.
            if key in journal:
                check('evidence:' + name + ':' + field, evidence.get(field) == journal[key])
        observed = evidence.get('monotonic')
        # Read the clock after the evidence file: the button refreshes it every
        # second, so a clock read earlier in this sample makes it look future-dated.
        checked_at = time.monotonic()
        valid_time = type(observed) in {int, float} and journal['started_mono'] <= observed <= checked_at
        check('evidence:' + name + ':monotonic', valid_time)
        if name == 'heartbeat':
            check('evidence:heartbeat:fresh', valid_time and checked_at - observed <= 90)
        else:
            check('evidence:button:pid', str(evidence.get('pid')) == pids.get('messagebox-button.service', (None,))[0])
        check('evidence:' + name + ':ready', evidence.get('ready') is True)
    try:
        values = dict(line.split('=', 1) for line in _rooted(root, '/run/messagebox-audio/audio.env').read_text().splitlines() if '=' in line)
    except (OSError, ValueError):
        values = {}
    for field, tool, suffix in (('MSGBOX_MIC_DEV', 'arecord', 'c'), ('MSGBOX_SPK_DEV', 'aplay', 'p')):
        device = re.fullmatch(r'(?:plug)?hw:CARD=([A-Za-z0-9_-]+),DEV=([0-9]+)', values.get(field, ''))
        check('audio:' + field + ':configured', device is not None)
        try:
            listing = _run(['runuser', '-u', 'messagebox', '--', tool, '-l'], run,
                           capture_output=True, text=True, check=False, timeout=5)
            match = None if not device else re.search(r'card ([0-9]+): ' + re.escape(device[1])
                + r' \[.*?\], device ' + re.escape(device[2]) + r':', listing.stdout)
            check('audio:' + field + ':enumerated', listing.returncode == 0 and match is not None)
            if match:
                node = '/dev/snd/' + f'pcmC{match[1]}D{device[2]}' + suffix
                for access, label in (('-r', 'readable'), ('-w', 'writable')):
                    observed = _run(['runuser', '-u', 'messagebox', '--', 'test', access, node],
                                    run, check=False, timeout=5)
                    check('audio:' + field + ':' + label, observed.returncode == 0)
        except (OSError, UpdateError, subprocess.SubprocessError):
            check('audio:' + field + ':probe', False)
    try:
        pointer = controlled_release(root)
        check('pointer', pointer == journal['new'])
    except (OSError, UpdateError):
        pointer = None
        check('pointer', False)
    for entry in journal['hashes']:
        relative = entry['relative']
        # Journal paths are program paths, never household paths; still reject
        # traversal and bound names before using them in diagnostics or reads.
        safe = (isinstance(relative, str) and len(relative) <= 150
                and re.fullmatch(r'[A-Za-z0-9_./-]+', relative)
                and not PurePosixPath(relative).is_absolute() and '..' not in PurePosixPath(relative).parts)
        passed = False
        if pointer and safe:
            try:
                path = _rooted(root, pointer) / relative
                passed = path.is_file() and _sha256(path) == entry['sha256']
            except OSError:
                pass
        check('hash:' + (relative if safe else 'invalid'), passed)
    return checks, pids


def health_sample(root, journal, run=subprocess.run, *, failures=None):
    checks, pids = health_checks(root, journal, run)
    if failures is not None:
        failures.extend(name for name, passed in checks if not passed)
        del failures[HEALTH_FAILURE_LIMIT:]
    return all(passed for _name, passed in checks), pids


def sustain_health(root, journal, run=subprocess.run, *, sample=health_sample,
                   monotonic=time.monotonic, sleep=time.sleep):
    stable, prior = None, {}
    restarts = {}
    while monotonic() < journal['deadline_mono']:
        ready, pids = sample(root, journal, run)
        now = monotonic()
        changed = False
        for unit, identity in pids.items():
            if unit in prior and prior[unit] != identity:
                changed = True
                try:
                    count = max(1, int(identity[1]) - int(prior[unit][1]))
                except (TypeError, ValueError):
                    count = 1
                restarts[unit] = [at for at in restarts.get(unit, []) if now - at <= 60] + [now] * min(3, count)
                if len(restarts[unit]) >= 3:
                    raise UpdateError('crash_loop')
            prior[unit] = identity
        if not ready or changed:
            stable = None
        elif stable is None:
            stable = now
        elif now - stable >= HEALTH_STABLE and now < journal['deadline_mono']:
            return
        sleep(min(2, max(0, journal['deadline_mono'] - now)))
    raise UpdateError('health_timeout')


def install_transaction(source_root, manifest_path, *, operation_id=None, root=Path('/'),
                        run=subprocess.run, health=sustain_health, sequence=None, allow_setup=False):
    import uuid
    root = Path(root).resolve()
    with transaction_locks(root) as locks:
        pending = read_state(root, 'journal.json')
        if pending and pending['phase'] not in {'committed', 'rolled_back'}:
            raise UpdateError('recovery_required')
        # Only an attended install may run in setup mode. The marker stays in
        # place: the snapshot, health and rollback all describe setup mode.
        setup = _rooted(root, MODE_MARKER).exists()
        if setup and not allow_setup:
            raise UpdateError('onboarding_active')
        old = controlled_release(root)
        manifest, entries, manifest_hash = load_candidate(source_root, manifest_path, root)
        new = RELEASES + '/' + manifest['version'] + '-' + manifest['commit']
        if old == new:
            return manifest
        if new in read_state(root, 'quarantine.json', []):
            raise UpdateError('quarantined')
        if sequence is None or type(sequence) is not int or sequence <= read_state(root, 'sequence.json', 0):
            raise UpdateError('downgrade')
        candidate = _rooted(root, new)
        if candidate.exists():
            raise UpdateError('release_exists')
        external = [entry for entry in entries if not entry['target'].startswith('/opt/messagebox/')]
        for entry in entries:
            frozen = entry['target'].endswith('/requirements-nfc.txt') or Path(entry['target']).name in FIXED_UNITS
            if frozen and (not entry['destination'].is_file() or _sha256(entry['destination']) != entry['sha256']):
                raise UpdateError('maintenance_release_required')
        size = sum(entry['source_path'].stat().st_size for entry in entries)
        if shutil.disk_usage(_rooted(root, RELEASES)).free < size * 2 + 64 * 1024 * 1024:
            raise UpdateError('low_disk')
        candidate.mkdir(mode=0o755)
        hashes = []
        try:
            for entry in entries:
                if entry in external:
                    continue
                relative = entry['target'].removeprefix('/opt/messagebox/')
                target = candidate / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                _atomic_install(entry['source_path'], target, entry['mode'])
                hashes.append({'relative': relative, 'sha256': entry['sha256']})
            for name in ('bounded_update', 'release_verify', 'trust_update', 'update_controller', 'update_discovery', 'health-probe'):
                source = Path(source_root) / 'scripts/install' / (name + '.py')
                _validate_staged_file(source, Path(source_root), 0 if root == Path('/') else os.geteuid())
                try:
                    compile(source.read_bytes(), name + '.py', 'exec')
                except SyntaxError as exc:
                    raise UpdateError('invalid_candidate_controller') from exc
                target = candidate / 'updater' / (name + '.py')
                target.parent.mkdir(exist_ok=True, mode=0o755)
                _atomic_install(source, target, 0o644)
                hashes.append({'relative': 'updater/' + name + '.py', 'sha256': _sha256(target)})
            durable_json(candidate / 'release.json', {'version': manifest['version'], 'commit': manifest['commit'],
                         'manifest_sha256': manifest_hash}, 0o644)
            (candidate / 'VERSION').write_text(manifest['version'] + '\n')
            (candidate / 'VERSION').chmod(0o644)
            hashes.extend({'relative': name, 'sha256': _sha256(candidate / name)} for name in ('release.json', 'VERSION'))
            shutil.copyfile(manifest_path, candidate / 'release-manifest.json')
            (candidate / 'release-manifest.json').chmod(0o644)
            (candidate / 'venv-nfc').symlink_to(STABLE_NFC)
            _sync_tree(candidate)
            states, auxiliary = _settled_unit_scan(run)
            if setup and states['comitup.service']['active'] != 'active':
                # Setup readiness could never be proven; refuse before mutation.
                raise UpdateError('setup_not_ready')
            backup = UPDATE_STATE + '/snapshots/' + uuid.uuid4().hex
            create_backup(_rooted(root, backup), root, external, states, auxiliary, external_only=True)
        except Exception:
            shutil.rmtree(candidate)
            raise
        transaction = uuid.uuid4().hex
        journal = {'format': 1, 'kind': 'application', 'transaction': transaction,
                   'operation_id': operation_id, 'version': manifest['version'], 'commit': manifest['commit'],
                   'old': old, 'new': new, 'backup': backup, 'units': states, 'hashes': hashes,
                   'auxiliary_units': auxiliary, 'activators_suspended': True,
                   'mode': 'setup' if setup else 'runtime',
                   'boot': _boot(), 'started_mono': time.monotonic(),
                   'deadline_mono': time.monotonic() + HEALTH_DEADLINE,
                   'pid': os.getpid(), 'process_token': _process_token(os.getpid()),
                   'previous_good': read_state(root, 'known-good.json', {}).get('previous')}
        _journal(root, journal, 'prepared')
        # Advance outside application rollback. A failed signed candidate still
        # cannot allow a network downgrade; recovery permits only journal.old.
        save_state(root, 'sequence.json', sequence)
        try:
            _result(root, journal, 'installing', 'none')
            _inhibit(root, True)
            _stop_active(states, run, auxiliary_units=auxiliary)
            _journal(root, journal, 'quiesced')
            for entry in external:
                if Path(entry['target']).name not in FIXED_UNITS:
                    _atomic_install(entry['source_path'], entry['destination'], entry['mode'])
            switch_release(root, new)
            _journal(root, journal, 'switched')
            _run(['systemctl', 'daemon-reload'], run, check=True, timeout=30)
            # Evidence is authored only by the current systemd main processes,
            # through the independent controller socket; old files cannot pass.
            durable_json(_rooted(root, '/run/messagebox-update/transaction.json'), {
                'transaction': transaction, 'boot': journal['boot'], 'software_version': manifest['version'],
                'software_commit': manifest['commit']}, 0o644)
            _inhibit(root, False)
            # Reopen application interactions for probation; the update and mode
            # locks still prevent another installation or reconciliation.
            fcntl.flock(locks[-1], fcntl.LOCK_UN)
            activators = {unit for unit in (*UNITS, *auxiliary) if unit.endswith(('.path', '.timer'))}
            _restore_active(states, run, skip=activators)
            _restore_auxiliary_active(auxiliary, run, skip=activators)
            _journal(root, journal, 'probation')
            health(root, journal, run)
            _journal(root, journal, 'restoring_activators')
            _restore_active(states, run, skip=set(UNITS) - activators)
            _restore_auxiliary_active(auxiliary, run, skip=set(auxiliary) - activators)
            for unit in activators:
                if unit in states and states[unit]['active'] != 'active':
                    continue
                observed = _run(['systemctl', 'is-active', unit], run, capture_output=True, text=True, check=False, timeout=5)
                if observed.stdout.strip() != 'active':
                    raise UpdateError('activator_restore_failed')
            # An enabled activator that was rate-limited under the old release
            # gets one fresh start under the healthy candidate, best effort.
            retry = [unit for unit in activators if unit in states
                     and states[unit]['active'] == 'failed' and states[unit]['enabled'] in ENABLED_STATES]
            if retry:
                _run(['systemctl', 'reset-failed', *retry], run, check=False, timeout=30)
                for unit in retry:
                    _run(['systemctl', '--job-mode=ignore-dependencies', 'start', unit], run, check=False, timeout=30)
            journal['activators_suspended'] = False
            _journal(root, journal, 'committed')
            _result(root, journal, 'installed', 'healthy')
            save_state(root, 'known-good.json', {'current': new, 'previous': old})
            prune_releases(root)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, UpdateError) and str(exc) in {'health_timeout', 'crash_loop'} else 'install_failed'
            _recover_locked(root, run, reason=reason)
            raise UpdateError(reason) from exc
        return manifest


def prune_releases(root):
    journal = read_state(root, 'journal.json')
    keep = set()
    if journal and journal['phase'] not in {'committed', 'rolled_back'}:
        keep.update((journal['old'], journal['new']))
        return  # Never prune snapshots or staging during an unfinished journal.
    known = read_state(root, 'known-good.json', {})
    keep.update(value for value in known.values() if value)
    try:
        keep.add(controlled_release(root))
    except (FileNotFoundError, UpdateError):
        pass
    releases = _rooted(root, RELEASES)
    if releases.exists():
        for child in releases.iterdir():
            if (RELEASES + '/' + child.name not in keep and child.is_dir() and not child.is_symlink()
                    and re.fullmatch(r'[A-Za-z0-9._-]+-[0-9a-f]{40}', child.name)):
                shutil.rmtree(child)
        _fsync_directory(releases)
    available = read_state(root, 'available.json', {})
    if journal and journal.get('kind') == 'application' and available.get('version') == journal.get('version'):
        save_state(root, 'available.json', {})
        available = {}
    downloads = _rooted(root, UPDATE_STATE + '/downloads')
    if downloads.exists():
        for child in downloads.iterdir():
            if str(child) != available.get('bundle') and child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
        _fsync_directory(downloads)
    for name in ('snapshots', 'staging'):
        directory = _rooted(root, UPDATE_STATE + '/' + name)
        if directory.exists():
            for child in directory.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
            _fsync_directory(directory)


def supervise_transaction(*, root=Path('/'), run=subprocess.run):
    journal = read_state(root, 'journal.json')
    if not journal or journal['phase'] in {'committed', 'rolled_back', 'rollback_failed'}:
        ticket = read_state(root, 'ticket.json')
        if ticket and isinstance(ticket.get('operation_id'), str):
            name = 'operations/' + hashlib.sha256(ticket['operation_id'].encode()).hexdigest() + '.json'
            operation = read_state(root, name, {})
            if operation.get('state') == 'installing':
                result = read_state(root, 'result.json', {})
                if (result.get('operation_id') == ticket['operation_id']
                        and result.get('state') in {'installed', 'rolled_back', 'rollback_failed'}):
                    operation.update({key: result[key] for key in
                                      ('state', 'reason', 'detail', 'software_version', 'software_commit') if key in result})
                    save_state(root, name, operation)
                    return
                old_boot = ticket.get('boot') != _boot()
                if old_boot or time.monotonic() - ticket.get('queued_mono', time.monotonic()) >= 30:
                    observed = _run(['systemctl', 'is-active', 'messagebox-update-install.service'],
                                    run, check=False, capture_output=True, text=True, timeout=5)
                    if old_boot or observed.stdout.strip() in {'failed', 'inactive'}:
                        _result(root, ticket, 'failed', 'updater_died')
                        operation.update(state='failed', reason='updater_died')
                        save_state(root, name, operation)
        return
    _validate_journal(root, journal)
    reason = None
    if (journal.get('boot') != _boot() or _process_token(journal.get('pid')) is None
            or _process_token(journal.get('pid')) != journal.get('process_token')):
        reason = 'updater_died'
    elif time.monotonic() >= journal['deadline_mono']:
        reason = 'health_timeout'
    if not reason and journal['phase'] == 'probation':
        try:
            failures = []
            ready, pids = health_sample(Path(root), journal, run, failures=failures)
            monitor = read_state(root, 'supervisor.json', {})
            if monitor.get('transaction') != journal['transaction']:
                monitor = {'transaction': journal['transaction'], 'pids': {}, 'restarts': {}, 'was_ready': False}
            now = time.monotonic()
            for unit, identity in pids.items():
                previous = monitor['pids'].get(unit)
                if previous and list(identity) != previous:
                    try:
                        count = max(1, int(identity[1]) - int(previous[1]))
                    except (TypeError, ValueError):
                        count = 1
                    history = [at for at in monitor['restarts'].get(unit, []) if now - at <= 60] + [now] * min(3, count)
                    monitor['restarts'][unit] = history
                    if len(history) >= 3:
                        reason = 'crash_loop'
                monitor['pids'][unit] = list(identity)
            if ready:
                monitor.pop('first_unready_mono', None)
            else:
                monitor['last_failures'] = failures
                first = monitor.setdefault('first_unready_mono', now)
                if monitor['was_ready'] and now - first >= HEALTH_UNREADY:
                    reason = reason or 'readiness_lost'
            monitor['was_ready'] |= ready
            save_state(root, 'supervisor.json', monitor)
        except Exception:
            reason = 'health_probe_failed'
    if reason:
        monitor = read_state(root, 'supervisor.json', {})
        if monitor.get('transaction') != journal.get('transaction'):
            monitor = {'transaction': journal.get('transaction')}
        monitor['rollback_reason'] = reason
        save_state(root, 'supervisor.json', monitor)
        # Stop the independent worker before acquiring its locks. The broker,
        # supervisor and recovery never belong to the application target.
        _run(['systemctl', 'stop', 'messagebox-update-install.service'], run, check=True, timeout=30)
        recover_transaction(root=root, run=run, reason=reason)


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    apply_parser = subparsers.add_parser("apply", help="apply a checked release manifest")
    apply_parser.add_argument("--source-root", required=True)
    apply_parser.add_argument("--manifest", required=True)
    apply_parser.add_argument("--backup-dir", required=True)
    rollback_parser = subparsers.add_parser("rollback", help="restore a bounded-update backup")
    rollback_parser.add_argument("--backup-dir", required=True)
    subparsers.add_parser("bootstrap", help="convert an attended in-place installation")
    recovery = subparsers.add_parser("recover", help="recover an interrupted transaction")
    recovery.add_argument("--boot", action="store_true")
    subparsers.add_parser("supervise", help="enforce the independent transaction deadline")
    options = parser.parse_args(arguments)
    try:
        if options.command == "apply":
            manifest = apply(
                options.source_root,
                options.manifest,
                options.backup_dir,
                root=options.root,
            )
            print(f"Installed {manifest['version']} ({manifest['commit']}).")
        elif options.command == "bootstrap":
            bootstrap_layout(root=options.root)
            print("Converted the installed application layout.")
        elif options.command == "recover":
            recover_transaction(root=options.root, start=not options.boot)
        elif options.command == "supervise":
            supervise_transaction(root=options.root)
        else:
            rollback(options.backup_dir, root=options.root)
            print("Restored the recorded pre-update state.")
    except UpdateError as exc:
        parser.exit(1, f"bounded update: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
