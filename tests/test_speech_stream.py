"""Local HTTP synthesis and a fake Qt audio sink; no audio devices are opened."""

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import time
import wave

from PyQt5 import QtMultimedia
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtMultimedia import QAudio
from PyQt5.QtTest import QTest
import pytest

from companion_desktop.client.audio import PCM_CONTENT_TYPE, PCM_RATE, Player
from companion_desktop.client.legacy_backend import LegacyBackend

PCM = b"\x00\x20" * (PCM_RATE // 50)
FRAME = len(PCM).to_bytes(4, "big") + PCM


def wav_payload():
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setparams((1, 2, PCM_RATE, 0, "NONE", "not compressed"))
        wav.writeframes(PCM * 2)
    return {"status": "ok", "data": {"id": "wav-demo", "audio": base64.b64encode(out.getvalue()).decode()}}


@contextmanager
def service(synthesize):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            synthesize(self, payload["text"])

        def wave(self):
            data = json.dumps(wav_payload()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def pcm(self):
            self.send_response(200)
            self.send_header("Content-Type", PCM_CONTENT_TYPE)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

        def chunk(self, data):
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def wait_for(condition):
    deadline = time.monotonic() + 4
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(5)
    assert condition()


class FakeMedia(QObject):
    PlayingState, PausedState, EndOfMedia, NoError = 1, 2, 3, 0
    stateChanged = pyqtSignal(int)
    positionChanged = pyqtSignal(int)
    mediaStatusChanged = pyqtSignal(int)
    error = pyqtSignal(int)

    def setNotifyInterval(self, _):
        pass

    def setMedia(self, _):
        pass

    def setVolume(self, _):
        pass

    def play(self):
        self.stateChanged.emit(self.PlayingState)

    def stop(self):
        pass

    def volume(self):
        return 100

    def duration(self):
        return 40


class FakePcmOutput(QObject):
    def __init__(self, fmt, parent):
        super().__init__(parent)
        assert (fmt.sampleRate(), fmt.sampleSize(), fmt.channelCount()) == (PCM_RATE, 16, 1)
        assert fmt.sampleType() == fmt.SignedInt and fmt.byteOrder() == fmt.LittleEndian
        self.audio_state = QAudio.IdleState
        self.written = bytearray()

    def setVolume(self, _):
        pass

    def setBufferSize(self, size):
        self.buffer_size = size

    def start(self):
        return self

    def bytesFree(self):
        return self.buffer_size

    def error(self):
        return QAudio.NoError

    def state(self):
        return self.audio_state

    def write(self, data):
        self.written.extend(data)
        self.audio_state = QAudio.ActiveState
        return len(data)

    def reset(self):
        self.audio_state = QAudio.StoppedState


@pytest.fixture
def desk(qapp, monkeypatch):
    monkeypatch.setattr(QtMultimedia, "QMediaPlayer", FakeMedia)
    monkeypatch.setattr(QtMultimedia, "QAudioOutput", FakePcmOutput)

    @contextmanager
    def connected(base, *, streaming=True):
        backend = LegacyBackend(base, "synthetic", {
            "server": {"api_path": "/desktop"}, "account": {"username": "demo", "session_id": "demo"},
        })
        if streaming:
            backend.enable_pcm()
        player = Player(local_levels=True)
        backend.pcm.connect(lambda generation, clip: player.enqueue_pcm(clip))
        backend.pcm_lost.connect(lambda generation, clip: player.discard_pcm(clip))
        backend.audio.connect(lambda generation, *args: player.enqueue(*args))
        try:
            yield backend, player
        finally:
            player.stop()
            backend._request = lambda *args, **kwargs: None
            backend.close(0)

    return connected


def test_mock_time_to_first_audio_and_pcm_clock(desk):
    def synthesize(req, _):
        time.sleep(0.15)
        if req.headers.get("Accept") == PCM_CONTENT_TYPE:
            req.pcm()
            req.chunk(FRAME[:3])
            req.chunk(FRAME[3:])
            time.sleep(0.55)
            req.chunk(FRAME + b"\0" * 4)
            req.chunk(b"")
        else:
            time.sleep(0.55)
            req.wave()

    with service(synthesize) as base:
        times = []
        for streaming in (False, True):
            with desk(base, streaming=streaming) as (backend, player):
                started, progress, pauses, finished = [], [], [], []
                player.started.connect(lambda clip: started.append((time.perf_counter(), clip)))
                player.progress.connect(lambda clip, ms, fraction: progress.append((ms, clip.level_at(ms))))
                player.paused.connect(lambda: pauses.append(True))
                player.finished.connect(finished.append)
                request_at = time.perf_counter()
                backend.enqueue_speech(["Demo sentence."])
                wait_for(lambda: started)
                times.append(started[0][0] - request_at)
                if streaming:
                    clip = started[0][1]
                    output = player._pcm_output
                    assert not clip.complete and output.written == PCM
                    assert progress[0][0] == 0 and progress[0][1] > 0.9
                    output.audio_state = QAudio.IdleState
                    wait_for(lambda: pauses)  # no mouth movement while waiting for more audio
                    wait_for(lambda: clip.complete and output.written == PCM * 2)
                    assert clip.duration_ms == 40
                    wait_for(lambda: progress[-1][0] == 20)
                    output.audio_state = QAudio.IdleState
                    wait_for(lambda: finished)
                    assert finished == [clip]
    print(f"Mock first audio: WAV {times[0] * 1000:.0f} ms; PCM {times[1] * 1000:.0f} ms")
    assert times[1] < times[0]


@pytest.mark.parametrize("partial", [False, True])
def test_incomplete_stream_retries_same_sentence_as_wav(desk, partial):
    attempts = []

    def synthesize(req, text):
        pcm = req.headers.get("Accept") == PCM_CONTENT_TYPE
        attempts.append((text, pcm))
        if pcm and text == "First.":
            req.pcm()
            if partial:
                req.chunk(FRAME)
            req.chunk(b"")  # HTTP completed, but no successful PCM terminator
        else:
            req.wave()  # WAV response also models an older or disabled host

    with service(synthesize) as base, desk(base) as (backend, player):
        started, pauses = [], []
        player.started.connect(started.append)
        player.paused.connect(lambda: pauses.append(True))
        backend.enqueue_speech(["First.", "Second."])
        wait_for(lambda: player.queue)
        assert attempts == [("First.", True), ("First.", False), ("Second.", True)]
        assert [clip.path is None for clip in started] == ([True, False] if partial else [False])
        assert bool(pauses) == partial
        assert player.current.index == 0 and player.queue[0].index == 1


def test_stop_aborts_network_and_pcm_without_fallback(desk):
    disconnected = threading.Event()
    attempts = []

    def synthesize(req, text):
        attempts.append(text)
        req.pcm()
        req.chunk(FRAME)
        req.connection.settimeout(2)
        if not req.rfile.read(1):
            disconnected.set()
        req.close_connection = True

    with service(synthesize) as base, desk(base) as (backend, player):
        backend.enqueue_speech(["First.", "Second."])
        wait_for(lambda: player.began)
        output = player._pcm_output
        backend.stop_speech()
        player.stop()
        assert backend.speech_stream.reply is None and not player.active
        assert output.state() == QAudio.StoppedState
        wait_for(disconnected.is_set)
        assert attempts == ["First."]
