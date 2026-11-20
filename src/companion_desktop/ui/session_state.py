"""The panel's single state source.

Avatar ring, status line, tray icon, send/stop key and the subtitle feed all subscribe to
``SessionState.changed``; none of them infer state on their own.

U1 drives it from signals the client already has (send time, the local media player,
stop). U2 swaps the inputs for desktop-session / cerebellum events through
``apply_event`` without touching the widgets.
"""

import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from .theme import MOTION, STATE

PRESENCE = ("idle", "listening", "thinking", "speaking", "interrupted")
CONNECTION = ("connecting", "connected", "offline")


class SessionState(QObject):
    changed = pyqtSignal(object)  # emits self
    progressed = pyqtSignal(float)  # current segment playback fraction

    # Exit safeguard: thinking must always end (B05). If text arrived but no audio
    # started within this window, fall back to idle.
    SPEECH_START_GRACE_MS = 45_000

    def __init__(self, parent=None, clock=time.monotonic):
        super().__init__(parent)
        self.clock = clock
        self.connection = "connecting"
        self.presence = "idle"
        self.typing = False
        self.turn_id = None
        self.thinking_since = None
        self.segment_index = 0
        self.segment_total = 0
        self.segment_text = ""
        self.progress = 0.0
        self.attempt = 0
        self.next_retry_at = None
        self.reason = ""
        self.capturing = False
        self.periodic = False
        self.autonomous = False
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._second)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._interrupt_settled)
        self._grace = QTimer(self)
        self._grace.setSingleShot(True)
        self._grace.timeout.connect(self._speech_never_started)

    # ------------------------------------------------------------ derived
    @property
    def state(self):
        if self.connection == "connecting":
            return "connecting"
        if self.connection == "offline":
            return "offline"
        if self.capturing:
            return "observing"
        return self.presence

    @property
    def busy(self):
        """The send key shows "stop" while the companion thinks or speaks."""
        return self.connection == "connected" and self.presence in ("thinking", "speaking")

    @property
    def can_send(self):
        return self.connection == "connected"

    @property
    def waited_s(self):
        if self.presence != "thinking" or self.thinking_since is None:
            return 0
        return int(self.clock() - self.thinking_since)

    @property
    def retry_in_s(self):
        if self.next_retry_at is None:
            return None
        return max(0, int(round(self.next_retry_at - self.clock())))

    def status_text(self):
        s = self.state
        if s == "offline":
            return "连接断开 · 正在重连"
        if s == "observing":
            return "正在看当前窗口"
        if s == "thinking":
            w = self.waited_s
            return f"在想 · 已等 {w} 秒" if w * 1000 >= MOTION.thinking_hint_after else "在想…"
        return STATE[s]["label"]

    @property
    def slow(self):
        return self.presence == "thinking" and self.waited_s * 1000 >= MOTION.thinking_slow_after

    # ------------------------------------------------------------ inputs (U1 adapter)
    def _emit(self):
        self.changed.emit(self)

    def set_connection(self, state, attempt=0, next_retry_ms=None, reason=""):
        assert state in CONNECTION, state
        self.connection = state
        self.attempt = attempt
        self.next_retry_at = None if next_retry_ms is None else self.clock() + next_retry_ms / 1000.0
        self.reason = reason
        if state == "offline":
            self.capturing = False
            if self.presence in ("thinking", "speaking", "listening"):
                self._set_presence("idle")
        self._emit()

    def set_typing(self, active):
        self.typing = bool(active)
        if active and self.presence == "idle":
            self._set_presence("listening")
        elif not active and self.presence == "listening":
            self._set_presence("idle")
        self._emit()

    def turn_sent(self, turn_id):
        self.turn_id = turn_id
        self.typing = False
        self._set_presence("thinking")
        self._emit()

    def reply_arrived(self, turn_id, will_speak):
        if turn_id != self.turn_id:
            return
        if will_speak:
            self._grace.start(self.SPEECH_START_GRACE_MS)
        else:
            self._set_presence("idle")
        self._emit()

    def turn_failed(self, turn_id):
        if turn_id == self.turn_id and self.presence == "thinking":
            self._set_presence("idle")
            self._emit()

    def segment_started(self, index, total, text):
        self.segment_index, self.segment_total, self.segment_text = index, total, text
        self.progress = 0.0
        self._set_presence("speaking")
        self._emit()

    def segment_progress(self, fraction):
        # separate signal: 10 Hz playback ticks must not re-render the whole panel
        self.progress = max(0.0, min(1.0, fraction))
        self.progressed.emit(self.progress)

    def speech_finished(self):
        if self.presence in ("speaking", "thinking"):
            self._set_presence("idle")
            self._emit()

    def interrupted(self):
        if self.presence in ("speaking", "thinking"):
            self._set_presence("interrupted")
            self._settle.start(MOTION.interrupt_chip)
            self._emit()

    def set_observe(self, periodic=None, autonomous=None, capturing=None):
        if periodic is not None:
            self.periodic = bool(periodic)
        if autonomous is not None:
            self.autonomous = bool(autonomous)
        if capturing is not None:
            self.capturing = bool(capturing)
        self._emit()

    # ------------------------------------------------------------ U2 seam
    def apply_event(self, event):
        """Map desktop-session / cerebellum events (see the U1 report) onto the same inputs.

        Recognised: connection, presence, turn.accepted, turn.failed, segment.start,
        segment.progress, segment.end, utterance.interrupted, observe.state.
        Unknown events are ignored so the UI keeps running against newer sessions.
        """
        kind = event.get("type")
        if kind == "connection":
            mapping = {"connecting": "connecting", "connected": "connected",
                       "reconnecting": "offline", "closed": "offline"}
            self.set_connection(mapping.get(event.get("state"), "offline"), event.get("attempt", 0),
                                event.get("next_retry_ms"), event.get("reason", ""))
        elif kind == "presence" and event.get("state") in PRESENCE:
            self._set_presence(event["state"])
            if event["state"] == "interrupted":
                self._settle.start(MOTION.interrupt_chip)
            self._emit()
        elif kind == "turn.accepted":
            if event.get("turn_id") and self.presence != "thinking":
                self.turn_sent(event["turn_id"])
        elif kind == "turn.failed":
            self.turn_failed(event.get("turn_id"))
        elif kind == "segment.start":
            self.segment_started(event.get("index", 0), event.get("total", 0), event.get("text", ""))
        elif kind == "segment.progress":
            d = event.get("duration_ms") or 0
            self.segment_progress((event.get("position_ms", 0) / d) if d else 0.0)
        elif kind == "segment.end" and event.get("last"):
            self.speech_finished()
        elif kind == "utterance.interrupted":
            self.interrupted()
        elif kind == "observe.state":
            self.set_observe(capturing=event.get("phase") in ("requested", "capturing"))

    # ------------------------------------------------------------ internals
    def _set_presence(self, presence):
        self._grace.stop()
        if presence != "interrupted":
            self._settle.stop()
        if presence == "thinking":
            self.thinking_since = self.clock()
            self._tick.start()
        else:
            self.thinking_since = None
            self._tick.stop()
        if presence != "speaking":
            self.progress = 0.0
        if presence == "idle" and self.typing:
            presence = "listening"
        self.presence = presence

    def _second(self):
        self._emit()

    def _interrupt_settled(self):
        if self.presence == "interrupted":
            self._set_presence("idle")
            self._emit()

    def _speech_never_started(self):
        if self.presence == "thinking":
            self._set_presence("idle")
            self._emit()
