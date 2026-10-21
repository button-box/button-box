import json
from contextlib import ExitStack, contextmanager
import sys
import tempfile
import threading
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

gpiozero = types.ModuleType("gpiozero")
gpiozero.Button = object
gpiozero.LED = object
with mock.patch.dict(sys.modules, {"gpiozero": gpiozero}):
    import messagebox.button_send as button_send  # noqa: E402
from messagebox.contacts import ContactStore  # noqa: E402
from messagebox.guided_reply import OutboxStore, RecordingResult  # noqa: E402
from messagebox import guided_reply  # noqa: E402
from messagebox.nfc import Announcer, NfcRuntime  # noqa: E402
from messagebox.nfc_state import AnnouncementStore, EnrollmentStore, NfcRouter, SelectionStore  # noqa: E402


GRANDMA = "15551234567@s.whatsapp.net"
FAMILY = "120363123456789@g.us"
CARD = "04:A1:00:FF"


class FakeLed:
    def off(self):
        pass


class ButtonRoutingTests(unittest.TestCase):
    def setUp(self):
        moments = mock.patch.object(button_send, "play_moment")
        moments.start()
        self.addCleanup(moments.stop)
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.root = root
        self.contacts_path = root / "contacts.json"
        self.selection_path = root / "nfc-selection.json"
        self.health_path = root / "nfc-health"
        self.announcement_path = root / "nfc-announcement.json"
        self.queue_path = root / "queue"
        self.queue_path.mkdir()
        self.contacts = ContactStore(self.contacts_path, clock=lambda: 1000)
        self.announcements = AnnouncementStore(
            self.announcement_path, clock=lambda: 1000
        )
        self.paths = (
            mock.patch.object(button_send, "CONTACTS_FILE", str(self.contacts_path)),
            mock.patch.object(
                button_send, "NFC_SELECTION_FILE", str(self.selection_path)
            ),
            mock.patch.object(button_send, "NFC_HEALTH_FILE", self.health_path),
            mock.patch.object(
                button_send, "nfc_announcement_store", self.announcements
            ),
            mock.patch.object(button_send, "QUEUE_DIR", str(self.queue_path)),
            mock.patch.object(button_send.time, "time", return_value=1000),
            mock.patch.object(button_send, "log"),
            mock.patch.object(button_send, "log_event"),
        )
        for patcher in self.paths:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.paths):
            patcher.stop()
        self.directory.cleanup()

    def add_grandma(self):
        return self.contacts.add_contact(
            GRANDMA,
            "Grandma",
            card_clip="/var/lib/messagebox/assets/grandma.wav",
            receive_after=0,
        )

    def mark_nfc_healthy(self):
        self.health_path.write_text("ready\n", encoding="ascii")

    def add_family(self):
        return self.contacts.add_contact(FAMILY, "Family", receive_after=0)

    def queue_incoming(self, name="0001-message.wav"):
        wav = self.queue_path / name
        wav.write_bytes(b"incoming audio")
        sidecar = Path(str(wav) + ".json")
        sidecar.write_text(
            json.dumps({"chat": FAMILY, "msgid": "message-1"}),
            encoding="utf-8",
        )
        return wav, sidecar

    def test_zero_one_and_multi_contact_routing(self):
        self.assertIsNone(button_send.current_recipient_context())
        self.assertEqual(button_send.routing_mode(), "no_contacts")

        self.add_grandma()
        sole = button_send.current_recipient_context()
        self.assertEqual(sole["contact"]["jid"], GRANDMA)
        self.assertFalse(sole["via_card"])
        self.assertEqual(button_send.routing_mode(), "default_recipient")

        self.contacts.assign_card(GRANDMA, CARD)
        self.mark_nfc_healthy()
        self.add_family()
        default = button_send.current_recipient_context()
        self.assertEqual(default["contact"]["jid"], GRANDMA)
        self.assertFalse(default["via_card"])
        selection = SelectionStore(self.selection_path)
        selection.select(CARD, GRANDMA, self.contacts.load()["revision"])
        selected = button_send.current_recipient_context()
        self.assertEqual(selected["contact"]["jid"], GRANDMA)
        self.assertTrue(selected["via_card"])
        self.assertIsNotNone(selection.load())
        self.assertEqual(button_send.routing_mode(), "default_recipient")

    def test_corrupt_contacts_fail_closed(self):
        self.contacts_path.write_text("not json", encoding="utf-8")
        self.assertIsNone(button_send.current_recipient_context())
        self.assertEqual(button_send.routing_mode(), "unavailable")

    def test_successful_claim_moves_to_recent_history_with_original_route(self):
        inflight = self.root / "queue" / ".inflight"
        inflight.mkdir(parents=True)
        claimed = inflight / "1000-message.wav"
        with wave.open(str(claimed), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * 8000)
        metadata = {"chat": FAMILY, "msgid": "synthetic", "media_type": "audio"}
        Path(f"{claimed}.json").write_text(json.dumps(metadata), encoding="utf-8")

        button_send.finish_claim(
            {"path": claimed, "meta": metadata, "played_at": 2_000_000_000}
        )

        archived = self.root / "queue" / ".played" / claimed.name
        self.assertTrue(archived.is_file())
        archived_metadata = json.loads(
            Path(f"{archived}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(archived_metadata["chat"], FAMILY)
        self.assertEqual(archived_metadata["msgid"], "synthetic")
        self.assertEqual(archived_metadata["played_at"], 2_000_000_000)

    def test_standalone_mode_never_claims_cloud_marked_audio(self):
        wav, sidecar = self.queue_incoming()
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        metadata["cloud"] = True
        sidecar.write_text(json.dumps(metadata), encoding="utf-8")

        with mock.patch.dict(button_send.os.environ, {"MSGBOX_TRANSPORT": "wacli"}):
            self.assertIsNone(button_send.claim_oldest())

        self.assertTrue(wav.exists())
        self.assertTrue(sidecar.exists())

    def test_cloud_mode_claims_only_fresh_authorized_audio(self):
        wav, _sidecar = self.queue_incoming()
        with mock.patch.dict(button_send.os.environ, {"MSGBOX_TRANSPORT": "cloud"}), \
             mock.patch.object(button_send.cloud_runtime, "playable", return_value=True) as playable:
            claim = button_send.claim_oldest()

        self.assertEqual(claim["path"].name, wav.name)
        playable.assert_called_once_with(claim["meta"])

    def test_legacy_play_skips_cloud_audio_and_plays_next_standalone_message(self):
        cloud_wav, cloud_sidecar = self.queue_incoming("0001-cloud.wav")
        cloud_metadata = json.loads(cloud_sidecar.read_text(encoding="utf-8"))
        cloud_metadata["cloud"] = True
        cloud_sidecar.write_text(json.dumps(cloud_metadata), encoding="utf-8")
        standalone_wav, _ = self.queue_incoming("0002-standalone.wav")

        with mock.patch.dict(button_send.os.environ, {"MSGBOX_TRANSPORT": "wacli"}), \
             mock.patch.object(button_send, "play_pending_listened"), \
             mock.patch.object(button_send, "play_moment") as cues, \
             mock.patch.object(button_send.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
             mock.patch.object(button_send, "archive_played_file") as archive, \
             mock.patch.object(button_send, "react_played"), \
             mock.patch.object(button_send, "wait_for_stable_open"), \
             mock.patch.object(button_send, "refresh_led"):
            button_send.play_next_legacy()

        self.assertEqual(Path(run.call_args.args[0][-1]), standalone_wav)
        self.assertEqual(archive.call_args.args[1], standalone_wav)
        self.assertTrue(cloud_wav.exists())
        self.assertTrue(cloud_sidecar.exists())
        self.assertEqual(cues.call_args_list, [mock.call("msg_start")])

    def test_guided_recheck_releases_cloud_claim_after_mode_switch(self):
        self.add_family()
        claim = {
            "path": self.queue_path / ".inflight" / "0001-cloud.wav",
            "meta": {"chat": FAMILY, "cloud": True},
        }
        session = mock.Mock()
        button_send.led = FakeLed()
        with mock.patch.dict(button_send.os.environ, {"MSGBOX_TRANSPORT": "wacli"}), \
             mock.patch.object(button_send, "claim_oldest", return_value=claim), \
             mock.patch.object(button_send, "GuidedSession", return_value=session), \
             mock.patch.object(button_send, "play_pending_listened"), \
             mock.patch.object(button_send, "release_claim") as release, \
             mock.patch.object(button_send, "mark_queue_known"), \
             mock.patch.object(button_send, "refresh_led"), \
             mock.patch.object(button_send, "quiet_hours", return_value=False), \
             mock.patch.object(button_send, "queued", return_value=[]):
            button_send.run_guided_once(
                {"max_recording_seconds": 60, "after_listening": "play_only"}
            )

        release.assert_called_once_with(claim)
        session.run.assert_not_called()

    def test_cards_block_default_when_reader_or_card_state_is_unsafe(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)

        self.assertIsNone(button_send.current_recipient_context())

        self.mark_nfc_healthy()
        self.announcements.put(action="unknown", uid="04:00:00:01", prompt="unknown.wav")
        self.assertIsNone(button_send.current_recipient_context())

        self.announcements.clear()
        safe_default = button_send.current_recipient_context()
        self.assertEqual(safe_default["contact"]["jid"], GRANDMA)
        self.assertFalse(safe_default["via_card"])

    def test_held_unknown_blocks_default_and_recent_after_announcement_is_consumed(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)
        self.add_family()
        self.mark_nfc_healthy()
        selection = SelectionStore(self.selection_path, clock=lambda: 1000)
        router = NfcRouter(self.contacts, selection, EnrollmentStore(self.root / "enrollment.json"), self.announcements)
        reader = NfcRuntime(router, Announcer(self.announcements))
        self.assertEqual(reader.observe("04:00:00:01", 0).action, "unknown")
        self.announcements.take()
        self.assertIsNone(reader.observe("04:00:00:01", 1))
        self.assertIsNone(self.announcements.pending_action())
        with mock.patch.object(button_send, "recent_reply_recipient", side_effect=AssertionError("recent route inspected")):
            self.assertIsNone(button_send.current_recipient_context())
            self.assertIsNone(button_send.recording_recipient_context())
            self.assertIsNone(reader.observe(None, 1.1))
            self.assertIsNone(button_send.recording_recipient_context())
        self.assertEqual(reader.observe(None, 2).action, "removed")
        self.assertFalse(selection.unknown_present())
        restored = button_send.recording_recipient_context()
        self.assertEqual(restored["contact"]["jid"], GRANDMA)
        self.assertFalse(restored["via_card"])

    def test_unknown_reader_marker_blocks_default_even_without_paired_cards(self):
        self.add_grandma()
        SelectionStore(self.selection_path).block_unknown()
        self.assertIsNone(button_send.recording_recipient_context())

    def test_unknown_marker_malformed_or_unavailable_never_permits_fallback(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)
        self.mark_nfc_healthy()
        selection = SelectionStore(self.selection_path)
        selection.unknown_path.write_text("malformed marker")
        self.assertIsNone(button_send.current_recipient_context())
        self.assertIsNone(button_send.recording_recipient_context())
        selection.clear_unknown()
        selection.unknown_path.symlink_to(self.root / "missing-unknown-target")
        self.assertIsNone(button_send.current_recipient_context())
        self.assertIsNone(button_send.recording_recipient_context())
        selection.clear_unknown()
        selection.unknown_path.mkdir()
        self.assertIsNone(button_send.current_recipient_context())
        self.assertIsNone(button_send.recording_recipient_context())
        selection.unknown_path.rmdir()
        with mock.patch.object(button_send.SelectionStore, "unknown_present", side_effect=PermissionError("unavailable")):
            self.assertIsNone(button_send.current_recipient_context())
            self.assertIsNone(button_send.recording_recipient_context())

    def test_short_legacy_press_plays_without_resolving_or_claiming(self):
        button_send.button = types.SimpleNamespace(is_pressed=False)
        with mock.patch.object(button_send, "wait_for_stable_open"), mock.patch.object(
            button_send, "play_next_legacy"
        ) as play, mock.patch.object(
            button_send, "beep"
        ), mock.patch.object(
            button_send, "log_event"
        ), mock.patch.object(
            button_send,
            "current_recipient_context",
            side_effect=AssertionError("recipient resolved on short press"),
        ):
            button_send.record_and_send_legacy()
        play.assert_called_once_with()

    def test_recently_played_sender_routes_recording_without_nfc(self):
        self.add_grandma()
        self.add_family()
        with mock.patch.object(
            button_send,
            "recent_reply_recipient",
            return_value=("route", FAMILY),
        ), mock.patch.object(
            button_send,
            "current_recipient_context",
            side_effect=AssertionError("default route inspected"),
        ):
            context = button_send.recording_recipient_context()

        self.assertEqual(context["contact"]["jid"], FAMILY)
        self.assertFalse(context["via_card"])
        self.assertTrue(context["via_recent_reply"])

    def test_expired_recent_sender_falls_back_to_configured_default(self):
        configured = {
            "contact": {"jid": GRANDMA, "label": "Grandma"},
            "via_card": False,
        }
        with mock.patch.object(
            button_send,
            "recent_reply_recipient",
            return_value=("fallback", None),
        ), mock.patch.object(
            button_send,
            "current_recipient_context",
            return_value=configured,
        ) as current:
            self.assertEqual(button_send.recording_recipient_context(), configured)

        current.assert_called_once_with()

    def test_invalid_recent_sender_blocks_without_default_fallback(self):
        self.add_grandma()
        with mock.patch.object(
            button_send,
            "recent_reply_recipient",
            return_value=("blocked", None),
        ), mock.patch.object(
            button_send,
            "current_recipient_context",
            side_effect=AssertionError("default route inspected"),
        ):
            self.assertIsNone(button_send.recording_recipient_context())

    def test_recent_sender_never_bypasses_unknown_card_or_nfc_health(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)
        self.add_family()
        self.mark_nfc_healthy()
        self.announcements.put(action="unknown", uid="04:00:00:01", prompt="unknown.wav")

        with mock.patch.object(
            button_send,
            "recent_reply_recipient",
            side_effect=AssertionError("recent route inspected"),
        ):
            self.assertIsNone(button_send.recording_recipient_context())

        self.announcements.clear()
        self.health_path.unlink()
        with mock.patch.object(
            button_send,
            "recent_reply_recipient",
            side_effect=AssertionError("recent route inspected"),
        ):
            self.assertIsNone(button_send.recording_recipient_context())

    def test_fresh_card_overrides_recently_played_sender(self):
        selected = {
            "contact": {"jid": GRANDMA, "label": "Grandma"},
            "uid": CARD,
            "via_card": True,
        }
        with mock.patch.object(
            button_send,
            "claim_fresh_card_intent",
            return_value=("claimed", selected),
        ), mock.patch.object(
            button_send,
            "acknowledge_and_classify_legacy_press",
            return_value="record",
        ), mock.patch.object(
            button_send,
            "recording_recipient_context",
            side_effect=AssertionError("recent route inspected"),
        ), mock.patch.object(
            button_send,
            "ensure_nfc_confirmation",
            return_value=False,
        ) as confirm:
            button_send.record_and_send_legacy({"max_recording_seconds": 60})

        confirm.assert_called_once_with(selected)

    def test_fresh_card_guided_send_preserves_queue_for_next_press(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)
        self.add_family()
        self.mark_nfc_healthy()
        selection = SelectionStore(self.selection_path)
        selection.select(CARD, GRANDMA, self.contacts.load()["revision"])
        wav, sidecar = self.queue_incoming()
        selected_context = button_send.current_recipient_context()
        original_wav = wav.read_bytes()
        original_sidecar = sidecar.read_text(encoding="utf-8")
        sessions = []

        class FakeSession:
            def run(self, **kwargs):
                sessions.append(kwargs)
                if kwargs["flow_kind"] == "standalone":
                    button_send.recording_session_event(
                        selected_context, "guided_approved"
                    )
                    return "approved"
                return "played"

        button_send.led = FakeLed()
        settings = {"max_recording_seconds": 60, "after_listening": "play_only"}
        with mock.patch.object(
            button_send, "ensure_nfc_confirmation", return_value=True
        ), mock.patch.object(
            button_send, "GuidedSession", return_value=FakeSession()
        ), mock.patch.object(
            button_send, "play_pending_listened"
        ), mock.patch.object(
            button_send, "mark_queue_known"
        ), mock.patch.object(
            button_send, "refresh_led"
        ), mock.patch.object(
            button_send, "quiet_hours", return_value=False
        ):
            button_send.run_guided_once(settings)

            self.assertEqual(sessions[0]["flow_kind"], "standalone")
            self.assertEqual(sessions[0]["recipient"], GRANDMA)
            self.assertIsNone(sessions[0]["incoming_path"])
            self.assertEqual(wav.read_bytes(), original_wav)
            self.assertEqual(sidecar.read_text(encoding="utf-8"), original_sidecar)
            self.assertIsNone(selection.load())
            self.assertTrue(selection.claimed_path.exists())

            # A held card refresh cannot rearm the consumed selection. The next
            # ordinary press therefore handles the original queued message.
            self.assertFalse(
                selection.select(
                    CARD,
                    GRANDMA,
                    self.contacts.load()["revision"],
                    new_presentation=False,
                )
            )
            button_send.run_guided_once(settings)

        self.assertEqual(sessions[1]["flow_kind"], "reply")
        self.assertEqual(sessions[1]["recipient"], FAMILY)
        self.assertEqual(Path(sessions[1]["incoming_path"]).name, wav.name)
        self.assertFalse(wav.exists())
        self.assertFalse(sidecar.exists())

    def test_stale_card_selection_does_not_preempt_guided_queue(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)
        self.add_family()
        self.mark_nfc_healthy()
        SelectionStore(self.selection_path, clock=lambda: 900).select(
            CARD, GRANDMA, self.contacts.load()["revision"]
        )
        wav, _sidecar = self.queue_incoming()
        sessions = []

        class FakeSession:
            def run(self, **kwargs):
                sessions.append(kwargs)
                return "played"

        button_send.led = FakeLed()
        settings = {"max_recording_seconds": 60, "after_listening": "play_only"}
        with mock.patch.object(
            button_send, "GuidedSession", return_value=FakeSession()
        ), mock.patch.object(
            button_send, "play_pending_listened"
        ), mock.patch.object(
            button_send, "mark_queue_known"
        ), mock.patch.object(
            button_send, "refresh_led"
        ), mock.patch.object(
            button_send, "quiet_hours", return_value=False
        ):
            button_send.run_guided_once(settings)

        self.assertEqual(sessions[0]["flow_kind"], "reply")
        self.assertEqual(sessions[0]["recipient"], FAMILY)
        self.assertEqual(Path(sessions[0]["incoming_path"]).name, wav.name)

    def test_message_voice_intro_reaches_guided_playback_only_when_offered(self):
        self.add_family()
        self.queue_incoming()
        sessions = []

        class FakeSession:
            def run(self, **kwargs):
                sessions.append(kwargs)
                return "played"

        button_send.led = FakeLed()
        settings = {"max_recording_seconds": 60, "after_listening": "play_only",
                    "announce_messages_voice": True}
        intro = Path("/synthetic/voice-msg-start.wav")
        with mock.patch.object(button_send, "GuidedSession", return_value=FakeSession()), \
             mock.patch.object(button_send, "play_pending_listened"), \
             mock.patch.object(button_send, "mark_queue_known"), \
             mock.patch.object(button_send, "refresh_led"), \
             mock.patch.object(button_send, "quiet_hours", return_value=False), \
             mock.patch.object(button_send, "message_intro_voice", return_value=intro) as offered:
            button_send.run_guided_once(settings)
        offered.assert_called_once_with(settings)
        self.assertEqual(sessions[0]["incoming_cue_path"], str(button_send.sound_pack.cue_path("msg_start")))
        self.assertEqual(sessions[0]["incoming_voice_path"], str(intro))

    def test_message_age_line_reaches_guided_playback_after_the_intro(self):
        self.add_family()
        self.queue_incoming()
        sessions = []

        class FakeSession:
            def run(self, **kwargs):
                sessions.append(kwargs)
                return "played"

        button_send.led = FakeLed()
        settings = {"max_recording_seconds": 60, "after_listening": "play_only"}
        age = Path("/synthetic/voice-age-last-night.wav")
        with mock.patch.object(button_send, "GuidedSession", return_value=FakeSession()), \
             mock.patch.object(button_send, "play_pending_listened"), \
             mock.patch.object(button_send, "mark_queue_known"), \
             mock.patch.object(button_send, "refresh_led"), \
             mock.patch.object(button_send, "quiet_hours", return_value=False), \
             mock.patch.object(button_send, "message_age_voice", return_value=age) as offered:
            button_send.run_guided_once(settings)
        self.assertEqual(offered.call_args.args[1], settings)
        self.assertIsInstance(offered.call_args.args[0], dict)
        self.assertEqual(sessions[0]["incoming_age_path"], str(age))

    def test_wacli_guided_playback_keeps_cue_only_even_with_voice_intro_setting(self):
        self.add_family()
        self.queue_incoming()
        sessions = []

        class FakeSession:
            def run(self, **kwargs):
                sessions.append(kwargs)
                return "played"

        button_send.led = FakeLed()
        settings = {"max_recording_seconds": 60, "after_listening": "play_only",
                    "announce_messages_voice": True}
        with mock.patch.dict(button_send.os.environ, {"MSGBOX_TRANSPORT": "wacli"}), \
             mock.patch.object(button_send, "GuidedSession", return_value=FakeSession()), \
             mock.patch.object(button_send, "play_pending_listened"), \
             mock.patch.object(button_send, "mark_queue_known"), \
             mock.patch.object(button_send, "refresh_led"), \
             mock.patch.object(button_send, "quiet_hours", return_value=False):
            button_send.run_guided_once(settings)
        self.assertEqual(sessions[0]["flow_kind"], "reply")
        self.assertIsNone(sessions[0]["incoming_voice_path"])
        self.assertIsNone(sessions[0]["incoming_age_path"])

    def test_cancelled_guided_card_intent_keeps_selection_and_queue(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)
        self.add_family()
        self.mark_nfc_healthy()
        selection = SelectionStore(self.selection_path)
        selection.select(CARD, GRANDMA, self.contacts.load()["revision"])
        wav, sidecar = self.queue_incoming()

        class CancelledSession:
            def run(self, **_kwargs):
                return "deleted"

        button_send.led = FakeLed()
        with mock.patch.object(
            button_send, "ensure_nfc_confirmation", return_value=True
        ), mock.patch.object(
            button_send, "GuidedSession", return_value=CancelledSession()
        ), mock.patch.object(
            button_send, "play_pending_listened"
        ), mock.patch.object(
            button_send, "mark_queue_known"
        ), mock.patch.object(
            button_send, "refresh_led"
        ), mock.patch.object(
            button_send, "quiet_hours", return_value=False
        ), mock.patch.object(button_send, "ring_alert"):
            button_send.run_guided_once(
                {"max_recording_seconds": 60, "after_listening": "play_only"}
            )

        self.assertTrue(wav.exists())
        self.assertTrue(sidecar.exists())
        self.assertIsNotNone(selection.load())
        self.assertFalse(selection.claimed_path.exists())

    def test_tap_review_standalone_uses_recent_sender_without_claiming_queue(self):
        self.add_grandma()
        self.add_family()
        recent = {
            "contact": {"jid": FAMILY, "label": "Family"},
            "via_card": False,
            "via_recent_reply": True,
        }
        captured = {}

        class FakeSession:
            def run(self, **kwargs):
                captured.update(kwargs)
                return "sent"

        button_send.led = FakeLed()
        with mock.patch.object(
            button_send, "claim_oldest", return_value=None
        ), mock.patch.object(
            button_send, "recording_recipient_context", return_value=recent
        ), mock.patch.object(
            button_send, "GuidedSession", return_value=FakeSession()
        ), mock.patch.object(
            button_send, "play_pending_listened"
        ), mock.patch.object(
            button_send, "mark_queue_known"
        ), mock.patch.object(
            button_send, "refresh_led"
        ), mock.patch.object(
            button_send, "quiet_hours", return_value=False
        ):
            button_send.run_guided_once(
                {"max_recording_seconds": 60, "after_listening": "play_only"}
            )

        self.assertEqual(captured["flow_kind"], "standalone")
        self.assertEqual(captured["recipient"], FAMILY)
        self.assertIsNone(captured["incoming_path"])

    def test_card_claim_race_fails_closed_without_claiming_queue(self):
        self.selection_path.write_text("{}", encoding="utf-8")
        selected = {
            "contact": {"jid": GRANDMA, "label": "Grandma"},
            "uid": CARD,
            "via_card": True,
        }
        with mock.patch.object(
            button_send,
            "current_recipient_context",
            side_effect=[selected, None],
        ), mock.patch.object(
            button_send,
            "claim_oldest",
            side_effect=AssertionError("queue was claimed"),
        ), mock.patch.object(button_send, "block_unavailable_recipient") as blocked:
            button_send.run_guided_once(
                {"max_recording_seconds": 60, "after_listening": "play_only"}
            )

        blocked.assert_called_once_with()

    def test_selected_card_quick_release_never_plays_legacy_queue(self):
        self.add_grandma()
        self.contacts.assign_card(GRANDMA, CARD)
        self.add_family()
        self.mark_nfc_healthy()
        selection = SelectionStore(self.selection_path)
        selection.select(CARD, GRANDMA, self.contacts.load()["revision"])
        wav, sidecar = self.queue_incoming()

        with mock.patch.object(
            button_send, "acknowledge_and_classify_legacy_press", return_value="play"
        ), mock.patch.object(button_send, "wait_for_stable_open"), mock.patch.object(
            button_send, "play_next_legacy"
        ) as play:
            button_send.record_and_send_legacy({"max_recording_seconds": 60})

        play.assert_not_called()
        self.assertTrue(wav.exists())
        self.assertTrue(sidecar.exists())
        self.assertIsNotNone(selection.load())
        self.assertFalse(selection.claimed_path.exists())

    def test_missing_or_corrupt_legacy_recipient_sidecar_blocks(self):
        wav = Path(self.directory.name) / "legacy.wav"
        wav.write_bytes(b"audio")
        self.assertIsNone(button_send.legacy_job_recipient(str(wav)))
        Path(str(wav) + ".json").write_text("not json", encoding="utf-8")
        self.assertIsNone(button_send.legacy_job_recipient(str(wav)))
        Path(str(wav) + ".json").write_text(
            json.dumps({"recipient": GRANDMA}), encoding="utf-8"
        )
        self.assertEqual(button_send.legacy_job_recipient(str(wav)), GRANDMA)

    def test_exact_guided_reply_never_resolves_or_claims_a_card(self):
        self.contacts.add_contact(FAMILY, "Family")
        incoming = Path(self.directory.name) / "incoming.wav"
        claim = {"path": incoming, "meta": {"chat": FAMILY}}
        captured = {}

        class FakeSession:
            def run(self, **kwargs):
                captured.update(kwargs)
                return "played"

        button_send.led = FakeLed()
        with mock.patch.object(button_send, "claim_oldest", return_value=claim), mock.patch.object(
            button_send,
            "current_recipient_context",
            side_effect=AssertionError("exact reply resolved a contact"),
        ), mock.patch.object(
            button_send,
            "recording_recipient_context",
            side_effect=AssertionError("exact reply resolved recent history"),
        ), mock.patch.object(
            button_send, "ensure_nfc_confirmation", side_effect=AssertionError("card confirmed")
        ), mock.patch.object(
            button_send, "GuidedSession", return_value=FakeSession()
        ), mock.patch.object(
            button_send, "play_pending_listened"
        ), mock.patch.object(
            button_send, "finish_claim"
        ) as finish, mock.patch.object(
            button_send, "queued", return_value=[]
        ), mock.patch.object(
            button_send, "mark_queue_known"
        ), mock.patch.object(
            button_send, "refresh_led"
        ):
            button_send.run_guided_once()

        self.assertEqual(captured["recipient"], FAMILY)
        self.assertEqual(captured["flow_kind"], "reply")
        self.assertEqual(captured["incoming_path"], str(incoming))
        finish.assert_called_once_with(claim)


class CardSelectionLifecycleTests(unittest.TestCase):
    @contextmanager
    def session(self, mode, talk):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            now = [1000.0]
            contacts_path = root / "contacts.json"
            contacts = ContactStore(contacts_path)
            contacts.add_contact(GRANDMA, "Card person")
            contacts.add_contact(FAMILY, "Recent sender")
            contacts.assign_card(GRANDMA, CARD)
            contacts.assign_card(FAMILY, "04:B2:00:FF")
            selection_type = button_send.SelectionStore
            selection = selection_type(root / "selection.json", clock=lambda: now[0])
            selection.select(CARD, GRANDMA, contacts.load()["revision"])
            health = root / "health"
            health.touch()
            store = OutboxStore(root / "outbox", transport=mode, selection_path=selection.path)
            audio = root / "recording.wav"
            io = mock.Mock()
            io.wait_for_approval.return_value = True
            io.delete.side_effect = lambda path: Path(path).unlink(missing_ok=True)

            def record():
                audio.write_bytes(b"synthetic recording")
                return RecordingResult(str(audio), 2.0, True, False, 2.0)

            io.record.side_effect = record
            stack.enter_context(mock.patch.dict(button_send.os.environ, {"MSGBOX_TRANSPORT": mode}))
            for name, value in {
                "CONTACTS_FILE": contacts_path, "NFC_SELECTION_FILE": selection.path,
                "NFC_HEALTH_FILE": health, "outbox_store": store,
                "button": mock.Mock(is_pressed=True), "led": FakeLed(),
            }.items():
                stack.enter_context(mock.patch.object(button_send, name, value, create=True))
            stack.enter_context(mock.patch.object(button_send, "SelectionStore", side_effect=lambda path: selection_type(path, clock=lambda: now[0])))
            stack.enter_context(mock.patch.object(button_send.time, "time", side_effect=lambda: now[0]))
            stack.enter_context(mock.patch.object(button_send, "PiGuidedIO", return_value=io))
            stack.enter_context(mock.patch.object(button_send, "recent_reply_recipient", return_value=("route", FAMILY)))
            scope = stack.enter_context(mock.patch.object(button_send.cloud_runtime, "account_scope", return_value="a" * 64))
            stack.enter_context(mock.patch.object(button_send.CloudDeviceClient, "from_environment", side_effect=AssertionError("unexpected Cloud client")))
            for name, value in {
                "claim_oldest": None, "ensure_nfc_confirmation": True,
                "acknowledge_and_classify_legacy_press": "record", "queued": [],
                "quiet_hours": False,
            }.items():
                stack.enter_context(mock.patch.object(button_send, name, return_value=value))
            for name in ("log", "log_event", "mark_queue_known", "refresh_led", "cleanup_temp_recordings", "ring_alert", "wait_for_stable_open", "play_next_legacy"):
                stack.enter_context(mock.patch.object(button_send, name))
            settings = {"max_recording_seconds": 60, "after_listening": "invite_reply", "review_before_send": True}
            run = button_send.record_and_send_legacy if talk == "hold" else button_send.run_guided_once
            yield types.SimpleNamespace(
                run=lambda: run(settings), io=io, record=record, audio=audio,
                store=store, selection=selection, contacts=contacts, now=now,
            )
            if mode == "wacli":
                scope.assert_not_called()

    def assert_card_job(self, session, mode):
        jobs = session.store.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].recipient, GRANDMA)
        self.assertEqual(jobs[0].transport, mode)
        self.assertEqual(jobs[0].account_scope, "a" * 64 if mode == "cloud" else None)
        self.assertIsNone(session.selection.load())
        self.assertTrue(session.selection.claimed_path.exists())

    def test_unsent_attempt_then_record_uses_card_in_both_modes(self):
        for mode in ("wacli", "cloud"):
            for talk in ("hold", "tap"):
                for failure in ("short_press", "short_audio", "empty", "cancel", "timeout", "audio_error", "crash", "queue_error"):
                    if failure == "short_press" and talk == "tap":
                        continue
                    with self.subTest(mode=mode, talk=talk, failure=failure), self.session(mode, talk) as s:
                        original = s.selection.load()
                        s.now[0] += 5
                        if failure == "short_press":
                            with mock.patch.object(button_send, "acknowledge_and_classify_legacy_press", return_value="play"):
                                s.run()
                            s.io.record.assert_not_called()
                            button_send.play_next_legacy.assert_not_called()
                        elif failure in {"audio_error", "crash"}:
                            error = RuntimeError if failure == "audio_error" else SystemExit
                            s.io.record.side_effect = error("synthetic failure")
                            with self.assertRaises(error):
                                s.run()
                        elif failure == "queue_error":
                            with mock.patch.object(s.store, "approve", side_effect=OSError("synthetic queue failure")):
                                with self.assertRaises(OSError):
                                    s.run()
                        else:
                            s.io.wait_for_approval.return_value = failure != "cancel"
                            if failure != "cancel":
                                result = {
                                    "short_audio": RecordingResult(str(s.audio), 0.2, True, False, 0.2),
                                    "empty": RecordingResult(None, 2, False, False, 2),
                                    "timeout": RecordingResult(str(s.audio), 60, True, True, 60),
                                }[failure]
                                s.io.record.side_effect = lambda: (s.record(), result)[1]
                            s.run()
                        self.assertEqual(s.store.jobs(), [])
                        self.assertEqual(s.selection.load(), original)
                        self.assertFalse(s.selection.claimed_path.exists())
                        # Reopen the persisted state as a restarted service would.
                        self.assertEqual(SelectionStore(s.selection.path, clock=lambda: s.now[0]).load(), original)
                        s.io.record.side_effect = s.record
                        s.io.wait_for_approval.return_value = True
                        s.run()
                        self.assert_card_job(s, mode)
                        self.assertFalse(s.selection.select(CARD, GRANDMA, s.contacts.load()["revision"], new_presentation=False))
                        s.run()
                        self.assertEqual(sorted(job.recipient for job in s.store.jobs()), sorted([GRANDMA, FAMILY]))

    def test_expiry_uses_original_time_and_restores_normal_routing(self):
        for mode in ("wacli", "cloud"):
            for talk in ("hold", "tap"):
                with self.subTest(mode=mode, talk=talk), self.session(mode, talk) as s:
                    s.io.wait_for_approval.return_value = False
                    s.now[0] += 29
                    s.selection.select(CARD, GRANDMA, s.contacts.load()["revision"], new_presentation=False)
                    s.run()
                    self.assertEqual(s.selection.load()["selected_at"], 1000)
                    s.now[0] += 2
                    s.io.wait_for_approval.return_value = True
                    s.run()
                    self.assertEqual([job.recipient for job in s.store.jobs()], [FAMILY])
                    self.assertFalse(s.selection.select(CARD, GRANDMA, s.contacts.load()["revision"], new_presentation=False))

    def test_last_scan_wins_and_older_send_cannot_clear_new_scan(self):
        for mode in ("wacli", "cloud"):
            for talk in ("hold", "tap"):
                for during_recording in (False, True):
                    with self.subTest(mode=mode, talk=talk, during_recording=during_recording), self.session(mode, talk) as s:
                        def scan_next():
                            s.selection.select("04:B2:00:FF", FAMILY, s.contacts.load()["revision"])
                            return s.record()
                        if during_recording:
                            s.io.record.side_effect = scan_next
                        else:
                            scan_next()
                        s.run()
                        self.assertEqual([job.recipient for job in s.store.jobs()], [GRANDMA if during_recording else FAMILY])
                        if during_recording:
                            self.assertEqual(s.selection.load()["jid"], FAMILY)
                            s.io.record.side_effect = s.record
                            s.run()
                            self.assertEqual(len(s.store.jobs()), 2)
                        self.assertIsNone(s.selection.load())

    def test_invalid_selection_blocks_repeated_presses_in_both_modes(self):
        for mode in ("wacli", "cloud"):
            for talk in ("hold", "tap"):
                for invalid in ("mapping", "corrupt", "health"):
                    with self.subTest(mode=mode, talk=talk, invalid=invalid), self.session(mode, talk) as s:
                        if invalid == "mapping":
                            s.contacts.clear_cards(GRANDMA)
                        elif invalid == "corrupt":
                            s.selection.path.write_text("not json")
                        else:
                            button_send.NFC_HEALTH_FILE.unlink()
                        with mock.patch.object(button_send, "block_unavailable_recipient") as blocked:
                            s.run()
                            s.run()
                        self.assertEqual(blocked.call_count, 2)
                        s.io.record.assert_not_called()
                        self.assertEqual(s.store.jobs(), [])

    def test_cleanup_failure_after_queue_still_consumes_card(self):
        for mode in ("wacli", "cloud"):
            for talk in ("hold", "tap"):
                with self.subTest(mode=mode, talk=talk), self.session(mode, talk) as s:
                    s.io.delete.side_effect = OSError("synthetic cleanup failure")
                    with self.assertRaises(OSError):
                        s.run()
                    self.assert_card_job(s, mode)


    def test_queue_commit_crashes_reconcile_before_the_next_recording(self):
        for mode in ("wacli", "cloud"):
            for talk in ("hold", "tap"):
                for crash in ("before_queue_commit", "queue_commit", "before_consumption", "after_consumption"):
                    with self.subTest(mode=mode, talk=talk, crash=crash), self.session(mode, talk) as s:
                        original = s.selection.load()
                        selection_type = type(s.selection)
                        if crash in {"before_queue_commit", "queue_commit"}:
                            real_fsync = guided_reply._fsync_dir

                            def stop_after_commit(path):
                                real_fsync(path)
                                if Path(path) == s.store.root or (crash == "before_queue_commit" and Path(path).name.endswith(".tmp")):
                                    raise SystemExit("crash after durable queue commit")

                            patcher = mock.patch.object(guided_reply, "_fsync_dir", side_effect=stop_after_commit)
                        else:
                            real_consume = selection_type.consume

                            def stop_at_consumption(store, snapshot):
                                if crash == "after_consumption":
                                    real_consume(store, snapshot)
                                raise SystemExit("crash at selection consumption")

                            patcher = mock.patch.object(selection_type, "consume", new=stop_at_consumption)
                        with patcher, self.assertRaises(SystemExit):
                            s.run()
                        jobs = s.store.jobs()
                        self.assertEqual(len(jobs), 0 if crash == "before_queue_commit" else 1)
                        if jobs:
                            metadata = json.loads((jobs[0].path / "job.json").read_text())
                            self.assertEqual(metadata["nfc_selection_id"], original["selection_id"])
                            self.assertEqual(jobs[0].recipient, GRANDMA)
                        self.assertEqual(s.selection.load() is None, crash == "after_consumption")
                        restarted = OutboxStore(s.store.root, transport=mode, selection_path=s.selection.path)
                        restarted.recover_startup()

                        def record_after_recovery():
                            if crash == "before_queue_commit":
                                self.assertEqual(s.selection.load(), original)
                            else:
                                self.assertIsNone(s.selection.load(), "input preceded reconciliation")
                            return s.record()

                        s.io.record.side_effect = record_after_recovery
                        with mock.patch.object(button_send, "outbox_store", restarted):
                            s.run()
                        expected = [GRANDMA] if crash == "before_queue_commit" else [GRANDMA, FAMILY]
                        self.assertEqual(sorted(job.recipient for job in restarted.jobs()), sorted(expected))

    def test_button_startup_reconciles_before_constructing_the_button(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.session(mode, "tap") as s:
                with mock.patch.object(type(s.selection), "consume", side_effect=SystemExit), self.assertRaises(SystemExit):
                    s.run()

                def button_after_recovery(*_args):
                    self.assertIsNone(s.selection.load())
                    raise SystemExit("button constructed after recovery")

                with ExitStack() as stack:
                    for name, value in {"OUTBOX_DIR": str(s.store.root), "QUEUE_DIR": str(s.store.root.parent / "queue"),
                                        "TEMP_DIR": str(s.store.root.parent / "temp")}.items():
                        stack.enter_context(mock.patch.object(button_send, name, value))
                    stack.enter_context(mock.patch.object(button_send, "Button", side_effect=button_after_recovery))
                    stack.enter_context(mock.patch.object(button_send, "ReceiptStore", return_value=mock.Mock(recover_inflight=lambda: 0)))
                    stack.enter_context(mock.patch.object(button_send.threading, "Thread"))
                    stack.enter_context(mock.patch.object(button_send.sys, "argv", ["button_send.py"]))
                    stack.enter_context(mock.patch.dict(button_send.os.environ, {"MSGBOX_CLAIM_ONLY": "0"}))
                    for name in ("validate_sounds", "validate_prompts", "recover_inflight"):
                        stack.enter_context(mock.patch.object(button_send, name))
                    with self.assertRaisesRegex(SystemExit, "button constructed after recovery"):
                        button_send.main()
                self.assertEqual(len(s.store.jobs()), 1)

    def test_sender_deletion_between_queue_commit_and_recorder_return_is_safe(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.session(mode, "tap") as s:
                real_fsync = guided_reply._fsync_dir

                def sender_finishes_before_recorder_returns(path):
                    real_fsync(path)
                    if Path(path) == s.store.root:
                        # complete() fsyncs this directory too; restore the real
                        # boundary while simulating the sender's cleanup turn.
                        with mock.patch.object(guided_reply, "_fsync_dir", side_effect=real_fsync):
                            s.store.complete(s.store.jobs()[0])
                        raise SystemExit("recorder crashed after sender cleanup")

                with mock.patch.object(guided_reply, "_fsync_dir", side_effect=sender_finishes_before_recorder_returns), self.assertRaises(SystemExit):
                    s.run()
                self.assertEqual(s.store.jobs(), [])
                self.assertIsNone(s.selection.load())
                s.run()
                self.assertEqual([job.recipient for job in s.store.jobs()], [FAMILY])

    def test_reconciliation_failure_blocks_input_and_job_deletion(self):
        for mode in ("wacli", "cloud"):
            for talk in ("hold", "tap"):
                with self.subTest(mode=mode, talk=talk), self.session(mode, talk) as s:
                    with mock.patch.object(type(s.selection), "consume", side_effect=SystemExit), self.assertRaises(SystemExit):
                        s.run()
                    job = s.store.jobs()[0]
                    s.io.record.reset_mock()
                    with mock.patch.object(guided_reply.SelectionStore, "consume_id", side_effect=PermissionError("synthetic consumption failure")):
                        with self.assertRaises(PermissionError):
                            s.run()
                        with self.assertRaises(PermissionError):
                            s.store.complete(job)
                        self.assertFalse(button_send.handle_confirmed_press(0))
                    s.io.record.assert_not_called()
                    self.assertTrue(job.path.exists())
                    self.assertIsNotNone(s.selection.load())
                    s.store.complete(job)
                    self.assertFalse(job.path.exists())
                    self.assertIsNone(s.selection.load())

    def test_old_committed_job_cannot_consume_a_new_presentation(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.session(mode, "tap") as s:
                with mock.patch.object(type(s.selection), "consume", side_effect=SystemExit), self.assertRaises(SystemExit):
                    s.run()
                # Same card, same wall time: only the presentation ID differs.
                s.selection.select(CARD, GRANDMA, s.contacts.load()["revision"])
                replacement = s.selection.load()
                s.store.reconcile_card_selections()
                s.store.complete(s.store.jobs()[0])
                self.assertEqual(s.selection.load(), replacement)

    def test_unknown_writer_before_reader_never_uses_default_or_recent_sender(self):
        for mode in ("wacli", "cloud"):
            for resolver in (button_send.current_recipient_context, button_send.recording_recipient_context):
                with self.subTest(mode=mode, resolver=resolver.__name__), self.session(mode, "tap") as s:
                    published, reader_entered, release = threading.Event(), threading.Event(), threading.Event()
                    errors, results = [], []
                    selection_type = type(s.selection)
                    namespace = selection_type.block_unknown.__globals__
                    real_write, real_resolve = namespace["_atomic_json"], selection_type.resolve

                    def publish_then_pause(path, payload):
                        real_write(path, payload)
                        if Path(path) == s.selection.unknown_path:
                            published.set()
                            if not release.wait(3):
                                raise RuntimeError("reader did not enter")

                    def read_state(store, *args, **kwargs):
                        reader_entered.set()
                        return real_resolve(store, *args, **kwargs)

                    def writer():
                        try:
                            # The reader service has not published its announcement.
                            s.selection.block_unknown("04:FF:00:FF")
                        except BaseException as exc:
                            errors.append(exc)

                    def reader():
                        try:
                            results.append(resolver())
                        except BaseException as exc:
                            errors.append(exc)

                    with mock.patch.dict(namespace, {"_atomic_json": publish_then_pause}), mock.patch.object(selection_type, "resolve", new=read_state):
                        writer_thread = threading.Thread(target=writer)
                        reader_thread = threading.Thread(target=reader)
                        writer_thread.start()
                        try:
                            self.assertTrue(published.wait(3))
                            self.assertTrue(s.selection.path.exists())  # The old route still exists.
                            reader_thread.start()
                            self.assertTrue(reader_entered.wait(3))
                        finally:
                            release.set()
                            writer_thread.join(3)
                            if reader_thread.ident is not None:
                                reader_thread.join(3)
                    self.assertFalse(writer_thread.is_alive())
                    self.assertFalse(reader_thread.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(results, [None])
                    self.assertEqual(s.selection.resolve(s.contacts.load())[0], "blocked")

    def test_unknown_writer_between_block_and_selection_read_is_serialized(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.session(mode, "tap") as s:
                at_selection_read, writer_attempted = threading.Event(), threading.Event()
                errors = []
                selection_type = type(s.selection)
                real_load = selection_type._load

                def pause_read(store, **kwargs):
                    at_selection_read.set()
                    self.assertTrue(writer_attempted.wait(3))
                    return real_load(store, **kwargs)

                def writer():
                    try:
                        if not at_selection_read.wait(3):
                            raise RuntimeError("reader did not inspect selection")
                        writer_attempted.set()
                        s.selection.block_unknown("04:FF:00:FF")
                    except BaseException as exc:
                        errors.append(exc)

                thread = threading.Thread(target=writer)
                with mock.patch.object(selection_type, "_load", new=pause_read):
                    thread.start()
                    try:
                        context = button_send.current_recipient_context()
                    finally:
                        thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(context["via_card"])
                self.assertEqual(context["contact"]["jid"], GRANDMA)
                self.assertIsNone(button_send.current_recipient_context())

    def test_unknown_scan_during_history_lookup_blocks_fallback(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.session(mode, "tap") as s:
                s.selection.clear()

                def lookup(*_args):
                    s.selection.block_unknown("04:FF:00:FF")
                    return "route", FAMILY

                with mock.patch.object(button_send, "recent_reply_recipient", side_effect=lookup):
                    self.assertIsNone(button_send.recording_recipient_context())

    def test_contacts_refresh_preserves_expiry_and_changed_mapping_stays_blocked(self):
        for mode in ("wacli", "cloud"):
            for changed_person in (False, "reassigned", "unpaired"):
                with self.subTest(mode=mode, changed_person=changed_person), self.session(mode, "tap") as s:
                    initial = s.selection.load()
                    announcements = AnnouncementStore(s.selection.path.parent / "announcements.json")
                    router = NfcRouter(s.contacts, s.selection, EnrollmentStore(s.selection.path.parent / "enrollment.json"), announcements)
                    reader = NfcRuntime(router, Announcer(announcements))
                    reader.observe(CARD, 0)
                    initial = s.selection.load()
                    s.now[0] = 1029
                    if changed_person == "reassigned":
                        s.contacts.assign_card(FAMILY, CARD)
                    elif changed_person == "unpaired":
                        s.contacts.clear_cards(GRANDMA)
                    else:
                        s.contacts.add_contact("15551234569@s.whatsapp.net", "Another person")
                    reader.observe(CARD, 1)
                    if changed_person:
                        self.assertIsNone(button_send.current_recipient_context())
                        s.contacts.assign_card(GRANDMA, CARD)
                        reader.observe(CARD, 2)
                        self.assertIsNone(button_send.current_recipient_context())
                        reader.observe(None, 3)
                        self.assertIsNone(button_send.current_recipient_context())
                        reader.observe(CARD, 4)
                        self.assertEqual(button_send.current_recipient_context()["contact"]["jid"], GRANDMA)
                    else:
                        refreshed = s.selection.load()
                        self.assertEqual(refreshed["selected_at"], initial["selected_at"])
                        self.assertEqual(refreshed["selection_id"], initial["selection_id"])
                        self.assertEqual(refreshed["contacts_revision"], s.contacts.load()["revision"])
                        s.now[0] = 1031
                        s.run()
                        self.assertEqual([job.recipient for job in s.store.jobs()], [FAMILY])

    def test_changed_mapping_blocks_after_expiry_before_reader_refresh(self):
        for mode in ("wacli", "cloud"):
            for talk in ("hold", "tap"):
                for change in ("reassigned", "unpaired"):
                    with self.subTest(mode=mode, talk=talk, change=change), self.session(mode, talk) as s:
                        announcements = AnnouncementStore(s.selection.path.parent / "announcements.json")
                        router = NfcRouter(s.contacts, s.selection, EnrollmentStore(s.selection.path.parent / "enrollment.json"), announcements)
                        reader = NfcRuntime(router, Announcer(announcements))
                        reader.observe(CARD, 0)
                        s.now[0] = 1029
                        if change == "reassigned":
                            s.contacts.assign_card(FAMILY, CARD)
                        else:
                            s.contacts.clear_cards(GRANDMA)
                        # The button resolves the expired choice before the
                        # held card gets its next reader refresh.
                        s.now[0] = 1031
                        self.assertIsNone(button_send.current_recipient_context())
                        self.assertIsNone(button_send.recording_recipient_context())
                        self.assertFalse(s.selection.claimed_path.exists())
                        with mock.patch.object(button_send, "block_unavailable_recipient") as blocked:
                            s.run()
                            # Repair the mapping before the reader refresh, so
                            # only the resolver can retain the invalid state.
                            s.contacts.assign_card(GRANDMA, CARD)
                            self.assertIsNone(button_send.current_recipient_context())
                            self.assertIsNone(button_send.recording_recipient_context())
                            reader.observe(CARD, 1)
                            s.run()
                            reader.observe(None, 2)
                            s.run()
                        self.assertEqual(blocked.call_count, 3)
                        s.io.record.assert_not_called()
                        self.assertEqual(s.store.jobs(), [])
                        reader.observe(CARD, 3)
                        context = button_send.current_recipient_context()
                        self.assertTrue(context["via_card"])
                        self.assertEqual(context["contact"]["jid"], GRANDMA)
                        s.run()
                        self.assert_card_job(s, mode)


if __name__ == "__main__":
    unittest.main()
