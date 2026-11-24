"""Adapter for the current AstrBot desktop HTTP API (before the P1 desktop session).

Endpoints (relative to ``server.api_path``): POST /prepare, GET /history, POST /speech,
POST /playback, POST /screen; chat goes through AstrBot's ``/api/v1/chat`` SSE stream.
All generation and device access stay on the server.
"""

import base64
from collections import deque
import logging
import queue
import threading
from uuid import uuid4

import requests
from PyQt5.QtCore import QObject, pyqtSignal

from .protocol import read_sse

log = logging.getLogger(__name__)

SKIP_CHAINS = {"tool_call", "tool_call_result", "reasoning", "agent_stats"}


class ConnectionLost(RuntimeError):
    """The request never reached the server (tunnel down, refused, timed out connecting)."""


def request(base, key, path, *, method="POST", payload=None, timeout=15):
    try:
        response = requests.request(method, base.rstrip("/") + path, headers={"Authorization": "Bearer " + key},
                                    json=payload, timeout=timeout)
    except (requests.ConnectionError, requests.Timeout) as error:
        raise ConnectionLost(type(error).__name__) from error
    response.raise_for_status()
    result = response.json()
    if result.get("status") != "ok":
        raise RuntimeError(result.get("message") or "request failed")
    return result.get("data")


def probe(base, key, api_path):
    """Cheap liveness + authorisation check (idempotent prepare); raises on failure."""
    request(base, key, api_path.rstrip("/") + "/prepare", payload={}, timeout=8)


class LegacyBackend(QObject):
    history = pyqtSignal(object)
    history_failed = pyqtSignal()
    turn_accepted = pyqtSignal(str)
    chat_done = pyqtSignal(str, str)              # turn_id, full text
    chat_failed = pyqtSignal(str, str, str)       # turn_id, reason (offline|rejected|interrupted), partial
    audio = pyqtSignal(int, str, int, str, bytes, bool)  # generation, audio id, index, sentence, wav, mouth
    pcm = pyqtSignal(int, object)                 # generation, PcmClip with first audio
    pcm_lost = pyqtSignal(int, object)            # generation, incomplete PcmClip
    speech_error = pyqtSignal(int, int)           # generation, failed index
    screen_result = pyqtSignal(int, object)
    unreachable = pyqtSignal()                    # any request failed to connect -> probe the link

    def __init__(self, base, key, cfg):
        super().__init__()
        self.base = base.rstrip("/")
        self.key = key
        self.api = cfg["server"]["api_path"].rstrip("/")
        self.chat_path = cfg["server"].get("chat_path") or "/api/v1/chat"
        self.username = cfg["account"]["username"]
        self.session_id = cfg["account"]["session_id"]
        self.speech_jobs = queue.Queue()
        self.speech_stream = None
        self.sync_jobs = deque()
        self.sync_condition = threading.Condition()
        self.generation = 0
        self.screen_session = uuid4().hex
        self.cancelled = set()
        self.closed = False
        self._speech_thread = threading.Thread(target=self._speech_loop, daemon=True, name="speech")
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True, name="playback-sync")
        self._speech_thread.start()
        self._sync_thread.start()

    def set_base(self, base):
        self.base = base.rstrip("/")

    @property
    def headers(self):
        return {"Authorization": "Bearer " + self.key}

    def _request(self, path, *, method="POST", payload=None, timeout=15):
        return request(self.base, self.key, path, method=method, payload=payload, timeout=timeout)

    # ------------------------------------------------------------ session
    def load_history(self):
        def run():
            try:
                self._request(self.api + "/prepare", payload={})
                self.history.emit(self._request(self.api + "/history", method="GET") or [])
            except ConnectionLost:
                self.unreachable.emit()
                self.history_failed.emit()
            except Exception:
                log.exception("history failed")
                self.history_failed.emit()
        threading.Thread(target=run, daemon=True, name="history").start()

    # ------------------------------------------------------------ chat
    def chat(self, text, turn_id):
        def run():
            pieces, accepted = [], False
            try:
                payload = {"username": self.username, "session_id": self.session_id,
                           "message": text, "enable_streaming": False}
                try:
                    response = requests.post(self.base + self.chat_path, headers=self.headers, json=payload,
                                             stream=True, timeout=(10, 240))
                except (requests.ConnectionError, requests.Timeout) as error:
                    raise ConnectionLost(type(error).__name__) from error
                with response:
                    response.raise_for_status()
                    accepted = True
                    self.turn_accepted.emit(turn_id)
                    for item in read_sse(response.iter_lines()):
                        if turn_id in self.cancelled:
                            break
                        if item.get("type") == "plain" and item.get("chain_type") not in SKIP_CHAINS:
                            part = item.get("data")
                            if isinstance(part, str):
                                pieces.append(part)
                        elif item.get("type") == "error":
                            raise RuntimeError("chat error event")
                if turn_id in self.cancelled:
                    self.cancelled.discard(turn_id)
                    return
                self.chat_done.emit(turn_id, "".join(pieces))
            except ConnectionLost:
                self.unreachable.emit()
                self.chat_failed.emit(turn_id, "interrupted" if accepted else "offline", "".join(pieces))
            except Exception:
                log.warning("chat failed (accepted=%s)", accepted)
                if accepted:
                    self.unreachable.emit()
                self.chat_failed.emit(turn_id, "interrupted" if accepted else "rejected", "".join(pieces))
        threading.Thread(target=run, daemon=True, name="chat").start()

    def cancel(self, turn_id):
        """Today's server has no cancel: stop reading; the server may still finish the reply."""
        self.cancelled.add(turn_id)
        return "unknown"

    # ------------------------------------------------------------ speech
    def enable_pcm(self):
        from .speech_stream import SpeechStream

        self.speech_stream = SpeechStream(self)

    def disable_pcm(self):
        self.speech_stream.stop()
        self.speech_stream.deleteLater()
        self.speech_stream = None

    def enqueue_speech(self, segments, start=0):
        generation = self.generation
        jobs = ((generation, i, sentence) for i, sentence in enumerate(segments)
                if i >= start and sentence.strip())
        if self.speech_stream is not None:
            self.speech_stream.enqueue(jobs)
        else:
            for job in jobs:
                self.speech_jobs.put(job)
        return generation

    def stop_speech(self):
        self.generation += 1
        if self.speech_stream is not None:
            self.speech_stream.stop()

    def _speech_loop(self):
        while not self.closed:
            generation, index, sentence = self.speech_jobs.get()
            if generation != self.generation or self.closed:
                continue
            try:
                data = self._request(self.api + "/speech", payload={"text": sentence}, timeout=90)
                audio = base64.b64decode(data["audio"], validate=True)
                if not self.closed and generation == self.generation:
                    self.audio.emit(generation, data["id"], index, sentence, audio, bool(data.get("mouth_available")))
            except Exception as error:
                if self.closed or generation != self.generation:
                    continue
                if isinstance(error, ConnectionLost):
                    self.unreachable.emit()
                if generation == self.generation:
                    # Drop the rest of this reply: no more paid synthesis until a new request.
                    self.generation += 1
                    self.speech_error.emit(generation, index)

    def sync(self, action, audio_id, position_ms=0):
        with self.sync_condition:
            item = {"action": action, "id": audio_id, "position_ms": max(0, int(position_ms))}
            if action == "position" and self.sync_jobs and self.sync_jobs[-1]["action"] == "position":
                self.sync_jobs[-1] = item
            else:
                self.sync_jobs.append(item)
            self.sync_condition.notify()

    def _sync_loop(self):
        while not self.closed:
            with self.sync_condition:
                self.sync_condition.wait_for(lambda: self.sync_jobs or self.closed)
                if self.closed:
                    return
                item = self.sync_jobs.popleft()
            try:
                self._request(self.api + "/playback", payload=item, timeout=3)
            except Exception:
                pass

    # ------------------------------------------------------------ screen
    def screen(self, generation, action):
        def run():
            try:
                result = self._request(self.api + "/screen", payload={
                    "action": action, "generation": generation, "session": self.screen_session}, timeout=150)
                self.screen_result.emit(generation, result)
            except Exception as error:
                if isinstance(error, ConnectionLost):
                    self.unreachable.emit()
                self.screen_result.emit(generation, {"state": "error"})
        threading.Thread(target=run, daemon=True, name="screen").start()

    def close(self, generation):
        if self.speech_stream is not None:
            self.disable_pcm()
        try:
            self._request(self.api + "/screen", payload={"action": "end", "generation": generation,
                                                          "session": self.screen_session}, timeout=(0.5, 2))
        except Exception:
            pass
        self.closed = True
        self.speech_jobs.put((-1, 0, ""))
        with self.sync_condition:
            self.sync_condition.notify_all()
