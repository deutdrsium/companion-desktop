"""Composer behaviour and offscreen rendering of every panel state."""

import sys

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QKeyEvent

from companion_desktop.ui.composer import Composer
from companion_desktop.ui.paint import Character, StateAvatar, Toggle, tray_icon
from companion_desktop.ui.settings import PAGES, SettingsWindow
from companion_desktop.ui.splash import LaunchCard
from companion_desktop.ui.theme import STATE


def key(widget, k, mod=Qt.NoModifier, text=""):
    widget.keyPressEvent(QKeyEvent(QEvent.KeyPress, k, mod, text))


def test_enter_sends_shift_enter_breaks_escape_stops(qapp):
    c = Composer("测试")
    sent, stops, typing = [], [], []
    c.send.connect(sent.append)
    c.stop.connect(lambda: stops.append(1))
    c.typing.connect(typing.append)
    c.set_text("第一行")
    assert typing == [True]
    key(c.entry, Qt.Key_Return, Qt.ShiftModifier)
    c.entry.insertPlainText("第二行")
    assert c.text() == "第一行\n第二行"
    key(c.entry, Qt.Key_Return)
    assert sent == ["第一行\n第二行"] and c.text() == ""
    assert typing[-1] is False
    key(c.entry, Qt.Key_Escape)
    assert stops == [1]


def test_enter_during_ime_composition_does_not_send(qapp):
    c = Composer()
    sent = []
    c.send.connect(sent.append)
    c.set_text("ni")
    c.entry.composing = True
    key(c.entry, Qt.Key_Return)
    assert sent == []


def test_composer_grows_to_four_lines_and_blocks_when_offline(qapp):
    c = Composer()
    c.resize(400, 200)
    one = c.entry.height()
    c.set_text("1\n2\n3\n4\n5\n6")
    c._grow()
    four = c.entry.height()
    assert four > one * 2.5
    c.set_text("1\n2\n3\n4\n5\n6\n7\n8")
    c._grow()
    assert c.entry.height() == four
    c.set_blocked(True)
    assert not c.button.isEnabled() and c.entry.isEnabled()
    c.set_mode("stop")
    assert c.button.isEnabled()


def test_restore_draft_keeps_newer_typing(qapp):
    c = Composer()
    c.set_text("后来写的")
    c.restore_draft("没发出去的")
    assert c.text() == "没发出去的\n后来写的"


def _render(w):
    img = w.grab().toImage()
    assert not img.isNull() and img.width() > 0
    return img


def test_every_state_renders(ctl):
    c = ctl
    c.panel.show()
    for state in ("connecting", "idle", "listening", "thinking", "speaking", "interrupted", "offline", "observing"):
        if state == "connecting":
            c.state.set_connection("connecting")
        elif state == "offline":
            c.state.set_connection("offline", 3, 8000)
        else:
            c.state.set_connection("connected")
            c.state.set_observe(capturing=state == "observing")
            if state not in ("observing",):
                c.state._set_presence(state)
                c.state._emit()
        assert c.panel.header.avatar.state == state
        assert c.panel.header.status.text()
        assert c.panel.header.accessibleDescription() == c.state.status_text()
        _render(c.panel)
    assert c.panel.banner.isVisibleTo(c.panel) is False  # last state (observing) is online
    c.panel.hide()


def test_accessible_names(ctl):
    p = ctl.panel
    for b in (p.header.eye, p.header.pin, p.header.more, p.composer.button):
        assert b.accessibleName()
    assert STATE["offline"]["fg"] != STATE["idle"]["fg"]


def test_painted_controls_need_no_svg(qapp):
    for st in STATE:
        a = StateAvatar(Character("测"), st, 46)
        _render(a)
        assert not tray_icon(st, Character("测")).isNull()
    t = Toggle(True)
    _render(t)
    assert "PyQt5.QtSvg" not in sys.modules


def test_settings_and_launch_card_render(qapp, prefs):
    from companion_desktop.ui.session_state import SessionState
    s = SessionState()
    w = SettingsWindow(Character("测试"), prefs, s, "http://127.0.0.1:18676/" + "a" * 48 + "/")
    for i in range(len(PAGES)):
        w.show_page(i)
        _render(w)
    from PyQt5.QtWidgets import QLineEdit
    urls = [e.text() for e in w.findChildren(QLineEdit)]
    assert any("••••" in u for u in urls) and not any("a" * 48 in u for u in urls)
    card = LaunchCard(Character("测试"), "测试")
    card.set_step(2)
    _render(card)
    card.fail(1, "暂时连不上服务器", "原因")
    assert card.buttons.isVisibleTo(card)
    _render(card)
