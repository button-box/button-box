#!/usr/bin/env python3
"""Configure the device-local dashboard origin and private Tailscale Serve."""

import argparse
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from messagebox.tailnet import normalize_device_name, normalize_tailscale_host


ENV_KEY = "MSGBOX_TAILSCALE_HOST"
BACKEND = "http://127.0.0.1:80"
SERVICES = (
    "messagebox-dash.service",
    "messagebox-onboarding-home.service",
)


class ProvisionError(RuntimeError):
    pass


def _run(command, *, check=True):
    return subprocess.run(command, capture_output=True, text=True, check=check)


def _read_json(command, error, run):
    try:
        result = run(command)
        payload = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ProvisionError(error) from exc
    if not isinstance(payload, dict):
        raise ProvisionError(error)
    return payload


def read_device_status(run=_run):
    return _read_json(
        ["tailscale", "status", "--json"],
        "could not read Tailscale device status",
        run,
    )


def read_serve_status(run=_run):
    return _read_json(
        ["tailscale", "serve", "status", "--json"],
        "could not read Tailscale Serve configuration",
        run,
    )


def discover_dns_name(payload, expected_name):
    self_status = payload.get("Self")
    if not isinstance(self_status, dict):
        raise ProvisionError("Tailscale did not report this device")
    try:
        return normalize_tailscale_host(
            self_status.get("DNSName"), device_host=normalize_device_name(expected_name)
        )
    except ValueError as exc:
        raise ProvisionError(str(exc)) from exc


def serve_443_state(payload, hostname):
    """Return absent or ready; refuse unknown/conflicting HTTPS 443 state."""
    endpoint = f"{hostname}:443"
    web = payload.get("Web", {})
    tcp = payload.get("TCP", {})
    funnel = payload.get("AllowFunnel", {})
    if not isinstance(web, dict) or not isinstance(tcp, dict) or not isinstance(funnel, dict):
        raise ProvisionError("Tailscale Serve configuration is invalid")

    endpoint_key = next(
        (key for key in web if isinstance(key, str) and key.casefold() == endpoint),
        None,
    )
    tcp_443 = tcp.get("443", tcp.get(443))
    funnel_enabled = any(
        isinstance(key, str) and key.casefold() == endpoint and value is True
        for key, value in funnel.items()
    )
    if endpoint_key is None:
        if tcp_443 is not None or funnel_enabled:
            raise ProvisionError("HTTPS port 443 already has a conflicting Tailscale mapping")
        return "absent"

    entry = web.get(endpoint_key)
    handlers = entry.get("Handlers") if isinstance(entry, dict) else None
    root = handlers.get("/") if isinstance(handlers, dict) else None
    if (
        not isinstance(root, dict)
        or root.get("Proxy") != BACKEND
        or funnel_enabled
    ):
        raise ProvisionError("HTTPS port 443 already has a conflicting Tailscale mapping")
    return "ready"


def _atomic_write(path, data, metadata):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode))
            os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def set_env_value(path, hostname):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ProvisionError("Button Box runtime configuration is not a regular file")
    original = path.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvisionError("Button Box runtime configuration is invalid") from exc
    prefix = f"{ENV_KEY}="
    indexes = [index for index, line in enumerate(text.splitlines()) if line.startswith(prefix)]
    if len(indexes) > 1:
        raise ProvisionError("Button Box runtime configuration defines Tailscale twice")
    lines = text.splitlines()
    setting = prefix + hostname
    if indexes:
        if lines[indexes[0]] == setting:
            return original, metadata, False
        lines[indexes[0]] = setting
    else:
        if lines and lines[-1]:
            lines.append("")
        lines.extend(("# Tailnet-only dashboard hostname, managed by Tailscale provisioning.", setting))
    updated = ("\n".join(lines) + "\n").encode("utf-8")
    _atomic_write(path, updated, metadata)
    return original, metadata, True


def provision(expected_name, env_path, app_dir, run=_run):
    expected_name = normalize_device_name(expected_name)
    if not (app_dir / "messagebox" / "tailnet.py").is_file():
        raise ProvisionError("deploy dashboard Tailscale support before enabling it")
    hostname = discover_dns_name(read_device_status(run), expected_name)
    previous_state = serve_443_state(read_serve_status(run), hostname)
    original, metadata, env_changed = set_env_value(env_path, hostname)
    serve_attempted = False
    try:
        if previous_state == "absent":
            serve_attempted = True
            run(["tailscale", "serve", "--bg", "--https=443", BACKEND])
            if serve_443_state(read_serve_status(run), hostname) != "ready":
                raise ProvisionError("Tailscale Serve did not retain the dashboard mapping")
        if env_changed:
            run(["systemctl", "try-restart", *SERVICES])
    except (OSError, subprocess.CalledProcessError, ProvisionError) as exc:
        rollback_errors = []
        if serve_attempted:
            try:
                run(["tailscale", "serve", "--https=443", "off"])
            except (OSError, subprocess.CalledProcessError):
                rollback_errors.append("Serve mapping")
        if env_changed:
            try:
                _atomic_write(env_path, original, metadata)
                run(["systemctl", "try-restart", *SERVICES], check=False)
            except OSError:
                rollback_errors.append("runtime configuration")
        detail = ""
        if rollback_errors:
            detail = "; manual rollback required for " + " and ".join(rollback_errors)
        raise ProvisionError("could not enable the private dashboard" + detail) from exc
    return f"https://{hostname}/"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-name", required=True)
    parser.add_argument("--env", type=Path, default=Path("/etc/messagebox/env"))
    parser.add_argument("--app-dir", type=Path, default=Path("/opt/messagebox"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run as root")
    try:
        url = provision(args.expected_name, args.env, args.app_dir)
    except (OSError, ValueError, ProvisionError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(url)


if __name__ == "__main__":
    main()
