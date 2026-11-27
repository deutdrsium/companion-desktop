from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event, Thread
from time import monotonic

import companion_desktop
import pytest
from PyQt5.QtCore import QSettings, QTimer
from PyQt5.QtWidgets import QApplication

from companion_desktop.client.controller import Controller
from companion_desktop.client.device_backend import DeviceBackend
from companion_desktop.client.launcher import Launch
from companion_desktop.client.subtitle_server import SubtitleServer
from companion_desktop.ui.paint import Character
from companion_desktop.ui.settings import Prefs


@pytest.mark.parametrize("phase", ["backoff", "in_flight"])
def test_quit_during_registration_failure(tmp_path, phase):
    completed = subprocess.run(
        [sys.executable, __file__, phase, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        capture_output=True, text=True, timeout=8,
    )
    finished = monotonic()
    assert completed.returncode == 0, completed.stderr
    quit_started, registrations = json.loads(completed.stdout)
    assert finished - quit_started < 2
    assert registrations == (2 if phase == "backoff" else 3)


def run_client(phase, folder):
    registrations = []
    stalled = Event()
    screen_end = Event()
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.respond([])

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/session":
                registrations.append(body["operation"])
                if phase == "in_flight" and len(registrations) == 3:
                    stalled.set()
                    release.wait(15)
                    return
                self.send_error(500)
                return
            if self.path == "/screen" and body["action"] == "end":
                screen_end.set()
                release.wait(15)
                return
            self.respond({})

        def respond(self, data):
            body = json.dumps({"status": "ok", "data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    host = str(IPv4Address(socket.INADDR_LOOPBACK))
    with ThreadingHTTPServer((host, 0), Handler) as server:
        Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        app = QApplication([])
        app.setQuitOnLastWindowClosed(False)
        launch = Launch(None)
        launch.subtitles = SubtitleServer(folder / "subtitle-token", port=0)
        prefs = Prefs("DesktopExitTest", "Device")
        prefs.store = QSettings(str(folder / "prefs.ini"), QSettings.IniFormat)
        cfg = {"server": {"api_path": ""}, "account": {"username": "demo", "session_id": "demo"},
               "avatar": {}}
        backend = DeviceBackend(f"http://{host}:{server.server_port}", "demo", cfg, folder)
        controller = Controller(Character("Demo"), "Desktop Exit Test", prefs, backend,
                                subtitles=launch.subtitles, device=True)
        launch.controller = controller
        quit_started = None

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

        timer = QTimer()
        timer.timeout.connect(quit_when_ready)
        timer.start(5)
        controller.start()
        try:
            assert app.exec_() == 0
        finally:
            launch.shutdown()
            server.shutdown()
        assert backend.closed and backend.loop.is_closed()
        assert backend.session.connection["state"] == "closed"
        assert screen_end.is_set()
        assert quit_started is not None
        assert set(registrations) == {"register"}
        print(json.dumps([quit_started, len(registrations)]), flush=True)


if __name__ == "__main__":
    run_client(sys.argv[1], Path(sys.argv[2]))
