"""Model PID 1 and HTTPS only; run the copied client and real minisign verifier."""
import base64
import contextlib
import io
import json
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

from field_updater import write
from messagebox import updater_admission as admission


class AdmissionHost:
    def __init__(self, box, public, secret, *, decision='allow'):
        self.box, self.public, self.secret, self.decision = box, public, secret, decision
        self.real_run = subprocess.run
        self.calls, self.requests, self.responses = [], [], []
        self.revision = 5
        self.replay = None
        self.registered_source = admission.set_hash(box.root / 'usr/lib/messagebox-update')
        self.subject = {'box_id': 'box_synthetic_12345678', 'device_id': 'device_synthetic_12345678', 'account_scope': 'a' * 64}
        write(box.root / 'var/lib/messagebox-cloud/device.json', admission.canonical({
            'device_id': self.subject['device_id'], 'credential': 'c' * 48}), 0o600)
        write(box.root / 'var/lib/messagebox-cloud/runtime.json', admission.canonical({'snapshot': self.subject}), 0o600)

    @property
    def key(self):
        return self.public.read_text().splitlines()[1]

    def pin(self, source):
        write(source / 'messagebox/updater_rollout_keys.py', (repr({'schema': 1, 'keys': [self.key]}) + '\n').encode())

    def signed(self, payload, *, raw=None, prehash=False, secret=None):
        raw = admission.canonical(payload) if raw is None else raw
        with tempfile.TemporaryDirectory() as directory:
            document = write(Path(directory) / 'payload', raw)
            signature = Path(directory) / 'signature'
            self.real_run(['minisign', '-S', *([] if prehash else ['-l']), '-m', str(document),
                '-s', str(secret or self.secret), '-x', str(signature), '-t', admission.DOMAIN], check=True, capture_output=True)
            key_id = base64.b64decode(self.key)[2:10][::-1].hex().upper()
            return admission.canonical({'schema': 1, 'key_id': key_id,
                'payload_b64': base64.b64encode(raw).decode(), 'signature_minisign': signature.read_text()})

    def open(self, request, **kwargs):
        self.assert_heartbeat(request)
        body = json.loads(request.data)
        self.requests.append(body)
        if self.decision == 'timeout':
            raise TimeoutError('synthetic network timeout')
        if self.decision == 'deny':
            return io.BytesIO(b'{}')
        if self.replay:
            return io.BytesIO(admission.canonical({'updater_permit': json.loads(self.replay)}))
        fields = {n:v for n,v in body['updater_permit_request'].items() if n != 'protocol'}
        now = int(time.time())
        payload = {**fields, **self.subject, 'schema': 1, 'domain': admission.DOMAIN, 'action': 'permit',
            'key_id': base64.b64decode(self.key)[2:10][::-1].hex().upper(),
            'source_controller_set_sha256': self.registered_source,
            'policy_revision': self.revision, 'issued_at': now-1, 'expires_at': now+299}
        changes = {'expired': {'expires_at': now}, 'wrong-box': {'box_id': 'different_box_12345678'},
            'wrong-account': {'account_scope': 'd' * 64}, 'wrong-nonce': {'nonce': '0' * 32},
            'wrong-purpose': {'purpose': 'promote_controller'}, 'wrong-target': {'target_controller_set_sha256': 'e' * 64}}
        payload.update(changes.get(self.decision, {}))
        envelope = self.signed(payload)
        self.responses.append(envelope)
        return io.BytesIO(admission.canonical({'updater_permit': json.loads(envelope)}))

    def assert_heartbeat(self, request):
        if request.full_url != 'https://button.box/cloud-api/v1/device/heartbeat':
            raise AssertionError('wrong endpoint')
        body = json.loads(request.data)
        installed = json.loads(self.box.installed('/opt/messagebox/release.json').read_bytes())
        if (body['software_version'] != installed['version'] or body['software_commit'] != installed['commit']
                or 'capabilities' in body or 'settings' in body
                or request.headers.get('Authorization') != 'Bearer ' + 'c' * 48):
            raise AssertionError('admission changed installed application identity/capabilities')

    def run(self, command, **options):
        self.calls.append(command)
        if command[0] == 'timedatectl':
            return subprocess.CompletedProcess(command, 0, 'yes\n')
        if command[:3] == ['systemctl','show','messagebox-update-discovery.service']:
            override = self.box.root / 'etc/systemd/system/messagebox-update-discovery.service.d/messagebox.conf'
            return subprocess.CompletedProcess(command, 0, '30min\n' if override.exists() else '15min\n')
        if command == ['systemctl','stop',admission.UNIT]:
            return subprocess.CompletedProcess(command, 0, '')
        if command[0] == 'systemd-run':
            if (command[-4:] != ['/usr/bin/python3','-I','-B','/' + admission.RUN + '/client.py']
                    or '--unit=' + admission.UNIT not in command or 'User=messagebox' not in command
                    or 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6' not in command):
                raise AssertionError('unsafe client argv')
            root = self.box.root
            client = admission.load(root / admission.RUN / 'client.py')
            request = json.loads((root / admission.RUN / 'request.json').read_bytes())
            raw = client.exchange(request, root=root, opener=self.open, environ={'MSGBOX_TRANSPORT': 'cloud'})
            write(root / admission.RUN / 'out/response.json', raw, 0o600)
            return subprocess.CompletedProcess(command, 0, '')
        if command[0] == 'minisign':
            return self.real_run(command, **options)
        raise AssertionError('unexpected admission boundary: ' + repr(command))

    @contextlib.contextmanager
    def installed(self):
        # New mapper modules bind subprocess.run when loaded. Keep all real
        # worker IO, real cryptography and the copied stdlib client intact.
        with mock.patch.object(subprocess, 'run', side_effect=self.run):
            yield self
