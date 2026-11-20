"""Observation controls: popover ("what can she see"), first-time consent, result chips.

The three paths are separate: periodic, autonomous, and a one-off "look now".
Turning on one switch never turns on the other.
"""

import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from .paint import Chip, StateAvatar, Toggle
from .widgets import Card, button, hline, label, shadow

# result -> (chip state, text). Names follow the observation contract (05); "waiting" is
# today's server answer when the five-minute minimum has not passed yet.
RESULTS = {
    "observed": ("observing", "已观察"),
    "skipped": ("interrupted", "窗口已跳过"),
    "unchanged": ("interrupted", "画面无变化"),
    "waiting": ("interrupted", "刚看过，稍后再看"),
    "unavailable": ("offline", "设备不可用"),
    "failed": ("offline", "失败"),
}


class Layer(QWidget):
    """Full-panel layer: closes on a click outside its card; optional dim."""

    closed = pyqtSignal()

    def __init__(self, parent, dim=False):
        super().__init__(parent)
        self.dim = dim
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("Layer")
        # scoped by id: an unscoped background would cascade into the card's buttons
        self.setStyleSheet("QWidget#Layer { background: %s; }" % ("rgba(43,39,34,0.38)" if dim else "transparent"))
        self.card = None
        self.hide()

    def open(self):
        self.setGeometry(self.parent().rect())
        self.show()
        self.raise_()
        self.place()

    def place(self):
        pass

    def mousePressEvent(self, e):
        if self.card is None or not self.card.geometry().contains(e.pos()):
            self.close_layer()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close_layer()
        else:
            super().keyPressEvent(e)

    def close_layer(self):
        self.hide()
        self.closed.emit()


class ObservePopover(Layer):
    periodic_toggled = pyqtSignal(bool)
    autonomous_toggled = pyqtSignal(bool)
    look_now = pyqtSignal()

    def __init__(self, parent, name="她", anchor=None):
        super().__init__(parent)
        self.anchor = anchor
        self.recent = []  # (timestamp, result)
        card = Card(parent=self)
        self.card = card
        card.setFixedWidth(330)
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 14)
        v.setSpacing(10)
        v.addWidget(label(f"{name}能看到什么", "title"))
        v.addWidget(label("只看当前活动窗口。锁屏、VTube Studio 退出或关掉开关时立刻停止。", "caption", "ink3", wrap=True))
        v.addWidget(hline())
        self.periodic = Toggle(name="定期看看")
        self.autonomous = Toggle(name=f"让{name}自己决定看一眼")
        self.last_chip = Chip("observing", " ", small=True)
        self.last_chip.hide()
        self.autonomous_note = label("", "caption", "ink3", wrap=True)
        v.addWidget(self._row("定期看看", "约每 5 分钟一次。看到的不一定会说出来。", self.periodic, self.last_chip))
        v.addWidget(self._row(f"让{name}自己决定看一眼", "聊天中她觉得需要时才看，有冷却；默认关闭。",
                              self.autonomous, self.autonomous_note))
        v.addWidget(hline())
        self.now = button("现在看一下当前窗口", "secondary")
        self.now.setToolTip("只看这一次，不会打开任何持续观察")
        v.addWidget(self.now)
        self.history = label("", "caption", "ink3", wrap=True)
        v.addWidget(self.history)
        shadow(card)
        self.periodic.toggled.connect(self.periodic_toggled)
        self.autonomous.toggled.connect(self.autonomous_toggled)
        self.now.clicked.connect(self.look_now)
        self.set_autonomous_available(False, "需要新版桌面会话，暂不可用")
        self._render_history()

    @staticmethod
    def _row(title, desc, toggle, extra=None):
        r = QWidget()
        h = QHBoxLayout(r)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        tv = QVBoxLayout()
        tv.setSpacing(2)
        tv.addWidget(label(title, "body", bold=True))
        tv.addWidget(label(desc, "caption", "ink3", wrap=True))
        if extra is not None:
            cw = QHBoxLayout()
            cw.addWidget(extra)
            cw.addStretch(1)
            tv.addLayout(cw)
        h.addLayout(tv, 1)
        h.addWidget(toggle, 0, Qt.AlignTop)
        return r

    def place(self):
        self.card.adjustSize()
        x = max(12, self.width() - self.card.width() - 12)
        y = 64
        if self.anchor is not None:
            y = self.anchor.mapTo(self.parent(), self.anchor.rect().bottomLeft()).y() + 6
        self.card.move(x, y)

    def set_switches(self, periodic, autonomous):
        for t, value in ((self.periodic, periodic), (self.autonomous, autonomous)):
            t.blockSignals(True)
            t.setChecked(value)
            t.blockSignals(False)
            t.update()

    def set_autonomous_available(self, available, reason=""):
        self.autonomous.setEnabled(available)
        self.autonomous_note.setText("" if available else reason)
        self.autonomous_note.setVisible(not available and bool(reason))

    def set_busy(self, busy):
        self.now.setEnabled(not busy)
        self.now.setText("正在看…" if busy else "现在看一下当前窗口")

    def add_result(self, result, at=None):
        at = at or time.time()
        self.recent.insert(0, (at, result))
        del self.recent[5:]
        state, text = RESULTS.get(result, RESULTS["failed"])
        self.last_chip.set(state, f"上次 {time.strftime('%H:%M', time.localtime(at))} · {text}")
        self.last_chip.show()
        self._render_history()

    def _render_history(self):
        if not self.recent:
            self.history.setText("还没有看过。")
            return
        parts = [f"{time.strftime('%H:%M', time.localtime(at))} {RESULTS.get(r, RESULTS['failed'])[1]}"
                 for at, r in self.recent[:3]]
        self.history.setText("最近：" + " · ".join(parts))


CONSENT = {
    "periodic": ("让{name}定期看看你的屏幕？", "开启定期观察", [
        "只截取当前活动窗口，约每 5 分钟一次。",
        "锁屏、VTube Studio 退出、关闭开关或关掉面板时立刻停止。",
        "每次启动都默认关闭，需要你重新打开。",
        "图片默认只用于这一次观察；需要记住的内容按现有记忆规则处理。",
    ], "“让{name}自己决定看一眼”是另一个开关，这里不会一起打开。"),
    "autonomous": ("让{name}自己决定什么时候看一眼？", "允许她自己决定", [
        "只截取当前活动窗口，只在聊天中她觉得需要时才看，有冷却。",
        "锁屏、VTube Studio 退出、关闭开关或关掉面板时立刻停止。",
        "每次启动都默认关闭，需要你重新打开。",
        "图片默认只用于这一次观察；需要记住的内容按现有记忆规则处理。",
    ], "“定期看看”是另一个开关，这里不会一起打开。"),
}


class ConsentDialog(Layer):
    """In-panel modal: explains four things before the first observation switch-on."""

    decided = pyqtSignal(str, bool)  # kind, accepted

    def __init__(self, parent, character, kind="periodic"):
        super().__init__(parent, dim=True)
        self.kind = kind
        name = character.name
        title, ok, points, note = CONSENT[kind]
        card = Card(radius=18, parent=self)
        self.card = card
        card.setFixedWidth(min(360, max(300, parent.width() - 40)))
        v = QVBoxLayout(card)
        v.setContentsMargins(22, 20, 22, 18)
        v.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(StateAvatar(character, "observing", 40))
        top.addSpacing(6)
        top.addWidget(label(title.format(name=name), "title", wrap=True), 1)
        v.addLayout(top)
        for t in points:
            r = QHBoxLayout()
            r.setSpacing(8)
            r.addWidget(label("·", "body", "accent"), 0, Qt.AlignTop)
            r.addWidget(label(t, "body", "ink2", wrap=True), 1)
            v.addLayout(r)
        v.addWidget(label(note.format(name=name), "caption", "ink3", wrap=True))
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.no = button("先不要", "secondary")
        self.yes = button(ok, "primary")
        btns.addWidget(self.no)
        btns.addWidget(self.yes)
        v.addLayout(btns)
        shadow(card, 40, 12, 70)
        self.no.clicked.connect(lambda: self._decide(False))
        self.yes.clicked.connect(lambda: self._decide(True))
        self.setAccessibleName(title.format(name=name))

    def place(self):
        self.card.adjustSize()
        self.card.move((self.width() - self.card.width()) // 2, max(12, (self.height() - self.card.height()) // 2 - 20))
        self.yes.setFocus()

    def mousePressEvent(self, e):
        pass  # modal: only the buttons decide

    def close_layer(self):
        self._decide(False)

    def _decide(self, accepted):
        self.hide()
        self.decided.emit(self.kind, accepted)
        self.deleteLater()
