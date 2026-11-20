"""Server link: optional SSH tunnel, health probe and automatic reconnect with backoff.

Replaces "restart the program" after a dropped tunnel: the panel goes offline, keeps the
draft, and the link is rebuilt after 1, 2, 4 ... 30 s.
"""

import logging
import socket
import subprocess
import threading
import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

log = logging.getLogger(__name__)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BACKOFF_S = (1, 2, 4, 8, 16, 30)


class TunnelError(RuntimeError):
    pass


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def open_tunnel(server, timeout=10.0, popen=subprocess.Popen):
    """Start ``ssh -N -L`` to the configured target; returns (process, local_port)."""
    port = free_port()
    argv = ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
            *server.get("ssh_options", []),
            "-L", f"127.0.0.1:{port}:127.0.0.1:{int(server.get('remote_port', 6185))}", server["ssh_target"]]
    process = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise TunnelError("refused")
        if _listening(port):
            return process, port
        time.sleep(0.1)
    process.terminate()
    raise TunnelError("timeout")


class Link(QObject):
    """Owns the base URL. Emits ``changed(state, attempt, next_retry_ms)`` and ``up(base_url)``."""

    changed = pyqtSignal(str, int, object)
    up = pyqtSignal(str)
    _result = pyqtSignal(bool, str, object)

    def __init__(self, server, probe, tunnel_factory=open_tunnel, parent=None):
        super().__init__(parent)
        self.server = server
        self.probe = probe          # callable(base_url) -> raises if the API is not usable
        self.tunnel_factory = tunnel_factory
        self.process = None
        self.base = None
        self.state = "connecting"
        self.attempt = 0
        self.busy = False
        self.closed = False
        self.retry = QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.timeout.connect(self.connect_now)
        self.watch = QTimer(self)
        self.watch.setInterval(2000)
        self.watch.timeout.connect(self._watch)
        self._result.connect(self._finished)

    # ------------------------------------------------------------ public
    def start_blocking(self):
        """First connection (from the launch worker thread). Raises TunnelError/other on failure."""
        base, process = self._open()
        self.probe(base)
        return base, process

    def adopt(self, base, process):
        """Take over the link established during launch."""
        self.base, self.process = base, process
        self.state, self.attempt = "connected", 0
        self.watch.start()

    def connect_now(self):
        if self.busy or self.closed:
            return
        self.retry.stop()
        self.busy = True
        if self.state != "connected":
            self.attempt += 1
            self.changed.emit("offline", self.attempt, None)

        def work():
            try:
                base, process = self._open()
                try:
                    self.probe(base)
                except Exception:
                    if process is not None:
                        process.terminate()
                    raise
                self._result.emit(True, base, process)
            except Exception as error:
                log.info("reconnect failed: %s", type(error).__name__)
                self._result.emit(False, "", None)
        threading.Thread(target=work, daemon=True, name="reconnect").start()

    def lost(self):
        """Something could not reach the server: verify, then reconnect if really down."""
        if self.closed or self.busy or self.state != "connected":
            return
        self.busy = True

        def work():
            try:
                self.probe(self.base)
                self._result.emit(True, self.base, self.process)
            except Exception:
                self._result.emit(False, "lost", None)
        threading.Thread(target=work, daemon=True, name="probe").start()

    def close(self):
        self.closed = True
        self.retry.stop()
        self.watch.stop()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()

    # ------------------------------------------------------------ internals
    def _open(self):
        base_url = self.server.get("base_url")
        if base_url:
            return base_url.rstrip("/"), None
        process, port = self.tunnel_factory(self.server)
        return f"http://127.0.0.1:{port}", process

    def _watch(self):
        if self.process is not None and self.process.poll() is not None and self.state == "connected":
            log.info("tunnel exited")
            self._down()

    def _down(self):
        self.state = "offline"
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
        self.process = None
        self._schedule()

    def _schedule(self):
        delay = BACKOFF_S[min(self.attempt, len(BACKOFF_S) - 1)]
        self.changed.emit("offline", self.attempt + 1, delay * 1000)
        self.retry.start(delay * 1000)

    def _finished(self, ok, base, process):
        self.busy = False
        if self.closed:
            if process is not None and process is not self.process:
                process.terminate()
            return
        if ok:
            if process is not self.process and self.process is not None and self.process.poll() is None:
                self.process.terminate()
            was = self.state
            self.base, self.process = base, process
            self.state, self.attempt = "connected", 0
            self.watch.start()
            self.changed.emit("connected", 0, None)
            if was != "connected":
                self.up.emit(base)
        elif base == "lost" or self.state == "connected":
            self.watch.stop()
            self._down()
        else:
            self._schedule()
