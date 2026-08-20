#!/usr/bin/env python3
"""Message Box dashboard: stats + queue manager, served over Tailscale.

Stdlib only (http.server) — the Pi Zero doesn't need Flask. Reads the
events JSONL written by voicepoll/button_send and the queue dir. Held messages
go to QUEUE_DIR/.hold and are skipped until reinstated. Deleted messages go to
QUEUE_DIR/.trash (also reinstatable), so test messages never reach the kids but
nothing is lost by accident.

Endpoints:
  GET  /               dashboard HTML
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
import os
import subprocess
import threading
import time
import urllib.parse
import wave
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from contacts import ContactError, ContactStore, validate_contact
except ModuleNotFoundError:  # Support import-by-path in local tests.
    from src.contacts import ContactError, ContactStore, validate_contact

try:
    from runtime_paths import APP_DIR, CONTACTS_FILE, OUTBOX_DIR as DEFAULT_OUTBOX_DIR
    from runtime_paths import QUEUE_DIR as DEFAULT_QUEUE_DIR
    from runtime_paths import RUNTIME_DIR, STATE_DIR
except ModuleNotFoundError:
    from src.runtime_paths import APP_DIR, CONTACTS_FILE, OUTBOX_DIR as DEFAULT_OUTBOX_DIR
    from src.runtime_paths import QUEUE_DIR as DEFAULT_QUEUE_DIR
    from src.runtime_paths import RUNTIME_DIR, STATE_DIR

try:
    from listened_receipts import (
        MAX_WEBHOOK_BYTES,
        ReceiptStore,
        load_listener_profiles,
        valid_webhook_signature,
    )
except ModuleNotFoundError:  # Support import-by-path in local tests.
    from src.listened_receipts import (
        MAX_WEBHOOK_BYTES,
        ReceiptStore,
        load_listener_profiles,
        valid_webhook_signature,
    )

BIND = os.environ.get("MSGBOX_DASH_BIND", "").strip()
PORT = int(os.environ.get("MSGBOX_DASH_PORT", "8080"))
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
RING_REQUEST_FILE = str(RUNTIME_DIR / "ring-request")
QUEUE_ACTION_LOCK = threading.Lock()


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
        "empty" if contact_count == 0 else "automatic" if contact_count == 1 else "cards"
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
    for item in queue + hold + trash:
        meta = file_meta.get(item["file"], {})
        item["chat"] = label(meta.get("chat", "")) or "?"
        item["sender"] = meta.get("sender") or "?"

    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
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


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Message Box</title><style>
:root{--bg:#12141a;--card:#1c2028;--ink:#e8eaf0;--dim:#8a90a0;--acc:#f5b942;--ok:#5dbb63;--bad:#e05555}
*{box-sizing:border-box;margin:0}body{background:var(--bg);color:var(--ink);
font:15px/1.45 -apple-system,system-ui,sans-serif;padding:24px;max-width:960px;margin:0 auto}
h1{font-size:22px;margin-bottom:4px}h2{font-size:15px;color:var(--dim);margin:26px 0 10px;
text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--dim);font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-top:16px}
.card{background:var(--card);border-radius:10px;padding:12px 14px}
.card b{font-size:24px;display:block}.card span{color:var(--dim);font-size:12px}
.ringbox{display:flex;align-items:center;gap:12px;background:var(--card);border-radius:10px;padding:14px}
.ring{background:var(--acc);color:#241b08;font-size:15px;font-weight:700;padding:10px 18px}
.ring:disabled{opacity:.55;cursor:default}.ringstatus{color:var(--dim);font-size:13px}
.contactpanel{background:var(--card);border-radius:10px;padding:14px;margin-bottom:10px}.contactmode{color:#cbd0d9;
font-size:13px;margin-bottom:12px}.contactlist,.listenerlist{display:grid;gap:7px;margin-bottom:12px}
.contactrow{display:grid;grid-template-columns:minmax(160px,1fr) auto;gap:10px;align-items:center;
background:#262b35;border-radius:8px;padding:10px 11px}.contactname{font-weight:650}.contactmeta{display:block;
color:var(--dim);font-size:11px;margin-top:2px}.contactform{display:grid;grid-template-columns:minmax(150px,1fr)
minmax(130px,1fr) auto;gap:8px;align-items:end}.listenerform{grid-template-columns:minmax(130px,1fr)
minmax(110px,1fr) minmax(160px,1fr) auto}.contactform label{color:var(--dim);font-size:11px}
.contactform input,.contactform select{display:block;width:100%;margin-top:4px;background:#262b35;color:var(--ink);
border:1px solid #353c49;border-radius:7px;padding:8px 9px;font:inherit}.contactstatus{color:var(--dim);
font-size:12px;margin:8px 0}.contactactions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.secondary{background:#39404c}
.listenertitle{font-size:13px;color:#cbd0d9;margin:20px 0 9px}.listenerjid,.listenerclip{display:block;color:var(--dim);
font-size:11px;overflow-wrap:anywhere}.remove{background:var(--bad)}
.chart{background:var(--card);border-radius:10px;padding:14px;margin-top:8px}
.bars{display:flex;align-items:flex-end;gap:4px;height:110px}
.bcol{flex:1;display:flex;flex-direction:column;justify-content:flex-end;gap:2px}
.bar{border-radius:3px 3px 0 0;min-height:2px}.bar.s{background:var(--acc)}.bar.r{background:#5a8dee}
.blab{font-size:9px;color:var(--dim);text-align:center;margin-top:4px;overflow:hidden}
.legend{font-size:12px;color:var(--dim);margin-top:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 12px}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden}
td,th{padding:9px 12px;text-align:left;font-size:13px;border-top:1px solid #262b35}
th{color:var(--dim);font-weight:500;border:0}
button{border:0;border-radius:7px;padding:6px 12px;font-size:12px;cursor:pointer;color:#fff}
.actions{display:flex;gap:6px;flex-wrap:wrap}.del{background:var(--bad)}.hold{background:#a46f17}.rei{background:var(--ok)}
audio{height:30px;max-width:190px;vertical-align:middle}
.empty{color:var(--dim);padding:14px;background:var(--card);border-radius:10px;font-size:13px}
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.summarycard{background:var(--card);
border-radius:10px;padding:14px}.summarycard b{display:block;font-size:26px}.summarycard span{font-size:12px;
color:var(--dim)}.summarycard small{display:block;color:#b9bfca;margin-top:5px;font-size:11px}
.interactions{display:grid;gap:8px}.interaction{background:var(--card);border-radius:10px;padding:13px 14px}
.itop{display:flex;justify-content:space-between;gap:12px;align-items:center}.ititle{font-weight:650}
.itime{color:var(--dim);font-size:12px;white-space:nowrap}.railwrap{overflow-x:auto;margin-top:13px;padding:2px 0}
.rail{display:flex;min-width:460px}.tstage{position:relative;flex:1;text-align:center;color:var(--dim);font-size:10px}
.tstage:not(:last-child)::after{content:"";position:absolute;left:50%;right:-50%;top:9px;height:2px;
background:#353b47;z-index:0}.tstage.done:not(:last-child)::after{background:var(--ok)}
.tdot{position:relative;z-index:1;width:20px;height:20px;border-radius:50%;margin:0 auto 5px;
display:grid;place-items:center;background:#353b47;color:#111;font-size:12px;font-weight:800}
.tstage.done .tdot{background:var(--ok)}.tstage.done .tdot::after{content:"✓"}
.tstage.failed{color:#ff8b8b}.tstage.failed .tdot{background:var(--bad);color:#fff}.tstage.failed .tdot::after{content:"!"}
.tstage.current{color:#f5c45a}.tstage.current .tdot{background:var(--acc)}.tstage.current .tdot::after{content:"•"}
.tstage.muted .tdot::after{content:""}
.imeta{color:var(--dim);font-size:12px;margin-top:8px}.outcome{font-size:11px;font-weight:700;
border-radius:999px;padding:4px 8px;white-space:nowrap}.outcome.ok{background:#203a28;color:#77d681}
.outcome.warn{background:#45391f;color:#f5c45a}.outcome.bad{background:#472528;color:#ff8585}
.outcome.dim{background:#2a2e37;color:#aeb4c1}.privacy{color:var(--dim);font-size:12px;margin:7px 0 10px}
.morebtn{display:block;margin:10px auto 0;background:#2a2f39;color:#cbd0d9}.morestats{margin-top:26px}
.morestats>summary{cursor:pointer;color:var(--dim);font-weight:650;letter-spacing:.04em;text-transform:uppercase;
font-size:13px;padding:10px 0}.morestats[open]>summary{margin-bottom:10px}
@media(max-width:600px){body{padding:16px}.itop{align-items:flex-start}.contactform,.listenerform{grid-template-columns:1fr}
.summary{grid-template-columns:repeat(2,1fr)}.rail{min-width:400px}}
</style></head><body>
<h1>📮 Message Box</h1><div class="sub" id="gen"></div>
<h2>Contacts</h2><section class="contactpanel"><div id="contactmode" class="contactmode">Loading contacts…</div>
<div id="contactlist" class="contactlist"></div>
<div class="contactform"><label>WhatsApp chat<select id="contactjid" onchange="selectContact()"></select></label>
<label>Display name<input id="contactlabel" maxlength="80"></label><button class="rei" id="contactadd" onclick="addContact()">Add contact</button></div>
<div class="contactactions"><button class="secondary" id="contactrefresh" onclick="loadContacts(true)">Refresh WhatsApp chats</button>
<span class="contactstatus" id="contactstatus"></span></div>
<h3 class="listenertitle">Listener profiles</h3><div id="listenerlist" class="listenerlist"></div>
<div class="contactform listenerform"><label>Listener JID<input id="listenerjid" placeholder="15550001@s.whatsapp.net"></label>
<label>Name<input id="listenername" maxlength="80"></label><label>Optional listened clip<input id="listenerclip" placeholder="/var/lib/messagebox/assets/listened.wav"></label>
<button class="rei" onclick="saveListener()">Save profile</button></div><div class="contactstatus" id="listenerstatus"></div></section>
<h2>On demand</h2><div class="ringbox">
<button class="ring" id="ring" onclick="ringNow()">🔔 Ring the box</button>
<span class="ringstatus" id="ringstatus">Call the kids when they're home.</span></div>
<h2>At a glance — last 7 days</h2><div class="summary" id="behavior"></div>
<h2>What happened</h2>
<div class="privacy">Content-free timeline: no audio, transcripts, message IDs or phone numbers.</div>
<div class="interactions" id="interactions"></div>
<button class="morebtn" id="moreInteractions" onclick="toggleInteractions()" hidden></button>
<details class="morestats"><summary>More stats</summary><div class="cards" id="cards"></div>
<h2>Last 14 days</h2><div class="chart"><div class="bars" id="bars"></div>
<div class="legend">per day:<i style="background:var(--acc)"></i>sent<i style="background:#5a8dee"></i>received</div></div>
<h2>By chat</h2><div id="bychat"></div></details>
<h2>Queue — plays next</h2><div id="queue"></div>
<h2>On hold — skipped until reinstated</h2><div id="hold"></div>
<h2>Trash — deleted, reinstatable</h2><div id="trash"></div>
<script>
async function act(ep,f){
 try{
  const r=await fetch('/api/'+ep+'?f='+f,{method:'POST'});
  if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.error||'Could not move message')}
 }catch(e){alert(e.message)}
 await load()
}
async function ringNow(){
 const btn=document.getElementById('ring'),status=document.getElementById('ringstatus');
 btn.disabled=true;status.textContent='Sending ring…';
 try{
  const r=await fetch('/api/ring',{method:'POST'});
  if(!r.ok)throw new Error();
  status.textContent='Ring requested ✓';
 }catch(e){status.textContent='Could not request ring.'}
 setTimeout(()=>{btn.disabled=false;status.textContent="Call the kids when they're home."},3000);
}
let contactsData=null;
function selectContact(){
 const jid=document.getElementById('contactjid').value;
 const chat=contactsData&&contactsData.discovered.find(item=>item.jid==jid);
 if(chat)document.getElementById('contactlabel').value=chat.label;
}
function renderContacts(d){
 contactsData=d;
 const contacts=Object.entries(d.contacts),automatic=contacts.length==1;
 document.getElementById('contactmode').textContent=contacts.length==0?'No contacts configured. Add a WhatsApp chat to begin.':
  automatic?'This contact is the automatic destination for new messages.':'Cards select the outgoing contact for new messages.';
 document.getElementById('contactlist').innerHTML=contacts.length?contacts.map(([jid,c])=>{
  const state=(automatic?'automatic · ':'')+(c.paired?'paired':'unpaired')+' · '+c.card_count+' card'+(c.card_count==1?'':'s');
  return '<div class="contactrow"><div><span class="contactname">'+esc(c.label)+'</span><span class="contactmeta">'+esc(c.kind)+' · '+state+'</span></div>'+
   '<button class="remove" data-jid="'+esc(jid)+'" onclick="removeContact(this.dataset.jid)">Remove</button></div>'}).join(''):
  '<div class="empty">No contacts yet.</div>';
 const choices=d.discovered.filter(chat=>!chat.configured),select=document.getElementById('contactjid');
 select.innerHTML=choices.length?choices.map(chat=>'<option value="'+esc(chat.jid)+'">'+esc(chat.label)+' ('+esc(chat.kind)+')</option>').join(''):
  '<option value="">No unconfigured chats found</option>';
 document.getElementById('contactadd').disabled=!choices.length;selectContact();
 const listeners=Object.entries(d.listeners);
 document.getElementById('listenerlist').innerHTML=listeners.length?listeners.map(([jid,p])=>
  '<div class="contactrow"><div><span class="contactname">'+esc(p.name)+'</span><span class="listenerjid">'+esc(jid)+'</span><span class="listenerclip">'+
  esc(p.listened_clip||'Default listened sound')+'</span></div><div class="actions"><button class="secondary" data-jid="'+esc(jid)+'" onclick="editListener(this.dataset.jid)">Edit</button>'+
  '<button class="remove" data-jid="'+esc(jid)+'" onclick="removeListener(this.dataset.jid)">Remove</button></div></div>').join(''):
  '<div class="empty">No listener profiles.</div>';
}
async function loadContacts(refresh=false){
 const btn=document.getElementById('contactrefresh'),status=document.getElementById('contactstatus');
 btn.disabled=true;status.textContent=refresh?'Syncing WhatsApp…':'Loading…';
 try{
  const r=await fetch('/api/contacts'+(refresh?'?refresh=1':'')),d=await r.json();
   if(!r.ok)throw new Error(d.error||'Could not load contacts');renderContacts(d);status.textContent=d.discovery_error||(refresh?'Refreshed ✓':'');
 }catch(e){status.textContent=e.message}
 btn.disabled=false;
}
async function contactMutation(payload){
 const status=document.getElementById('contactstatus');status.textContent='Saving…';
 try{
  const r=await fetch('/api/contacts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await r.json();
  if(!r.ok)throw new Error(d.error||'Could not save contact');await loadContacts();status.textContent='Saved ✓';
 }catch(e){status.textContent=e.message}
}
function addContact(){contactMutation({action:'add',jid:document.getElementById('contactjid').value,label:document.getElementById('contactlabel').value})}
function removeContact(jid){contactMutation({action:'remove',jid})}
function editListener(jid){
 const p=contactsData.listeners[jid];document.getElementById('listenerjid').value=jid;
 document.getElementById('listenername').value=p.name;document.getElementById('listenerclip').value=p.listened_clip;
}
async function listenerMutation(payload){
 const status=document.getElementById('listenerstatus');status.textContent='Saving…';
 try{
  const r=await fetch('/api/listeners',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await r.json();
  if(!r.ok)throw new Error(d.error||'Could not save listener');await loadContacts();status.textContent='Saved ✓';
 }catch(e){status.textContent=e.message}
}
function saveListener(){listenerMutation({action:'upsert',jid:document.getElementById('listenerjid').value,
 name:document.getElementById('listenername').value,listened_clip:document.getElementById('listenerclip').value})}
function removeListener(jid){listenerMutation({action:'remove',jid})}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function fmtd(s){
 if(s==null)return '—';
 if(s<60)return s.toFixed(0)+'s';
 if(s<3600)return (s/60).toFixed(1)+'m';
 if(s<86400)return (s/3600).toFixed(1)+'h';
 return (s/86400).toFixed(1)+'d';
}
function fmtt(ts){return new Date(ts*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}
function pct(v){return v==null?'—':v+'%'}
let allInteractions=[],interactionsExpanded=false;
function interactionRows(items){
 if(!items.length)return '<div class="empty">No guided interactions yet.</div>';
 return items.map(i=>{
  const who=i.flow=='standalone'?'Child · new message':esc(i.sender)+' · reply';
  const meta=[];
  if(i.chat)meta.push(esc(i.chat));
  if(i.wait_to_play_s!=null&&i.source_confidence=='exact')meta.push('waited '+fmtd(i.wait_to_play_s)+' to play');
  if(i.duration!=null)meta.push(fmtd(i.duration)+' recording');
  const rail=i.stages.map(s=>'<div class="tstage '+esc(s.state)+'"><div class="tdot"></div><span>'+esc(s.label)+'</span></div>').join('');
  return '<div class="interaction"><div class="itop"><div><div class="ititle">'+who+'</div>'+
   '<div class="itime">'+fmtt(i.ts)+'</div></div><span class="outcome '+esc(i.outcome_tone)+'">'+
   esc(i.outcome_label)+'</span></div><div class="railwrap"><div class="rail">'+rail+
   '</div></div>'+(meta.length?'<div class="imeta">'+meta.join(' · ')+'</div>':'')+'</div>'}).join('');
}
function renderInteractions(){
 const shown=interactionsExpanded?allInteractions:allInteractions.slice(0,6);
 document.getElementById('interactions').innerHTML=interactionRows(shown);
 const btn=document.getElementById('moreInteractions'),extra=allInteractions.length-6;
 btn.hidden=extra<=0;btn.textContent=interactionsExpanded?'Show less':'Show '+extra+' more';
}
function toggleInteractions(){interactionsExpanded=!interactionsExpanded;renderInteractions()}
function rows(items,kind){
 if(!items.length)return '<div class="empty">'+(kind=='queue'?'Nothing waiting.':'Empty.')+'</div>';
 let btn=kind=='queue'?f=>'<div class="actions"><button class="hold" onclick="act(\\'hold\\',\\''+f+'\\')">hold</button><button class="del" onclick="act(\\'delete\\',\\''+f+'\\')">delete</button></div>'
        :kind=='hold'?f=>'<button class="rei" onclick="act(\\'resume\\',\\''+f+'\\')">reinstate</button>'
                     :f=>'<button class="rei" onclick="act(\\'reinstate\\',\\''+f+'\\')">reinstate</button>';
 const audioQuery=kind=='hold'?'?hold=1':kind=='trash'?'?trash=1':'';
 return '<table><tr><th>when</th><th>from</th><th>chat</th><th>len</th><th>listen</th><th></th></tr>'+
  items.map(i=>'<tr><td>'+fmtt(i.ts)+'</td><td>'+esc(i.sender)+'</td><td>'+esc(i.chat)+'</td><td>'+fmtd(i.dur)+
   '</td><td><audio controls preload="none" src="/audio/'+encodeURIComponent(i.file)+audioQuery+
   '"></audio></td><td>'+btn(encodeURIComponent(i.file))+'</td></tr>').join('')+'</table>'}
async function load(){
 const d=await (await fetch('/api/data')).json();
 document.getElementById('gen').textContent='updated '+d.generated;
 const c=d.cards;
 const cards=[['sent total',c.sent_total],['received total',c.recv_total],
  ['sent today',c.sent_today],['received today',c.recv_today],
  ['plays',c.plays],['rings',c.rings],['listened receipts',c.listened],
  ['waiting to announce',c.listened_pending],
  ['outbox pending',c.outbox_pending],['outbox attention',c.outbox_attention],
  ['avg sent len',c.avg_sent_dur?c.avg_sent_dur+'s':'—'],
 ['avg recv len',c.avg_recv_dur?c.avg_recv_dur+'s':'—'],
 ['avg wait to play',c.avg_wait_min?c.avg_wait_min+'m':'—']];
 document.getElementById('cards').innerHTML=cards.map(x=>'<div class="card"><b>'+x[1]+'</b><span>'+x[0]+'</span></div>').join('');
 const b=d.behavior;
 const noSend=b.no_speech+b.not_sent+b.incomplete;
 const behavior=[[pct(b.reply_rate),'reply rate',b.reply_approved+' of '+b.reply_sessions+' replies approved'],
  [pct(b.review_send_rate),'review → send',b.reviewed+' recordings reviewed'],
  [noSend,'sessions without a send',b.no_speech+' no speech · '+b.not_sent+' not approved · '+b.incomplete+' interrupted'],
  [b.standalone_sessions,'new messages','started directly by the kids']];
 document.getElementById('behavior').innerHTML=behavior.map(x=>'<div class="summarycard"><b>'+x[0]+'</b><span>'+x[1]+'</span><small>'+x[2]+'</small></div>').join('');
 allInteractions=d.interactions;renderInteractions();
 const mx=Math.max(1,...d.sent_per_day,...d.recv_per_day);
 document.getElementById('bars').innerHTML=d.days.map((day,i)=>{
  const s=d.sent_per_day[i],r=d.recv_per_day[i];
  return '<div class="bcol" title="'+day+': '+s+' sent, '+r+' recv">'+
   '<div class="bar s" style="height:'+(s/mx*80)+'px"></div>'+
   '<div class="bar r" style="height:'+(r/mx*80)+'px"></div>'+
   '<div class="blab">'+day.slice(5)+'</div></div>'}).join('');
 const bc=t=>t.length?'<table>'+t.map(x=>'<tr><td>'+esc(x[0])+'</td><td>'+x[1]+'</td></tr>').join('')+'</table>':'<div class="empty">No data yet.</div>';
 document.getElementById('bychat').innerHTML=
  '<h2 style="margin-top:0">incoming from</h2>'+bc(d.by_chat_in)+'<h2>outgoing to</h2>'+bc(d.by_chat_out);
 document.getElementById('queue').innerHTML=rows(d.queue,'queue');
 document.getElementById('hold').innerHTML=rows(d.hold,'hold');
 document.getElementById('trash').innerHTML=rows(d.trash,'trash');
}
load();loadContacts();setInterval(load,15000);
</script></body></html>"""


def safe_name(raw):
    name = os.path.basename(urllib.parse.unquote(raw))
    if not name.endswith(".wav") or "/" in name or name.startswith("."):
        return None
    return name


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _require_json(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send(415, json.dumps({"ok": False, "error": "application/json required"}))
            return False
        return True

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
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
            name = safe_name(url.path[len("/audio/"):])
            if not name:
                return self._send(400, "{}")
            query = urllib.parse.parse_qs(url.query or "")
            if query.get("hold") == ["1"]:
                d = HOLD_DIR
            elif query.get("trash") == ["1"]:
                d = TRASH_DIR
            else:
                d = QUEUE_DIR
            path = os.path.join(d, name)
            if not os.path.exists(path):
                return self._send(404, "{}")
            with open(path, "rb") as f:
                return self._send(200, f.read(), "audio/wav")
        return self._send(404, "{}")

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
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
        name = safe_name((q.get("f") or [""])[0])
        if not name:
            return self._send(400, "{}")
        if url.path == "/api/hold":
            src, dst = os.path.join(QUEUE_DIR, name), os.path.join(HOLD_DIR, name)
            ev = "dash_hold"
        elif url.path == "/api/resume":
            src, dst = os.path.join(HOLD_DIR, name), os.path.join(QUEUE_DIR, name)
            ev = "dash_resume"
        elif url.path == "/api/delete":
            src, dst = os.path.join(QUEUE_DIR, name), os.path.join(TRASH_DIR, name)
            ev = "dash_delete"
        elif url.path == "/api/reinstate":
            src, dst = os.path.join(TRASH_DIR, name), os.path.join(QUEUE_DIR, name)
            ev = "dash_reinstate"
        else:
            return self._send(404, "{}")
        try:
            move_queue_message(
                os.path.dirname(src),
                os.path.dirname(dst),
                name,
                exposing_to_player=os.path.dirname(dst) == QUEUE_DIR,
            )
        except FileNotFoundError:
            return self._send(
                409,
                json.dumps({"ok": False, "error": "Message was claimed by the box"}),
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


if __name__ == "__main__":
    if not BIND:
        raise SystemExit("configure MSGBOX_DASH_BIND")
    os.makedirs(HOLD_DIR, exist_ok=True)
    os.makedirs(TRASH_DIR, exist_ok=True)
    print(f"dashboard on http://{BIND}:{PORT}", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
