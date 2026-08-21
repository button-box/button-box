"""Small, synchronous boundary around Comitup's D-Bus API."""

from __future__ import annotations

import importlib
from collections.abc import Mapping


BUS_NAME = INTERFACE = "com.github.davesteele.comitup"
OBJECT_PATH = "/com/github/davesteele/comitup"
VALID_STATES = frozenset({"HOTSPOT", "CONNECTING", "CONNECTED"})


class ComitupError(RuntimeError):
    """A safe error suitable for returning across the onboarding boundary."""


class ComitupAdapter:
    """Expose individual Comitup operations without retries or polling."""

    def __init__(self, *, bus=None, backend=None):
        if bus is not None and backend is not None:
            raise ValueError("provide either bus or backend, not both")
        self._bus = bus
        self._backend = backend

    def _service(self):
        if self._backend is not None:
            return self._backend

        bus = self._bus
        if bus is None:
            try:
                dbus = importlib.import_module("dbus")
                bus = dbus.SystemBus()
            except Exception:
                raise ComitupError("Comitup D-Bus is unavailable") from None
            self._bus = bus

        try:
            self._backend = bus.get_object(BUS_NAME, OBJECT_PATH, introspect=False)
        except Exception:
            raise ComitupError("Comitup D-Bus is unavailable") from None
        return self._backend

    def _call(self, method_name, *args):
        try:
            service = self._service()
            get_dbus_method = getattr(service, "get_dbus_method", None)
            if callable(get_dbus_method):
                method = service.get_dbus_method(method_name, INTERFACE)
            else:
                method = getattr(service, method_name)
            return method(*args)
        except ComitupError:
            raise
        except Exception:
            raise ComitupError(f"Comitup {method_name} failed") from None

    def scan_networks(self):
        """Return safe AP records, ignoring malformed records from D-Bus."""
        raw_records = self._call("access_points")
        if isinstance(raw_records, (str, bytes)):
            return []
        try:
            records = iter(raw_records)
        except TypeError:
            return []

        networks = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            ssid = record.get("ssid")
            if not isinstance(ssid, str) or not ssid or "\x00" in ssid:
                continue

            security = record.get("security")
            if not isinstance(security, str) or not security:
                security = "unknown"

            raw_signal = record.get("signal", record.get("strength"))
            try:
                signal = int(round(float(raw_signal)))
            except (TypeError, ValueError, OverflowError):
                signal = None
            if signal is not None:
                signal = max(0, min(100, signal))

            networks.append(
                {"ssid": ssid, "security": security, "signal": signal}
            )
        return networks

    def get_stable_state(self):
        """Read and validate one state snapshot; this method never polls."""
        snapshot = self._call("state")
        if (
            not isinstance(snapshot, (list, tuple))
            or len(snapshot) != 2
            or not isinstance(snapshot[0], str)
        ):
            raise ComitupError("Comitup returned an invalid state")
        state = snapshot[0].upper()
        if state not in VALID_STATES:
            raise ComitupError("Comitup returned an invalid state")
        return state

    def connect_once(self, ssid, password):
        if not isinstance(ssid, str) or not ssid or "\x00" in ssid:
            raise ValueError("SSID must be a non-empty string")
        if not isinstance(password, str):
            raise ValueError("password must be a string")
        self._call("connect", ssid, password)

    def delete_active_connection_once(self):
        state = self.get_stable_state()
        if state == "HOTSPOT":
            raise ComitupError("cannot delete a connection in HOTSPOT state")
        self._call("delete_connection")
