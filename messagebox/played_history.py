"""Durable, bounded history for successfully played inbound audio."""

from __future__ import annotations

import fcntl
import json
import math
import os
import secrets
import shutil
import time
import wave
from contextlib import contextmanager
from pathlib import Path

from messagebox.contacts import ContactError, validate_contact


METADATA_LIMIT = 20
MEDIA_LIMIT = 10
RETENTION_SECONDS = 14 * 86400
MEDIA_BYTES_LIMIT = 128 * 1024 * 1024


def played_dir(queue_dir: str | Path) -> Path:
    return Path(queue_dir) / ".played"


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError("played message name is invalid")
    if not name.endswith(".wav") or name.startswith("."):
        raise ValueError("played message name is invalid")
    return name


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".part")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _wav_duration(path: Path) -> float | None:
    try:
        with wave.open(os.fspath(path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
    except (OSError, EOFError, ValueError, wave.Error, ZeroDivisionError):
        return None
    return duration if math.isfinite(duration) and duration >= 0 else None


@contextmanager
def _history_lock(queue: Path):
    queue.mkdir(parents=True, exist_ok=True)
    lock_path = queue / ".played.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _record_time(metadata_path: Path, metadata: dict) -> float:
    value = metadata.get("played_at")
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    try:
        return metadata_path.stat().st_mtime
    except OSError:
        return 0


def _has_reply_route(metadata: dict) -> bool:
    try:
        contact = validate_contact(metadata.get("chat"), "Original reply route")
    except ContactError:
        return False
    return contact["jid"] == metadata.get("chat")


def _replay_name(metadata: dict) -> str | None:
    value = metadata.get("replay_name")
    try:
        return _safe_name(value)
    except ValueError:
        return None


def _active_replay(queue: Path, metadata: dict) -> bool:
    name = _replay_name(metadata)
    return bool(
        name
        and any(
            (directory / name).is_file()
            for directory in (queue, queue / ".inflight", queue / ".hold")
        )
    )


def _allocate_queue_name(queue: Path, now: float) -> str:
    largest_prefix = 0
    for directory in (queue, queue / ".inflight", queue / ".hold"):
        try:
            names = (path.name for path in directory.iterdir() if path.suffix == ".wav")
            for name in names:
                prefix = name.split("-", 1)[0]
                if prefix.isdigit():
                    largest_prefix = max(largest_prefix, int(prefix))
        except FileNotFoundError:
            continue
    prefix = max(int(now * 1000), largest_prefix + 1)
    return f"{prefix}-replay-{secrets.token_hex(8)}.wav"


def _require_retained(
    directory: Path,
    name: str,
    metadata: dict,
    *,
    now: float,
) -> Path:
    metadata_path = Path(f"{directory / name}.json")
    if not metadata or _record_time(metadata_path, metadata) < now - RETENTION_SECONDS:
        metadata_path.unlink(missing_ok=True)
        (directory / name).unlink(missing_ok=True)
        raise FileNotFoundError(directory / name)
    media = directory / name
    if not media.is_file():
        raise FileNotFoundError(media)
    return media


def _prune_locked(
    directory: Path,
    *,
    now: float,
    metadata_limit: int,
    media_limit: int,
    retention_seconds: float,
    media_bytes_limit: int,
) -> None:
    records = []
    for metadata_path in directory.glob("*.wav.json"):
        metadata = _read_json(metadata_path)
        records.append((_record_time(metadata_path, metadata), metadata_path))
    records.sort(key=lambda item: item[0], reverse=True)

    retained = []
    for index, (played_at, metadata_path) in enumerate(records):
        wav_path = Path(os.fspath(metadata_path)[:-5])
        if index >= metadata_limit or played_at < now - retention_seconds:
            metadata_path.unlink(missing_ok=True)
            wav_path.unlink(missing_ok=True)
        else:
            retained.append((played_at, wav_path))

    media_bytes = 0
    media_count = 0
    for _played_at, wav_path in retained:
        try:
            size = wav_path.stat().st_size
        except OSError:
            continue
        keep = media_count < media_limit and media_bytes + size <= media_bytes_limit
        if keep:
            media_count += 1
            media_bytes += size
        else:
            wav_path.unlink(missing_ok=True)


def archive_played_file(
    queue_dir: str | Path,
    source_path: str | Path,
    *,
    metadata: dict | None = None,
    played_at: float | None = None,
    metadata_limit: int = METADATA_LIMIT,
    media_limit: int = MEDIA_LIMIT,
    retention_seconds: float = RETENTION_SECONDS,
    media_bytes_limit: int = MEDIA_BYTES_LIMIT,
) -> Path:
    """Move a successfully played WAV into private history and prune it."""
    queue = Path(queue_dir)
    source = Path(source_path)
    source_name = _safe_name(source.name)
    source_metadata = Path(f"{source}.json")
    now = time.time() if played_at is None else float(played_at)
    directory = played_dir(queue)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    with _history_lock(queue):
        document = _read_json(source_metadata)
        if metadata:
            document.update(metadata)
        history_name = document.pop("replay_history_file", source_name)
        name = _safe_name(history_name)
        destination = directory / name
        destination_metadata = Path(f"{destination}.json")
        document.setdefault("version", 1)
        document.setdefault("media_type", "audio")
        document["played_at"] = now
        document.pop("replay_queued_at", None)
        document.pop("replay_name", None)
        duration = _wav_duration(source)
        if duration is not None:
            document["duration_s"] = duration
        _write_json(destination_metadata, document)
        os.replace(source, destination)
        if source_metadata != destination_metadata:
            source_metadata.unlink(missing_ok=True)
        _prune_locked(
            directory,
            now=now,
            metadata_limit=metadata_limit,
            media_limit=media_limit,
            retention_seconds=retention_seconds,
            media_bytes_limit=media_bytes_limit,
        )
    return destination


def list_played_history(queue_dir: str | Path, *, now: float | None = None) -> list[dict]:
    """Return private history records newest-first without exposing them directly."""
    queue = Path(queue_dir)
    directory = played_dir(queue)
    current = time.time() if now is None else now
    records = []
    with _history_lock(queue):
        _prune_locked(
            directory,
            now=current,
            metadata_limit=METADATA_LIMIT,
            media_limit=MEDIA_LIMIT,
            retention_seconds=RETENTION_SECONDS,
            media_bytes_limit=MEDIA_BYTES_LIMIT,
        )
        for metadata_path in directory.glob("*.wav.json"):
            name = metadata_path.name[:-5]
            try:
                _safe_name(name)
            except ValueError:
                continue
            metadata = _read_json(metadata_path)
            played_at = _record_time(metadata_path, metadata)
            records.append(
                {
                    "file": name,
                    "played_at": played_at,
                    "available": (directory / name).is_file(),
                    "queued": _active_replay(queue, metadata),
                    "metadata": metadata,
                }
            )
    return sorted(records, key=lambda item: item["played_at"], reverse=True)[:METADATA_LIMIT]


def read_played_file(
    queue_dir: str | Path,
    name: str,
    *,
    now: float | None = None,
) -> bytes:
    """Read retained history media while enforcing the retention boundary."""
    queue = Path(queue_dir)
    name = _safe_name(name)
    directory = played_dir(queue)
    current = time.time() if now is None else now
    with _history_lock(queue):
        metadata = _read_json(Path(f"{directory / name}.json"))
        media = _require_retained(directory, name, metadata, now=current)
        return media.read_bytes()


def requeue_played_file(
    queue_dir: str | Path,
    name: str,
    *,
    now: float | None = None,
) -> str:
    """Copy one retained item back to the queue exactly once."""
    queue = Path(queue_dir)
    name = _safe_name(name)
    directory = played_dir(queue)
    source = directory / name
    source_metadata = Path(f"{source}.json")
    current = time.time() if now is None else now

    with _history_lock(queue):
        metadata = _read_json(source_metadata)
        source = _require_retained(directory, name, metadata, now=current)
        if not metadata or not _has_reply_route(metadata):
            raise ValueError("Original reply route is unavailable")
        if _active_replay(queue, metadata):
            return "already_queued"

        interrupted_name = _replay_name(metadata)
        if interrupted_name:
            for suffix in ("", ".json", ".part", ".json.part"):
                (queue / f"{interrupted_name}{suffix}").unlink(missing_ok=True)

        replay_name = _allocate_queue_name(queue, current)
        destination = queue / replay_name
        destination_metadata = Path(f"{destination}.json")
        wav_temporary = destination.with_name(destination.name + ".part")
        metadata_temporary = destination_metadata.with_name(
            destination_metadata.name + ".part"
        )
        metadata["replay_queued_at"] = current
        metadata["replay_name"] = replay_name
        _write_json(source_metadata, metadata)
        queued_metadata = dict(metadata)
        queued_metadata["replay_history_file"] = name
        try:
            shutil.copyfile(source, wav_temporary)
            with metadata_temporary.open("w", encoding="utf-8") as handle:
                json.dump(queued_metadata, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(metadata_temporary, destination_metadata)
            os.replace(wav_temporary, destination)
        except Exception:
            wav_temporary.unlink(missing_ok=True)
            metadata_temporary.unlink(missing_ok=True)
            if not destination.exists():
                destination_metadata.unlink(missing_ok=True)
            metadata.pop("replay_queued_at", None)
            metadata.pop("replay_name", None)
            _write_json(source_metadata, metadata)
            raise
    return "queued"
