#!/usr/bin/env python3
"""Button Box caregiver dashboard and local queue manager.

Stdlib only (http.server) — the Pi Zero doesn't need Flask. Reads the
events JSONL written by voicepoll/button_send and the queue dir. Held messages
go to QUEUE_DIR/.hold and are skipped until reinstated. Deleted messages go to
QUEUE_DIR/.trash (also reinstatable), so test messages never reach the kids but
nothing is lost by accident.

Endpoints:
  GET  /               dashboard HTML
  GET  /static/<asset> dashboard JavaScript and CSS
  GET  /api/data       stats + queue + hold + trash as JSON
  GET  /api/contacts   contact settings + WhatsApp chat discovery as JSON
  GET  /audio/<f>      stream a WAV (?hold=1 or ?trash=1)
  POST /api/contacts   add or remove a contact
  POST /api/listeners  add, update, or remove a listener profile
  POST /api/wacli-receipt      authenticated WhatsApp played-receipt webhook
  POST /api/ring       request an on-demand ringtone
  POST /api/hold       ?f=<file>   queue -> hold
  POST /api/resume     ?f=<file>   hold -> queue
  POST /api/delete     ?f=<file>   queue -> trash
  POST /api/reinstate  ?f=<file>   trash -> queue
"""
import json
import fcntl
import os
import secrets
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import wave
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from messagebox.contacts import ContactError, ContactStore, validate_contact
from messagebox.nfc import router as nfc_router
from messagebox.nfc_state import NfcError, active_selection
from messagebox.runtime_paths import APP_DIR, CONTACTS_FILE, OUTBOX_DIR as DEFAULT_OUTBOX_DIR
from messagebox.runtime_paths import QUEUE_DIR as DEFAULT_QUEUE_DIR
from messagebox.runtime_paths import NFC_HEALTH_FILE, NFC_SELECTION_FILE, RUNTIME_DIR, STATE_DIR
from messagebox.listened_receipts import (
    MAX_WEBHOOK_BYTES,
    ReceiptStore,
    load_listener_profiles,
    valid_webhook_signature,
)
from messagebox.onboarding.recipients import RecipientError, RecipientSetup
from messagebox.onboarding.whatsapp import PairingEngine, PairingError, normalize_phone
from messagebox.settings import (
    RINGTONES,
    RevisionConflict,
    SettingsError,
    SettingsStore,
    ringtone_path,
)
from messagebox.tailnet import (
    normalize_device_name,
    normalize_tailscale_host,
    request_origin,
)
from messagebox.wifi_change import WifiChangeError, load_status as wifi_change_status
from messagebox.wifi_change import request_change as request_wifi_change

BIND = os.environ.get("MSGBOX_DASH_BIND", "wlan0").strip()
PORT = int(os.environ.get("MSGBOX_DASH_PORT", "80"))
QUEUE_DIR = str(DEFAULT_QUEUE_DIR)
HOLD_DIR = os.path.join(QUEUE_DIR, ".hold")
TRASH_DIR = os.path.join(QUEUE_DIR, ".trash")
OUTBOX_DIR = str(DEFAULT_OUTBOX_DIR)
EVENTS_FILE = str(STATE_DIR / "events.jsonl")
WACLI_BIN = "/usr/local/bin/wacli"
LISTENED_DIR = str(STATE_DIR / "listened-receipts")
LISTENED_FALLBACK_WAV = os.environ.get(
    "MSGBOX_LISTENED_FALLBACK_WAV",
    str(APP_DIR / "sounds" / "listen-receipts" / "someone-listened.wav"),
)
WACLI_WEBHOOK_SECRET = os.environ.get("MSGBOX_WACLI_WEBHOOK_SECRET", "")
TAILSCALE_HOST_SETTING = os.environ.get("MSGBOX_TAILSCALE_HOST", "")
RING_REQUEST_FILE = str(RUNTIME_DIR / "ring-request")
QUEUE_ACTION_LOCK = threading.Lock()
DASHBOARD_STATIC_DIR = Path(__file__).resolve().parents[1] / "onboarding" / "static"
DASHBOARD_STATIC = {
    "/": (
        DASHBOARD_STATIC_DIR.joinpath("index.html").read_bytes(),
        "text/html; charset=utf-8",
    ),
    "/static/app.js": (
        DASHBOARD_STATIC_DIR.joinpath("app.js").read_bytes(),
        "text/javascript; charset=utf-8",
    ),
    "/static/styles.css": (
        DASHBOARD_STATIC_DIR.joinpath("styles.css").read_bytes(),
        "text/css; charset=utf-8",
    ),
}
RINGTONE_PREVIEW_LOCK = threading.Lock()
PUBLIC_MESSAGE_LOCK = threading.Lock()
PUBLIC_MESSAGES = {}
PUBLIC_MESSAGE_REVERSE = {}
PAIRING_ENGINE_LOCK = threading.Lock()
_PAIRING_ENGINE = None


def resolve_bind(value):
    """Resolve an interface name without broadening the no-login listener."""
    if not value:
        raise ValueError("dashboard bind interface is required")
    if not Path("/sys/class/net", value).is_dir():
        return value
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        request = struct.pack("256s", value[:15].encode("ascii"))
        response = fcntl.ioctl(control.fileno(), 0x8915, request)
    return socket.inet_ntoa(response[20:24])


def settings_store():
    return SettingsStore()


def pairing_engine():
    global _PAIRING_ENGINE
    with PAIRING_ENGINE_LOCK:
        if _PAIRING_ENGINE is None:
            _PAIRING_ENGINE = PairingEngine()
        return _PAIRING_ENGINE


def settings_payload():
    document, warning = settings_store().load()
    return {"ok": True, "settings": document, "attention": warning}


def whatsapp_authenticated(value):
    """Recognize the bounded public result of `wacli auth status`."""
    if isinstance(value, dict):
        return value.get("authenticated") is True or any(
            whatsapp_authenticated(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(whatsapp_authenticated(item) for item in value)
    return False


def runtime_state():
    contacts = {"contacts": {}}
    try:
        contacts = contacts_store().public_view()
        recipient_ready = bool(contacts.get("default_recipient"))
        recipient_count = len(contacts.get("contacts", {}))
    except (ContactError, OSError):
        recipient_ready = False
        recipient_count = 0
    wifi_connected = False
    network_name = None
    try:
        for line in Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 4 and fields[0] == "wlan0" and fields[1] == "00000000":
                wifi_connected = bool(int(fields[3], 16) & 0x2)
                break
    except (OSError, ValueError):
        pass
    if wifi_connected:
        try:
            connection = subprocess.run(
                ["nmcli", "--get-values", "GENERAL.CONNECTION", "device", "show", "wlan0"],
                capture_output=True,
                check=False,
                text=True,
                timeout=2,
            )
            candidate = connection.stdout.strip()
            if connection.returncode == 0 and candidate and len(candidate) <= 64:
                network_name = candidate
        except (OSError, subprocess.SubprocessError):
            pass
    whatsapp_connected = False
    first_message_ready = False
    try:
        recipient_state = RecipientSetup().public_state()
        first_message_ready = recipient_state.get("status") == "complete"
    except (OSError, RecipientError):
        pass
    try:
        result = subprocess.run(
            [WACLI_BIN, "--read-only", "--json", "auth", "status"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
        status = json.loads(result.stdout) if result.returncode == 0 else {}

        whatsapp_connected = whatsapp_authenticated(status)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        pass
    return {
        "mode": "RUNTIME",
        "phase": "COMPLETE",
        "product": "Button Box",
        "setup": {
            "wifi": "complete" if wifi_connected else "attention",
            "whatsapp": "complete" if whatsapp_connected else "attention",
            "recipient": "complete" if recipient_ready else "attention",
            "first_message": "complete" if first_message_ready else "attention",
            "nfc": "complete" if any(
                contact.get("card_count", 0)
                for contact in contacts.get("contacts", {}).values()
            ) else "optional",
        },
        "recipient_count": recipient_count,
        "health": {
            "wifi": "connected" if wifi_connected else "attention",
            "network_name": network_name,
            "whatsapp": "connected" if whatsapp_connected else "attention",
            "runtime": "running",
            "software_version": os.environ.get("MSGBOX_VERSION", "installed"),
        },
    }


def preview_ringtone(ringtone_id):
    document, _warning = settings_store().load()
    if ringtone_id not in RINGTONES:
        raise SettingsError("ringtone is invalid")
    preview = {**document, "ringtone_id": ringtone_id}
    path = ringtone_path(preview)
    if not path.is_file():
        raise SettingsError("ringtone is unavailable")
    if not RINGTONE_PREVIEW_LOCK.acquire(blocking=False):
        raise SettingsError("Button Box audio is busy")

    def play():
        try:
            subprocess.run(
                ["aplay", "-q", "-D", os.environ.get("MSGBOX_SPK_DEV", "default"), os.fspath(path)],
                check=False,
                timeout=30,
            )
        finally:
            RINGTONE_PREVIEW_LOCK.release()

    threading.Thread(target=play, daemon=True).start()


def log_event(**ev):
    ev["ts"] = time.time()
    os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
    with open(EVENTS_FILE, "a") as f:
        f.write(json.dumps(ev) + "\n")


def contacts_store():
    return ContactStore(CONTACTS_FILE)


def force_refresh_whatsapp_chats():
    result = subprocess.run(
        [
            WACLI_BIN, "--json", "--lock-wait", "10s", "sync", "--once",
            "--idle-exit", "5s", "--presence-mode", "quiet", "--refresh-groups",
        ],
        capture_output=True,
        text=True,
        timeout=25,
    )
    if result.returncode != 0:
        raise RuntimeError("WhatsApp group refresh failed")


def discover_whatsapp_chats():
    """Return sanitized direct and group chats supported by ContactStore."""
    result = subprocess.run(
        [WACLI_BIN, "--read-only", "--json", "--full", "chats", "list", "--limit", "200"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError("could not read WhatsApp chats")
    payload = json.loads(result.stdout or "{}")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("WhatsApp chat list is invalid")
    chats = {}
    for chat in payload.get("data") or []:
        if not isinstance(chat, dict):
            continue
        jid = chat.get("jid")
        name = chat.get("name")
        cleaned_name = (
            " ".join(name.replace("\x00", "").split())[:80]
            if isinstance(name, str)
            else ""
        )
        try:
            candidate = validate_contact(
                jid,
                cleaned_name or "Unnamed contact",
            )
        except ContactError:
            continue
        last_active = chat.get("last_message_ts")
        chats[candidate["jid"]] = {
            "jid": candidate["jid"],
            "label": cleaned_name or (
                "Unnamed group" if candidate["kind"] == "group" else "Unnamed person"
            ),
            "kind": candidate["kind"],
            "last_active": last_active[:64] if isinstance(last_active, str) else None,
            "discovered": True,
        }
    return sorted(chats.values(), key=lambda chat: chat["label"].casefold())


def contact_settings(refresh=False):
    public = contacts_store().public_view()
    discovery_error = None
    try:
        if refresh:
            force_refresh_whatsapp_chats()
        discovered = {chat["jid"]: chat for chat in discover_whatsapp_chats()}
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        discovered = {}
        discovery_error = "WhatsApp chat discovery is temporarily unavailable."
    for jid, contact in public["contacts"].items():
        chat = discovered.setdefault(
            jid,
            {
                "jid": jid,
                "label": contact["label"],
                "kind": contact["kind"],
                "last_active": None,
                "discovered": False,
            },
        )
        chat["label"] = contact["label"]
        chat["configured"] = True
    for chat in discovered.values():
        chat.setdefault("configured", False)
    public["discovered"] = sorted(
        discovered.values(), key=lambda chat: chat["label"].casefold()
    )
    contact_count = len(public["contacts"])
    public["mode"] = (
        "empty"
        if contact_count == 0
        else "default"
        if public["default_recipient"] is not None
        else "no_default"
    )
    public["discovery_error"] = discovery_error
    return public


def load_events():
    events = []
    try:
        with open(EVENTS_FILE) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except FileNotFoundError:
        pass
    return events


def wav_meta(path):
    try:
        with wave.open(path) as w:
            dur = w.getnframes() / w.getframerate()
    except Exception:
        dur = None
    name = os.path.basename(path)
    try:
        ts = int(name.split("-", 1)[0]) / 1000
    except ValueError:
        ts = os.path.getmtime(path)
    return {"file": name, "dur": dur, "ts": ts}


def list_wavs(d):
    try:
        items = [wav_meta(os.path.join(d, f))
                 for f in os.listdir(d) if f.endswith(".wav")]
        return sorted(items, key=lambda x: x["ts"])
    except FileNotFoundError:
        return []


def public_message_token(kind, name):
    """Return a process-local opaque handle; never expose the queue filename."""
    key = (kind, name)
    with PUBLIC_MESSAGE_LOCK:
        token = PUBLIC_MESSAGE_REVERSE.get(key)
        if token is None:
            token = secrets.token_urlsafe(18)
            PUBLIC_MESSAGE_REVERSE[key] = token
            PUBLIC_MESSAGES[token] = key
        return token


def resolve_message_token(token, expected_kind):
    if not isinstance(token, str) or len(token) > 64:
        return None
    with PUBLIC_MESSAGE_LOCK:
        value = PUBLIC_MESSAGES.get(token)
    if value is None or value[0] != expected_kind:
        return None
    return value[1]


def move_queue_message(source_dir, destination_dir, name, *, exposing_to_player):
    """Move a WAV and routing sidecar without exposing a metadata-less item."""
    source = os.path.join(source_dir, name)
    destination = os.path.join(destination_dir, name)
    source_meta = source + ".json"
    destination_meta = destination + ".json"
    with QUEUE_ACTION_LOCK:
        if not os.path.exists(source):
            raise FileNotFoundError(source)
        if os.path.exists(destination) or os.path.exists(destination_meta):
            raise FileExistsError(destination)
        os.makedirs(destination_dir, exist_ok=True)
        moved_wav = moved_meta = False
        try:
            # When returning to the playable queue, place routing metadata
            # first and expose the WAV last. When holding/deleting, hide the
            # WAV first so the button cannot claim it mid-move.
            if exposing_to_player and os.path.exists(source_meta):
                os.replace(source_meta, destination_meta)
                moved_meta = True
            os.replace(source, destination)
            moved_wav = True
            if not exposing_to_player and os.path.exists(source_meta):
                os.replace(source_meta, destination_meta)
                moved_meta = True
        except Exception:
            # Best-effort rollback in the inverse safe order.
            if exposing_to_player:
                if moved_wav and os.path.exists(destination):
                    os.replace(destination, source)
                if moved_meta and os.path.exists(destination_meta):
                    os.replace(destination_meta, source_meta)
            else:
                if moved_meta and os.path.exists(destination_meta):
                    os.replace(destination_meta, source_meta)
                if moved_wav and os.path.exists(destination):
                    os.replace(destination, source)
            raise


def listened_store():
    return ReceiptStore(LISTENED_DIR)


def guided_outbox_counts():
    """Content-free delivery health; recipient metadata never leaves the Pi."""
    counts = {"pending": 0, "attention": 0}
    try:
        paths = [
            os.path.join(OUTBOX_DIR, name, "job.json")
            for name in os.listdir(OUTBOX_DIR)
            if name.endswith(".job")
        ]
    except FileNotFoundError:
        return counts
    for path in paths:
        try:
            with open(path) as handle:
                state = json.load(handle).get("state", "pending")
        except (OSError, ValueError):
            state = "failed"
        if state == "pending":
            counts["pending"] += 1
        elif state in {"failed", "uncertain"}:
            counts["attention"] += 1
    return counts


def guided_outbox_states():
    """Map internal message IDs to delivery state for session correlation only."""
    states = {}
    try:
        names = [name for name in os.listdir(OUTBOX_DIR) if name.endswith(".job")]
    except FileNotFoundError:
        return states
    for name in names:
        try:
            with open(os.path.join(OUTBOX_DIR, name, "job.json")) as handle:
                job = json.load(handle)
            if job.get("message_id"):
                states[job["message_id"]] = job.get("state", "pending")
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return states


OUTCOME_LABELS = {
    "delivered": ("Delivered", "ok"),
    "queued": ("Approved · waiting", "warn"),
    "sending": ("Sending", "warn"),
    "retrying": ("Retrying silently", "warn"),
    "needs_attention": ("Delivery needs attention", "bad"),
    "approved": ("Approved · delivery unknown", "warn"),
    "no_speech": ("No speech captured", "dim"),
    "not_sent": ("Not approved", "dim"),
    "played_only": ("Played", "ok"),
    "interrupted": ("Interrupted", "bad"),
    "incomplete": ("Incomplete", "bad"),
    "in_progress": ("In progress", "warn"),
}


def safe_contact_label(jid, labels):
    if jid in labels:
        return labels[jid]
    try:
        validate_contact(jid, "Historical contact")
    except ContactError:
        return "Unknown contact"
    return "Removed contact"


def build_guided_observability(events, names, outbox_states=None, now=None, limit=30):
    """Build a content-free behavioral funnel and recent session timeline.

    Raw chat JIDs, message IDs, session IDs and queue filenames are used only
    for local correlation and are never returned by this function.
    """
    outbox_states = outbox_states or {}
    now = time.time() if now is None else now
    ordered = sorted(
        (event for event in events if isinstance(event.get("ts"), (int, float))),
        key=lambda event: event["ts"],
    )
    received = [event for event in ordered if event.get("type") == "received"]
    received_by_file = {
        event.get("file"): event for event in received if event.get("file")
    }
    used_sources = set()
    sessions = {}
    session_order = []

    def source_for(event):
        source_file = event.get("source_file")
        source = received_by_file.get(source_file)
        confidence = "exact" if source else None
        if source is None:
            for candidate in received:
                filename = candidate.get("file")
                if (
                    filename
                    and filename not in used_sources
                    and candidate["ts"] <= event["ts"]
                ):
                    source = candidate
                    source_file = filename
                    confidence = "oldest_first"
                    break
        if source_file:
            used_sources.add(source_file)
        return source, confidence

    def latest_open(flow=None, event_ts=None):
        for session in reversed(session_order):
            if flow and session["flow"] != flow:
                continue
            if event_ts is not None and session["ts"] > event_ts:
                continue
            if session.get("ended_at") is None:
                return session
        return None

    for event in ordered:
        kind = event.get("type")
        sid = event.get("session_id")
        if kind == "guided_session_started":
            sid = sid or f"legacy-{len(session_order)}-{event['ts']}"
            source = confidence = None
            if event.get("flow") == "reply":
                source, confidence = source_for(event)
            chat_jid = source.get("chat") if source else None
            session = {
                "_sid": sid,
                "_message_id": None,
                "ts": event["ts"],
                "flow": event.get("flow") or "unknown",
                "sender": (source.get("sender") if source else None) or (
                    "Child" if event.get("flow") == "standalone" else "Family"
                ),
                "chat": safe_contact_label(chat_jid, names) if chat_jid else None,
                "source_confidence": confidence,
                "received_at": source.get("ts") if source else None,
                "played_at": None,
                "reviewed_at": None,
                "approved_at": None,
                "delivered_at": None,
                "listened_at": None,
                "listeners": [],
                "ended_at": None,
                "duration": None,
                "actions": [],
                "outcome": "in_progress",
            }
            sessions[sid] = session
            session_order.append(session)
            continue

        session = sessions.get(sid)
        if session is None and kind == "guided_press" and event.get("action") != "start_session":
            session = latest_open(event_ts=event["ts"])
        if session is None and kind == "guided_session_interrupted":
            session = latest_open(event.get("flow"), event["ts"])
        if session is None:
            continue

        if kind == "guided_inbound_played":
            session["played_at"] = event["ts"]
        elif kind == "guided_playback_only":
            session["ended_at"] = event["ts"]
            session["outcome"] = "played_only"
        elif kind == "guided_review_played":
            session["reviewed_at"] = event["ts"]
            session["duration"] = event.get("duration")
        elif kind == "guided_press":
            action = event.get("action")
            if action and action not in session["actions"]:
                session["actions"].append(action)
        elif kind == "guided_approved":
            session["approved_at"] = event["ts"]
            session["ended_at"] = event["ts"]
            session["duration"] = event.get("duration", session["duration"])
            session["_message_id"] = event.get("message_id")
            session["outcome"] = "approved"
        elif kind == "guided_recording_empty":
            session["ended_at"] = event["ts"]
            session["outcome"] = "no_speech"
        elif kind == "guided_deleted":
            session["ended_at"] = event["ts"]
            session["outcome"] = "not_sent"
        elif kind == "guided_session_interrupted":
            session["ended_at"] = event["ts"]
            session["outcome"] = "interrupted"

    by_message_id = {
        session["_message_id"]: session
        for session in session_order
        if session.get("_message_id")
    }
    whatsapp_to_message_id = {
        event.get("whatsapp_id"): event.get("message_id")
        for event in ordered
        if event.get("type") == "sent"
        and event.get("whatsapp_id")
        and event.get("message_id")
    }
    for event in ordered:
        session = by_message_id.get(event.get("message_id"))
        if session is None and event.get("type") == "listen_receipt":
            session = by_message_id.get(
                whatsapp_to_message_id.get(event.get("whatsapp_id"))
            )
        if not session:
            continue
        kind = event.get("type")
        if kind == "sent":
            session["delivered_at"] = event["ts"]
            session["outcome"] = "delivered"
        elif kind == "outbox_retry":
            session["outcome"] = "retrying"
        elif kind in {"outbox_failed", "outbox_uncertain"}:
            session["outcome"] = "needs_attention"
        elif kind == "listen_receipt":
            listener = event.get("listener")
            if listener and listener not in session["listeners"]:
                session["listeners"].append(listener)
            session["listened_at"] = event["ts"]

    for session in session_order:
        if session["outcome"] == "approved":
            state = outbox_states.get(session.get("_message_id"))
            session["outcome"] = {
                "pending": "queued",
                "sending": "sending",
                "failed": "needs_attention",
                "uncertain": "needs_attention",
            }.get(state, "approved")
        if session["outcome"] == "in_progress" and now - session["ts"] > 120:
            session["outcome"] = "incomplete"

    cutoff = now - 7 * 86400
    recent = [session for session in session_order if session["ts"] >= cutoff]
    replies = [
        session
        for session in recent
        if session["flow"] == "reply" and session["outcome"] != "played_only"
    ]
    reviewed = [session for session in recent if session["reviewed_at"] is not None]
    approved_outcomes = {
        "approved", "queued", "sending", "retrying", "needs_attention", "delivered"
    }
    approved = [session for session in recent if session["outcome"] in approved_outcomes]
    approved_replies = [session for session in replies if session["outcome"] in approved_outcomes]
    play_waits = [
        session["played_at"] - session["received_at"]
        for session in replies
        if session["played_at"] is not None and session["received_at"] is not None
    ]

    def percentage(numerator, denominator):
        return round(100 * numerator / denominator) if denominator else None

    behavior = {
        "period_days": 7,
        "reply_sessions": len(replies),
        "reply_approved": len(approved_replies),
        "reply_rate": percentage(len(approved_replies), len(replies)),
        "reviewed": len(reviewed),
        "review_send_rate": percentage(len(approved), len(reviewed)),
        "no_speech": sum(session["outcome"] == "no_speech" for session in recent),
        "not_sent": sum(session["outcome"] == "not_sent" for session in recent),
        "incomplete": sum(
            session["outcome"] in {"incomplete", "interrupted"} for session in recent
        ),
        "standalone_sessions": sum(
            session["flow"] == "standalone" for session in recent
        ),
        "listened_messages": sum(bool(session["listeners"]) for session in recent),
        "avg_wait_to_play_s": round(sum(play_waits) / len(play_waits), 1)
        if play_waits
        else None,
    }

    def stages_for(session):
        outcome = session["outcome"]
        stopped = "stop_recording" in session["actions"]
        reviewed = session["reviewed_at"] is not None
        approved = session["approved_at"] is not None
        stages = []

        def add(label, state):
            stages.append({"label": label, "state": state})

        if session["flow"] == "reply":
            add("Received", "done")
            if session["played_at"] is not None:
                add("Played", "done")
            elif outcome in {"incomplete", "interrupted"}:
                add("Interrupted", "failed")
            else:
                add("Played", "current" if outcome == "in_progress" else "muted")
        else:
            add("Started", "done")

        if outcome == "no_speech":
            add("No speech", "failed")
        elif stopped or reviewed:
            add("Recorded", "done")
        else:
            add("Recorded", "current" if outcome == "in_progress" else "muted")

        if reviewed:
            add("Reviewed", "done")
        elif outcome in {"incomplete", "interrupted"} and stopped:
            add("Interrupted", "failed")
        else:
            add("Reviewed", "muted")

        if approved:
            add("Approved", "done")
        elif outcome == "not_sent":
            add("Not approved", "failed")
        elif outcome == "in_progress" and reviewed:
            add("Approve", "current")
        else:
            add("Approved", "muted")

        if outcome == "delivered":
            add("Sent", "done")
        elif outcome in {"approved", "queued", "sending", "retrying"}:
            add("Waiting", "current")
        elif outcome == "needs_attention":
            add("Send failed", "failed")
        else:
            add("Sent", "muted")
        return stages

    timeline = []
    for session in reversed(session_order[-limit:]):
        journey = []
        if session["flow"] == "reply":
            journey.append("Message received")
        else:
            journey.append("New message started")
        if session["played_at"] is not None:
            journey.append("Message played")
        if "stop_recording" in session["actions"]:
            journey.append("Stop pressed")
        if session["reviewed_at"] is not None:
            duration = session["duration"]
            journey.append(
                f"Recording reviewed ({duration:.1f}s)"
                if isinstance(duration, (int, float))
                else "Recording reviewed"
            )
        if session["approved_at"] is not None:
            journey.append("Send approved")
        if session["outcome"] == "delivered":
            journey.append("Delivered")
        elif session["outcome"] == "played_only":
            journey.append("Playback complete")
        elif session["outcome"] == "not_sent":
            journey.append("Not approved · deleted")
        elif session["outcome"] == "no_speech":
            journey.append("No meaningful speech")
        elif session["outcome"] in {"incomplete", "interrupted"}:
            journey.append("Session ended early")
        for listener in session["listeners"]:
            journey.append(f"{listener} listened")
        label, tone = OUTCOME_LABELS[session["outcome"]]
        timeline.append(
            {
                "ts": session["ts"],
                "flow": session["flow"],
                "sender": session["sender"],
                "chat": session["chat"],
                "source_confidence": session["source_confidence"],
                "outcome": session["outcome"],
                "outcome_label": label,
                "outcome_tone": tone,
                "duration": session["duration"],
                "listeners": list(session["listeners"]),
                "wait_to_play_s": round(
                    session["played_at"] - session["received_at"], 1
                )
                if session["played_at"] is not None
                and session["received_at"] is not None
                else None,
                "stages": stages_for(session),
                "journey": journey,
            }
        )
    return {"behavior": behavior, "interactions": timeline}


def build_data():
    events = load_events()
    names = {
        jid: contact["label"]
        for jid, contact in contacts_store().public_view()["contacts"].items()
    }
    days = 14
    today = time.strftime("%Y-%m-%d")
    day_keys = [time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400 * i))
                for i in range(days - 1, -1, -1)]
    sent_day = defaultdict(int)
    recv_day = defaultdict(int)
    by_chat_in = defaultdict(int)
    by_chat_out = defaultdict(int)
    durs_sent, durs_recv, waits = [], [], []
    plays = rings = listened = 0
    # queue files -> received event metadata (sender/chat per file)
    file_meta = {}
    for e in events:
        day = time.strftime("%Y-%m-%d", time.localtime(e["ts"]))
        t = e.get("type")
        if t == "sent":
            sent_day[day] += 1
            by_chat_out[e.get("target", "?")] += 1
            if e.get("dur"):
                durs_sent.append(e["dur"])
        elif t == "received":
            recv_day[day] += 1
            by_chat_in[e.get("chat", "?")] += 1
            if e.get("dur"):
                durs_recv.append(e["dur"])
            if e.get("file"):
                file_meta[e["file"]] = {"chat": e.get("chat"),
                                        "sender": e.get("sender")}
        elif t == "played":
            plays += 1
            if e.get("wait_s") is not None:
                waits.append(e["wait_s"])
        elif t == "ring":
            rings += 1
        elif t == "listen_receipt":
            listened += 1

    def label(jid):
        return safe_contact_label(jid, names)

    queue = list_wavs(QUEUE_DIR)
    hold = list_wavs(HOLD_DIR)
    trash = list_wavs(TRASH_DIR)
    for kind, items in (("queue", queue), ("hold", hold), ("trash", trash)):
        for item in items:
            meta = file_meta.get(item["file"], {})
            item["chat"] = label(meta.get("chat", "")) or "?"
            item["sender"] = meta.get("sender") or "?"
            item["token"] = public_message_token(kind, item.pop("file"))

    def avg(values):
        return round(sum(values) / len(values), 1) if values else None

    outbox = guided_outbox_counts()
    observability = build_guided_observability(
        events, names, guided_outbox_states()
    )
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "today": today,
        "cards": {
            "sent_total": sum(sent_day.values()),
            "recv_total": sum(recv_day.values()),
            "sent_today": sent_day[today],
            "recv_today": recv_day[today],
            "plays": plays,
            "rings": rings,
            "listened": listened,
            "listened_pending": listened_store().pending_count(),
            "avg_sent_dur": avg(durs_sent),
            "avg_recv_dur": avg(durs_recv),
            "avg_wait_min": round(avg(waits) / 60, 1) if waits else None,
            "outbox_pending": outbox["pending"],
            "outbox_attention": outbox["attention"],
        },
        "days": day_keys,
        "sent_per_day": [sent_day[d] for d in day_keys],
        "recv_per_day": [recv_day[d] for d in day_keys],
        "by_chat_in": sorted(((label(k), v) for k, v in by_chat_in.items()),
                             key=lambda x: -x[1]),
        "by_chat_out": sorted(((label(k), v) for k, v in by_chat_out.items()),
                              key=lambda x: -x[1]),
        "behavior": observability["behavior"],
        "interactions": observability["interactions"],
        "queue": queue,
        "hold": hold,
        "trash": trash,
    }


def safe_name(raw):
    name = os.path.basename(urllib.parse.unquote(raw))
    if not name.endswith(".wav") or "/" in name or name.startswith("."):
        return None
    return name


class Handler(BaseHTTPRequestHandler):
    local_host = None
    tailscale_host = None

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; object-src 'none'; script-src 'self'; "
            "style-src 'self'; connect-src 'self'; media-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        self.wfile.write(data)

    def _require_json(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send(415, json.dumps({"ok": False, "error": "application/json required"}))
            return False
        return True

    def _trusted_origin(self):
        headers = getattr(self, "headers", {})
        host = headers.get("Host", "")
        local_host = self.local_host
        http_hosts = (local_host,) if local_host else ()
        client = getattr(self, "client_address", ("", 0))
        return request_origin(
            host,
            remote_addr=client[0],
            forwarded_proto=headers.get("X-Forwarded-Proto"),
            http_hosts=http_hosts,
            tailscale_host=self.tailscale_host,
        )

    def _require_trusted_host(self):
        if self._trusted_origin() is None:
            self._send(400, json.dumps({"ok": False, "error": "invalid dashboard address"}))
            return False
        return True

    def _require_same_origin(self):
        headers = getattr(self, "headers", {})
        if headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            self._send(403, json.dumps({"ok": False, "error": "cross-site request rejected"}))
            return False
        expected_origin = self._trusted_origin()
        if expected_origin is None:
            self._send(400, json.dumps({"ok": False, "error": "invalid dashboard address"}))
            return False
        origin = headers.get("Origin")
        if origin and origin.rstrip("/").casefold() != expected_origin.casefold():
            self._send(403, json.dumps({"ok": False, "error": "cross-site request rejected"}))
            return False
        return True

    def _json_body(self, limit=16384):
        if not self._require_json():
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, json.dumps({"ok": False, "error": "invalid request size"}))
            return None
        if length < 2 or length > limit:
            self._send(400, json.dumps({"ok": False, "error": "invalid request size"}))
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeError, json.JSONDecodeError):
            self._send(400, json.dumps({"ok": False, "error": "invalid JSON"}))
            return None
        if not isinstance(payload, dict):
            self._send(400, json.dumps({"ok": False, "error": "request must be an object"}))
            return None
        return payload

    def _form_body(self, limit=8192):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            self._send(415, json.dumps({"ok": False, "error": "form submission required"}))
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > limit:
                raise ValueError
            text = self.rfile.read(length).decode("utf-8")
            pairs = urllib.parse.parse_qsl(text, keep_blank_values=True, strict_parsing=True)
            if len({key for key, _value in pairs}) != len(pairs):
                raise ValueError
            return dict(pairs)
        except (UnicodeError, ValueError):
            self._send(400, json.dumps({"ok": False, "error": "invalid form submission"}))
            return None

    def do_GET(self):
        if not self._require_trusted_host():
            return
        url = urllib.parse.urlparse(self.path)
        static = DASHBOARD_STATIC.get(url.path)
        if static is not None:
            return self._send(200, *static)
        if url.path == "/api/state":
            return self._send(200, json.dumps(runtime_state()))
        if url.path == "/api/settings":
            return self._send(200, json.dumps(settings_payload()))
        if url.path == "/api/whatsapp":
            try:
                return self._send(200, json.dumps(pairing_engine().public_state()))
            except (OSError, PairingError):
                return self._send(503, json.dumps({"error": "WhatsApp status is unavailable"}))
        if url.path == "/api/recipients":
            try:
                return self._send(200, json.dumps(pairing_engine().recipient_list()))
            except (OSError, PairingError):
                return self._send(409, json.dumps({"error": "Recipient setup is unavailable"}))
        if url.path == "/api/nfc-runtime":
            try:
                enrollment = nfc_router().enrollment.active()
                health_path = Path(NFC_HEALTH_FILE)
                healthy = health_path.is_file() and time.time() - health_path.stat().st_mtime <= 10
                return self._send(
                    200,
                    json.dumps(
                        {
                            "status": "waiting" if enrollment else "idle",
                            "recipient": enrollment.get("label") if enrollment else None,
                            "healthy": healthy,
                        }
                    ),
                )
            except (ContactError, NfcError, OSError):
                return self._send(503, json.dumps({"error": "NFC status is unavailable"}))
        if url.path == "/api/wifi-change":
            try:
                return self._send(200, json.dumps(wifi_change_status()))
            except WifiChangeError as exc:
                return self._send(503, json.dumps({"error": str(exc)}))
        if url.path == "/api/data":
            return self._send(200, json.dumps(build_data()))
        if url.path == "/api/contacts":
            try:
                query = urllib.parse.parse_qs(url.query or "")
                refresh = query.get("refresh") == ["1"]
                return self._send(200, json.dumps(contact_settings(refresh=refresh)))
            except (
                ContactError, OSError, RuntimeError, subprocess.SubprocessError,
                ValueError, json.JSONDecodeError,
            ) as exc:
                return self._send(503, json.dumps({"ok": False, "error": str(exc)}))
        if url.path.startswith("/audio/"):
            query = urllib.parse.parse_qs(url.query or "")
            if query.get("hold") == ["1"]:
                d, kind = HOLD_DIR, "hold"
            elif query.get("trash") == ["1"]:
                d, kind = TRASH_DIR, "trash"
            else:
                d, kind = QUEUE_DIR, "queue"
            token = urllib.parse.unquote(url.path[len("/audio/"):])
            name = resolve_message_token(token, kind)
            if not name:
                return self._send(400, "{}")
            path = os.path.join(d, name)
            if not os.path.exists(path):
                return self._send(404, "{}")
            with open(path, "rb") as f:
                return self._send(200, f.read(), "audio/wav")
        return self._send(404, "{}")

    def do_PUT(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/api/settings":
            return self._send(404, "{}")
        if not self._require_same_origin():
            return
        payload = self._json_body()
        if payload is None:
            return
        try:
            if set(payload) != {"revision", "settings"}:
                raise SettingsError("settings request has an invalid schema")
            document = settings_store().update(payload["settings"], payload["revision"])
        except RevisionConflict as exc:
            return self._send(409, json.dumps({"ok": False, "error": str(exc)}))
        except (OSError, SettingsError) as exc:
            return self._send(400, json.dumps({"ok": False, "error": str(exc)}))
        log_event(type="settings_updated", revision=document["revision"])
        return self._send(200, json.dumps({"ok": True, "settings": document, "attention": False}))

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/api/wacli-receipt" and not self._require_same_origin():
            return
        if url.path == "/api/ringtone-preview":
            payload = self._json_body(1024)
            if payload is None:
                return
            try:
                if set(payload) != {"ringtone_id"}:
                    raise SettingsError("ringtone preview request is invalid")
                preview_ringtone(payload["ringtone_id"])
            except SettingsError as exc:
                return self._send(409, json.dumps({"ok": False, "error": str(exc)}))
            return self._send(202, json.dumps({"ok": True}))
        if url.path in {"/whatsapp/pair/start", "/whatsapp/pair/cancel", "/whatsapp/unlink"}:
            payload = self._form_body()
            if payload is None:
                return
            try:
                engine = pairing_engine()
                if url.path == "/whatsapp/pair/start":
                    if set(payload) != {"phone"}:
                        raise PairingError("phone_number_invalid")
                    result = engine.start(normalize_phone(payload["phone"]))
                elif url.path == "/whatsapp/pair/cancel":
                    if payload:
                        raise PairingError("pairing_request_invalid")
                    result = engine.cancel()
                else:
                    if payload != {"confirm": "unlink"}:
                        raise PairingError("unlink_confirmation_required")
                    result = engine.relink()
                phase = "WHATSAPP_READY" if result.get("status") == "ready" else "WHATSAPP_PENDING"
                return self._send(200, json.dumps({"mode": "RUNTIME", "phase": phase, "whatsapp": result}))
            except PairingError as exc:
                code = 400 if str(exc) in {"phone_number_invalid", "unlink_confirmation_required"} else 409
                return self._send(code, json.dumps({"error": str(exc).replace("_", " ")}))
            except OSError:
                return self._send(503, json.dumps({"error": "WhatsApp pairing is unavailable"}))
        if url.path in {
            "/recipients/refresh",
            "/recipients/select",
            "/recipients/select-number",
            "/recipients/add",
            "/recipients/add-number",
            "/recipients/remove",
            "/recipients/default",
            "/recipients/defer",
        }:
            payload = self._form_body()
            if payload is None:
                return
            try:
                engine = pairing_engine()
                if url.path == "/recipients/refresh":
                    if payload:
                        raise PairingError("recipient_request_invalid")
                    result = engine.recipient_list(refresh=True)
                elif url.path == "/recipients/defer":
                    if payload:
                        raise PairingError("recipient_request_invalid")
                    result = engine.recipient_defer()
                elif url.path in {"/recipients/select-number", "/recipients/add-number"}:
                    if set(payload) != {"phone"}:
                        raise PairingError("phone_number_invalid")
                    operation = (
                        engine.recipient_select_phone
                        if url.path == "/recipients/select-number"
                        else engine.recipient_add_phone
                    )
                    result = operation(normalize_phone(payload["phone"]))
                else:
                    if set(payload) != {"token"}:
                        raise PairingError("recipient_request_invalid")
                    operation = {
                        "/recipients/add": engine.recipient_add,
                        "/recipients/select": engine.recipient_select,
                        "/recipients/remove": engine.recipient_remove,
                        "/recipients/default": engine.recipient_default,
                    }[url.path]
                    result = operation(payload["token"])
                return self._send(200, json.dumps(result))
            except PairingError as exc:
                return self._send(409, json.dumps({"error": str(exc).replace("_", " ")}))
            except OSError:
                return self._send(503, json.dumps({"error": "Recipient setup is unavailable"}))
        if url.path in {"/nfc/enroll", "/nfc/cancel-runtime", "/nfc/unpair-presented"}:
            payload = self._form_body()
            if payload is None:
                return
            try:
                if url.path == "/nfc/enroll":
                    if set(payload) != {"token"}:
                        raise NfcError("recipient token is invalid")
                    candidate = pairing_engine().recipients.configured_candidate(payload["token"])
                    nfc_router().begin_enrollment(
                        label=candidate["label"],
                        jid=candidate["jid"],
                        ttl_s=120,
                        create_contact=False,
                    )
                    return self._send(
                        202,
                        json.dumps({"status": "waiting", "recipient": candidate["label"]}),
                    )
                if payload:
                    raise NfcError("NFC request is invalid")
                if url.path == "/nfc/cancel-runtime":
                    nfc_router().cancel_enrollment()
                    return self._send(200, json.dumps({"status": "idle"}))
                selection = active_selection(CONTACTS_FILE, NFC_SELECTION_FILE)
                if selection is None:
                    raise NfcError("Present a paired NFC card, then try again")
                ContactStore(CONTACTS_FILE).remove_card(selection["uid"])
                Path(NFC_SELECTION_FILE).unlink(missing_ok=True)
                return self._send(200, json.dumps({"status": "unpaired"}))
            except (ContactError, NfcError, PairingError) as exc:
                return self._send(409, json.dumps({"error": str(exc)}))
            except OSError:
                return self._send(503, json.dumps({"error": "NFC setup is unavailable"}))
        if url.path == "/api/wifi-change":
            payload = self._json_body(2048)
            if payload is None:
                return
            try:
                request_wifi_change(payload)
                return self._send(
                    202,
                    json.dumps(
                        {
                            "status": "connecting",
                            "message": "Reconnect this phone after Button Box changes networks",
                        }
                    ),
                )
            except WifiChangeError as exc:
                return self._send(409, json.dumps({"error": str(exc)}))
            except OSError:
                return self._send(503, json.dumps({"error": "Wi-Fi change is unavailable"}))
        if url.path == "/api/wacli-receipt":
            if not WACLI_WEBHOOK_SECRET:
                return self._send(
                    503,
                    json.dumps({"ok": False, "error": "receipt webhook is not configured"}),
                )
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > MAX_WEBHOOK_BYTES:
                    raise ValueError("invalid request size")
                body = self.rfile.read(length)
                if not valid_webhook_signature(
                    WACLI_WEBHOOK_SECRET,
                    body,
                    self.headers.get("X-Wacli-Signature"),
                ):
                    return self._send(
                        401, json.dumps({"ok": False, "error": "invalid signature"})
                    )
                payload = json.loads(body)
                notices = listened_store().ingest_played(
                    payload,
                    load_listener_profiles(CONTACTS_FILE),
                    LISTENED_FALLBACK_WAV,
                )
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                return self._send(
                    400, json.dumps({"ok": False, "error": str(exc)})
                )
            for notice in notices:
                log_event(
                    type="listen_receipt",
                    listener=notice.listener_name,
                    whatsapp_id=notice.whatsapp_id,
                )
            return self._send(
                202,
                json.dumps({"ok": True, "queued": len(notices)}),
            )
        if url.path == "/api/contacts":
            if not self._require_json():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 4096:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("request must be an object")
                action = payload.get("action")
                jid = payload.get("jid")
                store = contacts_store()
                if action == "add":
                    candidate = validate_contact(jid, payload.get("label"))
                    discovered = {chat["jid"] for chat in discover_whatsapp_chats()}
                    if candidate["jid"] not in discovered:
                        raise ContactError("contact must be a discovered WhatsApp chat")
                    store.add_contact(candidate["jid"], candidate["label"])
                    event_type = "dash_contact_added"
                elif action == "remove":
                    if not store.remove_contact(jid):
                        raise ContactError("contact does not exist")
                    event_type = "dash_contact_removed"
                else:
                    raise ValueError("contact action is invalid")
            except (
                ContactError,
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as exc:
                return self._send(
                    400, json.dumps({"ok": False, "error": str(exc)})
                )
            public = store.public_view()
            log_event(type=event_type, contact_count=len(public["contacts"]))
            return self._send(
                200, json.dumps({"ok": True, "revision": public["revision"]})
            )
        if url.path == "/api/listeners":
            if not self._require_json():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 4096:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("request must be an object")
                action = payload.get("action")
                store = contacts_store()
                if action == "upsert":
                    store.upsert_listener(
                        payload.get("jid"),
                        payload.get("name"),
                        listened_clip=payload.get("listened_clip", ""),
                    )
                    event_type = "dash_listener_upserted"
                elif action == "remove":
                    if not store.remove_listener(payload.get("jid")):
                        raise ContactError("listener does not exist")
                    event_type = "dash_listener_removed"
                else:
                    raise ValueError("listener action is invalid")
            except (
                ContactError, OSError, TypeError, ValueError, json.JSONDecodeError,
            ) as exc:
                return self._send(400, json.dumps({"ok": False, "error": str(exc)}))
            public = store.public_view()
            log_event(type=event_type, listener_count=len(public["listeners"]))
            return self._send(
                200, json.dumps({"ok": True, "revision": public["revision"]})
            )
        if url.path == "/api/ring":
            try:
                # O_EXCL makes repeated taps idempotent until the button service
                # consumes the request. The marker persists while it is busy.
                os.makedirs(os.path.dirname(RING_REQUEST_FILE) or ".", exist_ok=True)
                fd = os.open(RING_REQUEST_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
            except OSError as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}))
            return self._send(202, '{"ok":true,"status":"queued"}')
        q = urllib.parse.parse_qs(url.query or "")
        if url.path == "/api/hold":
            source_kind, source_dir, destination_dir = "queue", QUEUE_DIR, HOLD_DIR
            ev = "dash_hold"
        elif url.path == "/api/resume":
            source_kind, source_dir, destination_dir = "hold", HOLD_DIR, QUEUE_DIR
            ev = "dash_resume"
        elif url.path == "/api/delete":
            source_kind, source_dir, destination_dir = "queue", QUEUE_DIR, TRASH_DIR
            ev = "dash_delete"
        elif url.path == "/api/reinstate":
            source_kind, source_dir, destination_dir = "trash", TRASH_DIR, QUEUE_DIR
            ev = "dash_reinstate"
        else:
            return self._send(404, "{}")
        token = (q.get("f") or [""])[0]
        name = resolve_message_token(token, source_kind)
        if not name:
            return self._send(400, "{}")
        try:
            move_queue_message(
                source_dir,
                destination_dir,
                name,
                exposing_to_player=destination_dir == QUEUE_DIR,
            )
        except FileNotFoundError:
            return self._send(
                409,
                json.dumps({"ok": False, "error": "Message was claimed by Button Box"}),
            )
        except FileExistsError:
            return self._send(
                409,
                json.dumps({"ok": False, "error": "Message already exists in destination"}),
            )
        except OSError as exc:
            return self._send(
                500, json.dumps({"ok": False, "error": str(exc)})
            )
        log_event(type=ev, file=name)
        return self._send(200, '{"ok":true}')

    def log_message(self, fmt, *args):  # quiet the access log
        pass


def dashboard_bind_addresses(bind, tailscale_host):
    """Return the narrow LAN listener plus loopback for Tailscale Serve."""
    primary = resolve_bind(bind)
    addresses = [primary]
    if tailscale_host and primary not in {"127.0.0.1", "0.0.0.0"}:
        addresses.append("127.0.0.1")
    return addresses


def serve_dashboard(server_factory=ThreadingHTTPServer):
    device_name = normalize_device_name(socket.gethostname())
    local_host = f"{device_name}.local"
    tailscale_host = normalize_tailscale_host(
        TAILSCALE_HOST_SETTING, device_host=device_name
    )
    handler = type(
        "ConfiguredHandler",
        (Handler,),
        {"local_host": local_host, "tailscale_host": tailscale_host},
    )
    addresses = dashboard_bind_addresses(BIND, tailscale_host)
    os.makedirs(HOLD_DIR, exist_ok=True)
    os.makedirs(TRASH_DIR, exist_ok=True)
    servers = [server_factory((address, PORT), handler) for address in addresses]
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(
        "dashboard on " + ", ".join(f"http://{address}:{PORT}" for address in addresses),
        flush=True,
    )
    servers[0].serve_forever()


if __name__ == "__main__":
    serve_dashboard()
