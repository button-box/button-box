"""Exercise the deployed server command over loopback, without device data."""

import contextlib
import http.client
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor


ROOT = Path(__file__).resolve().parents[1]
SERVICES = (
    "systemd/onboarding/messagebox-onboarding-home.service",
    "systemd/onboarding/comitup-web.service.d/messagebox.conf",
)


def fixture_app(environ, start_response):
    if environ["PATH_INFO"] == "/slow":
        directory = Path(os.environ["MSGBOX_HTTP_TEST_DIR"])
        (directory / "started").touch()
        deadline = time.monotonic() + 5
        while not (directory / "release").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    body = b'{"ok":true}'
    start_response("200 OK", [("Content-Length", str(len(body)))])
    return [body]


def request(port, path="/", timeout=1):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


@contextlib.contextmanager
def server(service):
    command = next(
        shlex.split(line.removeprefix("ExecStart="))
        for line in (ROOT / service).read_text().splitlines()
        if line.startswith("ExecStart=/usr/bin/gunicorn ")
    )
    with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        port = listener.getsockname()[1]
        command[command.index("--bind") + 1] = f"fd://{listener.fileno()}"
        command[-1] = "test_onboarding_http:fixture_app"
        environment = dict(os.environ)
        environment.pop("GUNICORN_CMD_ARGS", None)
        environment["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(ROOT / "tests")))
        environment["MSGBOX_HTTP_TEST_DIR"] = directory
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "gunicorn", *command[1:]],
                cwd=directory, env=environment, pass_fds=(listener.fileno(),),
                stdout=log, stderr=log,
            )
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        if request(port, timeout=0.1) == 200:
                            break
                    except (OSError, http.client.HTTPException):
                        pass
                    if process.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise AssertionError(log.read().decode())
                    time.sleep(0.02)
                yield port, Path(directory)
            finally:
                (Path(directory) / "release").touch()
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


class OnboardingHTTPTests(unittest.TestCase):
    def test_idle_browser_connections_do_not_block_pages_or_state(self):
        for service in SERVICES:
            with self.subTest(service=service), server(service) as (port, _), contextlib.ExitStack() as sockets:
                # More speculative connections than application workers; none send bytes.
                for _ in range(6):
                    sockets.enter_context(socket.create_connection(("127.0.0.1", port)))
                time.sleep(0.1)
                for path in ("/", "/static/app.js", "/api/state"):
                    self.assertEqual(request(port, path), 200)

    def test_slow_operation_and_idle_connections_leave_dashboard_available(self):
        for service in SERVICES:
            with self.subTest(service=service), server(service) as (port, directory), contextlib.ExitStack() as sockets:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    slow = pool.submit(request, port, "/slow", 6)
                    try:
                        deadline = time.monotonic() + 2
                        while not (directory / "started").exists():
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(0.01)
                        for _ in range(6):
                            sockets.enter_context(socket.create_connection(("127.0.0.1", port)))
                        self.assertEqual(request(port, "/api/state"), 200)
                        self.assertEqual(request(port, "/static/app.js"), 200)
                        self.assertFalse(slow.done())
                    finally:
                        (directory / "release").touch()
                    self.assertEqual(slow.result(timeout=2), 200)
