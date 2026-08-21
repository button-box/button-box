import json
import os
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from messagebox.guided_reply import (
    EnergyVAD,
    GuidedSession,
    OutboxStore,
    RecordingResult,
    claim_inbox_file,
    discard_held_playback_press,
    env_flag,
    finish_inbox_file,
    invalid_prompt_files,
    raw_pcm_to_trimmed_wav,
    recover_inflight_files,
    release_inbox_file,
    voice_send_command,
)


def pcm_frame(level, samples=320):
    return struct.pack("<" + "h" * samples, *([level] * samples))


def write_wav(path, seconds=0.25, level=1000, rate=16000):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm_frame(level, int(seconds * rate)))


class PressPolicyTests(unittest.TestCase):
    def test_feature_flag_defaults_off_and_rolls_back(self):
        self.assertFalse(env_flag("MSGBOX_GUIDED_REPLY", environ={}))
        self.assertTrue(env_flag("MSGBOX_GUIDED_REPLY", environ={"MSGBOX_GUIDED_REPLY": "1"}))
        self.assertFalse(env_flag("MSGBOX_GUIDED_REPLY", environ={"MSGBOX_GUIDED_REPLY": "0"}))
        self.assertFalse(env_flag("MSGBOX_AUTO_RECORD_AFTER_INCOMING", environ={}))
        self.assertTrue(
            env_flag(
                "MSGBOX_AUTO_RECORD_AFTER_INCOMING",
                environ={"MSGBOX_AUTO_RECORD_AFTER_INCOMING": "1"},
            )
        )

    def test_prompt_gate_rejects_missing_and_non_audio_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "valid.wav"
            invalid = Path(directory) / "invalid.wav"
            missing = Path(directory) / "missing.wav"
            write_wav(valid)
            invalid.write_text("not audio", encoding="utf-8")
            self.assertEqual(
                invalid_prompt_files([valid, invalid, missing]),
                [str(invalid), str(missing)],
            )

    def test_open_button_after_playback_has_no_approval_dead_zone(self):
        waits = []
        discarded = discard_held_playback_press(lambda: False, lambda: waits.append(True))
        self.assertFalse(discarded)
        self.assertEqual(waits, [])

    def test_press_held_at_playback_end_is_discarded(self):
        waits = []
        discarded = discard_held_playback_press(lambda: True, lambda: waits.append(True))
        self.assertTrue(discarded)
        self.assertEqual(waits, [True])


class VADTests(unittest.TestCase):
    def test_twenty_seconds_of_silence_stops_without_meaningful_speech(self):
        vad = EnergyVAD(silence_seconds=20)
        vad.start(0)
        for i in range(25):
            vad.feed(pcm_frame(40), now=i * 0.02)
        self.assertFalse(vad.meaningful)
        self.assertFalse(vad.silence_expired(19.99))
        self.assertTrue(vad.silence_expired(20.0))

    def test_speech_resets_silence_timer_and_has_no_total_cap(self):
        vad = EnergyVAD(silence_seconds=20)
        vad.start(0)
        for i in range(12):
            vad.feed(pcm_frame(3000), now=100 + i * 0.02)
        self.assertTrue(vad.meaningful)
        self.assertFalse(vad.silence_expired(119.99))
        self.assertTrue(vad.silence_expired(120.22))
        vad.feed(pcm_frame(3000), now=121)
        self.assertFalse(vad.silence_expired(140.99))

    def test_click_is_not_meaningful_and_trim_keeps_internal_silence(self):
        vad = EnergyVAD()
        vad.start(0)
        vad.feed(pcm_frame(5000), now=0.02)
        for i in range(8):
            vad.feed(pcm_frame(30), now=0.04 + i * 0.02)
        self.assertFalse(vad.meaningful)

        vad = EnergyVAD()
        vad.start(0)
        raw = bytearray()
        levels = [20] * 15 + [3000] * 12 + [20] * 20 + [3000] * 12 + [20] * 20
        for i, level in enumerate(levels):
            frame = pcm_frame(level)
            raw.extend(frame)
            vad.feed(frame, now=i * 0.02)
        bounds = vad.trim_bounds()
        self.assertIsNotNone(bounds)
        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "capture.raw"
            wav_path = Path(directory) / "capture.wav"
            raw_path.write_bytes(raw)
            duration = raw_pcm_to_trimmed_wav(str(raw_path), str(wav_path), bounds)
            # The 400 ms internal pause remains; only the outside silence shrinks.
            self.assertGreater(duration, 0.75)
            self.assertLess(duration, len(levels) * 0.02)


class OutboxTests(unittest.TestCase):
    def test_approval_binds_exact_recipient_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            write_wav(source)
            store = OutboxStore(str(Path(directory) / "outbox"))
            first = store.approve(
                str(source),
                "exact-origin@s.whatsapp.net",
                "reply",
                0.25,
                message_id="stable-id",
            )
            second = store.approve(
                str(source),
                "exact-origin@s.whatsapp.net",
                "reply",
                0.25,
                message_id="stable-id",
            )
            self.assertEqual(first.path, second.path)
            self.assertEqual(len(store.jobs()), 1)
            self.assertEqual(store.jobs()[0].recipient, "exact-origin@s.whatsapp.net")
            with self.assertRaises(ValueError):
                store.approve(
                    str(source),
                    "wrong-recipient@g.us",
                    "reply",
                    0.25,
                    message_id="stable-id",
                )

    def test_send_command_uses_bound_recipient_without_fallback(self):
        command = voice_send_command(
            "/usr/local/bin/wacli",
            "/tmp/message.ogg",
            "exact-origin@s.whatsapp.net",
            "60s",
        )
        self.assertEqual(command[command.index("--to") + 1], "exact-origin@s.whatsapp.net")
        self.assertNotIn("configured-family-group@g.us", command)
        with self.assertRaises(ValueError):
            voice_send_command("wacli", "/tmp/message.ogg", "", "60s")

    def test_restart_removes_unapproved_and_quarantines_ambiguous_send(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            write_wav(source)
            store = OutboxStore(str(Path(directory) / "outbox"))
            pending = store.approve(str(source), "family@g.us", "standalone", 0.25)
            sending = store.approve(str(source), "person@s.whatsapp.net", "reply", 0.25)
            store.set_state(sending, "sending")
            staging = Path(store.root) / ".abandoned.tmp"
            staging.mkdir()
            (staging / "audio.wav").write_bytes(b"partial")
            uncertain = store.recover_startup()
            self.assertEqual(uncertain, [sending.message_id])
            self.assertFalse(staging.exists())
            self.assertEqual(store.load(pending.path).state, "pending")
            self.assertEqual(store.load(sending.path).state, "uncertain")


class InboxRestartTests(unittest.TestCase):
    def test_interrupted_inbound_returns_to_front_with_routing_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory)
            wav = queue / "0001-message.wav"
            meta = Path(str(wav) + ".json")
            write_wav(wav)
            meta.write_text(json.dumps({"chat": "origin@g.us"}), encoding="utf-8")
            claimed = claim_inbox_file(directory, wav.name)
            self.assertFalse(wav.exists())
            self.assertTrue(claimed.exists())
            recovered = recover_inflight_files(directory)
            self.assertEqual(recovered, [wav.name])
            self.assertTrue(wav.exists())
            self.assertEqual(json.loads(meta.read_text())["chat"], "origin@g.us")

    def test_release_and_finish_are_scoped_to_claimed_message(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory)
            first = queue / "0001.wav"
            second = queue / "0002.wav"
            write_wav(first)
            write_wav(second)
            claimed = claim_inbox_file(directory, first.name)
            release_inbox_file(directory, claimed)
            self.assertEqual(
                sorted(path.name for path in queue.glob("*.wav")),
                ["0001.wav", "0002.wav"],
            )
            claimed = claim_inbox_file(directory, first.name)
            finish_inbox_file(claimed)
            self.assertEqual(
                sorted(path.name for path in queue.glob("*.wav")), ["0002.wav"]
            )


class FakeIO:
    def __init__(self, recordings, approve_initial=False, approve_warning=False):
        self.recordings = list(recordings)
        self.approve_initial = approve_initial
        self.approve_warning = approve_warning
        self.calls = []

    def play_ordinary(self, path):
        self.calls.append(("ordinary", os.path.basename(path)))
        # Any simulated press here has no return channel and therefore cannot
        # leak into the next state.

    def record(self):
        self.calls.append(("record",))
        return self.recordings.pop(0)

    def wait_for_approval(self, timeout):
        self.calls.append(("wait", timeout))
        return self.approve_initial

    def play_warning_for_approval(self, path):
        self.calls.append(("warning", os.path.basename(path)))
        return self.approve_warning

    def delete(self, path):
        self.calls.append(("delete", os.path.basename(path)))


class SessionTests(unittest.TestCase):
    def _paths(self, directory):
        paths = {}
        for name in ("incoming", "reply", "standalone", "send", "warning", "not-sent"):
            path = Path(directory) / f"{name}.wav"
            write_wav(path)
            paths[name] = str(path)
        return paths

    def test_inbound_warning_press_approves_exact_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._paths(directory)
            io = FakeIO(
                [RecordingResult(paths["reply"], 0.25, True)],
                approve_initial=False,
                approve_warning=True,
            )
            events = []
            store = OutboxStore(str(Path(directory) / "outbox"))
            session = GuidedSession(io, store, lambda kind, **data: events.append((kind, data)))
            result = session.run(
                recipient="origin@g.us",
                flow_kind="reply",
                countdown_path=paths["reply"],
                send_prompt_path=paths["send"],
                delete_warning_path=paths["warning"],
                not_sent_path=paths["not-sent"],
                incoming_path=paths["incoming"],
            )
            self.assertEqual(result, "approved")
            self.assertEqual(store.jobs()[0].recipient, "origin@g.us")
            self.assertEqual(io.calls[0], ("ordinary", "incoming.wav"))
            self.assertEqual(io.calls[-1], ("delete", "reply.wav"))

    def test_inbound_playback_does_not_record_when_auto_record_is_off(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._paths(directory)
            io = FakeIO([])
            events = []
            store = OutboxStore(str(Path(directory) / "outbox"))
            session = GuidedSession(
                io, store, lambda kind, **data: events.append((kind, data))
            )
            result = session.run(
                recipient="origin@g.us",
                flow_kind="reply",
                countdown_path=paths["reply"],
                send_prompt_path=paths["send"],
                delete_warning_path=paths["warning"],
                not_sent_path=paths["not-sent"],
                incoming_path=paths["incoming"],
                auto_record_after_incoming=False,
            )
            self.assertEqual(result, "played")
            self.assertEqual(io.calls, [("ordinary", "incoming.wav")])
            self.assertFalse(store.jobs())
            self.assertIn("guided_playback_only", [kind for kind, _ in events])

    def test_no_press_deletes_and_consecutive_session_still_works(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._paths(directory)
            io = FakeIO(
                [
                    RecordingResult(paths["reply"], 0.25, True),
                    RecordingResult(paths["standalone"], 0.25, True),
                ],
                approve_initial=False,
                approve_warning=False,
            )
            store = OutboxStore(str(Path(directory) / "outbox"))
            session = GuidedSession(io, store, lambda *args, **kwargs: None)
            common = dict(
                recipient="family@g.us",
                countdown_path=paths["standalone"],
                send_prompt_path=paths["send"],
                delete_warning_path=paths["warning"],
                not_sent_path=paths["not-sent"],
            )
            self.assertEqual(session.run(flow_kind="standalone", **common), "deleted")
            io.approve_initial = True
            self.assertEqual(session.run(flow_kind="standalone", **common), "approved")
            self.assertEqual(len(store.jobs()), 1)

    def test_empty_recording_returns_without_review_or_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._paths(directory)
            io = FakeIO([RecordingResult(None, 20.0, False)])
            store = OutboxStore(str(Path(directory) / "outbox"))
            session = GuidedSession(io, store, lambda *args, **kwargs: None)
            result = session.run(
                recipient="family@g.us",
                flow_kind="standalone",
                countdown_path=paths["standalone"],
                send_prompt_path=paths["send"],
                delete_warning_path=paths["warning"],
                not_sent_path=paths["not-sent"],
            )
            self.assertEqual(result, "empty")
            self.assertFalse(store.jobs())
            self.assertFalse(any(call[0] == "wait" for call in io.calls))

    def test_caller_supplied_session_id_correlates_every_session_event(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._paths(directory)
            io = FakeIO(
                [RecordingResult(paths["standalone"], 0.25, True)],
                approve_initial=True,
            )
            store = OutboxStore(str(Path(directory) / "outbox"))
            events = []
            session = GuidedSession(
                io, store, lambda kind, **data: events.append((kind, data))
            )
            session.run(
                recipient="family@g.us",
                flow_kind="standalone",
                countdown_path=paths["standalone"],
                send_prompt_path=paths["send"],
                delete_warning_path=paths["warning"],
                not_sent_path=paths["not-sent"],
                session_id="stable-session-id",
            )
            self.assertTrue(events)
            self.assertTrue(
                all(data["session_id"] == "stable-session-id" for _, data in events)
            )


if __name__ == "__main__":
    unittest.main()
