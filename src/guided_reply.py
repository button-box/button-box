#!/usr/bin/env python3
"""Pure guided-reply state, VAD, and durable outbox primitives.

The GPIO/audio process lives in button_send.py.  This module deliberately has
no Raspberry Pi dependencies so its safety-critical behavior can be tested on
the development machine.
"""

from __future__ import annotations

import array
import json
import math
import os
import shutil
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path


def env_flag(name: str, default: bool = False, environ=None) -> bool:
    values = os.environ if environ is None else environ
    raw = values.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def discard_held_playback_press(is_pressed, wait_for_open) -> bool:
    """Discard only a press that is still held when ordinary playback ends.

    A press completed during playback is already gone. If the switch is open
    at the boundary, return immediately so a new approval press is not hidden
    behind an unnecessary release-settle delay.
    """
    if not is_pressed():
        return False
    wait_for_open()
    return True


def should_ring_after_unsent_session(
    outcome: str | None, queue_waiting: bool, quiet: bool
) -> bool:
    """Ring for a waiting message after the child's recording was not kept."""
    return outcome in {"deleted", "empty"} and queue_waiting and not quiet


def invalid_prompt_files(paths) -> list[str]:
    invalid = []
    for raw_path in paths:
        path = str(raw_path)
        try:
            with wave.open(path, "rb") as prompt:
                if prompt.getnframes() <= 0 or prompt.getnchannels() != 1:
                    raise ValueError("empty or non-mono WAV")
        except (OSError, EOFError, ValueError, wave.Error):
            invalid.append(path)
    return invalid


def voice_send_command(wacli_bin: str, ogg_path: str, recipient: str, lock_wait: str) -> list[str]:
    if not recipient:
        raise ValueError("recipient is required")
    return [
        wacli_bin,
        "send",
        "voice",
        "--file",
        ogg_path,
        "--to",
        recipient,
        "--lock-wait",
        lock_wait,
        "--json",
    ]


def claim_inbox_file(queue_dir: str, filename: str) -> Path:
    queue = Path(queue_dir)
    source = queue / filename
    inflight = queue / ".inflight"
    inflight.mkdir(parents=True, exist_ok=True)
    claimed = inflight / source.name
    os.replace(source, claimed)
    source_meta = Path(str(source) + ".json")
    if source_meta.exists():
        os.replace(source_meta, Path(str(claimed) + ".json"))
    return claimed


def release_inbox_file(queue_dir: str, claimed: Path) -> None:
    target = Path(queue_dir) / claimed.name
    claimed_meta = Path(str(claimed) + ".json")
    target_meta = Path(str(target) + ".json")
    if claimed_meta.exists():
        os.replace(claimed_meta, target_meta)
    if claimed.exists():
        os.replace(claimed, target)


def finish_inbox_file(claimed: Path) -> None:
    for path in (claimed, Path(str(claimed) + ".json")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def recover_inflight_files(queue_dir: str) -> list[str]:
    queue = Path(queue_dir)
    inflight = queue / ".inflight"
    recovered = []
    if not inflight.exists():
        return recovered
    for wav in sorted(inflight.glob("*.wav")):
        target = queue / wav.name
        meta = Path(str(wav) + ".json")
        target_meta = Path(str(target) + ".json")
        if meta.exists() and not target_meta.exists():
            os.replace(meta, target_meta)
        if not target.exists():
            os.replace(wav, target)
            recovered.append(wav.name)
    # Repair the narrow crash window after the WAV was claimed but before its
    # sidecar moved. The WAV recovery above leaves the original sidecar valid.
    for meta in inflight.glob("*.wav.json"):
        target_meta = queue / meta.name
        if not target_meta.exists():
            os.replace(meta, target_meta)
    return recovered


@dataclass(frozen=True)
class RecordingResult:
    path: str | None
    duration: float
    meaningful: bool


class EnergyVAD:
    """Adaptive PCM-energy VAD for a quiet-room, close-mic child recording.

    Frames are 16-bit mono PCM.  A small consecutive-frame requirement rejects
    clicks/knocks; a cumulative requirement rejects very short incidental
    sounds.  The silence timer resets on every voice frame and has no total
    recording cap.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        silence_seconds: float = 20.0,
        min_rms: float = 350.0,
        threshold_ratio: float = 3.0,
        meaningful_ms: int = 200,
        consecutive_ms: int = 80,
    ):
        self.frame_ms = frame_ms
        self.frame_samples = sample_rate * frame_ms // 1000
        self.frame_bytes = self.frame_samples * 2
        self.silence_seconds = silence_seconds
        self.min_rms = min_rms
        self.threshold_ratio = threshold_ratio
        self.meaningful_frames = max(1, math.ceil(meaningful_ms / frame_ms))
        self.consecutive_frames = max(1, math.ceil(consecutive_ms / frame_ms))
        self.noise_floor = 100.0
        self.total_frames = 0
        self.voiced_frames = 0
        self.current_run = 0
        self.max_run = 0
        self.first_voice_frame: int | None = None
        self.last_voice_frame: int | None = None
        self.started_at: float | None = None
        self.last_voice_at: float | None = None
        self._buffer = b""

    @property
    def meaningful(self) -> bool:
        return (
            self.voiced_frames >= self.meaningful_frames
            and self.max_run >= self.consecutive_frames
        )

    def start(self, now: float | None = None) -> None:
        self.started_at = time.monotonic() if now is None else now

    def feed(self, pcm: bytes, now: float | None = None) -> int:
        """Feed arbitrary even-length PCM bytes; return processed frame count."""
        if self.started_at is None:
            self.start(now)
        self._buffer += pcm
        processed = 0
        while len(self._buffer) >= self.frame_bytes:
            frame = self._buffer[: self.frame_bytes]
            self._buffer = self._buffer[self.frame_bytes :]
            frame_now = time.monotonic() if now is None else now
            self._feed_frame(frame, frame_now)
            processed += 1
        return processed

    def _feed_frame(self, frame: bytes, now: float) -> None:
        samples = array.array("h")
        samples.frombytes(frame)
        if os.sys.byteorder != "little":
            samples.byteswap()
        rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
        threshold = max(self.min_rms, self.noise_floor * self.threshold_ratio)
        voiced = rms >= threshold
        index = self.total_frames
        self.total_frames += 1
        if voiced:
            self.voiced_frames += 1
            self.current_run += 1
            self.max_run = max(self.max_run, self.current_run)
            if self.first_voice_frame is None:
                self.first_voice_frame = index
            self.last_voice_frame = index
            self.last_voice_at = now
        else:
            self.current_run = 0
            # Learn only from frames comfortably below the current threshold so
            # speech never ratchets the baseline upward.
            if rms < threshold * 0.7:
                self.noise_floor = self.noise_floor * 0.98 + rms * 0.02

    def silence_expired(self, now: float | None = None) -> bool:
        if self.started_at is None:
            return False
        current = time.monotonic() if now is None else now
        anchor = self.last_voice_at if self.last_voice_at is not None else self.started_at
        return current - anchor >= self.silence_seconds

    def trim_bounds(self, leading_pad_ms: int = 200, trailing_pad_ms: int = 300) -> tuple[int, int] | None:
        if not self.meaningful or self.first_voice_frame is None or self.last_voice_frame is None:
            return None
        leading = math.ceil(leading_pad_ms / self.frame_ms)
        trailing = math.ceil(trailing_pad_ms / self.frame_ms)
        first = max(0, self.first_voice_frame - leading)
        last_exclusive = min(self.total_frames, self.last_voice_frame + trailing + 1)
        return first * self.frame_samples, last_exclusive * self.frame_samples


def raw_pcm_to_trimmed_wav(
    raw_path: str,
    wav_path: str,
    sample_bounds: tuple[int, int],
    sample_rate: int = 16000,
) -> float:
    """Write only the leading/trailing-trimmed region; internal silence stays."""
    first, last = sample_bounds
    if last <= first:
        raise ValueError("empty trim bounds")
    with open(raw_path, "rb") as src:
        src.seek(first * 2)
        pcm = src.read((last - first) * 2)
    with wave.open(wav_path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm)
    return len(pcm) / 2 / sample_rate


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


@dataclass(frozen=True)
class OutboxJob:
    message_id: str
    path: Path
    audio_path: Path
    recipient: str
    flow_kind: str
    duration: float
    state: str


class OutboxStore:
    """Crash-aware jobs whose recipient is always persisted with the audio."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def approve(
        self,
        source_wav: str,
        recipient: str,
        flow_kind: str,
        duration: float,
        message_id: str | None = None,
    ) -> OutboxJob:
        if not recipient:
            raise ValueError("recipient is required")
        mid = message_id or uuid.uuid4().hex
        final_dir = self.root / f"{mid}.job"
        if final_dir.exists():
            existing = self.load(final_dir)
            if existing.recipient != recipient:
                raise ValueError("message id already bound to a different recipient")
            return existing
        tmp_dir = self.root / f".{mid}.tmp"
        tmp_dir.mkdir(mode=0o700)
        audio = tmp_dir / "audio.wav"
        shutil.copyfile(source_wav, audio)
        with open(audio, "rb") as handle:
            os.fsync(handle.fileno())
        payload = {
            "version": 1,
            "message_id": mid,
            "recipient": recipient,
            "flow_kind": flow_kind,
            "duration": round(float(duration), 3),
            "state": "pending",
            "created_at": time.time(),
            "attempts": 0,
        }
        _write_json_atomic(tmp_dir / "job.json", payload)
        _fsync_dir(tmp_dir)
        os.replace(tmp_dir, final_dir)
        _fsync_dir(self.root)
        return self.load(final_dir)

    def load(self, job_dir: Path) -> OutboxJob:
        with open(job_dir / "job.json", encoding="utf-8") as handle:
            data = json.load(handle)
        return OutboxJob(
            message_id=data["message_id"],
            path=job_dir,
            audio_path=job_dir / "audio.wav",
            recipient=data["recipient"],
            flow_kind=data["flow_kind"],
            duration=float(data.get("duration") or 0),
            state=data.get("state", "pending"),
        )

    def jobs(self, states: tuple[str, ...] = ("pending",)) -> list[OutboxJob]:
        rows = []
        for path in sorted(self.root.glob("*.job"), key=lambda item: item.stat().st_mtime):
            try:
                job = self.load(path)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
            if job.state in states:
                rows.append(job)
        return rows

    def set_state(self, job: OutboxJob, state: str, *, increment_attempts: bool = False) -> OutboxJob:
        meta = job.path / "job.json"
        with open(meta, encoding="utf-8") as handle:
            data = json.load(handle)
        data["state"] = state
        data["updated_at"] = time.time()
        if increment_attempts:
            data["attempts"] = int(data.get("attempts") or 0) + 1
        _write_json_atomic(meta, data)
        return self.load(job.path)

    def complete(self, job: OutboxJob) -> None:
        shutil.rmtree(job.path)
        _fsync_dir(self.root)

    def recover_startup(self) -> list[str]:
        """Remove unapproved staging and quarantine crash-ambiguous sends.

        A job in ``sending`` may already have reached WhatsApp.  Retrying it
        could duplicate a child's message, so it becomes ``uncertain`` for a
        parent to inspect instead of being sent automatically.
        """
        uncertain = []
        for path in self.root.glob(".*.tmp"):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        for job in self.jobs(states=("sending",)):
            self.set_state(job, "uncertain")
            uncertain.append(job.message_id)
        _fsync_dir(self.root)
        return uncertain


class GuidedSession:
    """Orchestrate one inbound reply or one standalone outgoing message.

    ``io`` owns real audio and button operations.  Ordinary playback never
    returns a button result, making carried presses impossible by contract.
    Only ``wait_for_approval`` and ``play_warning_for_approval`` can approve.
    """

    def __init__(self, io, outbox: OutboxStore, event):
        self.io = io
        self.outbox = outbox
        self.event = event

    def run(
        self,
        *,
        recipient: str,
        flow_kind: str,
        countdown_path: str,
        send_prompt_path: str,
        delete_warning_path: str,
        not_sent_path: str,
        incoming_path: str | None = None,
        session_id: str | None = None,
        auto_record_after_incoming: bool = True,
    ) -> str:
        session_id = session_id or uuid.uuid4().hex
        self.event("guided_session_started", session_id=session_id, flow=flow_kind)
        if incoming_path:
            self.io.play_ordinary(incoming_path)
            self.event("guided_inbound_played", session_id=session_id)
            if not auto_record_after_incoming:
                self.event("guided_playback_only", session_id=session_id)
                return "played"
        self.io.play_ordinary(countdown_path)
        recording: RecordingResult = self.io.record()
        if not recording.meaningful or not recording.path:
            if recording.path:
                self.io.delete(recording.path)
            self.event("guided_recording_empty", session_id=session_id)
            return "empty"
        self.io.play_ordinary(recording.path)
        self.event("guided_review_played", session_id=session_id, duration=recording.duration)
        self.io.play_ordinary(send_prompt_path)
        approved = self.io.wait_for_approval(10.0)
        if not approved:
            approved = self.io.play_warning_for_approval(delete_warning_path)
        if approved:
            job = self.outbox.approve(
                recording.path,
                recipient,
                flow_kind,
                recording.duration,
            )
            self.event(
                "guided_approved",
                session_id=session_id,
                message_id=job.message_id,
                flow=flow_kind,
                duration=recording.duration,
            )
            self.io.delete(recording.path)
            return "approved"
        self.io.play_ordinary(not_sent_path)
        self.io.delete(recording.path)
        self.event("guided_deleted", session_id=session_id, flow=flow_kind)
        return "deleted"
