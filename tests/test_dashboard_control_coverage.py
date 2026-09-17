import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "messagebox" / "onboarding" / "static" / "index.html"
APP = ROOT / "messagebox" / "onboarding" / "static" / "app.js"
MATRIX = ROOT / "docs" / "box-acceptance.md"


CONTROL_CASES = {
    "skip-link": ["BB-LAYOUT-01"],
    "copy-box-id": ["BB-LAYOUT-01"],
    "owner-community": ["BB-LAYOUT-01"],
    "manual-default-name": ["BB-RECIP-11"],
    "manual-allow-name": ["BB-RECIP-11"],
    "change-test-recipient": ["BB-RECIP-01"],
    "nfc-allow-phone": ["BB-RECIP-05", "BB-RECIP-06"],
    "nfc-allow-name": ["BB-RECIP-11"],
    "allow-nfc-recipient": ["BB-RECIP-05", "BB-NFC-03"],
    "nav-home": ["BB-NAV-01"],
    "nav-setup": ["BB-NAV-01"],
    "nav-settings": ["BB-NAV-01"],
    "nav-activity": ["BB-NAV-01"],
    "nav-advanced": ["BB-NAV-01"],
    "home-continue-setup": ["BB-SETUP-01"],
    "home-button-settings": ["BB-NAV-01"],
    "home-sound-settings": ["BB-NAV-01"],
    "home-connections-setup": ["BB-SETUP-01"],
    "ring-now": ["BB-HOME-02"],
    "recording-mode-tap-review": ["BB-SET-02"],
    "recording-mode-hold-release": ["BB-SET-02"],
    "after-listening-play-only": ["BB-SET-02"],
    "after-listening-invite-reply": ["BB-SET-02"],
    "max-recording": ["BB-SET-02"],
    "ringtone": ["BB-SET-02"],
    "preview-ringtone": ["BB-AUDIO-02"],
    "master-volume": ["BB-SET-02"],
    "arrival-signal": ["BB-SET-02", "BB-VOICE-02"],
    "quiet-enabled": ["BB-SET-02", "BB-VOICE-02"],
    "quiet-start": ["BB-SET-02", "BB-VOICE-02"],
    "quiet-end": ["BB-SET-02", "BB-VOICE-02"],
    "timezone": ["BB-SET-02", "BB-VOICE-02"],
    "nfc-beep": ["BB-SET-02", "BB-NFC-03"],
    "save-settings": ["BB-SET-01", "BB-SET-02"],
    "advanced-connections-setup": ["BB-SETUP-01"],
    "advanced-recipients-setup": ["BB-SETUP-01"],
    "manage-whatsapp": ["BB-NAV-02", "BB-WA-01"],
    "manage-recipients": ["BB-NAV-03"],
    "new-wifi-name": ["BB-WIFI-04"],
    "new-wifi-security-protected": ["BB-WIFI-04"],
    "new-wifi-security-open": ["BB-WIFI-04"],
    "new-wifi-password": ["BB-WIFI-04"],
    "change-wifi-submit": ["BB-WIFI-04"],
    "listener-jid": ["BB-ADV-01"],
    "listener-name": ["BB-ADV-01"],
    "listener-clip": ["BB-ADV-01"],
    "save-listener-profile": ["BB-ADV-01"],
    "scan-again": ["BB-WIFI-01"],
    "ssid": ["BB-WIFI-01", "BB-WIFI-02"],
    "wifi-security-protected": ["BB-WIFI-01", "BB-WIFI-02"],
    "wifi-security-open": ["BB-WIFI-01", "BB-WIFI-02"],
    "wifi-password": ["BB-WIFI-02"],
    "copy-setup-url": ["BB-WIFI-03"],
    "connect-wifi": ["BB-WIFI-02"],
    "checking-recheck": ["BB-WIFI-02"],
    "whatsapp-phone": ["BB-WA-02"],
    "start-whatsapp-pairing": ["BB-WA-02"],
    "copy-pairing-code": ["BB-WA-02"],
    "cancel-pairing": ["BB-WA-02"],
    "cancel-progress": ["BB-WA-02"],
    "retry-pairing": ["BB-WA-02"],
    "continue-recipients": ["BB-RECIP-01"],
    "show-unlink": ["BB-WA-03"],
    "confirm-whatsapp-unlink": ["BB-WA-03"],
    "keep-account": ["BB-WA-03"],
    "manual-default-phone": ["BB-RECIP-01", "BB-RECIP-02", "BB-RECIP-06"],
    "choose-manual-default": ["BB-RECIP-01", "BB-RECIP-02", "BB-RECIP-06"],
    "refresh-recipients": ["BB-RECIP-03"],
    "defer-recipients": ["BB-RECIP-04"],
    "resume-recipients": ["BB-RECIP-04"],
    "open-recipient-manager": ["BB-NAV-03"],
    "manual-allow-phone": ["BB-RECIP-05", "BB-RECIP-06"],
    "allow-manual-recipient": ["BB-RECIP-05", "BB-RECIP-06"],
    "manager-refresh": ["BB-RECIP-03"],
    "continue-nfc": ["BB-NFC-01", "BB-NFC-03"],
    "unpair-presented-nfc": ["BB-NFC-03"],
    "cancel-runtime-nfc": ["BB-NFC-03"],
    "back-from-nfc": ["BB-NFC-01"],
    "finish-nfc": ["BB-NFC-01"],
    "cancel-nfc-tag": ["BB-NFC-01", "BB-NFC-03"],
    "keep-nfc-pairing": ["BB-NFC-03"],
    "reassign-nfc": ["BB-NFC-03"],
    "cancel-mapped-tag": ["BB-NFC-01", "BB-NFC-03"],
    "pair-another-nfc": ["BB-NFC-03"],
    "done-nfc": ["BB-NFC-01", "BB-NFC-03"],
    "retry-nfc": ["BB-NFC-01"],
    "skip-unavailable-nfc": ["BB-NFC-01"],
    "back-from-unavailable": ["BB-NFC-01"],
    "failure-recheck": ["BB-WIFI-02"],
    "try-again": ["BB-WIFI-02"],
    "reset-failed-network": ["BB-WIFI-02"],
}


class InteractiveParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag not in {"a", "button", "input", "select"}:
            return
        if tag == "input" and attributes.get("type") == "hidden":
            return
        self.controls.append(attributes.get("id"))


class DashboardControlCoverageTests(unittest.TestCase):
    def test_every_static_control_maps_to_a_canonical_case(self):
        parser = InteractiveParser()
        parser.feed(INDEX.read_text(encoding="utf-8"))
        self.assertNotIn(None, parser.controls, "every control needs a stable id")
        self.assertEqual(len(parser.controls), len(set(parser.controls)), "control ids must be unique")
        self.assertEqual(set(parser.controls), set(CONTROL_CASES))

        case_ids = set(re.findall(r"^\| (BB-[A-Z]+-[0-9]+) \|", MATRIX.read_text(encoding="utf-8"), re.MULTILINE))
        mapped_ids = {case_id for values in CONTROL_CASES.values() for case_id in values}
        self.assertFalse(mapped_ids - case_ids, "control mapping references unknown cases")

    def test_every_dynamic_control_uses_acceptance_wrapper(self):
        source = APP.read_text(encoding="utf-8")
        self.assertFalse('document.createElement("button")' in source,
                         "dynamic buttons need an acceptance case")
        self.assertFalse('document.createElement("a")' in source,
                         "dynamic links need an acceptance case")
        dynamic_ids = set(re.findall(r'acceptanceControl\("(?:button|a)", "(BB-[A-Z]+-[0-9]+)"', source))
        case_ids = set(re.findall(r"^\| (BB-[A-Z]+-[0-9]+) \|", MATRIX.read_text(encoding="utf-8"), re.MULTILINE))
        self.assertTrue(dynamic_ids)
        self.assertFalse(dynamic_ids - case_ids)


if __name__ == "__main__":
    unittest.main()
