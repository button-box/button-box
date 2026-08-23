"""Dependency-free WSGI application for Message Box Wi-Fi onboarding."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl

from messagebox.onboarding.comitup_adapter import ComitupAdapter, ComitupError
from messagebox.onboarding.connectivity import ConnectivityChecker
from messagebox.onboarding.paths import ONBOARDING_CONFIG_PATH, ONBOARDING_STATE_PATH
from messagebox.onboarding.state import (
    PROOFS,
    SAFE_ERRORS,
    WHATSAPP_PENDING,
    WHATSAPP_PROOFS,
    WHATSAPP_READY,
    WIFI_ASSOCIATED,
    WIFI_CONNECTING,
    WIFI_FAILED,
    StateError,
    StateStore,
)
from messagebox.onboarding.whatsapp import (
    SAFE_ERRORS as WHATSAPP_SAFE_ERRORS,
    PairingError,
    WhatsAppPairingClient,
    normalize_pair_code,
    normalize_phone,
)


CONFIG_PATH = str(ONBOARDING_CONFIG_PATH)
STATE_PATH = str(ONBOARDING_STATE_PATH)
BODY_LIMIT = 8192
HANDOFF_DELAY = 3.0
HOTSPOT_HOST = "10.41.0.1"
HOTSPOT_URL = f"http://{HOTSPOT_HOST}/"
STATIC_DIR = Path(__file__).with_name("static")

_DEVICE_ID = re.compile(r"[A-Za-z0-9-]{1,32}\Z")
_CANONICAL_HOST = re.compile(r"button-box-[A-Za-z0-9-]{1,32}\.local\Z", re.IGNORECASE)
_HEX_PSK = re.compile(r"[0-9a-fA-F]{64}\Z")
_PERCENT_ESCAPE = re.compile(br"%(?![0-9A-Fa-f]{2})")
_PHONE_HINT = re.compile(r"WhatsApp number ending in [0-9]{4}\Z")
_OPEN_SECURITY = frozenset({"open", "unencrypted", "none"})
_PROTECTED_SECURITY = frozenset(
    {"protected", "encrypted", "secured", "wpa", "wpa2", "wpa3"}
)

_SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'; script-src 'self'; "
        "style-src 'self'; connect-src 'self'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Cache-Control", "no-store"),
)

_HANDOFF_HTML = b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connecting | Message Box</title>
  <style nonce="messagebox-handoff">body{margin:0;background:#05070a;color:#f5f1e8;font:18px/1.5 system-ui,sans-serif}.shell{min-height:100vh;display:grid;place-items:center;padding:24px;box-sizing:border-box}.card{max-width:34rem;background:#171a1d;border:1px solid #363b3d;border-radius:20px;padding:28px}.eyebrow{color:#69c5a5;text-transform:uppercase;letter-spacing:.12em;font-size:.75rem;font-weight:700}h1{line-height:1.1}.lede,.status{color:#adb7b0}.pulse{width:34px;height:34px;border:4px solid #363b3d;border-top-color:#69c5a5;border-radius:50%;animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.pulse{animation:none;border-color:#69c5a5}}.button{display:inline-block;color:#07120e;background:#69c5a5;padding:12px 18px;border-radius:10px;text-decoration:none;font-weight:700}</style>
  <script nonce="messagebox-handoff">"use strict";let remaining=120;const status=()=>{const element=document.getElementById("handoff-status");if(remaining>0){const minutes=Math.floor(remaining/60);const seconds=String(remaining%60).padStart(2,"0");element.textContent=`Connecting to home Wi-Fi... ${minutes}:${seconds} remaining`;}else{element.textContent="Still waiting. Rejoin home Wi-Fi, then try the setup URL.";}remaining=Math.max(0,remaining-1);};window.addEventListener("DOMContentLoaded",status);window.setInterval(status,1000);</script>
</head>
<body>
  <main class="shell">
    <section class="card handoff" aria-labelledby="handoff-title">
      <p class="eyebrow">Wi-Fi setup</p>
      <div class="pulse" aria-hidden="true"></div>
      <h1 id="handoff-title">Switching to home Wi-Fi</h1>
      <p class="lede">The setup network will disappear. That is expected.</p>
      <ol class="steps">
        <li>Reconnect this phone to your home Wi-Fi.</li>
        <li>Open <strong>__MESSAGEBOX_URL__</strong> to continue with WhatsApp.</li>
      </ol>
      <p class="status" id="handoff-status" role="status" aria-live="polite">Connecting to home Wi-Fi... 2:00 remaining</p>
      <a class="button secondary" href="__MESSAGEBOX_URL__">Try the setup URL now</a>
      <p class="status">If the setup hotspot returns, reopen it and check the Wi-Fi details.</p>
    </section>
  </main>
</body>
</html>
"""

_CHANGE_HTML = b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Changing Wi-Fi | Message Box</title>
  <style nonce="messagebox-handoff">body{margin:0;background:#05070a;color:#f5f1e8;font:18px/1.5 system-ui,sans-serif}.shell{min-height:100vh;display:grid;place-items:center;padding:24px;box-sizing:border-box}.card{max-width:34rem;background:#171a1d;border:1px solid #363b3d;border-radius:20px;padding:28px}.eyebrow{color:#69c5a5;text-transform:uppercase;letter-spacing:.12em;font-size:.75rem;font-weight:700}h1{line-height:1.1}.lede{color:#adb7b0}</style>
</head>
<body>
  <main class="shell">
    <section class="card" aria-labelledby="change-title">
      <p class="eyebrow">Wi-Fi setup</p>
      <h1 id="change-title">Returning to setup mode</h1>
      <p class="lede">The Message Box setup hotspot may take up to five minutes to return.</p>
      <ol class="steps">
        <li>Open Wi-Fi settings on this phone.</li>
        <li>Join the printed Message Box hotspot when it appears.</li>
        <li>Open the printed Message Box address to continue.</li>
      </ol>
      <a class="button secondary" href="/">Return if the hotspot does not come back</a>
    </section>
  </main>
</body>
</html>
"""


class RequestError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Response:
    def __init__(self, body=b"", status="200 OK", headers=()):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.body = body
        self.status = status
        self.headers = list(headers)

    def __call__(self, start_response):
        headers = list(_SECURITY_HEADERS) + self.headers
        if not any(name.lower() == "content-length" for name, _ in headers):
            headers.append(("Content-Length", str(len(self.body))))
        start_response(self.status, headers)
        return [self.body]


class HandoffIterable:
    """Run the radio-switch callback only when the WSGI server closes response."""

    def __init__(self, body, callback):
        self._body = body
        self._callback = callback
        self._closed = False

    def __iter__(self):
        return iter((self._body,))

    def close(self):
        if self._closed:
            return
        self._closed = True
        callback, self._callback = self._callback, None
        if callback is not None:
            callback()


def _json_response(document, status="200 OK", headers=()):
    body = json.dumps(
        document, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return Response(
        body,
        status,
        [("Content-Type", "application/json; charset=utf-8"), *headers],
    )


def _html_response(body, status="200 OK", headers=()):
    return Response(
        body,
        status,
        [("Content-Type", "text/html; charset=utf-8"), *headers],
    )


def _read_body(environ, limit):
    raw_length = environ.get("CONTENT_LENGTH", "")
    try:
        length = int(raw_length) if raw_length else 0
    except (TypeError, ValueError) as exc:
        raise RequestError("400 Bad Request", "Invalid request body") from exc
    if length < 0:
        raise RequestError("400 Bad Request", "Invalid request body")
    if length > limit:
        raise RequestError("413 Content Too Large", "Request body is too large")
    stream = environ.get("wsgi.input")
    if stream is None:
        data = b""
    else:
        data = stream.read(length if raw_length else limit + 1)
    if len(data) > limit:
        raise RequestError("413 Content Too Large", "Request body is too large")
    if raw_length and len(data) != length:
        raise RequestError("400 Bad Request", "Incomplete request body")
    return data


def _media_type(environ):
    return environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()


def _pairs_to_dict(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise RequestError("400 Bad Request", "Duplicate form field")
        values[key] = value
    return values


def _form(environ, limit):
    if _media_type(environ) != "application/x-www-form-urlencoded":
        raise RequestError("415 Unsupported Media Type", "Expected a form submission")
    raw = _read_body(environ, limit)
    if _PERCENT_ESCAPE.search(raw):
        raise RequestError("400 Bad Request", "Invalid form encoding")
    if not raw:
        return {}
    try:
        text = raw.decode("ascii")
        pairs = parse_qsl(
            text, keep_blank_values=True, strict_parsing=True, encoding="utf-8", errors="strict"
        )
    except (UnicodeError, ValueError) as exc:
        raise RequestError("400 Bad Request", "Invalid form encoding") from exc
    return _pairs_to_dict(pairs)


def _load_config(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("could not load onboarding configuration") from exc


def _valid_ssid(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return False
    return 1 <= len(encoded) <= 32 and not any(
        unicodedata.category(character) == "Cc" for character in value
    )


def _credentials(document):
    if set(document) != {"ssid", "password", "security"}:
        raise RequestError("400 Bad Request", "Invalid Wi-Fi form")
    ssid = document["ssid"]
    password = document["password"]
    security = document["security"].lower() if isinstance(document["security"], str) else ""
    if not _valid_ssid(ssid):
        raise RequestError("400 Bad Request", "SSID must contain 1 to 32 UTF-8 bytes and no controls")
    if not isinstance(password, str):
        raise RequestError("400 Bad Request", "Invalid Wi-Fi password")
    try:
        password_length = len(password.encode("utf-8"))
    except UnicodeError as exc:
        raise RequestError("400 Bad Request", "Invalid Wi-Fi password") from exc
    if security in _OPEN_SECURITY:
        if password:
            raise RequestError("400 Bad Request", "Open networks require an empty password")
    elif security in _PROTECTED_SECURITY:
        if _HEX_PSK.fullmatch(password):
            raise RequestError("400 Bad Request", "Raw hexadecimal Wi-Fi keys are not accepted")
        if not 8 <= password_length <= 63:
            raise RequestError("400 Bad Request", "Wi-Fi password must contain 8 to 63 UTF-8 bytes")
    else:
        raise RequestError("400 Bad Request", "Select whether the network is open or protected")
    return ssid, password


def _canonical_request_host(environ, canonical_host):
    supplied = environ.get("HTTP_HOST", "")
    if not isinstance(supplied, str) or not supplied or any(
        character in supplied for character in "\r\n,/@"
    ):
        return False
    host, separator, port = supplied.rpartition(":")
    if not separator:
        host, port = supplied, ""
    if separator and (not port.isdigit() or int(port) != 80):
        return False
    return host.rstrip(".").casefold() == canonical_host.casefold()


def _require_same_origin(environ, canonical_host):
    """Reject browser-driven cross-site mutations without using ambient cookies."""
    if environ.get("HTTP_SEC_FETCH_SITE", "").lower() == "cross-site":
        raise RequestError("403 Forbidden", "Cross-site request rejected")
    origin = environ.get("HTTP_ORIGIN")
    if origin is not None and origin.rstrip("/").casefold() != (
        f"http://{canonical_host}".casefold()
    ):
        raise RequestError("403 Forbidden", "Cross-site request rejected")


def create_app(
    mode=None,
    *,
    config=None,
    config_path=CONFIG_PATH,
    state_store=None,
    state_path=STATE_PATH,
    adapter=None,
    connectivity_checker=None,
    whatsapp_client=None,
    clock=time.time,
    sleep=time.sleep,
    handoff_delay=HANDOFF_DELAY,
    body_limit=BODY_LIMIT,
):
    """Build an isolated WSGI application with injectable hardware boundaries."""
    selected_mode = (mode or os.environ.get("MSGBOX_ONBOARDING_MODE", "HOTSPOT")).upper()
    if selected_mode not in {"HOTSPOT", "HOME"}:
        raise ValueError("onboarding mode must be HOTSPOT or HOME")
    if config is None:
        config = _load_config(config_path)
    if not isinstance(config, dict):
        raise RuntimeError("onboarding configuration is invalid")
    device_id = config.get("device_id")
    if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
        raise RuntimeError("onboarding device ID is invalid")
    canonical_host = config.get("canonical_host", f"button-box-{device_id}.local")
    if not isinstance(canonical_host, str) or not _CANONICAL_HOST.fullmatch(canonical_host):
        raise RuntimeError("onboarding canonical hostname is invalid")
    canonical_url = f"http://{canonical_host}/"
    portal_url = HOTSPOT_URL if selected_mode == "HOTSPOT" else canonical_url
    store = state_store or StateStore(state_path, clock=clock)
    store.initialize()
    comitup = adapter or ComitupAdapter()
    checker = connectivity_checker or ConnectivityChecker()
    whatsapp = whatsapp_client or WhatsAppPairingClient()

    def reconcile_home():
        try:
            state = store.load()
            if state["phase"] in {WHATSAPP_PENDING, WHATSAPP_READY}:
                return state
            result = checker.check() if hasattr(checker, "check") else checker()
            if not isinstance(result, dict):
                return state
            association_proofs = {
                "wlan0_nm_active",
                "wlan0_non_ap",
                "wlan0_ipv4",
                "wlan0_default_route",
            }
            observed_proofs = set(result.get("proof", ())) & PROOFS
            if association_proofs <= observed_proofs and state["phase"] in {
                WIFI_CONNECTING,
                WIFI_FAILED,
            }:
                state = store.mark_associated(state["generation"])
            if state["phase"] != WIFI_ASSOCIATED:
                return state
            if result.get("ok") is True and observed_proofs == PROOFS:
                state = store.record_connectivity_result(observed_proofs)
            elif result.get("ok") is False and result.get("error") in SAFE_ERRORS:
                state = store.record_connectivity_result(
                    observed_proofs, result["error"]
                )
            return state
        except (StateError, KeyError, TypeError, ValueError, OSError):
            return store.load()

    if selected_mode == "HOTSPOT":
        store.reconcile_hotspot()
    else:
        reconcile_home()

    static_files = {}
    for name, content_type in (
        ("index.html", "text/html; charset=utf-8"),
        ("app.js", "text/javascript; charset=utf-8"),
        ("styles.css", "text/css; charset=utf-8"),
    ):
        static_files[name] = (STATIC_DIR.joinpath(name).read_bytes(), content_type)

    def safe_whatsapp_state(state):
        if selected_mode != "HOME" or state["phase"] not in {
            WHATSAPP_PENDING,
            WHATSAPP_READY,
        }:
            return {
                "status": "idle",
                "pairing_code": None,
                "phone_hint": None,
                "eligible_count": 0,
                "safe_error": None,
            }
        try:
            worker = whatsapp.status()
        except (OSError, PairingError):
            return {
                "status": "failed",
                "pairing_code": None,
                "phone_hint": None,
                "eligible_count": 0,
                "safe_error": "PAIRING_UNAVAILABLE",
            }
        allowed_statuses = {
            "idle",
            "starting",
            "code_pending",
            "bootstrapping",
            "verifying",
            "expired",
            "failed",
            "ready",
        }
        status = worker.get("status")
        code = worker.get("pairing_code")
        hint = worker.get("phone_hint")
        count = worker.get("eligible_count")
        error = worker.get("safe_error")
        if (
            status not in allowed_statuses
            or (code is not None and not isinstance(code, str))
            or (
                hint is not None
                and (
                    not isinstance(hint, str)
                    or (hint != "Linked account" and not _PHONE_HINT.fullmatch(hint))
                )
            )
            or type(count) is not int
            or not 0 <= count <= 10
            or (error is not None and error not in WHATSAPP_SAFE_ERRORS)
        ):
            raise PairingError("pairing_response_invalid")
        if code is not None:
            try:
                code = normalize_pair_code(code)
            except PairingError as exc:
                raise PairingError("pairing_response_invalid") from exc
        if status == "ready" and state["phase"] == WHATSAPP_PENDING:
            state = store.mark_whatsapp_ready(WHATSAPP_PROOFS)
        return {
            "status": status,
            "pairing_code": code if status == "code_pending" else None,
            "phone_hint": hint if status == "ready" else None,
            "eligible_count": count if status == "ready" else 0,
            "safe_error": error,
        }

    def safe_state(state):
        whatsapp_state = safe_whatsapp_state(state)
        if whatsapp_state["status"] == "ready" and state["phase"] == WHATSAPP_PENDING:
            state = store.load()
        return {
            "phase": state["phase"],
            "safe_error": state["safe_error"],
            "mode": selected_mode,
            "whatsapp": whatsapp_state,
        }

    def handoff(start_response, callback=None, body=None):
        if body is None:
            body = _HANDOFF_HTML.replace(
                b"__MESSAGEBOX_URL__", canonical_url.encode("ascii")
            )
        headers = [
            header for header in _SECURITY_HEADERS
            if header[0].lower() != "content-security-policy"
        ] + [
            (
                "Content-Security-Policy",
                "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self'; script-src 'nonce-messagebox-handoff'; "
                "style-src 'nonce-messagebox-handoff'; connect-src 'self'",
            ),
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ]
        start_response("202 Accepted", headers)
        return HandoffIterable(body, callback)

    def dispatch(generation, ssid, password):
        sleep(handoff_delay)
        try:
            if not store.mark_dispatched(generation):
                return
            comitup.connect_once(ssid, password)
        except (ComitupError, StateError, ValueError):
            try:
                if store.load()["phase"] == WIFI_CONNECTING:
                    store.fail("ASSOCIATION_FAILED")
            except StateError:
                pass

    def application(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "")
        try:
            if method == "GET" and path in {
                "/hotspot-detect.html",
                "/generate_204",
                "/gen_204",
                "/connecttest.txt",
                "/ncsi.txt",
            }:
                if selected_mode == "HOTSPOT":
                    return Response(
                        b"",
                        "302 Found",
                        [("Location", portal_url), ("Content-Type", "text/plain; charset=utf-8")],
                    )(start_response)
                if path in {"/generate_204", "/gen_204"}:
                    return Response(b"", "204 No Content")(start_response)
                if path == "/hotspot-detect.html":
                    return _html_response("<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>")(start_response)
                body = "Microsoft NCSI" if path == "/ncsi.txt" else "Microsoft Connect Test"
                return Response(body, headers=[("Content-Type", "text/plain; charset=utf-8")])(start_response)

            canonical = _canonical_request_host(environ, canonical_host) or (
                selected_mode == "HOTSPOT"
                and _canonical_request_host(environ, HOTSPOT_HOST)
            )
            if not canonical:
                if method == "GET":
                    location = portal_url.rstrip("/") + (path if path.startswith("/") else "/")
                    query = environ.get("QUERY_STRING", "")
                    if query:
                        location += "?" + query
                    return Response(
                        b"",
                        "302 Found",
                        [("Location", location), ("Content-Type", "text/plain; charset=utf-8")],
                    )(start_response)
                raise RequestError("400 Bad Request", "Use the printed Message Box address")

            if method == "GET" and path == "/":
                body, content_type = static_files["index.html"]
                body = body.replace(
                    b"__MESSAGEBOX_URL__", canonical_url.encode("ascii")
                )
                return Response(body, headers=[("Content-Type", content_type)])(start_response)
            if method == "GET" and path in {"/static/app.js", "/static/styles.css"}:
                name = path.rsplit("/", 1)[-1]
                body, content_type = static_files[name]
                return Response(body, headers=[("Content-Type", content_type)])(start_response)

            if method == "GET" and path == "/api/state":
                state = reconcile_home() if selected_mode == "HOME" else store.load()
                return _json_response(safe_state(state))(start_response)

            if method == "GET" and path == "/api/networks":
                if selected_mode != "HOTSPOT":
                    raise RequestError("409 Conflict", "Wi-Fi scanning is available in setup mode")
                try:
                    networks = comitup.scan_networks()
                except ComitupError:
                    return _json_response(
                        {"error": "Wi-Fi scan is unavailable"}, "503 Service Unavailable"
                    )(start_response)
                return _json_response({"networks": networks})(start_response)

            if method == "POST" and path == "/wifi/connect":
                if selected_mode != "HOTSPOT":
                    raise RequestError("409 Conflict", "Wi-Fi connection can start in setup mode")
                document = _form(environ, body_limit)
                ssid, password = _credentials(document)
                current = store.load()
                if current["phase"] == WIFI_CONNECTING:
                    return handoff(start_response)
                try:
                    state = store.begin_connect(ssid)
                except StateError as exc:
                    if store.load()["phase"] == WIFI_CONNECTING:
                        return handoff(start_response)
                    raise RequestError("409 Conflict", "Wi-Fi connection cannot start now") from exc
                generation = state["generation"]
                return handoff(
                    start_response,
                    lambda: dispatch(generation, ssid, password),
                )

            if method == "POST" and path == "/wifi/change":
                _require_same_origin(environ, canonical_host)
                if selected_mode != "HOME":
                    raise RequestError("409 Conflict", "The active network can be changed on home Wi-Fi")
                document = _form(environ, body_limit)
                if document:
                    raise RequestError("400 Bad Request", "Invalid network-change request")
                if store.load()["phase"] not in {
                    WIFI_ASSOCIATED,
                    WHATSAPP_PENDING,
                    WHATSAPP_READY,
                }:
                    raise RequestError("409 Conflict", "The active network cannot be changed")
                def delete_after_response():
                    sleep(handoff_delay)
                    try:
                        comitup.delete_active_connection_once()
                    except ComitupError:
                        pass

                return handoff(start_response, delete_after_response, _CHANGE_HTML)

            if method == "POST" and path == "/whatsapp/pair/start":
                _require_same_origin(environ, canonical_host)
                if selected_mode != "HOME":
                    raise RequestError("409 Conflict", "WhatsApp pairing requires home Wi-Fi")
                document = _form(environ, body_limit)
                if set(document) != {"phone"}:
                    raise RequestError("400 Bad Request", "Invalid WhatsApp pairing request")
                if store.load()["phase"] != WHATSAPP_PENDING:
                    raise RequestError("409 Conflict", "WhatsApp pairing cannot start now")
                try:
                    phone = normalize_phone(document["phone"])
                    whatsapp.start(phone)
                except PairingError as exc:
                    if str(exc) == "phone_number_invalid":
                        raise RequestError(
                            "400 Bad Request",
                            "Enter a valid international phone number beginning with +",
                        ) from exc
                    if str(exc) == "pairing_already_in_progress":
                        raise RequestError(
                            "409 Conflict", "Another WhatsApp pairing attempt is active"
                        ) from exc
                    raise RequestError(
                        "503 Service Unavailable", "WhatsApp pairing is unavailable"
                    ) from exc
                return _json_response(
                    safe_state(store.load()), "202 Accepted"
                )(start_response)

            if method == "POST" and path == "/whatsapp/pair/cancel":
                _require_same_origin(environ, canonical_host)
                if selected_mode != "HOME":
                    raise RequestError("409 Conflict", "WhatsApp pairing requires home Wi-Fi")
                document = _form(environ, body_limit)
                if document:
                    raise RequestError("400 Bad Request", "Invalid pairing cancellation")
                if store.load()["phase"] != WHATSAPP_PENDING:
                    raise RequestError("409 Conflict", "WhatsApp pairing is not active")
                try:
                    whatsapp.cancel()
                except (OSError, PairingError) as exc:
                    raise RequestError(
                        "503 Service Unavailable", "WhatsApp pairing is unavailable"
                    ) from exc
                return _json_response(safe_state(store.load()))(start_response)

            if method == "POST" and path == "/whatsapp/unlink":
                _require_same_origin(environ, canonical_host)
                if selected_mode != "HOME":
                    raise RequestError("409 Conflict", "WhatsApp unlink requires home Wi-Fi")
                document = _form(environ, body_limit)
                if set(document) != {"confirm"} or document["confirm"] != "unlink":
                    raise RequestError("400 Bad Request", "Confirm account unlinking")
                if store.load()["phase"] != WHATSAPP_READY:
                    raise RequestError("409 Conflict", "WhatsApp is not ready")
                try:
                    state = whatsapp.unlink()
                except (OSError, PairingError) as exc:
                    raise RequestError(
                        "503 Service Unavailable", "WhatsApp unlink is unavailable"
                    ) from exc
                if state.get("status") != "idle":
                    raise RequestError(
                        "409 Conflict", "WhatsApp logout failed; the linked account was preserved"
                    )
                store.mark_whatsapp_unlinked()
                return _json_response(safe_state(store.load()))(start_response)

            if path in {
                "/api/state",
                "/api/networks",
                "/wifi/connect",
                "/wifi/change",
                "/whatsapp/pair/start",
                "/whatsapp/pair/cancel",
                "/whatsapp/unlink",
            }:
                return _json_response(
                    {"error": "Method not allowed"},
                    "405 Method Not Allowed",
                    [("Allow", _allowed_methods(path))],
                )(start_response)
            return _json_response({"error": "Not found"}, "404 Not Found")(start_response)
        except RequestError as exc:
            return _json_response({"error": exc.message}, exc.status)(start_response)
        except (PairingError, StateError, OSError):
            return _json_response(
                {"error": "Onboarding state is unavailable"}, "503 Service Unavailable"
            )(start_response)

    return application


def _allowed_methods(path):
    return {
        "/api/state": "GET",
        "/api/networks": "GET",
        "/wifi/connect": "POST",
        "/wifi/change": "POST",
        "/whatsapp/pair/start": "POST",
        "/whatsapp/pair/cancel": "POST",
        "/whatsapp/unlink": "POST",
    }[path]


class _LazyApplication:
    """Avoid reading installed credentials merely by importing this module."""

    def __init__(self):
        self._application = None
        self._lock = threading.Lock()

    def __call__(self, environ, start_response):
        if self._application is None:
            with self._lock:
                if self._application is None:
                    self._application = create_app()
        return self._application(environ, start_response)


app = _LazyApplication()
