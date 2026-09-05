"""Bounded, interface-specific internet connectivity proof."""

from __future__ import annotations

import ipaddress
import subprocess
import time
from urllib.parse import urlsplit


WIFI_INTERFACE = "wlan0"
DEFAULT_HOSTNAME = "connectivitycheck.gstatic.com"
DEFAULT_HTTPS_URL = "https://connectivitycheck.gstatic.com/generate_204"

PROOF_NM = "wlan0_nm_active"
PROOF_MODE = "wlan0_non_ap"
PROOF_ADDRESS = "wlan0_ipv4"
PROOF_ROUTE = "wlan0_default_route"
PROOF_DNS = "approved_dns"
PROOF_HTTPS = "wlan0_https_204"
PROOFS = frozenset(
    {PROOF_NM, PROOF_MODE, PROOF_ADDRESS, PROOF_ROUTE, PROOF_DNS, PROOF_HTTPS}
)


class ConnectivityChecker:
    def __init__(
        self,
        *,
        command_runner=subprocess.run,
        sleeper=time.sleep,
        attempts=3,
        retry_delay=2.0,
        hostname=DEFAULT_HOSTNAME,
        https_url=DEFAULT_HTTPS_URL,
        command_timeout=5.0,
        connect_timeout=3.0,
        https_timeout=5.0,
    ):
        if not isinstance(attempts, int) or attempts < 1:
            raise ValueError("attempts must be a positive integer")
        if min(command_timeout, connect_timeout, https_timeout) <= 0 or retry_delay < 0:
            raise ValueError("timeouts must be positive")
        parsed = urlsplit(https_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            raise ValueError("HTTPS URL must use the approved hostname")
        self.runner = command_runner
        self.sleeper = sleeper
        self.attempts = attempts
        self.retry_delay = float(retry_delay)
        self.hostname = hostname
        self.https_url = https_url
        self.command_timeout = float(command_timeout)
        self.connect_timeout = float(connect_timeout)
        self.https_timeout = float(https_timeout)

    def _run(self, arguments, *, timeout=None):
        return self.runner(
            list(arguments),
            capture_output=True,
            text=True,
            timeout=self.command_timeout if timeout is None else timeout,
            check=False,
            shell=False,
        )

    def _one_attempt(self):
        proof = []

        result = self._run(
            [
                "nmcli",
                "-t",
                "-f",
                "GENERAL.STATE,GENERAL.TYPE",
                "device",
                "show",
                WIFI_INTERFACE,
            ]
        )
        fields = _colon_fields(result.stdout) if result.returncode == 0 else {}
        state = fields.get("GENERAL.STATE", "")
        if state.split()[:1] != ["100"] or fields.get("GENERAL.TYPE") != "wifi":
            return proof, "WLAN0_NOT_ACTIVE"
        proof.append(PROOF_NM)

        result = self._run(["iw", "dev", WIFI_INTERFACE, "info"])
        if result.returncode != 0 or not any(
            line.strip() == "type managed" for line in result.stdout.splitlines()
        ):
            return proof, "WLAN0_AP_MODE"
        proof.append(PROOF_MODE)

        result = self._run(
            ["ip", "-4", "-o", "addr", "show", "dev", WIFI_INTERFACE]
        )
        if result.returncode != 0 or not _has_usable_ipv4(
            result.stdout, WIFI_INTERFACE
        ):
            return proof, "WLAN0_NO_IPV4"
        proof.append(PROOF_ADDRESS)

        result = self._run(
            ["ip", "-4", "route", "show", "default"]
        )
        if result.returncode != 0 or not _has_default_route(
            result.stdout, WIFI_INTERFACE
        ):
            return proof, "WLAN0_NO_DEFAULT_ROUTE"
        proof.append(PROOF_ROUTE)

        result = self._run(["getent", "ahostsv4", self.hostname])
        if result.returncode != 0 or not _has_dns_address(result.stdout):
            return proof, "DNS_FAILED"
        proof.append(PROOF_DNS)

        result = self._run(
            [
                "curl",
                "--interface",
                WIFI_INTERFACE,
                "--silent",
                "--show-error",
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code}",
                "--connect-timeout",
                _seconds(self.connect_timeout),
                "--max-time",
                _seconds(self.https_timeout),
                "--max-redirs",
                "0",
                self.https_url,
            ],
            timeout=self.https_timeout + 1.0,
        )
        if result.returncode != 0 or result.stdout.strip() != "204":
            return proof, "HTTPS_204_FAILED"
        proof.append(PROOF_HTTPS)
        return proof, None

    def check(self):
        last_proof = []
        last_error = "CONNECTIVITY_FAILED"
        for attempt in range(1, self.attempts + 1):
            try:
                last_proof, last_error = self._one_attempt()
            except Exception:
                last_proof, last_error = [], "CHECK_COMMAND_FAILED"
            if last_error is None:
                return {
                    "ok": True,
                    "proof": last_proof,
                    "error": None,
                    "attempts": attempt,
                }
            if attempt < self.attempts:
                self.sleeper(self.retry_delay)
        return {
            "ok": False,
            "proof": last_proof,
            "error": last_error,
            "attempts": self.attempts,
        }


def _colon_fields(output):
    fields = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key] = value
    return fields


def _has_usable_ipv4(output, interface):
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[1].rstrip(":") != interface:
            continue
        for token in fields:
            if "/" not in token:
                continue
            try:
                address = ipaddress.ip_interface(token).ip
            except ValueError:
                continue
            if (
                isinstance(address, ipaddress.IPv4Address)
                and not address.is_link_local
                and not address.is_loopback
                and not address.is_unspecified
            ):
                return True
    return False


def _has_default_route(output, interface):
    for line in output.splitlines():
        fields = line.split()
        if fields[:1] != ["default"]:
            continue
        if any(
            fields[index : index + 2] == ["dev", interface]
            for index in range(len(fields) - 1)
        ):
            return True
    return False


def _has_dns_address(output):
    for line in output.splitlines():
        try:
            if isinstance(
                ipaddress.ip_address(line.split()[0]), ipaddress.IPv4Address
            ):
                return True
        except (ValueError, IndexError):
            continue
    return False


def _seconds(value):
    return format(value, "g")
