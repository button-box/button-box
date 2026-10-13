"""Cloud-only IPC and GPIO-owner update approval. Never creates a capture permit."""

import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from messagebox.software import software_fields


def transport_mode():
    return os.environ.get("MSGBOX_TRANSPORT", "wacli")

SOCKET = '/run/messagebox-update/control.sock'
STATUS = Path('/var/lib/button-box-update/status.json')
CUE = 'Software update ready. Hold the button for three seconds to install.'
AUTO_CUE = 'Automatic software updates ready. Hold the button for three seconds to enable.'
TERMINAL = {'installed', 'failed', 'expired', 'rolled_back', 'rollback_failed'}


# Bounded codes the fixed updater may return. Unknown values never reach the Cloud.
REJECTION_CODES = frozenset({
    'update_busy', 'invalid_operation', 'invalid_version', 'invalid_mode', 'operation_conflict',
    'request_expired', 'invalid_policy', 'unauthorized_peer', 'not_button_owner', 'not_heartbeat_owner',
    'not_downloader', 'approval_candidate_reserved', 'candidate_changed', 'approval_expired',
    'short_or_expired_hold', 'unsupported_command', 'invalid_request', 'invalid_trust_request',
    'trust_update_busy', 'request_rejected',
})


class UpdateUnavailable(ValueError):
    """str(exc) is always a bounded code: updater_unavailable, updater_rejected or a REJECTION_CODES value."""


def request(command, **fields):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        try:
            connection.connect(SOCKET)
            connection.sendall(json.dumps({'command': command, **fields}).encode() + b'\n')
            data = bytearray()
            while b'\n' not in data and len(data) <= 16384:
                block = connection.recv(4096)
                if not block:
                    break
                data.extend(block)
            value = json.loads(data)
        except (OSError, ValueError) as exc:
            raise UpdateUnavailable('updater_unavailable') from exc
    if not isinstance(value, dict) or value.get('error'):
        code = value.get('error') if isinstance(value, dict) else None
        raise UpdateUnavailable(code if code in REJECTION_CODES else 'updater_rejected')
    return value


def status():
    if transport_mode() != 'cloud':
        return {}
    try:
        value = request('status')
    except UpdateUnavailable:
        return {}
    return value


def capability():
    value = status()
    return ({'software_update': {'protocol': 1, 'modes': ['auto', 'ask'], 'physical_confirmation': True}}
            if value.get('installed') is True else {})


def heartbeat_fields():
    value = status()
    if not value.get('installed'):
        return {}
    return {'software_update': {key: value.get(key) for key in
            ('effective_mode', 'channel', 'available_version', 'last_result', 'trust_sequence')}}


def heartbeat_succeeded():
    try:
        request('heartbeat', **software_fields())
    except UpdateUnavailable:
        pass


def install_request(operation_id, version, remaining):
    return request('install', operation_id=operation_id, version=version, remaining=remaining)


def mode_request(operation_id, mode, remaining):
    return request('mode', operation_id=operation_id, mode=mode, remaining=remaining)


def operation_result(operation_id):
    return request('result', operation_id=operation_id)


def _voice(text, play):
    # Fixed English text only; no network strings or microphone input.
    with tempfile.TemporaryDirectory(prefix='messagebox-update-cue-') as directory:
        path = Path(directory) / 'cue.wav'
        subprocess.run(['espeak-ng', '-v', 'en', '-s', '145', '-w', str(path), text],
                       check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        play(path, timeout=30)


# Upper bound on waiting for the hold cue before `confirm`. Playback holds a
# shared maintenance reservation; the install needs the exclusive one, so a
# confirm sent while the cue still plays would fail `not_idle` and burn the
# one-use approval.
HOLD_CUE_WAIT_S = 1.5


class Confirmation:
    def __init__(self, *, ipc=request, monotonic=time.monotonic, sleep=time.sleep, voice=_voice, on_hold=None):
        self.ipc, self.monotonic, self.sleep, self.voice = ipc, monotonic, sleep, voice
        # Optional: called once when a valid hold reaches 3 s. Returns a
        # wait(timeout) callable (or None). Default: no sound.
        self.on_hold = on_hold
        self.binding = None
        self.consume_release = False

    def _hold_reached(self):
        if self.on_hold is None:
            return None
        try:
            return self.on_hold()
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def idle(self, button, play):
        """Only the actual button-owner PID can arm a root-owned window."""
        if transport_mode() != 'cloud':
            return
        try:
            response = self.ipc('button', released=not button.is_pressed, **software_fields())
            approval = response.get('approval')
            if not approval:
                self.binding = None
                return
            if approval.get('cue_pending'):
                self.voice(AUTO_CUE if approval['action'] == 'enable_auto' else CUE, play)
                # Cue completion alone cannot approve: observe release first.
                while button.is_pressed:
                    self.consume_release = True
                    self.sleep(0.02)
                response = self.ipc('arm', binding=approval['binding'], released=True)
                approval = response.get('approval')
            self.binding = approval
        except (UpdateUnavailable, OSError, subprocess.SubprocessError):
            self.binding = None

    def intercept(self, button):
        if transport_mode() != 'cloud':
            return False
        # Query on the press as well. It closes the cue/normal-dispatch race and
        # consumes expired/rebooted windows rather than opening the microphone.
        try:
            response = self.ipc('button', released=False, **software_fields())
        except UpdateUnavailable:
            response = {}
        approval = response.get('approval') or self.binding
        if not approval and not self.consume_release:
            return False
        self.consume_release = False
        self.binding = None
        started = self.monotonic()
        eligible = bool(approval and not approval.get('cue_pending')
                        and started < approval.get('deadline_mono', 0))
        # Debounce is part of the continuous hold; an open edge resets it.
        confirmed = False
        cue_wait = None
        while button.is_pressed:
            now = self.monotonic()
            if (eligible and not confirmed and now - started >= 3
                    and now < approval['deadline_mono']):
                confirmed = True
                # Audible "yes" while the button is still held.
                cue_wait = self._hold_reached()
            if approval and now >= approval.get('deadline_mono', now + 1):
                eligible = False
            self.sleep(0.02)
        held_seconds = self.monotonic() - started
        if cue_wait is not None:
            # Release the shared audio reservation before the exclusive install.
            try:
                cue_wait(HOLD_CUE_WAIT_S)
            except (OSError, subprocess.SubprocessError):
                pass
        # The gesture is consumed completely, including short or expired holds.
        if confirmed and eligible:
            try:
                self.ipc('confirm', binding=approval['binding'], held_seconds=held_seconds,
                         released=True)
            except UpdateUnavailable:
                pass
        elif approval and not approval.get('cue_pending'):
            # Keep the same original window after a short hold; never rearm it.
            self.binding = approval
        return True


confirmation = Confirmation()


INSTALL_POLL_S = 10
INSTALL_WINDOW_S = 15 * 60


def owner_install_verdict(result, fields):
    """'announce', 'stop' or None (keep polling) for one `last_result`."""
    if not isinstance(result, dict) or result.get('state') not in TERMINAL:
        return None
    operation_id = result.get('operation_id')
    if (result.get('state') == 'installed' and result.get('reason') == 'healthy'
            and isinstance(operation_id, str) and operation_id
            and not operation_id.startswith(('auto:', 'bootstrap:'))
            and fields.get('software_version') and fields.get('software_commit')
            and result.get('software_version') == fields['software_version']
            and result.get('software_commit') == fields['software_commit']):
        return 'announce'
    return 'stop'


class InstallAnnouncer:
    """After startup, watch for a healthy owner-approved install of this release.

    Polling only; playback happens in the button idle loop via `take()`.
    """

    def __init__(self, *, get_status=None, fields=software_fields, monotonic=time.monotonic, sleep=time.sleep):
        self.get_status = get_status or (lambda: status())
        self.fields, self.monotonic, self.sleep = fields, monotonic, sleep
        self.pending = None
        self.deadline = None

    def run(self):
        self.deadline = self.monotonic() + INSTALL_WINDOW_S
        while self.monotonic() < self.deadline:
            try:
                result = (self.get_status() or {}).get('last_result')
                verdict = owner_install_verdict(result, self.fields())
            except (OSError, ValueError, UpdateUnavailable):
                verdict = None
            if verdict == 'announce':
                self.pending = result['operation_id']
                return
            if verdict == 'stop':
                return
            self.sleep(INSTALL_POLL_S)

    def take(self):
        """Return the operation id to announce once, inside the window only."""
        operation_id, self.pending = self.pending, None
        if operation_id and self.deadline is not None and self.monotonic() < self.deadline:
            return operation_id
        return None


install_announcer = InstallAnnouncer()


def button_ready(button):
    if transport_mode() == 'cloud':
        try:
            request('button', released=not button.is_pressed, **software_fields())
        except UpdateUnavailable:
            pass
