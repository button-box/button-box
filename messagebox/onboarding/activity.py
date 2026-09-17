"""Content-free event projection for the unprivileged setup portal."""
import json
import math
from collections import deque
from pathlib import Path


def setup_activity(events_path):
    """Read content-free history without opening WhatsApp or mutating setup."""
    labels = {
        "sent": "Accepted for sending",
        "received": "Voice message received",
        "played": "Message played",
        "ring": "Incoming message ring",
        "guided_session_started": "Voice session started",
        "guided_review_played": "Recording played back",
        "guided_approved": "Send approved",
        "outbox_retry": "Waiting to retry sending",
    }
    cards = dict.fromkeys(("sent_total", "recv_total", "plays", "rings"), 0)
    counters = {"sent": "sent_total", "received": "recv_total", "played": "plays", "ring": "rings"}
    recent = deque(maxlen=30)
    try:
        with Path(events_path).open(encoding="utf-8") as source:
            for line in source:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind, ts = event.get("type"), event.get("ts")
                if not isinstance(kind, str) or kind not in labels:
                    continue
                if type(ts) not in (int, float) or not math.isfinite(ts):
                    continue
                if kind in counters:
                    cards[counters[kind]] += 1
                recent.append({"ts": ts, "outcome_label": labels[kind], "flow": event.get("flow") if event.get("flow") in ("reply", "standalone") else None})
    except FileNotFoundError:
        pass
    # No queue/audio/recipient identifiers or mutation controls during setup.
    return {
        "cards": cards,
        "interactions": list(reversed(recent)),
        "queue": [],
        "recently_played": [],
        "hold": [],
        "trash": [],
    }
