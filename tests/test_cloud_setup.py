"""Setup speech and first-reply contracts, without GPIO or network access."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from messagebox import cloud_setup, speaker
from messagebox.cloud_claim import CloudClaim
from test_cloud_claim import FakeClient, ID, NOW
from messagebox.guided_reply import RecordingResult, OutboxStore
from messagebox.settings import defaults, SettingsStore
from speaker_helpers import isolate_audio
from mic_helpers import physical_capture

with mock.patch.dict(sys.modules, {"gpiozero": types.SimpleNamespace(Button=object, LED=object)}):
    from messagebox import button_send as runtime


class SetupTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)
        clock = mock.patch.object(cloud_setup, "time", types.SimpleNamespace(time=lambda: NOW))
        clock.start()
        self.addCleanup(clock.stop)
        self.guide = cloud_setup.SetupGuide()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_online_scan_waits_for_remote_pending_claim_before_press_then_names(self):
        client = FakeClient()
        claim = CloudClaim(client, path=self.root / 'claim.json', clock=lambda: NOW)
        pending = {'claimed': False, 'pending_claim': None}
        client.setup_checkin = lambda: pending
        cue = [True]
        def online(**kwargs):
            return cue.pop() if cue else False
        played = []
        with mock.patch.object(runtime, 'quiet_hours', return_value=False), \
             mock.patch.object(runtime.cloud_claim, 'CLAIM_FILE', claim.path), \
             mock.patch.object(runtime.cloud_claim, 'setup_online_cue', side_effect=online), \
             mock.patch.object(runtime, 'play_setup_line', side_effect=played.append):
            claim.start()  # Registration creates a file before WhatsApp is sent.
            claim.setup_checkin()
            runtime.play_setup_online({})
            for _ in range(3):
                claim.setup_checkin()
                runtime.play_setup_online({})
            self.assertEqual(played, ['setup-scan'])
            self.assertEqual(self.guide.load()['stage'], 'scan')
            pending['pending_claim'] = {'claim_id': ID, 'expires_at': NOW + 600}
            claim.setup_checkin()
            runtime.play_setup_online({})
            claim.setup_checkin()
            runtime.play_setup_online({})
            self.assertEqual(played, ['setup-scan', 'setup-press'])
            self.assertTrue(claim.consume_press())
            pending['claimed'] = True
            claim.setup_checkin()
            runtime.play_setup_online({})
        self.assertEqual(played, ['setup-scan', 'setup-press', 'setup-names'])

    def test_remote_pending_claim_repairs_an_early_press_receipt_without_repeating(self):
        self.guide.change('start')
        self.guide.change('stage', stage='press', spoken=['scan', 'press'])
        client = FakeClient()
        claim = CloudClaim(client, path=self.root / 'claim.json', clock=lambda: NOW)
        claim.start()
        play = mock.Mock()
        self.assertFalse(self.guide.speak(play))  # Old receipt has no Cloud proof.
        client.setup_checkin = lambda: {'claimed': False,
            'pending_claim': {'claim_id': ID, 'expires_at': NOW + 600}}
        claim.setup_checkin()
        self.assertTrue(self.guide.speak(play))
        claim.setup_checkin()
        self.assertFalse(self.guide.speak(play))
        play.assert_called_once_with('setup-press')

    def test_missing_expired_or_cancelled_remote_claim_cannot_arm_press_speech(self):
        client = FakeClient()
        claim = CloudClaim(client, path=self.root / 'claim.json', clock=lambda: NOW)
        claim.start()
        self.guide.change('start')
        self.guide.change('spoken')  # Scan was already heard.
        self.guide.pending_claim(ID, NOW + 600)
        for pending in (None, {'claim_id': ID, 'expires_at': NOW}):
            with self.subTest(pending=pending):
                client.setup_checkin = lambda: {'claimed': False, 'pending_claim': pending}
                claim.setup_checkin()
                self.assertEqual(self.guide.load()['stage'], 'scan')
                self.assertFalse(self.guide.speak(mock.Mock()))
        self.guide.pending_claim(ID, NOW + 600)
        with mock.patch.object(cloud_setup.time, 'time', return_value=NOW + 600):
            self.assertFalse(self.guide.speak(mock.Mock()))
        state = claim._read()
        state['cancel_pending'] = True
        runtime.cloud_claim.atomic_json(claim.path, state)
        client.setup_checkin = lambda: {'claimed': False,
            'pending_claim': {'claim_id': ID, 'expires_at': NOW + 600}}
        claim.setup_checkin()
        self.assertEqual(self.guide.load()['stage'], 'scan')
        self.assertFalse(self.guide.speak(mock.Mock()))

    def test_first_setup_note_still_invites_a_reply_with_play_only(self):
        self.test_first_note_invites_separate_press_and_reply_keeps_exact_recipient(after_listening='play_only')

    def test_receipts_survive_restart_hotspot_return_quiet_and_audio_failure(self):
        self.guide.change("start")
        play = mock.Mock(side_effect=subprocess.CalledProcessError(1, "aplay"))
        self.assertFalse(self.guide.speak(play, quiet=True))
        self.assertTrue(self.guide.speak(play))
        restarted = cloud_setup.SetupGuide()
        restarted.change("start")  # Hotspot return is not a new setup.
        self.assertFalse(restarted.speak(play))
        self.assertTrue(restarted.speak(play, replay=True))
        self.assertEqual(play.call_count, 2)
        restarted.pending_claim(ID, NOW + 600)
        self.assertTrue(restarted.speak(mock.Mock()))
        self.assertIsNone(restarted.advance({"scan"}, "names"))
        self.guide.path.write_text("invalid")
        self.assertFalse(self.guide.speak(play))
        self.assertFalse(self.guide.first_reply())

    def test_existing_jessica_survives_without_a_selection_receipt(self):
        path = self.root / "settings.json"
        old = {**defaults({}), "voice_pack": "jessica"}
        path.write_text(json.dumps(old))
        store = SettingsStore(path, environ={"MSGBOX_TRANSPORT": "wacli"})
        current, warning = store.load()
        self.assertFalse(warning)
        self.assertEqual(current, old)
        candidate = {key: value for key, value in current.items() if key not in {"version", "revision"}}
        saved = store.update({**candidate, "voice_pack": "jessica"}, 0)
        self.assertEqual(SettingsStore(path, environ={"MSGBOX_TRANSPORT": "wacli"}).load(), (saved, False))

    def test_first_note_invites_separate_press_and_reply_keeps_exact_recipient(self, after_listening='invite_reply'):
        self.guide.change("start")
        self.guide.advance({"scan"}, "names")
        incoming = self.root / "incoming.wav"
        incoming.write_bytes(b"synthetic")
        recording = self.root / "reply.wav"
        recording.write_bytes(b"synthetic")
        claim = {"path": incoming, "meta": {"chat": "person", "cloud": True}}
        settings = {**defaults({}), "after_listening": after_listening, "talk_mode": "hold",
                    "max_recording_seconds": 30, "review_before_send": False}
        store = OutboxStore(self.root / "outbox", transport="cloud")
        with contextlib.ExitStack() as stack:
            for name, value in (("transport_mode", lambda: "cloud"), ("quiet_hours", lambda *a: False),
                                ("claim_fresh_card_intent", lambda: ("none", None)),
                                ("current_recipient_context", lambda **kw: None),
                                ("recording_recipient_context", lambda: None),
                                ("inbound_audio_authorized", lambda meta: True), ("queued", lambda: []),
                                ("wait_for_stable_open", mock.Mock()), ("play_audio_ordinary", mock.Mock()),
                                ("play_audio_after_cue", mock.Mock()), ("play_setup_line", mock.Mock()),
                                ("finish_claim", mock.Mock()), ("react_played", mock.Mock()),
                                ("mark_queue_known", mock.Mock()), ("refresh_led", mock.Mock()),
                                ("log_event", mock.Mock()), ("led", mock.Mock()),
                                ("button", types.SimpleNamespace(is_pressed=False)), ("outbox_store", store)):
                stack.enter_context(mock.patch.object(runtime, name, value, create=True))
            stack.enter_context(mock.patch.object(runtime.cloud_runtime, "account_scope", return_value="a" * 64))
            stack.enter_context(mock.patch.object(runtime, "ContactStore", return_value=types.SimpleNamespace(allowed_jids=lambda: {"person"})))
            capture = stack.enter_context(mock.patch.object(runtime, "capture_guided_recording",
                return_value=RecordingResult(str(recording), 2, True, True, 60)))
            approve = stack.enter_context(mock.patch.object(runtime, "wait_for_approval", return_value=True))
            with mock.patch.object(runtime, "claim_oldest", return_value=claim):
                runtime.run_guided_once(settings)
            capture.assert_not_called()
            self.assertEqual(self.guide.load()["stage"], "your_turn")
            runtime.play_setup_line.assert_called_once_with("setup-your-turn")
            with mock.patch.object(runtime, "claim_oldest", return_value=None):
                runtime.run_guided_once(settings)
            self.assertEqual(capture.call_args.args[0], "person")
            self.assertEqual(capture.call_args.args[2], 60)
            self.assertTrue(capture.call_args.kwargs["setup_reply"])
            approve.assert_called_once()
            self.assertEqual(store.jobs()[0].recipient, "person")
            self.assertEqual(self.guide.load()["stage"], "await_listened")
            self.assertFalse(recording.exists())

    def test_reply_binding_is_durable_before_job_visibility_and_recovery_matches_it(self):
        self.guide.change("start")
        self.guide.change("stage", stage="review", scope="a" * 64)
        def approve(*args, **kwargs):
            state = cloud_setup.SetupGuide().load()
            self.assertEqual(state["stage"], "await_listened")
            self.assertEqual(state["local_id"], kwargs["message_id"])
            self.assertEqual(state["scope"], kwargs["account_scope"])
            return kwargs["message_id"]
        store = types.SimpleNamespace(approve=approve)
        message_id = cloud_setup.ReplyOutbox(store, self.guide).approve(
            "synthetic.wav", "person", "reply", 2, account_scope="a" * 64)
        restarted = cloud_setup.SetupGuide()
        restarted.sent(message_id, "b" * 64, {"message_id": "cloud-reply"}, ready=True)
        self.assertNotIn("message_id", restarted.load())
        restarted.sent(message_id, "a" * 64, {"message_id": "cloud-reply"}, ready=True)
        play = mock.Mock()
        self.assertFalse(restarted.speak(play, quiet=True))
        self.assertTrue(restarted.speak(play))
        play.assert_called_once_with("setup-sent")
        self.assertFalse(cloud_setup.SetupGuide().speak(play))
        self.assertTrue(restarted.listened({"message_id": "cloud-reply", "account_scope": "a" * 64}))

    def test_setup_reminder_plays_once_at_forty_seconds_and_waits_during_quiet(self):
        player = mock.Mock()
        player.poll.return_value = 0
        with mock.patch.object(runtime, "start_player", return_value=player) as start, \
             mock.patch.object(runtime, "box_muted", return_value=False), \
             mock.patch.object(runtime, "quiet_hours", return_value=False):
            reminder = runtime.RecordingLimitCue(0, 45)
            reminder.path = runtime.sound_pack.voice_path("setup-remind-stop")
            reminder.update(39.99)
            start.assert_not_called()
            reminder.update(40)
            reminder.update(59)
            start.assert_called_once()
            self.assertEqual(Path(start.call_args.args[0][-1]).name, "voice-setup-remind-stop.wav")
        with mock.patch.object(runtime, "start_player") as start, \
             mock.patch.object(runtime, "box_muted", return_value=False), \
             mock.patch.object(runtime, "quiet_hours", return_value=True):
            reminder = runtime.RecordingLimitCue(0, 45)
            reminder.path = runtime.sound_pack.voice_path("setup-remind-stop")
            reminder.update(40)
            start.assert_not_called()

    def test_first_reply_keeps_audio_beyond_vad_trim_at_the_sixty_second_limit(self):
        recorder = mock.Mock()
        recorder.communicate.return_value = (b"\0\0" * 48000, None)
        vad = mock.Mock()
        vad.trim_bounds.return_value = (0, 16000)
        with mock.patch.object(runtime, "TEMP_DIR", str(self.root)), \
             mock.patch.object(runtime, "play_sound"), \
             mock.patch.object(runtime, "EnergyVAD", return_value=vad), \
             mock.patch.object(runtime, "RecordingLimitCue"), \
             mock.patch.object(runtime, "presence"), \
             mock.patch.object(runtime, "led", mock.Mock(), create=True), \
             mock.patch.object(runtime, "button", types.SimpleNamespace(is_pressed=False), create=True), \
             mock.patch.object(runtime, "open_microphone", return_value=recorder), \
             mock.patch.object(runtime.select, "select", return_value=([], [], [])), \
             mock.patch.object(runtime.time, "monotonic", side_effect=[0, 60, 60, 60]):
            result = physical_capture(runtime, "person", setup_reply=True)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.duration, 3)
        self.assertTrue(Path(result.path).is_file())

    def test_normal_buttonbox_recording_reminds_at_forty_seconds(self):
        recorder = mock.Mock()
        recorder.poll.return_value = None
        recorder.communicate.return_value = (b'\x01\x02' * 48000, None)
        vad = mock.Mock()
        vad.trim_bounds.return_value = (0, 48000)
        player = mock.Mock()
        player.poll.return_value = 0
        with mock.patch.object(runtime, 'TEMP_DIR', str(self.root)), \
             mock.patch.object(runtime, 'play_sound'), \
             mock.patch.object(runtime, 'buttonbox_voice', return_value=True), \
             mock.patch.object(runtime, 'EnergyVAD', return_value=vad), \
             mock.patch.object(runtime, 'presence'), \
             mock.patch.object(runtime, 'led', mock.Mock(), create=True), \
             mock.patch.object(runtime, 'button', types.SimpleNamespace(is_pressed=False), create=True), \
             mock.patch.object(runtime, 'open_microphone', return_value=recorder), \
             mock.patch.object(runtime, 'start_player', return_value=player) as start, \
             mock.patch.object(runtime, 'box_muted', return_value=False), \
             mock.patch.object(runtime, 'quiet_hours', return_value=False), \
             mock.patch.object(runtime.select, 'select', return_value=([], [], [])), \
             mock.patch.object(runtime.time, 'monotonic', side_effect=[0, 40, 60, 60, 60]):
            result = physical_capture(runtime, 'person')
        self.assertTrue(result.timed_out)
        self.assertTrue(Path(result.path).is_file())
        self.assertIn('voice-setup-remind-stop.wav', [Path(call.args[0][-1]).name for call in start.call_args_list])

    def test_setup_recording_timeout_goes_to_review_wacli_timeout_stays_unchanged(self):
        result = RecordingResult("synthetic.wav", 60, True, True, 60)
        with mock.patch.object(runtime, "capture_guided_recording", return_value=result):
            io = runtime.PiGuidedIO("person", "session", 60)
            self.assertTrue(io.record().timed_out)
            io.setup_reply = True
            self.assertFalse(io.record().timed_out)

    def test_normal_limit_review_explains_send_and_wait_to_delete(self):
        result = RecordingResult('synthetic.wav', 60, True, True, 60)
        with mock.patch.object(runtime, 'capture_guided_recording', return_value=result), \
             mock.patch.object(runtime, 'buttonbox_voice', return_value=True), \
             mock.patch.object(runtime, 'play_audio_ordinary') as play, \
             mock.patch.object(runtime, 'wait_for_approval', return_value=False), \
             mock.patch.object(runtime.sound_pack, 'next_send_prompt', return_value=Path('voice-ask-send-1.wav')) as short_prompt:
            io = runtime.PiGuidedIO('person', 'session', 60)
            self.assertTrue(io.record().timed_out)
            self.assertFalse(io.wait_for_approval(10))
        self.assertEqual(Path(play.call_args.args[0]).name, 'voice-setup-ask-send.wav')
        short_prompt.assert_not_called()

    def test_listened_matches_only_the_first_reply_in_the_same_account(self):
        self.guide.change("start")
        self.guide.change("stage", stage="await_listened", message_id="reply", scope="a" * 64)
        self.assertTrue(self.guide.listened({"message_id": "reply", "account_scope": "a" * 64}))
        self.assertFalse(self.guide.listened({"message_id": "other", "account_scope": "a" * 64}))
        self.assertFalse(self.guide.listened({"message_id": "reply", "account_scope": "b" * 64}))
        self.guide.advance({"await_listened"}, "done")
        self.assertFalse(self.guide.listened({"message_id": "reply", "account_scope": "a" * 64}))

    def test_setup_listened_audio_failure_or_restart_never_retries_a_name_clip(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                self.guide.change("start")
                self.guide.change("stage", stage="await_listened", message_id="reply", scope="a" * 64,
                                  listened_started=interrupted)
                notice = types.SimpleNamespace(cloud={"message_id": "reply", "account_scope": "a" * 64,
                                                      "voice_pack": "buttonbox"}, clip="")
                receipts = mock.Mock()
                receipts.claim_next.side_effect = [notice, None]
                with mock.patch.object(runtime, "transport_mode", return_value="cloud"), \
                     mock.patch.object(runtime, "quiet_hours", return_value=False), \
                     mock.patch.object(runtime, "_recording", False), \
                     mock.patch.object(runtime, "_guided_active", False), \
                     mock.patch.object(runtime, "button", types.SimpleNamespace(is_pressed=False), create=True), \
                     mock.patch.object(runtime, "receipt_store", receipts, create=True), \
                     mock.patch.object(runtime.cloud_runtime, "listened_status", return_value="ready"), \
                     mock.patch.object(runtime.sound_pack, "SOUND_DIR", Path(__file__).resolve().parents[1] / "sounds"), \
                     mock.patch.object(runtime.sound_pack, "current_voice_pack", return_value="buttonbox"), \
                     mock.patch.object(runtime, "play_moment"), \
                     mock.patch.object(runtime, "play_audio_ordinary", side_effect=OSError("synthetic")) as play:
                    self.assertEqual(runtime.play_pending_listened(), 0)
                receipts.complete.assert_called_once_with(notice, cloud_result="rejected")
                receipts.release.assert_not_called()
                self.assertEqual(self.guide.load()["stage"], "done")
                self.assertEqual(play.call_count, 0 if interrupted else 1)

    def test_press_while_another_service_speaks_has_no_second_action(self):
        with mock.patch.object(speaker, "is_busy", return_value=True), \
             mock.patch.object(runtime, "wait_for_stable_open") as release, \
             mock.patch.object(runtime, "run_guided_once") as guided, \
             mock.patch.object(runtime.cloud_claim, "consume_claim_press") as claim:
            self.assertTrue(runtime._handle_confirmed_press(0))
            release.assert_called_once()
            guided.assert_not_called()
            claim.assert_not_called()

    def test_wacli_does_not_read_cloud_guide_for_normal_press(self):
        with mock.patch.dict(os.environ, {"MSGBOX_TRANSPORT": "wacli"}), \
             mock.patch.object(runtime.cloud_setup.SetupGuide, "load", side_effect=AssertionError("Cloud path")), \
             mock.patch.object(runtime, "button", types.SimpleNamespace(is_pressed=False), create=True), \
             mock.patch.object(runtime, "caregiver_settings", return_value=defaults({})), \
             mock.patch.object(runtime, "acknowledge_guided_press"), \
             mock.patch.object(runtime, "wait_for_stable_open"), \
             mock.patch.object(runtime, "refresh_led"), \
             mock.patch.object(runtime, "run_guided_once") as guided:
            self.assertTrue(runtime._handle_confirmed_press(0))
            guided.assert_called_once()


if __name__ == "__main__":
    unittest.main()
