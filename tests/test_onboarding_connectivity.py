import subprocess
import unittest

from messagebox.onboarding.connectivity import (
    DEFAULT_HOSTNAME,
    DEFAULT_HTTPS_URL,
    PROOF_ADDRESS,
    PROOF_DNS,
    PROOF_HTTPS,
    PROOF_MODE,
    PROOF_NM,
    PROOF_ROUTE,
    ConnectivityChecker,
)


NM_OK = "GENERAL.STATE:100 (connected)\nGENERAL.TYPE:wifi\n"
IW_OK = "Interface wlan0\n\ttype managed\n"
ADDRESS_OK = "2: wlan0 inet 192.0.2.8/24 brd 192.0.2.255 scope global wlan0\n"
ROUTE_OK = "default via 192.0.2.1 dev wlan0 proto dhcp\n"
DNS_OK = "142.250.1.1 STREAM connectivitycheck.gstatic.com\n"


class FakeRunner:
    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls = []

    def __call__(self, arguments, **kwargs):
        self.calls.append((arguments, kwargs))
        key = tuple(arguments)
        if key in self.overrides:
            value = self.overrides[key]
            if isinstance(value, Exception):
                raise value
            return subprocess.CompletedProcess(arguments, *value)
        if arguments[0] == "nmcli":
            value = (0, NM_OK, "")
        elif arguments[0] == "iw":
            value = (0, IW_OK, "")
        elif arguments[:3] == ["ip", "-4", "-o"]:
            value = (0, ADDRESS_OK, "")
        elif arguments[0] == "ip":
            value = (0, ROUTE_OK, "")
        elif arguments[0] == "getent":
            value = (0, DNS_OK, "")
        else:
            value = (0, "204", "")
        return subprocess.CompletedProcess(arguments, *value)


class ConnectivityTests(unittest.TestCase):
    def test_full_success_is_structured_and_commands_are_safe(self):
        runner = FakeRunner()
        result = ConnectivityChecker(command_runner=runner).check()

        self.assertEqual(
            result,
            {
                "ok": True,
                "proof": [
                    PROOF_NM,
                    PROOF_MODE,
                    PROOF_ADDRESS,
                    PROOF_ROUTE,
                    PROOF_DNS,
                    PROOF_HTTPS,
                ],
                "error": None,
                "attempts": 1,
            },
        )
        for arguments, kwargs in runner.calls:
            self.assertIsInstance(arguments, list)
            self.assertFalse(kwargs["shell"])
            self.assertGreater(kwargs["timeout"], 0)
        curl = next(arguments for arguments, _ in runner.calls if arguments[0] == "curl")
        self.assertIn("wlan0", curl)
        self.assertIn("--max-redirs", curl)
        self.assertIn("--connect-timeout", curl)
        self.assertIn("--max-time", curl)
        self.assertEqual(curl[-1], DEFAULT_HTTPS_URL)
        dns = next(arguments for arguments, _ in runner.calls if arguments[0] == "getent")
        self.assertEqual(dns, ["getent", "ahostsv4", DEFAULT_HOSTNAME])

    def test_ethernet_does_not_create_a_wifi_false_positive(self):
        runner = FakeRunner()
        runner.overrides[
            (
                "nmcli",
                "-t",
                "-f",
                "GENERAL.STATE,GENERAL.TYPE",
                "device",
                "show",
                "wlan0",
            )
        ] = (0, "GENERAL.STATE:30 (disconnected)\nGENERAL.TYPE:wifi\n", "")

        result = ConnectivityChecker(command_runner=runner, attempts=1).check()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "WLAN0_NOT_ACTIVE")
        self.assertEqual(len(runner.calls), 1)

    def test_link_local_address_default_route_dns_and_https_fail_closed(self):
        cases = [
            ("ip", (0, "2: wlan0 inet 169.254.2.3/16 scope global wlan0\n", ""), "WLAN0_NO_IPV4"),
            (
                "route",
                (0, "default via 192.0.2.1 dev eth0 proto dhcp\n", ""),
                "WLAN0_NO_DEFAULT_ROUTE",
            ),
            ("getent", (2, "", "resolver details",), "DNS_FAILED"),
            ("curl", (0, "200", ""), "HTTPS_204_FAILED"),
            ("curl", (0, "301", ""), "HTTPS_204_FAILED"),
        ]
        for stage, response, error in cases:
            with self.subTest(stage=stage, response=response):
                runner = FakeRunner()
                checker = ConnectivityChecker(command_runner=runner, attempts=1)
                original = runner.__call__

                def fail_selected(arguments, **kwargs):
                    is_stage = (
                        (stage == "ip" and arguments[:3] == ["ip", "-4", "-o"])
                        or (stage == "route" and arguments[:4] == ["ip", "-4", "route", "show"])
                        or arguments[0] == stage
                    )
                    if is_stage:
                        runner.calls.append((arguments, kwargs))
                        return subprocess.CompletedProcess(arguments, *response)
                    return original(arguments, **kwargs)

                checker.runner = fail_selected
                result = checker.check()
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], error)

    def test_non_managed_mode_is_rejected(self):
        runner = FakeRunner({("iw", "dev", "wlan0", "info"): (0, "type AP\n", "")})
        result = ConnectivityChecker(command_runner=runner, attempts=1).check()
        self.assertEqual(result["error"], "WLAN0_AP_MODE")

    def test_failures_retry_exactly_configured_attempts(self):
        runner = FakeRunner({("getent", "ahostsv4", DEFAULT_HOSTNAME): (2, "", "private resolver failure")})
        sleeps = []
        result = ConnectivityChecker(
            command_runner=runner, sleeper=sleeps.append, attempts=3
        ).check()
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["error"], "DNS_FAILED")
        self.assertEqual(
            sum(call[0][0] == "getent" for call in runner.calls), 3
        )
        self.assertEqual(sleeps, [2.0, 2.0])

    def test_runner_exception_text_is_not_returned(self):
        secret = "internal-command-secret"
        runner = FakeRunner()
        runner.overrides[
            (
                "nmcli",
                "-t",
                "-f",
                "GENERAL.STATE,GENERAL.TYPE",
                "device",
                "show",
                "wlan0",
            )
        ] = RuntimeError(secret)
        result = ConnectivityChecker(command_runner=runner, attempts=1).check()
        self.assertEqual(result["error"], "CHECK_COMMAND_FAILED")
        self.assertNotIn(secret, repr(result))

    def test_endpoint_must_match_approved_hostname(self):
        with self.assertRaises(ValueError):
            ConnectivityChecker(
                hostname="approved.example",
                https_url="https://other.example/generate_204",
            )


if __name__ == "__main__":
    unittest.main()
