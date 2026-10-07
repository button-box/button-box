#!/usr/bin/env python3
"""Button Box physical-button service with caregiver-selectable interaction."""

import json
import hashlib
import os
import queue
import re
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gpiozero import Button, LED

from messagebox.event_log import append_event, UnavailableEvents
from messagebox.guided_reply import (
    EnergyVAD,
    GuidedSession,
    OutboxStore,
    RecordingResult,
    claim_inbox_file,
    cloud_outbox_lock,
    cloud_upload_payload,
    discard_held_playback_press,
    invalid_prompt_files,
    raw_pcm_to_trimmed_wav,
    recover_inflight_files,
    release_inbox_file,
    should_ring_after_unsent_session,
    voice_send_command,
    valid_account_scope,
)
from messagebox.audio_requests import AudioRequests, success_key
from messagebox.played_history import archive_played_file, recent_reply_recipient
from messagebox.listened_receipts import AnnouncementGate, ReceiptStore, parse_wacli_send_id
from messagebox.contacts import ContactError, ContactStore
from messagebox.nfc_state import AnnouncementStore, NfcError, SelectionStore, active_selection, claim_selection
from messagebox.runtime_paths import APP_DIR, OUTBOX_DIR as DEFAULT_OUTBOX_DIR
from messagebox.runtime_paths import QUEUE_DIR as DEFAULT_QUEUE_DIR
from messagebox.runtime_paths import (
    CONTACTS_FILE as LOCAL_CONTACTS_FILE,
    NFC_ANNOUNCEMENT_FILE,
    NFC_HEALTH_FILE,
    NFC_SELECTION_FILE,
    RUNTIME_DIR,
    STATE_DIR as DEFAULT_STATE_DIR,
)
from messagebox.settings import (
    RINGTONES, SettingsReader, load_ring_lamp_schedule, normalize_ringtone_id, ringtone_path,
)
from messagebox.cloud_device import CloudDeviceClient, CloudDeviceError, CloudSendRejected, CloudSendUncertain, CloudVoiceNotFound, atomic_json
from messagebox import cloud_runtime, cloud_claim, sound_pack
from messagebox.cloud_runtime import CloudRuntimeError


MIC_DEV = os.environ.get("MSGBOX_MIC_DEV", "plughw:CARD=Device,DEV=0")
SPK_DEV = os.environ.get("MSGBOX_SPK_DEV", "plughw:CARD=Device_1,DEV=0")
SPEAKER_CARD = os.environ.get("MSGBOX_SPEAKER_CARD", "Device")
SPEAKER_CONTROL = os.environ.get("MSGBOX_SPEAKER_CONTROL", "PCM")
BUTTON_PIN = int(os.environ.get("MSGBOX_BUTTON_PIN", "17"))
LED_PIN = int(os.environ.get("MSGBOX_LED_PIN", "26"))
# Sync owns the store continuously. An immediate lock failure lets wacli
# delegate supported sends to its active sync connection without delaying them.
SEND_LOCK_WAIT = "0s"
WACLI_BIN = "/usr/local/bin/wacli"
QUEUE_DIR = str(DEFAULT_QUEUE_DIR)
OUTBOX_DIR = str(DEFAULT_OUTBOX_DIR)
STATE_DIR = str(DEFAULT_STATE_DIR)
TEMP_DIR = os.path.join(str(RUNTIME_DIR), "guided-reply-tmp")
EVENTS_FILE = os.path.join(STATE_DIR, "events.jsonl")
LISTENED_DIR = os.path.join(STATE_DIR, "listened-receipts")
LISTENED_FALLBACK_WAV = os.environ.get(
    "MSGBOX_LISTENED_FALLBACK_WAV", str(sound_pack.voice_path("listened")),
)
SEND_SUCCESS_WAV = str(sound_pack.cue_path("sent"))
send_success_notices = queue.SimpleQueue()
cloud_audio_requests = AudioRequests(cloud_runtime.STATE_FILE.parent / "audio-requests")
CONTACTS_FILE = (cloud_runtime.CONTACTS_FILE if os.environ.get("MSGBOX_TRANSPORT") == "cloud"
                 else LOCAL_CONTACTS_FILE)

LISTENED_POLL_S = float(os.environ.get("MSGBOX_LISTENED_POLL_S", "0.2"))
LISTENED_RETRY_S = float(os.environ.get("MSGBOX_LISTENED_RETRY_S", "30"))
RING_REQUEST_FILE = str(RUNTIME_DIR / "ring-request")
NFC_SELECTION_TTL_S = float(os.environ.get("MSGBOX_NFC_SELECTION_TTL_S", "30"))
NFC_ANNOUNCEMENT_POLL_S = float(
    os.environ.get("MSGBOX_NFC_ANNOUNCEMENT_POLL_S", "0.1")
)
NFC_HEALTH_MAX_AGE_S = float(os.environ.get("MSGBOX_NFC_HEALTH_MAX_AGE_S", "5"))
PLACE_TOKEN_WAV = os.environ.get("MSGBOX_PLACE_TOKEN_WAV", str(sound_pack.voice_path("card-needed")))
GUIDED_SILENCE_SECONDS = float(os.environ.get("MSGBOX_GUIDED_SILENCE_SECONDS", "20"))
PROMPT_DIR = Path(os.environ.get("MSGBOX_PROMPT_DIR", str(sound_pack.SOUND_DIR / "voice")))
PROMPTS = {
    "reply": PROMPT_DIR / "voice-count-reply.wav",
    "standalone": PROMPT_DIR / "voice-count-new.wav",
    "send": PROMPT_DIR / "voice-ask-send-1.wav",
    "delete_warning": PROMPT_DIR / "voice-last-chance.wav",
    "not_sent": PROMPT_DIR / "voice-not-sent.wav",
}

# Quiet hours: lamp dark, no ringtone (messages still queue; a deliberate press
# still plays). Overnight arrivals do not ring when quiet hours end.
RING_PHRASE = [
    (True, 1.2),
    (True, 1.6),
    (False, 0.4),
    (True, 1.2),
    (True, 1.6),
    (False, 0.6),
] * 3
# The audible press acknowledgement doubles as the hold-intent window. A press
# released while the cue plays remains a playback tap; a press still held when
# it ends starts capture immediately after the speaker is quiet.
MIN_HOLD_S = 0.4
POLL_S = 0.005
SETTLE_OPEN_S = 0.5
CONFIRM_PRESS_S = 0.08
CONFIRM_RELEASE_S = 0.2
LED_REFRESH_S = 0.5
SEND_FAIL_BEEP_AT = 3
CUES = {name: sound_pack.cue_path(asset) for name, asset in {
    "press": "press", "nfc": "card", "ready": "ready", "online": "connected", "fail": "oops",
}.items()}
stuck_notices = queue.SimpleQueue()

# Registration failures share the unrecognized family-card tone.
NFC_UNKNOWN_BEEP = "fail"


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def log_event(event_type, **fields):
    """Append content-free operational events; audio is never inspected/logged."""
    try:
        fields["type"] = event_type
        fields["ts"] = time.time()
        append_event(EVENTS_FILE, fields)
    except Exception as exc:
        log(f"event log error: {exc}")


def quiet_hours(settings=None, now=None):
    settings = settings or caregiver_settings()
    quiet = settings["quiet_hours"]
    if not quiet["enabled"]:
        return False
    current = now or datetime.now(ZoneInfo(settings["timezone"]))
    minute = current.hour * 60 + current.minute
    start_hour, start_minute = (int(value) for value in quiet["start"].split(":"))
    end_hour, end_minute = (int(value) for value in quiet["end"].split(":"))
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


_applied_volume_revision = None


def apply_master_volume(settings=None):
    global _applied_volume_revision
    settings = settings or caregiver_settings()
    if settings["revision"] == _applied_volume_revision:
        return True
    result = subprocess.run(
        [
            "amixer",
            "-q",
            "-c",
            SPEAKER_CARD,
            "sset",
            SPEAKER_CONTROL,
            f'{settings["master_volume_percent"]}%',
            "unmute",
        ],
        check=False,
    )
    if result.returncode == 0:
        if transport_mode() == "cloud":
            try:
                cloud_runtime.atomic_json(cloud_runtime.APPLIED_FILE, {
                    "revision": settings["revision"], "settings": settings,
                    "boot_id": cloud_audio_requests.boot_id,
                    "applied_mono": time.monotonic(),
                    "settings_sound": _applied_volume_revision is not None and not quiet_hours(settings),
                })
            except OSError:
                return False
        _applied_volume_revision = settings["revision"]
        return True
    return False


def validate_sounds():
    sound_pack.validate_sounds()


def beep(name):
    return subprocess.run(["aplay", "-q", "-D", SPK_DEV, str(CUES[name])], check=True, timeout=5)


def play_moment(cue=None, voice=None):
    for path in ([sound_pack.cue_path(cue)] if cue else []) + ([sound_pack.voice_path(voice)] if voice else []):
        play_audio_ordinary(path)


def announce_first_online():
    if sound_pack.consume_moment("online"):
        play_moment(voice="online")


def announce_all_set():
    if not quiet_hours() and sound_pack.ALL_SET_REQUEST.is_file() and sound_pack.consume_moment("all_set"):
        play_moment("all_set", "all-set")


def announce_runtime_ready():
    """Signal readiness once without making audio availability a boot gate."""
    if quiet_hours():
        return
    try:
        result = beep("ready")
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, "aplay")
        log_event("runtime_ready_cue")
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"runtime ready cue unavailable: {exc}")
        log_event("runtime_ready_cue_unavailable", error=type(exc).__name__)


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


def compatible_legacy_outbox_files():
    mode = transport_mode()
    scope = None
    if mode == "cloud":
        try:
            scope = cloud_runtime.account_scope(fresh=True)
        except (CloudRuntimeError, OSError):
            return []
    return [
        name
        for name in legacy_outbox_files()
        if legacy_job_transport(os.path.join(OUTBOX_DIR, name)) == mode
        and (mode != "cloud" or
             (legacy_job_metadata(os.path.join(OUTBOX_DIR, name)) or {}).get("account_scope") == scope)
    ]


_recording = False
_guided_active = False
outbox_store = None
receipt_store = None
announcement_gate = AnnouncementGate(LISTENED_POLL_S, LISTENED_RETRY_S)
nfc_announcement_store = AnnouncementStore(NFC_ANNOUNCEMENT_FILE)
_last_nfc_announcement_poll = 0.0
settings_reader = SettingsReader()


def caregiver_settings():
    return settings_reader.snapshot()


TRANSPORTS = frozenset({"wacli", "cloud"})
# The Business bench adapter was removed. Its tag is still read from old
# recordings so they stay on the box and are never sent through another account.
RETIRED_TRANSPORTS = frozenset({"business"})
PERSON_JID = re.compile(r"[1-9][0-9]{6,14}@s\.whatsapp\.net")


def transport_mode():
    mode = os.environ.get("MSGBOX_TRANSPORT", "wacli")
    if mode not in TRANSPORTS:
        raise ValueError("unsupported message transport")
    return mode


def current_recipient_context(*, claim=False):
    """Resolve standalone routing from an authoritative contacts snapshot."""
    try:
        document = ContactStore(CONTACTS_FILE).load()
        contacts = document["contacts"]
        has_cards = any(contact["card_uids"] for contact in contacts.values())
        if has_cards:
            try:
                if time.time() - os.stat(NFC_HEALTH_FILE).st_mtime > NFC_HEALTH_MAX_AGE_S:
                    log("recipient unavailable: NFC reader health is stale")
                    return None
            except OSError:
                log("recipient unavailable: NFC reader health is unavailable")
                return None
            if SelectionStore(NFC_SELECTION_FILE).unknown_present():
                log("recipient unavailable: unrecognized card presentation")
                return None
            resolver = claim_selection if claim else active_selection
            selection_existed = Path(NFC_SELECTION_FILE).exists()
            context = resolver(
                CONTACTS_FILE,
                NFC_SELECTION_FILE,
                max_age=NFC_SELECTION_TTL_S,
            )
            if context is not None:
                return {
                    "contact": context["contact"],
                    "uid": context["uid"],
                    "via_card": True,
                }
            if selection_existed:
                log("recipient unavailable: stale card selection")
                return None
            if nfc_announcement_store.pending_action() in {"unknown", "invalid"}:
                log("recipient unavailable: unrecognized card presentation")
                return None
        default_recipient = document["default_recipient"]
        if default_recipient is None:
            log("recipient unavailable: no default configured")
            return None
        contact = contacts.get(default_recipient)
        if contact is None:
            log("recipient unavailable: default is invalid")
            return None
        return {
            "contact": {"jid": default_recipient, **contact},
            "via_card": False,
        }
    except (ContactError, NfcError, OSError) as exc:
        log(f"contact routing unavailable: {exc}")
        return None


def claim_fresh_card_intent():
    """Claim a fresh card selection before choosing inbound playback.

    The state distinguishes no scan, an already-stale scan, a claimed valid
    scan, and a valid scan that became invalid during the claim. Callers use
    that distinction to preserve normal playback while failing closed for
    outbound routing and claim races.
    """
    if not Path(NFC_SELECTION_FILE).exists():
        return "none", None
    pending = current_recipient_context(claim=False)
    if pending is None or not pending["via_card"]:
        # Consume stale state so it cannot keep blocking later interactions.
        current_recipient_context(claim=True)
        return "stale", None
    claimed = current_recipient_context(claim=True)
    if claimed is None or not claimed["via_card"]:
        log_event("nfc_selection_expired")
        return "expired", None
    return "claimed", claimed


def nfc_idle_routing_is_safe(contacts):
    """Reject recent/default routing while fitted NFC state is unsafe."""
    if not any(contact["card_uids"] for contact in contacts.values()):
        return True
    try:
        if time.time() - os.stat(NFC_HEALTH_FILE).st_mtime > NFC_HEALTH_MAX_AGE_S:
            log("recipient unavailable: NFC reader health is stale")
            return False
        if SelectionStore(NFC_SELECTION_FILE).unknown_present():
            log("recipient unavailable: unrecognized card presentation")
            return False
    except OSError:
        log("recipient unavailable: NFC reader state is unavailable")
        return False
    if nfc_announcement_store.pending_action() in {"unknown", "invalid"}:
        log("recipient unavailable: unrecognized card presentation")
        return False
    return True


def recording_recipient_context():
    """Prefer the exact recently played sender, then the configured default."""
    try:
        document = ContactStore(CONTACTS_FILE).load()
    except (ContactError, OSError):
        return None
    contacts = document["contacts"]
    # A presentation that arrives after the initial press check still owns the
    # interaction. Invalid or unsafe card state must never fall through to a
    # recent sender.
    if Path(NFC_SELECTION_FILE).exists():
        return current_recipient_context(claim=True)
    if not nfc_idle_routing_is_safe(contacts):
        return None

    route_state, recipient = recent_reply_recipient(QUEUE_DIR, contacts)
    if Path(NFC_SELECTION_FILE).exists():
        return current_recipient_context(claim=True)
    if not nfc_idle_routing_is_safe(contacts):
        return None
    if route_state == "route":
        return {
            "contact": {"jid": recipient, **contacts[recipient]},
            "via_card": False,
            "via_recent_reply": True,
        }
    if route_state == "blocked":
        log("recipient unavailable: recent reply route is no longer valid")
        return None
    return current_recipient_context(claim=True)


def routing_mode():
    """Describe startup routing without exposing a contact JID."""
    try:
        document = ContactStore(CONTACTS_FILE).load()
    except (ContactError, OSError) as exc:
        log(f"contact routing unavailable: {exc}")
        return "unavailable"
    if not document["contacts"]:
        return "no_contacts"
    if document["default_recipient"] is None:
        return "no_default"
    return "default_recipient"


def wait_for_hold_intent(
    is_pressed,
    minimum_hold_s,
    poll_s,
    *,
    started_at=None,
    monotonic=time.monotonic,
    sleeper=time.sleep,
):
    """Classify the shared button before resolving or claiming a recipient."""
    if minimum_hold_s <= 0 or poll_s <= 0:
        raise ValueError("button timing values must be positive")
    started = monotonic() if started_at is None else started_at
    while monotonic() - started < minimum_hold_s:
        if not is_pressed():
            return "play"
        sleeper(poll_s)
    return "record" if is_pressed() else "play"


def acknowledge_and_classify_legacy_press(pressed_at=None):
    """Acknowledge the initial press, then classify it after the cue ends."""
    started = time.monotonic() if pressed_at is None else pressed_at
    beep("press")
    log_event("legacy_press")
    return wait_for_hold_intent(
        lambda: button.is_pressed,
        MIN_HOLD_S,
        POLL_S,
        started_at=started,
    )


def prompt_for_token():
    """Refuse outbound recording without leaking the previous selection."""
    log_event("nfc_token_required")
    if PLACE_TOKEN_WAV == str(sound_pack.voice_path("card-needed")):
        play_moment(voice="card-needed")
    else:
        play_audio_ordinary(PLACE_TOKEN_WAV)


def block_unavailable_recipient():
    """Give safe feedback without treating an unconfigured box as card-ready."""
    mode = routing_mode()
    log_event("recipient_required", routing_mode=mode)
    if mode in {"card_selection", "default_recipient"}:
        if play_pending_nfc_announcement(force=True) != "unknown":
            if mode == "card_selection":
                prompt_for_token()
            else:
                play_moment("oops", "fail")
    else:
        play_moment("oops", "fail")


def _play_nfc_prompt(uid, action, card_clip):
    if action == "unknown":
        play_moment("oops")
        play_audio_ordinary(card_clip or sound_pack.voice_path("card-unknown"))
        nfc_announcement_store.acknowledge(uid)
        log_event("nfc_announced", action=action, played=True, mode="spoken")
        return True
    beeped = False
    if caregiver_settings()["nfc_confirmation_beep"]:
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
        beep(NFC_UNKNOWN_BEEP)
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


def legacy_job_metadata(path):
    try:
        with open(path + ".json", encoding="utf-8") as handle:
            metadata = json.load(handle)
        return metadata if isinstance(metadata, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def legacy_job_recipient(path):
    """Use only the recipient snapshot bound at recording time."""
    try:
        recipient = (legacy_job_metadata(path) or {}).get("recipient")
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


def legacy_job_transport(path):
    metadata = legacy_job_metadata(path)
    if metadata is None:
        return None
    # Sidecars deployed before transport metadata existed belong to wacli.
    # Retired transports stay recognized so their recordings are kept, unsent.
    transport = metadata.get("transport", "wacli")
    return (
        transport
        if isinstance(transport, str) and transport in TRANSPORTS | RETIRED_TRANSPORTS
        else None
    )


def bind_legacy_job_recipient(path, recipient, *, account_scope=None):
    """Persist routing before the WAV becomes visible to the sender thread."""
    metadata = {"version": 1, "recipient": recipient, "transport": transport_mode()}
    if metadata["transport"] == "cloud":
        if not valid_account_scope(account_scope):
            raise ValueError("cloud recording account is unavailable")
        metadata["account_scope"] = account_scope
    metadata_path = path + ".json"
    temporary_path = metadata_path + ".part"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(
            metadata,
            handle,
            sort_keys=True,
        )
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
    if transport_mode() != "wacli" or legacy_job_transport(path) != "wacli":
        log_event("send_blocked", flow="legacy", reason="transport")
        return False
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
            SEND_LOCK_WAIT,
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
        send_success_notices.put(time.monotonic())
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


def stage_hold_release_cloud_job(fname):
    """Move an approved hold-release WAV into the keyed, durable send queue.

    The stable ID makes a crash after approving the job but before removing the
    source harmless: the next attempt finds the same job and never changes its
    recipient. The source WAV is removed before the sender can complete the job.
    """
    path = os.path.join(OUTBOX_DIR, fname)
    mode = transport_mode()
    if mode != "cloud" or legacy_job_transport(path) != mode:
        log_event("send_blocked", flow="hold_release", reason="transport")
        return False
    recipient = legacy_job_recipient(path)
    if recipient is None:
        log_event("send_blocked", flow="hold_release", reason="missing_recipient")
        return False
    try:
        if not PERSON_JID.fullmatch(recipient):
            raise ValueError("unsupported recipient")
        milliseconds, _, raw_duration = fname[:-4].partition("-")
        duration = float(raw_duration)
        if not milliseconds.isdecimal() or duration <= 0:
            raise ValueError("invalid hold-release filename")
        message_id = "hold_" + hashlib.sha256(fname.encode("ascii")).hexdigest()[:48]
        # Never adopt an older recording into the currently linked account.
        scope = (legacy_job_metadata(path) or {}).get("account_scope")
        outbox_store.approve(path, recipient, "hold_release", duration, message_id,
                             account_scope=scope)
    except (OSError, ValueError):
        log_event("send_blocked", flow="hold_release", reason="staging")
        return False
    try:
        os.remove(path)
    except OSError:
        # The approved job and the source coexist after an interrupted move.
        # Keep the sender stopped until the source is removed on a later pass.
        log_event("send_blocked", flow="hold_release", reason="source_cleanup")
        return False
    try:
        Path(path + ".json").unlink(missing_ok=True)
    except OSError:
        # The WAV is gone and the durable job is the only sendable copy.
        pass
    return True


def send_guided_job(job):
    if transport_mode() == "cloud":
        try:
            with cloud_outbox_lock(job.path) as acquired:
                return _send_guided_job(job) if acquired else False
        except (OSError, ValueError, CloudDeviceError, subprocess.SubprocessError):
            log_event("send_blocked", flow=job.flow_kind, reason="cloud_payload")
            return False
    return _send_guided_job(job)


def _send_guided_job(job):
    """Send only to the recipient stored atomically with this approved audio."""
    mode = transport_mode()
    try:
        durable_job = outbox_store.load(job.path)
    except (AttributeError, OSError, ValueError, KeyError, json.JSONDecodeError):
        log_event("send_blocked", flow=job.flow_kind, reason="metadata")
        return False
    if durable_job.message_id != job.message_id or durable_job.transport != mode:
        log_event("send_blocked", flow=job.flow_kind, reason="transport")
        return False
    job = durable_job
    if mode == "cloud":
        try:
            if (not valid_account_scope(job.account_scope)
                    or job.account_scope != cloud_runtime.account_scope(fresh=True)):
                raise CloudRuntimeError("recording belongs to another account")
        except (CloudRuntimeError, OSError):
            log_event("send_blocked", flow=job.flow_kind, reason="account_scope")
            return False
    if mode == "cloud" and job.state != "pending":
        return True
    if mode == "cloud":
        metadata = json.loads((job.path / "job.json").read_text(encoding="utf-8"))
        if "cloud_upload" in metadata:
            return _send_cloud_upload(job)
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

    if mode == "cloud":
        try:
            target = cloud_runtime.recipient_id(job.recipient)
            outbox_store.prepare_cloud_upload(job, ogg, target, cloud_runtime.outbox_retry_until())
        except (CloudRuntimeError, CloudDeviceError, OSError, ValueError):
            outbox_store.set_state(job, "failed", increment_attempts=True)
            log_event("outbox_failed", flow=job.flow_kind, reason="cloud_preflight")
            return True
        finally:
            Path(ogg).unlink(missing_ok=True)
        return _send_cloud_upload(job)

    # Persist 'sending' before crossing the external side-effect boundary.  A
    # crash from here until completion becomes uncertain on restart, never an
    # automatic duplicate resend.
    job = outbox_store.set_state(job, "sending", increment_attempts=True)
    sent = subprocess.run(
        voice_send_command(WACLI_BIN, ogg, job.recipient, SEND_LOCK_WAIT),
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
        send_success_notices.put(time.monotonic())
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


def _send_cloud_upload(job):
    metadata_path = job.path / "job.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    try:
        ogg, upload = cloud_upload_payload(job.path, metadata)
        if (upload["recipient_id"] != cloud_runtime.recipient_id(job.recipient)
                or cloud_runtime.outbox_now() >= upload["retry_until"]):
            raise CloudRuntimeError("cloud upload authorization changed")
        client = CloudDeviceClient.from_environment()
    except (CloudRuntimeError, CloudDeviceError, OSError, ValueError):
        outbox_store.set_state(job, "uncertain" if metadata.get("attempts", 0) else "failed")
        log_event("send_blocked", flow=job.flow_kind, reason="cloud_payload")
        return True
    # A delayed first request may have committed since recovery's 404. Read
    # again before replaying; the server key still deduplicates the remaining race.
    result = None
    prior_attempts = metadata.get("attempts", 0)
    if prior_attempts:
        try:
            result = client.voice_status(job.message_id)
        except CloudVoiceNotFound:
            pass
        except CloudDeviceError:
            return False
    if result is None:
        job = outbox_store.set_state(job, "sending", increment_attempts=True)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["cloud_send_started_at"] = int(cloud_runtime.outbox_now())
        atomic_json(metadata_path, metadata)
        try:
            result = client.send_voice(ogg, upload["recipient_id"], upload["idempotency_key"],
                                       upload["duration"], account_scope=upload["account_scope"])
        except CloudSendRejected:
            # A racing original request can still succeed after a rejected
            # retry. Keep ambiguous attempts available to keyed reconciliation.
            outbox_store.set_state(job, "uncertain" if prior_attempts else "failed")
            log_event("outbox_failed", flow=job.flow_kind, reason="cloud_rejected")
            return True
        except CloudSendUncertain:
            outbox_store.set_state(job, "uncertain")
            log_event("outbox_uncertain", flow=job.flow_kind, reason="cloud_send")
            return True
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    observed = metadata.get("cloud_status_checked_at", metadata.get("cloud_send_started_at"))
    if (result["state"] in {"accepted", "delivered", "read"}
            and result.get("deleted") is not True
            and result["server_time"] < result["expires_at"]
            and type(observed) in (int, float)
            and 0 <= result["server_time"] - observed <= 30):
        try:
            remaining = result["server_time"] + 30 - cloud_runtime.outbox_now()
        except (CloudRuntimeError, OSError):
            pass  # A stale heartbeat can suppress sound, never the accepted outcome.
        else:
            cloud_audio_requests.enqueue_for(success_key(job.account_scope, job.message_id),
                "success", job.account_scope, remaining)
    metadata.update({"state": "cloud_retained", "cloud_message_id": result["message_id"],
                     "cloud_state": result["state"], "expires_at": result["expires_at"],
                     "server_time": result["server_time"],
                     "cloud_status_checked_at": result["server_time"]})
    atomic_json(metadata_path, metadata)
    if result["state"] == "delivery_uncertain":
        log_event("outbox_uncertain", flow=job.flow_kind, reason="cloud_delivery")
        return True
    log_event("cloud_uploaded", flow=job.flow_kind, state=result["state"], dur=job.duration)
    return True


def compatible_guided_jobs():
    jobs = outbox_store.jobs()
    if transport_mode() != "cloud":
        return jobs
    try:
        scope = cloud_runtime.account_scope(fresh=True)
    except (CloudRuntimeError, OSError):
        return []
    # Preserve unbound/foreign jobs without starving this account's recordings.
    return [job for job in jobs if job.account_scope == scope]


def sender_loop():
    failures = 0
    current = None
    while True:
        if transport_mode() == "cloud":
            staged = all(stage_hold_release_cloud_job(filename)
                         for filename in compatible_legacy_outbox_files())
            if not staged:
                time.sleep(5)
                continue
        guided = compatible_guided_jobs()
        legacy = compatible_legacy_outbox_files() if not guided else []
        job = guided[0] if guided else (legacy[0] if legacy else None)
        key = ("guided", job.message_id) if guided else (("legacy", job) if legacy else None)
        if key != current:
            current, failures = key, 0
        if job is None:
            time.sleep(0.5)
            continue
        sent = send_guided_job(job) if guided else send_legacy_outbox_file(job)
        if sent:
            current, failures = None, 0
            continue
        failures += 1
        if failures == SEND_FAIL_BEEP_AT:
            stuck_notices.put(key)
            log_event("send_failed", flow=key[0], reason="send")
        time.sleep(min(60, 5 * failures))


def maybe_play_still_trying():
    if _recording or _guided_active or button.is_pressed or quiet_hours():
        return False
    try:
        key = stuck_notices.get_nowait()
    except queue.Empty:
        return False
    # Drop notices if this job has since succeeded; receipt survives restart.
    jobs = compatible_guided_jobs() if key[0] == "guided" else compatible_legacy_outbox_files()
    present = any(job.message_id == key[1] for job in jobs) if key[0] == "guided" else key[1] in jobs
    digest = hashlib.sha256(str(key).encode()).hexdigest()
    if not present or not sound_pack.consume_moment("stuck_" + digest):
        return False
    play_moment("still_trying", "stuck")
    return True


_offline_announced = False
_online_announced = False


def maybe_play_connectivity():
    """Reuse persisted heartbeat age; never probe the network for a sound."""
    global _offline_announced, _online_announced
    if transport_mode() != "cloud" or _recording or _guided_active or button.is_pressed:
        return False
    runtime = cloud_runtime.CloudRuntime(client=object(), state_path=cloud_runtime.STATE_FILE)
    snapshot = runtime.state.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("boot_id") != runtime.boot_id:
        return False
    verified = snapshot.get("verified_mono")
    if type(verified) not in (int, float):
        return False
    age = time.monotonic() - verified
    if 0 <= age <= 90:
        _offline_announced = False
        if not quiet_hours() and not _online_announced:
            play_moment("connected")
            announce_first_online()
            _online_announced = True
            return True
    if age > 90:
        _online_announced = False
    if age >= 180 and not _offline_announced and not quiet_hours():
        token = f"{runtime.boot_id}:{verified}"
        if sound_pack.consume_moment("offline", token=token):
            play_moment("offline")
            _offline_announced = True
            return True
    return False


_led_last = 0.0


def refresh_led(force=False, settings=None):
    global _led_last
    now = time.monotonic()
    if not force and now - _led_last < LED_REFRESH_S:
        return
    _led_last = now
    settings = settings or caregiver_settings()
    lamp_enabled = settings["arrival_signal"] in {"ring_and_lamp", "lamp_only"}
    (led.on if queued() and lamp_enabled and not quiet_hours(settings) else led.off)()


def ring_windows():
    windows, elapsed = [], 0.0
    for is_note, duration in RING_PHRASE:
        if is_note:
            windows.append((elapsed, elapsed + min(0.8, duration / 2)))
        elapsed += duration
    return windows


RING_WINS = ring_windows()


@lru_cache(maxsize=len(RINGTONES))
def ring_lamp_schedule(ringtone_id):
    if ringtone_id not in RINGTONES:
        return None
    try:
        return load_ring_lamp_schedule(APP_DIR / "ringtones" / f"{ringtone_id}.lamp.json", ringtone_id)
    except (OSError, ValueError):
        return None


def ring_lamp_on(elapsed, ringtone_id):
    schedule = ring_lamp_schedule(normalize_ringtone_id(ringtone_id))
    if schedule is not None:
        return any(start <= elapsed < end for start, end in schedule)
    if ringtone_id != "ding_dong":
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
    settings = caregiver_settings()
    if fresh and not quiet_hours(settings) and settings["arrival_signal"] != "silent":
        ring_alert(settings=settings)


def maybe_manual_ring():
    if not os.path.exists(RING_REQUEST_FILE):
        return
    # Keep the marker in place so a crash cannot lose the request and repeated
    # dashboard taps stay idempotent until playback finishes.
    if not ring_alert(source="dashboard"):
        return
    try:
        os.remove(RING_REQUEST_FILE)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"manual ring request error: {exc}")


def ring_alert(source="new_message", settings=None):
    settings = settings or caregiver_settings()
    path = os.fspath(ringtone_path(settings))
    manual = source == "dashboard"
    signal = "ring_and_lamp" if manual else settings["arrival_signal"]
    play_ring = signal in {"ring_and_lamp", "ring_only"}
    flash_lamp = signal in {"ring_and_lamp", "lamp_only"}
    if play_ring and not os.path.exists(path):
        log(f"ring skipped ({source}): ringtone unavailable")
        return False
    log(f"ringing: {source}")
    log_event("ring", source=source)
    process = subprocess.Popen(["aplay", "-q", "-D", SPK_DEV, path]) if play_ring else None
    started = time.monotonic()
    try:
        while (process is not None and process.poll() is None) or (
            process is None and time.monotonic() - started < 9
        ):
            elapsed = time.monotonic() - started
            (led.on if flash_lamp and ring_lamp_on(elapsed, settings["ringtone_id"]) else led.off)()
            if button.is_pressed:
                if process is not None:
                    process.terminate()
                log("ring cut by press")
                break
            time.sleep(0.02)
    finally:
        if process is not None and process.poll() is None:
            process.wait()
        refresh_led(force=True)
    return process is None or process.returncode == 0


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
    for name in queued():
        source = Path(QUEUE_DIR) / name
        metadata = queue_metadata(source)
        if not inbound_audio_authorized(metadata):
            continue
        claimed = claim_inbox_file(QUEUE_DIR, source.name)
        return {"path": claimed, "meta": metadata}
    return None


def finish_claim(claim):
    cloud = transport_mode() == "cloud" and (claim.get("meta") or {}).get("cloud") is True
    archive_played_file(
        QUEUE_DIR, claim["path"], metadata=claim.get("meta"),
        played_at=claim.get("played_at"),
        **({"retention_seconds": 91 * 86400, "metadata_limit": 1000000,
            "media_limit": 1000000, "media_bytes_limit": 1 << 60} if cloud else {}),
    )
    if cloud and claim.get("played_at"):
        cloud_runtime.record_played(claim["meta"])


def release_claim(claim):
    release_inbox_file(QUEUE_DIR, claim["path"])


def inbound_audio_authorized(metadata):
    if transport_mode() == "cloud":
        return cloud_runtime.playable(metadata)
    return not (isinstance(metadata, dict) and metadata.get("cloud") is True)


def react_played(meta):
    if not meta or not meta.get("msgid") or not meta.get("chat"):
        return
    if transport_mode() == "cloud":
        # The Cloud API played reaction is a separate, later integration.
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
            SEND_LOCK_WAIT,
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
    if quiet_hours():
        return 0
    played = 0
    for _ in range(max(0, limit)):
        notice = receipt_store.claim_next()
        if notice is None:
            break
        clip = notice.clip or LISTENED_FALLBACK_WAV
        # Pending receipts from prior releases persist the old bundled default.
        if clip == str(APP_DIR / "sounds/listen-receipts/someone-listened.wav"):
            clip = LISTENED_FALLBACK_WAV
        if not os.path.isabs(clip) or not os.path.exists(clip):
            receipt_store.release(notice)
            announcement_gate.blocked()
            if unavailable_events.unavailable("listen_announcement"):
                log_event("listen_announcement_blocked", reason="missing_clip")
                log("listen announcement blocked: missing clip")
            break
        try:
            play_moment("listened")
            play_audio_ordinary(clip)
            receipt_store.complete(notice)
            played += 1
            unavailable_events.available("listen_announcement")
            log_event("listen_announced", listener=notice.listener_name)
            log(f"announced listened receipt: {notice.listener_name}")
        except Exception as exc:
            receipt_store.release(notice)
            announcement_gate.blocked()
            if unavailable_events.unavailable("listen_announcement"):
                log_event("listen_announcement_blocked", reason=type(exc).__name__)
                log("listen announcement audio unavailable")
            break
    return played


def play_idle_sound(path, timeout):
    """Yield to a press and release the speaker before recording can start."""
    if button.is_pressed:
        return False
    process = subprocess.Popen(["aplay", "-q", "-D", SPK_DEV, str(path)])
    deadline = time.monotonic() + timeout
    try:
        while (code := process.poll()) is None:
            if button.is_pressed:
                return False
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("aplay", timeout)
            time.sleep(POLL_S)
        if code:
            raise subprocess.CalledProcessError(code, "aplay")
        return True
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.2)


def play_send_success_cue():
    if not caregiver_settings().get("swoosh_sound_enabled", True):
        return False
    return play_idle_sound(SEND_SUCCESS_WAV, 5)


unavailable_events = UnavailableEvents()


def maybe_play_cloud_sound():
    """Only this main-loop owner starts cloud sounds, never the poller."""
    if transport_mode() != "cloud" or _recording or _guided_active or button.is_pressed:
        return False
    try:
        scope = cloud_runtime.account_scope(fresh=True)
        with cloud_audio_requests.owner() as acquired:
            if not acquired:
                unavailable_events.available("cloud_scope")
                return False
            request = cloud_audio_requests.claim_next(scope)
            unavailable_events.available("cloud_scope")
            if request is None:
                return False
            try:
                if request["kind"] == "success":
                    played = play_send_success_cue()
                elif request["kind"] == "settings_saved":
                    # The main loop applies volume before consuming these requests.
                    played = (not quiet_hours() and apply_master_volume()
                              and play_idle_sound(sound_pack.cue_path("card_saved"), 5))
                    if not played and not quiet_hours():
                        cloud_audio_requests.finish(request, "pending")
                        return False
                elif request["kind"] == "preview" and normalize_ringtone_id(request["ringtone_id"]) in cloud_runtime.RINGTONES:
                    path = cloud_runtime.RINGTONE_DIR / cloud_runtime.RINGTONES[normalize_ringtone_id(request["ringtone_id"])]
                    played = play_idle_sound(path, cloud_runtime._ringtone_preview_timeout(path))
                else:
                    played = False
                cloud_audio_requests.finish(request, "played" if played else "rejected")
                unavailable_events.available("cloud_playback")
                return played
            except (OSError, subprocess.SubprocessError, CloudRuntimeError):
                cloud_audio_requests.finish(request, "rejected")
                if unavailable_events.unavailable("cloud_playback"):
                    log_event("cloud_sound_unavailable")
    except (OSError, ValueError, CloudRuntimeError):
        if unavailable_events.unavailable("cloud_scope"):
            log_event("cloud_sound_unavailable")
    return False


def maybe_play_send_success():
    """The main audio owner plays accepted-send cues only while idle."""
    if _recording or _guided_active or button.is_pressed:
        return False
    try:
        accepted_at = send_success_notices.get_nowait()
    except queue.Empty:
        return False
    # A late cue could be mistaken for confirmation of a newer recording.
    if time.monotonic() - accepted_at > 30:
        return False
    try:
        play_send_success_cue()
    except (OSError, subprocess.SubprocessError):
        # Audio failure must never turn an accepted message into a retry.
        log_event("send_cue_unavailable")
        return False
    return True


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


def play_audio_for_approval(path, session_id=None, *, action="approve_warning"):
    """Consume only a fresh deliberate press in review or deletion warning."""
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
        acknowledge_guided_press(action, session_id)
    wait_for_stable_open()
    return approved


def play_warning_for_approval(path, session_id=None):
    return play_audio_for_approval(path, session_id)


def presence(kind, recipient):
    if transport_mode() == "cloud":
        return
    subcommand = ["typing", "--media", "audio"] if kind == "recording" else ["paused"]
    subprocess.Popen(
        [WACLI_BIN, "presence", *subcommand, "--to", recipient, "--lock-wait", SEND_LOCK_WAIT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def cleanup_temp_recordings():
    os.makedirs(TEMP_DIR, exist_ok=True)
    for path in Path(TEMP_DIR).iterdir():
        if path.is_file() and path.suffix in {".raw", ".wav", ".ogg", ".part"}:
            path.unlink()


class RecordingLimitCue:
    def __init__(self, started, maximum):
        self.started = started
        self.maximum = maximum
        self.process = None
        self.intervals = []

    def update(self, now):
        if not self.intervals and now - self.started >= self.maximum - 5:
            self.intervals.append([now - self.started, None])
            try:
                self.process = subprocess.Popen(["aplay", "-q", "-D", SPK_DEV,
                                                 str(sound_pack.cue_path("rec_limit"))])
            except OSError:
                self.intervals[-1][1] = now - self.started
                log_event("recording_limit_cue_unavailable")
        if self.process is not None:
            code = self.process.poll()
            if code is not None:
                self.intervals[-1][1] = now - self.started
                self.process = None
                if code:
                    log_event("recording_limit_cue_unavailable")
            elif now - self.started - self.intervals[-1][0] > 3:
                self.finish(now)
                log_event("recording_limit_cue_unavailable")

    def finish(self, now):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=0.2)
            self.process = None
        if self.intervals and self.intervals[-1][1] is None:
            self.intervals[-1][1] = now - self.started


def capture_guided_recording(recipient, session_id=None, max_seconds=60):
    global _recording
    # Synchronous playback must complete before the microphone is opened.
    play_moment("rec_go")
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
    _recording = True
    led.on()
    started = time.monotonic()
    vad.start(started)
    warning = RecordingLimitCue(started, max_seconds)
    presence("recording", recipient)
    presence_last = started
    closed_since = None
    stopped_by_press = False
    try:
        with open(raw_path, "wb") as raw:
            while True:
                now = time.monotonic()
                warning.update(now)
                readable, _, _ = select.select([process.stdout], [], [], 0.02)
                if readable:
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if chunk:
                        raw.write(chunk)
                        vad.feed(b"\0" * len(chunk) if warning.process else chunk, now=now)
                if button.is_pressed:
                    closed_since = closed_since or now
                    if now - closed_since >= CONFIRM_PRESS_S:
                        stopped_by_press = True
                        break
                else:
                    closed_since = None
                if vad.silence_expired(now):
                    break
                if now - started >= max_seconds:
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
        warning.finish(time.monotonic())
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
    def __init__(self, recipient, session_id, max_seconds):
        self.recipient = recipient
        self.session_id = session_id
        self.max_seconds = max_seconds

    def play_ordinary(self, path):
        if str(path) == str(PROMPTS["send"]):
            path = PROMPT_DIR / sound_pack.next_send_prompt().name
        play_audio_ordinary(path)

    def play_review_for_approval(self, path):
        play_moment(voice="review")
        return play_audio_for_approval(path, self.session_id, action="approve_review")

    def record(self):
        return capture_guided_recording(
            self.recipient, self.session_id, self.max_seconds
        )

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
    selected = None
    for name in names:
        path = Path(QUEUE_DIR) / name
        metadata = queue_metadata(path)
        if inbound_audio_authorized(metadata):
            selected = (path, metadata)
            break
    if selected is None:
        return
    path, meta = selected
    log(f"playing {path.name} ({len(names)} waiting)")
    try:
        play_moment("msg_start", "msg-start")
        subprocess.run(["aplay", "-q", "-D", SPK_DEV, str(path)], check=True, timeout=600)
        if len(names) == 1:
            play_moment("msg_end")
        played_at = time.time()
        archive_played_file(
            QUEUE_DIR, path, metadata=meta, played_at=played_at,
            **({"retention_seconds": 91 * 86400, "metadata_limit": 1000000,
                "media_limit": 1000000, "media_bytes_limit": 1 << 60}
               if transport_mode() == "cloud" else {}),
        )
        if transport_mode() == "cloud":
            cloud_runtime.record_played(meta)
        react_played(meta)
        try:
            wait_s = time.time() - int(path.name.split("-", 1)[0]) / 1000
        except ValueError:
            wait_s = None
        log_event("played", wait_s=wait_s)
    except Exception as exc:
        log(f"play error: {exc}")
        beep("fail")
    wait_for_stable_open()
    refresh_led(force=True)


def record_and_send_legacy(settings=None, pressed_at=None):
    global _recording
    settings = settings or caregiver_settings()
    max_seconds = settings["max_recording_seconds"]
    scope = cloud_runtime.account_scope() if transport_mode() == "cloud" else None
    card_state, context = claim_fresh_card_intent()
    intent = acknowledge_and_classify_legacy_press(pressed_at)
    if intent == "play":
        wait_for_stable_open()
        if card_state in {"claimed", "expired"}:
            # Hold-to-record still needs a hold. A quick release consumes the
            # one-shot choice without playing an unrelated queued message or
            # creating a near-silent recording.
            log_event("nfc_recording_cancelled", reason="short_press")
            return
        play_next_legacy()
        return
    if card_state in {"stale", "expired"}:
        block_unavailable_recipient()
        return
    if card_state == "none":
        context = recording_recipient_context()
    if context is None:
        block_unavailable_recipient()
        return
    recipient = context["contact"]["jid"]
    if context["via_card"] and not ensure_nfc_confirmation(context):
        log_event("nfc_confirmation_failed")
        return
    if context["via_card"] and not button.is_pressed:
        return
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
            str(max_seconds + 2),
            part,
        ]
    )
    _recording = True
    led.on()
    started = time.monotonic()
    warning = RecordingLimitCue(started, max_seconds)
    open_since = None
    presence_last = None
    try:
        while True:
            time.sleep(POLL_S)
            now = time.monotonic()
            warning.update(now)
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
            if now - started >= max_seconds:
                break
        held = min(time.monotonic() - started, max_seconds)
        led.off()
        recorder.send_signal(signal.SIGINT)
        recorder.wait()
        warning.finish(time.monotonic())
        if presence_last:
            presence("paused", recipient)
        final_path = part[:-5] + f"-{held:.1f}.wav"
        bind_legacy_job_recipient(final_path, recipient, account_scope=scope)
        os.replace(part, final_path)
    finally:
        warning.finish(time.monotonic())
        if recorder.poll() is None:
            recorder.send_signal(signal.SIGINT)
            recorder.wait(timeout=3)
        led.off()
        _recording = False


def run_guided_once(settings=None):
    global _guided_active
    settings = settings or caregiver_settings()
    session_id = uuid.uuid4().hex
    scope = cloud_runtime.account_scope() if transport_mode() == "cloud" else None
    card_state, context = claim_fresh_card_intent()
    if card_state == "expired":
        block_unavailable_recipient()
        return
    claim = None if card_state == "claimed" else claim_oldest()
    if card_state == "stale" and not claim:
        block_unavailable_recipient()
        return
    flow_kind = "reply" if claim else "standalone"
    metadata = claim["meta"] if claim else None
    if not claim and card_state == "none":
        context = recording_recipient_context()
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
    claim_recipient_allowed = False
    if claim and recipient:
        try:
            claim_recipient_allowed = recipient in ContactStore(CONTACTS_FILE).allowed_jids()
        except (ContactError, OSError):
            claim_recipient_allowed = False
    if claim and (not recipient or not claim_recipient_allowed):
        # The message may be heard, but a reply is never guessed or rerouted.
        try:
            if not inbound_audio_authorized(metadata):
                release_claim(claim)
                return
            play_moment("msg_start", "msg-start")
            play_audio_ordinary(claim["path"])
            if not queued():
                play_moment("msg_end")
            claim["played_at"] = time.time()
            finish_claim(claim)
            log_event("guided_unroutable_inbound")
        except Exception:
            release_claim(claim)
            raise
        return

    led.off()
    io = PiGuidedIO(recipient, session_id, settings["max_recording_seconds"])

    def session_event(kind, **data):
        if kind == "guided_recording_empty":
            play_moment("oops", "empty")
        if kind == "guided_session_started" and claim:
            data["source_file"] = claim["path"].name
        log_event(kind, **data)
        if kind == "guided_inbound_played":
            claim["played_at"] = time.time()
            react_played(metadata)

    session = GuidedSession(io, outbox_store, session_event)
    _guided_active = True
    outcome = None
    try:
        # Catch a receipt that arrived after the idle loop saw this press. The
        # announcement finishes before the requested child interaction begins.
        play_pending_listened()
        if claim and not inbound_audio_authorized(metadata):
            release_claim(claim)
            return
        outcome = session.run(
            recipient=recipient,
            flow_kind=flow_kind,
            countdown_path=str(PROMPTS["reply" if claim else "standalone"]),
            send_prompt_path=str(PROMPTS["send"]),
            delete_warning_path=str(PROMPTS["delete_warning"]),
            not_sent_path=str(PROMPTS["not_sent"]),
            incoming_path=str(claim["path"]) if claim else None,
            session_id=session_id,
            auto_record_after_incoming=settings["after_listening"] == "invite_reply",
            account_scope=scope,
            incoming_cue_path=str(sound_pack.cue_path("msg_start")),
            incoming_voice_path=str(sound_pack.voice_path("msg-start")),
            incoming_end_path=str(sound_pack.cue_path("msg_end")) if not queued() else None,
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
            outcome, bool(queued()), quiet_hours(settings)
        )
        mark_queue_known()
        refresh_led(force=True)
        if ring_waiting:
            ring_alert(source="queued_after_unsent")


def validate_prompts():
    invalid = invalid_prompt_files(PROMPT_DIR / f"voice-{name}.wav" for name in sound_pack.VOICE_NAMES)
    if invalid:
        raise RuntimeError(
            "guided reply prompt assets missing/invalid: " + ", ".join(invalid)
        )


def claim_beeps():
    try:
        validate_sounds()
    except ValueError:
        log("claim sound assets missing/invalid")
    return {name: CUES[name] for name in ("press", "fail", "online")}


def play_claim_cue(cues, name):
    try:
        subprocess.run(["aplay", "-q", "-D", SPK_DEV, str(cues[name])], check=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        log("claim audio playback unavailable")
        return False


def claim_button_press(cues):
    play_claim_cue(cues, "press")
    result = cloud_claim.claim_press_result()
    if result in {cloud_claim.ClaimPressResult.NOT_ACTIVE, cloud_claim.ClaimPressResult.RETRY}:
        play_claim_cue(cues, NFC_UNKNOWN_BEEP)


_setup_online_failed = False


def play_setup_online(cues):
    global _setup_online_failed
    if quiet_hours():
        return
    try:
        if cloud_claim.setup_online_cue(consume=True):
            if play_claim_cue(cues, "online"):
                # Claim mode has no Button instance used by ordinary playback.
                if sound_pack.consume_moment("online"):
                    subprocess.run(["aplay", "-q", "-D", SPK_DEV,
                                    str(sound_pack.voice_path("online"))], check=True, timeout=5)
        _setup_online_failed = False
    except (OSError, ValueError, CloudDeviceError, subprocess.SubprocessError):
        # This runs on every idle poll; report a failure once until it recovers.
        if not _setup_online_failed:
            log("setup online audio unavailable")
        _setup_online_failed = True


def claim_only_loop():
    """Physical possession confirmation without any household audio/outbox work."""
    cues = claim_beeps()
    apply_master_volume()
    switch = Button(BUTTON_PIN)
    lamp = LED(LED_PIN)
    lamp.off()
    while True:
        while switch.is_pressed:
            time.sleep(POLL_S)
        while not switch.is_pressed:
            apply_master_volume()
            play_setup_online(cues)
            time.sleep(POLL_S)
        started = time.monotonic()
        while switch.is_pressed and time.monotonic() - started < CONFIRM_PRESS_S:
            time.sleep(POLL_S)
        if switch.is_pressed:
            claim_button_press(cues)
        while switch.is_pressed:
            time.sleep(POLL_S)


def handle_confirmed_press(closed_at):
    """Dispatch a debounced press through the normal routing and audio flow."""
    if transport_mode() == "cloud" and cloud_claim.consume_claim_press():
        log_event("cloud_claim_button_pressed")
        wait_for_stable_open()
        return True
    interaction_settings = caregiver_settings()
    try:
        if interaction_settings["recording_mode"] == "tap_review":
            # The session starts from this press only after its release; it can
            # never be carried into incoming audio, countdown, or recording.
            acknowledge_guided_press("start_session")
            wait_for_stable_open()
            run_guided_once(interaction_settings)
        else:
            record_and_send_legacy(interaction_settings, pressed_at=closed_at)
    except Exception as exc:
        log(f"button flow error: {exc}")
        log_event(
            "button_flow_error",
            recording_mode=interaction_settings["recording_mode"],
            error=type(exc).__name__,
        )
        if not _guided_active:
            beep("fail")
        return False
    finally:
        refresh_led(force=True)
    return True


def main():
    global button, led, outbox_store, receipt_store
    try:
        transport_mode()
    except ValueError as exc:
        log(str(exc))
        return 2
    if transport_mode() == "cloud" and os.environ.get("MSGBOX_CLAIM_ONLY") == "1":
        if "--drain" in sys.argv:
            return 2
        return claim_only_loop()
    try:
        validate_sounds()
    except ValueError as exc:
        log(str(exc))
        return 2
    os.makedirs(QUEUE_DIR, exist_ok=True)
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    outbox_store = OutboxStore(OUTBOX_DIR, transport=transport_mode())
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
        if transport_mode() == "cloud":
            for filename in compatible_legacy_outbox_files():
                if not stage_hold_release_cloud_job(filename):
                    return 1
        for job in compatible_guided_jobs():
            ok = send_guided_job(job) and ok
        if transport_mode() != "cloud":
            for filename in compatible_legacy_outbox_files():
                ok = send_legacy_outbox_file(filename) and ok
        return 0 if ok else 1

    try:
        validate_prompts()
    except RuntimeError as exc:
        log(str(exc))
        return 2

    threading.Thread(target=sender_loop, daemon=True).start()
    button = Button(BUTTON_PIN)
    led = LED(LED_PIN)
    apply_master_volume()
    if button.is_pressed:
        log("switch CLOSED at startup - waiting for it to open")
    wait_for_stable_open()
    refresh_led(force=True)
    startup_settings = caregiver_settings()
    log(
        f'armed: recording_mode={startup_settings["recording_mode"]} '
        f'after_listening={startup_settings["after_listening"]} '
        f"routing_mode={routing_mode()} "
        f"({len(queued())} queued, "
        f"{len(outbox_store.jobs()) + len(compatible_legacy_outbox_files())} unsent)"
    )
    announce_runtime_ready()

    while True:
        wait_for_stable_open()
        while not button.is_pressed:
            time.sleep(POLL_S)
            apply_master_volume()
            maybe_play_send_success()
            maybe_play_cloud_sound()
            try:
                maybe_play_connectivity()
                announce_all_set()
                maybe_play_still_trying()
                unavailable_events.available("sound_moments")
            except (OSError, ValueError, CloudRuntimeError, subprocess.SubprocessError):
                if unavailable_events.unavailable("sound_moments"):
                    log_event("sound_moment_unavailable")
            if button.is_pressed:
                break
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
        handle_confirmed_press(closed_at)


if __name__ == "__main__":
    raise SystemExit(main())
