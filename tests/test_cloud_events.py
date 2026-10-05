import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import urlsplit

from messagebox.cloud_events import CloudWorkEvents, connect_events
from messagebox.cloud_runtime import CloudRuntime
from messagebox.cloud_device import CloudDeviceClient, CloudDeviceError


class ClockEvent:
    def __init__(self, clock, *, advance=None):
        self.clock = clock
        self.value = False
        self.advance = advance

    def is_set(self):
        return self.value

    def set(self):
        self.value = True

    def clear(self):
        self.value = False

    def wait(self, timeout):
        self.clock[0] += timeout
        if self.advance:
            self.advance(self.clock[0])
        return self.value


class EventsTests(unittest.TestCase):
    def test_connection_uses_only_same_origin_wss_header_auth_and_no_redirect(self):
        client = mock.Mock(api_url="https://example.invalid/cloud-api/v1",
                           identity={"credential": "synthetic"})
        socket = mock.Mock()
        socket.getstatus.return_value = 101
        with mock.patch("websocket.create_connection", return_value=socket) as connect:
            self.assertIs(connect_events(client), socket)
        args, kwargs = connect.call_args
        self.assertEqual(args, ("wss://example.invalid/cloud-api/v1/device/events",))
        self.assertEqual(kwargs["header"]["Authorization"], "Bearer synthetic")
        self.assertEqual(kwargs["redirect_limit"], 0)
        self.assertEqual(kwargs["http_no_proxy"], ["*"])
        socket.getstatus.return_value = 302
        with mock.patch("websocket.create_connection", return_value=socket):
            with self.assertRaises(OSError):
                connect_events(client)
        socket.close.assert_called_once_with(timeout=0)

    def test_uppercase_https_keeps_validated_origin_and_header_only_credential(self):
        client = CloudDeviceClient("HTTPS://Example.invalid:443/cloud-api/v1",
            {"device_id": "synthetic-device-001", "credential": "x" * 43})
        socket = mock.Mock()
        socket.getstatus.return_value = 101
        with mock.patch("websocket.create_connection", return_value=socket) as connect:
            connect_events(client)
        url = connect.call_args.args[0]
        self.assertEqual(url, "wss://Example.invalid:443/cloud-api/v1/device/events")
        self.assertEqual(urlsplit(url).netloc, urlsplit(client.api_url).netloc)
        self.assertEqual(urlsplit(url).query, "")
        self.assertNotIn(client.identity["credential"], url)
        self.assertEqual(connect.call_args.kwargs["header"]["Authorization"],
                         "Bearer " + client.identity["credential"])

    def test_actual_library_does_not_follow_redirect_with_bearer_credential(self):
        import websocket
        client = mock.Mock(api_url="https://example.invalid/cloud-api/v1",
                           identity={"credential": "synthetic"})
        socket = mock.Mock()
        socket.send.return_value = 1000
        response = SimpleNamespace(status=302, headers={"location": "wss://other.invalid/events"})
        with mock.patch("websocket._core.connect", return_value=(socket, ("example.invalid", 443, "/cloud-api/v1/device/events"))) as connect, \
                mock.patch("websocket._core.handshake", return_value=response):
            with self.assertRaises((OSError, websocket.WebSocketException)):
                connect_events(client)
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(connect.call_args.args[0], "wss://example.invalid/cloud-api/v1/device/events")

    def test_only_exact_work_hint_sets_event_and_duplicate_hints_coalesce(self):
        events = CloudWorkEvents(object())
        socket = mock.Mock()
        socket.recv.side_effect = [b'{"type":"work"}', '{"type":"audio"}',
                                  '{"type":"work","payload":"ignored"}', 'bad',
                                  '{"type":"work"}', '{"type":"work"}', '']
        with self.assertRaises(OSError):
            events._listen(socket)
        self.assertTrue(events.wake.is_set())
        self.assertFalse(events.connected.is_set())
        events.wake.clear()
        socket.recv.side_effect = ['{"type":"work","payload":"ignored"}', '']
        with self.assertRaises(OSError):
            events._listen(socket)
        self.assertFalse(events.wake.is_set())

    def test_text_ping_pong_and_silent_loss_are_bounded(self):
        clock = [0.0]
        events = CloudWorkEvents(object(), monotonic=lambda: clock[0])
        socket = mock.Mock()
        def receive():
            clock[0] += 20 if clock[0] == 0 else 10
            if clock[0] == 30:
                return "pong"
            raise TimeoutError()
        socket.recv.side_effect = receive
        with self.assertRaises(OSError):
            events._listen(socket)
        self.assertEqual(socket.send.call_args_list, [mock.call("ping"), mock.call("ping")])
        self.assertEqual(clock[0], 50)

    def test_auth_unsupported_and_missing_dependency_back_off_without_busy_loop(self):
        for status in (None, 401, 403, 404):
            error = ImportError() if status is None else CloudDeviceError("synthetic")
            if status is not None:
                error.status_code = status
            events = CloudWorkEvents(object(), connector=mock.Mock(side_effect=error))
            events.stopped = mock.Mock()
            events.stopped.is_set.side_effect = [False, True]
            events._run()
            events.stopped.wait.assert_called_once_with(300)
            self.assertFalse(events.connected.is_set())
            self.assertTrue(events.wake.is_set())

    def test_repeated_disconnects_have_bounded_exponential_backoff(self):
        events = CloudWorkEvents(object(), connector=mock.Mock(side_effect=OSError()))
        events.stopped = mock.Mock()
        events.stopped.is_set.side_effect = [False] * 7 + [True]
        with mock.patch("messagebox.cloud_events.random.random", return_value=0):
            events._run()
        self.assertEqual(events.stopped.wait.call_args_list,
                         [mock.call(delay) for delay in (2, 4, 8, 16, 32, 60, 60)])

    def test_disconnect_reconnect_wakes_for_lost_hints_and_stop_aborts_receive(self):
        first = mock.Mock()
        first.recv.return_value = ""
        second = mock.Mock()
        entered = threading.Event()
        aborted = threading.Event()
        def receive():
            entered.set()
            aborted.wait(2)
            return ""
        second.recv.side_effect = receive
        second.abort.side_effect = aborted.set
        connector = mock.Mock(side_effect=[first, second])
        events = CloudWorkEvents(object(), connector=connector)
        original_wait = events.stopped.wait
        # Skip only reconnect delay; the second socket still blocks normally.
        events.stopped.wait = lambda delay: original_wait(min(delay, 0.01))
        events.start()
        self.assertTrue(entered.wait(1))
        self.assertTrue(events.connected.is_set())
        self.assertTrue(events.wake.is_set())
        self.assertEqual(connector.call_count, 2)
        events.stop()
        self.assertFalse(events.thread.is_alive())
        self.assertFalse(events.connected.is_set())
        first.close.assert_called_once_with(timeout=0)
        second.close.assert_called_once_with(timeout=0)


class RuntimeSchedulingTests(unittest.TestCase):
    def runtime(self, *, connected=True, until=65, changes=None):
        clock = [0.0]
        stop = ClockEvent(clock)
        def advance(now):
            if changes:
                changes(now, events)
            if now >= until:
                stop.set()
        stop.advance = advance
        events = mock.Mock()
        events.wake = ClockEvent(clock, advance=advance)
        events.connected = threading.Event()
        if connected:
            events.connected.set()
        runtime = object.__new__(CloudRuntime)
        runtime.state = {"snapshot": {"box_id": "synthetic"}}
        runtime.client = object()
        runtime.monotonic = lambda: clock[0]
        polls, beats = [], []
        runtime.poll_once = lambda: polls.append(clock[0])
        runtime.heartbeat = lambda: beats.append(clock[0])
        runtime._maintain_local = mock.Mock()
        return runtime, stop, events, polls, beats

    def test_lost_hint_backup_poll_and_heartbeat_every_thirty_seconds(self):
        runtime, stop, events, polls, beats = self.runtime()
        runtime.run(stop=stop, events=events)
        self.assertEqual(polls, [0, 30, 60])
        self.assertEqual(beats, [0, 30, 60])
        events.start.assert_called_once()
        events.stop.assert_called_once()

    def test_work_wakes_poll_immediately_and_disconnect_resumes_fast_polling(self):
        triggered = set()
        def change(now, events):
            if now >= 5 and "work" not in triggered:
                triggered.add("work")
                events.wake.set()
            if now >= 10 and "disconnect" not in triggered:
                triggered.add("disconnect")
                events.connected.clear()
                events.wake.set()
        runtime, stop, events, polls, beats = self.runtime(until=17, changes=change)
        with mock.patch("messagebox.cloud_runtime.random.random", return_value=0):
            runtime.run(stop=stop, events=events)
        self.assertEqual(polls, [0, 5, 10, 12, 14, 16])
        self.assertEqual(beats, [0])

    def test_cross_process_receipts_and_outbox_keep_fast_housekeeping(self):
        runtime, stop, events, polls, _ = self.runtime(until=7)
        times = []
        runtime._maintain_local = lambda: times.append(runtime.monotonic())
        with mock.patch("messagebox.cloud_runtime.random.random", return_value=0):
            runtime.run(stop=stop, events=events)
        self.assertEqual(polls, [0])
        self.assertEqual(times, [2, 4, 6])

    def test_local_housekeeping_failure_backs_off_without_flooding_inbox(self):
        runtime, stop, events, polls, _ = self.runtime(until=30)
        times = []
        def fail():
            times.append(runtime.monotonic())
            raise CloudDeviceError("synthetic")
        runtime._maintain_local = fail
        with mock.patch("messagebox.cloud_runtime.random.random", return_value=0), mock.patch("builtins.print"):
            runtime.run(stop=stop, events=events)
        self.assertEqual(polls, [0])
        self.assertEqual(times, [2, 6, 14])

    def test_duplicate_flood_is_coalesced_without_busy_loop(self):
        runtime, stop, events, polls, _ = self.runtime(until=2)
        def poll():
            polls.append(runtime.monotonic())
            events.wake.set()  # Hint arriving during HTTP must survive.
        runtime.poll_once = poll
        runtime.run(stop=stop, events=events)
        self.assertEqual(polls, [0, .25, .5, .75, 1, 1.25, 1.5, 1.75])

    def test_hint_cannot_bypass_http_failure_backoff_or_authorize_work(self):
        runtime, stop, events, polls, beats = self.runtime(until=65)
        def fail():
            polls.append(runtime.monotonic())
            events.wake.set()
            raise CloudDeviceError("synthetic HTTP rejection")
        runtime.poll_once = fail
        with mock.patch("messagebox.cloud_runtime.random.random", return_value=0), mock.patch("builtins.print"):
            runtime.run(stop=stop, events=events)
        self.assertEqual(polls, [0, 4, 12, 28, 60])
        self.assertEqual(beats, [0, 30, 60])

    def test_explicit_optout_retains_polling_and_stops(self):
        runtime, stop, events, polls, _ = self.runtime(connected=False, until=5)
        # A plain wake event is needed when there is no listener.
        with mock.patch.dict("os.environ", {"MSGBOX_CLOUD_EVENTS": "0"}), \
                mock.patch("messagebox.cloud_runtime.threading.Event", return_value=events.wake), \
                mock.patch("messagebox.cloud_runtime.random.random", return_value=0), \
                mock.patch("messagebox.cloud_runtime.CloudWorkEvents") as listener:
            runtime.run(stop=stop)
        self.assertEqual(polls, [0, 2, 4])
        listener.assert_not_called()
