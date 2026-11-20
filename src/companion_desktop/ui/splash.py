"""Launch card: steps, plain-language failure, retry / quit / logs."""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from .paint import StateAvatar, StepIcon
from .widgets import Card, button, label, set_label_color, shadow

STEPS = ("读取本机设置", "连接服务器", "读取聊天记录", "准备字幕和语音")


class LaunchCard(QWidget):
    retry = pyqtSignal()
    quit = pyqtSignal()
    open_logs = pyqtSignal()

    def __init__(self, character, title, quiet=False):
        super().__init__()
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        if quiet:  # Steam autostart: do not steal focus
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 32)
        card = Card(radius=18)
        card.setFixedWidth(380)
        outer.addWidget(card)
        shadow(card, 36, 10, 60)
        v = QVBoxLayout(card)
        v.setContentsMargins(22, 20, 22, 18)
        v.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.avatar = StateAvatar(character, "thinking", 48)
        top.addWidget(self.avatar)
        tv = QVBoxLayout()
        tv.setSpacing(2)
        self.name = character.name
        self.heading = label(f"正在叫醒{character.name}…", "title")
        self.detail = label("通常 2–5 秒。VTube Studio 可以先开着。", "caption", "ink3", wrap=True)
        tv.addWidget(self.heading)
        tv.addWidget(self.detail)
        top.addLayout(tv, 1)
        v.addLayout(top)
        self.rows = []
        for name in STEPS:
            r = QHBoxLayout()
            r.setSpacing(10)
            icon = StepIcon()
            text = label(name, "body", "ink3")
            r.addWidget(icon)
            r.addWidget(text, 1)
            v.addLayout(r)
            self.rows.append((icon, text))
        self.buttons = QWidget()
        b = QHBoxLayout(self.buttons)
        b.setContentsMargins(0, 4, 0, 0)
        logs = button("查看日志", "ghost")
        q = button("退出", "secondary")
        self.retry_button = button("重试", "primary")
        b.addWidget(logs)
        b.addStretch(1)
        b.addWidget(q)
        b.addWidget(self.retry_button)
        v.addWidget(self.buttons)
        self.buttons.hide()
        logs.clicked.connect(self.open_logs)
        q.clicked.connect(self.quit)
        self.retry_button.clicked.connect(self.retry)
        self.set_step(0)

    def set_step(self, index):
        """Steps before index are done, index is in progress."""
        self.buttons.hide()
        self.avatar.set_state("thinking")
        self.heading.setText(f"正在叫醒{self.name}…")
        self.detail.setText("通常 2–5 秒。VTube Studio 可以先开着。")
        for i, (icon, text) in enumerate(self.rows):
            kind = "done" if i < index else "doing" if i == index else "todo"
            icon.set_kind(kind)
            set_label_color(text, "ink" if kind != "todo" else "ink3")

    def fail(self, index, title, detail):
        self.avatar.set_state("offline")
        self.heading.setText(title)
        self.detail.setText(detail)
        for i, (icon, text) in enumerate(self.rows):
            kind = "done" if i < index else "fail" if i == index else "todo"
            icon.set_kind(kind)
            set_label_color(text, "ink" if kind != "todo" else "ink3")
        self.buttons.show()
        self.adjustSize()
        self.retry_button.setFocus()

    def center_on_screen(self):
        self.adjustSize()
        screen = self.screen().availableGeometry() if hasattr(self, "screen") else None
        if screen is not None:
            self.move(screen.center().x() - self.width() // 2, screen.center().y() - self.height() // 2)
