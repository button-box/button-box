import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.contacts import ContactError, ContactStore, main


PERSON = "15551234567@s.whatsapp.net"
GROUP = "120363123456789@g.us"
LEGACY_GROUP = "15551234567-1700000000@g.us"
CARD_ONE = "04:A1:00:FF"
CARD_TWO = "01:02:03:04:05:06:07"


class Clock:
    def __init__(self, value=1_000.5):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


class ContactStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "private" / "contacts.json"
        self.clock = Clock()
        self.store = ContactStore(self.path, clock=self.clock)

    def tearDown(self):
        self.directory.cleanup()

    def test_missing_store_loads_empty_without_writing(self):
        self.assertEqual(
            self.store.load(),
            {"version": 1, "revision": 0, "contacts": {}, "listeners": {}},
        )
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_malformed_store_raises_and_mutation_never_replaces_it(self):
        self.path.parent.mkdir()
        original = b'{"version": 1, broken'
        self.path.write_bytes(original)

        with self.assertRaises(ContactError):
            self.store.load()
        with self.assertRaises(ContactError):
            self.store.add_contact(PERSON, "Grandma")

        self.assertEqual(self.path.read_bytes(), original)

    def test_existing_document_schema_is_strictly_validated(self):
        valid = {
            "version": 1,
            "revision": 0,
            "contacts": {
                PERSON: {
                    "label": "Grandma",
                    "kind": "person",
                    "receive_after": 0,
                    "card_uids": [CARD_ONE],
                    "card_clip": "",
                }
            },
            "listeners": {},
        }
        invalid_documents = []

        wrong_kind = json.loads(json.dumps(valid))
        wrong_kind["contacts"][PERSON]["kind"] = "group"
        invalid_documents.append(wrong_kind)

        duplicate_card = json.loads(json.dumps(valid))
        duplicate_card["contacts"][GROUP] = {
            "label": "Family",
            "kind": "group",
            "receive_after": 0,
            "card_uids": [CARD_ONE],
            "card_clip": "",
        }
        invalid_documents.append(duplicate_card)

        linked_listener_key = json.loads(json.dumps(valid))
        linked_listener_key["listeners"] = {
            "15550001:7@s.whatsapp.net": {"name": "Mom", "listened_clip": ""}
        }
        invalid_documents.append(linked_listener_key)

        for document in (
            {**valid, "revision": -1},
            {**valid, "revision": True},
            {**valid, "extra": True},
            *invalid_documents,
        ):
            with self.subTest(document=document):
                self.path.parent.mkdir(exist_ok=True)
                self.path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(ContactError):
                    self.store.load()

    def test_chat_jids_are_exact_and_kind_is_inferred(self):
        person = self.store.add_contact(PERSON, "Grandma", receive_after=0)
        group = self.store.add_contact(GROUP, "Family", receive_after=0)
        legacy = self.store.add_contact(LEGACY_GROUP, "Cousins", receive_after=0)

        self.assertEqual(person["kind"], "person")
        self.assertEqual(group["kind"], "group")
        self.assertEqual(legacy["kind"], "group")
        self.assertEqual(
            self.store.allowed_jids(), tuple(sorted((PERSON, GROUP, LEGACY_GROUP)))
        )
        for invalid in (
            "family@g.us",
            "123-abc@g.us",
            "person@s.whatsapp.net",
            "123@G.US",
            "123@other.example",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ContactError):
                    self.store.add_contact(invalid, "Invalid")

    def test_listener_jids_are_lowercase_and_drop_linked_device_suffix(self):
        result = self.store.upsert_listener(
            " 15550001:7@S.WHATSAPP.NET ",
            " Mom ",
            listened_clip="/var/lib/messagebox/assets/mom.wav",
        )

        self.assertEqual(result["jid"], "15550001@s.whatsapp.net")
        self.assertEqual(
            self.store.listener_profiles(),
            {
                "15550001@s.whatsapp.net": {
                    "name": "Mom",
                    "clip": "/var/lib/messagebox/assets/mom.wav",
                }
            },
        )

    def test_mutations_increment_once_use_one_default_clock_reading_and_write_private(self):
        self.store.add_contact(PERSON, "Grandma")
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(self.store.load()["revision"], 1)
        self.assertEqual(self.store.contact(PERSON)["receive_after"], self.clock.value)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

        self.store.assign_card(PERSON, CARD_ONE)
        self.store.upsert_listener("15550001@s.whatsapp.net", "Mom")
        self.store.remove_listener("15550001:9@s.whatsapp.net")
        self.assertEqual(self.store.load()["revision"], 4)

        with self.assertRaises(ContactError):
            self.store.add_contact(PERSON, "Duplicate")
        self.assertEqual(self.clock.calls, 1)
        self.assertEqual(self.store.load()["revision"], 4)

    def test_multiple_cards_normalize_and_resolve_to_the_contact(self):
        self.store.add_contact(PERSON, "Grandma", receive_after=0)
        self.store.assign_card(PERSON, b"\x04\xa1\x00\xff")
        self.store.assign_card(PERSON, "01-02 03:04-05 06:07")

        contact = self.store.contact(PERSON)
        self.assertEqual(contact["card_uids"], [CARD_ONE, CARD_TWO])
        self.assertEqual(self.store.resolve_card("04-a1-00-ff")["jid"], PERSON)
        self.assertEqual(self.store.resolve_card("10:20:30:40"), None)

    def test_card_reassignment_is_unique_and_assignment_is_idempotent(self):
        self.store.add_contact(PERSON, "Grandma", receive_after=0)
        self.store.add_contact(GROUP, "Family", receive_after=0)
        self.store.assign_card(PERSON, CARD_ONE)
        revision = self.store.load()["revision"]

        self.store.assign_card(PERSON, "04-a1-00-ff")
        self.assertEqual(self.store.load()["revision"], revision)

        self.store.assign_card(GROUP, CARD_ONE)
        self.assertEqual(self.store.load()["revision"], revision + 1)
        self.assertEqual(self.store.contact(PERSON)["card_uids"], [])
        self.assertEqual(self.store.contact(GROUP)["card_uids"], [CARD_ONE])
        self.assertEqual(self.store.resolve_card(CARD_ONE)["jid"], GROUP)

    def test_enrollment_creates_contact_and_assigns_card_in_one_revision(self):
        result = self.store.enroll_card(PERSON, CARD_ONE, label="Grandma")

        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["contact_count"], 1)
        self.assertEqual(result["contact"]["jid"], PERSON)
        self.assertEqual(self.store.resolve_card(CARD_ONE)["jid"], PERSON)
        self.assertEqual(self.store.load()["revision"], 1)

    def test_card_assignment_requires_an_existing_contact(self):
        with self.assertRaises(ContactError):
            self.store.assign_card(PERSON, CARD_ONE)
        self.assertFalse(self.path.exists())

    def test_deletions_remove_nested_cards_and_no_ops_do_not_increment(self):
        self.store.add_contact(PERSON, "Grandma", receive_after=0)
        self.store.assign_card(PERSON, CARD_ONE)
        self.store.assign_card(PERSON, CARD_TWO)
        self.assertTrue(self.store.remove_card(CARD_ONE))
        self.assertIsNone(self.store.resolve_card(CARD_ONE))

        self.assertTrue(self.store.remove_contact(PERSON))
        revision = self.store.load()["revision"]
        self.assertIsNone(self.store.resolve_card(CARD_TWO))
        self.assertFalse(self.store.remove_contact(PERSON))
        self.assertFalse(self.store.remove_card(CARD_ONE))
        self.assertEqual(self.store.load()["revision"], revision)

    def test_contacts_and_listeners_are_isolated(self):
        self.store.add_contact(PERSON, "Grandma", receive_after=0)
        self.store.upsert_listener("15550001@s.whatsapp.net", "Mom")

        self.store.remove_contact(PERSON)
        self.assertIn("15550001@s.whatsapp.net", self.store.listener_profiles())
        self.store.remove_listener("15550001:4@s.whatsapp.net")
        self.assertEqual(self.store.listener_profiles(), {})
        self.assertEqual(self.store.allowed_jids(), ())

    def test_clips_must_be_wav_files_lexically_below_asset_root(self):
        contact = self.store.add_contact(
            PERSON,
            "Grandma",
            card_clip="/var/lib/messagebox/assets/cards/../grandma.wav",
            receive_after=0,
        )
        self.assertEqual(
            contact["card_clip"], "/var/lib/messagebox/assets/grandma.wav"
        )
        self.store.upsert_listener("15550001@s.whatsapp.net", "Mom", listened_clip="")

        for clip in (
            "relative.wav",
            "/var/lib/messagebox/assets/../private.wav",
            "/var/lib/messagebox/assets/not-a-wave.mp3",
            "/var/lib/messagebox/assets/fake.wav/child",
        ):
            with self.subTest(clip=clip):
                with self.assertRaises(ContactError):
                    ContactStore(Path(self.directory.name) / "other.json").add_contact(
                        GROUP, "Invalid", card_clip=clip, receive_after=0
                    )
                with self.assertRaises(ContactError):
                    self.store.upsert_listener(
                        "15550002@s.whatsapp.net", "Dad", listened_clip=clip
                    )

    def test_receive_after_and_text_bounds_are_enforced(self):
        for value in (-1, float("inf"), float("nan"), True, "0"):
            with self.subTest(value=value):
                with self.assertRaises(ContactError):
                    ContactStore(Path(self.directory.name) / "other.json").add_contact(
                        GROUP, "Family", receive_after=value
                    )
        for label in ("", "   ", "x" * 81):
            with self.subTest(label=label):
                with self.assertRaises(ContactError):
                    self.store.add_contact(GROUP, label, receive_after=0)
        with self.assertRaises(ContactError):
            self.store.upsert_listener("15550001@s.whatsapp.net", "x" * 81)

    def test_public_view_redacts_uids_and_reports_pairing(self):
        self.store.add_contact(PERSON, "Grandma", receive_after=0)
        self.store.add_contact(GROUP, "Family", receive_after=0)
        self.store.assign_card(PERSON, CARD_ONE)
        self.store.upsert_listener("15550001@s.whatsapp.net", "Mom")

        public = self.store.public_view()
        self.assertEqual(public["contacts"][PERSON]["card_count"], 1)
        self.assertTrue(public["contacts"][PERSON]["paired"])
        self.assertEqual(public["contacts"][GROUP]["card_count"], 0)
        self.assertFalse(public["contacts"][GROUP]["paired"])
        self.assertNotIn("card_uids", public["contacts"][PERSON])
        self.assertNotIn(CARD_ONE, json.dumps(public))
        self.assertEqual(
            public["listeners"]["15550001@s.whatsapp.net"]["name"], "Mom"
        )


class FakeEnrollmentStore:
    def __init__(self, path):
        self.path = path
        self.requests = []

    def begin(self, **request):
        self.requests.append(request)


class ContactCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ContactStore(
            Path(self.directory.name) / "contacts.json", clock=Clock()
        )
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def tearDown(self):
        self.directory.cleanup()

    def run_cli(self, arguments, **kwargs):
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(
            self.stderr
        ):
            return main(arguments, store=self.store, **kwargs)

    def test_add_without_card_mutates_immediately_and_explains_multi_contact_mode(self):
        self.assertEqual(self.run_cli(["add", "Grandma", PERSON, "--no-card"]), 0)
        self.assertIsNotNone(self.store.contact(PERSON))
        self.assertNotIn("multiple contacts", self.stdout.getvalue())

        self.assertEqual(self.run_cli(["add", "Family", GROUP, "--no-card"]), 0)
        self.assertIn("multiple contacts", self.stdout.getvalue())
        self.assertIn("requires a recently presented enrolled card", self.stdout.getvalue())

    def test_default_add_stages_enrollment_without_creating_contact(self):
        enrollment = FakeEnrollmentStore("ignored")
        checked = []

        result = self.run_cli(
            [
                "add",
                "Grandma",
                PERSON,
                "--card-clip",
                "/var/lib/messagebox/assets/grandma.wav",
            ],
            enrollment_factory=lambda path: enrollment,
            service_check=lambda: checked.append(True),
        )

        self.assertEqual(result, 0)
        self.assertEqual(checked, [True])
        self.assertIsNone(self.store.contact(PERSON))
        self.assertEqual(
            enrollment.requests,
            [
                {
                    "label": "Grandma",
                    "jid": PERSON,
                    "card_clip": "/var/lib/messagebox/assets/grandma.wav",
                    "create_contact": True,
                    "ttl_s": 300,
                }
            ],
        )
        rendered = self.stdout.getvalue()
        self.assertIn("armed for five minutes", rendered)
        self.assertIn("helper now exits", rendered)
        self.assertIn("NFC service continues", rendered)
        self.assertIn("beep confirms", rendered)

    def test_enroll_requires_contact_and_preserves_its_profile(self):
        enrollment = FakeEnrollmentStore("ignored")
        self.assertEqual(
            self.run_cli(
                ["enroll", PERSON],
                enrollment_factory=lambda path: enrollment,
                service_check=lambda: None,
            ),
            2,
        )
        self.store.add_contact(
            PERSON,
            "Grandma",
            card_clip="/var/lib/messagebox/assets/grandma.wav",
            receive_after=0,
        )

        self.assertEqual(
            self.run_cli(
                ["enroll", PERSON],
                enrollment_factory=lambda path: enrollment,
                service_check=lambda: None,
            ),
            0,
        )
        self.assertEqual(enrollment.requests[-1]["label"], "Grandma")
        self.assertFalse(enrollment.requests[-1]["create_contact"])
        self.assertEqual(
            enrollment.requests[-1]["card_clip"],
            "/var/lib/messagebox/assets/grandma.wav",
        )

    def test_list_count_and_remove_never_print_card_uids(self):
        self.store.add_contact(PERSON, "Grandma", receive_after=0)
        self.store.assign_card(PERSON, CARD_ONE)

        self.assertEqual(self.run_cli(["list"]), 0)
        rendered = self.stdout.getvalue()
        self.assertIn("Grandma", rendered)
        self.assertIn("1 card", rendered)
        self.assertNotIn(CARD_ONE, rendered)

        self.stdout = io.StringIO()
        self.assertEqual(self.run_cli(["count"]), 0)
        self.assertEqual(self.stdout.getvalue(), "1\n")

        self.stdout = io.StringIO()
        self.assertEqual(self.run_cli(["remove", PERSON]), 0)
        self.assertIsNone(self.store.resolve_card(CARD_ONE))
        self.assertNotIn(CARD_ONE, self.stdout.getvalue())

if __name__ == "__main__":
    unittest.main()
