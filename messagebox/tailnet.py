"""Validation helpers for the optional tailnet-only dashboard origin."""

import ipaddress
import re


_DEVICE_LABEL = r"(?:button|message)-box-[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TAILSCALE_HOST = re.compile(
    rf"(?P<device>{_DEVICE_LABEL})\.(?P<tailnet>{_DNS_LABEL})\.ts\.net",
    re.ASCII,
)


def normalize_device_name(value):
    """Return a supported Button Box machine name."""
    if not isinstance(value, str):
        raise ValueError("Button Box hostname is invalid")
    name = value.strip().rstrip(".").casefold()
    if not re.fullmatch(_DEVICE_LABEL, name, re.ASCII):
        raise ValueError("Button Box hostname is invalid")
    return name


def normalize_tailscale_host(value, *, device_host=None):
    """Return a canonical Tailscale FQDN or fail closed on invalid input."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Tailscale dashboard hostname is invalid")
    host = value.strip().rstrip(".").casefold()
    match = _TAILSCALE_HOST.fullmatch(host)
    if match is None or len(host) > 253:
        raise ValueError("Tailscale dashboard hostname is invalid")
    if device_host is not None:
        expected = str(device_host).strip().rstrip(".").casefold()
        if expected.endswith(".local"):
            expected = expected[:-6]
        expected = normalize_device_name(expected)
        if match.group("device") != expected:
            raise ValueError("Tailscale dashboard hostname does not match this device")
    return host


def request_origin(
    host_header,
    *,
    remote_addr,
    forwarded_proto=None,
    http_hosts=(),
    tailscale_host=None,
):
    """Return the exact trusted browser origin for a request, or ``None``."""
    authority = _parse_authority(host_header)
    if authority is None:
        return None
    host, port = authority
    normalized_http_hosts = {
        candidate.strip().rstrip(".").casefold() for candidate in http_hosts
    }
    if host in normalized_http_hosts and port in {None, 80}:
        return f"http://{host}"
    if tailscale_host is None or host != tailscale_host or port not in {None, 443}:
        return None
    try:
        loopback = ipaddress.ip_address(remote_addr).is_loopback
    except ValueError:
        return None
    if not loopback or forwarded_proto != "https":
        return None
    return f"https://{tailscale_host}"


def _parse_authority(value):
    if not isinstance(value, str) or not value or any(
        character in value for character in "\r\n,/@[]"
    ):
        return None
    host, separator, port_text = value.rpartition(":")
    if not separator:
        host, port_text = value, ""
    elif not port_text.isdigit():
        return None
    host = host.rstrip(".").casefold()
    if not host:
        return None
    port = int(port_text) if port_text else None
    if port is not None and not 1 <= port <= 65535:
        return None
    return host, port
