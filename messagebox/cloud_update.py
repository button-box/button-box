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


class UpdateUnavailable(ValueError):
    pass


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
        raise UpdateUnavailable('updater_rejected')
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
            ('effective_mode', 'channel', 'available_version', 'last_result')}}


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


class Confirmation:
    def __init__(self, *, ipc=request, monotonic=time.monotonic, sleep=time.sleep, voice=_voice):
        self.ipc, self.monotonic, self.sleep, self.voice = ipc, monotonic, sleep, voice
        self.binding = None
        self.consume_release = False

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
        while button.is_pressed:
            now = self.monotonic()
            if (eligible and not confirmed and now - started >= 3
                    and now < approval['deadline_mono']):
                confirmed = True
            if approval and now >= approval.get('deadline_mono', now + 1):
                eligible = False
            self.sleep(0.02)
        # The gesture is consumed completely, including short or expired holds.
        if confirmed and eligible:
            try:
                self.ipc('confirm', binding=approval['binding'], held_seconds=self.monotonic() - started,
                         released=True)
            except UpdateUnavailable:
                pass
        elif approval and not approval.get('cue_pending'):
            # Keep the same original window after a short hold; never rearm it.
            self.binding = approval
        return True


confirmation = Confirmation()


def button_ready(button):
    if transport_mode() == 'cloud':
        try:
            request('button', released=not button.is_pressed, **software_fields())
        except UpdateUnavailable:
            pass
