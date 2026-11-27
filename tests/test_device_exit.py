import asyncio
from contextlib import contextmanager
import faulthandler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

import companion_desktop
import pytest
from PyQt5.QtCore import QSettings, QTimer
from PyQt5.QtWidgets import QApplication
from urllib3.connection import HTTPConnection

from astrbot_plugin_desktop_access.session import Binding, SessionHost
from companion_desktop.client import config
from companion_desktop.client.connection import Link
from companion_desktop.client.controller import Controller
from companion_desktop.client.device_backend import DeviceBackend
from companion_desktop.client.launcher import Launch, SingleInstance, main
from companion_desktop.client.subtitle_server import SubtitleServer
from companion_desktop.contracts.wire import ProtocolError
from companion_desktop.session.client import DesktopSession, HttpTransport
from companion_desktop.ui.settings import Prefs


@pytest.mark.parametrize("phase", ["backoff", "in_flight", "screen_connect"])
def test_quit_during_registration_failure(tmp_path, phase):
    mutex = f"DesktopExitTest-{uuid4().hex}"
    completed = subprocess.run(
        [sys.executable, __file__, phase, str(tmp_path), mutex],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        capture_output=True, text=True, timeout=8,
    )
    finished = monotonic()
    assert completed.returncode == 0, completed.stderr
    quit_started, registrations = json.loads(completed.stdout)
    assert finished - quit_started < 2
    assert registrations == (3 if phase == "in_flight" else 2)
    instance = SingleInstance(mutex)
    try:
        assert instance.ok
    finally:
        instance.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.respond([])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.dispatch(self, body)

    def respond(self, data, *, reason=None):
        result = {"status": "ok", "data": data} if reason is None else {"status": "error", "reason": reason}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def loopback(dispatch):
    host = str(IPv4Address(socket.INADDR_LOOPBACK))
    with ThreadingHTTPServer((host, 0), Handler) as server:
        server.dispatch = dispatch
        Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        try:
            yield f"http://{host}:{server.server_port}"
        finally:
            server.shutdown()


@pytest.mark.parametrize("delayed_operation", ["register", "exchange"])
def test_late_session_request_cannot_replace_restarted_client(delayed_operation):
    host = SessionHost()
    binding = Binding("device-demo", "binding-demo", frozenset({"speak"}))
    stalled, release, handled = Event(), Event(), Event()
    host_lock = Lock()
    late_result = []

    def dispatch(handler, body):
        operation, payload = body["operation"], body["payload"]
        old = handler.headers["Authorization"] == "Bearer old-demo"
        delayed = old and operation == delayed_operation
        if delayed:
            stalled.set()
            assert release.wait(5)
        if old and operation == "disconnect":
            handler.send_error(500)
            return
        data = reason = None
        with host_lock:
            try:
                data = host.handle(binding, operation, payload)
            except ProtocolError as error:
                reason = str(error)
        if delayed:
            late_result.append(reason)
            handled.set()
        try:
            handler.respond(data, reason=reason)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            if not delayed:
                raise

    async def exercise(base):
        endpoint = base + "/session"
        old = DesktopSession(HttpTransport(endpoint, "old-demo"), permissions=("speak",))
        new = DesktopSession(HttpTransport(endpoint, "new-demo"), permissions=("speak",))
        old.start()
        try:
            assert await asyncio.to_thread(stalled.wait, 3)
            await asyncio.wait_for(old.close(), 1)
            assert old.connection["state"] == "closed"
            new.start()
            async with asyncio.timeout(3):
                while new.connection["state"] != "connected":
                    await asyncio.sleep(0.01)
            current = new.session
            release.set()
            assert await asyncio.to_thread(handled.wait, 3)
            assert late_result == ([None] if delayed_operation == "register" else ["stale_session"])
            with host_lock:
                assert host.current_session(binding.device_id) == current
            await new._exchange()
            assert new.session == current
        finally:
            release.set()
            await old.close()
            await new.close()

    with loopback(dispatch) as base:
        asyncio.run(exercise(base))


def run_client(phase, folder, mutex):
    registrations = []
    stalled = Event()
    screen_end = Event()
    release = Event()

    def dispatch(handler, body):
        if handler.path == "/session":
            registrations.append(body["operation"])
            if phase == "in_flight" and len(registrations) == 3:
                stalled.set()
                release.wait(15)
                return
            handler.send_error(500)
            return
        if handler.path == "/screen" and body["action"] == "end":
            assert not link.closed
            screen_end.set()
            if phase != "screen_connect":
                release.wait(15)
                return
        handler.respond({})

    connect = HTTPConnection._new_conn

    def delayed_connect(connection):
        if phase == "screen_connect" and current_thread().name == "screen-end":
            release.wait(0.75)
            assert not link.closed
        return connect(connection)

    quit_started = backend = link = None

    def begin(launch, cfg, character):
        nonlocal backend, link
        if sys.platform == "win32":
            contender = SingleInstance(mutex)
            try:
                assert not contender.ok
            finally:
                contender.release()
        app = QApplication.instance()
        app.setStyleSheet("")
        launch.subtitles = SubtitleServer(folder / "subtitle-token", port=0)
        prefs = Prefs("DesktopExitTest", "Device")
        prefs.store = QSettings(str(folder / "prefs.ini"), QSettings.IniFormat)
        link = launch.link = Link({"base_url": base}, lambda _: None)
        link.adopt(base, None)
        backend = DeviceBackend(base, "demo", cfg, folder)
        controller = Controller(character, cfg["title"], prefs, backend, link=link,
                                subtitles=launch.subtitles, device=True)
        launch.controller = controller

        def quit_when_ready():
            nonlocal quit_started
            connection = backend.session.connection
            ready = stalled.is_set() if phase == "in_flight" else (
                connection["state"] == "reconnecting" and connection["attempt"] == 2
            )
            if ready:
                timer.stop()
                quit_started = monotonic()
                if phase == "backoff":
                    app.quit()
                else:
                    controller.quit()

        timer = QTimer(launch)
        timer.timeout.connect(quit_when_ready)
        timer.start(5)
        controller.start()

    cfg = {**config.DEFAULTS, "server": {"api_path": ""}, "avatar": {},
           "account": {"username": "demo", "session_id": "demo"}, "title": "Desktop Exit Test",
           "data_dir": str(folder), "instance_mutex": mutex}
    with (loopback(dispatch) as base, patch.object(Launch, "begin", begin),
          patch.object(config, "load", return_value=cfg),
          patch.object(QApplication, "setAttribute"),
          patch.object(HTTPConnection, "_new_conn", delayed_connect)):
        try:
            assert main(["--no-helpers"]) == 0
            assert backend.closed and backend.loop.is_closed()
            assert backend.session.connection["state"] == "closed"
            assert screen_end.is_set() and link.closed
            reopened = SingleInstance(mutex)
            try:
                assert reopened.ok
            finally:
                reopened.release()
        finally:
            release.set()
        assert quit_started is not None
        assert set(registrations) == {"register"}
        print(json.dumps([quit_started, len(registrations)]), flush=True)


if __name__ == "__main__":
    faulthandler.dump_traceback_later(7, exit=True)
    run_client(sys.argv[1], Path(sys.argv[2]), sys.argv[3])
    faulthandler.cancel_dump_traceback_later()
