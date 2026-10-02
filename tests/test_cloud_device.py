import json
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from messagebox.cloud_device import (
    CloudDeviceClient,
    CloudDeviceError,
    CloudSendRejected,
    CloudSendUncertain,
    CloudVoiceNotFound,
    DeviceIdentityStore,
)


class _Response:
    status = 200

    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return self.payload[:limit]


class CloudIdentityTests(unittest.TestCase):
    def test_identity_survives_repeat_load_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.json"
            store = DeviceIdentityStore(path)
            first = store.load_or_create()
            self.assertEqual(first, store.load_or_create())
            self.assertGreaterEqual(len(first["credential"]), 43)
            self.assertEqual(path.stat().st_mode & 0o007, 0)
            path.write_text('{"device_id":"bad","credential":"bad"}', encoding="utf-8")
            with self.assertRaises(CloudDeviceError):
                store.load_or_create()

    def test_identity_rejects_symlink_and_public_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "device.json"
            store = DeviceIdentityStore(path)
            store.load_or_create()
            path.chmod(0o664)
            with self.assertRaises(CloudDeviceError):
                store.load_or_create()
            path.unlink()
            path.symlink_to(root / "missing.json")
            with self.assertRaises(CloudDeviceError):
                store.load_or_create()

    def test_existing_group_lock_is_not_chmoded_by_second_service_user(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeviceIdentityStore(Path(directory) / "device.json")
            original = store.load_or_create()
            with mock.patch("messagebox.cloud_device.os.fchmod", side_effect=PermissionError("other owner")):
                self.assertEqual(store.load_or_create(), original)

    def test_root_completion_can_reuse_group_lock_without_supplementary_group(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeviceIdentityStore(Path(directory) / "device.json")
            original = store.load_or_create()
            with mock.patch("messagebox.cloud_device.os.geteuid", return_value=0), \
                 mock.patch("messagebox.cloud_device.os.getgroups", return_value=[]), \
                 mock.patch("messagebox.cloud_device.os.getegid", return_value=-1), \
                 mock.patch("messagebox.cloud_device.os.fchmod", side_effect=PermissionError("other owner")):
                self.assertEqual(store.load_or_create(), original)

    def test_client_scopes_all_requests_to_exact_api_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = DeviceIdentityStore(Path(directory) / "device.json").load_or_create()
            requests = []

            def opener(request, *, timeout):
                requests.append(request)
                return _Response({"ok": True})

            client = CloudDeviceClient(
                "https://example.invalid/cloud-api/v1", identity, opener=opener
            )
            client.register({"audio": True})
            client.ack({"operation_id": "synthetic", "state": "received"})
            self.assertEqual(
                [request.full_url for request in requests],
                [
                    "https://example.invalid/cloud-api/v1/device/register",
                    "https://example.invalid/cloud-api/v1/device/ack",
                ],
            )
            self.assertEqual(
                requests[0].get_header("Authorization"),
                "Bearer " + identity["credential"],
            )
            self.assertIn(identity["credential"].encode(), requests[0].data)
            self.assertNotIn(identity["credential"].encode(), requests[1].data)
            for unsafe in (
                "http://example.invalid/cloud-api/v1",
                "https://example.invalid/other/v1",
                "https://user:pass@example.invalid/cloud-api/v1",
            ):
                with self.subTest(url=unsafe), self.assertRaises(CloudDeviceError):
                    CloudDeviceClient(unsafe, identity)
            for unsafe in (
                "https://other.invalid/cloud-api/v1/device/media/a",
                "https://example.invalid/cloud-api/v1/boxes/a",
                "https://example.invalid/cloud-api/v1/device/media/a?token=x",
            ):
                with self.subTest(url=unsafe), self.assertRaises(CloudDeviceError):
                    client.media(unsafe)

    def test_voice_upload_uses_one_scoped_target_and_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = DeviceIdentityStore(root / "device.json").load_or_create()
            audio = root / "voice.ogg"
            audio.write_bytes(b"OggSsynthetic")
            requests = []

            def opener(request, *, timeout):
                requests.append(request)
                return _Response({"message_id": "synthetic-mid", "state": "queued",
                                  "expires_at": 1_800_604_800, "server_time": 1_800_000_000})

            client = CloudDeviceClient(
                "https://example.invalid/cloud-api/v1", identity, opener=opener
            )
            result = client.send_voice(audio, "person_1", "synthetic_key_12345", 1.25, account_scope="a" * 64)
            self.assertEqual(result["state"], "queued")
            [request] = requests
            self.assertEqual(request.full_url, "https://example.invalid/cloud-api/v1/device/voice")
            self.assertEqual(request.get_header("Idempotency-key"), "synthetic_key_12345")
            self.assertIn(b'name="recipient_id"\r\n\r\nperson_1', request.data)
            self.assertIn(b'name="idempotency_key"\r\n\r\nsynthetic_key_12345', request.data)
            self.assertIn(b'name="account_scope"\r\n\r\n' + b"a" * 64, request.data)
            self.assertIn(b"OggSsynthetic", request.data)
            self.assertNotIn(b'name="box"', request.data)
            with self.assertRaises(CloudSendRejected):
                client.send_voice(audio, "other household", "synthetic_key_12345", 1.25, account_scope="a" * 64)
            self.assertEqual(len(requests), 1)

    def test_voice_accepts_cloud_create_202_and_keyed_replay_200(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = DeviceIdentityStore(root / "device.json").load_or_create()
            audio = root / "voice.ogg"
            audio.write_bytes(b"OggSsynthetic")
            responses = [
                _Response({"message_id": "cloud-message", "state": "waiting_for_reply",
                           "expires_at": 1_800_604_800, "server_time": 1_800_000_000}, 202),
                _Response({"message_id": "cloud-message", "state": "waiting_for_reply",
                           "expires_at": 1_800_604_800, "server_time": 1_800_000_100}, 200),
            ]
            client = CloudDeviceClient("https://example.invalid/cloud-api/v1", identity,
                                       opener=lambda *_args, **_kwargs: responses.pop(0))
            for _ in range(2):
                result = client.send_voice(audio, "person_1", "synthetic_key_12345", 1.25, account_scope="a" * 64)
                self.assertEqual(result["state"], "waiting_for_reply")

    def test_read_only_voice_status_binds_known_expiry_without_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = DeviceIdentityStore(Path(directory) / "device.json").load_or_create()
            requests = []
            def opener(request, *, timeout):
                requests.append(request)
                return _Response({"message_id": "cloud-message", "state": "delivered",
                                  "expires_at": 1_800_604_800, "server_time": 1_800_000_100,
                                  "deleted": False})
            client = CloudDeviceClient("https://example.invalid/cloud-api/v1", identity, opener=opener)
            result = client.voice_status("original_key_123456")
            self.assertEqual(result["expires_at"], 1_800_604_800)
            self.assertEqual(requests[0].get_method(), "GET")
            self.assertTrue(requests[0].full_url.endswith("/device/voice/status?idempotency_key=original_key_123456"))

    def test_voice_status_exact_not_found_is_distinct_from_network_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = DeviceIdentityStore(Path(directory) / "device.json").load_or_create()
            def missing(request, *, timeout):
                payload = b'{"error":{"code":"voice_upload_not_found","message":"not found"}}'
                raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, io.BytesIO(payload))
            client = CloudDeviceClient("https://example.invalid/cloud-api/v1", identity, opener=missing)
            with self.assertRaises(CloudVoiceNotFound):
                client.voice_status("original_key_123456")

    def test_voice_rejection_and_unknown_result_stay_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = DeviceIdentityStore(root / "device.json").load_or_create()
            audio = root / "voice.ogg"
            audio.write_bytes(b"OggSsynthetic")

            def rejected(request, *, timeout):
                raise urllib.error.HTTPError(request.full_url, 403, "rejected", {}, io.BytesIO())

            def ambiguous(_request, *, timeout):
                raise TimeoutError()

            for opener, error in ((rejected, CloudSendRejected), (ambiguous, CloudSendUncertain)):
                with self.subTest(error=error), self.assertRaises(error):
                    CloudDeviceClient("https://example.invalid/cloud-api/v1", identity, opener=opener).send_voice(
                        audio, "person_1", "synthetic_key_12345", 1.25, account_scope="a" * 64
                    )


if __name__ == "__main__":
    unittest.main()
