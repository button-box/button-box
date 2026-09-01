import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIREWALL = ROOT / "config" / "onboarding" / "firewall.nft"


class OnboardingFirewallTests(unittest.TestCase):
    def test_loopback_dashboard_is_allowed_before_other_port_80_is_dropped(self):
        rules = FIREWALL.read_text(encoding="utf-8")
        loopback = 'iifname "lo" tcp dport 80 accept'
        port_drop = "tcp dport 80 drop"

        self.assertEqual(rules.count(loopback), 1)
        self.assertEqual(rules.count(port_drop), 1)
        self.assertLess(rules.index(loopback), rules.index(port_drop))


if __name__ == "__main__":
    unittest.main()
