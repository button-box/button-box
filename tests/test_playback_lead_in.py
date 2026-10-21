"""An inbound message after a cue starts with a short silence (USB speaker settle)."""

from speaker_helpers import isolate_audio

import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from messagebox.guided_reply import GuidedSession, OutboxStore
from test_mic_access import runtime


def write_wav(path, frames, rate=48000, width=2):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(frames)


class LeadInTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)

    def test_copy_starts_with_150_ms_of_silence_then_the_exact_message(self):
        for width, silence in ((2, b"\x00\x00"), (1, b"\x80")):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "message.wav"
                body = bytes(range(1, 201)) * 120
                write_wav(source, body, rate=16000, width=width)
                with mock.patch.dict(os.environ, {"RUNTIME_DIRECTORY": directory}):
                    padded = runtime.with_lead_in(source)
                self.assertEqual(padded.parent, Path(directory))
                with wave.open(str(padded), "rb") as result, wave.open(str(source), "rb") as original:
                    self.assertEqual(result.getparams()[:3], original.getparams()[:3])
                    lead = int(16000 * runtime.LEAD_IN_S)
                    self.assertEqual(result.getnframes(), original.getnframes() + lead)
                    self.assertEqual(result.readframes(lead), silence * lead)
                    self.assertEqual(result.readframes(original.getnframes()), body)

    def test_unreadable_audio_has_no_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, data in (("text.wav", b"not a wav"), ("missing.wav", None)):
                path = Path(directory) / name
                if data is not None:
                    path.write_bytes(data)
                with mock.patch.dict(os.environ, {"RUNTIME_DIRECTORY": directory}):
                    self.assertIsNone(runtime.with_lead_in(path))
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["text.wav"])

    def test_after_cue_plays_padded_copy_and_removes_it_or_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "message.wav"
            write_wav(source, b"\x01\x02" * 4800)
            played = []
            with mock.patch.dict(os.environ, {"RUNTIME_DIRECTORY": directory}), \
                 mock.patch.object(runtime, "play_audio_ordinary", side_effect=lambda path: played.append(Path(path))):
                runtime.play_audio_after_cue(source)
                self.assertNotEqual(played[-1], source)
                self.assertFalse(played[-1].exists())
                broken = Path(directory) / "broken.wav"
                broken.write_bytes(b"x")
                runtime.play_audio_after_cue(broken)
                self.assertEqual(played[-1], broken)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["broken.wav", "message.wav"])

    def test_padded_copy_is_removed_when_playback_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "message.wav"
            write_wav(source, b"\x01\x02" * 4800)
            with mock.patch.dict(os.environ, {"RUNTIME_DIRECTORY": directory}), \
                 mock.patch.object(runtime, "play_audio_ordinary", side_effect=OSError("speaker")):
                with self.assertRaises(OSError):
                    runtime.play_audio_after_cue(source)
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["message.wav"])


class IO:
    def __init__(self, lead_in=True):
        self.calls = []
        if lead_in:
            self.play_after_cue = lambda path: self.calls.append(("after_cue", path))

    def play_ordinary(self, path):
        self.calls.append(("ordinary", path))


class SessionLeadInTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)

    def run_session(self, io, **paths):
        with tempfile.TemporaryDirectory() as directory:
            store = OutboxStore(Path(directory) / "outbox", transport="wacli")
            return GuidedSession(io, store, lambda kind, **data: None).run(
                recipient="origin@example.invalid", flow_kind="reply", deleted_cue_path="cue-deleted.wav",
                incoming_path="incoming.wav", auto_record_after_incoming=False, **paths)

    def test_message_after_cue_uses_lead_in_and_end_cue_stays_ordinary(self):
        io = IO()
        self.assertEqual(self.run_session(io, incoming_cue_path="cue-msg_start.wav",
                                          incoming_end_path="cue-msg_end.wav"), "played")
        self.assertEqual(io.calls, [("ordinary", "cue-msg_start.wav"), ("after_cue", "incoming.wav"),
                                    ("ordinary", "cue-msg_end.wav")])

    def test_no_cue_or_io_without_lead_in_plays_ordinary(self):
        io = IO()
        self.run_session(io)
        self.assertEqual(io.calls, [("ordinary", "incoming.wav")])
        io = IO(lead_in=False)
        self.run_session(io, incoming_cue_path="cue-msg_start.wav")
        self.assertEqual(io.calls, [("ordinary", "cue-msg_start.wav"), ("ordinary", "incoming.wav")])

    def test_runtime_io_wires_lead_in(self):
        with mock.patch.object(runtime, "play_audio_after_cue") as after_cue:
            runtime.PiGuidedIO("r", "s", 30).play_after_cue("incoming.wav")
        after_cue.assert_called_once_with("incoming.wav")


if __name__ == "__main__":
    unittest.main()
