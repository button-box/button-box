"""Loopback-only UI preview with synthetic data; never connects to a device.

Run: python3 scripts/dev/dashboard-preview.py [--port 8766] [--state ready|setup|attention]
Serves the actual shared dashboard assets. Mutations below are in-memory mocks,
not backend integration tests. Restart to discard all settings changes.
"""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from messagebox.settings import defaults  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--state", choices=("ready", "setup", "attention"), default="ready")
    args = parser.parse_args()
    settings = defaults({"TZ": "Europe/Lisbon"})
    state = {
        "mode": "RUNTIME", "phase": "COMPLETE", "box_id": "BOX-42",
        "setup": {key: "complete" for key in ("wifi", "whatsapp", "recipient", "first_message", "nfc")},
        "health": {"wifi": "connected", "network_name": "Example home", "whatsapp": "linked", "runtime": "running", "software_version": "local preview"},
    }
    if args.state == "attention":
        state["setup"]["first_message"] = "attention"
        state["box_id"] = None
    if args.state == "setup":
        state = {"mode": "HOTSPOT", "phase": "WIFI_SELECT", "box_id": None, "whatsapp": {"status": "idle"}}

    class Handler(BaseHTTPRequestHandler):
        def send(self, payload, code=200, content_type="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            assets = {"/": ("index.html", "text/html"), **{
                f"/static/{name}": (name, mime) for name, mime in
                (("app.js", "text/javascript"), ("clipboard.js", "text/javascript"), ("styles.css", "text/css"))
            }}
            if path in assets:
                name, mime = assets[path]
                body = (ROOT / "messagebox/onboarding/static" / name).read_bytes()
                if name == "index.html":
                    body = body.replace(b"__MESSAGEBOX_URL__", b"http://button-box-example.local/")
                return self.send(body, content_type=mime)
            responses = {
                "/api/state": state,
                "/api/settings": {"settings": settings, "attention": False},
                "/api/data": {"cards": {"sent_total": 4, "recv_total": 6, "plays": 6, "rings": 3},
                              "interactions": [{"outcome_label": "Message sent", "flow": "reply", "ts": 1789423200}],
                              "queue": [], "hold": [], "trash": []},
                "/api/contacts": {"listeners": {}},
                "/api/networks": {"networks": [{"ssid": "Example home", "security": "encrypted", "signal": 85}]},
            }
            if path in responses:
                return self.send(responses[path])
            return self.send({"error": "Not included in this local preview"}, 404)

        def do_POST(self):
            if self.path == "/api/ring":
                return self.send({"queued": True}, 202)
            return self.send({"error": "Preview only: no device actions"}, 409)

        def do_PUT(self):
            if self.path != "/api/settings":
                return self.send({"error": "Preview only"}, 404)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                return self.send({"error": "Invalid preview request"}, 400)
            data = json.loads(self.rfile.read(length))
            if data.get("revision") != settings["revision"]:
                return self.send({"error": "Settings changed. Reload before saving."}, 409)
            settings.update(data["settings"])
            settings["revision"] += 1
            return self.send({"settings": settings, "attention": False})

        def log_message(self, *_args):
            pass

    print(f"Synthetic {args.state} preview: http://127.0.0.1:{args.port}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
