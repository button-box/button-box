#!/usr/bin/env python3
"""Unprivileged, bounded GitHub Releases discovery and signed download."""

import json
import os
import random
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from release_verify import MAX_ARCHIVE, TAG, ReleaseError, local_platform, verify_bundle
from trust_update import MAX_TRUST, TRUST_TAG, verify_trust

API = 'https://api.github.com/repos/button-box/button-box/releases'
CACHE = Path('/var/cache/button-box-update')
KEYS = Path('/etc/button-box-update/release-keys')
MAX_PAGES = 10


class DownloadError(ValueError):
    pass


def allowed_url(url, *, api=False):
    value = urlsplit(url)
    if value.scheme != 'https' or value.username or value.password or value.port not in {None, 443}:
        return False
    if api:
        return value.hostname == 'api.github.com' and value.path.startswith('/repos/button-box/button-box/releases')
    return (value.hostname == 'github.com' and value.path.startswith('/button-box/button-box/releases/download/')
            or value.hostname in {'release-assets.githubusercontent.com', 'objects.githubusercontent.com'})


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise DownloadError('unsafe_redirect')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


OPEN = urllib.request.build_opener(SafeRedirect()).open


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix('.part')
    with temporary.open('w') as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def backoff(headers, now, failures):
    waits = [min(43200, 60 * 2 ** min(failures, 9))]
    try:
        waits.append(float(headers.get('Retry-After', 0)))
    except ValueError:
        from email.utils import parsedate_to_datetime
        try:
            waits.append(parsedate_to_datetime(headers['Retry-After']).timestamp() - now)
        except (ValueError, TypeError, KeyError):
            pass
    if headers.get('X-RateLimit-Remaining') == '0':
        try:
            waits.append(float(headers.get('X-RateLimit-Reset', now)) - now)
        except ValueError:
            pass
    return now + max(60, *waits) + random.uniform(5, 60)


DOWNLOAD_SECONDS = 1500


class Discovery:
    def __init__(self, directory=CACHE, *, opener=OPEN, clock=time.time, jitter=random.uniform, monotonic=time.monotonic):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        self.opener, self.clock, self.jitter = opener, clock, jitter
        self.monotonic = monotonic
        self.archive_urls = {}
        try:
            self.state = json.loads((self.directory / 'discovery.json').read_bytes())
        except FileNotFoundError:
            self.state = {'next_poll': 0, 'backoff_until': 0, 'failures': 0, 'responses': {}}

    def persist(self):
        save(self.directory / 'discovery.json', self.state)

    def json(self, url):
        if not allowed_url(url, api=True):
            raise DownloadError('unsafe_api')
        cache = self.state['responses'].get(url, {})
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'button-box-updater/1',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if cache.get('etag'):
            headers['If-None-Match'] = cache['etag']
        try:
            response = self.opener(urllib.request.Request(url, headers=headers), timeout=30)
            with response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise DownloadError('api_limit')
                value = json.loads(raw)
                self.state['responses'][url] = {'etag': response.headers.get('ETag'), 'body': value}
                if response.headers.get('X-RateLimit-Remaining') == '0':
                    self.state['backoff_until'] = backoff(response.headers, self.clock(), 0)
                self.persist()
                return value
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and 'body' in cache:
                if exc.headers.get('X-RateLimit-Remaining') == '0':
                    self.state['backoff_until'] = backoff(exc.headers, self.clock(), 0)
                self.persist()
                return cache['body']
            self.state['failures'] += 1
            self.state['backoff_until'] = backoff(exc.headers, self.clock(), self.state['failures'])
            self.persist()
            raise DownloadError('api_backoff') from exc

    def download(self, url, path, limit):
        if not allowed_url(url):
            raise DownloadError('unsafe_asset')
        partial = path.with_suffix(path.suffix + '.part')
        deadline = self.monotonic() + DOWNLOAD_SECONDS
        try:
            with self.opener(urllib.request.Request(url, headers={'User-Agent': 'button-box-updater/1'}), timeout=30) as incoming:
                with partial.open('wb') as output:
                    size = 0
                    while block := incoming.read(64 * 1024):
                        if self.monotonic() >= deadline:
                            raise DownloadError('download_timeout')
                        size += len(block)
                        if size > limit:
                            raise DownloadError('download_limit')
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
            os.replace(partial, path)
        finally:
            partial.unlink(missing_ok=True)

    def candidate(self, release, channel, floor, platform):
        if not isinstance(release, dict) or release.get('draft') is not False:
            raise DownloadError('invalid_release')
        version = release.get('tag_name')
        if (not isinstance(version, str) or not TAG.fullmatch(version)
                or type(release.get('prerelease')) is not bool
                or release['prerelease'] != ('-canary.' in version)
                or channel == 'stable' and release['prerelease']):
            raise DownloadError('wrong_channel')
        assets = release.get('assets')
        names = {f'button-box-{version}.tar.gz', 'release-manifest.json', 'SHA256SUMS', 'SHA256SUMS.minisig'}
        if not isinstance(assets, list):
            raise DownloadError('incomplete_release')
        selected = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') in names]
        if {asset['name'] for asset in selected} != names or len(selected) != 4:
            raise DownloadError('incomplete_release')
        directory = self.directory / version
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(mode=0o750)
        try:
            for asset in selected:
                if asset['name'].endswith('.tar.gz'):
                    if not allowed_url(asset.get('browser_download_url', '')) or type(asset.get('size')) is not int or not 0 < asset['size'] <= MAX_ARCHIVE:
                        raise DownloadError('download_limit')
                    self.archive_urls[version] = asset['browser_download_url']
                    continue
                limit = MAX_ARCHIVE if asset['name'].endswith('.tar.gz') else 2 * 1024 * 1024
                if type(asset.get('size')) is not int or not 0 < asset['size'] <= limit:
                    raise DownloadError('download_limit')
                self.download(asset.get('browser_download_url', ''), directory / asset['name'], limit)
            manifest = verify_bundle(directory, KEYS, version=version, channel=channel, floor=floor, platform=platform, verify_archive=False)
            return manifest
        except BaseException:
            shutil.rmtree(directory)
            raise

    def complete(self, manifest, channel, floor, platform):
        version = manifest["version"]
        directory = self.directory / version
        self.download(self.archive_urls[version], directory / f"button-box-{version}.tar.gz", MAX_ARCHIVE)
        verify_bundle(directory, KEYS, version=version, channel=channel, floor=floor, platform=platform)

    def trust_candidate(self, release):
        if not isinstance(release, dict) or release.get('draft') is not False:
            raise DownloadError('invalid_release')
        names = {'trust-update.json': MAX_TRUST, 'trust-update.json.minisig': 4096}
        assets = release.get('assets')
        if not isinstance(assets, list):
            raise DownloadError('incomplete_release')
        selected = [item for item in assets if isinstance(item, dict) and item.get('name') in names]
        if len(selected) != 2 or {item['name'] for item in selected} != set(names):
            raise DownloadError('incomplete_release')
        directory = self.directory / 'trust-download'
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(mode=0o750)
        try:
            for asset in selected:
                limit = names[asset['name']]
                if type(asset.get('size')) is not int or not 0 < asset['size'] <= limit:
                    raise DownloadError('download_limit')
                self.download(asset.get('browser_download_url', ''), directory / asset['name'], limit)
            value = verify_trust(directory, KEYS.parent / 'emergency-keys')
            target = self.directory / ('trust-v' + str(value['trust_sequence']))
            shutil.rmtree(target, ignore_errors=True)
            os.replace(directory, target)
            return value
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def poll(self, channel, floor, platform, *, requested=None, notify=lambda version: None,
             failed=lambda version, reason: None, hint=False, trust_enabled=False, trust_floor=0,
             notify_trust=lambda version: None):
        now = self.clock()
        # Boot, timer and hints share this persisted cooldown. Cloud requests
        # cannot flood GitHub or bypass a rate-limit response.
        if now < self.state['backoff_until'] or now < self.state.get('last_poll', 0) + 600:
            return None
        if not (hint or requested) and now < self.state['next_poll']:
            return None
        self.state.update(last_poll=now, next_poll=now + 43200 + self.jitter(-900, 900))
        self.persist()
        try:
            if requested:
                if not isinstance(requested, str) or not TAG.fullmatch(requested):
                    raise DownloadError('invalid_version')
                releases = [self.json(API + '/tags/' + requested)]
            elif channel == 'stable' and not trust_enabled:
                releases = [self.json(API + '/latest')]
            else:
                releases = []
            if trust_enabled or not requested and channel == 'canary':
                requested_releases = releases if requested else []
                releases = []
                for page in range(1, MAX_PAGES + 1):
                    batch = self.json(API + f'?per_page=100&page={page}')
                    if not isinstance(batch, list):
                        raise DownloadError('invalid_api')
                    releases.extend(batch)
                    if len(batch) < 100:
                        break
                else:
                    raise DownloadError('pagination_limit')
                releases.extend(requested_releases)
            if trust_enabled:
                best_trust = None
                for release in releases:
                    tag = str(release.get('tag_name', '')) if isinstance(release, dict) else ''
                    if not (TAG.fullmatch(tag) or TRUST_TAG.fullmatch(tag)):
                        continue
                    try:
                        value = self.trust_candidate(release)
                    except (ReleaseError, DownloadError):
                        continue
                    if value['trust_sequence'] > max(trust_floor, (best_trust or {}).get('trust_sequence', 0)):
                        best_trust = value
                if best_trust:
                    # Root swaps pins before normal metadata or queued candidates
                    # are considered. Cloud supplies no trust payload or URL.
                    notify_trust('trust-v' + str(best_trust['trust_sequence']))
            best = None
            # Authenticate metadata for every bounded API page, then fetch only
            # the archive with the highest eligible signed sequence.
            for release in releases:
                if not isinstance(release, dict) or not TAG.fullmatch(str(release.get('tag_name', ''))):
                    continue
                if requested and release['tag_name'] != requested:
                    continue
                try:
                    manifest = self.candidate(release, channel, floor, platform)
                except (ReleaseError, DownloadError) as exc:
                    if requested:
                        failed(requested, str(exc))
                    continue
                if best is None or manifest['release_sequence'] > best['release_sequence']:
                    if best:
                        shutil.rmtree(self.directory / best['version'], ignore_errors=True)
                    best = manifest
                else:
                    shutil.rmtree(self.directory / manifest['version'], ignore_errors=True)
            if best:
                self.complete(best, channel, floor, platform)
                notify(best['version'])
            self.state['failures'] = 0
            self.persist()
            # Root has copied and reverified the complete bytes before ACK.
            self.prune()
            return best
        except (OSError, ValueError, urllib.error.URLError):
            if requested:
                failed(requested, "download_failed")
            self.prune()
            self.state['failures'] += 1
            self.state['backoff_until'] = max(self.state['backoff_until'], backoff({}, now, self.state['failures']))
            self.persist()
            raise DownloadError('discovery_failed') from None

    def prune(self, keep=None):
        for child in self.directory.iterdir():
            if child.is_dir() and child.name != keep and (TAG.fullmatch(child.name) or TRUST_TAG.fullmatch(child.name)
                                                        or child.name == 'trust-download'):
                shutil.rmtree(child)
            elif child.name.endswith('.part') and self.clock() - child.stat().st_mtime > 86400:
                child.unlink()


def main():
    # Import the pinned bootstrap client, not the candidate application.
    import socket

    def request(command, **fields):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(60)
            connection.connect("/run/messagebox-update/control.sock")
            connection.sendall(json.dumps({"command": command, **fields}).encode() + b"\n")
            data = bytearray()
            while b"\n" not in data and len(data) <= 16384:
                block = connection.recv(4096)
                if not block:
                    break
                data.extend(block)
            result = json.loads(data)
            if result.get("error"):
                raise DownloadError("updater_rejected")
            return result
    try:
        status = request('status')
        Discovery().poll(status['channel'], status['sequence_floor'], local_platform(),
                         requested=status.get('requested_version'),
                         hint=bool(status.get('discovery_hint')),
                         trust_enabled=status.get('trust_updates_enabled') is True,
                         trust_floor=status.get('trust_sequence', 0),
                         notify_trust=lambda version: request('trust_downloaded', version=version),
                         notify=lambda version: request('downloaded', version=version),
                         failed=lambda version, reason: request('download_failed', version=version, reason=reason))
    except Exception:
        print('software update: discovery deferred or rejected.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
