"""Local playback queue. Subtitle, highlight and mouth all key off "this clip started playing"."""

from collections import deque
import os
import tempfile

from PyQt5.QtCore import QObject, QUrl, pyqtSignal


class Clip:
    __slots__ = ("audio_id", "index", "text", "path", "mouth", "levels", "duration_ms")

    def __init__(self, audio_id, index, text, path, mouth, levels=()):
        self.audio_id, self.index, self.text, self.path, self.mouth = audio_id, index, text, path, mouth
        self.levels, self.duration_ms = levels, 0


class Player(QObject):
    started = pyqtSignal(object)          # Clip
    progress = pyqtSignal(object, int, float)   # Clip, position ms, fraction
    finished = pyqtSignal(object)         # Clip
    failed = pyqtSignal(object)           # Clip
    drained = pyqtSignal()                # queue empty after the last clip
    paused = pyqtSignal()

    def __init__(self, parent=None, *, local_levels=False):
        super().__init__(parent)
        from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
        self._content, self._qmp = QMediaContent, QMediaPlayer
        self.player = QMediaPlayer(self)
        self.local_levels = local_levels
        self.player.setNotifyInterval(30 if local_levels else 100)
        self.player.stateChanged.connect(self._state)
        self.player.positionChanged.connect(self._position)
        self.player.mediaStatusChanged.connect(self._status)
        self.player.error.connect(self._error)
        self.queue = deque()
        self.current = None
        self.began = False

    def set_volume(self, volume):
        self.player.setVolume(int(volume))

    @property
    def active(self):
        return self.current is not None or bool(self.queue)

    def enqueue(self, audio_id, index, text, wav, mouth):
        with tempfile.NamedTemporaryFile(prefix="companion-speech-", suffix=".wav", delete=False) as f:
            f.write(wav)
            path = f.name
        levels = ()
        if self.local_levels:
            from .timeline import wav_levels
            levels = wav_levels(wav)
        self.queue.append(Clip(audio_id, index, text, path, mouth, levels))
        self._next()

    def _next(self):
        if self.current is not None:
            return
        if not self.queue:
            return
        self.current = self.queue.popleft()
        self.began = False
        clip = self.current
        self.player.setMedia(self._content(QUrl.fromLocalFile(clip.path)))
        if self.current is clip:
            self.player.play()

    def _state(self, state):
        if state == self._qmp.PlayingState and self.current is not None and not self.began:
            self.began = True
            self.current.duration_ms = self.player.duration()
            self.started.emit(self.current)
        elif state == self._qmp.PausedState:
            self.paused.emit()

    def _position(self, position):
        if (self.current is not None and self.began
                and (not self.local_levels or self.player.state() == self._qmp.PlayingState)):
            d = self.current.duration_ms = self.player.duration()
            self.progress.emit(self.current, position, (position / d) if d > 0 else 0.0)

    def _release(self):
        clip, self.current = self.current, None
        self.player.setMedia(self._content())
        if clip is not None:
            try:
                os.unlink(clip.path)
            except OSError:
                pass
        return clip

    def _status(self, status):
        if status == self._qmp.EndOfMedia and self.current is not None:
            clip = self._release()
            self.finished.emit(clip)
            self._after()

    def _error(self, error):
        if error != self._qmp.NoError and self.current is not None:
            clip = self._release()
            self.failed.emit(clip)
            self._after()

    def _after(self):
        if self.queue:
            self._next()
        else:
            self.drained.emit()

    def stop(self):
        """Stop now and drop everything queued. Returns the clip that was playing (or None)."""
        for clip in self.queue:
            try:
                os.unlink(clip.path)
            except OSError:
                pass
        self.queue.clear()
        clip = self._release()
        self.player.stop()
        return clip
