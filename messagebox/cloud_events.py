"""Optional work hints; only the runtime thread may consume HTTP work."""

from __future__ import annotations

import json
import random
import threading
import time
from urllib.parse import urlsplit

from messagebox.device_http import DEVICE_USER_AGENT


def connect_events(client):
    # Import lazily: older installations retain polling until the OS package exists.
    import websocket

    parsed = urlsplit(client.api_url)
    events_url = parsed._replace(scheme="wss", path=parsed.path + "/device/events").geturl()
    socket = websocket.create_connection(
        events_url,
        header={"Authorization": "Bearer " + client.identity["credential"],
                "User-Agent": DEVICE_USER_AGENT},
        timeout=5, redirect_limit=0, suppress_origin=True,
        http_no_proxy=["*"],
    )
    if socket.getstatus() != 101:
        socket.close(timeout=0)
        raise OSError("cloud events upgrade was rejected")
    return socket


class CloudWorkEvents:
    """A bounded reconnecting listener that only sets thread-safe events."""

    def __init__(self, client, *, connector=connect_events, monotonic=time.monotonic):
        self.client = client
        self.connector = connector
        self.monotonic = monotonic
        self.wake = threading.Event()
        self.connected = threading.Event()
        self.stopped = threading.Event()
        self.thread = None
        self.socket = None
        self.lock = threading.Lock()

    def start(self):
        self.thread = threading.Thread(target=self._run, name="cloud-work-events", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.wake.set()
        with self.lock:
            if self.socket is not None:
                try:
                    self.socket.abort()
                except OSError:
                    pass
        if self.thread is not None:
            self.thread.join(timeout=6)

    def _listen(self, socket):
        next_ping = self.monotonic() + 20
        pong_deadline = None
        while not self.stopped.is_set():
            now = self.monotonic()
            if pong_deadline is not None and now >= pong_deadline:
                raise OSError("cloud events heartbeat was lost")
            if now >= next_ping:
                socket.send("ping")
                pong_deadline = now + 10
                next_ping = now + 20
            deadline = pong_deadline if pong_deadline is not None else next_ping
            socket.settimeout(max(0.01, deadline - now))
            try:
                message = socket.recv()
            except TimeoutError:
                continue
            except Exception as exc:
                # websocket-client uses its own timeout type on supported Debian builds.
                if type(exc).__name__ == "WebSocketTimeoutException":
                    continue
                raise
            if not message:
                raise OSError("cloud events connection closed")
            if message == "pong":
                pong_deadline = None
            elif isinstance(message, str) and len(message) <= 64:
                try:
                    hint = json.loads(message)
                except ValueError:
                    continue
                if hint == {"type": "work"}:
                    self.wake.set()

    def _run(self):
        failures = 0
        while not self.stopped.is_set():
            opened_at = self.monotonic()
            delay = 0
            socket = None
            try:
                socket = self.connector(self.client)
                with self.lock:
                    self.socket = socket
                if self.stopped.is_set():
                    break
                self.connected.set()
                self.wake.set()  # Recover hints lost while disconnected.
                self._listen(socket)
            except Exception as exc:
                # Never log exception text: handshake errors may contain private headers.
                if isinstance(exc, ImportError) or getattr(exc, "status_code", None) in {401, 403, 404}:
                    delay = 300
            finally:
                self.connected.clear()
                self.wake.set()  # Resume legacy polling immediately after a disconnect.
                with self.lock:
                    self.socket = None
                if socket is not None:
                    try:
                        socket.close(timeout=0)
                    except Exception:
                        pass  # A failed close must not stop polling or future reconnects.
            if self.monotonic() - opened_at >= 20:
                failures = 0
            failures += 1
            delay = delay or min(60, 2 ** min(failures, 6)) + random.random()
            self.stopped.wait(delay)
