import hashlib
import contextlib
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
from messagebox.played_history import list_played_history, recent_reply_recipient
from messagebox.nfc_state import CardReferenceStore, EnrollmentStore, NfcRouter, SelectionStore
from messagebox.settings import SettingsStore

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with mock.patch.dict(sys.modules, {"gpiozero": gpiozero}):
    from messagebox import button_send


NOW = 1_800_000_000
BOX = {"id": "link1234567890123456", "box_name": "Example Box"}
BOX_JID = "box:" + BOX["id"]
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
        return {**({"connected_boxes": self.connected_boxes} if hasattr(self, "connected_boxes") else {}),
                "box_id": "box1234567890123456", "account_scope": "a" * 64, "server_time": self.server_time,
                "people": self.people, "default_recipient_id": self.people[0]["id"] if self.people else None,
                "entitlement": {"ingest": True, "deliver": self.deliver, "send": self.send,
                                "until": getattr(self, "heartbeat_until", NOW + 3600)}, "queue_hold": False,
                "retention_days": self.retention_days,
                "desired_revision": state["applied_revision"], "desired_settings": state["settings"]}

    def inbox(self, cursor):
        return {"box_id": "box1234567890123456", "server_time": self.server_time,
                "cursor": len(self.items), "items": self.items, "deleted": self.deleted}

    def media(self, url, *, limit):
        assert url.startswith(("https://button.box/cloud-api/v1/device/media/",
                               "https://button.box/cloud-api/v1/device/listened-media/"))
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
        receipts = mock.patch("messagebox.cloud_runtime.LISTENED_DIR", self.root / "listened-receipts")
        receipts.start()
        self.addCleanup(receipts.stop)
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

    def update_settings(self, **changes):
        current, _ = self.runtime.settings.load()
        candidate = {key: value for key, value in current.items() if key not in {"version", "revision"}}
        self.runtime.settings.update({**candidate, **changes}, current["revision"])

    def connect_box(self):
        self.client.connected_boxes = [BOX.copy()]
        self.runtime.heartbeat()

    def box_listened_item(self, *, clip=True, operation_id="listened:box"):
        item = self.listened_item(clip=clip, operation_id=operation_id)
        item["expires_at"] = NOW + 1800
        payload = item["payload"]
        payload.pop("listener_identity_id")
        payload.pop("listener_first_name")
        payload.update(listener_link_id=BOX["id"], listener_name=BOX["box_name"])
        return item

    def box_audio_item(self):
        item = self.audio_item()
        item["payload"].update(sender_id=BOX["id"], sender_kind="box",
                               sender_name=BOX["box_name"], reply_to=None)
        return item

    def nfc_router(self):
        return NfcRouter(self.runtime.contacts,
            SelectionStore(self.root / "nfc-selection.json", clock=self.runtime.clock),
            EnrollmentStore(self.root / "nfc-enrollment.json", clock=self.runtime.clock))

    def test_connected_boxes_capability_and_invalid_entries_are_dropped(self):
        self.client.connected_boxes = [None, {}, {"id": "bad/id", "box_name": "Box"},
            {"id": "bad-name", "box_name": "x" * 61}, {"id": "empty", "box_name": " "},
            {"id": "control", "box_name": "Box\0"}, {"id": [], "box_name": "Box"},
            {"id": "type", "box_name": 1}, BOX, BOX.copy(),
            {"id": PERSON["id"], "box_name": "Collision"}]
        with mock.patch.object(self.client, "heartbeat", wraps=self.client.heartbeat) as heartbeat:
            self.runtime.heartbeat()
        self.assertTrue(heartbeat.call_args.args[0]["capabilities"]["box_link"])
        self.assertEqual(self.runtime.state["snapshot"]["connected_boxes"], [BOX])
        self.assertEqual(self.runtime.state["snapshot"]["people"], [PERSON])
        self.assertEqual(self.runtime.contacts.contact(BOX_JID)["kind"], "box")
        self.assertEqual(self.runtime.contacts.contact(BOX_JID)["label"], BOX["box_name"])

    def test_connected_boxes_limit_and_malformed_list_do_not_fail_heartbeat(self):
        self.client.connected_boxes = [{"id": f"link-{i}", "box_name": "Box"} for i in range(51)]
        self.runtime.heartbeat()
        self.assertEqual(len(self.runtime.state["snapshot"]["connected_boxes"]), 50)
        self.assertNotIn("box:link-50", self.runtime.contacts.load()["contacts"])
        for value in (None, "bad", {}, 1):
            self.client.connected_boxes = value
            self.runtime.heartbeat()
            self.assertEqual(self.runtime.state["snapshot"]["connected_boxes"], [])

    def test_old_cloud_heartbeat_preserves_people_and_removes_box_cards_and_default(self):
        self.connect_box()
        jid = PERSON["wa_id"] + "@s.whatsapp.net"
        self.runtime.contacts.assign_card(jid, "01AABBCC")
        self.runtime.contacts.assign_card(BOX_JID, "04AABBCC")
        self.runtime.contacts.choose_default_recipient(BOX_JID)
        self.client.people = []
        del self.client.connected_boxes
        self.runtime.heartbeat()
        document = self.runtime.contacts.load()
        self.assertEqual(document["contacts"], {})
        self.assertIsNone(document["default_recipient"])
        result = self.nfc_router().card_seen("04AABBCC")
        self.assertEqual(result.action, "unknown")
        self.assertTrue(result.announce)
        with self.assertRaises(CloudRuntimeError):
            self.runtime.recipient_id(BOX_JID)
        self.client.people = [PERSON.copy()]
        self.runtime.heartbeat()
        self.assertEqual(self.runtime.recipient_id(jid), PERSON["id"])

    def test_link_removal_preserves_people_cards_and_rejects_pending_box_audio(self):
        self.connect_box()
        self.runtime.contacts.assign_card(BOX_JID, "04AABBCC")
        jid = PERSON["wa_id"] + "@s.whatsapp.net"
        self.runtime.contacts.assign_card(jid, "01AABBCC")
        self.runtime._audio(self.box_audio_item(), NOW)
        metadata = json.loads(next(self.runtime.queue_dir.glob("*.json")).read_text())
        self.client.connected_boxes = []
        self.runtime.heartbeat()
        self.assertFalse(self.runtime.playable(metadata))
        self.assertIsNone(self.runtime.contacts.resolve_card("04AABBCC"))
        self.assertEqual(self.runtime.contacts.resolve_card("01AABBCC")["jid"], jid)

    def test_box_family_card_enroll_inventory_and_targeted_remove_are_replay_safe(self):
        self.connect_box()
        item = self.nfc_item()
        item["payload"]["recipient_id"] = BOX["id"]
        self.runtime._nfc(item)
        active = self.nfc_router().enrollment.active()
        self.assertEqual((active["jid"], active["label"]), (BOX_JID, BOX["box_name"]))
        self.assertEqual(self.nfc_router().card_seen("04AABBCC").action, "enrolled")
        self.runtime._finish_nfc()
        self.assertEqual(json.loads(next(self.ack_dir.glob("*.json")).read_text())["state"], "applied")
        self.runtime.contacts.assign_card(BOX_JID, "05AABBCC")
        inventory = self.runtime._nfc_inventory()
        self.assertEqual([card["recipient_id"] for card in inventory["cards"]], [BOX["id"], BOX["id"]])
        card = inventory["cards"][0]
        remove = {"operation_id": "remove-box-card", "kind": "nfc_unpair", "payload": {
            **card, "inventory_revision": inventory["revision"]}}
        self.runtime._nfc(remove)
        self.runtime._nfc(remove)
        self.assertEqual(len(self.runtime.contacts.contact(BOX_JID)["card_uids"]), 1)
        self.assertNotIn("04:AA:BB:CC", json.dumps(inventory))

    def test_incoming_box_audio_plays_normally_and_reply_targets_box(self):
        self.connect_box()
        with mock.patch.object(self.client, "media", wraps=self.client.media) as media:
            self.runtime._audio(self.box_audio_item(), NOW)
            self.runtime._audio(self.box_audio_item(), NOW)
        media.assert_called_once()
        [path] = list(self.runtime.queue_dir.glob("*.wav"))
        metadata = json.loads(Path(str(path) + ".json").read_text())
        self.assertEqual(metadata["chat"], BOX_JID)
        self.assertEqual(metadata["sender_kind"], "box")
        self.assertTrue(self.runtime.playable(metadata))
        self.assertEqual(path.read_bytes(), self.client.audio)
        with self.listened_playback(), \
             mock.patch.object(button_send, "QUEUE_DIR", str(self.runtime.queue_dir)), \
             mock.patch.object(button_send, "EVENTS_FILE", str(self.root / "events.jsonl")), \
             mock.patch.object(button_send.cloud_runtime, "playable", side_effect=self.runtime.playable), \
             mock.patch.object(button_send.cloud_runtime, "record_played") as played_ack, \
             mock.patch.object(button_send, "play_moment") as cues, \
             mock.patch.object(button_send.subprocess, "run", return_value=mock.Mock(returncode=0)) as playback, \
             mock.patch.object(button_send, "wait_for_stable_open"), \
             mock.patch.object(button_send, "refresh_led"), \
             mock.patch.object(button_send.time, "time", return_value=NOW):
            button_send.play_next_legacy()
        self.assertEqual(playback.call_args.args[0][-1], str(path))
        self.assertEqual(cues.call_args_list, [mock.call("msg_start", "msg-start"), mock.call("msg_end")])
        played_ack.assert_called_once_with(metadata)
        self.assertEqual(recent_reply_recipient(self.runtime.queue_dir,
            self.runtime.contacts.allowed_jids(), now=NOW), ("route", BOX_JID))
        with mock.patch.object(button_send, "CONTACTS_FILE", self.runtime.contacts.path), \
             mock.patch.object(button_send, "QUEUE_DIR", str(self.runtime.queue_dir)), \
             mock.patch.object(button_send, "nfc_idle_routing_is_safe", return_value=True), \
             mock.patch.object(button_send, "NFC_SELECTION_FILE", self.root / "no-selection"), \
             mock.patch.object(button_send.time, "time", return_value=NOW):
            self.assertEqual(button_send.recording_recipient_context()["contact"]["jid"], BOX_JID)

    def test_incoming_box_audio_requires_current_link_and_explicit_sender_kind(self):
        self.connect_box()
        for sender, kind in ((BOX["id"], None), ("removed-link", "box"), (PERSON["id"], "box")):
            item = self.box_audio_item()
            item["payload"].update(sender_id=sender, sender_kind=kind)
            with self.subTest(sender=sender, kind=kind), \
                 mock.patch.object(self.client, "media") as media, self.assertRaises(CloudRuntimeError):
                self.runtime._audio(item, NOW)
            media.assert_not_called()

    def test_box_recording_upload_uses_link_id_and_recovery_keeps_exact_route(self):
        self.connect_box()
        source, encoded = self.root / "record.wav", self.root / "record.ogg"
        source.write_bytes(b"synthetic WAV")
        encoded.write_bytes(b"OggS exact bytes")
        store = OutboxStore(self.runtime.outbox_dir, transport="cloud")
        job = store.approve(str(source), BOX_JID, "reply", 1.25,
                            message_id="box-recording", account_scope="a" * 64)
        store.prepare_cloud_upload(job, encoded, self.runtime.recipient_id(job.recipient), NOW + 1800)
        client = mock.Mock()
        client.send_voice.return_value = {"message_id": "sent-box", "state": "queued",
                                         "server_time": NOW, "expires_at": NOW + 1800}
        with mock.patch.object(button_send, "outbox_store", store), \
             mock.patch.object(button_send.CloudDeviceClient, "from_environment", return_value=client), \
             mock.patch.object(button_send.cloud_runtime, "recipient_id", side_effect=self.runtime.recipient_id), \
             mock.patch.object(button_send.cloud_runtime, "outbox_now", side_effect=self.runtime.server_now), \
             mock.patch.object(button_send, "log"), mock.patch.object(button_send, "log_event"):
            self.assertTrue(button_send._send_cloud_upload(job))
        self.assertEqual(client.send_voice.call_args.args[1], BOX["id"])
        store.set_state(job, "sending")
        metadata = json.loads((job.path / "job.json").read_text())
        metadata.pop("cloud_message_id")
        atomic_json(job.path / "job.json", metadata)
        self.client.voice_status = mock.Mock(side_effect=CloudVoiceNotFound("missing"))
        self.runtime.recover_outbox()
        self.assertEqual(store.load(job.path).state, "pending")
        self.assertEqual((job.path / "audio.ogg").read_bytes(), b"OggS exact bytes")
        store.set_state(job, "sending")
        self.client.connected_boxes = []
        self.runtime.heartbeat()
        self.runtime.recover_outbox()
        self.assertEqual(store.load(job.path).state, "sending")

    def test_hold_release_box_recording_is_staged_with_bound_recipient(self):
        self.connect_box()
        name = f"{NOW * 1000}-1.25.wav"
        path = self.runtime.outbox_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic WAV")
        store = OutboxStore(self.runtime.outbox_dir, transport="cloud")
        with mock.patch.object(button_send, "OUTBOX_DIR", str(self.runtime.outbox_dir)), \
             mock.patch.object(button_send, "outbox_store", store), \
             mock.patch.object(button_send, "transport_mode", return_value="cloud"), \
             mock.patch.object(button_send, "log_event"):
            button_send.bind_legacy_job_recipient(str(path), BOX_JID, account_scope="a" * 64)
            self.assertEqual(button_send.legacy_job_recipient(str(path)), BOX_JID)
            self.assertTrue(button_send.stage_hold_release_cloud_job(name))
        [job] = store.jobs()
        self.assertEqual(job.recipient, BOX_JID)

    def test_box_listened_clip_playback_dedup_and_restart_ack(self):
        self.connect_box()
        item = self.box_listened_item()
        self.client.items = [item]
        self.runtime.poll_once()
        self.runtime.state = self.runtime._load()
        self.runtime.poll_once()
        store = self.runtime._receipt_store()
        self.assertEqual(store.pending_count(), 1)
        notice = store.load(next(store.pending.glob("*.json")))
        self.assertEqual(notice.cloud["listener_kind"], "box")
        self.assertEqual(Path(notice.clip).read_bytes(), self.client.audio)
        with self.listened_playback(), mock.patch.object(button_send, "play_moment"), \
             mock.patch.object(button_send, "play_audio_ordinary") as play:
            self.assertEqual(button_send.play_pending_listened(), 1)
            play.assert_called_once_with(notice.clip)
        self.runtime.state = self.runtime._load()
        self.runtime._maintain_local()
        self.assertEqual(self.client.acks[-1]["state"], "applied")
        self.runtime.poll_once()
        self.assertEqual(store.pending_count(), 0)
        self.client.connected_boxes = []
        self.runtime.heartbeat()
        self.assertEqual(list((self.root / "listened-clips").glob("*.wav")), [])

    def test_box_listened_expired_quiet_and_silent_never_queue_or_download(self):
        self.connect_box()
        for index, gate in enumerate(("expired", "quiet", "silent")):
            with self.subTest(gate=gate):
                item = self.box_listened_item(operation_id=f"listened:gate{index}")
                current, _ = self.runtime.settings.load()
                self.update_settings(arrival_signal="silent" if gate == "silent" else "ring_and_lamp",
                    quiet_hours={"enabled": gate == "quiet", "start": "00:00", "end": "00:00"})
                if gate == "expired":
                    item["expires_at"] = NOW
                self.client.items = [item]
                with mock.patch.object(self.client, "media") as download:
                    self.runtime.poll_once()
                download.assert_not_called()
                self.assertEqual(self.client.acks[-1]["state"], "expired")
                self.assertEqual(self.runtime._receipt_store().pending_count(), 0)
        current, _ = self.runtime.settings.load()
        self.update_settings(arrival_signal="ring_and_lamp",
            quiet_hours={"enabled": False, "start": "00:00", "end": "00:00"})
        self.runtime.poll_once()
        self.assertEqual(self.runtime._receipt_store().pending_count(), 0)

    def test_box_listened_lamp_only_pulses_without_any_audio(self):
        self.connect_box()
        current, _ = self.runtime.settings.load()
        self.update_settings(arrival_signal="lamp_only")
        self.runtime._command(self.box_listened_item(), NOW)
        with self.listened_playback(), mock.patch.object(button_send, "ring_alert") as pulse, \
             mock.patch.object(button_send, "play_moment") as cue, \
             mock.patch.object(button_send, "play_audio_ordinary") as audio:
            self.assertEqual(button_send.play_pending_listened(), 1)
        self.assertEqual(pulse.call_args.kwargs["settings"]["arrival_signal"], "lamp_only")
        cue.assert_not_called()
        audio.assert_not_called()
        self.runtime._finish_listened()
        self.runtime.flush_acks()
        self.assertEqual(self.client.acks[-1]["state"], "applied")

    def test_quiet_hours_discard_box_notices_behind_identity_even_while_busy(self):
        self.connect_box()
        self.runtime._command(self.listened_item(clip=False), NOW)
        self.runtime._command(self.box_listened_item(clip=False), NOW)
        store = self.runtime._receipt_store()
        with self.listened_playback(quiet=True), \
             mock.patch.object(button_send, "_recording", True), \
             mock.patch.object(store, "claim_next") as claim, \
             mock.patch.object(button_send, "play_moment") as cue:
            self.assertEqual(button_send.maybe_play_pending_listened(), 0)
        claim.assert_not_called()
        cue.assert_not_called()
        self.assertEqual(store.pending_count(), 1)
        self.runtime._finish_listened()
        self.runtime.flush_acks()
        states = {ack["operation_id"]: ack["state"] for ack in self.client.acks}
        self.assertEqual(states["listened:box"], "expired")
        with self.listened_playback(), mock.patch.object(button_send, "play_moment"), \
             mock.patch.object(button_send, "play_audio_ordinary"):
            self.assertEqual(button_send.play_pending_listened(), 1)

    def test_box_listened_lamp_failure_retains_notice_without_success_ack(self):
        self.connect_box()
        self.update_settings(arrival_signal="lamp_only")
        self.runtime._command(self.box_listened_item(clip=False), NOW)
        with self.listened_playback(), \
             mock.patch.object(button_send, "ring_alert", side_effect=OSError("lamp unavailable")):
            self.assertEqual(button_send.play_pending_listened(), 0)
        self.assertEqual(self.runtime._receipt_store().pending_count(), 1)
        self.runtime._finish_listened()
        self.runtime.flush_acks()
        self.assertEqual(self.client.acks[-1]["state"], "received")

    def test_box_listened_rechecks_expiry_quiet_silent_and_unlink_before_playback(self):
        self.connect_box()
        for index, gate in enumerate(("expired", "quiet", "silent", "unlinked")):
            with self.subTest(gate=gate):
                self.client.connected_boxes = [BOX.copy()]
                current, _ = self.runtime.settings.load()
                self.update_settings(arrival_signal="ring_and_lamp")
                self.runtime.heartbeat()
                self.runtime._command(self.box_listened_item(clip=False, operation_id=f"listened:later{index}"), NOW)
                if gate == "expired":
                    self.client.server_time = NOW + 1800
                    with mock.patch.object(self.runtime, "clock", return_value=NOW + 1800):
                        self.runtime.heartbeat()
                    self.runtime.clock = lambda: self.client.server_time
                elif gate == "silent":
                    current, _ = self.runtime.settings.load()
                    self.update_settings(arrival_signal="silent")
                elif gate == "unlinked":
                    self.client.connected_boxes = []
                    self.runtime.heartbeat()
                with self.listened_playback(quiet=gate == "quiet"), \
                     mock.patch.object(button_send, "play_moment") as cue, \
                     mock.patch.object(button_send, "play_audio_ordinary") as audio:
                    self.assertEqual(button_send.play_pending_listened(), 0)
                cue.assert_not_called()
                audio.assert_not_called()
                self.runtime._finish_listened()
                self.runtime.flush_acks()
                self.assertEqual(self.client.acks[-1]["state"], "rejected" if gate == "unlinked" else "expired")
                self.client.server_time = NOW
                self.runtime.clock = lambda: NOW

    def test_box_listened_missing_invalid_clip_and_long_name_use_generic_voice(self):
        self.connect_box()
        for index, failure in enumerate(("absent", "invalid", "long-name")):
            item = self.box_listened_item(clip=failure != "absent", operation_id=f"listened:fallback{index}")
            if failure == "invalid":
                item["payload"]["sha256"] = "invalid"
            elif failure == "long-name":
                item["payload"]["listener_name"] = "x" * 60
            self.runtime._command(item, NOW)
            with self.listened_playback(), mock.patch.object(button_send, "play_moment"), \
                 mock.patch.object(button_send, "play_audio_ordinary") as play:
                self.assertEqual(button_send.play_pending_listened(), 1)
            play.assert_called_once_with(str(self.root / "voice/voice-listened.wav"))
            self.runtime._finish_listened()

    def listened_item(self, *, clip=True, operation_id="listened:" + "b" * 64):
        payload = {"message_id": MID, "listener_identity_id": PERSON["id"],
                   "listener_first_name": "Avery", "voice_pack": "jessica"}
        if clip:
            path = self.root / "name.wav"
            write_pcm_wav(path, 0.1, sample_rate=48000)
            self.client.audio = path.read_bytes()
            payload.update(text_hash="c" * 64,
                media_url="https://button.box/cloud-api/v1/device/listened-media/" + "c" * 64,
                sha256=hashlib.sha256(self.client.audio).hexdigest(), content_type="audio/wav")
        return {"operation_id": operation_id, "sequence": 1, "kind": "listened",
                "created_at": NOW, "expires_at": NOW + 3600, "payload": payload}

    def listened_playback(self, *, quiet=False):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(button_send, "receipt_store", self.runtime._receipt_store()))
        stack.enter_context(mock.patch.object(button_send, "button", types.SimpleNamespace(is_pressed=False), create=True))
        stack.enter_context(mock.patch.object(button_send, "_recording", False))
        stack.enter_context(mock.patch.object(button_send, "_guided_active", False))
        stack.enter_context(mock.patch.object(button_send, "announcement_gate", button_send.AnnouncementGate()))
        stack.enter_context(mock.patch.object(button_send, "transport_mode", return_value="cloud"))
        stack.enter_context(mock.patch.object(button_send, "quiet_hours", return_value=quiet))
        stack.enter_context(mock.patch.object(button_send, "LISTENED_FALLBACK_WAV", str(self.root / "voice/voice-listened.wav")))
        fallback = self.root / "voice/voice-listened.wav"
        fallback.parent.mkdir(exist_ok=True)
        fallback.write_bytes(b"synthetic fallback")
        stack.enter_context(mock.patch.object(button_send.sound_pack, "SOUND_DIR", self.root))
        stack.enter_context(mock.patch.object(button_send.cloud_runtime, "listened_status", side_effect=self.runtime.listened_status))
        stack.enter_context(mock.patch.object(button_send, "log"))
        stack.enter_context(mock.patch.object(button_send, "log_event"))
        return stack

    def test_listened_clip_receipt_replay_and_playback_ack_survive_restart(self):
        self.runtime.heartbeat()
        item = self.listened_item()
        self.client.items = [item]
        with mock.patch.object(self.client, "media", wraps=self.client.media) as download:
            self.runtime.poll_once()
            store = self.runtime._receipt_store()
            [path] = list(store.pending.glob("*.json"))
            notice = store.load(path)
            self.assertEqual(notice.listener_name, "Avery")
            self.assertEqual(Path(notice.clip).read_bytes(), self.client.audio)
            self.assertEqual(Path(notice.clip).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(notice.clip).parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.client.acks[-1]["state"], "received")
            self.runtime.state = self.runtime._load()
            self.runtime.poll_once()
            download.assert_called_once()
            self.assertEqual(store.pending_count(), 1)
            order = []
            with self.listened_playback(), \
                 mock.patch.object(button_send, "play_moment", side_effect=lambda cue: order.append(cue)), \
                 mock.patch.object(button_send, "play_audio_ordinary", side_effect=lambda clip: order.append(clip)):
                self.assertEqual(button_send.maybe_play_pending_listened(), 1)
            self.assertEqual(order, ["listened", notice.clip])
            self.runtime.state = self.runtime._load()
            self.runtime._maintain_local()
            self.assertEqual(self.client.acks[-1]["state"], "applied")
            self.runtime.poll_once()
            self.assertEqual(store.pending_count(), 0)
            download.assert_called_once()
            self.assertNotIn("played", [ack["state"] for ack in self.client.acks])
            self.assertEqual(list(self.runtime.queue_dir.glob("*.wav")), [])
            self.runtime.clock = lambda: NOW + 3600
            self.client.server_time = NOW + 3600
            self.runtime.heartbeat()
            self.runtime.poll_once()
            self.assertEqual(self.client.acks[-1]["state"], "applied")

    def test_listened_clip_pack_is_checked_at_playback_after_settings_change(self):
        self.runtime.heartbeat()
        for index, (payload_pack, current_pack, matches) in enumerate((
            ("pirate", "pirate", True), ("pirate", "alien", False),
            (None, "jessica", False), ("jessica", "dj", False),
        )):
            item = self.listened_item(operation_id=f"listened:pack{index}")
            item["payload"]["voice_pack"] = payload_pack
            self.runtime._command(item, NOW)
            store = self.runtime._receipt_store()
            [path] = list(store.pending.glob("*.json"))
            notice = store.load(path)
            self.assertEqual(notice.cloud["voice_pack"], payload_pack)
            with self.listened_playback(), \
                 mock.patch.object(button_send.sound_pack, "current_voice_pack", return_value=current_pack), \
                 mock.patch.object(button_send, "play_moment"), \
                 mock.patch.object(button_send, "play_audio_ordinary") as play:
                self.assertEqual(button_send.play_pending_listened(), 1)
                play.assert_called_once_with(notice.clip if matches else str(self.root / "voice/voice-listened.wav"))
            self.runtime._maintain_local()

    def test_listened_receipt_deduplicates_crash_before_state_save(self):
        self.runtime.heartbeat()
        item = self.listened_item()
        with mock.patch.object(self.runtime, "_save", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.runtime._command(item, NOW)
        self.runtime.state = self.runtime._load()
        with mock.patch.object(self.client, "media") as download:
            self.runtime._command(item, NOW)
            download.assert_not_called()
        self.assertEqual(self.runtime._receipt_store().pending_count(), 1)

    def test_listened_missing_failed_or_corrupt_clip_uses_fallback(self):
        self.runtime.heartbeat()
        for index, failure in enumerate(("absent", "sha", "download", "format")):
            with self.subTest(failure=failure):
                item = self.listened_item(clip=failure != "absent", operation_id=f"listened:case{index}")
                if failure == "sha":
                    item["payload"]["sha256"] = "0" * 64
                if failure == "format":
                    self.client.audio = b"not a wave"
                    item["payload"]["sha256"] = hashlib.sha256(self.client.audio).hexdigest()
                with mock.patch.object(self.client, "media", side_effect=CloudDeviceError("unavailable")) if failure == "download" else contextlib.nullcontext():
                    self.runtime._command(item, NOW)
                store = self.runtime._receipt_store()
                [path] = list(store.pending.glob("*.json"))
                self.assertEqual(store.load(path).clip, "")
                with self.listened_playback(), mock.patch.object(button_send, "play_moment"), \
                     mock.patch.object(button_send, "play_audio_ordinary") as play:
                    self.assertEqual(button_send.play_pending_listened(), 1)
                    play.assert_called_once_with(str(self.root / "voice/voice-listened.wav"))
                self.runtime._maintain_local()
                self.assertEqual(self.client.acks[-1]["state"], "applied")

    def test_listened_quiet_hours_and_busy_owner_defer_then_expire(self):
        self.runtime.heartbeat()
        self.runtime._command(self.listened_item(clip=False), NOW)
        store = self.runtime._receipt_store()
        with self.listened_playback(quiet=True), mock.patch.object(button_send, "play_moment") as cue:
            self.assertEqual(button_send.play_pending_listened(), 0)
            cue.assert_not_called()
        with self.listened_playback(), mock.patch.object(button_send, "play_moment") as cue:
            for flag in ("_recording", "_guided_active"):
                with mock.patch.object(button_send, flag, True):
                    self.assertEqual(button_send.play_pending_listened(), 0)
            with mock.patch.object(button_send.button, "is_pressed", True):
                self.assertEqual(button_send.play_pending_listened(), 0)
            self.assertEqual(store.pending_count(), 1)
            self.runtime.clock = lambda: NOW + 3600
            self.client.server_time = NOW + 3600
            self.runtime.heartbeat()
            self.assertEqual(button_send.play_pending_listened(), 0)
            cue.assert_not_called()
        self.runtime._maintain_local()
        self.assertEqual(store.pending_count(), 0)
        self.assertEqual(self.client.acks[-1]["state"], "expired")

    def test_listened_cache_reuses_clip_invalidates_text_and_clears_revocation(self):
        self.runtime.heartbeat()
        item = self.listened_item()
        with mock.patch.object(self.client, "media", wraps=self.client.media) as download:
            first = self.runtime._listened_clip(item["payload"], "a" * 64)
            self.assertEqual(self.runtime._listened_clip(item["payload"], "a" * 64), first)
            download.assert_called_once()
            item["payload"]["text_hash"] = "d" * 64
            second = self.runtime._listened_clip(item["payload"], "a" * 64)
            self.assertFalse(Path(first).exists())
            self.assertTrue(Path(second).exists())
        self.client.people = []
        self.runtime.heartbeat()
        self.assertFalse(Path(second).exists())

    def test_listened_cache_keeps_same_text_in_different_packs_separate(self):
        self.runtime.heartbeat()
        item = self.listened_item()
        with mock.patch.object(self.client, "media", wraps=self.client.media) as download:
            jessica = self.runtime._listened_clip(item["payload"], "a" * 64)
            item["payload"]["voice_pack"] = "pirate"
            pirate = self.runtime._listened_clip(item["payload"], "a" * 64)
            self.assertNotEqual(jessica, pirate)
            self.assertTrue(Path(jessica).exists())
            self.assertEqual(self.runtime._listened_clip(item["payload"], "a" * 64), pirate)
            self.assertEqual(download.call_count, 2)
            item["payload"]["text_hash"] = "d" * 64
            self.runtime._listened_clip(item["payload"], "a" * 64)
            self.assertFalse(Path(pirate).exists())
            self.assertTrue(Path(jessica).exists())

    def test_listened_cache_bounds_and_evicted_notice_fallback(self):
        self.runtime.heartbeat()
        item = self.listened_item()
        self.runtime._command(item, NOW)
        directory = self.root / "listened-clips"
        prefix = self.runtime._listened_prefix("a" * 64, PERSON["id"])
        for index in range(4):
            (directory / f"{prefix}-{index:064x}.wav").write_bytes(b"synthetic")
        with mock.patch("messagebox.cloud_runtime.MAX_LISTENED_CLIPS", 2):
            self.runtime._prune_listened_clips()
        self.assertEqual(len(list(directory.glob("*.wav"))), 2)
        for path in directory.glob("*.wav"):
            path.unlink()
        with self.listened_playback(), mock.patch.object(button_send, "play_moment"), \
             mock.patch.object(button_send, "play_audio_ordinary") as play:
            self.assertEqual(button_send.play_pending_listened(), 1)
            play.assert_called_once_with(str(self.root / "voice/voice-listened.wav"))

    def test_listened_capability_is_reported_by_heartbeat(self):
        with mock.patch.object(self.client, "heartbeat", wraps=self.client.heartbeat) as send:
            self.runtime.heartbeat()
        self.assertIs(send.call_args.args[0]["capabilities"]["listened_announcements"], True)

    def test_listened_stale_authorization_waits_then_revoked_listener_is_rejected(self):
        self.runtime.heartbeat()
        item = self.listened_item()
        self.runtime._command(item, NOW)
        store = self.runtime._receipt_store()
        self.mono[0] += 91
        with self.listened_playback(), mock.patch.object(button_send, "play_moment") as cue:
            self.assertEqual(button_send.play_pending_listened(), 0)
            self.assertEqual(store.pending_count(), 1)
            self.client.people = []
            self.runtime.heartbeat()
            self.assertEqual(button_send.play_pending_listened(), 0)
            cue.assert_not_called()
        self.runtime._maintain_local()
        self.assertEqual(self.client.acks[-1]["state"], "rejected")
        self.assertEqual(list((self.root / "listened-clips").glob("*.wav")), [])

    def test_listened_expired_and_invalid_commands_never_download(self):
        self.runtime.heartbeat()
        item = self.listened_item()
        item["expires_at"] = NOW
        self.client.items = [item]
        with mock.patch.object(self.client, "media") as download:
            self.runtime.poll_once()
            self.assertEqual(self.client.acks[-1]["state"], "expired")
            item = self.listened_item(operation_id="listened:invalid")
            item["payload"]["listener_identity_id"] = "unknown-listener"
            self.client.items = [item]
            self.runtime.poll_once()
            self.assertEqual(self.client.acks[-1]["state"], "rejected")
            download.assert_not_called()
        self.assertEqual(self.runtime._receipt_store().pending_count(), 0)

    def test_listened_playback_failure_releases_receipt_without_applied_ack(self):
        self.runtime.heartbeat()
        item = self.listened_item(clip=False)
        self.runtime._command(item, NOW)
        with self.listened_playback(), mock.patch.object(button_send, "play_moment"), \
             mock.patch.object(button_send, "play_audio_ordinary", side_effect=OSError("speaker unavailable")):
            self.assertEqual(button_send.play_pending_listened(), 0)
        self.assertEqual(self.runtime._receipt_store().pending_count(), 1)
        self.runtime._maintain_local()
        self.assertEqual(self.client.acks[-1]["state"], "received")
        with self.listened_playback(), mock.patch.object(button_send, "play_moment"), \
             mock.patch.object(button_send, "play_audio_ordinary"):
            self.assertEqual(button_send.play_pending_listened(), 1)
        self.runtime._maintain_local()
        self.assertEqual(self.client.acks[-1]["state"], "applied")

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

    def settings_command(self, **changes):
        current, _ = self.runtime.settings.load()
        desired = {**current, **changes, "revision": current["revision"] + 1}
        return {"operation_id": f"settings_operation_{desired['revision']}", "kind": "settings",
                "payload": {"settings": desired, "expected_revision": current["revision"],
                            "desired_revision": desired["revision"]}}

    def test_settings_unknown_keys_report_through_restart_and_completed_replay(self):
        self.runtime.heartbeat()
        item = self.settings_command(master_volume_percent=37, future_setting={"value": 1})
        item.update(sequence=1, created_at=NOW, expires_at=NOW + 100)
        self.client.items = [item]
        self.runtime.poll_once()
        self.assertEqual(self.client.acks[-1]["ignored_settings"], ["future_setting"])
        saved, warning = self.runtime.settings.load()
        self.assertFalse(warning)
        self.assertEqual(saved["master_volume_percent"], 37)
        self.assertNotIn("future_setting", saved)
        self.runtime = self.restart()
        (self.root / "applied-settings.json").write_text(json.dumps({"revision": 1, "settings": saved}))
        self.client.items = []
        self.runtime.poll_once()
        applied = self.client.acks[-1]
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(applied["ignored_settings"], ["future_setting"])
        self.client.items = [item]
        self.runtime.poll_once()
        self.assertEqual(self.client.acks[-1], applied)
        self.assertEqual(self.runtime.settings.load()[0]["revision"], 1)

    def test_unknown_settings_do_not_hide_invalid_known_values(self):
        self.runtime.heartbeat()
        original, _ = self.runtime.settings.load()
        item = self.settings_command(master_volume_percent=101, future_setting=True)
        item.update(sequence=1, created_at=NOW, expires_at=NOW + 100)
        self.client.items = [item]
        self.runtime.poll_once()
        self.assertEqual(self.client.acks[-1]["state"], "rejected")
        self.assertEqual(self.client.acks[-1]["error_code"], "device_command_rejected")
        self.assertEqual(self.runtime.settings.load()[0], original)

    def settings_audio_owner(self, revision=0):
        return mock.patch.multiple(button_send,
            cloud_audio_requests=self.runtime.audio_requests,
            caregiver_settings=lambda: self.runtime.settings.load()[0],
            _applied_volume_revision=revision, _recording=False, _guided_active=False,
            button=types.SimpleNamespace(is_pressed=False), led=mock.Mock(), create=True)

    def apply_settings_on_button(self):
        with mock.patch.object(button_send.time, "monotonic", side_effect=lambda: self.mono[0]), \
             mock.patch.object(button_send.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)):
            self.assertTrue(button_send.apply_master_volume())

    def test_settings_saved_queues_after_apply_and_plays_once_at_new_volume(self):
        self.runtime.heartbeat()
        item = self.settings_command(master_volume_percent=40, ringtone_id="sunshine")
        with self.settings_audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "quiet_hours", return_value=False), \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send, "play_ringtone_snippet", return_value=True) as play:
            self.runtime._command(item, NOW)
            self.runtime._finish_settings()
            self.assertFalse(list(self.runtime.audio_requests.directory.glob("*.json")))
            self.apply_settings_on_button()
            self.runtime._finish_settings()
            self.assertEqual(len(list(self.runtime.audio_requests.directory.glob("*.json"))), 1)
            self.assertFalse(button_send.maybe_play_cloud_sound())
            self.mono[0] += 3
            def hear_new_volume(*_args):
                self.assertEqual(button_send._applied_volume_revision, 1)
                self.assertEqual(button_send.caregiver_settings()["master_volume_percent"], 40)
                return True
            play.side_effect = hear_new_volume
            self.assertTrue(button_send.maybe_play_cloud_sound())
            self.runtime._command(item, NOW)
            self.runtime._finish_settings()
            self.assertFalse(button_send.maybe_play_cloud_sound())
        play.assert_called_once_with(None)  # Ringtone changed too: the whole new ringtone at the new level.

    def test_voice_switch_plays_new_pack_sample_after_apply_and_debounce(self):
        self.runtime.heartbeat()
        item = self.settings_command(voice_pack="dj", master_volume_percent=35)
        with self.settings_audio_owner(), \
             mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "quiet_hours", return_value=False), \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send.sound_pack, "SOUND_DIR", Path(__file__).resolve().parents[1] / "sounds"), \
             mock.patch.object(button_send, "led", mock.Mock(), create=True) as lamp, \
             mock.patch.object(button_send, "play_idle_sound", return_value=True) as play, \
             mock.patch.object(button_send, "play_ringtone_snippet") as ringtone:
            self.runtime._command(item, NOW)
            self.apply_settings_on_button()
            self.runtime._finish_settings()
            self.assertFalse(button_send.maybe_play_cloud_sound())
            self.mono[0] += 3
            self.assertTrue(button_send.maybe_play_cloud_sound())
            self.assertFalse(button_send.maybe_play_cloud_sound())
        self.assertEqual(play.call_args.args[0], Path(__file__).resolve().parents[1] / "sounds/voices/dj/voice-msg-start.wav")
        ringtone.assert_not_called()
        lamp.off.assert_called_once()

    def test_volume_change_previews_the_ringtone_instead_of_the_saved_cue(self):
        self.runtime.heartbeat()
        item = self.settings_command(master_volume_percent=35)
        with self.settings_audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "quiet_hours", return_value=False), \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send, "play_idle_sound", return_value=True) as cue, \
             mock.patch.object(button_send, "play_ringtone_snippet", return_value=True) as snippet:
            self.runtime._command(item, NOW)
            self.apply_settings_on_button()
            self.runtime._finish_settings()
            self.mono[0] += 3
            self.assertTrue(button_send.maybe_play_cloud_sound())
        snippet.assert_called_once_with(button_send.VOLUME_PREVIEW_SECONDS)
        cue.assert_not_called()

    def test_ringtone_change_plays_the_whole_new_ringtone(self):
        self.runtime.heartbeat()
        item = self.settings_command(ringtone_id="island")
        with self.settings_audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "quiet_hours", return_value=False), \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send, "play_idle_sound", return_value=True) as cue, \
             mock.patch.object(button_send, "play_ringtone_snippet", return_value=True) as snippet:
            self.runtime._command(item, NOW)
            self.apply_settings_on_button()
            self.runtime._finish_settings()
            self.mono[0] += 3
            self.assertTrue(button_send.maybe_play_cloud_sound())
        snippet.assert_called_once_with(None)  # whole ringtone
        cue.assert_not_called()

    def test_settings_saved_noop_boot_adoption_and_local_echo_are_silent(self):
        self.runtime.heartbeat()
        with self.settings_audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "quiet_hours", return_value=False):
            # A revision-only Cloud change is acknowledged without a sound.
            self.runtime._command(self.settings_command(), NOW)
            self.apply_settings_on_button()
            self.runtime._finish_settings()
            # First adoption after boot is silent even if values changed.
            button_send._applied_volume_revision = None
            self.runtime._command(self.settings_command(master_volume_percent=40), NOW)
            self.apply_settings_on_button()
            self.runtime._finish_settings()
            # A box-originated revision echoed by Cloud has no remote-change intent.
            item = self.settings_command(master_volume_percent=60)
            desired = item["payload"]["settings"]
            self.runtime.settings.update({k: v for k, v in desired.items() if k not in {"version", "revision"}},
                                         item["payload"]["expected_revision"])
            self.runtime._command(item, NOW)
            self.apply_settings_on_button()
            self.runtime._finish_settings()
        self.assertEqual(self.runtime.state["pending_settings"], {})
        self.assertFalse(list(self.runtime.audio_requests.directory.glob("*.json")))

    def test_settings_saved_failed_apply_and_command_restart_preserve_order(self):
        self.runtime.heartbeat()
        item = self.settings_command(swoosh_sound_enabled=False)
        with self.settings_audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "quiet_hours", return_value=False):
            self.runtime._command(item, NOW)
            # Simulate a crash after settings mutation, before pending persistence.
            self.runtime.state["pending_settings"].clear()
            self.runtime._save()
            self.runtime = CloudRuntime(self.client, state_path=self.runtime.state_path,
                contacts_path=self.runtime.contacts.path, queue_dir=self.runtime.queue_dir,
                outbox_dir=self.runtime.outbox_dir, settings_path=self.runtime.settings.path,
                clock=self.runtime.clock, monotonic=lambda: self.mono[0], boot_id="test-boot")
            self.runtime._command(item, NOW)
            with mock.patch.object(button_send.subprocess, "run", return_value=types.SimpleNamespace(returncode=1)):
                self.assertFalse(button_send.apply_master_volume())
            self.runtime._finish_settings()
            self.assertFalse(list(self.runtime.audio_requests.directory.glob("*.json")))
            self.apply_settings_on_button()
            self.runtime._finish_settings()
            self.assertEqual(len(list(self.runtime.audio_requests.directory.glob("*.json"))), 1)

    def test_settings_saved_interrupted_press_keeps_original_deadline(self):
        self.runtime.heartbeat()
        with self.settings_audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "quiet_hours", return_value=False), \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send, "play_idle_sound", side_effect=[False, True]) as play:
            self.runtime._command(self.settings_command(swoosh_sound_enabled=False), NOW)
            self.apply_settings_on_button()
            self.runtime._finish_settings()
            self.mono[0] += 3
            self.assertFalse(button_send.maybe_play_cloud_sound())
            self.assertEqual(self.runtime.audio_requests.outcome("settings_saved:settings_operation_1"), "pending")
            self.mono[0] += 1
            self.assertTrue(button_send.maybe_play_cloud_sound())
            self.assertEqual(play.call_count, 2)

    def test_settings_saved_coalesces_across_restart_without_extending_deadline(self):
        self.runtime.heartbeat()
        with self.settings_audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "quiet_hours", return_value=False):
            for volume in (40, 60, 80):
                self.runtime._command(self.settings_command(master_volume_percent=volume), NOW)
                self.apply_settings_on_button()
                self.runtime._finish_settings()
                self.mono[0] += 0.5  # within the 1 s merge window
        requests = AudioRequests(self.runtime.audio_requests.directory, clock=lambda: NOW - 100,
                                monotonic=lambda: self.mono[0], boot_id="test-boot")
        self.assertEqual(len(list(requests.directory.glob("*.json"))), 1)
        with requests.owner():
            self.assertIsNone(requests.claim_next("a" * 64))
            self.mono[0] = 105
            request = requests.claim_next("a" * 64)
            self.assertEqual(request["kind"], "settings_saved")
            self.assertEqual(request["expires_mono"], 130)
            requests.finish(request, "played")
            self.assertIsNone(requests.claim_next("a" * 64))
        for revision in (1, 2, 3):
            requests.enqueue_settings_saved(f"settings_saved:settings_operation_{revision}", "a" * 64, 30)
        self.assertFalse(list(requests.directory.glob("*.json")))
        requests.enqueue_settings_saved("settings_saved:later_save", "a" * 64, 30)
        self.mono[0] += 3
        with requests.owner():
            self.assertIsNotNone(requests.claim_next("a" * 64))

    def test_settings_saved_defers_recording_guided_and_press_then_expires(self):
        self.runtime.heartbeat()
        with self.settings_audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "quiet_hours", return_value=False), \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send, "play_idle_sound", return_value=True) as play:
            self.runtime._command(self.settings_command(swoosh_sound_enabled=False), NOW)
            self.apply_settings_on_button()
            self.runtime._finish_settings()
            key = "settings_saved:settings_operation_1"
            self.mono[0] += 3
            for flag in ("_recording", "_guided_active"):
                with mock.patch.object(button_send, flag, True):
                    self.assertFalse(button_send.maybe_play_cloud_sound())
                    self.assertEqual(self.runtime.audio_requests.outcome(key), "pending")
            with mock.patch.object(button_send.button, "is_pressed", True):
                self.assertFalse(button_send.maybe_play_cloud_sound())
                self.assertEqual(self.runtime.audio_requests.outcome(key), "pending")
            play.assert_not_called()
            self.assertTrue(button_send.maybe_play_cloud_sound())
            self.runtime._command(self.settings_command(master_volume_percent=60), NOW)
            self.apply_settings_on_button()
            self.runtime._finish_settings()
            self.mono[0] += 30
            self.assertFalse(button_send.maybe_play_cloud_sound())
            self.assertEqual(self.runtime.audio_requests.outcome("settings_saved:settings_operation_2"), "expired")
            self.assertEqual(play.call_count, 1)

    def test_settings_saved_quiet_hours_drop_without_late_playback(self):
        self.runtime.heartbeat()
        with self.settings_audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send, "quiet_hours", return_value=True) as quiet, \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send, "play_idle_sound") as play:
            self.runtime._command(self.settings_command(master_volume_percent=40), NOW)
            self.apply_settings_on_button()
            self.runtime._finish_settings()
            self.assertFalse(list(self.runtime.audio_requests.directory.glob("*.json")))
            quiet.return_value = False
            self.runtime._command(self.settings_command(master_volume_percent=60), NOW)
            self.apply_settings_on_button()
            self.runtime._finish_settings()
            self.mono[0] += 3
            quiet.return_value = True
            self.assertFalse(button_send.maybe_play_cloud_sound())
            quiet.return_value = False
            self.assertFalse(button_send.maybe_play_cloud_sound())
            play.assert_not_called()

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

    def test_old_cloud_ringtones_migrate_replay_and_ack_after_restart(self):
        from messagebox.settings import LEGACY_RINGTONES
        self.runtime.heartbeat()
        for index, old_id in enumerate(sorted(LEGACY_RINGTONES)):
            with self.subTest(old_id=old_id):
                current, _ = self.runtime.settings.load()
                desired = {**current, "revision": current["revision"] + 1, "ringtone_id": old_id}
                operation_id = f"legacy_ringtone_operation_{index}"
                item = {"operation_id": operation_id, "sequence": index + 1,
                        "kind": "settings", "created_at": NOW, "expires_at": NOW + 100,
                        "payload": {"settings": desired, "expected_revision": current["revision"],
                                    "desired_revision": desired["revision"]}}
                self.runtime._command(item, NOW)
                saved, warning = self.runtime.settings.load()
                self.assertFalse(warning)
                self.assertEqual(saved, {**desired, "ringtone_id": "hello_piano"})
                # A crash between settings write and pending-state persistence
                # replays the same old command without a revision conflict.
                self.runtime.state["pending_settings"].pop(operation_id)
                self.runtime._command(item, NOW)
                # Also accept a pending intent saved by pre-update software.
                self.runtime.state["pending_settings"][operation_id] = desired
                self.runtime._save()
                restarted = CloudRuntime(self.client, state_path=self.runtime.state_path,
                    contacts_path=self.runtime.contacts.path, queue_dir=self.runtime.queue_dir,
                    outbox_dir=self.runtime.outbox_dir, settings_path=self.runtime.settings.path,
                    clock=self.runtime.clock, boot_id="test-boot")
                (self.root / "applied-settings.json").write_text(json.dumps({"revision": saved["revision"], "settings": saved}))
                restarted._finish_settings()
                restarted.flush_acks()
                self.assertEqual(self.client.acks[-1]["state"], "applied")
                self.assertNotIn(operation_id, restarted.state["pending_settings"])
                self.runtime = restarted

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
            button=types.SimpleNamespace(is_pressed=False), led=mock.Mock(), create=True)

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
        write_pcm_wav(ringtone_dir / "hello_piano.wav", 1)
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
        play.assert_called_once_with(ringtone_dir / "hello_piano.wav", 17.0, lamp_ringtone="hello_piano")
        self.runtime._finish_previews()
        self.assertEqual(json.loads(next(self.ack_dir.glob("*.json")).read_text())["state"], "applied")
        self.assertEqual(self.runtime.state["pending_previews"], {})

    def test_voice_preview_waits_for_owner_and_plays_requested_pack_with_lamp_off(self):
        self.runtime.heartbeat()
        item = {**self.preview(), "kind": "voice_preview", "payload": {"voice_pack": "robot"}}
        self.client.items = [item]
        with mock.patch.object(button_send.subprocess, "run") as poller_play:
            self.runtime.poll_once()
        poller_play.assert_not_called()
        with self.audio_owner(), mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64), \
             mock.patch.object(button_send.sound_pack, "SOUND_DIR", Path(__file__).resolve().parents[1] / "sounds"), \
             mock.patch.object(button_send, "led", mock.Mock(), create=True) as lamp, \
             mock.patch.object(button_send, "play_idle_sound", return_value=True) as play:
            with mock.patch.object(button_send, "_recording", True):
                self.assertFalse(button_send.maybe_play_cloud_sound())
            self.assertTrue(button_send.maybe_play_cloud_sound())
            self.assertFalse(button_send.maybe_play_cloud_sound())
        self.assertEqual(play.call_args.args[0], Path(__file__).resolve().parents[1] / "sounds/voices/robot/voice-msg-start.wav")
        self.assertEqual(play.call_args.kwargs, {})
        lamp.off.assert_called_once()
        self.runtime._finish_previews()
        self.assertEqual(json.loads(next(self.ack_dir.glob("*.json")).read_text())["state"], "applied")
        with self.assertRaises(CloudRuntimeError):
            self.runtime._command({**item, "operation_id": "bad-pack", "payload": {"voice_pack": "../bad"}}, NOW)

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
        write_pcm_wav(ringtone_dir / "hello_piano.wav", 19.8)
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
                                     str(ringtone_dir / "hello_piano.wav")])
        from messagebox.cloud_runtime import _ringtone_preview_timeout
        self.assertAlmostEqual(_ringtone_preview_timeout(ringtone_dir / "hello_piano.wav"), 24.8)

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
        write_pcm_wav(ringtone_dir / "hello_piano.wav", 19.8)
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
