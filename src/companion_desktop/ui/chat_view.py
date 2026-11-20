"""Conversation stream: bubbles, thinking bubble, system notes, date lines."""

import html
import time

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from . import theme
from .paint import Dots, Waveform
from .theme import C, RADIUS, STATE
from .widgets import Divider, Note, button, label, set_label_color

BUBBLE_RATIO = 0.78
DIVIDER_GAP_S = 5 * 60
RENDER_LIMIT = 300   # beyond this many items ...
RENDER_KEEP = 200    # ... keep only the newest, offer "加载更早的"


def _bubble_frame(companion):
    frame = QFrame()
    frame.setObjectName("Bubble")
    tl, tr = (6, RADIUS["md"]) if companion else (RADIUS["md"], 6)
    frame.setStyleSheet(
        f"QFrame#Bubble {{ background: {C['surface'] if companion else C['user_bubble']};"
        f" border: {'1px solid ' + C['line'] if companion else 'none'};"
        f" border-top-left-radius: {tl}px; border-top-right-radius: {tr}px;"
        f" border-bottom-left-radius: {RADIUS['md']}px; border-bottom-right-radius: {RADIUS['md']}px; }}")
    return frame


def _para(inner, voice):
    lh = 128 if voice else 140  # Qt adds this on top of the font's own leading
    return f'<p style="margin:0; line-height:{lh}%; color:{C["ink"]}">{inner}</p>'


def _esc(text):
    return html.escape(text).replace("\n", "<br>")


class _Row(QWidget):
    """A bubble row; the body label width follows the viewport."""

    def __init__(self, companion):
        super().__init__()
        self.companion = companion
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.col = QVBoxLayout()
        self.col.setSpacing(4)
        self.frame = _bubble_frame(companion)
        self.inner = QVBoxLayout(self.frame)
        self.inner.setContentsMargins(14, 10, 14, 10)
        self.inner.setSpacing(6)
        self.body = QLabel()
        self.body.setTextFormat(Qt.RichText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setStyleSheet("background: transparent;")
        self.body.setFont(theme.font("message", voice=companion))
        self.inner.addWidget(self.body)
        self.col.addWidget(self.frame)
        if companion:
            row.addLayout(self.col)
            row.addStretch(1)
        else:
            row.addStretch(1)
            row.addLayout(self.col)
        self.plain = ""
        self.viewport_w = 440

    def fit(self, viewport_w=None):
        if viewport_w:
            self.viewport_w = viewport_w
        max_w = int(self.viewport_w * BUBBLE_RATIO) - 28
        fm = QFontMetrics(self.body.font())
        natural = max((fm.horizontalAdvance(line) for line in (self.plain or " ").split("\n")), default=0) + 6
        # word-wrapped labels otherwise shrink to ~50 % of the space
        self.body.setFixedWidth(max(40, min(natural, max_w)))


class UserBubble(_Row):
    retry = pyqtSignal(object)

    def __init__(self, text, turn_id=None, at=None):
        super().__init__(companion=False)
        self.text, self.turn_id = text, turn_id
        self.at = at
        self.plain = text
        self.body.setText(_para(_esc(text), voice=False))
        self.body.setAccessibleName("你说：" + text)
        self.meta = QWidget()
        mh = QHBoxLayout(self.meta)
        mh.setContentsMargins(0, 0, 0, 0)
        mh.setSpacing(6)
        mh.addStretch(1)
        self.meta_label = label("", "caption", "ink3")
        mh.addWidget(self.meta_label)
        self.retry_button = button("重试", "ghost", tip="重新发送这条消息")
        self.retry_button.clicked.connect(lambda: self.retry.emit(self))
        mh.addWidget(self.retry_button)
        self.col.addWidget(self.meta, 0, Qt.AlignRight)
        self.set_receipt("history" if turn_id is None else "sending")

    def set_receipt(self, receipt):
        """history | sending | delivered | failed | resent | queued"""
        self.receipt = receipt
        stamp = time.strftime("%H:%M", time.localtime(self.at)) if self.at else ""
        text = {"history": "", "sending": "发送中…", "delivered": f"{stamp} · 已送达",
                "failed": "没送达", "resent": f"{stamp} · 已重发", "queued": "连上后发送"}[receipt]
        self.meta_label.setText(text)
        set_label_color(self.meta_label, theme.ERROR_FG if receipt == "failed" else "ink3")
        self.retry_button.setVisible(receipt == "failed")
        self.meta.setVisible(bool(text))


class ReplyBubble(_Row):
    """Companion message. segments: the sentences actually spoken, in order."""

    stop = pyqtSignal()
    resume = pyqtSignal(object, int)   # bubble, first unspoken segment
    replay = pyqtSignal(object)

    def __init__(self, text, segments=None, at=None):
        super().__init__(companion=True)
        self.text = text
        self.segments = list(segments) if segments else [text]
        self.plain = text
        self.at = at
        self.current = -1
        self.spoken_until = -1   # segments <= this have been read
        self.mode = "plain"
        self.footer = QWidget()
        self.fh = QHBoxLayout(self.footer)
        self.fh.setContentsMargins(0, 2, 0, 0)
        self.fh.setSpacing(8)
        self.wave = Waveform()
        self.foot_label = label("", "caption", "ink3")
        self.foot_button = button("", "ghost")
        self.foot_button.clicked.connect(self._footer_action)
        self.fh.addWidget(self.wave)
        self.fh.addWidget(self.foot_label)
        self.fh.addStretch(1)
        self.fh.addWidget(self.foot_button)
        self.inner.addWidget(self.footer)
        self.meta = label("", "caption", "ink3")
        self.col.addWidget(self.meta, 0, Qt.AlignLeft)
        self.body.setAccessibleName("回复：" + text)
        self.set_mode("plain")
        self._render()

    # segments: spoken | current | next | pending
    def _render(self):
        if len(self.segments) <= 1 and self.current < 0 and self.mode != "interrupted":
            self.body.setText(_para(_esc(self.text), voice=True))
            return
        parts = []
        for i, seg in enumerate(self.segments):
            s = _esc(seg)
            if i == self.current and self.mode == "speaking":
                parts.append(f'<span style="background:{STATE["speaking"]["soft"]}">{s}</span>')
            elif self.mode == "interrupted" and i > self.spoken_until:
                parts.append(f'<span style="color:{C["ink3"]}">{s}</span>')
            else:
                parts.append(s)
        self.body.setText(_para("".join(parts), voice=True))

    def set_current(self, index):
        self.current = index
        self.spoken_until = max(self.spoken_until, index - 1)
        self.set_mode("speaking")

    def segment_done(self, index):
        self.spoken_until = max(self.spoken_until, index)

    def set_mode(self, mode):
        """plain | speaking | interrupted | stopped | speech_failed | timed"""
        self.mode = mode
        speaking = mode == "speaking"
        self.wave.setVisible(speaking)
        self.wave.animate(speaking)
        remaining = len(self.segments) - 1 - self.spoken_until
        if speaking:
            total = len(self.segments)
            self.foot_label.setText(f"正在说 · 第 {self.current + 1}/{total} 句" if total > 1 else "正在说")
            set_label_color(self.foot_label, STATE["speaking"]["fg"])
            self.foot_button.setText("停下")
            self.foot_button.setToolTip("让她停下（Esc）")
        elif mode == "interrupted":
            self.foot_label.setText(f"已停下 · 还有 {remaining} 句没读" if remaining > 0 else "已停下")
            set_label_color(self.foot_label, "ink3")
            self.foot_button.setText("接着读")
            self.foot_button.setToolTip("从没读的那句接着读")
        elif mode == "speech_failed":
            self.foot_label.setText("语音没生成出来，文字都在这里")
            set_label_color(self.foot_label, theme.ERROR_FG)
            self.foot_button.setText("重新朗读")
            self.foot_button.setToolTip("重新生成这条的语音")
        elif mode == "stopped":
            self.foot_label.setText("已停下")
            set_label_color(self.foot_label, "ink3")
        self.foot_button.setVisible(speaking or mode == "speech_failed" or (mode == "interrupted" and remaining > 0))
        self.footer.setVisible(mode in ("speaking", "interrupted", "speech_failed", "stopped"))
        stamp = time.strftime("%H:%M", time.localtime(self.at)) if self.at else ""
        self.meta.setText(stamp)
        self.meta.setVisible(bool(stamp) and not speaking)
        self._render()

    def _footer_action(self):
        if self.mode == "speaking":
            self.stop.emit()
        elif self.mode == "interrupted":
            self.resume.emit(self, self.spoken_until + 1)
        elif self.mode == "speech_failed":
            self.replay.emit(self)


class ThinkingBubble(QWidget):
    def __init__(self):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        frame = _bubble_frame(True)
        h = QHBoxLayout(frame)
        h.setContentsMargins(12, 9, 14, 9)
        h.setSpacing(6)
        self.dots = Dots()
        h.addWidget(self.dots)
        self.text = label("在想", "caption", STATE["thinking"]["fg"])
        h.addWidget(self.text)
        row.addWidget(frame)
        row.addStretch(1)
        self.setAccessibleName("正在想")

    def set_waited(self, seconds):
        self.text.setText("在想" + (f" · 已等 {seconds} 秒" if seconds * 1000 >= theme.MOTION.thinking_hint_after
                                   else ""))


class ChatView(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Chat")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.setFocusPolicy(Qt.NoFocus)
        self.body = QWidget()
        self.body.setObjectName("ChatBody")
        self.lay = QVBoxLayout(self.body)
        self.lay.setContentsMargins(14, 12, 14, 12)
        self.lay.setSpacing(10)
        self.more = button("加载更早的", "ghost")
        self.more.clicked.connect(self.load_earlier)
        self.more.hide()
        self.lay.addWidget(self.more, 0, Qt.AlignHCenter)
        self.lay.addStretch(1)
        self.setWidget(self.body)
        self.items = []        # rendered widgets, oldest first
        self.hidden = []       # (factory) for items trimmed from the top
        self.thinking = None
        self.last_at = None
        self._stick = True
        self.verticalScrollBar().rangeChanged.connect(self._range_changed)
        self.verticalScrollBar().valueChanged.connect(self._value_changed)

    # ------------------------------------------------------------ scrolling
    def at_bottom(self):
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 24

    def _value_changed(self, _):
        self._stick = self.at_bottom()

    def _range_changed(self, _lo, hi):
        if self._stick:
            self.verticalScrollBar().setValue(hi)

    def scroll_to_bottom(self):
        self._stick = True
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        w = self.viewport().width()
        for it in self.items:
            if isinstance(it, _Row):
                it.fit(w)

    # ------------------------------------------------------------ items
    def _add(self, widget):
        stick = self._stick or self.at_bottom()
        if isinstance(widget, _Row):
            widget.fit(self.viewport().width())
        if self.thinking is not None and widget is not self.thinking:
            # the thinking bubble always stays last
            li, ii = self.lay.indexOf(self.thinking), self.items.index(self.thinking)
        else:
            li, ii = self.lay.count(), len(self.items)
        self.lay.insertWidget(li, widget)
        self.items.insert(ii, widget)
        self._stick = stick
        self._trim()
        return widget

    def _trim(self):
        if len(self.items) <= RENDER_LIMIT:
            return
        while len(self.items) > RENDER_KEEP:
            old = self.items.pop(0)
            self.lay.removeWidget(old)
            old.setParent(None)
            self.hidden.append(old)
        self.more.show()

    def load_earlier(self):
        bar = self.verticalScrollBar()
        before = bar.maximum() - bar.value()
        batch, self.hidden = self.hidden[-RENDER_KEEP:], self.hidden[:-RENDER_KEEP]
        for i, w in enumerate(batch):
            self.lay.insertWidget(2 + i, w)  # after the "load earlier" button and the top stretch
            self.items.insert(i, w)
            if isinstance(w, _Row):
                w.fit(self.viewport().width())
        self.more.setVisible(bool(self.hidden))
        self._stick = False
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum() - before))

    def _maybe_divider(self, at):
        if at is None:
            return
        if self.last_at is None or at - self.last_at > DIVIDER_GAP_S:
            day = "今天" if time.localtime(at)[:3] == time.localtime()[:3] else time.strftime("%m-%d", time.localtime(at))
            self._add(Divider(f"{day} {time.strftime('%H:%M', time.localtime(at))}"))
        self.last_at = at

    def load_history(self, messages, strip=lambda t: t):
        for m in messages:
            text = str(m.get("text", ""))
            if m.get("role") == "user":
                self._add(UserBubble(text))
            else:
                text = strip(text)
                if text:
                    self._add(ReplyBubble(text))
        if messages:
            self._add(Divider("以上是之前的对话"))
        self.scroll_to_bottom()

    def add_user(self, text, turn_id, at=None):
        at = at or time.time()
        self._maybe_divider(at)
        return self._add(UserBubble(text, turn_id, at))

    def show_thinking(self):
        if self.thinking is None:
            self.thinking = ThinkingBubble()
            self._add(self.thinking)
        return self.thinking

    def hide_thinking(self):
        if self.thinking is not None:
            if self.thinking in self.items:
                self.items.remove(self.thinking)
            self.lay.removeWidget(self.thinking)
            self.thinking.hide()  # removeWidget keeps the parent; hide until deletion runs
            self.thinking.dots.animate(False)
            self.thinking.deleteLater()
            self.thinking = None

    def add_reply(self, text, segments, at=None):
        """The thinking bubble is replaced in place by the reply."""
        at = at or time.time()
        self.hide_thinking()
        self.last_at = at
        return self._add(ReplyBubble(text, segments, at))

    def add_note(self, state, text):
        return self._add(Note(state, text))
