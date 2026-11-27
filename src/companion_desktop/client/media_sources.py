"""Local playback, timed text and opt-in Windows media discovery."""

import asyncio
from array import array
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite, sqrt
from pathlib import Path
import re
import sys
from time import monotonic
from uuid import uuid4

from PyQt5.QtCore import QObject, QUrl, pyqtSignal


_LOCATION = re.compile(
    r"(?:^|[\s(])(?:/|~[/\\]|\.{1,2}[/\\])|[a-z][a-z0-9+.-]*://|file:|\b[a-z]:[/\\]|\\\\|www\."
    r"|[\w.-]+[/\\][\w.-]+\.[a-z0-9]{2,8}\b", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
_SRT_TIME = r"(\d{2}:\d{2}:\d{2}[,.]\d{3})"
_SRT_RANGE = re.compile(rf"(?m)^{_SRT_TIME}\s+-->\s+{_SRT_TIME}[^\n]*\n")
_LRC_TIME = re.compile(r"\[(\d+:\d{2}(?:\.\d{1,3})?)\]")
_CREDITS = re.compile(r"^\[?(?:end credits|closing credits|片尾字幕|演职员表)\]?$", re.I)


def wire_text(value, limit):
    if not isinstance(value, str):
        return None
    value = _CONTROL.sub(" ", value).strip()
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

    def context(self, position):
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
        gap_end = self.starts[index] if index < len(self.cues) else 2**53 - 1
        return wire_text(cue, 300), 0 if active else gap_end - position, bool(
            active and cue and _CREDITS.fullmatch(cue))


@dataclass(frozen=True)
class MediaItem:
    key: object
    title: str
    status: str
    position_ms: int | None
    duration_ms: int | None
    artist: str | None = None
    album: str | None = None
    loudness: float | None = None
    ended: bool = False
    timeline_key: object = None


class LocalPlayer(QObject):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt5.QtMultimedia import QAudioProbe, QMediaContent, QMediaPlayer

        self.player = QMediaPlayer(self, QMediaPlayer.VideoSurface)
        self._content = QMediaContent
        self._probe = QAudioProbe(self)
        self._key = self._title = self._level = None
        self._level_at = 0.0
        self._probe.setSource(self.player)
        self._probe.audioBufferProbed.connect(self._audio)
        self._probe.flush.connect(self.reset_level)
        self.player.setNotifyInterval(100)
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
            wire_text(player.metaData("ContributingArtist"), 120) or wire_text(player.metaData("Author"), 120),
            wire_text(player.metaData("AlbumTitle"), 120),
            self._level if playing and monotonic() - self._level_at < 0.75 else None, ended,
        )

    def reset_level(self):
        self._level = None

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


class SystemMedia:
    def __init__(self):
        self.manager = None
        self.sessions = {}
        self._cached = None
        self._watch = []

    async def discover(self):
        if sys.platform != "win32":
            return ()
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager

        if self.manager is None:
            self.manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        sessions = {}
        for session in self.manager.get_sessions():
            token = next((key for key, previous in self.sessions.items() if previous == session), None)
            sessions[token or uuid4().hex] = session
        self.sessions = sessions
        return tuple({"id": token, "label": session.source_app_user_model_id} for token, session in sessions.items())

    def watch(self, token, notify):
        self.unwatch()
        session = self.sessions[token]
        for add, remove, kind in ((session.add_media_properties_changed, session.remove_media_properties_changed,
                                   "media"),
                                  (session.add_playback_info_changed, session.remove_playback_info_changed,
                                   "playback"),
                                  (session.add_timeline_properties_changed, session.remove_timeline_properties_changed,
                                   "timeline")):
            event = add(lambda _, __, kind=kind: notify(token, kind))
            self._watch.append((remove, event))

    def unwatch(self):
        for remove, event in self._watch:
            try:
                remove(event)
            except OSError:
                pass
        self._watch.clear()
        self._cached = None

    async def snapshot(self, token):
        session = self.sessions.get(token)
        if session is None or session not in self.manager.get_sessions():
            self._cached = None
            return None
        props = await asyncio.wait_for(session.try_get_media_properties_async(), 1)
        self._cached = token, props
        return self.readback(token, props)

    def cached_snapshot(self, token):
        if self._cached is None or self._cached[0] != token:
            return None
        return self.readback(token, self._cached[1])

    def readback(self, token, props):
        session = self.sessions.get(token)
        if session is None or props is None:
            return None
        info = session.get_playback_info()
        status = info.playback_status.name if info is not None else None
        title = wire_text(props.title, 160)
        if status not in {"PLAYING", "PAUSED", "STOPPED"} or not title:
            return None
        position = duration = stamp = None
        timeline = session.get_timeline_properties()
        if timeline is not None:
            start, end = timeline.start_time, timeline.end_time
            duration = int((end - start).total_seconds() * 1000) if end > start else None
            if duration is not None or timeline.position > start:
                stamp = timeline.last_updated_time
                position = max(0, int((timeline.position - start).total_seconds() * 1000))
                if status == "PLAYING" and stamp.year > 1970:
                    stamp = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - stamp).total_seconds()
                    rate = info.playback_rate or 1
                    if isfinite(rate):
                        position = max(0, position + int(1000 * max(0, age) * rate))
                if duration is not None:
                    position = min(position, duration)
        artist, album = wire_text(props.artist, 120), wire_text(props.album_title, 120)
        return MediaItem((token, title, artist, album, props.track_number), title,
                         "playing" if status == "PLAYING" else "paused", position, duration,
                         artist, album, ended=status == "STOPPED", timeline_key=stamp)
