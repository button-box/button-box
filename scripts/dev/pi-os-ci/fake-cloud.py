#!/usr/bin/env python3
"""CI only: a stand-in for the Button Box Cloud device API, so the installer's
registration never reaches the real Cloud. run.sh points button.box at
127.0.0.1 and trusts a throwaway CA for this certificate.

Usage: fake-cloud.py CERT KEY
"""
import json
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/cloud-api/v1/device/"
SETUP_CODE = "CI2345"


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return self.headers.get("Authorization", "").startswith("Bearer ")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self.authorized():
            return self.reply(401, {"error": {"code": "device_auth_invalid"}})
        if self.path == PREFIX + "register":
            token = "ci-claim-token-0001"
            return self.reply(200, {"device_id": "ci", "box_id": "ci-box-0000000001", "claim_id": "ci-claim-0000000001",
                                    "claim_token": token, "expires_at": int(time.time()) + 600,
                                    "whatsapp_url": "https://wa.me/15550000000?text=claim%20" + token})
        if self.path == PREFIX + "setup-checkin":
            return self.reply(200, {"claimed": False, "setup_code": SETUP_CODE,
                                    "setup_url": "https://button.box/s/" + SETUP_CODE, "pending_claim": None})
        return self.reply(404, {"error": {"code": "route_not_found"}})

    def do_GET(self):
        if self.path == PREFIX + "claim" and self.authorized():
            return self.reply(200, {"claimed": False, "box_id": "ci-box-0000000001", "expires_at": None})
        return self.reply(404, {"error": {"code": "route_not_found"}})

    def log_message(self, fmt, *args):
        print("fake-cloud: " + fmt % args, flush=True)


def main():
    cert, key = sys.argv[1:3]
    server = ThreadingHTTPServer(("127.0.0.1", 443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
