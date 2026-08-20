import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "onboarding"))

from comitup_adapter import (  # noqa: E402
    ComitupAdapter,
    ComitupError,
)


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.points = []
        self.snapshot = ("HOTSPOT", "")

    def access_points(self):
        self.calls.append(("access_points",))
        return self.points

    def state(self):
        self.calls.append(("state",))
        return self.snapshot

    def connect(self, ssid, password):
        self.calls.append(("connect", ssid, password))

    def delete_connection(self):
        self.calls.append(("delete_connection",))


class FakeProxy:
    def __init__(self, backend):
        self.backend = backend
        self.requests = []

    def get_dbus_method(self, name, interface):
        self.requests.append((name, interface))
        return getattr(self.backend, name)


class FakeBus:
    def __init__(self, proxy):
        self.proxy = proxy
        self.requests = []

    def get_object(self, name, path, **kwargs):
        self.requests.append((name, path, kwargs))
        return self.proxy


class ComitupAdapterTests(unittest.TestCase):
    def test_bus_uses_exact_service_path_and_interface(self):
        backend = FakeBackend()
        proxy = FakeProxy(backend)
        bus = FakeBus(proxy)

        ComitupAdapter(bus=bus).scan_networks()

        self.assertEqual(
            bus.requests,
            [
                (
                    "com.github.davesteele.comitup",
                    "/com/github/davesteele/comitup",
                    {"introspect": False},
                )
            ],
        )
        self.assertEqual(
            proxy.requests,
            [("access_points", "com.github.davesteele.comitup")],
        )

    def test_scan_normalizes_and_ignores_malformed_ap_records(self):
        backend = FakeBackend()
        backend.points = [
            {"ssid": "Home", "security": "encrypted", "strength": "88"},
            {"ssid": "Cafe", "security": "unencrypted", "signal": "101.2"},
            {"ssid": "Mystery", "strength": "not-a-number"},
            {"security": "encrypted", "strength": "42"},
            {"ssid": "", "strength": "42"},
            "not a mapping",
        ]

        self.assertEqual(
            ComitupAdapter(backend=backend).scan_networks(),
            [
                {"ssid": "Home", "security": "encrypted", "signal": 88},
                {"ssid": "Cafe", "security": "unencrypted", "signal": 100},
                {"ssid": "Mystery", "security": "unknown", "signal": None},
            ],
        )
        self.assertEqual(backend.calls, [("access_points",)])

    def test_state_connect_and_delete_are_each_one_shot(self):
        backend = FakeBackend()
        backend.snapshot = ("CONNECTED", "Home")
        adapter = ComitupAdapter(backend=backend)

        self.assertEqual(adapter.get_stable_state(), "CONNECTED")
        adapter.connect_once("Other", "secret")
        adapter.delete_active_connection_once()

        self.assertEqual(backend.calls.count(("state",)), 2)
        self.assertEqual(backend.calls.count(("connect", "Other", "secret")), 1)
        self.assertEqual(backend.calls.count(("delete_connection",)), 1)

    def test_delete_is_rejected_in_hotspot(self):
        backend = FakeBackend()
        with self.assertRaisesRegex(ComitupError, "HOTSPOT"):
            ComitupAdapter(backend=backend).delete_active_connection_once()
        self.assertNotIn(("delete_connection",), backend.calls)

    def test_backend_failure_does_not_log_or_expose_password(self):
        password = "super-secret-password"

        class FailingBackend(FakeBackend):
            def connect(self, _ssid, supplied_password):
                raise RuntimeError(f"failed with {supplied_password}")

        with mock.patch.object(logging.Logger, "_log") as logger:
            with self.assertRaises(ComitupError) as raised:
                ComitupAdapter(backend=FailingBackend()).connect_once(
                    "Home", password
                )
        self.assertNotIn(password, str(raised.exception))
        logger.assert_not_called()


if __name__ == "__main__":
    unittest.main()
