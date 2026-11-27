"""Tray icon (avatar + state ring, a dot at 16 px) and its menu."""

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QAction, QMenu, QSystemTrayIcon

from .paint import tray_icon


class Tray(QObject):
    show_panel = pyqtSignal()
    stop = pyqtSignal()
    periodic_toggled = pyqtSignal(bool)
    autonomous_toggled = pyqtSignal(bool)
    look_now = pyqtSignal()
    together = pyqtSignal()
    copy_subtitle_url = pyqtSignal()
    pin_toggled = pyqtSignal(bool)
    open_settings = pyqtSignal()
    quit = pyqtSignal()

    def __init__(self, character, title, state, parent=None):
        super().__init__(parent)
        self.character, self.title = character, title
        self.icon = QSystemTrayIcon()
        self.menu = QMenu()
        self.menu.setFixedWidth(250)
        self.a_show = self._act("显示面板", self.show_panel)
        self.a_stop = self._act(f"让{character.name}停下", self.stop)
        self.menu.addSeparator()
        self.a_periodic = self._act("定期看看（每 5 分钟）", None, checkable=True)
        self.a_periodic.toggled.connect(self.periodic_toggled)
        self.a_autonomous = self._act(f"让{character.name}自己决定看一眼", None, checkable=True)
        self.a_autonomous.toggled.connect(self.autonomous_toggled)
        self.a_autonomous.setEnabled(False)
        self.a_now = self._act("现在看一下当前窗口", self.look_now)
        self.menu.addSeparator()
        self.a_together = self._act("一起听 / 一起看…", self.together)
        self._act("复制 VTS 字幕地址", self.copy_subtitle_url)
        self.a_pin = self._act("窗口置顶", None, checkable=True)
        self.a_pin.toggled.connect(self.pin_toggled)
        self._act("设置…", self.open_settings)
        self.menu.addSeparator()
        self._act(f"退出{title}", self.quit)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self._last = None
        state.changed.connect(self.update_state)
        self.update_state(state)

    def _act(self, text, signal, checkable=False):
        a = QAction(text, self.menu)
        a.setCheckable(checkable)
        if signal is not None:
            a.triggered.connect(lambda _=False: signal.emit())
        self.menu.addAction(a)
        return a

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_panel.emit()

    def set_checks(self, periodic=None, autonomous=None, pinned=None):
        for action, value in ((self.a_periodic, periodic), (self.a_autonomous, autonomous), (self.a_pin, pinned)):
            if value is not None:
                action.blockSignals(True)
                action.setChecked(value)
                action.blockSignals(False)

    def set_together(self, active, verb="一起听"):
        self.a_together.setText(f"正在{verb} · 显示面板" if active else "一起听 / 一起看…")

    def update_state(self, state):
        s = state.state
        if s != self._last:
            self._last = s
            self.icon.setIcon(tray_icon(s, self.character))
        self.icon.setToolTip(f"{self.character.name} · {state.status_text()}")
        self.a_stop.setEnabled(state.busy)

    def show(self):
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.icon.show()

    def hide(self):
        self.icon.hide()

    def notify_once(self, text):
        if self.icon.isVisible():
            self.icon.showMessage(self.title, text, QSystemTrayIcon.Information, 4000)
