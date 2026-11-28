"""Bounded, in-memory image processing for the local movie player."""

from base64 import b64encode

from PyQt5.QtCore import QBuffer, QIODevice, Qt
from PyQt5.QtGui import QImage
from PyQt5.QtMultimedia import QAbstractVideoBuffer, QAbstractVideoSurface


class FrameTap(QAbstractVideoSurface):
    def __init__(self, output, sample, parent):
        super().__init__(parent)
        self.output, self.sample = output, sample

    def supportedPixelFormats(self, handle=QAbstractVideoBuffer.NoHandle):
        return self.output.supportedPixelFormats(handle)

    def present(self, frame):
        self.sample(self, frame)
        return True


def thumbnail(image):
    if max(image.width(), image.height()) > 512:
        return image.scaled(512, 512, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return image


def signature(image):
    small = image.scaled(32, 18, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).convertToFormat(QImage.Format_RGB888)
    return small.constBits().asstring(small.sizeInBytes())


def scene_cut(previous, current):
    return sum(abs(before - after) for before, after in zip(previous, current)) >= 42 * len(current)


def jpeg_base64(image):
    pixels = image.convertToFormat(QImage.Format_RGB32)
    clean = QImage(pixels.constBits(), pixels.width(), pixels.height(), pixels.bytesPerLine(), QImage.Format_RGB32)
    for quality in (75, 55, 35, 20):
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        if clean.save(buffer, "JPEG", quality) and buffer.size() <= 80_000:
            return b64encode(buffer.data()).decode("ascii")
