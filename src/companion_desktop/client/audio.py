"""Local playback queue. Subtitle, highlight and mouth all key off "this clip started playing"."""

from collections import deque
import os
import tempfile
import time

from PyQt5.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal

from .timeline import LEVEL_INTERVAL_MS, pcm_level


PCM_CONTENT_TYPE = "application/x-companion-pcm"
PCM_RATE = 32000
PCM_BYTES_PER_MS = PCM_RATE * 2 // 1000
MAX_PCM_BYTES = 8_000_000


class Clip:
    __slots__ = ("audio_id", "index", "text", "path", "mouth", "levels", "duration_ms")

    def __init__(self, audio_id, index, text, path, mouth, levels=()):
        self.audio_id, self.index, self.text, self.path, self.mouth = audio_id, index, text, path, mouth
        self.levels, self.duration_ms = levels, 0

    def level_at(self, position):
        index = position // LEVEL_INTERVAL_MS
        return self.levels[index] if 0 <= index < len(self.levels) else 0


class PcmClip(Clip):
    __slots__ = ("pcm", "complete")

    def __init__(self, audio_id, index, text):
        super().__init__(audio_id, index, text, None, False)
        self.pcm = bytearray()
        self.complete = False
        # There are no timestamps; use the text pace until the actual duration is known.
        self.duration_ms = max(900, 65 * len(text.strip()))

    def finish(self):
        self.complete = True
        self.duration_ms = max(1, len(self.pcm) // PCM_BYTES_PER_MS)

    def level_at(self, position):
        window = LEVEL_INTERVAL_MS * PCM_BYTES_PER_MS
        offset = position // LEVEL_INTERVAL_MS * window
        return pcm_level(self.pcm[offset:offset + window])


def _pcm_format():
    from PyQt5.QtMultimedia import QAudioFormat

    fmt = QAudioFormat()
    fmt.setCodec("audio/pcm")
    fmt.setSampleRate(PCM_RATE)
    fmt.setChannelCount(1)
    fmt.setSampleSize(16)
    fmt.setSampleType(QAudioFormat.SignedInt)
    fmt.setByteOrder(QAudioFormat.LittleEndian)
    return fmt


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
        self._volume = self.player.volume()
        self._pcm_output = None
        self._pcm_timer = QTimer(self)
        self._pcm_timer.setInterval(LEVEL_INTERVAL_MS)
        self._pcm_timer.setTimerType(Qt.PreciseTimer)
        self._pcm_timer.timeout.connect(self._pcm_tick)

    def set_volume(self, volume):
        self._volume = int(volume)
        self.player.setVolume(self._volume)
        if self._pcm_output is not None:
            self._pcm_output.setVolume(self._volume / 100)

    def supports_pcm(self):
        from PyQt5.QtMultimedia import QAudioDeviceInfo

        return QAudioDeviceInfo.defaultOutputDevice().isFormatSupported(_pcm_format())

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

    def enqueue_pcm(self, clip):
        self.queue.append(clip)
        self._next()

    def discard_pcm(self, clip):
        """Drop a partial clip before the backend retries this sentence as WAV."""
        if self.current is clip:
            self.paused.emit()
            self._release()
            self._after()
        elif clip in self.queue:
            self.queue.remove(clip)

    def _next(self):
        if self.current is not None:
            return
        if not self.queue:
            return
        self.current = self.queue.popleft()
        self.began = False
        clip = self.current
        if isinstance(clip, PcmClip):
            self._start_pcm()
            return
        self.player.setMedia(self._content(QUrl.fromLocalFile(clip.path)))
        if self.current is clip:
            self.player.play()

    def _start_pcm(self):
        from PyQt5.QtMultimedia import QAudioOutput

        self._pcm_output = QAudioOutput(_pcm_format(), self)
        self._pcm_output.setBufferSize(100 * PCM_BYTES_PER_MS)
        self._pcm_output.setVolume(self._volume / 100)
        self._pcm_offset = 0
        self._pcm_clock = None
        self._pcm_sink = self._pcm_output.start()
        self._pcm_timer.start()
        self._pcm_tick()

    def _pcm_tick(self):
        from PyQt5.QtMultimedia import QAudio

        clip, output = self.current, self._pcm_output
        state = output.state()
        if (state == QAudio.StoppedState or self._pcm_sink is None
                or output.error() not in (QAudio.NoError, QAudio.UnderrunError)):
            self.failed.emit(self._release())
            self._after()
            return
        offset = self._pcm_offset
        count = min(len(clip.pcm) - offset, output.bytesFree()) & ~1
        if count > 0:
            written = self._pcm_sink.write(bytes(clip.pcm[offset:offset + count]))
            if written < 0:
                self.failed.emit(self._release())
                self._after()
                return
            self._pcm_offset += written
            if written and not self.began:
                self.began = True
                self.started.emit(clip)
                if self.current is not clip:
                    return
        if output.state() == QAudio.IdleState:
            self._pcm_clock = None
            if clip.complete and self._pcm_offset == len(clip.pcm):
                self.finished.emit(self._release())
                self._after()
            elif self.began:
                self.paused.emit()
        elif self.began:
            # On Windows processedUSecs counts queued bytes, including audio not yet played.
            # Start a clock at each refill after Idle, where all previous bytes have played.
            now = time.monotonic()
            if state == QAudio.IdleState or self._pcm_clock is None:
                self._pcm_clock = now, offset // PCM_BYTES_PER_MS
            anchor, position = self._pcm_clock
            position = min(position + int((now - anchor) * 1000), self._pcm_offset // PCM_BYTES_PER_MS)
            self.progress.emit(clip, position, min(1.0, position / clip.duration_ms))

    def _state(self, state):
        if isinstance(self.current, PcmClip):
            return
        if state == self._qmp.PlayingState and self.current is not None and not self.began:
            self.began = True
            self.current.duration_ms = self.player.duration()
            self.started.emit(self.current)
        elif state == self._qmp.PausedState:
            self.paused.emit()

    def _position(self, position):
        if (self.current is not None and self.current.path and self.began
                and (not self.local_levels or self.player.state() == self._qmp.PlayingState)):
            d = self.current.duration_ms = self.player.duration()
            self.progress.emit(self.current, position, (position / d) if d > 0 else 0.0)

    def _release(self):
        clip, self.current = self.current, None
        if isinstance(clip, PcmClip):
            self._pcm_timer.stop()
            self._pcm_output.reset()
            self._pcm_output.deleteLater()
            self._pcm_output = self._pcm_sink = None
            return clip
        self.player.setMedia(self._content())
        if clip is not None:
            try:
                os.unlink(clip.path)
            except OSError:
                pass
        return clip

    def _status(self, status):
        if status == self._qmp.EndOfMedia and self.current is not None and self.current.path:
            clip = self._release()
            self.finished.emit(clip)
            self._after()

    def _error(self, error):
        if error != self._qmp.NoError and self.current is not None and self.current.path:
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
            if clip.path:
                try:
                    os.unlink(clip.path)
                except OSError:
                    pass
        self.queue.clear()
        clip = self._release()
        self.player.stop()
        return clip
