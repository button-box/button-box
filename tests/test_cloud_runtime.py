import hashlib
import json
import subprocess
import tempfile
import sys
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

from messagebox.audio_requests import AudioRequests, preview_key, success_key
from messagebox.cloud_runtime import CloudRuntime, CloudRuntimeError
from messagebox.cloud_device import CloudAckGone, CloudDeviceClient, CloudDeviceError, CloudVoiceNotFound, atomic_json
from messagebox.guided_reply import OutboxStore, cloud_outbox_lock
from messagebox.played_history import list_played_history
from messagebox.nfc_state import CardReferenceStore, EnrollmentStore
from messagebox.settings import SettingsStore

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with mock.patch.dict(sys.modules, {"gpiozero": gpiozero}):
    from messagebox import button_send


NOW = 1_800_000_000
PERSON = {"id": "person1234567890123456", "wa_id": "12025550101", "display_name": "Family", "role": "family"}
OP = "operation1234567890123456"
MID = "message1234567890123456"


class FakeClient:
    def __init__(self):
        self.items = []
        self.acks = []
        self.audio = b"OggS family audio"
        self.deleted = False
        self.deliver = True
        self.send = True
        self.people = [PERSON.copy()]
        self.server_time = NOW
        self.retention_days = 30

    def heartbeat(self, state):
        return {"box_id": "box1234567890123456", "account_scope": "a" * 64, "server_time": self.server_time,
                "people": self.people, "default_recipient_id": self.people[0]["id"] if self.people else None,
                "entitlement": {"ingest": True, "deliver": self.deliver, "send": self.send,
                                "until": getattr(self, "heartbeat_until", NOW + 3600)}, "queue_hold": False,
                "retention_days": self.retention_days,
                "desired_revision": state["applied_revision"], "desired_settings": state["settings"]}

    def inbox(self, cursor):
        return {"box_id": "box1234567890123456", "server_time": self.server_time,
                "cursor": len(self.items), "items": self.items, "deleted": self.deleted}

    def media(self, url, *, limit):
        assert url.startswith("https://button.box/cloud-api/v1/device/media/")
        return self.audio

    def ack(self, ack):
        self.acks.append(ack)
        return {"operation_id": ack["operation_id"], "accepted": True}


def queued_audio(message, source, *, queue_dir):
    path = Path(queue_dir) / message["QueueFilename"]
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"version": 1, "chat": message["ChatJID"], "msgid": message["MsgID"],
            "sender_jid": message["SenderJID"], "media_type": "audio", **message["CloudMetadata"]}
    Path(str(path) + ".json").write_text(json.dumps(meta))
    path.write_bytes(source.read_bytes())


def write_pcm_wav(path, duration_seconds, sample_rate=8000):
    frames = round(duration_seconds * sample_rate)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\0\0" * frames)


class CloudRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = FakeClient()
        self.mono = [100.0]
        self.runtime = CloudRuntime(self.client, state_path=self.root / "runtime.json",
            contacts_path=self.root / "cloud-contacts.json", queue_dir=self.root / "queue", outbox_dir=self.root / "outbox",
            settings_path=self.root / "settings.json", clock=lambda: NOW,
            monotonic=lambda: self.mono[0], boot_id="test-boot",
            converter=queued_audio)
        self.ack_dir = self.root / "acks"
        patch = mock.patch("messagebox.cloud_runtime.ACK_DIR", self.ack_dir)
        patch.start()
        self.addCleanup(patch.stop)
        applied = self.root / "applied-settings.json"
        marker = mock.patch("messagebox.cloud_runtime.APPLIED_FILE", applied)
        marker.start()
        self.addCleanup(marker.stop)
        completed = mock.patch("messagebox.cloud_runtime.COMPLETED_DIR", self.root / "completed")
        completed.start()
        self.addCleanup(completed.stop)
        intents = mock.patch("messagebox.cloud_runtime.INTENT_DIR", self.root / "intents")
        intents.start()
        self.addCleanup(intents.stop)
        for name, path in [("NFC_ENROLLMENT_FILE", self.root / "nfc-enrollment.json"),
                           ("NFC_SELECTION_FILE", self.root / "nfc-selection.json"),
                           ("NFC_CARD_REFERENCES_FILE", self.root / "family-card-references.json")]:
            patch = mock.patch("messagebox.cloud_runtime." + name, path)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch("messagebox.cloud_runtime.EnrollmentStore",
                           side_effect=lambda path: EnrollmentStore(path, clock=self.runtime.clock))
        patch.start()
        self.addCleanup(patch.stop)
        document, _ = SettingsStore(self.root / "settings.json").load()
        applied.write_text(json.dumps({"revision": document["revision"], "settings": document}))

    def test_heartbeat_reports_random_per_card_references_without_uids(self):
        with mock.patch.object(self.client, "heartbeat", wraps=self.client.heartbeat) as send:
            self.runtime.heartbeat()
            self.assertNotIn("nfc_inventory", send.call_args.args[0])
            jid = PERSON["wa_id"] + "@s.whatsapp.net"
            self.runtime.contacts.enroll_card(jid, "04AABBCC", label="Family")
            self.runtime.heartbeat()
            inventory = send.call_args.args[0]["nfc_inventory"]
            self.assertEqual(len(inventory["cards"]), 1)
            self.assertEqual(inventory["cards"][0]["recipient_id"], PERSON["id"])
            card_ref = inventory["cards"][0]["card_ref"]
            self.assertRegex(card_ref, r"^[a-f0-9]{32}$")
            self.assertEqual(inventory["account_scope"], "a" * 64)
            self.assertNotIn("04AABBCC", json.dumps(inventory))
            self.assertNotIn(PERSON["wa_id"], json.dumps(inventory))
            self.runtime.heartbeat()
            self.assertEqual(send.call_args.args[0]["nfc_inventory"]["cards"][0]["card_ref"], card_ref)
            self.runtime._nfc({"operation_id": "remove-card1234567890", "kind": "nfc_unpair",
                "payload": {"card_ref": card_ref, "recipient_id": PERSON["id"], "inventory_revision": inventory["revision"]}})
            self.runtime.heartbeat()
            updated = send.call_args.args[0]["nfc_inventory"]
            self.assertEqual(updated["cards"], [])
            self.assertGreater(updated["revision"], inventory["revision"])
            self.runtime.flush_acks()
            self.assertEqual(self.client.acks[-1]["state"], "applied")

    def test_saved_card_unpair_removes_only_selected_card_and_replays_after_crash(self):
        self.runtime.heartbeat()
        jid = PERSON["wa_id"] + "@s.whatsapp.net"
        self.runtime.contacts.enroll_card(jid, "04AABBCC", label="Family")
        self.runtime.contacts.enroll_card(jid, "04DDEEFF", label="Family")
        inventory = self.runtime._nfc_inventory()
        self.assertEqual(len(inventory["cards"]), 2)
        references = CardReferenceStore(self.root / "family-card-references.json")
        selected_ref = inventory["cards"][0]["card_ref"]
        selected_uid = references.resolve("a" * 64, selected_ref)
        remaining_uid = next(references.resolve("a" * 64, card["card_ref"])
                             for card in inventory["cards"] if card["card_ref"] != selected_ref)
        item = {"operation_id": "remove-crash1234567890", "kind": "nfc_unpair",
            "payload": {"card_ref": selected_ref, "recipient_id": PERSON["id"], "inventory_revision": inventory["revision"]}}
        remove = self.runtime.contacts.remove_card
        attempts = [0]
        def remove_then_crash(uid, *, expected_jid=None):
            result = remove(uid, expected_jid=expected_jid)
            if attempts[0] == 0:
                attempts[0] += 1
                raise RuntimeError("simulated restart boundary")
            return result
        with mock.patch.object(self.runtime.contacts, "remove_card", side_effect=remove_then_crash), \
             mock.patch("messagebox.cloud_runtime.INTENT_DIR", self.root / "intents"):
            with self.assertRaisesRegex(RuntimeError, "restart boundary"):
                self.runtime._nfc(item)
            self.runtime._nfc(item)
        document = self.runtime.contacts.load()
        self.assertNotIn(selected_uid, document["contacts"][jid]["card_uids"])
        self.assertIn(remaining_uid, document["contacts"][jid]["card_uids"])
        self.runtime.flush_acks()
        self.assertEqual(self.client.acks[-1]["state"], "applied")

    def test_saved_card_reference_cannot_remove_a_card_reassigned_to_another_person(self):
        self.runtime.heartbeat()
        jid = PERSON["wa_id"] + "@s.whatsapp.net"
        self.runtime.contacts.enroll_card(jid, "04AABBCC", label="Family")
        inventory = self.runtime._nfc_inventory()
        ref = inventory["cards"][0]["card_ref"]
        self.runtime.contacts.add_contact("12025550199@s.whatsapp.net", "Other", card_clip="")
        self.runtime.contacts.assign_card("12025550199@s.whatsapp.net", "04AABBCC")
        item = {"operation_id": "remove-stale1234567890", "kind": "nfc_unpair",
            "payload": {"card_ref": ref, "recipient_id": PERSON["id"], "inventory_revision": inventory["revision"]}}
        with mock.patch("messagebox.cloud_runtime.INTENT_DIR", self.root / "intents"):
            with self.assertRaisesRegex(CloudRuntimeError, "changed"):
                self.runtime._nfc(item)
        contacts = self.runtime.contacts.load()["contacts"]
        self.assertIn("04:AA:BB:CC", contacts["12025550199@s.whatsapp.net"]["card_uids"])

    def audio_item(self):
        return {"operation_id": OP, "sequence": 1, "kind": "audio",
                "created_at": NOW - 10, "expires_at": NOW + 3600,
                "payload": {"message_id": MID,
                    "media_url": f"https://button.box/cloud-api/v1/device/media/{MID}",
                    "sha256": hashlib.sha256(self.client.audio).hexdigest(),
                    "content_type": "audio/ogg", "sender_id": PERSON["id"],
                    "sender_name": "Family", "reply_to": "wamid123", "expires_at": NOW + 3600,
                    "message_created_at": NOW - 10}}

    def test_local_housekeeping_flushes_playback_receipt_without_inbox_query(self):
        self.runtime.heartbeat()
        self.runtime._ack(OP, "played")
        with mock.patch.object(self.client, "inbox") as inbox:
            self.runtime._maintain_local()
        inbox.assert_not_called()
        self.assertEqual(self.client.acks[-1]["state"], "played")
        self.assertEqual(list(self.ack_dir.glob("*.json")), [])

    def test_local_housekeeping_acknowledges_completed_preview_without_inbox_query(self):
        self.runtime.heartbeat()
        self.runtime._command(self.preview(), NOW)
        with self.runtime.audio_requests.owner():
            request = self.runtime.audio_requests.claim_next("a" * 64)
            self.runtime.audio_requests.finish(request, "played")
        with mock.patch.object(self.client, "inbox") as inbox:
            self.runtime._maintain_local()
        inbox.assert_not_called()
        self.assertEqual(self.client.acks[-1]["state"], "applied")
        self.assertEqual(self.runtime.state["pending_previews"], {})

    def test_stale_authorization_preserves_local_receipt_until_refreshed(self):
        self.runtime.heartbeat()
        self.runtime._ack(OP, "played")
        self.mono[0] = 191
        with self.assertRaises(CloudRuntimeError):
            self.runtime._maintain_local()
        self.assertEqual(self.client.acks, [])
        self.assertEqual(len(list(self.ack_dir.glob("*.json"))), 1)
        self.runtime.heartbeat()
        self.runtime._maintain_local()
        self.assertEqual(self.client.acks[-1]["state"], "played")

    def test_authorized_audio_is_durable_once_and_deleted_only_by_command(self):
        self.runtime.heartbeat()
        self.client.items = [self.audio_item()]
        self.runtime.poll_once()
        queued = list((self.root / "queue").glob("*.wav"))
        self.assertEqual(len(queued), 1)
        metadata = json.loads(Path(str(queued[0]) + ".json").read_text())
        self.assertTrue(self.runtime.playable(metadata))
        self.assertEqual(self.client.acks[-1]["state"], "received")
        restarted = CloudRuntime(self.client, state_path=self.root / "runtime.json",
            contacts_path=self.root / "cloud-contacts.json", queue_dir=self.root / "queue", outbox_dir=self.root / "outbox",
            settings_path=self.root / "settings.json", clock=lambda: NOW, converter=queued_audio,
            monotonic=lambda: self.mono[0], boot_id="test-boot")
        restarted.poll_once()
        self.assertEqual(len(list((self.root / "queue").glob("*.wav"))), 1)
        self.client.items = [{"operation_id": "delete1234567890123456", "sequence": 2,
                              "kind": "delete_message", "created_at": NOW,
                              "expires_at": NOW + 90 * 86400,
                              "payload": {"message_id": MID}}, self.audio_item()]
        restarted.poll_once()
        self.assertFalse(queued[0].exists())
        self.assertFalse(restarted.playable(metadata))

    def hold_item(self, held, sequence=2):
        return {"operation_id": f"hold_operation_{sequence:016d}", "sequence": sequence,
                "kind": "queue_hold", "created_at": NOW, "expires_at": NOW + 60,
                "payload": {"held": held}}

    def restart(self):
        return CloudRuntime(self.client, state_path=self.root / "runtime.json",
            contacts_path=self.root / "cloud-contacts.json", queue_dir=self.root / "queue",
            outbox_dir=self.root / "outbox", settings_path=self.root / "settings.json",
            clock=lambda: NOW, converter=queued_audio, monotonic=lambda: self.mono[0],
            boot_id="test-boot")

    def test_held_audio_waits_for_delayed_resume_without_terminal_receipt(self):
        self.runtime.heartbeat()
        self.runtime.state["snapshot"]["queue_hold"] = True
        self.runtime._save()
        self.client.items = [self.audio_item()]
        with mock.patch.object(self.client, "media", wraps=self.client.media) as media:
            self.runtime.poll_once()
            self.runtime = self.restart()
            self.runtime.poll_once()
            self.assertEqual(self.client.acks, [])
            self.assertEqual(self.runtime.state["cursor"], 0)
            self.assertFalse(list((self.root / "queue").glob("*.wav")))
            self.client.items.append(self.hold_item(False))
            self.runtime.poll_once()
            self.assertFalse(self.runtime.state["snapshot"]["queue_hold"])
            self.assertEqual([ack["state"] for ack in self.client.acks], ["applied"])
            self.runtime.poll_once()
            self.runtime = self.restart()
            self.runtime.poll_once()
            media.assert_called_once()
        self.assertEqual(len(list((self.root / "queue").glob("*.wav"))), 1)
        self.assertEqual({ack["state"] for ack in self.client.acks if ack["operation_id"] == OP},
                         {"received"})

    def test_resume_interrupted_after_intent_recovers_pending_audio(self):
        self.runtime.heartbeat()
        self.runtime.state["snapshot"]["queue_hold"] = True
        self.runtime._save()
        self.client.items = [self.audio_item(), self.hold_item(False)]
        with mock.patch.object(self.runtime, "_save", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.runtime.poll_once()
        self.runtime = self.restart()
        self.assertTrue(self.runtime.state["snapshot"]["queue_hold"])
        self.runtime.poll_once()
        self.assertFalse(self.runtime.state["snapshot"]["queue_hold"])
        self.runtime.poll_once()
        self.assertEqual(len(list((self.root / "queue").glob("*.wav"))), 1)
        self.assertNotIn("rejected", [ack["state"] for ack in self.client.acks])

    def test_resume_retry_after_saved_effect_cannot_undo_newer_hold(self):
        self.runtime.heartbeat()
        resume = self.hold_item(False)
        with mock.patch.object(self.runtime, "_ack", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.runtime._command(resume, NOW)
        self.runtime._command(self.hold_item(True, sequence=3), NOW)
        self.runtime = self.restart()
        self.runtime._command(resume, NOW)
        self.runtime.flush_acks()
        self.assertTrue(self.runtime.state["snapshot"]["queue_hold"])
        self.assertEqual(self.runtime.state["queue_hold_sequence"], 3)
        self.assertEqual({ack["state"] for ack in self.client.acks}, {"applied"})

    def test_temporary_hold_does_not_bypass_revoked_delivery_or_sender(self):
        for revoked in ("delivery", "sender"):
            with self.subTest(revoked=revoked):
                self.client.deliver = revoked != "delivery"
                self.client.people = [] if revoked == "sender" else [PERSON.copy()]
                self.runtime.heartbeat()
                self.runtime.state["snapshot"]["queue_hold"] = True
                self.runtime._save()
                item = self.audio_item()
                item["operation_id"] += revoked
                self.client.items = [item]
                with mock.patch.object(self.client, "media") as media:
                    self.runtime.poll_once()
                    media.assert_not_called()
                self.assertEqual(self.client.acks[-1]["state"], "rejected")
        self.assertFalse(list((self.root / "queue").glob("*.wav")))

    def test_delete_still_expires_older_pending_audio_while_held(self):
        self.runtime.heartbeat()
        self.runtime.state["snapshot"]["queue_hold"] = True
        self.runtime._save()
        deletion = {"operation_id": "delete_operation_123456789", "sequence": 2,
                    "kind": "delete_message", "created_at": NOW, "expires_at": NOW + 60,
                    "payload": {"message_id": MID}}
        self.client.items = [self.audio_item(), deletion]
        with mock.patch.object(self.client, "media") as media:
            self.runtime.poll_once()
            self.runtime = self.restart()
            self.runtime.poll_once()
            media.assert_not_called()
        self.assertIn(MID, self.runtime.state["deleted"])
        self.assertEqual({ack["state"] for ack in self.client.acks if ack["operation_id"] == OP},
                         {"expired"})

    def test_new_requeue_operation_returns_played_message_to_queue_once(self):
        self.runtime.heartbeat()
        self.client.items = [self.audio_item()]
        self.runtime.poll_once()
        queued = next((self.root / "queue").glob("*.wav"))
        played = self.root / "queue" / ".played"
        played.mkdir()
        archived = played / queued.name
        queued.rename(archived)
        Path(str(queued) + ".json").rename(Path(str(archived) + ".json"))
        requeue = self.audio_item()
        requeue.update(operation_id="new-operation1234567890", sequence=2, created_at=NOW + 1)
        self.client.items = [requeue]
        self.runtime.poll_once()
        new_files = list((self.root / "queue").glob("*.wav"))
        self.assertEqual(len(new_files), 1)
        self.assertNotEqual(new_files[0].name, archived.name)
        self.assertTrue(archived.exists())
        metadata = json.loads(Path(str(new_files[0]) + ".json").read_text())
        self.assertEqual(metadata["cloud_operation_id"], requeue["operation_id"])
        self.assertEqual(metadata["expires_at"], NOW + 3600)
        self.runtime.poll_once()
        self.assertEqual(len(list((self.root / "queue").glob("*.wav"))), 1)

    def test_revoked_family_and_paused_entitlement_block_playback_and_send(self):
        self.runtime.heartbeat()
        self.client.items = [self.audio_item()]
        self.runtime.poll_once()
        metadata = json.loads(next((self.root / "queue").glob("*.wav.json")).read_text())
        self.client.people = []
        self.runtime.heartbeat()
        self.assertFalse(self.runtime.playable(metadata))
        with self.assertRaises(CloudRuntimeError):
            self.runtime.recipient_id(PERSON["wa_id"] + "@s.whatsapp.net")
        self.assertTrue(next((self.root / "queue").glob("*.wav")).exists())
        self.client.deliver = False
        self.runtime.heartbeat()
        self.assertFalse(self.runtime.playable(metadata))

    def test_settings_revision_replay_and_local_conflict(self):
        self.runtime.heartbeat()
        document, _ = SettingsStore(self.root / "settings.json").load()
        candidate = {**document, "revision": 1, "master_volume_percent": 40, "swoosh_sound_enabled": False}
        item = {"operation_id": OP, "sequence": 1, "kind": "settings", "created_at": NOW,
                "expires_at": NOW + 100, "payload": {"settings": candidate,
                "expected_revision": 0, "desired_revision": 1}}
        self.client.items = [item]
        self.runtime.poll_once()
        self.assertEqual(SettingsStore(self.root / "settings.json").load()[0]["master_volume_percent"], 40)
        self.assertFalse(SettingsStore(self.root / "settings.json").load()[0]["swoosh_sound_enabled"])
        self.assertEqual(self.client.acks[-1]["state"], "received")
        (self.root / "applied-settings.json").write_text(json.dumps({"revision": 1, "settings": candidate}))
        self.client.items = []
        self.runtime.poll_once()
        self.assertEqual(self.client.acks[-1]["applied_revision"], 1)
        self.client.items = [item]
        self.runtime.poll_once()
        self.assertEqual(SettingsStore(self.root / "settings.json").load()[0]["revision"], 1)
        item["operation_id"] = "other1234567890123456"
        item["payload"] = {"settings": {**candidate, "master_volume_percent": 60},
                           "expected_revision": 0, "desired_revision": 1}
        self.runtime.poll_once()
        self.assertEqual(self.client.acks[-1]["state"], "rejected")
        self.assertEqual(SettingsStore(self.root / "settings.json").load()[0]["master_volume_percent"], 40)

    def test_pre_swoosh_queued_settings_command_keeps_existing_send_cue_default(self):
        self.runtime.heartbeat()
        old, _warning = self.runtime.settings.load()
        old.pop("swoosh_sound_enabled")
        self.runtime.settings.path.write_text(json.dumps(old))
        candidate = {**old, "revision": 1, "master_volume_percent": 40}
        item = {"operation_id": OP, "sequence": 1, "kind": "settings", "created_at": NOW,
                "expires_at": NOW + 100, "payload": {"settings": candidate,
                "expected_revision": 0, "desired_revision": 1}}
        self.client.items = [item]
        self.runtime.poll_once()
        saved, warning = self.runtime.settings.load()
        self.assertFalse(warning)
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["master_volume_percent"], 40)
        self.assertTrue(saved["swoosh_sound_enabled"])
        self.assertEqual(self.client.acks[-1]["state"], "received")
        (self.root / "applied-settings.json").write_text(json.dumps({"revision": 1, "settings": saved}))
        self.runtime._finish_settings()
        self.runtime.flush_acks()
        self.assertEqual(self.client.acks[-1]["state"], "applied")

    def test_settings_recovers_skipped_generations_without_claiming_early_success(self):
        self.runtime.heartbeat()
        document, _ = self.runtime.settings.load()
        candidate = {**document, "revision": 4, "master_volume_percent": 65}
        item = {"operation_id": OP, "sequence": 1, "kind": "settings", "created_at": NOW,
                "expires_at": NOW + 15, "payload": {"settings": candidate,
                "expected_revision": 0, "desired_revision": 4}}
        self.client.items = [item]
        self.runtime.poll_once()
        self.assertEqual(self.runtime.settings.load()[0], candidate)
        self.assertEqual(self.client.acks[-1]["state"], "received")
        # Restart/replay before the button process confirms must not increment again.
        self.runtime.poll_once()
        self.assertEqual(self.runtime.settings.load()[0]["revision"], 4)
        (self.root / "applied-settings.json").write_text(json.dumps({"revision": 4, "settings": candidate}))
        self.runtime.poll_once()
        self.assertEqual(self.client.acks[-1]["state"], "applied")
        self.assertEqual(self.client.acks[-1]["applied_revision"], 4)
        stale = {**candidate, "revision": 5, "master_volume_percent": 90}
        item = {**item, "operation_id": "other1234567890123456", "sequence": 2,
                "payload": {"settings": stale, "expected_revision": 0, "desired_revision": 5}}
        self.client.items = [item]
        self.runtime.poll_once()
        self.assertEqual(self.client.acks[-1]["state"], "rejected")
        self.assertEqual(self.runtime.settings.load()[0], candidate)

    def test_expiry_removes_only_expired_cloud_audio(self):
        self.runtime.heartbeat()
        self.client.items = [self.audio_item()]
        self.runtime.poll_once()
        path = next((self.root / "queue").glob("*.wav"))
        local = self.root / "queue" / "local.wav"
        local.write_bytes(b"local")
        self.runtime.clock = lambda: NOW + 3601
        self.client.server_time = NOW + 3601
        self.runtime.heartbeat()
        self.runtime.expire_local()
        self.assertFalse(path.exists())
        self.assertTrue(local.exists())

    def test_authorization_expires_by_monotonic_time_and_boot_not_wall_clock(self):
        self.runtime.heartbeat()
        self.client.items = [self.audio_item()]
        self.runtime.poll_once()
        metadata = json.loads(next((self.root / "queue").glob("*.wav.json")).read_text())
        self.assertTrue(self.runtime.playable(metadata))
        self.mono[0] = 191
        self.assertFalse(self.runtime.playable(metadata))
        self.mono[0] = 101
        self.runtime.boot_id = "next-boot"
        self.assertFalse(self.runtime.playable(metadata))

    def test_payment_suspension_allows_only_fresh_accepted_audio_not_send(self):
        self.client.heartbeat_until = NOW - 1
        self.runtime.heartbeat()
        self.client.items = [self.audio_item()]
        self.runtime.poll_once()
        metadata = json.loads(next((self.root / "queue").glob("*.wav.json")).read_text())
        self.assertTrue(self.runtime.playable(metadata))
        with self.assertRaises(CloudRuntimeError):
            self.runtime.recipient_id(PERSON["wa_id"] + "@s.whatsapp.net")
        self.mono[0] = 191
        self.assertFalse(self.runtime.playable(metadata))

    def test_forward_clock_cannot_prune_cloud_history_or_expire_local(self):
        self.runtime.heartbeat()
        self.client.items = [self.audio_item()]
        self.runtime.poll_once()
        queue = self.root / "queue"
        wav = next(queue.glob("*.wav"))
        history = queue / ".played"
        history.mkdir()
        moved = history / wav.name
        wav.replace(moved)
        Path(str(wav) + ".json").replace(Path(str(moved) + ".json"))
        list_played_history(queue, now=NOW + 365 * 86400)
        self.assertTrue(moved.exists())
        self.runtime.clock = lambda: NOW + 365 * 86400
        with self.assertRaises(CloudRuntimeError):
            self.runtime.expire_local()
        self.assertTrue(moved.exists())

    def test_shortened_retention_payload_uses_newer_message_expiry(self):
        self.runtime.heartbeat()
        item = self.audio_item()
        item["payload"]["expires_at"] = NOW + 1800
        self.client.items = [item]
        self.runtime.poll_once()
        metadata = json.loads(next((self.root / "queue").glob("*.wav.json")).read_text())
        self.assertEqual(metadata["expires_at"], NOW + 1800)

    def test_heartbeat_retention_shortening_expires_already_queued_message(self):
        self.runtime.heartbeat()
        item = self.audio_item()
        item["payload"]["expires_at"] = NOW + 30 * 86400
        item["expires_at"] = NOW + 30 * 86400
        self.client.items = [item]
        self.runtime.poll_once()
        wav = next((self.root / "queue").glob("*.wav"))
        metadata = json.loads(Path(str(wav) + ".json").read_text())
        self.assertTrue(self.runtime.playable(metadata))
        self.client.retention_days = 7
        self.client.server_time = NOW + 8 * 86400
        self.runtime.clock = lambda: NOW + 8 * 86400
        self.runtime.heartbeat()
        self.assertFalse(self.runtime.playable(metadata))
        self.runtime.expire_local()
        self.assertFalse(wav.exists())

    def test_played_ack_cannot_be_downgraded_to_received(self):
        self.runtime._ack(OP, "received")
        self.runtime._ack(OP, "played")
        self.runtime._ack(OP, "received")
        ack = json.loads(next(self.ack_dir.glob("*.json")).read_text())
        self.assertEqual(ack["state"], "played")

    def test_applied_effect_is_not_repeated_after_ack_failure(self):
        self.runtime.heartbeat()
        item = {"operation_id": OP, "sequence": 1, "kind": "queue_hold",
                "created_at": NOW, "expires_at": NOW + 60, "payload": {"held": True}}
        self.client.items = [item]
        original = self.client.ack
        attempts = [0]
        def flaky(ack):
            attempts[0] += 1
            if attempts[0] == 1:
                raise CloudDeviceError("offline")
            return original(ack)
        self.client.ack = flaky
        with self.assertRaises(CloudDeviceError):
            self.runtime.poll_once()
        self.assertTrue(self.runtime.state["snapshot"]["queue_hold"])
        self.runtime.poll_once()
        self.assertEqual(self.client.acks[-1]["state"], "applied")

    def test_heartbeat_requires_scope_and_records_transfer(self):
        original = self.client.heartbeat
        self.runtime.heartbeat()
        self.assertEqual(self.runtime.state["snapshot"]["account_scope"], "a" * 64)
        before = self.runtime.state_path.read_bytes()
        for invalid in (None, "bad", 123):
            self.client.heartbeat = lambda state: {**original(state), "account_scope": invalid}
            with self.assertRaises(CloudRuntimeError):
                self.runtime.heartbeat()
            self.assertEqual(self.runtime.state_path.read_bytes(), before)
        self.client.heartbeat = lambda state: {**original(state), "account_scope": "b" * 64}
        self.runtime.heartbeat()
        self.assertEqual(self.runtime.state["snapshot"]["account_scope"], "b" * 64)

    def test_authoritative_outbox_expiry_only_and_unknown_source_retained(self):
        self.runtime.heartbeat()
        root = self.root / "outbox"
        known = root / "known.job"
        unknown = root / "unknown.job"
        for path, expiry in ((known, NOW + 30), (unknown, None)):
            path.mkdir(parents=True)
            (path / "audio.wav").write_bytes(b"private")
            atomic_json(path / "job.json", {"transport": "cloud", "cloud_message_id": path.name,
                                                 "expires_at": expiry})
        self.client.server_time = NOW + 31
        self.runtime.clock = lambda: NOW + 31
        self.runtime.heartbeat()
        self.runtime.expire_local()
        self.assertFalse(known.exists())
        self.assertTrue((unknown / "audio.wav").exists())

    def test_deletion_only_pages_continue_after_restart(self):
        self.runtime.heartbeat()
        self.client.deleted = True
        all_items = [{"operation_id": f"delete_operation_{index:04d}", "sequence": index + 1,
                      "kind": "delete_message", "created_at": NOW,
                      "expires_at": NOW + 90 * 86400,
                      "payload": {"message_id": f"deleted_message_{index:04d}"}}
                     for index in range(101)]
        self.client.inbox = lambda cursor: {"server_time": NOW, "deleted": True,
                                            "items": all_items[cursor:cursor + 100]}
        self.runtime.poll_once()
        self.assertEqual(self.runtime.state["cursor"], 100)
        restarted = CloudRuntime(self.client, state_path=self.root / "runtime.json",
            contacts_path=self.root / "cloud-contacts.json", queue_dir=self.root / "queue",
            outbox_dir=self.root / "outbox", settings_path=self.root / "settings.json",
            clock=lambda: NOW, monotonic=lambda: self.mono[0], boot_id="test-boot",
            converter=queued_audio)
        restarted.heartbeat()
        restarted.poll_once()
        self.assertEqual(restarted.state["cursor"], 101)
        self.assertEqual(len(restarted.state["deleted"]), 101)

    def test_stale_missing_ack_is_quarantined_without_blocking_inbox(self):
        self.runtime.heartbeat()
        self.runtime._ack("old_operation_123456789", "applied")
        self.client.ack = lambda ack: (_ for _ in ()).throw(CloudAckGone("gone")) if ack["operation_id"].startswith("old_") else {"operation_id": ack["operation_id"], "accepted": True}
        self.client.items = [{"operation_id": OP, "sequence": 1, "kind": "queue_hold",
                              "created_at": NOW, "expires_at": NOW + 60,
                              "payload": {"held": True}}]
        self.runtime.poll_once()
        self.assertTrue(self.runtime.state["snapshot"]["queue_hold"])
        self.assertEqual(len(list((self.ack_dir / "gone").glob("*.json"))), 1)

    def test_nfc_unpair_crash_intent_never_retargets_new_card(self):
        self.runtime.heartbeat()
        item = {"operation_id": OP, "sequence": 1, "kind": "nfc_unpair",
                "created_at": NOW, "expires_at": NOW + 60, "payload": {}}
        router = mock.Mock()
        router.selection.load.side_effect = [{"uid": "A1B2C3D4"}, {"uid": "E5F6A7B8"}]
        self.runtime.contacts.remove_card = mock.Mock(side_effect=[RuntimeError("crash"), True])
        with mock.patch("messagebox.cloud_runtime.NfcRouter", return_value=router), \
             mock.patch("messagebox.cloud_runtime.INTENT_DIR", self.root / "intents"):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.runtime._nfc(item)
            self.runtime._nfc(item)
        self.assertEqual(self.runtime.contacts.remove_card.call_args_list,
                         [mock.call("A1B2C3D4", expected_jid=None), mock.call("A1B2C3D4", expected_jid=None)])
        self.assertEqual(router.selection.load.call_count, 1)

    def test_nfc_cancel_crash_intent_never_cancels_new_enrollment(self):
        self.runtime.heartbeat()
        item = {"operation_id": OP, "sequence": 1, "kind": "nfc_cancel",
                "created_at": NOW, "expires_at": NOW + 60, "payload": {}}
        router = mock.Mock()
        router.enrollment.active.side_effect = [{"request_id": "first-enrollment"},
                                                 {"request_id": "later-enrollment"}]
        router.cancel_enrollment.side_effect = [RuntimeError("crash"), True]
        with mock.patch("messagebox.cloud_runtime.NfcRouter", return_value=router), \
             mock.patch("messagebox.cloud_runtime.INTENT_DIR", self.root / "intents"):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.runtime._nfc(item)
            self.runtime._nfc(item)
        self.assertEqual(router.cancel_enrollment.call_args_list,
                         [mock.call("first-enrollment"), mock.call("first-enrollment")])
        self.assertEqual(router.enrollment.active.call_count, 1)

    def nfc_item(self, operation_id=OP, kind="nfc_enroll"):
        return {"operation_id": operation_id, "sequence": 1, "kind": kind,
                "created_at": NOW, "expires_at": NOW + 120,
                "payload": {"recipient_id": PERSON["id"]} if kind == "nfc_enroll" else {}}

    def test_nfc_canceled_legacy_request_finishes_after_restart_and_ack_retry(self):
        self.runtime.heartbeat()
        self.runtime._nfc(self.nfc_item())
        self.runtime.flush_acks()
        self.runtime._nfc(self.nfc_item("cancel-operation", "nfc_cancel"))
        self.assertFalse((self.root / "nfc-enrollment.json").exists())
        self.assertIsInstance(self.runtime.state["pending_nfc"][OP], str)
        contacts = self.runtime.contacts.path.read_bytes()
        restarted = CloudRuntime(self.client, state_path=self.runtime.state_path,
            contacts_path=self.runtime.contacts.path, queue_dir=self.runtime.queue_dir,
            outbox_dir=self.runtime.outbox_dir, settings_path=self.runtime.settings.path,
            clock=self.runtime.clock, boot_id="test-boot")
        restarted._finish_nfc()
        self.assertEqual(restarted.state["pending_nfc"], {})
        self.assertEqual(self.runtime.contacts.path.read_bytes(), contacts)
        self.assertEqual(json.loads(restarted._completed_path(OP).read_text()),
                         {"operation_id": OP, "state": "rejected", "error_code": "nfc_enrollment_ended"})
        with mock.patch.object(self.client, "ack", side_effect=CloudDeviceError("unavailable")):
            with self.assertRaises(CloudDeviceError):
                restarted.flush_acks()
        restarted.flush_acks()
        restarted._command(self.nfc_item(), NOW)
        restarted.flush_acks()
        self.assertFalse((self.root / "nfc-enrollment.json").exists())
        self.assertEqual(self.client.acks[-1]["state"], "rejected")

    def test_nfc_expired_or_replaced_request_finishes_without_retargeting(self):
        for ended in ("expired", "replaced", "missing"):
            with self.subTest(ended=ended):
                self.runtime.clock = lambda: NOW
                self.runtime.heartbeat()
                operation = OP + ended
                self.runtime._nfc(self.nfc_item(operation))
                store = EnrollmentStore(self.root / "nfc-enrollment.json", clock=self.runtime.clock)
                if ended == "expired":
                    self.runtime.clock = lambda: NOW + 120
                else:
                    store.cancel(self.runtime.state["pending_nfc"][operation])
                replacement = store.begin(label="New request", jid=PERSON["wa_id"] + "@s.whatsapp.net") if ended == "replaced" else None
                self.runtime._finish_nfc()
                self.assertNotIn(operation, self.runtime.state["pending_nfc"])
                self.assertEqual(json.loads(self.runtime._completed_path(operation).read_text())["state"], "rejected")
                if replacement:
                    self.assertEqual(store.active()["request_id"], replacement["request_id"])
                    store.cancel(replacement["request_id"])
                self.runtime.clock = lambda: NOW

    def test_nfc_pending_and_claimed_requests_wait_for_verified_success(self):
        self.runtime.heartbeat()
        self.runtime._nfc(self.nfc_item())
        store = EnrollmentStore(self.root / "nfc-enrollment.json", clock=self.runtime.clock)
        self.runtime._finish_nfc()
        self.assertIn(OP, self.runtime.state["pending_nfc"])
        request = store.claim("A1B2C3D4")
        self.runtime.clock = lambda: NOW + 120
        self.runtime._finish_nfc()
        self.assertIn(OP, self.runtime.state["pending_nfc"])
        self.assertFalse(self.runtime._completed_path(OP).exists())
        # Commit the matching device receipt after the active snapshot was read.
        original_active = store.active
        def finish_during_snapshot():
            active = original_active()
            with store.locked():
                store._record_success_locked(request)
                store._remove_locked()
            return active
        with mock.patch("messagebox.cloud_runtime.EnrollmentStore", return_value=store), \
             mock.patch.object(store, "active", side_effect=finish_during_snapshot):
            self.runtime._finish_nfc()
        self.assertNotIn(OP, self.runtime.state["pending_nfc"])
        self.assertEqual(json.loads(self.runtime._completed_path(OP).read_text())["state"], "applied")

    def test_nfc_restart_replays_durable_terminal_ack_before_clearing_residual(self):
        self.runtime.heartbeat()
        self.runtime.state["pending_nfc"][OP] = "legacy-request"
        self.runtime._save()
        # A crash after the terminal receipt but before queueing its ACK must recover it.
        atomic_json(self.runtime._completed_path(OP), {"operation_id": OP, "state": "applied"})
        restarted = CloudRuntime(self.client, state_path=self.runtime.state_path,
            contacts_path=self.runtime.contacts.path, queue_dir=self.runtime.queue_dir,
            outbox_dir=self.runtime.outbox_dir, settings_path=self.runtime.settings.path,
            clock=self.runtime.clock, boot_id="test-boot")
        restarted._finish_nfc()
        self.assertEqual(restarted.state["pending_nfc"], {})
        restarted.flush_acks()
        self.assertEqual(self.client.acks[-1], {"operation_id": OP, "state": "applied"})

    def preview(self, ringtone="gentle_music_box", operation_id=OP):
        return {"operation_id": operation_id, "sequence": 1, "kind": "preview_ringtone",
                "created_at": NOW, "expires_at": NOW + 60,
                "payload": {"ringtone_id": ringtone}}

    def audio_owner(self):
        return mock.patch.multiple(button_send, cloud_audio_requests=self.runtime.audio_requests,
            _recording=False, _guided_active=False,
            button=types.SimpleNamespace(is_pressed=False), create=True)

    def test_preview_waits_for_idle_button_owner_and_acknowledges_only_playback(self):
        self.runtime.heartbeat()
        item = self.preview()
        with mock.patch.object(button_send.subprocess, "run") as poller_play:
            self.runtime._command(item, NOW)
            self.runtime._command(item, NOW)
            self.runtime._finish_previews()
        poller_play.assert_not_called()
        self.assertEqual(json.loads(next(self.ack_dir.glob("*.json")).read_text())["state"], "received")
        ringtone_dir = self.root / "ringtones"
        ringtone_dir.mkdir()
        write_pcm_wav(ringtone_dir / "ring1.wav", 1)
        with self.audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send.cloud_runtime, "RINGTONE_DIR", ringtone_dir), \
             mock.patch.object(button_send, "play_idle_sound", return_value=True) as play:
            for flag in ("_recording", "_guided_active"):
                with mock.patch.object(button_send, flag, True):
                    self.assertFalse(button_send.maybe_play_cloud_sound())
            with mock.patch.object(button_send.button, "is_pressed", True):
                self.assertFalse(button_send.maybe_play_cloud_sound())
            play.assert_not_called()
            self.assertTrue(button_send.maybe_play_cloud_sound())
            self.assertFalse(button_send.maybe_play_cloud_sound())
        play.assert_called_once_with(ringtone_dir / "ring1.wav", 6.0)
        self.runtime._finish_previews()
        self.assertEqual(json.loads(next(self.ack_dir.glob("*.json")).read_text())["state"], "applied")
        self.assertEqual(self.runtime.state["pending_previews"], {})

    def test_preview_claim_before_crash_suppresses_replay_and_reports_unknown(self):
        self.runtime.heartbeat()
        self.runtime._command(self.preview(), NOW)
        with self.runtime.audio_requests.owner() as acquired:
            self.assertTrue(acquired)
            self.assertIsNotNone(self.runtime.audio_requests.claim_next("a" * 64))
            self.runtime._finish_previews()
            self.assertIn(OP, self.runtime.state["pending_previews"])
        restarted = CloudRuntime(self.client, state_path=self.runtime.state_path,
            contacts_path=self.runtime.contacts.path, queue_dir=self.runtime.queue_dir,
            outbox_dir=self.runtime.outbox_dir, settings_path=self.runtime.settings.path,
            clock=self.runtime.clock, boot_id="test-boot")
        restarted._finish_previews()
        restarted._command(self.preview(), NOW)
        self.assertEqual(json.loads(next(self.ack_dir.glob("*.json")).read_text())["error_code"],
                         "preview_outcome_unknown")
        with restarted.audio_requests.owner():
            self.assertIsNone(restarted.audio_requests.claim_next("a" * 64))

    def test_legacy_preview_crash_intent_is_never_replayed(self):
        self.runtime.heartbeat()
        atomic_json(self.runtime._intent_path(OP), {"operation_id": OP, "ringtone_id": "ding_dong"})
        self.runtime._command(self.preview(), NOW)
        self.assertEqual(json.loads(next(self.ack_dir.glob("*.json")).read_text())["error_code"],
                         "preview_outcome_unknown")
        self.assertIsNone(self.runtime.audio_requests.outcome(preview_key(OP)))

    def test_preview_long_wav_timeout_and_owner_speaker_are_preserved(self):
        self.runtime.heartbeat()
        ringtone_dir = self.root / "ringtones"
        ringtone_dir.mkdir()
        write_pcm_wav(ringtone_dir / "ring3.wav", 19.8)
        self.runtime._command(self.preview("ding_dong"), NOW)
        process = mock.Mock()
        process.poll.return_value = 0
        with self.audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send.cloud_runtime, "RINGTONE_DIR", ringtone_dir), \
             mock.patch.object(button_send, "SPK_DEV", "plughw:CARD=ExampleSpeaker,DEV=2"), \
             mock.patch.object(button_send.subprocess, "Popen", return_value=process) as play:
            self.assertTrue(button_send.maybe_play_cloud_sound())
        play.assert_called_once_with(["aplay", "-q", "-D", "plughw:CARD=ExampleSpeaker,DEV=2",
                                     str(ringtone_dir / "ring3.wav")])
        from messagebox.cloud_runtime import _ringtone_preview_timeout
        self.assertAlmostEqual(_ringtone_preview_timeout(ringtone_dir / "ring3.wav"), 24.8)

    def test_expired_preview_never_plays_and_is_acknowledged_expired(self):
        self.runtime.heartbeat()
        self.runtime._command(self.preview(), NOW)
        self.runtime.audio_requests.clock = lambda: NOW + 31
        self.mono[0] += 31
        self.runtime._finish_previews()
        self.assertEqual(json.loads(next(self.ack_dir.glob("*.json")).read_text())["state"], "expired")
        with self.runtime.audio_requests.owner():
            self.assertIsNone(self.runtime.audio_requests.claim_next("a" * 64))

    def test_preview_failure_does_not_stop_later_commands_and_never_replays(self):
        self.runtime.heartbeat()
        ringtone_dir = self.root / "ringtones"
        ringtone_dir.mkdir()
        write_pcm_wav(ringtone_dir / "ring3.wav", 19.8)
        self.client.items = [self.preview("ding_dong"),
            {"operation_id": "hold_operation_123456789", "sequence": 2,
             "kind": "queue_hold", "created_at": NOW, "expires_at": NOW + 60,
             "payload": {"held": True}}]
        self.runtime.poll_once()
        with self.audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send.cloud_runtime, "RINGTONE_DIR", ringtone_dir), \
             mock.patch.object(button_send, "log_event"), \
             mock.patch.object(button_send, "play_idle_sound", side_effect=subprocess.TimeoutExpired("aplay", 24.8)) as play:
            self.assertFalse(button_send.maybe_play_cloud_sound())
            self.assertFalse(button_send.maybe_play_cloud_sound())
        self.assertEqual(play.call_count, 1)
        self.runtime._finish_previews()
        self.runtime.flush_acks()
        states = {ack["operation_id"]: ack["state"] for ack in self.client.acks}
        self.assertEqual(states[OP], "rejected")
        self.assertEqual(states["hold_operation_123456789"], "applied")
        self.assertEqual(self.runtime.state["cursor"], 2)

    def test_delayed_acceptance_queues_durable_success_once_across_restart(self):
        self.runtime.heartbeat()
        job = self.runtime.outbox_dir / "original_key_123456.job"
        job.mkdir(parents=True)
        for state in ("accepted", "delivered", "read"):
            with self.subTest(state=state):
                metadata = {"transport": "cloud", "message_id": job.stem,
                    "account_scope": "a" * 64, "state": "cloud_retained", "cloud_state": "queued",
                    "cloud_status_checked_at": NOW - 5}
                atomic_json(job / "job.json", metadata)
                self.client.voice_status = lambda _: {"message_id": MID, "state": state,
                    "expires_at": NOW + 3600, "server_time": NOW, "deleted": False}
                self.runtime.recover_outbox()
                key = success_key("a" * 64, job.stem)
                self.assertEqual(self.runtime.audio_requests.outcome(key), "pending")
        # A separate process uses the same persisted request after restart.
        restarted = AudioRequests(self.runtime.audio_requests.directory, clock=lambda: NOW,
            monotonic=lambda: self.mono[0], boot_id="test-boot")
        with restarted.owner():
            request = restarted.claim_next("a" * 64)
            self.assertEqual(request["kind"], "success")
            restarted.finish(request, "played")
        self.runtime.recover_outbox()
        self.assertEqual(restarted.outcome(key), "played")
        with restarted.owner():
            self.assertIsNone(restarted.claim_next("a" * 64))

    def test_clock_step_during_enqueue_cannot_change_relative_sound_window(self):
        for step in (-20, 20):
            with self.subTest(step=step):
                wall = [NOW]
                mono = [100.0]
                def changing_clock():
                    value = wall[0]
                    wall[0] += step
                    return value
                requests = AudioRequests(self.root / f"step-{step}", clock=changing_clock,
                    monotonic=lambda: mono[0], boot_id="test-boot")
                requests.enqueue_for("preview-step", "preview", "a" * 64, 15,
                    ringtone_id="gentle_music_box")
                mono[0] = 114.9
                self.assertEqual(requests.outcome("preview-step"), "pending")
                mono[0] = 115.0
                self.assertEqual(requests.outcome("preview-step"), "expired")

    def test_skewed_wall_clock_keeps_server_send_time_retry_window_and_success_cue(self):
        store, job = self.staged_upload()
        original = json.loads((job.path / "job.json").read_text())
        for skew in (-20, 20):
            with self.subTest(skew=skew):
                elapsed = [0.0]
                self.mono[0] = 100
                self.runtime.clock = lambda: NOW + skew + elapsed[0]
                self.runtime.heartbeat()
                self.mono[0] += 0.25
                elapsed[0] = 0.25
                notices = AudioRequests(self.root / f"skew-{skew}", clock=self.runtime.clock,
                    monotonic=lambda: self.mono[0], boot_id="test-boot")
                metadata = {**original, "state": "pending", "attempts": 0}
                atomic_json(job.path / "job.json", metadata)
                self.client.send_voice = mock.Mock(return_value={"message_id": MID, "state": "accepted",
                    "server_time": NOW, "expires_at": NOW + 3600})
                with mock.patch.object(button_send.cloud_runtime, "CloudRuntime", return_value=self.runtime), \
                     mock.patch.object(button_send, "outbox_store", store), \
                     mock.patch.object(button_send, "cloud_audio_requests", notices), \
                     mock.patch.object(button_send.CloudDeviceClient, "from_environment", return_value=self.client), \
                     mock.patch.object(button_send, "log_event"):
                    self.assertEqual(button_send.cloud_runtime.outbox_now(), NOW + 0.25)
                    self.assertEqual(button_send.cloud_runtime.outbox_retry_until(), NOW + 0.25 + 30 * 86400)
                    self.assertTrue(button_send._send_cloud_upload(store.load(job.path)))
                saved = json.loads((job.path / "job.json").read_text())
                self.assertEqual(saved["cloud_send_started_at"], NOW)
                key = success_key("a" * 64, job.message_id)
                self.assertEqual(notices.outcome(key), "pending")
                # A wall-clock correction on process restart cannot extend or
                # shorten the original same-boot monotonic cue window.
                restarted = AudioRequests(notices.directory, clock=lambda: NOW - skew + 10,
                    monotonic=lambda: self.mono[0], boot_id="test-boot")
                self.mono[0] = 110
                self.assertEqual(restarted.outcome(key), "pending")
                self.mono[0] = 131
                self.assertEqual(restarted.outcome(key), "expired")

    def test_skewed_preview_deadline_survives_restart_without_extending_or_replay(self):
        for skew in (-20, 20):
            with self.subTest(skew=skew):
                self.mono[0] = 100
                self.runtime.clock = lambda: NOW + skew
                self.runtime.heartbeat()
                self.runtime.audio_requests = AudioRequests(self.root / f"preview-skew-{skew}",
                    clock=self.runtime.clock, monotonic=lambda: self.mono[0], boot_id="test-boot")
                item = self.preview(operation_id=OP + str(skew))
                item["expires_at"] = NOW + 15
                self.runtime._command(item, NOW)
                key = preview_key(item["operation_id"])
                self.assertEqual(self.runtime.audio_requests.outcome(key), "pending")
                # Restart at ten seconds with the wall clock corrected across
                # zero skew; only five seconds of the original window remain.
                self.mono[0] = 110
                restarted = AudioRequests(self.runtime.audio_requests.directory,
                    clock=lambda: NOW - skew + 10, monotonic=lambda: self.mono[0], boot_id="test-boot")
                self.assertEqual(restarted.outcome(key), "pending")
                self.mono[0] = 115
                with restarted.owner():
                    self.assertIsNone(restarted.claim_next("a" * 64))
                self.assertEqual(restarted.outcome(key), "expired")
                # Reboot invalidates any pending sound even if wall time is behind.
                reboot = AudioRequests(self.root / f"preview-reboot-{skew}", clock=lambda: NOW + skew,
                    monotonic=lambda: 100, boot_id="test-boot")
                reboot.enqueue_for(key, "preview", "a" * 64, 15, ringtone_id="gentle_music_box")
                after_boot = AudioRequests(reboot.directory, clock=lambda: NOW - 90,
                    monotonic=lambda: 1, boot_id="next-boot")
                self.assertEqual(after_boot.outcome(key), "expired")

    def test_stale_foreign_and_unsuccessful_outbox_statuses_never_cue(self):
        self.runtime.heartbeat()
        base = {"message_id": "local-key", "account_scope": "a" * 64, "state": "cloud_retained",
                "cloud_state": "queued", "cloud_status_checked_at": NOW - 5}
        status = {"message_id": MID, "state": "accepted", "expires_at": NOW + 60,
                  "server_time": NOW, "deleted": False}
        for state in ("queued", "waiting_for_reply", "delivery_uncertain", "failed", "expired"):
            self.runtime.queue_send_success(base, {**status, "state": state})
        for fields in ({"cloud_status_checked_at": NOW - 31}, {"account_scope": "b" * 64},
                       {"cloud_state": "accepted"}, {"cloud_state": "failed"}):
            self.runtime.queue_send_success({**base, **fields}, status)
        self.runtime.queue_send_success(base, {**status, "deleted": True})
        self.assertFalse(list(self.runtime.audio_requests.directory.glob("*.json")))

    def test_uncertain_outbox_recovers_expiry_by_read_only_key_without_resend(self):
        self.runtime.heartbeat()
        job = self.root / "outbox" / "original_key_123456.job"
        job.mkdir(parents=True)
        (job / "audio.wav").write_bytes(b"private recording")
        atomic_json(job / "job.json", {"transport": "cloud", "message_id": "original_key_123456",
                                       "state": "uncertain"})
        calls = []
        def status(key):
            calls.append(key)
            return {"message_id": "cloud-message", "state": "accepted",
                    "expires_at": NOW + 3600, "server_time": NOW, "deleted": False}
        self.client.voice_status = status
        self.runtime.recover_outbox()
        metadata = json.loads((job / "job.json").read_text())
        self.assertEqual(metadata["cloud_message_id"], "cloud-message")
        self.assertEqual(metadata["expires_at"], NOW + 3600)
        self.assertEqual(calls, ["original_key_123456"])
        self.assertTrue((job / "audio.wav").exists())

    def test_outbox_recovery_accepts_real_cloud_uncertain_and_review_states(self):
        for cloud_state in ("uncertain", "held_for_review"):
            with self.subTest(cloud_state=cloud_state):
                job = self.root / "outbox" / f"original_key_{cloud_state}.job"
                job.mkdir(parents=True)
                (job / "audio.wav").write_bytes(b"synthetic recording")
                atomic_json(job / "job.json", {"transport": "cloud", "message_id": job.stem,
                                               "state": "uncertain"})
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.status = 200
                response.read.return_value = json.dumps({"message_id": "cloud-message",
                    "state": cloud_state, "expires_at": NOW + 3600,
                    "server_time": NOW, "deleted": False}).encode()
                opener = mock.Mock(return_value=response)
                client = CloudDeviceClient("https://example.invalid/cloud-api/v1",
                    {"device_id": "synthetic-device-001", "credential": "x" * 43}, opener=opener)
                self.client.voice_status = client.voice_status
                self.runtime = self.restart()
                self.runtime.recover_outbox()
                metadata = json.loads((job / "job.json").read_text())
                self.assertEqual(metadata["state"], "cloud_retained")
                self.assertEqual(metadata["cloud_state"], cloud_state)
                self.assertEqual(metadata["expires_at"], NOW + 3600)
                self.assertTrue((job / "audio.wav").exists())
                self.assertTrue(all(call.args[0].get_method() == "GET" for call in opener.call_args_list))

    def test_unknown_upload_status_keeps_source_until_authoritative_answer(self):
        job = self.root / "outbox" / "original_key_123456.job"
        job.mkdir(parents=True)
        (job / "audio.wav").write_bytes(b"private recording")
        atomic_json(job / "job.json", {"transport": "cloud", "message_id": "original_key_123456",
                                       "state": "uncertain"})
        self.client.voice_status = lambda key: (_ for _ in ()).throw(CloudDeviceError("offline"))
        self.runtime.recover_outbox()
        self.assertTrue((job / "audio.wav").exists())

    def test_unknown_status_keys_do_not_starve_later_known_recording(self):
        for index in range(6):
            job = self.root / "outbox" / f"key_{index:02d}_1234567890.job"
            job.mkdir(parents=True)
            (job / "audio.wav").write_bytes(b"private recording")
            atomic_json(job / "job.json", {"transport": "cloud", "message_id": job.stem,
                                           "state": "uncertain"})
        calls = []
        def status(key):
            calls.append(key)
            if key != "key_05_1234567890":
                raise CloudDeviceError("offline")
            return {"message_id": "cloud-message", "state": "accepted",
                    "expires_at": NOW + 3600, "server_time": NOW, "deleted": False}
        self.client.voice_status = status
        self.runtime.recover_outbox()
        self.assertNotIn("key_05_1234567890", calls)
        restarted = CloudRuntime(self.client, state_path=self.root / "runtime.json",
            contacts_path=self.root / "cloud-contacts.json", queue_dir=self.root / "queue", outbox_dir=self.root / "outbox",
            settings_path=self.root / "settings.json", clock=lambda: NOW,
            monotonic=lambda: self.mono[0], boot_id="test-boot", converter=queued_audio)
        restarted.recover_outbox()
        self.assertIn("key_05_1234567890", calls)
        metadata = json.loads((self.root / "outbox" / "key_05_1234567890.job" / "job.json").read_text())
        self.assertEqual(metadata["cloud_message_id"], "cloud-message")

    def staged_upload(self):
        self.runtime.heartbeat()
        source = self.root / "recording.wav"
        source.write_bytes(b"synthetic WAV")
        encoded = self.root / "encoded.ogg"
        encoded.write_bytes(b"OggSoriginal-encoding")
        store = OutboxStore(self.runtime.outbox_dir, transport="cloud")
        job = store.approve(str(source), PERSON["wa_id"] + "@s.whatsapp.net",
                            "standalone", 1.25, message_id="original_key_123456",
                            account_scope="a" * 64)
        store.prepare_cloud_upload(job, encoded, PERSON["id"], NOW + 7 * 86400)
        store.set_state(job, "sending", increment_attempts=True)
        self.client.voice_status = mock.Mock(side_effect=CloudVoiceNotFound("not found"))
        return store, job

    def test_missing_upload_requeues_same_payload_after_interruption_and_restart(self):
        store, job = self.staged_upload()
        before = (job.path / "audio.ogg").read_bytes()
        restarted = CloudRuntime(self.client, state_path=self.runtime.state_path,
            contacts_path=self.root / "cloud-contacts.json", queue_dir=self.runtime.queue_dir,
            outbox_dir=self.runtime.outbox_dir, settings_path=self.runtime.settings.path,
            clock=lambda: NOW, monotonic=lambda: self.mono[0], boot_id="test-boot")
        restarted.recover_outbox()
        self.assertEqual(store.load(job.path).state, "pending")
        self.assertEqual((job.path / "audio.ogg").read_bytes(), before)
        self.assertEqual(json.loads((job.path / "job.json").read_text())["cloud_upload"]["recipient_id"], PERSON["id"])
        self.client.voice_status.assert_called_once_with(job.message_id)

    def test_not_found_never_requeues_missing_changed_or_legacy_payload(self):
        store, job = self.staged_upload()
        metadata_path = job.path / "job.json"
        original = json.loads(metadata_path.read_text())
        for change in ("audio", "legacy", "account", "recipient", "id", "duration", "deadline"):
            with self.subTest(change=change):
                metadata = json.loads(json.dumps(original))
                (job.path / "audio.ogg").write_bytes(b"OggSoriginal-encoding")
                if change == "audio":
                    (job.path / "audio.ogg").write_bytes(b"OggSdifferent-encoding")
                elif change == "legacy":
                    metadata.pop("cloud_upload")
                elif change == "account":
                    metadata["account_scope"] = "b" * 64
                elif change == "recipient":
                    metadata["recipient"] = "12025550102@s.whatsapp.net"
                elif change == "id":
                    metadata["cloud_upload"]["recipient_id"] = "different-person"
                elif change == "duration":
                    metadata["duration"] = 2.5
                else:
                    metadata["cloud_upload"]["retry_until"] = NOW
                atomic_json(metadata_path, metadata)
                self.runtime.recover_outbox()
                self.assertEqual(store.load(job.path).state, "sending")
                self.assertTrue(job.audio_path.exists())

    def test_transfer_stale_heartbeat_or_removed_person_blocks_not_found_retry(self):
        store, job = self.staged_upload()
        original = json.loads(json.dumps(self.runtime.state["snapshot"]))
        for change in ("account", "stale", "removed", "paused"):
            with self.subTest(change=change):
                self.runtime.state["snapshot"] = json.loads(json.dumps(original))
                snapshot = self.runtime.state["snapshot"]
                if change == "account":
                    snapshot["account_scope"] = "b" * 64
                elif change == "stale":
                    snapshot["boot_id"] = "old-boot"
                elif change == "removed":
                    snapshot["people"] = []
                else:
                    snapshot["entitlement"]["send"] = False
                self.runtime.recover_outbox()
                self.assertEqual(store.load(job.path).state, "sending")

    def test_active_sender_lock_prevents_status_or_requeue(self):
        store, job = self.staged_upload()
        with cloud_outbox_lock(job.path) as acquired:
            self.assertTrue(acquired)
            self.runtime.recover_outbox()
        self.client.voice_status.assert_not_called()
        self.assertEqual(store.load(job.path).state, "sending")
        self.runtime.recover_outbox()
        self.assertEqual(store.load(job.path).state, "pending")

    def test_retained_upload_not_found_never_creates_another_request(self):
        store, job = self.staged_upload()
        store.set_state(job, "cloud_retained")
        self.runtime.recover_outbox()
        self.assertEqual(store.load(job.path).state, "cloud_retained")

    def test_late_status_recovery_honors_prior_authorized_delete(self):
        job = self.root / "outbox" / "original_key_123456.job"
        job.mkdir(parents=True)
        (job / "audio.wav").write_bytes(b"private recording")
        atomic_json(job / "job.json", {"transport": "cloud", "message_id": "original_key_123456",
                                       "state": "uncertain"})
        self.runtime.state["deleted"].append("cloud-message")
        self.client.voice_status = lambda key: {"message_id": "cloud-message", "state": "accepted",
                                                "expires_at": NOW + 3600, "server_time": NOW,
                                                "deleted": False}
        self.runtime.recover_outbox()
        self.assertFalse(job.exists())


if __name__ == "__main__":
    unittest.main()
