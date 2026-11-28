"""Local playback, timed text and opt-in Windows media discovery."""

import asyncio
from array import array
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from math import isfinite, sqrt
from pathlib import Path
from queue import SimpleQueue
import re
import sys
from threading import Thread
from time import monotonic
from uuid import uuid4

from PyQt5.QtCore import QObject, Qt, QUrl, pyqtSignal, pyqtSlot

from .frame_glance import FrameSample, FrameTap


_LOCATION = re.compile(
    r"(?:^|[\s(])(?:/|~[/\\]|\.{1,2}[/\\])|file:|\b[a-z]:[/\\]|\\\\|[\w.-]+[/\\]", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
_HOST = r"(?:(?:[\w-]+\.)+(?:xn--[\w-]+|[^\W\d_]{2,63})|\b(?:\d{1,3}\.){3}\d{1,3}\b)"
_LINK = re.compile(
    rf"(?:[a-z][a-z0-9+.-]*://|mailto:)[^\s<>\[\]()，。；！？]+"
    rf"|[\w.!#$%&'*+/=?^_`{{|}}~-]+@[\w.-]+"
    rf"|{_HOST}(?::\d+)?(?:[/#?][^\s<>\[\]()，。；！？]*)?", re.I)
_LINK_TRANSLATION = str.maketrans({"。": ".", "．": ".", "｡": ".", "＠": "@"})
_SRT_TIME = r"(\d{2}:\d{2}:\d{2}[,.]\d{3})"
_SRT_RANGE = re.compile(rf"(?m)^{_SRT_TIME}\s+-->\s+{_SRT_TIME}[^\n]*\n")
_LRC_TIME = re.compile(r"\[(\d+:\d{2}(?:\.\d{1,3})?)\]")
_CREDITS = re.compile(r"^\[?(?:end credits|closing credits|片尾字幕|演职员表)\]?$", re.I)


def wire_text(value, limit):
    if not isinstance(value, str):
        return None
    value = _CONTROL.sub(" ", unescape(value)).strip()
    links = _LINK.finditer(value.translate(_LINK_TRANSLATION))
    start = 0
    parts = []
    for link in links:
        parts.append(value[start:link.start()])
        start = link.end()
    parts.append(value[start:])
    value = " ".join(" ".join(parts).split())
    if start and not re.search(r"\w", value):
        return None
    if value and not _LOCATION.search(value):
        return value[:limit]
    return None


def _milliseconds(value):
    seconds = 0
    whole, _, fraction = value.replace(",", ".").partition(".")
    for part in whole.split(":"):
        seconds = 60 * seconds + int(part)
    return 1000 * seconds + int(fraction.ljust(3, "0"))


class TimedText:
    def __init__(self, cues):
        self.cues = tuple(sorted(cues))
        self.starts = tuple(cue[0] for cue in self.cues)
        ends = []
        for _, end, _ in self.cues:
            ends.append(max(end, ends[-1] if ends else 0))
        self.ends = tuple(ends)

    @classmethod
    def load(cls, path):
        path = Path(path).expanduser()
        with path.open("rb") as source:
            content = source.read(2_000_001)
        if len(content) > 2_000_000:
            raise ValueError("timed_text_too_large")
        encoding = "utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        content = content.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
        cues = []
        if path.suffix.lower() == ".srt":
            for block in re.split(r"\n[ \t]*\n", content.strip()):
                match = _SRT_RANGE.search(block)
                if match:
                    start, end = map(_milliseconds, match.groups())
                    if start < end:
                        cues.append((start, end, re.sub(r"<[^>]*>", "", block[match.end():]).strip()))
        elif path.suffix.lower() == ".lrc":
            offset = re.search(r"\[offset:([+-]?\d+)\]", content, re.I)
            offset = int(offset.group(1)) if offset else 0
            lines = sorted((max(0, _milliseconds(timestamp) - offset), _LRC_TIME.sub("", line).strip())
                           for line in content.splitlines() for timestamp in _LRC_TIME.findall(line))
            cues = [(start, lines[index + 1][0] if index + 1 < len(lines) else 2**53 - 1, text)
                    for index, (start, text) in enumerate(lines) if text]
        if not cues:
            raise ValueError("invalid_timed_text")
        return cls(cues)

    def context(self, position, *, require_next=False):
        """Return the current cue, usable gap in ms and whether credits are marked."""
        if position is None:
            return None, 0, False
        index = bisect_right(self.starts, position)
        active = index > 0 and position < self.ends[index - 1]
        cue = None
        if active:
            cue_index = index - 1
            while position >= self.cues[cue_index][1]:
                cue_index -= 1
            cue = self.cues[cue_index][2]
        gap_end = self.starts[index] if index < len(self.cues) else (position if require_next else 2**53 - 1)
        return cue, 0 if active else gap_end - position, bool(
            active and cue and _CREDITS.fullmatch(cue))


@dataclass(frozen=True)
class MediaItem:
    key: object
    title: str
    status: str
    position_ms: int | None
    duration_ms: int | None
    artist: str | None = None
    loudness: float | None = None
    ended: bool = False
    timeline_key: object = None
    sampled_at: float | None = field(default=None, compare=False)
    playback_rate: float = 1.0


class LocalPlayer(QObject):
    changed = pyqtSignal()
    frame_sampled = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt5.QtMultimedia import QAbstractVideoBuffer, QAudioProbe, QMediaContent, QMediaPlayer, QVideoFrame

        self.player = QMediaPlayer(self, QMediaPlayer.VideoSurface)
        self._content = QMediaContent
        self._video_frame = QVideoFrame
        self._cpu_video = QAbstractVideoBuffer.NoHandle
        self._probe = QAudioProbe(self)
        self._key = self._title = self._level = None
        self._level_at = 0.0
        self._frame_tap = None
        self._sample_enabled = False
        self._sample_at = float("-inf")
        self._probe.setSource(self.player)
        self._probe.audioBufferProbed.connect(self._audio)
        self._probe.flush.connect(self.reset_level)
        self.player.setNotifyInterval(100)
        self.player.stateChanged.connect(self._video_state)
        for signal in (self.player.positionChanged, self.player.durationChanged, self.player.stateChanged,
                       self.player.mediaStatusChanged, self.player.metaDataChanged):
            signal.connect(self.changed)

    def load(self, path):
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_file() or str(path).startswith("\\\\"):
            raise ValueError("local_file_required")
        self._key = None
        self.player.stop()
        self._title = wire_text(path.stem, 160) or "本地媒体"
        self.reset_level()
        self.player.setMedia(self._content(QUrl.fromLocalFile(str(path))))
        self._key = uuid4().hex
        self.changed.emit()

    def snapshot(self):
        player = self.player
        if self._key is None or player.mediaStatus() in (player.NoMedia, player.InvalidMedia):
            return None
        ended = player.mediaStatus() == player.EndOfMedia
        playing = player.state() == player.PlayingState and not ended
        duration = player.duration() or None
        position = min(player.position(), duration) if duration is not None else None
        return MediaItem(
            self._key, wire_text(player.metaData("Title"), 160) or self._title,
            "playing" if playing else "paused", duration if ended else position, duration,
            loudness=self._level if playing and monotonic() - self._level_at < 0.75 else None, ended=ended,
        )

    def reset_level(self):
        self._level = None

    def set_video_output(self, widget):
        previous, self._frame_tap = self._frame_tap, None
        self._sample_at = float("-inf")
        if widget is not None:
            output = widget.videoSurface()
            self._frame_tap = FrameTap(output, self._sample_frame, self)
            self.player.setVideoOutput([output, self._frame_tap])
        else:
            self.player.setVideoOutput(None)
        if previous is not None:
            previous.deleteLater()

    def set_frame_sampling(self, enabled):
        self._sample_enabled = enabled
        if not enabled:
            self._sample_at = float("-inf")

    def _video_state(self, state):
        if self._sample_enabled and state == self.player.StoppedState:
            self._sample_at = float("-inf")
            self.frame_sampled.emit(None)

    def _sample_frame(self, tap, frame):
        now = monotonic()
        if (not self._sample_enabled or tap is not self._frame_tap or not frame.isValid()
                or frame.handleType() != self._cpu_video
                or self.player.state() == self.player.StoppedState or now - self._sample_at < 1 / 3):
            return
        self._sample_at = now
        surface_format = tap.surfaceFormat()
        mirrored = surface_format.scanLineDirection() == surface_format.BottomToTop
        self.frame_sampled.emit(FrameSample(self._video_frame(frame), mirrored))

    def _audio(self, buffer):
        if self.player.state() != self.player.PlayingState or not buffer.isValid():
            return
        fmt = buffer.format()
        size = fmt.sampleSize()
        if fmt.sampleType() == fmt.Float and size in (32, 64):
            code, offset, scale = ("f" if size == 32 else "d"), 0, 1
        elif fmt.sampleType() in (fmt.SignedInt, fmt.UnSignedInt) and size in (8, 16, 32):
            scale = 2 ** (size - 1)
            offset = scale if fmt.sampleType() == fmt.UnSignedInt else 0
            code = {8: "b", 16: "h", 32: "i"}[size]
            if offset:
                code = code.upper()
        else:
            return
        samples = array(code, buffer.constData().asstring(buffer.byteCount()))
        if (fmt.byteOrder() == fmt.LittleEndian) != (sys.byteorder == "little"):
            samples.byteswap()
        if samples:
            rms = sqrt(sum(((sample - offset) / scale) ** 2 for sample in samples) / len(samples))
            if isfinite(rms):
                self._level = min(1.0, rms)
                self._level_at = monotonic()


class SystemMedia(QObject):
    _completed = pyqtSignal(object, object, object)

    def __init__(self):
        super().__init__()
        self.manager = None
        self.sessions = {}
        self._jobs = SimpleQueue()
        self._worker = None
        self._closed = False
        self._selection = None
        self._watched = None
        self._watch = []
        self._completed.connect(self._finish, Qt.QueuedConnection)

    @pyqtSlot(object, object, object)
    def _finish(self, future, result, error):
        if not future.done():
            if error is None:
                future.set_result(result)
            else:
                future.set_exception(error)

    def _run(self):
        uninitialize = None
        try:
            while (job := self._jobs.get()) is not None:
                future, operation, args = job
                result = error = None
                try:
                    if sys.platform == "win32" and uninitialize is None:
                        from winrt.runtime import ApartmentType, init_apartment, uninit_apartment

                        init_apartment(ApartmentType.MULTI_THREADED)
                        uninitialize = uninit_apartment
                    result = operation(*args)
                except Exception as exc:
                    error = exc
                if future is not None and not self._closed:
                    self._completed.emit(future, result, error)
        finally:
            self._unwatch()
            self.sessions.clear()
            self.manager = None
            if uninitialize is not None:
                uninitialize()

    async def _submit(self, operation, *args):
        if self._worker is None:
            self._worker = Thread(target=self._run, name="system-media", daemon=True)
            self._worker.start()
        future = asyncio.get_running_loop().create_future()
        self._jobs.put((future, operation, args))
        return await future

    async def discover(self):
        if sys.platform != "win32":
            return ()
        return await self._submit(lambda: asyncio.run(self._discover()))

    async def _discover(self):
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager

        if self.manager is None:
            self.manager = await asyncio.wait_for(GlobalSystemMediaTransportControlsSessionManager.request_async(), 2)
        sessions = {}
        for session in self.manager.get_sessions():
            token = next((key for key, previous in self.sessions.items() if previous == session), None)
            sessions[token or uuid4().hex] = session
        self.sessions = sessions
        return tuple({"id": token, "label": session.source_app_user_model_id} for token, session in sessions.items())

    def watch(self, token, notify):
        self._selection = token, notify

    def _watch_session(self, selection, session):
        if self._watched == selection:
            return
        self._unwatch()
        if selection is None:
            return
        token, notify = selection
        for add, remove, kind in ((session.add_media_properties_changed, session.remove_media_properties_changed,
                                   "media"),
                                  (session.add_playback_info_changed, session.remove_playback_info_changed,
                                   "playback"),
                                  (session.add_timeline_properties_changed, session.remove_timeline_properties_changed,
                                   "timeline")):
            event = add(lambda _, __, kind=kind: notify(token, kind))
            self._watch.append((remove, event))
        self._watched = selection

    def unwatch(self):
        self._selection = None
        if self._worker is not None:
            self._jobs.put((None, self._unwatch, ()))

    def _unwatch(self):
        for remove, event in self._watch:
            try:
                remove(event)
            except (OSError, RuntimeError):
                pass
        self._watch.clear()
        self._watched = None

    def close(self):
        self._closed = True
        if self._worker is not None:
            self._jobs.put(None)

    async def snapshot(self, token):
        selection = self._selection
        return await self._submit(lambda: asyncio.run(self._snapshot(token, selection)))

    async def _snapshot(self, token, selection):
        session = self.sessions.get(token)
        if session is None or session not in self.manager.get_sessions():
            return None
        self._watch_session(selection if selection and selection[0] == token else None, session)
        props = await asyncio.wait_for(session.try_get_media_properties_async(), 1)
        return self.readback(token, props)

    def readback(self, token, props):
        session = self.sessions.get(token)
        if session is None or props is None:
            return None
        info = session.get_playback_info()
        status = info.playback_status.name if info is not None else None
        raw_title = props.title.strip()
        if status not in {"PLAYING", "PAUSED", "STOPPED"} or not raw_title:
            return None
        title = wire_text(raw_title, 160) or "媒体"
        position = duration = stamp = None
        timeline = session.get_timeline_properties()
        sampled_at = monotonic()
        rate = getattr(info, "playback_rate", None) or 1
        if not isfinite(rate):
            rate = 1
        if timeline is not None:
            start, end = timeline.start_time, timeline.end_time
            duration = int((end - start).total_seconds() * 1000) if end > start else None
            if duration is not None or timeline.position > start:
                stamp = timeline.last_updated_time
                position = max(0, int((timeline.position - start).total_seconds() * 1000))
                if status == "PLAYING" and stamp.year > 1970:
                    stamp = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - stamp).total_seconds()
                    position = max(0, position + int(1000 * max(0, age) * rate))
                if duration is not None:
                    position = min(position, duration)
        raw_artist = props.artist
        artist = wire_text(raw_artist, 120)
        ended = (status == "STOPPED" and position is not None and duration is not None
                 and duration - position <= 750)
        return MediaItem((token, raw_title, raw_artist, props.track_number), title,
                         "playing" if status == "PLAYING" else "paused", position, duration,
                         artist, ended=ended, timeline_key=stamp, sampled_at=sampled_at, playback_rate=rate)
