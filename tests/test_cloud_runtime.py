import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from messagebox.cloud_runtime import CloudRuntime, CloudRuntimeError
from messagebox.cloud_device import CloudAckGone, CloudDeviceError, atomic_json
from messagebox.played_history import list_played_history
from messagebox.settings import SettingsStore

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
        return {"box_id": "box1234567890123456", "server_time": self.server_time,
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
        document, _ = SettingsStore(self.root / "settings.json").load()
        applied.write_text(json.dumps({"revision": document["revision"], "settings": document}))

    def audio_item(self):
        return {"operation_id": OP, "sequence": 1, "kind": "audio",
                "created_at": NOW - 10, "expires_at": NOW + 3600,
                "payload": {"message_id": MID,
                    "media_url": f"https://button.box/cloud-api/v1/device/media/{MID}",
                    "sha256": hashlib.sha256(self.client.audio).hexdigest(),
                    "content_type": "audio/ogg", "sender_id": PERSON["id"],
                    "sender_name": "Family", "reply_to": "wamid123", "expires_at": NOW + 3600,
                    "message_created_at": NOW - 10}}

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
        candidate = {**document, "revision": 1, "master_volume_percent": 40}
        item = {"operation_id": OP, "sequence": 1, "kind": "settings", "created_at": NOW,
                "expires_at": NOW + 100, "payload": {"settings": candidate,
                "expected_revision": 0, "desired_revision": 1}}
        self.client.items = [item]
        self.runtime.poll_once()
        self.assertEqual(SettingsStore(self.root / "settings.json").load()[0]["master_volume_percent"], 40)
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
                         [mock.call("A1B2C3D4"), mock.call("A1B2C3D4")])
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

    def test_preview_crash_intent_suppresses_replay(self):
        self.runtime.heartbeat()
        item = {"operation_id": OP, "sequence": 1, "kind": "preview_ringtone",
                "created_at": NOW, "expires_at": NOW + 60,
                "payload": {"ringtone_id": "gentle_music_box"}}
        with mock.patch("messagebox.cloud_runtime.INTENT_DIR", self.root / "intents"), \
             mock.patch("messagebox.cloud_runtime.subprocess.run", side_effect=RuntimeError("crash")) as play:
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.runtime._command(item, NOW)
            self.runtime._command(item, NOW)
        self.assertEqual(play.call_count, 1)
        self.assertEqual(json.loads(next(self.ack_dir.glob("*.json")).read_text())["error_code"],
                         "preview_outcome_unknown")

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
