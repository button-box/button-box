"""Cloud-only IPC and GPIO-owner update approval. Never creates a capture permit."""

import json
import os
import socket
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from messagebox.software import software_fields
from messagebox import sound_pack, updater_admission


def transport_mode():
    return os.environ.get("MSGBOX_TRANSPORT", "wacli")

SOCKET = '/run/messagebox-update/control.sock'
STATUS = Path('/var/lib/button-box-update/status.json')
CUE = 'Software update ready. Hold the button for three seconds to install.'
AUTO_CUE = 'Automatic software updates ready. Hold the button for three seconds to enable.'
# Played once if nobody holds the button after the first ask. Recording only.
REMINDER_CUES = {'install': 'update-reminder.wav', 'enable_auto': 'auto-updates.wav'}
# Played after a successful hold is released, before the box confirms. Recording only;
# the 3-second beep is the fallback.
CONFIRMED_CUES = {'install': 'update-installing.wav', 'enable_auto': 'auto-updates-on.wav'}
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
    fields = {'software_update': {key: value.get(key) for key in
            ('effective_mode', 'channel', 'available_version', 'last_result', 'trust_sequence')}}
    try:
        fields['updater'] = (updater_admission.validate_report(value['updater']) if 'updater' in value
                             else updater_admission.cached_report())
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        pass
    return fields


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


def bundled_cue(name):
    """Choose only a local, bounded PCM recording; callers own the fallback."""
    path = sound_pack.SOUND_DIR / 'cues' / name
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return None
        with wave.open(str(path), 'rb') as sound:
            frames = sound.getnframes()
            if (sound.getnchannels() != 1 or sound.getsampwidth() != 2
                    or sound.getframerate() != 48000 or sound.getcomptype() != 'NONE'
                    or frames <= 0 or frames * 2 > 2 * 1024 * 1024
                    or len(sound.readframes(frames)) != frames * 2):
                return None
    except (OSError, EOFError, wave.Error):
        return None
    return path


def installed_cue():
    return bundled_cue('update-done.wav') or sound_pack.cue_path('all_set')


def _voice(text, play):
    name = 'auto-updates.wav' if text == AUTO_CUE else 'update-ready.wav'
    path = bundled_cue(name)
    if path is not None:
        play(path, timeout=30)
        return
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
# Upper bound on reaping the short press tick that starts the hold. The tick
# (0.4 s) has always ended before the 3 s "yes", and an early release waits at
# most this long so its shared reservation never outlives the gesture.
PRESS_CUE_WAIT_S = 0.6
# Ask again once when the window is still open this long after the first ask.
REMIND_AFTER_S = 40
# The reminder must finish well before the two-minute window closes.
REMIND_MARGIN_S = 20
# Upper bound on the "Got it!" line after release. It plays before `confirm`
# for the same reason as the hold cue, and only when the window can absorb it.
CONFIRMED_CUE_WAIT_S = 6


class Confirmation:
    def __init__(self, *, ipc=request, monotonic=time.monotonic, sleep=time.sleep, voice=_voice, on_hold=None,
                 on_confirmed=None, cue=bundled_cue, on_press=None):
        self.ipc, self.monotonic, self.sleep, self.voice = ipc, monotonic, sleep, voice
        # Optional: called once when a valid confirmation hold starts, so the
        # person hears that the box is counting. Returns a wait(timeout)
        # callable (or None). Default: no sound.
        self.on_press = on_press
        # Optional: called once when a valid hold reaches 3 s. Returns a
        # wait(timeout) callable (or None). Default: no sound.
        self.on_hold = on_hold
        # Optional: called with a bundled recording after a valid hold is
        # released. Returns a wait(timeout) callable (or None). Default: no sound.
        self.on_confirmed = on_confirmed
        self.cue = cue
        self.binding = None
        self.consume_release = False
        # One reminder per armed window: (binding, monotonic time to remind).
        self.reminder = None

    def _hold_reached(self):
        return self._start_cue(self.on_hold)

    @staticmethod
    def _start_cue(start):
        if start is None:
            return None
        try:
            return start()
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    @staticmethod
    def _reap(wait, timeout):
        if wait is None:
            return
        try:
            wait(timeout)
        except (OSError, subprocess.SubprocessError):
            pass

    def _confirmed(self, approval):
        """Say "Got it!" on the box before the updater takes the speaker."""
        name = CONFIRMED_CUES.get(approval.get('action'))
        if self.on_confirmed is None or name is None:
            return
        if approval['deadline_mono'] - self.monotonic() <= CONFIRMED_CUE_WAIT_S + 1:
            return  # Never let the line push the confirmation past the window.
        path = self.cue(name)
        if path is None:
            return
        try:
            wait = self.on_confirmed(path)
            if wait is not None:
                wait(CONFIRMED_CUE_WAIT_S)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

    def _remind(self, approval, play):
        """Ask once more if nobody held the button after the first ask."""
        if not self.reminder or self.reminder[0] != approval.get('binding'):
            return
        now = self.monotonic()
        if now < self.reminder[1]:
            return
        self.reminder = None
        name = REMINDER_CUES.get(approval.get('action'))
        path = self.cue(name) if name else None
        if path is None or approval.get('deadline_mono', 0) - now <= REMIND_MARGIN_S:
            return
        play(path, timeout=30)

    def idle(self, button, play):
        """Only the actual button-owner PID can arm a root-owned window."""
        if transport_mode() != 'cloud':
            return
        try:
            response = self.ipc('button', released=not button.is_pressed, **software_fields())
            approval = response.get('approval')
            if not approval:
                self.binding = None
                self.reminder = None
                return
            if not approval.get('cue_pending'):
                self.binding = approval
                self._remind(approval, play)
                return
            self.voice(AUTO_CUE if approval['action'] == 'enable_auto' else CUE, play)
            # Cue completion alone cannot approve: observe release first.
            while button.is_pressed:
                self.consume_release = True
                self.sleep(0.02)
            response = self.ipc('arm', binding=approval['binding'], released=True)
            approval = response.get('approval')
            if approval and not approval.get('cue_pending'):
                # Short holds keep this window and its one reminder.
                self.reminder = (approval['binding'], self.monotonic() + REMIND_AFTER_S)
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
        # Short tick on press-down: the box is counting the three seconds.
        press_wait = self._start_cue(self.on_press) if eligible and button.is_pressed else None
        while button.is_pressed:
            now = self.monotonic()
            if (eligible and not confirmed and now - started >= 3
                    and now < approval['deadline_mono']):
                confirmed = True
                # The tick ended long ago; reap it before the "yes".
                self._reap(press_wait, PRESS_CUE_WAIT_S)
                press_wait = None
                # Audible "yes" while the button is still held.
                cue_wait = self._hold_reached()
            if approval and now >= approval.get('deadline_mono', now + 1):
                eligible = False
            self.sleep(0.02)
        held_seconds = self.monotonic() - started
        # An early release still reaps the tick; it never confirms anything.
        self._reap(press_wait, PRESS_CUE_WAIT_S)
        # Release the shared audio reservation before the exclusive install.
        self._reap(cue_wait, HOLD_CUE_WAIT_S)
        # The gesture is consumed completely, including short or expired holds.
        if confirmed and eligible:
            self._confirmed(approval)
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
