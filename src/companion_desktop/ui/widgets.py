"""Small shared building blocks (labels, buttons, cards, banner, notes)."""

from PyQt5.QtCore import QPropertyAnimation, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout, QWidget)

from . import theme
from .paint import Chip, Dot
from .theme import C, RADIUS, STATE


def label(text, kind="body", color="ink", voice=False, bold=None, wrap=False):
    w = QLabel(text)
    w.setStyleSheet(f"color: {C.get(color, color)}; background: transparent;")
    w.setFont(theme.font(kind, voice, bold))  # after setStyleSheet: QSS polish resets inherited fonts
    w.setWordWrap(wrap)
    return w


def set_label_color(w, color):
    f = w.font()
    w.setStyleSheet(f"color: {C.get(color, color)}; background: transparent;")
    w.setFont(f)


def button(text, kind="primary", tip=None):
    b = QPushButton(text)
    b.setProperty("kind", kind)
    b.setCursor(Qt.PointingHandCursor)
    b.setAccessibleName(tip or text)
    if tip:
        b.setToolTip(tip)
    return b


def hline():
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {C['line']}; border: none;")
    return line


def shadow(widget, blur=28, dy=8, alpha=46):
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(QColor(60, 44, 28, alpha))
    widget.setGraphicsEffect(eff)


class Card(QFrame):
    def __init__(self, radius=RADIUS["lg"], bg=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setStyleSheet(f"QFrame#Card {{ background: {bg or C['surface']}; border: 1px solid {C['line']};"
                           f" border-radius: {radius}px; }}")


class Banner(QFrame):
    """Shown only when something is wrong (offline, speech service down)."""

    action = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Banner")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 9, 8, 9)
        h.setSpacing(10)
        self.dot = Dot(STATE["offline"]["ring"], 8)
        h.addWidget(self.dot, 0, Qt.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(1)
        self.title = label("", "label", bold=True)
        self.detail = label("", "caption", wrap=True)
        v.addWidget(self.title)
        v.addWidget(self.detail)
        h.addLayout(v, 1)
        self.button = button("", "ghost")
        self.button.clicked.connect(self.action)
        h.addWidget(self.button, 0, Qt.AlignVCenter)
        self.setAccessibleName("提示")
        self.hide()

    def show_message(self, state, title, detail, action=None):
        st = STATE[state]
        self.setStyleSheet(f"QFrame#Banner {{ background: {st['soft']}; border-radius: {RADIUS['sm']}px; }}")
        self.dot.set_color(st["ring"])
        self.title.setText(title)
        self.detail.setText(detail)
        set_label_color(self.title, st["fg"])
        set_label_color(self.detail, st["fg"])
        self.button.setVisible(bool(action))
        if action:
            self.button.setText(action)
            f = self.button.font()
            self.button.setStyleSheet(f"color: {st['fg']};")
            self.button.setFont(f)
        self.setAccessibleDescription(title + "。" + detail)
        self.show()


class Divider(QWidget):
    def __init__(self, text, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(24, 6, 24, 2)
        h.setSpacing(10)
        h.addWidget(hline(), 1)
        h.addWidget(label(text, "caption", "ink3"))
        h.addWidget(hline(), 1)


class Note(QWidget):
    """Centred system note in the conversation (reconnected, looked at the screen...)."""

    def __init__(self, state, text, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 2, 0, 2)
        h.addStretch(1)
        self.chip = Chip(state, text, small=True)
        h.addWidget(self.chip)
        h.addStretch(1)


class Toast(Chip):
    """Floating chip that fades out (e.g. "已重新连上")."""

    def __init__(self, parent):
        super().__init__("idle", " ", small=True, parent=parent)
        self.effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.effect)
        self.fade = QPropertyAnimation(self.effect, b"opacity", self)
        self.fade.setDuration(theme.MOTION.subtitle_out)
        self.fade.setStartValue(1.0)
        self.fade.setEndValue(0.0)
        self.fade.finished.connect(self.hide)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.fade.start)
        self.hide()

    def flash(self, state, text, ms=None, y=70):
        self.fade.stop()
        self.effect.setOpacity(1.0)
        self.set(state, text)
        self.move((self.parent().width() - self.width()) // 2, y)
        self.show()
        self.raise_()
        self.timer.start(ms or theme.MOTION.reconnected_toast)
