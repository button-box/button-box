"""Durable, bounded history for successfully played inbound audio."""

from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
import time
import wave
from contextlib import contextmanager
from pathlib import Path


METADATA_LIMIT = 20
MEDIA_LIMIT = 10
RETENTION_SECONDS = 14 * 86400
MEDIA_BYTES_LIMIT = 128 * 1024 * 1024
REPLAY_PENDING_SECONDS = 3600


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
    chat = metadata.get("chat")
    return isinstance(chat, str) and bool(chat.strip())


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
    name = _safe_name(source.name)
    source_metadata = Path(f"{source}.json")
    now = time.time() if played_at is None else float(played_at)
    directory = played_dir(queue)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = directory / name
    destination_metadata = Path(f"{destination}.json")

    with _history_lock(queue):
        document = _read_json(source_metadata)
        if metadata:
            document.update(metadata)
        document.setdefault("version", 1)
        document.setdefault("media_type", "audio")
        document["played_at"] = now
        document.pop("replay_queued_at", None)
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
    for metadata_path in directory.glob("*.wav.json"):
        name = metadata_path.name[:-5]
        try:
            _safe_name(name)
        except ValueError:
            continue
        metadata = _read_json(metadata_path)
        played_at = _record_time(metadata_path, metadata)
        if played_at < current - RETENTION_SECONDS:
            continue
        active = any(
            (path / name).exists()
            for path in (queue, queue / ".inflight", queue / ".hold")
        )
        queued_at = metadata.get("replay_queued_at")
        queued = active or (
            isinstance(queued_at, (int, float))
            and current - queued_at < REPLAY_PENDING_SECONDS
        )
        records.append(
            {
                "file": name,
                "played_at": played_at,
                "available": (directory / name).is_file(),
                "queued": queued,
                "metadata": metadata,
            }
        )
    return sorted(records, key=lambda item: item["played_at"], reverse=True)[:METADATA_LIMIT]


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
        if not metadata or not _has_reply_route(metadata):
            raise ValueError("Original reply route is unavailable")
        active = any(
            (path / name).exists()
            for path in (queue, queue / ".inflight", queue / ".hold")
        )
        queued_at = metadata.get("replay_queued_at")
        if active or (
            isinstance(queued_at, (int, float))
            and current - queued_at < REPLAY_PENDING_SECONDS
        ):
            return "already_queued"
        if not source.is_file():
            raise FileNotFoundError(source)

        destination = queue / name
        destination_metadata = Path(f"{destination}.json")
        wav_temporary = destination.with_name(destination.name + ".part")
        metadata_temporary = destination_metadata.with_name(
            destination_metadata.name + ".part"
        )
        metadata["replay_queued_at"] = current
        _write_json(source_metadata, metadata)
        try:
            shutil.copyfile(source, wav_temporary)
            with metadata_temporary.open("w", encoding="utf-8") as handle:
                json.dump(metadata, handle, sort_keys=True)
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
            _write_json(source_metadata, metadata)
            raise
    return "queued"
