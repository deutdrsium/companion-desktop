"""Multi-line input: grows 1-4 lines, Enter sends, Shift+Enter breaks, Esc stops."""

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget

from . import theme
from .paint import SendButton
from .widgets import label

TYPING_IDLE_MS = 1500
MAX_LINES = 4


class Entry(QPlainTextEdit):
    submit = pyqtSignal()
    escape = pyqtSignal()
    focus_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Entry")
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(4)
        self.composing = False

    def inputMethodEvent(self, e):
        # While the IME is composing, Enter belongs to the IME, not to "send".
        self.composing = bool(e.preeditString())
        super().inputMethodEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not self.composing:
            if e.modifiers() & Qt.ShiftModifier:
                self.insertPlainText("\n")
            else:
                self.submit.emit()
            return
        if e.key() == Qt.Key_Escape:
            self.escape.emit()
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.focus_changed.emit(True)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.focus_changed.emit(False)


class Composer(QWidget):
    send = pyqtSignal(str)
    stop = pyqtSignal()
    typing = pyqtSignal(bool)

    DEFAULT_HINT = "Enter 发送 · Shift+Enter 换行 · Esc 让她停下"

    def __init__(self, name="她", parent=None):
        super().__init__(parent)
        self.name = name
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 6, 14, 12)
        v.setSpacing(6)
        self.box = QFrame()
        self.box.setObjectName("Composer")
        h = QHBoxLayout(self.box)
        h.setContentsMargins(12, 7, 6, 7)
        h.setSpacing(8)
        self.entry = Entry()
        self.entry.setPlaceholderText(f"和{name}说点什么…")
        self.entry.setAccessibleName("输入消息")
        self.entry.setFont(theme.font("message"))
        h.addWidget(self.entry, 1)
        self.button = SendButton()
        h.addWidget(self.button, 0, Qt.AlignBottom)
        v.addWidget(self.box)
        self.hint = label("", "caption", "ink3")
        v.addWidget(self.hint)
        self.mode = "send"
        self.blocked = False
        self.custom_hint = None
        self._typing = False
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(TYPING_IDLE_MS)
        self._idle.timeout.connect(self._typing_idle)
        self.entry.textChanged.connect(self._changed)
        self.entry.submit.connect(self._submit)
        self.entry.escape.connect(self.stop)
        self.entry.focus_changed.connect(self._focus)
        self.button.clicked.connect(self._clicked)
        self._grow()
        self._refresh()

    # ------------------------------------------------------------ API
    def text(self):
        return self.entry.toPlainText()

    def set_text(self, text):
        self.entry.setPlainText(text)
        cur = self.entry.textCursor()
        cur.movePosition(cur.End)
        self.entry.setTextCursor(cur)

    def restore_draft(self, text):
        """Put a failed message back without losing whatever was typed since."""
        now = self.text()
        if text and text not in now:
            self.set_text(text + ("\n" + now if now.strip() else ""))

    def set_mode(self, mode):
        self.mode = mode
        self.button.set_mode(mode)
        self._refresh()

    def set_blocked(self, blocked):
        """Offline: keep the draft editable, only sending is disabled."""
        self.blocked = blocked
        self._refresh()

    def set_hint(self, text):
        self.custom_hint = text
        self._refresh()

    def screen_rect(self):
        """Entry rectangle in physical pixels (for the cerebellum's look-at, U2)."""
        r = self.entry.rect()
        tl = self.entry.mapToGlobal(r.topLeft())
        ratio = self.devicePixelRatioF()
        return [int(tl.x() * ratio), int(tl.y() * ratio), int(r.width() * ratio), int(r.height() * ratio)]

    # ------------------------------------------------------------ internals
    def _changed(self):
        self._grow()
        self._refresh()
        if self.text().strip():
            self._set_typing(True)
            self._idle.start()
        else:
            self._idle.stop()
            self._set_typing(False)

    @pyqtSlot()
    def _typing_idle(self):
        self._set_typing(False)

    def _set_typing(self, active):
        if active != self._typing:
            self._typing = active
            self.typing.emit(active)

    def _grow(self):
        fm = QFontMetrics(self.entry.font())
        lines = max(1, min(MAX_LINES, int(round(self.entry.document().size().height()))))
        self.entry.setFixedHeight(int(lines * fm.lineSpacing() + 2 * self.entry.document().documentMargin() + 6))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._grow()

    def _focus(self, focused):
        self.box.setProperty("focus", "true" if focused else "false")
        self.box.style().unpolish(self.box)
        self.box.style().polish(self.box)
        self._refresh()

    def _refresh(self):
        self.box.setProperty("blocked", "true" if self.blocked else "false")
        self.box.style().unpolish(self.box)
        self.box.style().polish(self.box)
        if self.mode == "stop":
            self.button.setEnabled(True)
        else:
            self.button.setEnabled(bool(self.text().strip()) and not self.blocked)
        hint = self.custom_hint
        if hint is None:
            hint = self.DEFAULT_HINT if self.entry.hasFocus() else ""
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))

    def _submit(self):
        if self.mode == "stop" and not self.text().strip():
            self.stop.emit()
            return
        text = self.text().strip()
        if not text or self.blocked:
            return
        self.entry.clear()
        self._idle.stop()
        self._set_typing(False)
        self.send.emit(text)

    def _clicked(self):
        if self.mode == "stop":
            self.stop.emit()
        else:
            self._submit()
