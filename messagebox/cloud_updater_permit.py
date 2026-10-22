"""One unprivileged heartbeat exchange. Copied to /run by verified root code.

Standalone stdlib only: the installed field application's API and identity stay
unchanged. No device registration, settings application, or command execution.
"""
import json
import os
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

RUN = Path('/run/messagebox-updater-admission')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def exchange(request, *, root=Path('/'), opener=None, environ=None):
    environ = os.environ if environ is None else environ
    if environ.get('MSGBOX_TRANSPORT', 'wacli') != 'cloud':
        raise ValueError('cloud_only')
    base = environ.get('MSGBOX_CLOUD_API_URL', 'https://button.box/cloud-api/v1')
    url = urlsplit(base)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('invalid_cloud_url')
    identity = json.loads((root / 'var/lib/messagebox-cloud/device.json').read_bytes())
    if (not isinstance(identity, dict) or set(identity) != {'device_id', 'credential'}
            or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', identity.get('device_id', ''))
            or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', identity.get('credential', ''))):
        raise ValueError('invalid_identity')
    target = os.readlink(root / 'opt/messagebox')
    if not target.startswith('/opt/messagebox-releases/') or '..' in Path(target).parts:
        raise ValueError('invalid_application_pointer')
    software = json.loads((root / target.lstrip('/') / 'release.json').read_bytes())
    body = {'version': 'cloud-mvp-1', 'updater_permit_request': request['updater_permit_request'],
            **({'updater': request['updater']} if 'updater' in request else {}), 'software_version': software['version'],
            'software_commit': software['commit']}
    raw = json.dumps(body, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    if len(raw) > 24576:
        raise ValueError('heartbeat_limit')
    call = urllib.request.Request(base.rstrip('/') + '/device/heartbeat', data=raw, method='POST',
        headers={'Authorization': 'Bearer ' + identity['credential'], 'Content-Type': 'application/json'})
    open_url = opener or urllib.request.build_opener(NoRedirect()).open
    with open_url(call, timeout=15) as response:
        raw = response.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError('heartbeat_response_limit')
    value = json.loads(raw).get('updater_permit')
    raw = json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    if len(raw) > 8192:
        raise ValueError('permit_limit')
    return raw


def main():
    raw = (RUN / 'request.json').read_bytes()
    if len(raw) > 8192:
        raise ValueError('request_limit')
    output = exchange(json.loads(raw))
    os.umask(0o077)
    with (RUN / 'out/response.json').open('xb') as stream:
        stream.write(output)


if __name__ == '__main__':
    main()
