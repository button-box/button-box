import io
import json
import subprocess
import unittest
import urllib.error

from messagebox.providers import (
    ProviderError,
    SignalProvider,
    WhatsAppProvider,
    provider_for_channel,
)


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class WhatsAppProviderTests(unittest.TestCase):
    def test_send_voice_builds_exact_wacli_command_and_parses_id(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return FakeCompletedProcess(
                returncode=0, stdout=json.dumps({"sent": True, "id": "ABC123"})
            )

        provider = WhatsAppProvider("/usr/local/bin/wacli", run=fake_run)
        result = provider.send_voice("15551234567@s.whatsapp.net", "/tmp/x.ogg", lock_wait="60s")

        self.assertTrue(result.ok)
        self.assertEqual(result.provider_message_id, "ABC123")
        [(command, kwargs)] = calls
        self.assertEqual(
            command,
            [
                "/usr/local/bin/wacli",
                "send",
                "voice",
                "--file",
                "/tmp/x.ogg",
                "--to",
                "15551234567@s.whatsapp.net",
                "--lock-wait",
                "60s",
                "--json",
            ],
        )
        self.assertEqual(kwargs, {"capture_output": True, "text": True})

    def test_send_voice_failure_has_no_message_id(self):
        provider = WhatsAppProvider(
            run=lambda *a, **k: FakeCompletedProcess(returncode=1, stdout="", stderr="boom")
        )
        result = provider.send_voice("x@s.whatsapp.net", "/tmp/x.ogg", lock_wait="60s")
        self.assertFalse(result.ok)
        self.assertIsNone(result.provider_message_id)

    def test_react_is_fire_and_forget_and_includes_sender_when_given(self):
        calls = []
        provider = WhatsAppProvider("/wacli", popen=lambda cmd, **k: calls.append(cmd))
        provider.react("chat@g.us", "MSG1", "\U0001f3a7", sender="s@s.whatsapp.net", lock_wait="60s")
        self.assertEqual(
            calls[0],
            [
                "/wacli",
                "send",
                "react",
                "--to",
                "chat@g.us",
                "--id",
                "MSG1",
                "--reaction",
                "\U0001f3a7",
                "--lock-wait",
                "60s",
                "--sender",
                "s@s.whatsapp.net",
            ],
        )

    def test_presence_maps_recording_to_typing_and_default_to_paused(self):
        calls = []
        provider = WhatsAppProvider("/wacli", popen=lambda cmd, **k: calls.append(cmd))
        provider.set_presence("recording", "r@s.whatsapp.net", lock_wait="60s")
        provider.set_presence("stopped", "r@s.whatsapp.net", lock_wait="60s")
        self.assertEqual(
            calls[0],
            ["/wacli", "presence", "typing", "--media", "audio", "--to", "r@s.whatsapp.net", "--lock-wait", "60s"],
        )
        self.assertEqual(
            calls[1],
            ["/wacli", "presence", "paused", "--to", "r@s.whatsapp.net", "--lock-wait", "60s"],
        )


class FakeHTTPResponse:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8") if payload is not None else b""

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SignalProviderTests(unittest.TestCase):
    def test_send_voice_posts_base64_attachment_and_returns_timestamp_id(self, tmp_path=None):
        import tempfile
        import os

        requests = []

        def opener(request, timeout):
            requests.append(request)
            return FakeHTTPResponse(201, {"timestamp": 1234567890})

        with tempfile.TemporaryDirectory() as directory:
            audio_path = os.path.join(directory, "clip.ogg")
            with open(audio_path, "wb") as handle:
                handle.write(b"fake-audio-bytes")

            provider = SignalProvider("http://127.0.0.1:8080", "+15550001111", opener=opener)
            result = provider.send_voice("+15550002222", audio_path, lock_wait=None)

        self.assertTrue(result.ok)
        self.assertEqual(result.provider_message_id, "1234567890")
        [request] = requests
        self.assertEqual(request.full_url, "http://127.0.0.1:8080/v2/send")
        body = json.loads(request.data)
        self.assertEqual(body["number"], "+15550001111")
        self.assertEqual(body["recipients"], ["+15550002222"])
        self.assertEqual(len(body["base64_attachments"]), 1)

    def test_send_voice_http_error_is_reported_as_failed_result(self):
        import tempfile
        import os

        def opener(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 400, "bad", {}, io.BytesIO(b'{"error":"bad number"}')
            )

        with tempfile.TemporaryDirectory() as directory:
            audio_path = os.path.join(directory, "clip.ogg")
            with open(audio_path, "wb") as handle:
                handle.write(b"x")
            provider = SignalProvider("http://x", "+1", opener=opener)
            result = provider.send_voice("+2", audio_path, lock_wait=None)

        self.assertFalse(result.ok)
        self.assertIsNone(result.provider_message_id)

    def test_send_voice_network_failure_does_not_raise(self):
        import tempfile
        import os

        def opener(request, timeout):
            raise urllib.error.URLError("connection refused")

        with tempfile.TemporaryDirectory() as directory:
            audio_path = os.path.join(directory, "clip.ogg")
            with open(audio_path, "wb") as handle:
                handle.write(b"x")
            provider = SignalProvider("http://x", "+1", opener=opener)
            result = provider.send_voice("+2", audio_path, lock_wait=None)

        self.assertFalse(result.ok)

    def test_react_swallows_provider_errors(self):
        def opener(request, timeout):
            raise urllib.error.URLError("down")

        provider = SignalProvider("http://x", "+1", opener=opener)
        provider.react("+2", "1700000000", "\U0001f3a7")  # must not raise

    def test_receive_returns_envelope_list(self):
        def opener(request, timeout):
            return FakeHTTPResponse(200, [{"envelope": {"source": "+1"}}])

        provider = SignalProvider("http://x", "+1", opener=opener)
        self.assertEqual(provider.receive(), [{"envelope": {"source": "+1"}}])

    def test_receive_returns_empty_list_on_error(self):
        def opener(request, timeout):
            raise urllib.error.URLError("down")

        provider = SignalProvider("http://x", "+1", opener=opener)
        self.assertEqual(provider.receive(), [])

    def test_fetch_attachment_returns_bytes(self):
        class FakeBinaryResponse(FakeHTTPResponse):
            def __init__(self):
                self.status = 200
                self._body = b"raw-audio-bytes"

        provider = SignalProvider("http://x", "+1", opener=lambda r, timeout: FakeBinaryResponse())
        self.assertEqual(provider.fetch_attachment("abc"), b"raw-audio-bytes")

    def test_presence_uses_put_and_delete(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return FakeHTTPResponse(204, None)

        provider = SignalProvider("http://x", "+1", opener=opener)
        provider.set_presence("recording", "+2")
        provider.set_presence("stopped", "+2")
        self.assertEqual(requests[0].get_method(), "PUT")
        self.assertEqual(requests[1].get_method(), "DELETE")


class ProviderFactoryTests(unittest.TestCase):
    def test_dispatches_by_channel(self):
        self.assertIsInstance(provider_for_channel("whatsapp"), WhatsAppProvider)
        self.assertIsInstance(provider_for_channel("signal"), SignalProvider)
        with self.assertRaises(ValueError):
            provider_for_channel("telegram")


if __name__ == "__main__":
    unittest.main()
