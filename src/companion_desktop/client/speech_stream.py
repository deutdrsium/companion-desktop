"""Cancellable Qt HTTP synthesis for the local voice timeline."""

import base64
from collections import deque
import json
from uuid import uuid4

from PyQt5.QtCore import QObject, QUrl
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .audio import MAX_PCM_BYTES, PCM_CONTENT_TYPE, PcmClip


class SpeechStream(QObject):
    def __init__(self, backend):
        super().__init__(backend)
        self.backend = backend
        self.network = QNetworkAccessManager(self)
        self.jobs = deque()
        self.reply = None

    def enqueue(self, jobs):
        self.jobs.extend(jobs)
        self._next()

    def stop(self):
        self.jobs.clear()
        if self.reply is not None:
            self._close_reply()

    def _next(self):
        if self.reply is None and self.jobs:
            self.job = self.jobs.popleft()
            self._post(offer_pcm=True)

    def _post(self, *, offer_pcm):
        self.offer_pcm = offer_pcm
        self.clip = None
        self.buffer = bytearray()
        self.complete = False
        _, _, text = self.job
        backend = self.backend
        request = QNetworkRequest(QUrl(backend.base + backend.api + "/speech"))
        request.setRawHeader(b"Authorization", backend.headers["Authorization"].encode())
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        request.setTransferTimeout(90000)
        if offer_pcm:
            request.setRawHeader(b"Accept", PCM_CONTENT_TYPE.encode())
        reply = self.reply = self.network.post(request, json.dumps({"text": text}).encode())
        reply.readyRead.connect(lambda: self._read(reply))
        reply.finished.connect(lambda: self._finished(reply))

    def _close_reply(self):
        reply, self.reply = self.reply, None
        if not reply.isFinished():
            reply.abort()
        reply.deleteLater()
        self.clip = None
        self.buffer.clear()

    def _is_pcm(self, reply):
        return self.offer_pcm and reply.header(QNetworkRequest.ContentTypeHeader) == PCM_CONTENT_TYPE

    def _read(self, reply):
        if reply is not self.reply or not self._is_pcm(reply):
            return
        self.buffer.extend(reply.readAll().data())
        # Each PCM chunk starts with a uint32 byte count. Zero marks successful EOS.
        while len(self.buffer) >= 4 and not self.complete:
            size = int.from_bytes(self.buffer[:4], "big")
            received = len(self.clip.pcm) if self.clip is not None else 0
            if size % 2 or received + size > MAX_PCM_BYTES:
                self._failed(reply)
                return
            if len(self.buffer) < 4 + size:
                break
            if size == 0:
                self.complete = True
                del self.buffer[:4]
                break
            first = self.clip is None
            if first:
                generation, index, text = self.job
                self.clip = PcmClip(uuid4().hex, index, text)
            self.clip.pcm.extend(self.buffer[4:4 + size])
            del self.buffer[:4 + size]
            if first:
                self.backend.pcm.emit(generation, self.clip)
                if self.reply is not reply:
                    return  # playback can fail or be interrupted in the slot

    def _finished(self, reply):
        if reply is not self.reply:
            return
        generation, index, text = self.job
        if reply.error() != QNetworkReply.NoError:
            self._failed(reply)
            return
        if self._is_pcm(reply):
            self._read(reply)
            if reply is not self.reply:
                return
            if not self.complete or self.buffer or self.clip is None:
                self._failed(reply)
                return
            self.clip.finish()
            self._close_reply()
        else:
            try:
                result = json.loads(reply.readAll().data())
                if result["status"] != "ok":
                    raise ValueError("speech request failed")
                data = result["data"]
                audio = base64.b64decode(data["audio"], validate=True)
                audio_id, mouth = data["id"], bool(data.get("mouth_available"))
            except (ValueError, KeyError, TypeError, AttributeError):
                self._failed(reply)
                return
            self._close_reply()
            self.backend.audio.emit(generation, audio_id, index, text, audio, mouth)
        self._next()

    def _failed(self, reply):
        generation, index, _ = self.job
        clip, retry, error = self.clip, self._is_pcm(reply), reply.error()
        self._close_reply()
        if clip is not None:
            self.backend.pcm_lost.emit(generation, clip)
        if generation != self.backend.generation:
            return
        if retry:
            self._post(offer_pcm=False)
            return
        self.backend.stop_speech()
        if error in (QNetworkReply.ConnectionRefusedError, QNetworkReply.HostNotFoundError,
                     QNetworkReply.TimeoutError, QNetworkReply.RemoteHostClosedError):
            self.backend.unreachable.emit()
        self.backend.speech_error.emit(generation, index)
