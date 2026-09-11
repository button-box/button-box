import unittest
from unittest import mock

from messagebox import nfc


class TransportSelectionTests(unittest.TestCase):
    def test_defaults_to_i2c(self):
        with mock.patch.dict(nfc.os.environ, {}, clear=False):
            nfc.os.environ.pop("MSGBOX_NFC_TRANSPORT", None)
            self.assertEqual(nfc.transport(), "i2c")

    def test_i2c_transport_builds_pn532_reader(self):
        with mock.patch.dict(nfc.os.environ, {"MSGBOX_NFC_TRANSPORT": "i2c"}):
            with mock.patch.object(nfc, "PN532I2CReader") as pn532:
                nfc.hardware_reader()
                pn532.assert_called_once_with()

    def test_switch_transport_builds_switch_reader_with_configured_pins(self):
        with mock.patch.dict(
            nfc.os.environ,
            {"MSGBOX_NFC_TRANSPORT": "switch", "MSGBOX_SWITCH_PINS": "5, 6,12"},
        ):
            with mock.patch.object(nfc, "SwitchReader") as switch:
                nfc.hardware_reader()
                switch.assert_called_once_with([5, 6, 12])

    def test_unknown_transport_raises(self):
        with mock.patch.dict(nfc.os.environ, {"MSGBOX_NFC_TRANSPORT": "bogus"}):
            with self.assertRaises(RuntimeError):
                nfc.hardware_reader()


class SwitchPinsTests(unittest.TestCase):
    def test_blank_yields_no_pins(self):
        with mock.patch.dict(nfc.os.environ, {"MSGBOX_SWITCH_PINS": ""}):
            self.assertEqual(nfc._switch_pins(), [])

    def test_parses_comma_separated_pins(self):
        with mock.patch.dict(nfc.os.environ, {"MSGBOX_SWITCH_PINS": "5,6,12,13,19"}):
            self.assertEqual(nfc._switch_pins(), [5, 6, 12, 13, 19])

    def test_invalid_pin_raises(self):
        with mock.patch.dict(nfc.os.environ, {"MSGBOX_SWITCH_PINS": "5,x"}):
            with self.assertRaises(RuntimeError):
                nfc._switch_pins()


class SwitchReaderTests(unittest.TestCase):
    def _reader(self, levels):
        """A SwitchReader wired to fake lgpio calls instead of real hardware."""
        fake_lgpio = mock.Mock()
        fake_lgpio._gpiochip_open.return_value = 3
        fake_lgpio._gpio_claim_input.return_value = 0
        fake_lgpio._gpio_read.side_effect = lambda chip, pin: levels[pin]
        with mock.patch.dict("sys.modules", {"_lgpio": fake_lgpio}):
            reader = nfc.SwitchReader([5, 6, 12], timeout=0)
        return reader

    def test_exactly_one_active_line_reports_its_position(self):
        reader = self._reader({5: 1, 6: 0, 12: 1})
        self.assertEqual(reader.read(), nfc.SWITCH_UID_PREFIX + bytes([1]))

    def test_no_active_line_reports_no_card(self):
        reader = self._reader({5: 1, 6: 1, 12: 1})
        self.assertIsNone(reader.read())

    def test_multiple_active_lines_report_no_card(self):
        reader = self._reader({5: 0, 6: 0, 12: 1})
        self.assertIsNone(reader.read())


if __name__ == "__main__":
    unittest.main()
