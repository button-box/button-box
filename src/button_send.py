#!/usr/bin/env python3
"""Message Box physical-button service.

With MSGBOX_GUIDED_REPLY=1, one press starts exactly one session. The oldest
incoming message (if any) is played; MSGBOX_AUTO_RECORD_AFTER_INCOMING controls
whether playback continues into a reply recording. With no incoming message, a
standalone family message is recorded. Recordings use silence-aware stop,
private playback, and explicit send approval. Approved audio is atomically
bound to its exact recipient in the durable outbox before the child flow
returns.

With the flag off (the default), the established hold-to-record / short-to-play
behavior remains available as the immediate rollback.
"""

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from gpiozero import Button, LED

from guided_reply import (
    EnergyVAD,
    GuidedSession,
    OutboxStore,
    RecordingResult,
    claim_inbox_file,
    discard_held_playback_press,
    env_flag,
    finish_inbox_file,
    invalid_prompt_files,
    raw_pcm_to_trimmed_wav,
    recover_inflight_files,
    release_inbox_file,
    should_ring_after_unsent_session,
    voice_send_command,
)
from listened_receipts import AnnouncementGate, ReceiptStore, parse_wacli_send_id
from contacts import ContactError, ContactStore
from nfc_state import AnnouncementStore, NfcError, active_selection, claim_selection

try:
    from runtime_paths import APP_DIR, OUTBOX_DIR as DEFAULT_OUTBOX_DIR
    from runtime_paths import QUEUE_DIR as DEFAULT_QUEUE_DIR
    from runtime_paths import (
        CONTACTS_FILE,
        NFC_ANNOUNCEMENT_FILE,
        NFC_SELECTION_FILE,
        RUNTIME_DIR,
        STATE_DIR as DEFAULT_STATE_DIR,
    )
except ModuleNotFoundError:
    from src.runtime_paths import APP_DIR, OUTBOX_DIR as DEFAULT_OUTBOX_DIR
    from src.runtime_paths import QUEUE_DIR as DEFAULT_QUEUE_DIR
    from src.runtime_paths import (
        CONTACTS_FILE,
        NFC_ANNOUNCEMENT_FILE,
        NFC_SELECTION_FILE,
        RUNTIME_DIR,
        STATE_DIR as DEFAULT_STATE_DIR,
    )


MIC_DEV = os.environ.get("MSGBOX_MIC_DEV", "plughw:CARD=Device,DEV=0")
SPK_DEV = os.environ.get("MSGBOX_SPK_DEV", "plughw:CARD=Device_1,DEV=0")
BUTTON_PIN = int(os.environ.get("MSGBOX_BUTTON_PIN", "17"))
LED_PIN = int(os.environ.get("MSGBOX_LED_PIN", "26"))
MAX_SECONDS = int(os.environ.get("MSGBOX_MAX_SECONDS", "60"))  # rollback flow only
LOCK_WAIT = os.environ.get("MSGBOX_LOCK_WAIT", "60s")
WACLI_BIN = "/usr/local/bin/wacli"
QUEUE_DIR = str(DEFAULT_QUEUE_DIR)
OUTBOX_DIR = str(DEFAULT_OUTBOX_DIR)
STATE_DIR = str(DEFAULT_STATE_DIR)
TEMP_DIR = os.path.join(str(RUNTIME_DIR), "guided-reply-tmp")
EVENTS_FILE = os.path.join(STATE_DIR, "events.jsonl")
LISTENED_DIR = os.path.join(STATE_DIR, "listened-receipts")
LISTENED_FALLBACK_WAV = os.environ.get(
    "MSGBOX_LISTENED_FALLBACK_WAV",
    str(APP_DIR / "sounds" / "listen-receipts" / "someone-listened.wav"),
)
LISTENED_POLL_S = float(os.environ.get("MSGBOX_LISTENED_POLL_S", "0.2"))
LISTENED_RETRY_S = float(os.environ.get("MSGBOX_LISTENED_RETRY_S", "30"))
RING_WAV = os.environ.get(
    "MSGBOX_RING_WAV", str(APP_DIR / "ringtones" / "ring3.wav")
)
RING_REQUEST_FILE = str(RUNTIME_DIR / "ring-request")
NFC_SELECTION_TTL_S = float(os.environ.get("MSGBOX_NFC_SELECTION_TTL_S", "30"))
NFC_ANNOUNCEMENT_POLL_S = float(
    os.environ.get("MSGBOX_NFC_ANNOUNCEMENT_POLL_S", "0.1")
)
NFC_DETECTION_BEEP = env_flag("MSGBOX_NFC_DETECTION_BEEP", default=False)
PLACE_TOKEN_WAV = os.environ.get(
    "MSGBOX_PLACE_TOKEN_WAV",
    str(APP_DIR / "sounds" / "nfc" / "place-token.wav"),
)
GUIDED_REPLY = env_flag("MSGBOX_GUIDED_REPLY", default=False)
AUTO_RECORD_AFTER_INCOMING = env_flag(
    "MSGBOX_AUTO_RECORD_AFTER_INCOMING", default=False
)
GUIDED_SILENCE_SECONDS = float(os.environ.get("MSGBOX_GUIDED_SILENCE_SECONDS", "20"))
PROMPT_DIR = Path(
    os.environ.get(
        "MSGBOX_PROMPT_DIR",
        str(APP_DIR / "sounds" / "guided-reply"),
    )
)
PROMPTS = {
    "reply": PROMPT_DIR / "reply-countdown.wav",
    "standalone": PROMPT_DIR / "standalone-countdown.wav",
    "send": PROMPT_DIR / "press-to-send.wav",
    "delete_warning": PROMPT_DIR / "delete-warning.wav",
    "not_sent": PROMPT_DIR / "not-sent.wav",
}

# Quiet hours: lamp dark, no ringtone (messages still queue; a deliberate press
# still plays). Overnight arrivals do not ring when quiet hours end.
QUIET_START_H = int(os.environ.get("MSGBOX_QUIET_START_H", "22"))
QUIET_END_H = int(os.environ.get("MSGBOX_QUIET_END_H", "7"))
RING_PHRASE = [
    (True, 1.2),
    (True, 1.6),
    (False, 0.4),
    (True, 1.2),
    (True, 1.6),
    (False, 0.6),
] * 3
MIN_HOLD_S = 0.7
POLL_S = 0.005
SETTLE_OPEN_S = 0.5
CONFIRM_PRESS_S = 0.08
CONFIRM_RELEASE_S = 0.2
LED_REFRESH_S = 0.5
SEND_FAIL_BEEP_AT = 3
BEEPS = {
    "press": (str(RUNTIME_DIR / "beep-press.wav"), "1175", "0.07"),
    "nfc": (str(RUNTIME_DIR / "beep-nfc.wav"), "1760", "0.08"),
    "start": (str(RUNTIME_DIR / "beep-start.wav"), "880", "0.12"),
    "sent": (str(RUNTIME_DIR / "beep-sent.wav"), "1320", "0.12"),
    "fail": (str(RUNTIME_DIR / "beep-fail.wav"), "220", "0.6"),
}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def log_event(event_type, **fields):
    """Append content-free operational events; audio is never inspected/logged."""
    try:
        fields["type"] = event_type
        fields["ts"] = time.time()
        os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
        with open(EVENTS_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(fields, sort_keys=True) + "\n")
    except Exception as exc:
        log(f"event log error: {exc}")


def quiet_hours():
    hour = time.localtime().tm_hour
    if QUIET_START_H <= QUIET_END_H:
        return QUIET_START_H <= hour < QUIET_END_H
    return hour >= QUIET_START_H or hour < QUIET_END_H


def make_beeps():
    for path, frequency, duration in BEEPS.values():
        if not os.path.exists(path):
            subprocess.run(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={frequency}:duration={duration}",
                    path,
                ],
                check=True,
            )


def beep(name):
    subprocess.run(["aplay", "-q", "-D", SPK_DEV, BEEPS[name][0]])


def acknowledge_guided_press(action, session_id=None):
    """Give immediate, content-free feedback for each actionable press."""
    beep("press")
    fields = {"action": action}
    if session_id:
        fields["session_id"] = session_id
    log_event("guided_press", **fields)


def queued():
    try:
        return sorted(name for name in os.listdir(QUEUE_DIR) if name.endswith(".wav"))
    except FileNotFoundError:
        return []


def legacy_outbox_files():
    try:
        return sorted(name for name in os.listdir(OUTBOX_DIR) if name.endswith(".wav"))
    except FileNotFoundError:
        return []


_recording = False
_guided_active = False
outbox_store = None
receipt_store = None
announcement_gate = AnnouncementGate(LISTENED_POLL_S, LISTENED_RETRY_S)
nfc_announcement_store = AnnouncementStore(NFC_ANNOUNCEMENT_FILE)
_last_nfc_announcement_poll = 0.0


def current_recipient_context(*, claim=False):
    """Resolve standalone routing from an authoritative contacts snapshot."""
    try:
        document = ContactStore(CONTACTS_FILE).load()
        contacts = document["contacts"]
        if not contacts:
            log("recipient unavailable: no contacts configured")
            return None
        if len(contacts) == 1:
            jid, contact = next(iter(contacts.items()))
            return {"contact": {"jid": jid, **contact}, "via_card": False}
        resolver = claim_selection if claim else active_selection
        context = resolver(
            CONTACTS_FILE,
            NFC_SELECTION_FILE,
            max_age=NFC_SELECTION_TTL_S,
        )
        if context is None:
            log("recipient unavailable: no active card selection")
            return None
        return {"contact": context["contact"], "uid": context["uid"], "via_card": True}
    except (ContactError, NfcError, OSError) as exc:
        log(f"contact routing unavailable: {exc}")
        return None


def routing_mode():
    """Describe startup routing without exposing a contact JID."""
    try:
        count = len(ContactStore(CONTACTS_FILE).load()["contacts"])
    except (ContactError, OSError) as exc:
        log(f"contact routing unavailable: {exc}")
        return "unavailable"
    if count == 0:
        return "no_contacts"
    if count == 1:
        return "single_contact"
    return "card_selection"


def wait_for_hold_intent(
    is_pressed,
    minimum_hold_s,
    poll_s,
    *,
    monotonic=time.monotonic,
    sleeper=time.sleep,
):
    """Classify the shared button before resolving or claiming a recipient."""
    if minimum_hold_s <= 0 or poll_s <= 0:
        raise ValueError("button timing values must be positive")
    started = monotonic()
    while monotonic() - started < minimum_hold_s:
        if not is_pressed():
            return "play"
        sleeper(poll_s)
    return "record"


def prompt_for_token():
    """Refuse outbound recording without leaking the previous selection."""
    log_event("nfc_token_required")
    if os.path.isfile(PLACE_TOKEN_WAV):
        subprocess.run(["aplay", "-q", "-D", SPK_DEV, PLACE_TOKEN_WAV], check=False)
    else:
        log(f"place-token prompt missing: {PLACE_TOKEN_WAV}")
        beep("fail")


def block_unavailable_recipient():
    """Give safe feedback without treating an unconfigured box as card-ready."""
    mode = routing_mode()
    log_event("recipient_required", routing_mode=mode)
    if mode == "card_selection":
        if play_pending_nfc_announcement(force=True) != "unknown":
            prompt_for_token()
    else:
        beep("fail")


def _play_nfc_prompt(uid, action, card_clip):
    beeped = False
    if NFC_DETECTION_BEEP:
        beep("nfc")
        beeped = True
    card_clip = os.path.expanduser(card_clip)
    if card_clip and os.path.isfile(card_clip):
        played = subprocess.run(
            ["aplay", "-q", "-D", SPK_DEV, card_clip], check=False
        ).returncode == 0
        mode = "spoken"
    elif beeped and action in ("recognized", "selected", "enrolled"):
        played = True
        mode = "beep"
    else:
        played = False
        mode = "missing"
        log(f"NFC announcement clip missing: {card_clip or '(not configured)'}")
    log_event("nfc_announced", action=action, played=played, mode=mode)
    if played:
        nfc_announcement_store.acknowledge(uid)
    else:
        nfc_announcement_store.clear_acknowledgement()
        beep("fail")
    return played


def play_pending_nfc_announcement(*, force=False, expected_uid=None):
    """Play at most one reader request while this process exclusively owns audio."""
    global _last_nfc_announcement_poll
    now = time.monotonic()
    if not force and now - _last_nfc_announcement_poll < NFC_ANNOUNCEMENT_POLL_S:
        return False
    _last_nfc_announcement_poll = now
    request = nfc_announcement_store.take()
    if request is None:
        return None
    if expected_uid is not None and request["uid"] != expected_uid:
        return None
    return request["action"] if _play_nfc_prompt(
        request["uid"], request["action"], request["prompt"]
    ) else None


def ensure_nfc_confirmation(context):
    """Require successful feedback for this selected tag."""
    uid = context.get("uid")
    if uid is None or nfc_announcement_store.is_acknowledged(uid):
        return True
    if play_pending_nfc_announcement(force=True, expected_uid=uid):
        return True
    contact = context["contact"]
    return _play_nfc_prompt(uid, "selected", contact.get("card_clip", ""))


def legacy_job_recipient(path):
    """Use only the recipient snapshot bound at recording time."""
    try:
        with open(path + ".json", encoding="utf-8") as handle:
            recipient = json.load(handle).get("recipient")
        if (
            isinstance(recipient, str)
            and recipient
            and recipient.strip() == recipient
            and "@" in recipient
            and not any(character.isspace() for character in recipient)
        ):
            return recipient
    except (OSError, AttributeError, ValueError, json.JSONDecodeError):
        pass
    return None


def bind_legacy_job_recipient(path, recipient):
    """Persist routing before the WAV becomes visible to the sender thread."""
    metadata_path = path + ".json"
    temporary_path = metadata_path + ".part"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "recipient": recipient}, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, metadata_path)


def track_sent_for_receipts(sent, recipient, *, local_message_id, flow):
    """Persist the WhatsApp ID after acceptance without risking a duplicate send."""
    whatsapp_id = parse_wacli_send_id(sent.stdout)
    if not whatsapp_id:
        log_event(
            "listen_tracking_unavailable",
            message_id=local_message_id,
            flow=flow,
            reason="missing_wacli_id",
        )
        return None
    try:
        tracked = receipt_store.track_sent(
            whatsapp_id,
            recipient,
            local_message_id=local_message_id,
            flow=flow,
        )
    except Exception as exc:
        tracked = False
        log(f"receipt tracking error after accepted send: {type(exc).__name__}")
    if not tracked:
        log_event(
            "listen_tracking_unavailable",
            message_id=local_message_id,
            flow=flow,
            reason="persist_failed",
        )
        return None
    return whatsapp_id


def send_legacy_outbox_file(fname):
    """Keep pre-feature durable WAV jobs working with the family-group target."""
    path = os.path.join(OUTBOX_DIR, fname)
    metadata_path = path + ".json"
    recipient = legacy_job_recipient(path)
    if not recipient:
        log(f"legacy send blocked for {fname}: no bound recipient")
        log_event("send_blocked", flow="legacy", reason="missing_recipient")
        return False
    ogg = os.path.join(TEMP_DIR, f"legacy-{uuid.uuid4().hex}.ogg")
    duration = wait_s = None
    try:
        milliseconds, _, raw_duration = fname[:-4].partition("-")
        wait_s = round(time.time() - int(milliseconds) / 1000, 1)
        duration = float(raw_duration)
    except ValueError:
        pass
    converted = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            path,
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-ar",
            "48000",
            "-ac",
            "1",
            ogg,
        ]
    )
    if converted.returncode != 0:
        try:
            os.remove(ogg)
        except OSError:
            pass
        bad = os.path.join(OUTBOX_DIR, ".bad")
        os.makedirs(bad, exist_ok=True)
        os.rename(path, os.path.join(bad, fname))
        if os.path.exists(metadata_path):
            os.replace(metadata_path, os.path.join(bad, fname + ".json"))
        log(f"ffmpeg failed on legacy {fname} - moved to .bad")
        log_event("send_failed", flow="legacy", reason="convert")
        return True
    sent = subprocess.run(
        [
            WACLI_BIN,
            "send",
            "voice",
            "--file",
            ogg,
            "--to",
            recipient,
            "--lock-wait",
            LOCK_WAIT,
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    try:
        os.remove(ogg)
    except OSError:
        pass
    if sent.returncode == 0:
        whatsapp_id = track_sent_for_receipts(
            sent,
            recipient,
            local_message_id=fname,
            flow="legacy",
        )
        os.remove(path)
        try:
            os.remove(metadata_path)
        except FileNotFoundError:
            pass
        log(f"SENT legacy {fname} (queued {wait_s}s)")
        log_event(
            "sent",
            flow="legacy",
            target=recipient,
            dur=duration,
            queue_wait_s=wait_s,
            whatsapp_id=whatsapp_id,
        )
        return True
    log(f"legacy send failed for {fname}: {(sent.stderr or sent.stdout).strip()[:200]}")
    return False


def send_guided_job(job):
    """Send only to the recipient stored atomically with this approved audio."""
    ogg = os.path.join(TEMP_DIR, f"guided-{uuid.uuid4().hex}.ogg")
    converted = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(job.audio_path),
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-ar",
            "48000",
            "-ac",
            "1",
            ogg,
        ]
    )
    if converted.returncode != 0:
        try:
            os.remove(ogg)
        except OSError:
            pass
        outbox_store.set_state(job, "failed", increment_attempts=True)
        log_event("outbox_failed", message_id=job.message_id, reason="convert")
        log(f"guided conversion failed {job.message_id}; retained for parent")
        return True

    # Persist 'sending' before crossing the external side-effect boundary.  A
    # crash from here until completion becomes uncertain on restart, never an
    # automatic duplicate resend.
    job = outbox_store.set_state(job, "sending", increment_attempts=True)
    sent = subprocess.run(
        voice_send_command(WACLI_BIN, ogg, job.recipient, LOCK_WAIT),
        capture_output=True,
        text=True,
    )
    try:
        os.remove(ogg)
    except OSError:
        pass
    if sent.returncode == 0:
        whatsapp_id = track_sent_for_receipts(
            sent,
            job.recipient,
            local_message_id=job.message_id,
            flow=job.flow_kind,
        )
        outbox_store.complete(job)
        log_event(
            "sent",
            flow=job.flow_kind,
            message_id=job.message_id,
            target=job.recipient,
            dur=job.duration,
            whatsapp_id=whatsapp_id,
        )
        log(f"SENT guided {job.message_id}")
        return True
    outbox_store.set_state(job, "pending")
    log_event("outbox_retry", message_id=job.message_id, flow=job.flow_kind)
    log(f"guided send retry {job.message_id}: {(sent.stderr or sent.stdout).strip()[:200]}")
    return False


def sender_loop():
    failures = 0
    while True:
        guided = outbox_store.jobs()
        if guided:
            if send_guided_job(guided[0]):
                failures = 0
                continue
            failures += 1
            time.sleep(min(60, 5 * failures))
            continue
        legacy = legacy_outbox_files()
        if legacy:
            if send_legacy_outbox_file(legacy[0]):
                failures = 0
                continue
            failures += 1
            if failures == SEND_FAIL_BEEP_AT:
                log_event("send_failed", flow="legacy", reason="send")
                if not _recording and not _guided_active:
                    beep("fail")
            time.sleep(min(60, 5 * failures))
            continue
        failures = 0
        time.sleep(0.5)


_led_last = 0.0


def refresh_led(force=False):
    global _led_last
    now = time.monotonic()
    if not force and now - _led_last < LED_REFRESH_S:
        return
    _led_last = now
    (led.on if queued() and not quiet_hours() else led.off)()


def ring_windows():
    windows, elapsed = [], 0.0
    for is_note, duration in RING_PHRASE:
        if is_note:
            windows.append((elapsed, elapsed + min(0.8, duration / 2)))
        elapsed += duration
    return windows


RING_WINS = ring_windows()
RING_CUSTOM = os.path.basename(RING_WAV) != "ring3.wav"


def ring_lamp_on(elapsed):
    if RING_CUSTOM:
        return (elapsed % 0.9) < 0.45
    return any(start <= elapsed < end for start, end in RING_WINS)


_known = None
_seen_ever = set()
_ring_last = 0.0


def mark_queue_known():
    """Suppress a delayed ring for arrivals that queued during a child session."""
    global _known
    snapshot = set(queued())
    _known = snapshot
    _seen_ever.update(snapshot)


def maybe_ring():
    global _known, _ring_last
    now = time.monotonic()
    if now - _ring_last < LED_REFRESH_S:
        return
    _ring_last = now
    snapshot = set(queued())
    if _known is None:
        _known = snapshot
        _seen_ever.update(snapshot)
        return
    fresh = (snapshot - _known) - _seen_ever
    _known = snapshot
    _seen_ever.update(snapshot)
    if fresh and not quiet_hours():
        ring_alert()


def maybe_manual_ring():
    try:
        os.remove(RING_REQUEST_FILE)
    except FileNotFoundError:
        return
    except OSError as exc:
        log(f"manual ring request error: {exc}")
        return
    ring_alert(source="dashboard")


def ring_alert(source="new_message"):
    if not os.path.exists(RING_WAV):
        log(f"ring skipped ({source}): missing {RING_WAV}")
        return
    log(f"ringing: {source}")
    log_event("ring", source=source)
    process = subprocess.Popen(["aplay", "-q", "-D", SPK_DEV, RING_WAV])
    started = time.monotonic()
    try:
        while process.poll() is None:
            elapsed = time.monotonic() - started
            (led.on if ring_lamp_on(elapsed) else led.off)()
            if button.is_pressed:
                process.terminate()
                log("ring cut by press")
                break
            time.sleep(0.02)
    finally:
        if process.poll() is None:
            process.wait()
        refresh_led(force=True)


def load_event_metadata(fname):
    """Compatibility for WAVs queued before durable sidecars were deployed."""
    try:
        match = None
        with open(EVENTS_FILE, encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                if event.get("type") == "received" and event.get("file") == fname:
                    match = {
                        "chat": event.get("chat"),
                        "msgid": event.get("msgid"),
                        "sender_jid": event.get("sender_jid"),
                    }
        return match
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return None


def queue_metadata(wav_path):
    meta_path = str(wav_path) + ".json"
    try:
        with open(meta_path, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return load_event_metadata(os.path.basename(wav_path))


def recover_inflight():
    for _filename in recover_inflight_files(QUEUE_DIR):
        log_event("guided_inbound_recovered")


def claim_oldest():
    names = queued()
    if not names:
        return None
    source = Path(QUEUE_DIR) / names[0]
    metadata = queue_metadata(source)
    claimed = claim_inbox_file(QUEUE_DIR, source.name)
    return {"path": claimed, "meta": metadata}


def finish_claim(claim):
    finish_inbox_file(claim["path"])


def release_claim(claim):
    release_inbox_file(QUEUE_DIR, claim["path"])


def react_played(meta):
    if not meta or not meta.get("msgid") or not meta.get("chat"):
        return
    try:
        command = [
            WACLI_BIN,
            "send",
            "react",
            "--to",
            meta["chat"],
            "--id",
            meta["msgid"],
            "--reaction",
            "🎧",
            "--lock-wait",
            LOCK_WAIT,
        ]
        if meta.get("sender_jid"):
            command += ["--sender", meta["sender_jid"]]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        log(f"react error: {exc}")


def wait_for_stable_open():
    since = None
    while True:
        time.sleep(0.05)
        if not _guided_active:
            refresh_led()
        if not button.is_pressed:
            since = since or time.monotonic()
            if time.monotonic() - since >= SETTLE_OPEN_S:
                return
        else:
            since = None


def wait_for_confirmed_press(timeout=None):
    deadline = None if timeout is None else time.monotonic() + timeout
    closed_since = None
    while deadline is None or time.monotonic() < deadline:
        now = time.monotonic()
        if button.is_pressed:
            closed_since = closed_since or now
            if now - closed_since >= CONFIRM_PRESS_S:
                return True
        else:
            closed_since = None
        time.sleep(POLL_S)
    return False


def play_audio_ordinary(path):
    """Play all audio and discard every press made during it."""
    subprocess.run(
        ["aplay", "-q", "-D", SPK_DEV, str(path)],
        check=True,
        timeout=600,
    )
    discard_held_playback_press(lambda: button.is_pressed, wait_for_stable_open)


def play_pending_listened(limit=4):
    """Play durable acknowledgements while the button service owns audio."""
    played = 0
    for _ in range(max(0, limit)):
        notice = receipt_store.claim_next()
        if notice is None:
            break
        clip = notice.clip or LISTENED_FALLBACK_WAV
        if not os.path.isabs(clip) or not os.path.exists(clip):
            receipt_store.release(notice)
            announcement_gate.blocked()
            log_event(
                "listen_announcement_blocked",
                listener=notice.listener_name,
                reason="missing_clip",
            )
            log(f"listen announcement blocked: missing {clip}")
            break
        try:
            play_audio_ordinary(clip)
            receipt_store.complete(notice)
            played += 1
            log_event("listen_announced", listener=notice.listener_name)
            log(f"announced listened receipt: {notice.listener_name}")
        except Exception as exc:
            receipt_store.release(notice)
            announcement_gate.blocked()
            log_event(
                "listen_announcement_blocked",
                listener=notice.listener_name,
                reason=type(exc).__name__,
            )
            log(f"listen announcement error: {exc}")
            break
    return played


def maybe_play_pending_listened():
    """Announce new played receipts promptly whenever the speaker is idle."""
    busy = _recording or _guided_active or button.is_pressed
    if not announcement_gate.ready(busy=busy):
        return 0
    if receipt_store.pending_count() == 0:
        return 0
    announcement_gate.succeeded()
    return play_pending_listened()


def wait_for_approval(timeout, session_id=None):
    if not wait_for_confirmed_press(timeout):
        return False
    acknowledge_guided_press("approve", session_id)
    wait_for_stable_open()
    return True


def play_warning_for_approval(path, session_id=None):
    """The one playback state where a press is consumed as approval."""
    discard_held_playback_press(lambda: button.is_pressed, wait_for_stable_open)
    process = subprocess.Popen(["aplay", "-q", "-D", SPK_DEV, str(path)])
    approved = False
    try:
        closed_since = None
        while process.poll() is None:
            now = time.monotonic()
            if button.is_pressed:
                closed_since = closed_since or now
                if now - closed_since >= CONFIRM_PRESS_S:
                    approved = True
                    process.terminate()
                    break
            else:
                closed_since = None
            time.sleep(POLL_S)
    finally:
        if process.poll() is None:
            process.wait()
    if approved:
        acknowledge_guided_press("approve_warning", session_id)
    wait_for_stable_open()
    return approved


def presence(kind, recipient):
    subcommand = ["typing", "--media", "audio"] if kind == "recording" else ["paused"]
    subprocess.Popen(
        [WACLI_BIN, "presence", *subcommand, "--to", recipient, "--lock-wait", LOCK_WAIT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def cleanup_temp_recordings():
    os.makedirs(TEMP_DIR, exist_ok=True)
    for path in Path(TEMP_DIR).iterdir():
        if path.is_file() and path.suffix in {".raw", ".wav", ".ogg", ".part"}:
            path.unlink()


def capture_guided_recording(recipient, session_id=None):
    global _recording
    _recording = True
    led.on()
    capture_id = uuid.uuid4().hex
    raw_path = Path(TEMP_DIR) / f"{capture_id}.raw"
    wav_path = Path(TEMP_DIR) / f"{capture_id}.wav"
    vad = EnergyVAD(silence_seconds=GUIDED_SILENCE_SECONDS)
    process = subprocess.Popen(
        [
            "arecord",
            "-q",
            "-D",
            MIC_DEV,
            "-t",
            "raw",
            "-f",
            "S16_LE",
            "-r",
            "16000",
            "-c",
            "1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    started = time.monotonic()
    vad.start(started)
    presence("recording", recipient)
    presence_last = started
    closed_since = None
    stopped_by_press = False
    try:
        with open(raw_path, "wb") as raw:
            while True:
                now = time.monotonic()
                readable, _, _ = select.select([process.stdout], [], [], 0.02)
                if readable:
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if chunk:
                        raw.write(chunk)
                        vad.feed(chunk, now=now)
                if button.is_pressed:
                    closed_since = closed_since or now
                    if now - closed_since >= CONFIRM_PRESS_S:
                        stopped_by_press = True
                        break
                else:
                    closed_since = None
                if vad.silence_expired(now):
                    break
                if now - presence_last >= 8:
                    presence("recording", recipient)
                    presence_last = now
                if process.poll() is not None:
                    raise RuntimeError("arecord exited unexpectedly")
            process.send_signal(signal.SIGINT)
            try:
                remainder, _ = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                remainder, _ = process.communicate(timeout=3)
            if remainder:
                raw.write(remainder)
                vad.feed(remainder, now=time.monotonic())
            raw.flush()
            os.fsync(raw.fileno())
    except Exception:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        for path in (raw_path, wav_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        presence("paused", recipient)
        led.off()
        _recording = False
        if stopped_by_press:
            acknowledge_guided_press("stop_recording", session_id)
            wait_for_stable_open()

    bounds = vad.trim_bounds()
    if bounds is None:
        raw_path.unlink(missing_ok=True)
        return RecordingResult(None, time.monotonic() - started, False)
    duration = raw_pcm_to_trimmed_wav(str(raw_path), str(wav_path), bounds)
    raw_path.unlink(missing_ok=True)
    return RecordingResult(str(wav_path), duration, True)


class PiGuidedIO:
    def __init__(self, recipient, session_id):
        self.recipient = recipient
        self.session_id = session_id

    def play_ordinary(self, path):
        play_audio_ordinary(path)

    def record(self):
        return capture_guided_recording(self.recipient, self.session_id)

    def wait_for_approval(self, timeout):
        return wait_for_approval(timeout, self.session_id)

    def play_warning_for_approval(self, path):
        return play_warning_for_approval(path, self.session_id)

    def delete(self, path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def play_next_legacy():
    play_pending_listened()
    names = queued()
    if not names:
        return
    path = Path(QUEUE_DIR) / names[0]
    meta = queue_metadata(path)
    log(f"playing {names[0]} ({len(names)} waiting)")
    try:
        subprocess.run(["aplay", "-q", "-D", SPK_DEV, str(path)], check=True, timeout=600)
        path.unlink()
        Path(str(path) + ".json").unlink(missing_ok=True)
        react_played(meta)
        try:
            wait_s = time.time() - int(names[0].split("-", 1)[0]) / 1000
        except ValueError:
            wait_s = None
        log_event("played", wait_s=wait_s)
    except Exception as exc:
        log(f"play error: {exc}")
        beep("fail")
    wait_for_stable_open()
    refresh_led(force=True)


def record_and_send_legacy():
    global _recording
    intent = wait_for_hold_intent(lambda: button.is_pressed, MIN_HOLD_S, POLL_S)
    if intent == "play":
        wait_for_stable_open()
        play_next_legacy()
        return
    context = current_recipient_context(claim=True)
    if context is None:
        block_unavailable_recipient()
        return
    recipient = context["contact"]["jid"]
    if context["via_card"] and not ensure_nfc_confirmation(context):
        log_event("nfc_confirmation_failed")
        return
    if context["via_card"] and not button.is_pressed:
        return
    _recording = True
    led.on()
    beep("start")
    part = os.path.join(OUTBOX_DIR, f"{int(time.time() * 1000)}.part")
    recorder = subprocess.Popen(
        [
            "arecord",
            "-q",
            "-D",
            MIC_DEV,
            "-t",
            "wav",
            "-f",
            "S16_LE",
            "-r",
            "48000",
            "-c",
            "1",
            "-d",
            str(MAX_SECONDS + 2),
            part,
        ]
    )
    started = time.monotonic()
    open_since = None
    presence_last = None
    try:
        while True:
            time.sleep(POLL_S)
            now = time.monotonic()
            if now - started >= MIN_HOLD_S and (
                presence_last is None or now - presence_last >= 8
            ):
                presence("recording", recipient)
                presence_last = now
            if not button.is_pressed:
                open_since = open_since or now
                if now - open_since >= CONFIRM_RELEASE_S:
                    break
            else:
                open_since = None
            if now - started > MAX_SECONDS + 4:
                led.off()
                recorder.send_signal(signal.SIGINT)
                recorder.wait()
                os.remove(part)
                beep("fail")
                if presence_last:
                    presence("paused", recipient)
                wait_for_stable_open()
                return
        held = time.monotonic() - started
        led.off()
        recorder.send_signal(signal.SIGINT)
        recorder.wait()
        if presence_last:
            presence("paused", recipient)
        if held >= MAX_SECONDS:
            os.remove(part)
            beep("fail")
            return
        final_path = part[:-5] + f"-{held:.1f}.wav"
        bind_legacy_job_recipient(final_path, recipient)
        os.replace(part, final_path)
        beep("sent")
    finally:
        _recording = False


def run_guided_once():
    global _guided_active
    session_id = uuid.uuid4().hex
    claim = claim_oldest()
    flow_kind = "reply" if claim else "standalone"
    metadata = claim["meta"] if claim else None
    context = current_recipient_context(claim=True) if not claim else None
    recipient = metadata.get("chat") if metadata else (
        context["contact"]["jid"] if context else None
    )
    if not claim:
        if context is None:
            block_unavailable_recipient()
            return
        if context["via_card"] and not ensure_nfc_confirmation(context):
            log_event("nfc_confirmation_failed")
            return
    if claim and not recipient:
        # The message may be heard, but a reply is never guessed or rerouted.
        try:
            play_audio_ordinary(claim["path"])
            finish_claim(claim)
            log_event("guided_unroutable_inbound")
        except Exception:
            release_claim(claim)
            raise
        return

    led.off()
    io = PiGuidedIO(recipient, session_id)

    def session_event(kind, **data):
        if kind == "guided_session_started" and claim:
            data["source_file"] = claim["path"].name
        log_event(kind, **data)
        if kind == "guided_inbound_played":
            react_played(metadata)

    session = GuidedSession(io, outbox_store, session_event)
    _guided_active = True
    outcome = None
    try:
        # Catch a receipt that arrived after the idle loop saw this press. The
        # announcement finishes before the requested child interaction begins.
        play_pending_listened()
        outcome = session.run(
            recipient=recipient,
            flow_kind=flow_kind,
            countdown_path=str(PROMPTS["reply" if claim else "standalone"]),
            send_prompt_path=str(PROMPTS["send"]),
            delete_warning_path=str(PROMPTS["delete_warning"]),
            not_sent_path=str(PROMPTS["not_sent"]),
            incoming_path=str(claim["path"]) if claim else None,
            session_id=session_id,
            auto_record_after_incoming=AUTO_RECORD_AFTER_INCOMING,
        )
        if claim:
            finish_claim(claim)
    except Exception:
        cleanup_temp_recordings()
        if claim:
            release_claim(claim)
        log_event(
            "guided_session_interrupted", flow=flow_kind, session_id=session_id
        )
        raise
    finally:
        _guided_active = False
        ring_waiting = should_ring_after_unsent_session(
            outcome, bool(queued()), quiet_hours()
        )
        mark_queue_known()
        refresh_led(force=True)
        if ring_waiting:
            ring_alert(source="queued_after_unsent")


def validate_prompts():
    invalid = invalid_prompt_files(PROMPTS.values())
    if invalid:
        raise RuntimeError(
            "guided reply prompt assets missing/invalid: " + ", ".join(invalid)
        )


def main():
    global button, led, outbox_store, receipt_store
    make_beeps()
    os.makedirs(QUEUE_DIR, exist_ok=True)
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    outbox_store = OutboxStore(OUTBOX_DIR)
    receipt_store = ReceiptStore(LISTENED_DIR)
    recover_inflight()
    for _ in range(receipt_store.recover_inflight()):
        log_event("listen_announcement_recovered")
    cleanup_temp_recordings()
    for partial in Path(OUTBOX_DIR).glob("*.part"):
        partial.unlink()
    for message_id in outbox_store.recover_startup():
        log_event("outbox_uncertain", message_id=message_id)

    if "--drain" in sys.argv:
        ok = True
        for job in outbox_store.jobs():
            ok = send_guided_job(job) and ok
        for filename in legacy_outbox_files():
            ok = send_legacy_outbox_file(filename) and ok
        return 0 if ok else 1

    if GUIDED_REPLY:
        try:
            validate_prompts()
        except RuntimeError as exc:
            log(str(exc))
            return 2

    threading.Thread(target=sender_loop, daemon=True).start()
    button = Button(BUTTON_PIN)
    led = LED(LED_PIN)
    if button.is_pressed:
        log("switch CLOSED at startup - waiting for it to open")
    wait_for_stable_open()
    refresh_led(force=True)
    log(
        f"armed: guided_reply={int(GUIDED_REPLY)} "
        f"auto_record_after_incoming={int(AUTO_RECORD_AFTER_INCOMING)} "
        f"routing_mode={routing_mode()} "
        f"({len(queued())} queued, "
        f"{len(outbox_store.jobs()) + len(legacy_outbox_files())} unsent)"
    )

    while True:
        wait_for_stable_open()
        while not button.is_pressed:
            time.sleep(POLL_S)
            play_pending_nfc_announcement()
            maybe_play_pending_listened()
            refresh_led()
            maybe_manual_ring()
            maybe_ring()
        closed_at = time.monotonic()
        solid = True
        while time.monotonic() - closed_at < CONFIRM_PRESS_S:
            time.sleep(POLL_S)
            if not button.is_pressed:
                solid = False
                break
        if not solid:
            continue
        try:
            if GUIDED_REPLY:
                # The session starts from this press only after its release; it can
                # never be carried into incoming audio, countdown, or recording.
                acknowledge_guided_press("start_session")
                wait_for_stable_open()
                run_guided_once()
            else:
                record_and_send_legacy()
        except Exception as exc:
            log(f"button flow error: {exc}")
            log_event(
                "button_flow_error", guided=GUIDED_REPLY, error=type(exc).__name__
            )
            if not _guided_active:
                beep("fail")
        finally:
            refresh_led(force=True)


if __name__ == "__main__":
    raise SystemExit(main())
