"""Opt-in microphone activity for interrupting local playback with a headset."""

from array import array

from PyQt5.QtCore import QObject, pyqtSignal


class BargeIn(QObject):
    speech_started = pyqtSignal()
    speech_ended = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt5.QtMultimedia import QAudioDeviceInfo, QAudioFormat, QAudioInput

        device = QAudioDeviceInfo.defaultInputDevice()
        fmt = QAudioFormat()
        fmt.setCodec("audio/pcm")
        fmt.setSampleRate(16000)
        fmt.setChannelCount(1)
        fmt.setSampleSize(16)
        fmt.setByteOrder(QAudioFormat.LittleEndian)
        fmt.setSampleType(QAudioFormat.SignedInt)
        if not device.isFormatSupported(fmt):
            fmt = device.nearestFormat(fmt)
        supported = (not device.isNull() and fmt.sampleSize() == 16 and fmt.sampleType() == QAudioFormat.SignedInt
                     and fmt.byteOrder() == QAudioFormat.LittleEndian and fmt.codec() == "audio/pcm")
        self.input = QAudioInput(device, fmt, self) if supported else None
        self.window_bytes = max(1, fmt.sampleRate() // 50) * max(1, fmt.channelCount()) * 2
        self.stream = None
        self.pending = bytearray()
        self.loud_windows = 0
        self.quiet_windows = 0
        self.monitoring = self.speaking = False

    def monitor(self, active):
        if active != self.monitoring:
            self.monitoring = active
            if active:
                self.start()
            else:
                self.stop()

    def start(self):
        if self.input is not None and self.stream is None:
            self.pending.clear()
            self.loud_windows = 0
            self.stream = self.input.start()
            if self.stream is not None:
                self.stream.readyRead.connect(self._read)

    def stop(self):
        if self.monitoring:
            return
        if self.stream is not None:
            self.stream, stream = None, self.stream
            stream.readyRead.disconnect(self._read)
            self.input.stop()
        self.pending.clear()
        self.speaking = False
        self.quiet_windows = 0

    def _read(self):
        self.pending.extend(bytes(self.stream.readAll()))
        while len(self.pending) >= self.window_bytes:
            samples = array("h", self.pending[:self.window_bytes])
            del self.pending[:self.window_bytes]
            loud = sum(value * value for value in samples) > len(samples) * 1200 ** 2
            self.loud_windows = self.loud_windows + 1 if loud else 0
            self.quiet_windows = 0 if loud else self.quiet_windows + 1
            if not self.speaking and self.loud_windows >= 5:
                if self.monitoring:
                    self.speaking = True
                else:
                    self.stop()
                self.speech_started.emit()
            elif self.speaking and self.quiet_windows >= 25:
                self.speaking = False
                self.speech_ended.emit()
