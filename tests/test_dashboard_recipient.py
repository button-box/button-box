import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


import messagebox.dashboard.app as dashboard


class DashboardContactTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.originals = {
            name: getattr(dashboard, name)
            for name in (
                "CONTACTS_FILE",
                "EVENTS_FILE",
                "QUEUE_DIR",
                "HOLD_DIR",
                "TRASH_DIR",
                "LISTENED_DIR",
                "OUTBOX_DIR",
            )
        }
        dashboard.CONTACTS_FILE = root / "contacts.json"
        dashboard.EVENTS_FILE = str(root / "events.jsonl")
        dashboard.QUEUE_DIR = str(root / "queue")
        dashboard.HOLD_DIR = str(root / "queue" / ".hold")
        dashboard.TRASH_DIR = str(root / "queue" / ".trash")
        dashboard.LISTENED_DIR = str(root / "listened")
        dashboard.OUTBOX_DIR = str(root / "outbox")
        for path in (
            dashboard.QUEUE_DIR,
            dashboard.HOLD_DIR,
            dashboard.TRASH_DIR,
            dashboard.OUTBOX_DIR,
        ):
            Path(path).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(dashboard, name, value)
        self.temporary_directory.cleanup()

    def post(self, path, payload, content_type="application/json"):
        body = json.dumps(payload).encode()
        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.path = path
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": content_type,
        }
        handler.rfile = io.BytesIO(body)
        responses = []
        handler._send = lambda code, data, ctype="application/json": responses.append(
            (code, json.loads(data))
        )
        handler.do_POST()
        return responses[0]

    def get(self, path):
        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.path = path
        responses = []
        handler._send = lambda code, data, ctype="application/json": responses.append(
            (code, json.loads(data))
        )
        handler.do_GET()
        return responses[0]

    def test_missing_store_starts_empty_without_a_default_recipient_gate(self):
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=[]):
            settings = dashboard.contact_settings()

        self.assertEqual(settings["contacts"], {})
        self.assertEqual(settings["listeners"], {})
        self.assertEqual(settings["mode"], "empty")
        self.assertFalse(Path(dashboard.CONTACTS_FILE).exists())
        self.assertNotIn("default_recipient", dashboard.build_data())
        self.assertFalse(hasattr(dashboard, "DEFAULT_CHAT_JID"))

    def test_discovery_accepts_only_exact_supported_direct_and_group_jids(self):
        payload = {
            "data": [
                {
                    "jid": "15550001@s.whatsapp.net",
                    "kind": "dm",
                    "name": " Grandma ",
                    "last_message_ts": "2026-08-01T09:00:00Z",
                },
                {
                    "jid": "120363000000-1700000000@g.us",
                    "kind": "group",
                    "name": " Family ",
                },
                {"jid": "120363000001@g.us", "kind": "group", "name": ""},
                {"jid": "not-numeric@s.whatsapp.net", "name": "Bad direct"},
                {"jid": "family@g.us", "kind": "group", "name": "Bad group"},
                {"jid": "15550002@lid", "name": "Unsupported"},
            ]
        }
        result = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        with patch.object(dashboard.subprocess, "run", return_value=result):
            chats = dashboard.discover_whatsapp_chats()

        self.assertEqual(
            chats,
            [
                {
                    "jid": "120363000000-1700000000@g.us",
                    "label": "Family",
                    "kind": "group",
                    "last_active": None,
                    "discovered": True,
                },
                {
                    "jid": "15550001@s.whatsapp.net",
                    "label": "Grandma",
                    "kind": "person",
                    "last_active": "2026-08-01T09:00:00Z",
                    "discovered": True,
                },
                {
                    "jid": "120363000001@g.us",
                    "label": "Unnamed group",
                    "kind": "group",
                    "last_active": None,
                    "discovered": True,
                },
            ],
        )

    def test_configured_contacts_are_preserved_when_not_discovered(self):
        dashboard.contacts_store().add_contact("15550001@s.whatsapp.net", "Grandma")
        discovered = [
            {
                "jid": "120363000001@g.us",
                "label": "Family from WhatsApp",
                "kind": "group",
                "last_active": None,
                "discovered": True,
            }
        ]

        with patch.object(dashboard, "discover_whatsapp_chats", return_value=discovered):
            code, settings = self.get("/api/contacts")

        self.assertEqual(code, 200)
        preserved = next(
            chat
            for chat in settings["discovered"]
            if chat["jid"] == "15550001@s.whatsapp.net"
        )
        self.assertEqual(preserved["label"], "Grandma")
        self.assertTrue(preserved["configured"])
        self.assertFalse(preserved["discovered"])

        with patch.object(
            dashboard,
            "discover_whatsapp_chats",
            side_effect=RuntimeError("wacli busy"),
        ):
            unavailable = dashboard.contact_settings()
        self.assertIn("15550001@s.whatsapp.net", unavailable["contacts"])
        self.assertIsNotNone(unavailable["discovery_error"])

    def test_add_remove_one_multi_mode_and_uid_redaction(self):
        direct = "15550001@s.whatsapp.net"
        group = "120363000001@g.us"
        discovered = [
            {"jid": direct, "label": "Grandma", "kind": "person"},
            {"jid": group, "label": "Family", "kind": "group"},
        ]
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=discovered):
            self.assertEqual(
                self.post(
                    "/api/contacts",
                    {"action": "add", "jid": direct, "label": "Grandma"},
                )[0],
                200,
            )
            one = dashboard.contact_settings()
            self.assertEqual(one["mode"], "default")
            self.assertFalse(one["contacts"][direct]["paired"])
            self.assertEqual(one["contacts"][direct]["card_count"], 0)

            private_uid = bytes(range(4))
            dashboard.contacts_store().assign_card(direct, private_uid)
            self.assertEqual(
                self.post(
                    "/api/contacts",
                    {"action": "add", "jid": group, "label": "Family"},
                )[0],
                200,
            )
            code, multiple = self.get("/api/contacts")
            self.assertEqual(code, 200)
            self.assertEqual(multiple["mode"], "default")
            self.assertTrue(multiple["contacts"][direct]["paired"])
            self.assertEqual(multiple["contacts"][direct]["card_count"], 1)
            rendered = json.dumps(multiple)
            self.assertNotIn("card_uids", rendered)
            self.assertNotIn(private_uid.hex().upper(), rendered.replace(":", ""))

            self.assertEqual(
                self.post("/api/contacts", {"action": "remove", "jid": direct})[0],
                200,
            )

        self.assertIsNone(dashboard.contacts_store().resolve_card(private_uid))
        self.assertEqual(dashboard.contacts_store().allowed_jids(), (group,))

    def test_add_rejects_malformed_and_undiscovered_jids(self):
        discovered = [
            {"jid": "15550001@s.whatsapp.net", "label": "Grandma", "kind": "person"}
        ]
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=discovered):
            malformed = self.post(
                "/api/contacts",
                {"action": "add", "jid": "grandma@s.whatsapp.net", "label": "Grandma"},
            )
            undiscovered = self.post(
                "/api/contacts",
                {"action": "add", "jid": "15559999@s.whatsapp.net", "label": "Stranger"},
            )
            wrong_content_type = self.post(
                "/api/contacts",
                {"action": "add", "jid": "15550001@s.whatsapp.net", "label": "Grandma"},
                content_type="text/plain",
            )

        self.assertEqual(malformed[0], 400)
        self.assertEqual(undiscovered[0], 400)
        self.assertEqual(wrong_content_type[0], 415)
        self.assertEqual(dashboard.contacts_store().allowed_jids(), ())

    def test_listener_profiles_support_upsert_empty_clip_and_remove(self):
        jid = "15550001:4@s.whatsapp.net"
        code, _ = self.post(
            "/api/listeners",
            {"action": "upsert", "jid": jid, "name": "Mommy", "listened_clip": ""},
        )
        self.assertEqual(code, 200)
        settings = dashboard.contacts_store().public_view()
        canonical_jid = "15550001@s.whatsapp.net"
        self.assertEqual(
            settings["listeners"][canonical_jid],
            {"name": "Mommy", "listened_clip": ""},
        )

        clip = "/var/lib/messagebox/assets/listened/mommy.wav"
        self.assertEqual(
            self.post(
                "/api/listeners",
                {
                    "action": "upsert",
                    "jid": canonical_jid,
                    "name": "Mom",
                    "listened_clip": clip,
                },
            )[0],
            200,
        )
        self.assertEqual(
            dashboard.contacts_store().listener_profiles()[canonical_jid],
            {"name": "Mom", "clip": clip},
        )
        self.assertEqual(
            self.post(
                "/api/listeners", {"action": "remove", "jid": canonical_jid}
            )[0],
            200,
        )
        self.assertEqual(dashboard.contacts_store().listener_profiles(), {})

    def test_historical_and_unknown_chats_never_render_raw_jids(self):
        removed = "120363000009@g.us"
        unknown = "legacy-private-chat@g.us"
        Path(dashboard.EVENTS_FILE).write_text(
            "\n".join(
                json.dumps(event)
                for event in (
                    {"type": "received", "ts": 1, "chat": removed},
                    {"type": "sent", "ts": 2, "target": unknown},
                )
            )
            + "\n",
            encoding="utf-8",
        )

        data = dashboard.build_data()
        rendered = json.dumps(data)
        self.assertIn("Removed contact", rendered)
        self.assertIn("Unknown contact", rendered)
        self.assertNotIn(removed, rendered)
        self.assertNotIn(unknown, rendered)


if __name__ == "__main__":
    unittest.main()
