import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
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
                "DISMISSED_CHATS_FILE",
                "NFC_SELECTION_FILE",
                "EVENTS_FILE",
                "QUEUE_DIR",
                "HOLD_DIR",
                "TRASH_DIR",
                "LISTENED_DIR",
                "OUTBOX_DIR",
            )
        }
        dashboard.CONTACTS_FILE = root / "contacts.json"
        dashboard.DISMISSED_CHATS_FILE = root / "dismissed-chats.json"
        dashboard.NFC_SELECTION_FILE = root / "nfc-selection.json"
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
            "Host": "button-box.local",
        }
        handler.client_address = ("192.168.1.20", 12345)
        handler.local_host = "button-box.local"
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
        handler.headers = {"Host": "button-box.local"}
        handler.client_address = ("192.168.1.20", 12345)
        handler.local_host = "button-box.local"
        responses = []
        handler._send = lambda code, data, ctype="application/json": responses.append(
            (code, json.loads(data))
        )
        handler.do_GET()
        return responses[0]

    def post_form(self, path, payload):
        body = urlencode(payload).encode()
        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.path = path
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": "button-box.local",
        }
        handler.client_address = ("192.168.1.20", 12345)
        handler.local_host = "button-box.local"
        handler.tailscale_host = None
        handler.rfile = io.BytesIO(body)
        responses = []
        handler._send = lambda code, data, ctype="application/json": responses.append(
            (code, json.loads(data))
        )
        handler.do_POST()
        return responses[0]

    def test_recipient_phone_form_accepts_optional_name_field(self):
        calls = []
        engine = SimpleNamespace(
            recipient_add_phone=lambda phone, name: calls.append((phone, name)) or {}
        )

        with patch.object(dashboard, "pairing_engine", return_value=engine):
            code, result = self.post_form(
                "/recipients/add-number",
                {"phone": "+1 555 123 4567", "name": ""},
            )

        self.assertEqual(code, 200)
        self.assertEqual(result, {})
        self.assertEqual(calls, [("+15551234567", "")])

    def test_recipient_selection_rejects_name_field(self):
        engine = SimpleNamespace(recipient_select_phone=lambda phone: {})

        with patch.object(dashboard, "pairing_engine", return_value=engine):
            code, result = self.post_form(
                "/recipients/select-number",
                {"phone": "+1 555 123 4567", "name": "Not accepted here"},
            )

        self.assertEqual(code, 409)
        self.assertEqual(result["error"], "phone number invalid")

    def test_runtime_pairing_requires_matching_success_receipt(self):
        store = SimpleNamespace(
            active=lambda: None,
            outcome=lambda attempt: {"status": "success"} if attempt == "confirmed" else None,
        )
        with patch.object(dashboard, "nfc_router", return_value=SimpleNamespace(enrollment=store)):
            code, result = self.get("/api/nfc-runtime?attempt=confirmed")
            self.assertEqual(code, 200)
            self.assertEqual(result["status"], "success")
            self.assertNotIn("request_id", result)
            self.assertEqual(self.get("/api/nfc-runtime?attempt=cancelled")[1]["status"], "idle")
            self.assertEqual(self.get("/api/nfc-runtime")[1]["status"], "idle")

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

    def test_dismissed_chats_are_flagged_and_can_be_restored(self):
        """A caregiver can hide a chat they will never message, such as the box's own."""
        own = "14152309027@s.whatsapp.net"
        other = "15550001@s.whatsapp.net"
        discovered = [
            {"jid": own, "label": own, "kind": "person"},
            {"jid": other, "label": "Grandma", "kind": "person"},
        ]
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=discovered):
            self.assertEqual(
                self.post("/api/contacts", {"action": "dismiss", "jid": own})[0], 200
            )
            _code, view = self.get("/api/contacts")
            flags = {chat["jid"]: chat["dismissed"] for chat in view["discovered"]}
            self.assertTrue(flags[own])
            self.assertFalse(flags[other])

            self.assertEqual(
                self.post("/api/contacts", {"action": "restore", "jid": own})[0], 200
            )
            _code, restored = self.get("/api/contacts")
            self.assertFalse(
                {chat["jid"]: chat["dismissed"] for chat in restored["discovered"]}[own]
            )

    def test_dismissing_never_removes_a_configured_contact(self):
        direct = "15550001@s.whatsapp.net"
        discovered = [{"jid": direct, "label": "Grandma", "kind": "person"}]
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=discovered):
            self.post("/api/contacts", {"action": "add", "jid": direct, "label": "Grandma"})
            self.post("/api/contacts", {"action": "dismiss", "jid": direct})
            _code, view = self.get("/api/contacts")

        self.assertEqual(dashboard.contacts_store().allowed_jids(), (direct,))
        entry = next(c for c in view["discovered"] if c["jid"] == direct)
        self.assertTrue(entry["configured"])
        self.assertFalse(entry["dismissed"], "a configured contact is never hidden")

    def test_add_accepts_a_phone_number_instead_of_a_raw_jid(self):
        direct = "15550001@s.whatsapp.net"
        group = "120363000001@g.us"
        discovered = [
            {"jid": direct, "label": "Grandma", "kind": "person"},
            {"jid": group, "label": "Family", "kind": "group"},
        ]
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=discovered):
            # a caregiver types the number as they would read it aloud
            self.assertEqual(
                self.post(
                    "/api/contacts",
                    {"action": "add", "kind": "person", "phone": "+1 555 0001", "label": "Grandma"},
                )[0],
                200,
            )
            self.assertEqual(
                self.post(
                    "/api/contacts",
                    {"action": "add", "kind": "group", "phone": "120363000001", "label": "Family"},
                )[0],
                200,
            )
        self.assertEqual(
            sorted(dashboard.contacts_store().allowed_jids()), sorted([direct, group])
        )

    def test_a_typed_number_does_not_have_to_be_a_discovered_chat(self):
        """The setup flow's add_phone accepts any number; this must match it."""
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=[]):
            code, _body = self.post(
                "/api/contacts",
                {"action": "add", "kind": "person", "phone": "+49 151 12345678", "label": "Oma"},
            )
        self.assertEqual(code, 200)
        self.assertEqual(
            dashboard.contacts_store().allowed_jids(), ("4915112345678@s.whatsapp.net",)
        )

    def test_a_jid_from_the_discovered_list_must_still_be_discovered(self):
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=[]):
            code, _body = self.post(
                "/api/contacts",
                {"action": "add", "jid": "15559999@s.whatsapp.net", "label": "Stranger"},
            )
        self.assertEqual(code, 400)
        self.assertEqual(dashboard.contacts_store().allowed_jids(), ())

    def test_add_rejects_a_missing_or_unknown_kind(self):
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=[]):
            for payload in (
                {"action": "add", "phone": "15550001", "label": "Grandma"},
                {"action": "add", "kind": "robot", "phone": "15550001", "label": "Grandma"},
                {"action": "add", "kind": "person", "phone": "", "label": "Grandma"},
            ):
                with self.subTest(payload=payload):
                    self.assertEqual(self.post("/api/contacts", payload)[0], 400)
        self.assertEqual(dashboard.contacts_store().allowed_jids(), ())

    def test_unnamed_discovered_chats_never_render_a_raw_jid(self):
        direct = "15550001@s.whatsapp.net"
        group = "120363000001@g.us"
        discovered = [
            {"jid": direct, "label": direct, "kind": "person"},
            {"jid": group, "label": group, "kind": "group"},
        ]
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=discovered):
            code, view = self.get("/api/contacts")

        self.assertEqual(code, 200)
        labels = {chat["label"] for chat in view["discovered"]}
        self.assertEqual(labels, {"+15550001", "Unnamed group"})
        for chat in view["discovered"]:
            self.assertNotIn("@", chat["label"])

    def test_rename_preserves_identity_cards_and_default(self):
        """BB-RECIP-11: renaming changes only the label."""
        direct = "15550001@s.whatsapp.net"
        discovered = [{"jid": direct, "label": "Grandma", "kind": "person"}]
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=discovered):
            self.post("/api/contacts", {"action": "add", "jid": direct, "label": "Grandma"})
        store = dashboard.contacts_store()
        store.assign_card(direct, bytes(range(4)))
        store.choose_default_recipient(direct)

        self.assertEqual(
            self.post(
                "/api/contacts",
                {"action": "rename", "jid": direct, "label": "Oma"},
            )[0],
            200,
        )

        code, view = self.get("/api/contacts")
        self.assertEqual(code, 200)
        self.assertEqual(view["contacts"][direct]["label"], "Oma")
        self.assertEqual(view["contacts"][direct]["card_count"], 1)
        self.assertEqual(view["default_recipient"], direct)
        self.assertEqual(dashboard.contacts_store().allowed_jids(), (direct,))

    def test_default_recipient_can_move_between_configured_contacts(self):
        """BB-RECIP-07: the default can change, unlike during onboarding."""
        first = "15550001@s.whatsapp.net"
        second = "15550002@s.whatsapp.net"
        discovered = [
            {"jid": first, "label": "Grandma", "kind": "person"},
            {"jid": second, "label": "Papa", "kind": "person"},
        ]
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=discovered):
            self.post("/api/contacts", {"action": "add", "jid": first, "label": "Grandma"})
            self.post("/api/contacts", {"action": "add", "jid": second, "label": "Papa"})

        self.assertEqual(
            self.post("/api/contacts", {"action": "default", "jid": first})[0], 200
        )
        self.assertEqual(self.get("/api/contacts")[1]["default_recipient"], first)

        # set_default_recipient would refuse this; choose_ must not.
        self.assertEqual(
            self.post("/api/contacts", {"action": "default", "jid": second})[0], 200
        )
        self.assertEqual(self.get("/api/contacts")[1]["default_recipient"], second)
        self.assertEqual(
            sorted(dashboard.contacts_store().allowed_jids()), sorted([first, second])
        )

    def test_rename_and_default_reject_unknown_contacts(self):
        for payload in (
            {"action": "rename", "jid": "15559999@s.whatsapp.net", "label": "Nobody"},
            {"action": "default", "jid": "15559999@s.whatsapp.net"},
        ):
            with self.subTest(action=payload["action"]):
                code, body = self.post("/api/contacts", payload)
                self.assertEqual(code, 400)
                self.assertFalse(body["ok"])
        self.assertIsNone(self.get("/api/contacts")[1]["default_recipient"])

    def test_presented_card_unpairs_with_an_empty_form_body(self):
        """The endpoint takes no fields, so the UI posts an empty body."""
        from messagebox.nfc_state import SelectionStore

        direct = "15550001@s.whatsapp.net"
        uid = bytes(range(4))
        discovered = [{"jid": direct, "label": "Grandma", "kind": "person"}]
        with patch.object(dashboard, "discover_whatsapp_chats", return_value=discovered):
            self.post("/api/contacts", {"action": "add", "jid": direct, "label": "Grandma"})
        store = dashboard.contacts_store()
        store.assign_card(direct, uid)
        SelectionStore(dashboard.NFC_SELECTION_FILE).select(
            uid, direct, store.public_view()["revision"]
        )

        code, _body = self.post_form("/nfc/unpair-presented", {})

        self.assertEqual(code, 200)
        after = dashboard.contacts_store()
        self.assertIsNone(after.resolve_card(uid))
        self.assertEqual(after.allowed_jids(), (direct,), "the person is kept")

    def test_empty_form_body_reaches_the_endpoint_rather_than_the_parser(self):
        code, body = self.post_form("/nfc/unpair-presented", {})
        self.assertEqual(code, 409)
        self.assertIn("Present a paired NFC card", body["error"])

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
