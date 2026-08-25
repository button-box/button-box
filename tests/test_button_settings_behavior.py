import unittest
import sys
import types
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with patch.dict(sys.modules, {"gpiozero": gpiozero}):
    import messagebox.button_send as button_send  # noqa: E402
from messagebox.settings import defaults  # noqa: E402


class FakeLed:
    def __init__(self):
        self.state = None

    def on(self):
        self.state = "on"

    def off(self):
        self.state = "off"


class ButtonSettingsBehaviorTests(unittest.TestCase):
    def settings(self, **changes):
        document = defaults({"TZ": "America/New_York"})
        document.update(changes)
        return document

    def test_quiet_hours_cross_midnight_and_dst_boundary(self):
        settings = self.settings()
        zone = ZoneInfo("America/New_York")
        self.assertTrue(button_send.quiet_hours(settings, datetime(2026, 3, 8, 1, 30, tzinfo=zone)))
        self.assertTrue(button_send.quiet_hours(settings, datetime(2026, 3, 8, 3, 30, tzinfo=zone)))
        self.assertFalse(button_send.quiet_hours(settings, datetime(2026, 3, 8, 12, 0, tzinfo=zone)))
        settings["quiet_hours"]["enabled"] = False
        self.assertFalse(button_send.quiet_hours(settings, datetime(2026, 3, 8, 1, 30, tzinfo=zone)))

    def test_short_and_long_press_classification(self):
        presses = iter([True, False])
        self.assertEqual(
            button_send.wait_for_hold_intent(
                presses.__next__,
                0.7,
                0.1,
                monotonic=iter([0.0, 0.1, 0.2]).__next__,
                sleeper=lambda _seconds: None,
            ),
            "play",
        )
        clock = iter([0.0, 0.2, 0.4, 0.8]).__next__
        self.assertEqual(
            button_send.wait_for_hold_intent(
                lambda: True,
                0.7,
                0.1,
                monotonic=clock,
                sleeper=lambda _seconds: None,
            ),
            "record",
        )

    def test_arrival_signal_lamp_combinations(self):
        original_led = getattr(button_send, "led", None)
        try:
            for signal, expected in (
                ("ring_and_lamp", "on"),
                ("ring_only", "off"),
                ("lamp_only", "on"),
                ("silent", "off"),
            ):
                with self.subTest(signal=signal):
                    button_send.led = FakeLed()
                    button_send._led_last = 0.0
                    settings = self.settings(arrival_signal=signal)
                    settings["quiet_hours"]["enabled"] = False
                    with patch.object(button_send, "queued", return_value=["waiting.wav"]), patch.object(
                        button_send.time, "monotonic", return_value=100.0
                    ):
                        button_send.refresh_led(force=True, settings=settings)
                    self.assertEqual(button_send.led.state, expected)
        finally:
            button_send.led = original_led


if __name__ == "__main__":
    unittest.main()
