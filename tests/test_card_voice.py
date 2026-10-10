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
from messagebox.nfc_state import AnnouncementStore
from messagebox.onboarding.nfc import TonePlayer
from messagebox.onboarding import nfc as onboarding_nfc
from messagebox.settings import defaults

ROOT = Path(__file__).resolve().parents[1]
CARD = "04:01:02:03"
JID = "12025550101@s.whatsapp.net"


class CardVoiceTests(unittest.TestCase):
    def setUp(self):
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
                player = TonePlayer(ROOT / "sounds/cues", run=lambda command, **kw: calls.append(command))
                with mock.patch.object(player.settings, "snapshot", return_value=self.settings):
                    player("success")
                self.assertEqual([Path(call[-1]) for call in calls if call[0] == "aplay"], [runtime.sound_pack.cue_path("card_saved"), runtime.sound_pack.voice_path("card-saved", pack)])

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
        context = {"via_card": True, "uid": CARD, "contact": {"jid": JID, "card_uids": [CARD]}}
        real_capture = runtime.capture_guided_recording
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
                with mock.patch.object(runtime, "TEMP_DIR", str(self.root)), \
                     mock.patch.object(runtime.subprocess, "run"), \
                     mock.patch.object(runtime, "acknowledge_and_classify_legacy_press", side_effect=AssertionError("no hold delay")), \
                     mock.patch.object(runtime, "capture_guided_recording", side_effect=real_capture), \
                     mock.patch.object(runtime.subprocess, "Popen", side_effect=RuntimeError("microphone boundary")) as open_mic:
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
            result = runtime.capture_guided_recording(JID, "session", card_prompt=True)
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
