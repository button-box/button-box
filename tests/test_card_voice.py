from speaker_helpers import isolate_audio, assert_padded_playback
from mic_helpers import physical_capture
from functools import partial
import contextlib
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

with mock.patch.dict(sys.modules, {"gpiozero": types.SimpleNamespace(Button=object, LED=object)}):
    from messagebox import button_send as runtime
from messagebox.guided_reply import OutboxStore, RecordingResult
from messagebox.contacts import ContactStore
from messagebox.nfc import Announcer, NfcRuntime
from messagebox.nfc_state import AnnouncementStore, EnrollmentStore, NfcRouter, SelectionStore
from messagebox.onboarding.nfc import TonePlayer
from messagebox.onboarding import nfc as onboarding_nfc
from messagebox.settings import defaults

ROOT = Path(__file__).resolve().parents[1]
CARD = "04:01:02:03"
OTHER_CARD = "04:05:06:07"
JID = "12025550101@s.whatsapp.net"


class CardVoiceTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = defaults({})
        self.button = types.SimpleNamespace(is_pressed=False)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for target, name, value in (
            (runtime, "button", self.button), (runtime, "led", mock.Mock()),
            (runtime, "caregiver_settings", lambda: self.settings),
            (runtime, "nfc_announcement_store", AnnouncementStore(self.root / "announcement.json")),
            (runtime, "log_event", mock.Mock()), (runtime, "refresh_led", mock.Mock()),
            (runtime.sound_pack, "SOUND_DIR", ROOT / "sounds"),
            (onboarding_nfc.sound_pack, "SOUND_DIR", ROOT / "sounds"),
            (runtime.sound_pack._settings, "snapshot", lambda: self.settings),
        ):
            self.stack.enter_context(mock.patch.object(target, name, value, create=True))

    @contextlib.contextmanager
    def reader(self, mode):
        tick = [0.0]
        root = Path(tempfile.mkdtemp(dir=self.root, prefix=mode))
        contacts = ContactStore(root / "contacts.json")
        contacts.add_contact(JID, "Card person")
        contacts.add_contact("12025550102@s.whatsapp.net", "Another person")
        contacts.assign_card(JID, CARD)
        contacts.assign_card("12025550102@s.whatsapp.net", OTHER_CARD)
        store = AnnouncementStore(root / "announcement.json", clock=lambda: 1000 + tick[0],
                                  monotonic=lambda: tick[0], boot_id="test-boot")
        router = NfcRouter(contacts, SelectionStore(root / "selection.json", clock=lambda: 1000 + tick[0]),
                           EnrollmentStore(root / "enrollment.json"), store)
        self.settings["nfc_confirmation_beep"] = False
        with mock.patch.dict("os.environ", {"MSGBOX_TRANSPORT": mode}), \
             mock.patch.object(runtime, "transport_mode", return_value=mode), \
             mock.patch.object(runtime, "CONTACTS_FILE", contacts.path), \
             mock.patch.object(runtime, "nfc_announcement_store", store), \
             mock.patch.object(runtime, "quiet_hours", return_value=False), \
             mock.patch.object(runtime.cloud_runtime, "card_prompt_clip", return_value=""), \
             mock.patch.object(runtime.time, "monotonic", side_effect=lambda: tick[0]):
            yield NfcRuntime(router, Announcer(store)), store, tick

    def test_same_card_during_playback_plays_once(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.reader(mode) as (reader, store, tick):
                reader.observe(CARD, 0)

                def play(_path, _timeout):
                    tick[0] = 1
                    reader.observe(None, 1)
                    tick[0] = 1.1
                    reader.observe(CARD, 1.1)
                    tick[0] = 2
                    return True

                with mock.patch.object(runtime, "play_idle_sound", side_effect=play) as audio:
                    runtime.play_pending_nfc_announcement(force=True)
                    runtime.play_pending_nfc_announcement(force=True)
                self.assertEqual(audio.call_count, 1)
                self.assertTrue(store.is_acknowledged(CARD))

    def test_pending_same_card_keeps_original_request_and_expiry(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.reader(mode) as (reader, store, tick):
                reader.observe(CARD, 0)
                original = store.path.read_bytes()
                tick[0] = 1
                reader.observe(None, 1)
                tick[0] = 1.1
                repeated = reader.observe(CARD, 1.1)
                self.assertFalse(repeated.announce)
                self.assertEqual(store.path.read_bytes(), original)
                tick[0] = 11
                reader.observe(None, 11)
                tick[0] = 12
                self.assertTrue(reader.observe(CARD, 12).announce)
                with mock.patch.object(runtime, "play_idle_sound", return_value=True) as audio:
                    runtime.play_pending_nfc_announcement(force=True)
                self.assertEqual(audio.call_count, 1)

    def test_reader_restart_does_not_erase_playing_claim(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.reader(mode) as (reader, store, tick):
                reader.observe(CARD, 0)

                def play(_path, _timeout):
                    tick[0] = 1
                    reader.observe(None, 1)
                    store.clear(preserve_claimed=True)
                    restarted = NfcRuntime(reader.router, Announcer(store))
                    tick[0] = 1.1
                    self.assertFalse(restarted.observe(CARD, 1.1).announce)
                    tick[0] = 2
                    return True

                with mock.patch.object(runtime, "play_idle_sound", side_effect=play) as audio:
                    runtime.play_pending_nfc_announcement(force=True)
                    runtime.play_pending_nfc_announcement(force=True)
                self.assertEqual(audio.call_count, 1)

    def test_consumer_drops_same_uid_duplicate_queued_before_ack(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.reader(mode) as (reader, store, tick):
                reader.observe(CARD, 0)

                def play(_path, _timeout):
                    # Force a duplicate past the producer check, as in a race
                    # with acknowledgment or a request from an older reader.
                    tick[0] = 1.1
                    store.put(action="selected", uid=CARD, prompt="")
                    tick[0] = 2
                    return True

                with mock.patch.object(runtime, "play_idle_sound", side_effect=play) as audio:
                    runtime.play_pending_nfc_announcement(force=True)
                    runtime.play_pending_nfc_announcement(force=True)
                self.assertEqual(audio.call_count, 1)
                self.assertFalse(store.path.exists())

    def test_confirmation_after_duplicate_drop_does_not_replay(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.reader(mode) as (reader, store, tick), \
                 mock.patch.object(runtime, "play_idle_sound", return_value=True) as audio:
                reader.observe(CARD, 0)
                runtime.play_pending_nfc_announcement(force=True)
                tick[0] = 1
                store.put(action="selected", uid=CARD, prompt="")
                self.assertTrue(runtime.ensure_nfc_confirmation({"uid": CARD}))
                self.assertEqual(audio.call_count, 1)

    def test_switch_away_and_back_during_playback_still_announces(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.reader(mode) as (reader, store, tick):
                reader.observe(CARD, 0)

                def play(_path, _timeout):
                    tick[0] = 1
                    self.assertTrue(reader.observe(OTHER_CARD, 1).announce)
                    tick[0] = 1.1
                    self.assertTrue(reader.observe(CARD, 1.1).announce)
                    tick[0] = 2
                    return True

                with mock.patch.object(runtime, "play_idle_sound", side_effect=play) as audio:
                    runtime.play_pending_nfc_announcement(force=True)
                    audio.side_effect = None
                    audio.return_value = True
                    runtime.play_pending_nfc_announcement(force=True)
                self.assertEqual(audio.call_count, 2)
                self.assertFalse(store.claimed_path.exists())

    def test_dropped_or_failed_playback_releases_claim_for_retry(self):
        for mode in ("wacli", "cloud"):
            for failure in ("wrong_card", "speaker"):
                with self.subTest(mode=mode, failure=failure), self.reader(mode) as (reader, store, tick):
                    reader.observe(CARD, 0)
                    if failure == "wrong_card":
                        self.assertIsNone(runtime.play_pending_nfc_announcement(force=True, expected_uid=OTHER_CARD))
                    else:
                        with mock.patch.object(runtime, "play_idle_sound", side_effect=OSError("speaker unavailable")):
                            with self.assertRaises(OSError):
                                runtime.play_pending_nfc_announcement(force=True)
                    self.assertFalse(store.claimed_path.exists())
                    tick[0] = 1
                    reader.observe(None, 1)
                    tick[0] = 1.1
                    self.assertTrue(reader.observe(CARD, 1.1).announce)
                    with mock.patch.object(runtime, "play_idle_sound", return_value=True) as audio:
                        runtime.play_pending_nfc_announcement(force=True)
                    self.assertEqual(audio.call_count, 1)

    def test_old_playback_cleanup_cannot_release_a_newer_claim(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.reader(mode) as (reader, store, tick):
                reader.observe(CARD, 0)
                first = store.take(keep_claimed=True)
                tick[0] = 0.1
                reader.observe(OTHER_CARD, 0.1)
                replacement = store.take(keep_claimed=True)
                store.finish(first["request_id"])
                tick[0] = 1
                reader.observe(None, 1)
                tick[0] = 1.1
                self.assertFalse(reader.observe(OTHER_CARD, 1.1).announce)
                store.finish(replacement["request_id"])
                self.assertFalse(store.claimed_path.exists())

    def test_acknowledged_switch_claim_is_not_replayed_after_consumer_crash(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), self.reader(mode) as (_reader, store, _tick), \
                 mock.patch.object(runtime, "play_idle_sound", return_value=True) as audio:
                store.put(action="selected", uid=CARD, prompt="", card_switch=True)
                request = store.take(keep_claimed=True)
                runtime._play_nfc_prompt(request["uid"], request["action"], request["prompt"])
                # Model a crash after acknowledgment but before claim cleanup.
                self.assertTrue(store.claimed_path.exists())
                runtime.play_pending_nfc_announcement(force=True)
                self.assertEqual(audio.call_count, 1)
                self.assertFalse(store.claimed_path.exists())

    def test_tap_uses_current_pack_generic_in_wacli_and_cached_names_in_cloud(self):
        for mode in ("wacli", "cloud"):
            for pack in ("jessica", "pirate", "robot", "dj"):
                self.settings["voice_pack"] = pack
                named = ROOT / "sounds/voice/voice-card-prompt.wav"
                with self.subTest(mode=mode, pack=pack), \
                     mock.patch.object(runtime, "transport_mode", return_value=mode), \
                     mock.patch.object(runtime, "ContactStore") as contacts, \
                     mock.patch.object(runtime.cloud_runtime, "card_prompt_clip", return_value=str(named) if pack not in {"robot", "dj"} else "") as cache, \
                     mock.patch.object(runtime, "play_idle_sound", return_value=True) as play:
                    contacts.return_value.resolve_card.return_value = {"jid": JID}
                    self.assertTrue(runtime._play_nfc_prompt(CARD, "selected", "/old/custom.wav"))
                    expected = str(named) if mode == "cloud" and pack not in {"robot", "dj"} else runtime.sound_pack.voice_path("card-prompt", pack)
                    self.assertEqual([call.args[0] for call in play.call_args_list], [runtime.sound_pack.cue_path("card"), expected])
                    if mode == "wacli":
                        cache.assert_not_called()
                        contacts.assert_not_called()

    def test_missing_name_beep_disabled_and_prompt_disabled_are_independent(self):
        with mock.patch.object(runtime, "transport_mode", return_value="cloud"), \
             mock.patch.object(runtime, "ContactStore") as contacts, \
             mock.patch.object(runtime.cloud_runtime, "card_prompt_clip", return_value=""), \
             mock.patch.object(runtime, "play_idle_sound", return_value=True) as play:
            contacts.return_value.resolve_card.return_value = {"jid": JID}
            self.settings["voice_pack"] = "alien"
            self.settings["nfc_confirmation_beep"] = False
            runtime._play_nfc_prompt(CARD, "recognized", "")
            self.assertEqual(play.call_args.args[0], runtime.sound_pack.voice_path("card-prompt", "alien"))
            play.reset_mock()
            self.settings["card_name_prompt"] = False
            runtime._play_nfc_prompt(CARD, "recognized", "")
            play.assert_not_called()
            self.settings["nfc_confirmation_beep"] = True
            runtime._play_nfc_prompt(CARD, "recognized", "")
            self.assertEqual(play.call_args.args[0], runtime.sound_pack.cue_path("card"))

    def test_reader_cooldown_preserves_quiet_playback_in_both_modes(self):
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), \
                 mock.patch.object(runtime, "transport_mode", return_value=mode), \
                 mock.patch.object(runtime, "nfc_announcement_store", AnnouncementStore(
                     self.root / (mode + "-announcement.json"), monotonic=lambda: 0.0, boot_id="test-boot")), \
                 mock.patch.object(runtime, "quiet_hours", return_value=True), \
                 mock.patch.object(runtime, "CONTACTS_FILE", self.root / (mode + "-contacts.json")), \
                 mock.patch.object(runtime.cloud_runtime, "card_prompt_clip", return_value=""), \
                 mock.patch.object(runtime, "start_player", return_value=mock.Mock(poll=lambda: 0)) as player:
                contacts = ContactStore(runtime.CONTACTS_FILE)
                contacts.add_contact(JID, "Card person")
                contacts.add_contact("12025550102@s.whatsapp.net", "Another person")
                contacts.assign_card(JID, CARD)
                announcements = runtime.nfc_announcement_store
                announcements.clear()
                router = NfcRouter(contacts, SelectionStore(self.root / (mode + "-selection.json")),
                                   EnrollmentStore(self.root / (mode + "-enrollment.json")), announcements)
                reader = NfcRuntime(router, Announcer(announcements))
                reader.observe(CARD, 0)
                self.assertEqual(runtime.play_pending_nfc_announcement(force=True), "selected")
                self.assertTrue(announcements.is_acknowledged(CARD))
                # Cloud quiet time remains silent. Self-hosted card feedback
                # retains its existing behavior, including its quiet-hours policy.
                self.assertEqual(player.call_count, 0 if mode == "cloud" else 2)
                player.reset_mock()
                reader.observe(None, 1)
                reader.observe(CARD, 2)
                self.assertIsNone(runtime.play_pending_nfc_announcement(force=True))
                self.assertTrue(runtime.ensure_nfc_confirmation({"uid": CARD}))
                player.assert_not_called()
                announcements.clear()
                reader = NfcRuntime(router, Announcer(announcements))
                reader.observe(CARD, 2.5)  # Restart while the card stays held.
                self.assertTrue(announcements.is_acknowledged(CARD))
                self.assertTrue(runtime.ensure_nfc_confirmation({"uid": CARD}))
                player.assert_not_called()
                reader.observe(None, 3.5)
                announcements.clear()  # NFC daemon shutdown/startup cleanup.
                reader = NfcRuntime(router, Announcer(announcements))
                reader.observe(CARD, 4)
                self.assertIsNone(runtime.play_pending_nfc_announcement(force=True))
                self.assertTrue(runtime.ensure_nfc_confirmation({"uid": CARD}))
                player.assert_not_called()

    def test_enrollment_and_onboarding_success_play_saved_cue_then_current_voice_in_both_modes(self):
        for mode in ("wacli", "cloud"):
            for pack in ("jessica", "tata"):
                self.settings.update(voice_pack=pack, card_name_prompt=False, nfc_confirmation_beep=False)
                with self.subTest(mode=mode, pack=pack), \
                     mock.patch.object(runtime, "transport_mode", return_value=mode), \
                     mock.patch.object(runtime, "play_idle_sound", return_value=True) as play:
                    runtime._play_nfc_prompt(CARD, "enrolled", "")
                    self.assertEqual([call.args[0] for call in play.call_args_list], [runtime.sound_pack.cue_path("card_saved"), runtime.sound_pack.voice_path("card-saved", pack)])
                calls = []
                expected = [runtime.sound_pack.cue_path("card_saved"), runtime.sound_pack.voice_path("card-saved", pack)]
                def run(command, **kwargs):
                    if command[0] == 'aplay':
                        assert_padded_playback(self, command[-1], expected[len(calls)])
                        calls.append(command)
                player = TonePlayer(ROOT / "sounds/cues", run=run)
                with mock.patch.object(player.settings, "snapshot", return_value=self.settings):
                    player("success")
                self.assertEqual(len(calls), 2)

    def test_interruption_reaps_speaker_before_dispatching_immediate_recording(self):
        self.settings["nfc_confirmation_beep"] = False
        process = mock.Mock()
        process.poll.side_effect = [None, None, None]
        process.wait.side_effect = [subprocess.TimeoutExpired("aplay", 0.2), 0]
        order = []
        process.terminate.side_effect = lambda: order.append("terminate")
        process.kill.side_effect = lambda: order.append("kill")
        def press(_seconds):
            self.button.is_pressed = True
        def start(_closed_at, *, card_prompt_uid):
            self.assertEqual(card_prompt_uid, CARD)
            self.assertTrue(runtime.nfc_announcement_store.is_acknowledged(CARD))
            self.assertEqual(process.wait.call_count, 2)
            order.append("record")
        with mock.patch.object(runtime, "transport_mode", return_value="wacli"), \
             mock.patch.object(runtime.subprocess, "Popen", return_value=process), \
             mock.patch.object(runtime.time, "sleep", side_effect=press), \
             mock.patch.object(runtime, "handle_confirmed_press", side_effect=start):
            runtime._play_nfc_prompt(CARD, "selected", "")
        self.assertEqual(order, ["terminate", "kill", "record"])

    def test_prompt_press_skips_guided_countdown_and_legacy_hold_classification(self):
        context = {"via_card": True, "uid": CARD, "contact": {"jid": JID, "card_uids": [CARD]},
                   "selection": {"selection_id": "a" * 32, "jid": JID}}
        real_capture = partial(physical_capture, runtime, capture_function=runtime.capture_guided_recording)
        for mode in ("wacli", "cloud"):
            with self.subTest(mode=mode), \
                 mock.patch.object(runtime, "transport_mode", return_value=mode), \
                 mock.patch.object(runtime.cloud_runtime, "account_scope", return_value="a" * 64), \
                 mock.patch.object(runtime, "claim_fresh_card_intent", return_value=("claimed", context)), \
                 mock.patch.object(runtime, "ensure_nfc_confirmation", return_value=True), \
                 mock.patch.object(runtime, "claim_oldest", side_effect=AssertionError("incoming must not preempt card")), \
                 mock.patch.object(runtime, "outbox_store", OutboxStore(self.root / mode, transport=mode), create=True), \
                 mock.patch.object(runtime, "queued", return_value=[]), \
                 mock.patch.object(runtime, "quiet_hours", return_value=False), \
                 mock.patch.object(runtime, "mark_queue_known"), \
                 mock.patch.object(runtime, "play_audio_ordinary", side_effect=lambda path: self.assertEqual(Path(path).name, "cue-deleted.wav")), \
                 mock.patch.object(runtime, "capture_guided_recording", return_value=RecordingResult(None, 0, False)) as capture, \
                 mock.patch.object(runtime, "play_moment"):
                runtime.run_guided_once(self.settings, card_prompt_uid=CARD)
                self.assertIs(capture.call_args.kwargs["card_prompt"], True)
                # Stop at the microphone boundary: neither hold classification
                # nor a press cue may delay the same immediate legacy path.
                self.button.is_pressed = True
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(runtime, "TEMP_DIR", str(self.root)))
                    stack.enter_context(mock.patch.object(runtime, "play_sound"))
                    stack.enter_context(mock.patch.object(runtime, "start_player", return_value=mock.Mock(poll=lambda: 0, wait=lambda **kwargs: 0)))
                    stack.enter_context(mock.patch.object(runtime, "acknowledge_and_classify_legacy_press", side_effect=AssertionError("no hold delay")))
                    stack.enter_context(mock.patch.object(runtime, "capture_guided_recording", side_effect=real_capture))
                    open_mic = stack.enter_context(mock.patch.object(runtime.subprocess, "Popen", side_effect=RuntimeError("microphone boundary")))
                    with self.assertRaisesRegex(RuntimeError, "microphone boundary"):
                        runtime.record_and_send_legacy(self.settings, card_prompt_uid=CARD)
                    self.assertEqual(open_mic.call_args.args[0][0], "arecord")

    def test_card_prompt_press_cannot_record_to_changed_mapping(self):
        context = {"via_card": True, "contact": {"jid": JID, "card_uids": []}}
        with mock.patch.object(runtime, "transport_mode", return_value="wacli"), \
             mock.patch.object(runtime, "claim_fresh_card_intent", return_value=("claimed", context)), \
             mock.patch.object(runtime, "block_unavailable_recipient") as block, \
             mock.patch.object(runtime.subprocess, "Popen") as microphone:
            runtime.run_guided_once(self.settings, card_prompt_uid=CARD)
            runtime.record_and_send_legacy(self.settings, card_prompt_uid=CARD)
        self.assertEqual(block.call_count, 2)
        microphone.assert_not_called()

    def test_guided_capture_ignores_starting_press_until_release(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.communicate.return_value = (b"", b"")
        vad = mock.Mock()
        vad.silence_expired.return_value = False
        vad.trim_bounds.return_value = None
        ticks = [0]
        self.button.is_pressed = True
        def poll(*_args):
            ticks[0] += 1
            self.button.is_pressed = ticks[0] != 2
            return [], [], []
        with mock.patch.object(runtime, "TEMP_DIR", str(self.root)), \
             mock.patch.object(runtime, "EnergyVAD", return_value=vad), \
             mock.patch.object(runtime.subprocess, "Popen", return_value=process), \
             mock.patch.object(runtime.time, "monotonic", side_effect=lambda: ticks[0] * 0.1), \
             mock.patch.object(runtime.select, "select", side_effect=poll), \
             mock.patch.object(runtime, "presence"), \
             mock.patch.object(runtime, "acknowledge_guided_press"), \
             mock.patch.object(runtime, "wait_for_stable_open"), \
             mock.patch.object(runtime.subprocess, "run") as audio:
            result = physical_capture(runtime, JID, "session", card_prompt=True)
        self.assertFalse(result.meaningful)
        self.assertEqual(ticks[0], 4)
        self.assertEqual(Path(audio.call_args.args[0][-1]).name, "cue-rec_go.wav")

    def test_guided_incoming_only_plays_start_cue_and_message_in_both_modes(self):
        from test_guided_reply import FakeIO
        claim = {"path": self.root / "incoming.wav", "meta": {"chat": JID}}
        for mode in ("wacli", "cloud"):
            io = FakeIO(recordings=[])
            with self.subTest(mode=mode), \
                 mock.patch.object(runtime, "transport_mode", return_value=mode), \
                 mock.patch.object(runtime.cloud_runtime, "account_scope", return_value="a" * 64), \
                 mock.patch.object(runtime, "claim_fresh_card_intent", return_value=("none", None)), \
                 mock.patch.object(runtime, "claim_oldest", return_value=claim), \
                 mock.patch.object(runtime, "ContactStore") as contacts, \
                 mock.patch.object(runtime, "inbound_audio_authorized", return_value=True), \
                 mock.patch.object(runtime, "PiGuidedIO", return_value=io), \
                 mock.patch.object(runtime, "outbox_store", OutboxStore(self.root / mode), create=True), \
                 mock.patch.object(runtime, "react_played"), \
                 mock.patch.object(runtime, "finish_claim"), \
                 mock.patch.object(runtime, "mark_queue_known"), \
                 mock.patch.object(runtime, "queued", return_value=[]), \
                 mock.patch.object(runtime, "quiet_hours", return_value=False):
                contacts.return_value.allowed_jids.return_value = {JID}
                runtime.run_guided_once(self.settings)
                self.assertEqual(io.calls, [("ordinary", "cue-msg_start.wav"), ("ordinary", "incoming.wav"), ("ordinary", "cue-msg_end.wav")])
                contacts.return_value.allowed_jids.return_value = set()
                with mock.patch.object(runtime, "play_moment") as cue, \
                     mock.patch.object(runtime, "play_audio_ordinary") as voice:
                    runtime.run_guided_once(self.settings)
                self.assertEqual(cue.call_args_list, [mock.call("msg_start"), mock.call("msg_end")])
                voice.assert_called_once_with(claim["path"])


class CardPromptPressContextTests(unittest.TestCase):
    """A press during the card prompt uses a claimed selection without card_uids."""

    def test_claimed_selection_without_card_list_matches_by_uid(self):
        from messagebox import button_send
        context = {"contact": {"jid": "15550001111@s.whatsapp.net", "name": "Abba"}, "uid": "04AABBCC", "via_card": True}
        self.assertTrue(button_send._context_matches_card(context, "04AABBCC"))
        self.assertFalse(button_send._context_matches_card(context, "04FFFFFF"))
        self.assertFalse(button_send._context_matches_card(None, "04AABBCC"))

    def test_full_contact_with_card_list_still_matches(self):
        from messagebox import button_send
        context = {"contact": {"jid": "15550001111@s.whatsapp.net", "card_uids": ["04AABBCC"]}, "via_card": False}
        self.assertTrue(button_send._context_matches_card(context, "04AABBCC"))
