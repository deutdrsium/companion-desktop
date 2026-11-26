"""Main panel: header / banner / conversation / composer. Pure view, driven by SessionState."""

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import QHBoxLayout, QMenu, QVBoxLayout, QWidget

from .chat_view import ChatView
from .composer import Composer
from .observe import ObservePopover
from .paint import Dot, IconButton, StateAvatar
from .theme import STATE
from .widgets import Banner, Toast, hline, label, set_label_color

try:
    from PyQt5.QtGui import QAccessible, QAccessibleEvent
except ImportError:  # pragma: no cover
    QAccessible = None


class Header(QWidget):
    def __init__(self, character, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 14, 12, 10)
        h.setSpacing(12)
        self.avatar = StateAvatar(character, "connecting", 46)
        h.addWidget(self.avatar)
        v = QVBoxLayout()
        v.setSpacing(3)
        v.addStretch(1)
        v.addWidget(label(character.name, "name", "ink", voice=True))
        status = QWidget()
        sh = QHBoxLayout(status)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.setSpacing(6)
        self.dot = Dot(STATE["connecting"]["ring"])
        sh.addWidget(self.dot)
        self.status = label("", "label")
        sh.addWidget(self.status)
        sh.addStretch(1)
        v.addWidget(status)
        v.addStretch(1)
        h.addLayout(v, 1)
        self.eye = IconButton("eye", f"{character.name}能看到什么", tint=STATE["observing"]["ring"])
        self.pin = IconButton("pin", "窗口置顶")
        self.more = IconButton("more", "更多")
        for b in (self.eye, self.pin, self.more):
            h.addWidget(b)
        self.setAccessibleName(character.name)

    def set_state(self, state):
        s = state.state
        st = STATE[s]
        self.avatar.set_state(s)
        self.dot.set_color(st["ring"])
        text = state.status_text()
        self.status.setText(text)
        set_label_color(self.status, st["fg"])
        self.setAccessibleDescription(text)
        if QAccessible is not None:
            try:
                QAccessible.updateAccessibility(QAccessibleEvent(self, QAccessible.DescriptionChanged))
            except Exception:
                pass
        observing = state.periodic or state.autonomous or state.capturing
        self.eye.set_look("eye_on" if observing else "eye", observing,
                          STATE["observing"]["ring"] if state.capturing else None)


class Panel(QWidget):
    send = pyqtSignal(str)
    stop = pyqtSignal()
    typing = pyqtSignal(bool)
    reconnect_now = pyqtSignal()
    pin_toggled = pyqtSignal(bool)
    open_settings = pyqtSignal(int)
    copy_subtitle_url = pyqtSignal()
    quit = pyqtSignal()

    def __init__(self, character, title, state, pinned=True):
        super().__init__()
        self.character, self.state = character, state
        self.setObjectName("Panel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle(title)
        self.setMinimumSize(360, 540)
        self.on_close = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.header = Header(character)
        v.addWidget(self.header)
        v.addWidget(hline())
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(14, 10, 14, 0)
        self.banner = Banner()
        wl.addWidget(self.banner)
        self.banner_wrap = wrap
        wrap.hide()
        v.addWidget(wrap)
        self.chat = ChatView()
        v.addWidget(self.chat, 1)
        self.composer = Composer(character.name)
        v.addWidget(self.composer)
        self.toast = Toast(self)
        self.popover = ObservePopover(self, character.name, anchor=self.header.eye)
        self.pinned = pinned
        self.header.pin.set_look("pin_on" if pinned else "pin", pinned)
        self.header.pin.clicked.connect(lambda: self.set_pinned(not self.pinned, emit=True))
        self.header.eye.clicked.connect(self.popover.open)
        self.header.more.clicked.connect(self._more_menu)
        self.composer.send.connect(self.send)
        self.composer.stop.connect(self.stop)
        self.composer.typing.connect(self.typing)
        self.banner.action.connect(self.reconnect_now)
        self.countdown = QTimer(self)
        self.countdown.setInterval(1000)
        self.countdown.timeout.connect(lambda: self._state(self.state))
        # Tab order: input -> send -> header icons -> conversation
        self.setTabOrder(self.composer.entry, self.composer.button)
        self.setTabOrder(self.composer.button, self.header.eye)
        self.setTabOrder(self.header.eye, self.header.pin)
        self.setTabOrder(self.header.pin, self.header.more)
        state.changed.connect(self._state)
        self._state(state)

    # ------------------------------------------------------------ geometry
    def default_size(self):
        screen = QGuiApplication.primaryScreen()
        h = 760
        if screen is not None:
            h = max(540, min(900, int(screen.availableGeometry().height() * 0.70)))
        self.resize(440, h)

    def set_pinned(self, pinned, emit=False):
        self.pinned = pinned
        self.header.pin.set_look("pin_on" if pinned else "pin", pinned)
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, pinned)
        if visible:
            self.show()
        if emit:
            self.pin_toggled.emit(pinned)

    # ------------------------------------------------------------ state
    def _state(self, state):
        name = self.character.name
        self.header.set_state(state)
        c = self.composer
        c.set_mode("stop" if state.busy else "send")
        c.set_blocked(not state.can_send)
        s = state.state
        if s == "offline":
            c.set_hint("连上以后可以发送，你写的内容会留着")
        elif s == "connecting":
            c.set_hint("连上以后可以发送")
        elif not state.can_send:
            c.set_hint("设备未获对话授权，你写的内容会留着")
        elif state.presence == "thinking":
            c.set_hint("比平时慢一些 · 网络或模型在排队，按 Esc 可以取消" if state.slow
                       else f"{name}在想 · 按 Esc 或点 ■ 可以取消")
        elif state.presence == "speaking":
            c.set_hint(f"{name}在说 · 直接输入会先让她停下")
        else:
            c.set_hint(None)
        if self.chat.thinking is not None:
            self.chat.thinking.set_waited(state.waited_s)
        if s == "offline":
            retry = state.retry_in_s
            when = f"（第 {state.attempt} 次，{retry} 秒后）" if retry else (
                f"（第 {state.attempt} 次）" if state.attempt else "")
            if state.connection_closed:
                self.banner.show_message("offline", "桌面会话已关闭", state.status_text(), "立即重连")
            else:
                self.banner.show_message("offline", "和服务器的连接断了", f"正在重连{when}。", "立即重连")
            self.banner_wrap.show()
            if state.connection_closed:
                self.countdown.stop()
            elif not self.countdown.isActive():
                self.countdown.start()
        else:
            self.countdown.stop()
            self.banner.hide()
            self.banner_wrap.hide()

    def show_service_banner(self, title, detail):
        self.banner.show_message("offline", title, detail)
        self.banner_wrap.show()

    # ------------------------------------------------------------ menus
    def more_menu(self):
        m = QMenu(self)
        m.addAction("设置…", lambda: self.open_settings.emit(0))
        m.addAction("复制 VTS 字幕地址", self.copy_subtitle_url.emit)
        m.addAction("声音…", lambda: self.open_settings.emit(1))
        m.addAction("关于", lambda: self.open_settings.emit(5))
        m.addSeparator()
        m.addAction("退出", self.quit.emit)
        return m

    def _more_menu(self):
        m = self.more_menu()
        m.exec_(self.header.more.mapToGlobal(self.header.more.rect().bottomLeft()))

    def flash(self, state, text):
        self.toast.flash(state, text, y=self.header.height() + 10)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        for layer in (self.popover,):
            if layer.isVisible():
                layer.setGeometry(self.rect())
                layer.place()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.stop.emit()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        if self.on_close is not None and not self.on_close():
            e.ignore()
            return
        e.accept()
