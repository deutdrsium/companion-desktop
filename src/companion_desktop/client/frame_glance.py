"""Bounded, in-memory image processing for the local movie player."""

from base64 import b64encode
from threading import Lock

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


class FrameSample:
    def __init__(self, frame, mirrored):
        self.frame, self.mirrored = frame, mirrored
        self._lock = Lock()
        self._signature = None

    def visual(self):
        with self._lock:
            if self._signature is None:
                self._signature = signature(self.frame)
            return self._signature

    def jpeg(self):
        with self._lock:
            return encode_frame(self.frame, self.mirrored)


def thumbnail(image):
    if max(image.width(), image.height()) > 512:
        return image.scaled(512, 512, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return image


def signature(frame):
    image_format = frame.imageFormatFromPixelFormat(frame.pixelFormat())
    if image_format == QImage.Format_Invalid or not frame.map(QAbstractVideoBuffer.ReadOnly):
        return None
    try:
        pixels = frame.bits()
        pixels.setsize(frame.mappedBytes())
        with memoryview(pixels).cast("B") as mapped:
            pixel_bytes = QImage(1, 1, image_format).depth() // 8
            stride = frame.bytesPerLine()
            columns = [column * frame.width() // 32 * pixel_bytes for column in range(32)]
            data = bytearray()
            for row in range(18):
                start = row * frame.height() // 18 * stride
                for offset in columns:
                    data.extend(mapped[start + offset:start + offset + pixel_bytes])
        small = QImage(bytes(data), 32, 18, 32 * pixel_bytes, image_format)
        small = small.convertToFormat(QImage.Format_RGB888)
        return small.constBits().asstring(small.sizeInBytes())
    finally:
        frame.unmap()


def scene_cut(previous, current):
    return sum(abs(before - after) for before, after in zip(previous, current)) >= 42 * len(current)


def changed_scene(previous, current):
    before, after = previous.visual(), current.visual()
    return before is not None and after is not None and scene_cut(before, after)


def jpeg_base64(image):
    pixels = image.convertToFormat(QImage.Format_RGB32)
    clean = QImage(pixels.constBits(), pixels.width(), pixels.height(), pixels.bytesPerLine(), QImage.Format_RGB32)
    for quality in (75, 55, 35, 20):
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        if clean.save(buffer, "JPEG", quality) and buffer.size() <= 80_000:
            return b64encode(buffer.data()).decode("ascii")


def encode_frame(frame, mirrored):
    image_format = frame.imageFormatFromPixelFormat(frame.pixelFormat())
    if image_format != QImage.Format_Invalid and frame.map(QAbstractVideoBuffer.ReadOnly):
        try:
            view = QImage(frame.bits(), frame.width(), frame.height(), frame.bytesPerLine(), image_format)
            image = thumbnail(view)
            return jpeg_base64(image.mirrored(False, True) if mirrored else image)
        finally:
            frame.unmap()
    image = thumbnail(frame.image())
    if not image.isNull():
        return jpeg_base64(image.mirrored(False, True) if mirrored else image)
