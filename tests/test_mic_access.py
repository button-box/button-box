"""Capture authority and regression inventory; no GPIO or audio hardware."""

from speaker_helpers import isolate_audio

import ast
import contextlib
from pathlib import Path
import re
import subprocess
import sys
import threading
import types
import unittest
from unittest import mock

with mock.patch.dict(sys.modules, {"gpiozero": types.SimpleNamespace(Button=object, LED=object)}):
    from messagebox import button_send as runtime

ROOT = Path(__file__).resolve().parents[1]


class MicAccessTests(unittest.TestCase):
    def setUp(self):
        isolate_audio(self)

    def test_synthetic_input_cannot_mint_a_physical_permit(self):
        class PhysicalButton:
            is_pressed = True
        with mock.patch.dict(runtime.capture_session.__wrapped__.__globals__, {"Button": PhysicalButton}):
            with self.assertRaises(RuntimeError), runtime.capture_session(types.SimpleNamespace(is_pressed=True)):
                self.fail("synthetic switch accepted")
            with runtime.capture_session(PhysicalButton()) as permit:
                runtime.require_permit(permit)

    def test_no_capture_without_a_press_in_both_transports(self):
        for mode in ("cloud", "wacli"):
            with self.subTest(mode=mode), contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(runtime, "transport_mode", return_value=mode))
                stack.enter_context(mock.patch.object(runtime, "button", types.SimpleNamespace(is_pressed=False), create=True))
                cue = stack.enter_context(mock.patch.object(runtime, "play_sound"))
                spawn = stack.enter_context(mock.patch.object(runtime.subprocess, "Popen"))
                flow = stack.enter_context(mock.patch.object(runtime, "_handle_confirmed_press"))
                self.assertFalse(runtime.handle_confirmed_press(0))
                for permit in (None, object()):
                    with self.assertRaises(RuntimeError):
                        runtime.capture_guided_recording("synthetic", press_token=permit)
                with self.assertRaises(RuntimeError):
                    runtime.PiGuidedIO("synthetic", "session", 60).record()
                cue.assert_not_called()
                spawn.assert_not_called()
                flow.assert_not_called()

    def test_short_press_is_rejected_and_session_is_one_shot_thread_bound_and_revoked(self):
        switch = types.SimpleNamespace(is_pressed=True)
        with mock.patch.object(runtime.time, "sleep", side_effect=lambda _: setattr(switch, "is_pressed", False)):
            with self.assertRaises(RuntimeError), runtime.capture_session(switch):
                self.fail("short press accepted")
        switch.is_pressed = True
        spawn = mock.Mock(side_effect=OSError("synthetic capture failure"))
        with runtime.capture_session(switch) as permit:
            failures = []
            def other_thread():
                try:
                    runtime.open_microphone(permit, "default", popen=spawn)
                except RuntimeError:
                    failures.append(True)
            worker = threading.Thread(target=other_thread)
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [True])
            spawn.assert_not_called()
            # Tap mode releases before the cue; its synchronous session retains consent.
            switch.is_pressed = False
            with self.assertRaises(OSError):
                runtime.open_microphone(permit, "default", popen=spawn)
            with self.assertRaises(RuntimeError):
                runtime.open_microphone(permit, "default", popen=spawn)
            self.assertEqual(spawn.call_count, 1)
        with self.assertRaises(RuntimeError):
            runtime.require_permit(permit)

    def test_dispatch_revokes_permission_after_flow_failure(self):
        saved = []
        def flow(*args, **kwargs):
            saved.append(runtime.PiGuidedIO("synthetic", "session", 60).press_token)
            raise OSError("synthetic flow failure")
        with mock.patch.object(runtime, "button", types.SimpleNamespace(is_pressed=True), create=True), \
             mock.patch.object(runtime, "_handle_confirmed_press", side_effect=flow):
            with self.assertRaises(OSError):
                runtime.handle_confirmed_press(0)
        self.assertIsNone(runtime._capture_permit)
        with self.assertRaises(RuntimeError):
            runtime.require_permit(saved[0])

    def test_presence_failure_reaps_recorder_even_when_terminate_times_out(self):
        import tempfile
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("arecord", 3), 0]
        led = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            permit = stack.enter_context(runtime.capture_session(types.SimpleNamespace(is_pressed=True)))
            stack.enter_context(mock.patch.object(runtime, "TEMP_DIR", directory))
            stack.enter_context(mock.patch.object(runtime, "button", types.SimpleNamespace(is_pressed=False), create=True))
            stack.enter_context(mock.patch.object(runtime, "led", led, create=True))
            stack.enter_context(mock.patch.object(runtime, "play_sound"))
            stack.enter_context(mock.patch.object(runtime, "presence", side_effect=OSError("synthetic presence failure")))
            stack.enter_context(mock.patch.object(runtime, "RecordingLimitCue", return_value=mock.Mock(process=None, intervals=[], played=False)))
            stack.enter_context(mock.patch.object(runtime.subprocess, "Popen", return_value=process))
            with self.assertRaises(OSError):
                runtime.capture_guided_recording("synthetic", press_token=permit)
            process.terminate.assert_called_once()
            process.kill.assert_called_once()
            led.off.assert_called_once()
            self.assertFalse(runtime._recording)

    def test_only_reviewed_sources_can_open_capture(self):
        # Fail on new capture tools/backends throughout shipped code and helpers.
        forbidden = re.compile(
            r"\b(?:arecord|parecord|pw-record|rec|pyaudio|sounddevice|alsaaudio)\b"
            r"|/dev/snd/pcm\S*c\b|(?:-[fi]\s+['\"]?alsa\b)", re.I)
        for folder in ("messagebox", "scripts", "systemd", "config"):
            for path in (ROOT / folder).rglob("*"):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                text = path.read_text()
                if path.suffix == ".py":
                    tree = ast.parse(text)
                    # Inspect executable strings/imports, excluding comments/docstrings.
                    parts = []
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Constant) and isinstance(node.value, str):
                            parts.append(node.value)
                        if isinstance(node, (ast.Import, ast.ImportFrom)):
                            parts.append(ast.unparse(node))
                    text = "\n".join(parts)
                relative = path.relative_to(ROOT).as_posix()
                if relative == "messagebox/mic_capture.py":
                    self.assertEqual(text.count("arecord"), 1)
                elif relative == "scripts/install/bounded_update.py":
                    # The health probe is permitted to enumerate capture cards,
                    # never open a capture PCM. Lock this exact command shape.
                    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                             and isinstance(node.func, ast.Name) and node.func.id == "_run"
                             and node.args and "tool" in ast.unparse(node.args[0])]
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(ast.unparse(calls[0].args[0]),
                                     "['runuser', '-u', 'messagebox', '--', tool, '-l']")
                    self.assertEqual(text.count("arecord"), 1)
                    self.assertIsNone(forbidden.search(text.replace("arecord", "")), relative)
                else:
                    # Error messages refer to the recorder without executing it.
                    text = text.replace("arecord exited unexpectedly", "")
                    self.assertIsNone(forbidden.search(text), relative)

    def test_capture_call_graph_has_no_remote_entry(self):
        expected = {
            "capture_session": {("messagebox/button_send.py", "_capture_confirmed_press")},
            "open_microphone": {("messagebox/button_send.py", "capture_guided_recording")},
            "capture_guided_recording": {("messagebox/button_send.py", "record")},
            "_handle_confirmed_press": {("messagebox/button_send.py", "_capture_confirmed_press")},
            "_capture_confirmed_press": {("messagebox/button_send.py", "handle_confirmed_press")},
            "run_guided_once": {("messagebox/button_send.py", "_handle_confirmed_press")},
            "record_and_send_legacy": {("messagebox/button_send.py", "_handle_confirmed_press")},
        }
        found = {key: set() for key in expected}
        class Calls(ast.NodeVisitor):
            function = "<module>"
            def visit_FunctionDef(self, node):
                previous = self.function
                self.function = node.name
                self.generic_visit(node)
                self.function = previous
            def visit_Call(self, node):
                name = (node.func.id if isinstance(node.func, ast.Name)
                        else node.func.attr if isinstance(node.func, ast.Attribute) else "")
                if name in found:
                    found[name].add((relative, self.function))
                self.generic_visit(node)
        for path in (ROOT / "messagebox").rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            Calls().visit(ast.parse(path.read_text()))
        self.assertEqual(found, expected)
        hardware = (ROOT / "scripts/dev/hardware-test.sh").read_text()
        self.assertIn("with capture_session(button) as permit:", hardware)
        self.assertIn('open_microphone(permit, os.environ["MIC_DEV"])', hardware)
        # The developer test shares the same guard and does not invoke capture tools.
        self.assertNotIn("arecord", hardware)
