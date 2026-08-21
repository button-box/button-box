"""Private WhatsApp phone-code pairing worker and onboarding client.

The web portal talks to this worker over a group-restricted Unix socket.  Only
the worker runs as the ``messagebox`` runtime user and can access the wacli
stores.  Responses deliberately contain only short-lived pairing material and
redacted, content-free proof.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import math
import os
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from messagebox.onboarding.paths import (
    MESSAGEBOX_HOME,
    WACLI_PATH,
    WHATSAPP_CANDIDATES_PATH,
    WHATSAPP_LIVE_STORE,
    WHATSAPP_PAIRING_ROOT,
    WHATSAPP_SOCKET_PATH,
)

SOCKET_PATH = str(WHATSAPP_SOCKET_PATH)
PAIRING_ROOT = str(WHATSAPP_PAIRING_ROOT)
LIVE_STORE = str(WHATSAPP_LIVE_STORE)
CANDIDATES_PATH = str(WHATSAPP_CANDIDATES_PATH)
WACLI_BIN = str(WACLI_PATH)
MAX_BOOTSTRAP_MESSAGES = 100
MAX_ELIGIBLE_CONVERSATIONS = 10
MAX_REQUEST_BYTES = 4096

ACTIVE_STATUSES = frozenset({"starting", "code_pending", "bootstrapping", "verifying"})
PUBLIC_STATUSES = frozenset(
    {
        "idle",
        "starting",
        "code_pending",
        "bootstrapping",
        "verifying",
        "expired",
        "failed",
        "ready",
    }
)
SAFE_ERRORS = frozenset(
    {
        "PAIRING_INTERRUPTED",
        "PAIRING_UNAVAILABLE",
        "AUTHENTICATION_FAILED",
        "WHATSAPP_UNREACHABLE",
        "CONVERSATIONS_UNAVAILABLE",
        "STORE_CONFLICT",
        "CLEANUP_FAILED",
        "UNLINK_FAILED",
    }
)

_FAILURE_CLASS = {
    "auth_status_failed": "AUTHENTICATION_FAILED",
    "doctor_failed": "WHATSAPP_UNREACHABLE",
    "conversation_list_failed": "CONVERSATIONS_UNAVAILABLE",
    "live_store_not_empty": "STORE_CONFLICT",
    "promotion_backup_exists": "STORE_CONFLICT",
}

_PHONE = re.compile(r"\+[1-9][0-9]{7,14}\Z")
_PAIR_CODE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 -]{2,30}[A-Za-z0-9])?\Z")
_PHONE_HINT = re.compile(r"WhatsApp number ending in [0-9]{4}\Z")
_JID = re.compile(r"(?:[0-9]+(?:-[0-9]+)?@g\.us|[0-9]+@s\.whatsapp\.net)\Z")


class PairingError(RuntimeError):
    """A safe failure at the worker boundary."""


def normalize_phone(value):
    """Return a strict E.164 number suitable for ``wacli --phone``."""
    if not isinstance(value, str):
        raise PairingError("phone_number_invalid")
    phone = value.strip().replace(" ", "").replace("-", "")
    if not _PHONE.fullmatch(phone):
        raise PairingError("phone_number_invalid")
    return phone


def normalize_pair_code(value):
    if not isinstance(value, str):
        raise PairingError("pairing_code_invalid")
    code = " ".join(value.strip().split())
    if not _PAIR_CODE.fullmatch(code):
        raise PairingError("pairing_code_invalid")
    return code.upper()


def pairing_command(phone):
    return [
        WACLI_BIN,
        "--events",
        "auth",
        "--idle-exit",
        "30s",
        "--phone",
        normalize_phone(phone),
    ]


def pairing_environment(store):
    return {
        "HOME": str(MESSAGEBOX_HOME),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "WACLI_STORE_DIR": str(store),
        "WACLI_SYNC_MAX_MESSAGES": str(MAX_BOOTSTRAP_MESSAGES),
        "WACLI_SYNC_MAX_DB_SIZE": "2GB",
    }


def parse_event(line):
    try:
        document = json.loads(line)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get("event"), str):
        return None
    data = document.get("data", {})
    if not isinstance(data, dict):
        data = {}
    return document["event"], data


def _json_document(output):
    try:
        document = json.loads(output)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PairingError("command_response_invalid") from exc
    return document


def _find_connected(value):
    if isinstance(value, dict):
        if value.get("connected") is True:
            return True
        return any(_find_connected(item) for item in value.values())
    if isinstance(value, list):
        return any(_find_connected(item) for item in value)
    return False


def _rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("data", "chats", "results"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


def eligible_conversations(value, limit=MAX_ELIGIBLE_CONVERSATIONS):
    """Return at most ``limit`` recent group/DM references from wacli JSON."""
    result = []
    seen = set()
    for row in _rows(value):
        if not isinstance(row, dict):
            continue
        jid = row.get("jid") or row.get("JID") or row.get("id") or row.get("chat_jid")
        if not isinstance(jid, str):
            continue
        jid = jid.strip().lower()
        if not _JID.fullmatch(jid) or jid in seen:
            continue
        label = (
            row.get("name")
            or row.get("Name")
            or row.get("subject")
            or row.get("label")
            or "WhatsApp conversation"
        )
        result.append(
            {"jid": jid, "label": str(label).strip()[:80] or "WhatsApp conversation"}
        )
        seen.add(jid)
        if len(result) >= limit:
            break
    return result


def _masked_phone(document):
    if isinstance(document, dict) and isinstance(document.get("data"), dict):
        document = document["data"]
    if not isinstance(document, dict) or document.get("authenticated") is not True:
        return None
    phone = document.get("phone")
    if not isinstance(phone, str) or not phone.isdigit():
        linked = document.get("linked_jid")
        phone = linked.split("@", 1)[0] if isinstance(linked, str) and "@" in linked else ""
    if not phone.isdigit():
        return "Linked account"
    return f"WhatsApp number ending in {phone[-4:]}"


class PairingEngine:
    """Serialize phone-code pairing and store promotion for one device."""

    VERSION = 1

    def __init__(
        self,
        *,
        pairing_root=PAIRING_ROOT,
        live_store=LIVE_STORE,
        candidates_path=CANDIDATES_PATH,
        clock=time.time,
        popen=subprocess.Popen,
        run=subprocess.run,
        recover=True,
    ):
        self.root = Path(pairing_root)
        self.stage = self.root / "staging"
        self.state_path = self.root / "state.json"
        self.backup = self.root / "live-empty-backup"
        self.live_store = Path(live_store)
        self.candidates_path = Path(candidates_path)
        if self.candidates_path.parent != self.live_store:
            raise PairingError("candidate_store_must_promote_with_wacli_store")
        self.clock = clock
        self.popen = popen
        self.run = run
        self._lock = threading.RLock()
        self._process = None
        self._cancel_requested = False
        self._active_phone_token = None
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        if not self.state_path.exists():
            self._write_state(self._default_state())
        if recover:
            self._recover_interrupted_attempt()

    def _default_state(self):
        return {
            "version": self.VERSION,
            "status": "idle",
            "pairing_code": None,
            "phone_hint": None,
            "eligible_count": 0,
            "safe_error": None,
            "attempt": 0,
            "updated_at": self.clock(),
        }

    def _load_state(self):
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise PairingError("pairing_state_unavailable") from exc
        expected = set(self._default_state())
        if not isinstance(document, dict) or set(document) != expected:
            raise PairingError("pairing_state_unavailable")
        if document["version"] != self.VERSION or document["status"] not in PUBLIC_STATUSES:
            raise PairingError("pairing_state_unavailable")
        if document["safe_error"] is not None and document["safe_error"] not in SAFE_ERRORS:
            raise PairingError("pairing_state_unavailable")
        code = document["pairing_code"]
        hint = document["phone_hint"]
        count = document["eligible_count"]
        attempt = document["attempt"]
        updated_at = document["updated_at"]
        try:
            valid_code = code is not None and normalize_pair_code(code) == code
        except PairingError:
            valid_code = False
        if document["status"] == "code_pending":
            if not valid_code:
                raise PairingError("pairing_state_unavailable")
        elif code is not None:
            raise PairingError("pairing_state_unavailable")
        if document["status"] == "ready":
            if (
                not isinstance(hint, str)
                or (hint != "Linked account" and not _PHONE_HINT.fullmatch(hint))
                or type(count) is not int
                or not 0 <= count <= MAX_ELIGIBLE_CONVERSATIONS
            ):
                raise PairingError("pairing_state_unavailable")
        elif hint is not None or count != 0:
            raise PairingError("pairing_state_unavailable")
        if type(attempt) is not int or attempt < 0:
            raise PairingError("pairing_state_unavailable")
        if type(updated_at) not in (int, float) or not math.isfinite(updated_at) or updated_at < 0:
            raise PairingError("pairing_state_unavailable")
        return document

    def _write_state(self, document):
        document = dict(document)
        document["updated_at"] = self.clock()
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.root, prefix=".state-", delete=False
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                json.dump(document, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            temporary = None
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _set_state(self, status, *, code=None, phone_hint=None, eligible_count=0, error=None):
        state = self._load_state()
        state.update(
            status=status,
            pairing_code=code,
            phone_hint=phone_hint,
            eligible_count=eligible_count,
            safe_error=error,
        )
        self._write_state(state)
        return self.public_state()

    def public_state(self):
        with self._lock:
            state = self._load_state()
            return {
                "status": state["status"],
                "pairing_code": (
                    state["pairing_code"] if state["status"] == "code_pending" else None
                ),
                "phone_hint": state["phone_hint"] if state["status"] == "ready" else None,
                "eligible_count": state["eligible_count"] if state["status"] == "ready" else 0,
                "safe_error": state["safe_error"],
                "attempt": state["attempt"],
            }

    def _recover_interrupted_attempt(self):
        with self._lock:
            state = self._load_state()
            self._recover_backup()
            if state["status"] not in ACTIVE_STATUSES:
                return
            if not self.stage.exists() and self.candidates_path.is_file():
                try:
                    phone_hint, eligible_count = self._prove_store(self.live_store)
                    self._set_state(
                        "ready",
                        phone_hint=phone_hint,
                        eligible_count=eligible_count,
                    )
                except (OSError, PairingError, subprocess.SubprocessError):
                    self._set_state("failed", error="STORE_CONFLICT")
                return
            if self._cleanup_stage(logout=True):
                self._set_state("expired", error="PAIRING_INTERRUPTED")
            else:
                self._set_state("failed", error="CLEANUP_FAILED")

    def start(self, phone):
        phone = normalize_phone(phone)
        phone_token = hashlib.sha256(phone.encode("ascii")).hexdigest()
        with self._lock:
            state = self._load_state()
            if state["status"] in ACTIVE_STATUSES:
                if self._active_phone_token == phone_token:
                    return self.public_state()
                raise PairingError("pairing_already_in_progress")
            if state["status"] == "ready":
                raise PairingError("unlink_current_account_first")
            if state["safe_error"] == "CLEANUP_FAILED" and self.stage.exists():
                raise PairingError("cleanup_required")
            self._remove_stage()
            self.stage.mkdir(mode=0o700)
            state.update(
                status="starting",
                pairing_code=None,
                phone_hint=None,
                eligible_count=0,
                safe_error=None,
                attempt=state["attempt"] + 1,
            )
            self._write_state(state)
            self._cancel_requested = False
            self._active_phone_token = phone_token
            worker = threading.Thread(target=self._pair, args=(phone,), daemon=True)
            worker.start()
            return self.public_state()

    def cancel(self):
        with self._lock:
            state = self._load_state()
            if state["status"] not in ACTIVE_STATUSES:
                return self.public_state()
            self._cancel_requested = True
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    process.terminate()
            return self.public_state()

    def unlink(self):
        with self._lock:
            state = self._load_state()
            if state["status"] != "ready":
                raise PairingError("whatsapp_not_ready")
            result = self._run_wacli(
                self.live_store,
                ["--json", "--timeout", "15s", "auth", "logout"],
                timeout=20,
            )
            if result.returncode != 0:
                return self._set_state(
                    "ready",
                    phone_hint=state["phone_hint"],
                    eligible_count=state["eligible_count"],
                    error="UNLINK_FAILED",
                )
            self._remove_directory(self.live_store)
            self.live_store.mkdir(parents=True, mode=0o700)
            self.candidates_path.unlink(missing_ok=True)
            return self._set_state("idle")

    def _pair(self, phone):
        process = None
        try:
            with self._lock:
                if self._cancel_requested:
                    if self._cleanup_stage(logout=True):
                        self._set_state("idle")
                    else:
                        self._set_state("failed", error="CLEANUP_FAILED")
                    return
            process = self.popen(
                pairing_command(phone),
                env=pairing_environment(self.stage),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
            with self._lock:
                self._process = process
            for line in process.stderr:
                event = parse_event(line)
                if event is None:
                    continue
                name, data = event
                if name == "pair_code":
                    try:
                        code = normalize_pair_code(data.get("code"))
                    except PairingError:
                        continue
                    with self._lock:
                        self._set_state("code_pending", code=code)
                elif name == "connected":
                    with self._lock:
                        self._set_state("bootstrapping")
            process.wait()
            with self._lock:
                cancelled = self._cancel_requested
            if cancelled:
                if self._cleanup_stage(logout=True):
                    with self._lock:
                        self._set_state("idle")
                else:
                    with self._lock:
                        self._set_state("failed", error="CLEANUP_FAILED")
                return
            self._verify_and_promote()
        except (OSError, PairingError, subprocess.SubprocessError) as exc:
            with self._lock:
                state = self._load_state()
                emitted_code = state["status"] in {"code_pending", "bootstrapping", "verifying"}
                cancelled = self._cancel_requested
            if cancelled:
                if self._cleanup_stage(logout=True):
                    with self._lock:
                        self._set_state("idle")
                else:
                    with self._lock:
                        self._set_state("failed", error="CLEANUP_FAILED")
                return
            safe_error = _FAILURE_CLASS.get(str(exc))
            if safe_error is None:
                safe_error = "PAIRING_INTERRUPTED" if emitted_code else "PAIRING_UNAVAILABLE"
            if self._cleanup_stage(logout=True):
                with self._lock:
                    self._set_state(
                        "expired" if safe_error == "PAIRING_INTERRUPTED" else "failed",
                        error=safe_error,
                    )
            else:
                with self._lock:
                    self._set_state("failed", error="CLEANUP_FAILED")
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
                self._active_phone_token = None

    def _verify_and_promote(self):
        with self._lock:
            self._set_state("verifying")
            self._raise_if_cancelled()
        phone_hint = self._prove_auth_and_connection(self.stage)
        with self._lock:
            self._raise_if_cancelled()
        chats = self._run_wacli(
            self.stage,
            [
                "--read-only",
                "--json",
                "--full",
                "chats",
                "list",
                "--limit",
                "50",
                "--no-archived",
            ],
            timeout=15,
        )
        if chats.returncode != 0:
            raise PairingError("conversation_list_failed")
        candidates = eligible_conversations(_json_document(chats.stdout))
        self._write_candidates(candidates, self.stage / self.candidates_path.name)
        with self._lock:
            self._raise_if_cancelled()
            self._promote_store()
            self._set_state(
                "ready",
                phone_hint=phone_hint,
                eligible_count=len(candidates),
            )

    def _raise_if_cancelled(self):
        if self._cancel_requested:
            raise PairingError("pairing_cancelled")

    def _prove_auth_and_connection(self, store):
        auth = self._run_wacli(
            store,
            ["--read-only", "--json", "auth", "status"],
            timeout=15,
        )
        if auth.returncode != 0:
            raise PairingError("auth_status_failed")
        auth_document = _json_document(auth.stdout)
        phone_hint = _masked_phone(auth_document)
        if phone_hint is None:
            raise PairingError("not_authenticated")
        doctor = self._run_wacli(
            store,
            ["--json", "--timeout", "15s", "doctor", "--connect"],
            timeout=20,
        )
        if doctor.returncode != 0 or not _find_connected(_json_document(doctor.stdout)):
            raise PairingError("doctor_failed")
        return phone_hint

    def _prove_store(self, store):
        phone_hint = self._prove_auth_and_connection(store)
        try:
            candidates = json.loads(self.candidates_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise PairingError("candidate_state_invalid") from exc
        rows = candidates.get("conversations") if isinstance(candidates, dict) else None
        if (
            not isinstance(candidates, dict)
            or set(candidates) != {"version", "conversations"}
            or candidates["version"] != 1
            or not isinstance(rows, list)
            or len(rows) > MAX_ELIGIBLE_CONVERSATIONS
            or eligible_conversations(rows) != rows
        ):
            raise PairingError("candidate_state_invalid")
        return phone_hint, len(rows)

    def _run_wacli(self, store, arguments, *, timeout):
        return self.run(
            [WACLI_BIN, *arguments],
            env=pairing_environment(store),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def _stage_authenticated(self):
        if not self.stage.exists():
            return False
        try:
            result = self._run_wacli(
                self.stage,
                ["--read-only", "--json", "auth", "status"],
                timeout=10,
            )
            document = _json_document(result.stdout)
            if isinstance(document, dict) and isinstance(document.get("data"), dict):
                document = document["data"]
            return (
                isinstance(document, dict) and document.get("authenticated") is True
            )
        except (OSError, PairingError, subprocess.SubprocessError):
            return False

    def _cleanup_stage(self, *, logout):
        if not self.stage.exists():
            return True
        if logout and self._stage_authenticated():
            try:
                result = self._run_wacli(
                    self.stage,
                    ["--json", "--timeout", "15s", "auth", "logout"],
                    timeout=20,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            if result.returncode != 0:
                return False
        self._remove_stage()
        return True

    def _promote_store(self):
        if self.backup.exists() or self.backup.is_symlink():
            raise PairingError("promotion_backup_exists")
        if self.stage.is_symlink() or not self.stage.is_dir():
            raise PairingError("staging_store_invalid")
        if self.live_store.is_symlink():
            raise PairingError("symlinked_store_rejected")
        if self.live_store.exists() and any(self.live_store.iterdir()):
            raise PairingError("live_store_not_empty")
        moved_live = False
        try:
            if self.live_store.exists():
                os.replace(self.live_store, self.backup)
                moved_live = True
            os.replace(self.stage, self.live_store)
        except OSError:
            if moved_live and self.backup.exists() and not self.live_store.exists():
                os.replace(self.backup, self.live_store)
            raise
        self._sync_directory(self.root)
        self._sync_directory(self.live_store.parent)
        if self.backup.exists():
            self.backup.rmdir()
            self._sync_directory(self.root)

    def _recover_backup(self):
        if self.backup.is_symlink():
            raise PairingError("symlinked_store_rejected")
        if not self.backup.exists():
            return
        if not self.backup.is_dir() or any(self.backup.iterdir()):
            raise PairingError("promotion_backup_invalid")
        if self.live_store.exists():
            self.backup.rmdir()
        else:
            os.replace(self.backup, self.live_store)
        self._sync_directory(self.root)
        self._sync_directory(self.live_store.parent)

    def _write_candidates(self, candidates, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=".whatsapp-candidates-",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                json.dump(
                    {"version": 1, "conversations": candidates},
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
            self._sync_directory(destination.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _remove_stage(self):
        self._remove_directory(self.stage)

    @staticmethod
    def _remove_directory(path):
        path = Path(path)
        if path.is_symlink():
            raise PairingError("symlinked_store_rejected")
        if path.exists():
            shutil.rmtree(path)

    @staticmethod
    def _sync_directory(path):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class WhatsAppPairingClient:
    """Small request/response client used by the unprivileged web portal."""

    def __init__(self, socket_path=SOCKET_PATH, *, timeout=5):
        self.socket_path = socket_path
        self.timeout = timeout

    def _request(self, document, *, timeout=None):
        encoded = json.dumps(document, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_REQUEST_BYTES:
            raise PairingError("pairing_request_too_large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout if timeout is None else timeout)
            connection.connect(self.socket_path)
            connection.sendall(encoded)
            response = bytearray()
            while b"\n" not in response:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > MAX_REQUEST_BYTES:
                    raise PairingError("pairing_response_too_large")
        try:
            result = json.loads(bytes(response).split(b"\n", 1)[0])
        except (ValueError, json.JSONDecodeError) as exc:
            raise PairingError("pairing_response_invalid") from exc
        if not isinstance(result, dict):
            raise PairingError("pairing_response_invalid")
        if result.get("ok") is not True:
            raise PairingError(str(result.get("error") or "pairing_worker_failed"))
        state = result.get("state")
        if not isinstance(state, dict):
            raise PairingError("pairing_response_invalid")
        return state

    def status(self):
        return self._request({"action": "status"})

    def start(self, phone):
        return self._request({"action": "start", "phone": normalize_phone(phone)})

    def cancel(self):
        return self._request({"action": "cancel"}, timeout=25)

    def unlink(self):
        return self._request({"action": "unlink"}, timeout=25)


class _PairingHandler(socketserver.StreamRequestHandler):
    def handle(self):
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            return self._respond({"ok": False, "error": "request_too_large"})
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError
            action = request.get("action")
            if action == "status" and set(request) == {"action"}:
                state = self.server.engine.public_state()
            elif action == "start" and set(request) == {"action", "phone"}:
                state = self.server.engine.start(request["phone"])
            elif action == "cancel" and set(request) == {"action"}:
                state = self.server.engine.cancel()
            elif action == "unlink" and set(request) == {"action"}:
                state = self.server.engine.unlink()
            else:
                raise PairingError("invalid_action")
            return self._respond({"ok": True, "state": state})
        except PairingError as exc:
            error = str(exc)
            if error not in {
                "phone_number_invalid",
                "pairing_already_in_progress",
                "unlink_current_account_first",
                "whatsapp_not_ready",
            }:
                error = "pairing_request_failed"
            return self._respond({"ok": False, "error": error})
        except (OSError, ValueError, json.JSONDecodeError):
            return self._respond({"ok": False, "error": "pairing_request_failed"})

    def _respond(self, document):
        self.wfile.write(
            json.dumps(document, separators=(",", ":")).encode("utf-8") + b"\n"
        )


class _PairingServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, engine):
        self.engine = engine
        super().__init__(path, _PairingHandler)


def serve(socket_path=SOCKET_PATH):
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.is_symlink():
        raise PairingError("symlinked_socket_rejected")
    socket_path.unlink(missing_ok=True)
    server = _PairingServer(str(socket_path), PairingEngine())
    os.chmod(socket_path, 0o660)
    group_name = os.environ.get("MSGBOX_PAIRING_SOCKET_GROUP", "messagebox-onboarding")
    os.chown(socket_path, -1, grp.getgrnam(group_name).gr_gid)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Message Box WhatsApp pairing worker")
    parser.add_argument("--serve", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.serve:
        parser.error("--serve is required")
    serve()


if __name__ == "__main__":
    main()
