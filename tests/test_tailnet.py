import unittest

from messagebox.tailnet import (
    normalize_device_name,
    normalize_tailscale_host,
    request_origin,
)


class TailnetTests(unittest.TestCase):
    def test_normalizes_exact_device_tailnet_hostname(self):
        self.assertEqual(normalize_device_name("Button-Box-A7"), "button-box-a7")
        self.assertEqual(
            normalize_tailscale_host(
                "Button-Box-A7.Example-Tailnet.ts.net.",
                device_host="button-box-a7.local",
            ),
            "button-box-a7.example-tailnet.ts.net",
        )

    def test_rejects_unrelated_or_malformed_tailnet_hostname(self):
        invalid = (
            "other-device.example-tailnet.ts.net",
            "button-box-other.example-tailnet.ts.net",
            "button-box-a7.example.com",
            "button-box-a7.a.b.ts.net",
            "button-box-a7.-bad.ts.net",
        )
        for hostname in invalid:
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                normalize_tailscale_host(
                    hostname, device_host="button-box-a7.local"
                )

    def test_accepts_local_http_and_private_https_origins(self):
        tailnet = "button-box-a7.example-tailnet.ts.net"
        self.assertEqual(
            request_origin(
                "button-box-a7.local",
                remote_addr="192.168.1.20",
                http_hosts=("button-box-a7.local",),
                tailscale_host=tailnet,
            ),
            "http://button-box-a7.local",
        )
        self.assertEqual(
            request_origin(
                f"{tailnet}:443",
                remote_addr="127.0.0.1",
                forwarded_proto="https",
                http_hosts=("button-box-a7.local",),
                tailscale_host=tailnet,
            ),
            f"https://{tailnet}",
        )

    def test_rejects_spoofed_proxy_header_bare_ip_and_wrong_port(self):
        tailnet = "button-box-a7.example-tailnet.ts.net"
        cases = (
            (tailnet, "192.168.1.20", "https"),
            (tailnet, "127.0.0.1", "http"),
            (f"{tailnet}:80", "127.0.0.1", "https"),
            ("100.64.0.10", "127.0.0.1", "https"),
        )
        for host, remote, proto in cases:
            with self.subTest(host=host, remote=remote, proto=proto):
                self.assertIsNone(
                    request_origin(
                        host,
                        remote_addr=remote,
                        forwarded_proto=proto,
                        http_hosts=("button-box-a7.local",),
                        tailscale_host=tailnet,
                    )
                )


if __name__ == "__main__":
    unittest.main()
