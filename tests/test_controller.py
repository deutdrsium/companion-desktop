"""End-to-end panel flows against the synthetic backend (offscreen)."""

from unittest.mock import patch

from companion_desktop.ui.chat_view import ReplyBubble, UserBubble
from companion_desktop.ui.observe import ConsentDialog


def users(c):
    return [w for w in c.panel.chat.items if isinstance(w, UserBubble)]


def replies(c):
    return [w for w in c.panel.chat.items if isinstance(w, ReplyBubble)]


def test_history_strips_control_tags_and_keeps_user_text(ctl):
    ctl.backend.history.emit([{"role": "user", "text": "<hello>"},
                              {"role": "bot", "text": '你好。<soullink>{"e":1}</soullink>'}])
    assert users(ctl)[0].text == "<hello>"
    assert "&lt;hello&gt;" in users(ctl)[0].body.text()
    assert replies(ctl)[0].text == "你好。"


def test_send_think_speak_interrupt_idle(ctl):
    c = ctl
    c.panel.composer.set_text("你好")
    c.panel.composer._submit()
    text, turn = c.backend.chats[-1]
    assert text == "你好" and c.state.state == "thinking"
    assert c.panel.chat.thinking is not None
    assert c.panel.composer.button.mode == "stop"
    assert users(c)[-1].meta_label.text() == "发送中…"
    c.backend.turn_accepted.emit(turn)
    assert "已送达" in users(c)[-1].meta_label.text()
    c.backend.chat_done.emit(turn, "第一句。第二句。<wardrobe>x</wardrobe>")
    assert c.panel.chat.thinking is None
    from companion_desktop.ui.chat_view import ThinkingBubble
    assert not [t for t in c.panel.chat.body.findChildren(ThinkingBubble) if t.isVisibleTo(c.panel.chat.body)]
    bubble = replies(c)[-1]
    assert bubble.segments == ["第一句。", "第二句。"]
    assert c.backend.speech[-1][0] == ["第一句。", "第二句。"]
    assert c.state.state == "thinking"
    gen = c.backend.generation
    c.backend.audio.emit(gen, "a1", 0, "第一句。", b"RIFF", True)
    c.player.play_next()
    assert c.state.state == "speaking"
    assert bubble.mode == "speaking" and "第 1/2 句" in bubble.foot_label.text()
    assert c.subtitles.states[-1] == ("speaking", "第一句。")
    assert ("sync", "start", "a1") in c.backend.calls
    c.stop()
    assert c.state.state == "interrupted"
    assert bubble.mode == "interrupted" and "还有 1 句没读" in bubble.foot_label.text()
    # bug fix: the subtitle is cleared on stop
    assert c.subtitles.states[-1][0] == "interrupted"
    assert c.backend.generation > gen  # queued synthesis is dropped
    assert ("sync", "stop", "a1") in c.backend.calls
    c.state._settle.timeout.emit()
    assert c.state.state == "idle" and bubble.mode == "stopped"


def test_full_reading_returns_to_idle_and_subtitle_holds(ctl):
    c = ctl
    c.send("在吗")
    turn = c.backend.last_turn()
    c.backend.chat_done.emit(turn, "在的。")
    c.backend.audio.emit(c.backend.generation, "a", 0, "在的。", b"x", False)
    c.player.play_next()
    c.player.finish()
    assert c.state.state == "idle"
    assert c.subtitles.states[-1] == ("idle", None)
    assert replies(c)[-1].mode == "plain"


def test_failed_send_keeps_the_typed_text(ctl):
    c = ctl
    c.send("这句别丢")
    c.backend.chat_failed.emit(c.backend.last_turn(), "rejected", "")
    assert c.panel.composer.text() == "这句别丢"
    bubble = users(c)[-1]
    assert bubble.receipt == "failed" and bubble.retry_button.isVisibleTo(bubble)
    assert c.state.state == "listening"  # the restored draft counts as typing
    # error colour is the error token, never the accent/success colour
    from companion_desktop.ui import theme
    assert theme.ERROR_FG in bubble.meta_label.styleSheet()
    assert theme.C["accent"] not in bubble.meta_label.styleSheet()


def test_offline_failure_is_resent_once_after_reconnect(ctl):
    c = ctl
    c.send("那就这样吧")
    c.backend.chat_failed.emit(c.backend.last_turn(), "offline", "")
    c._link_changed("offline", 1, 2000)
    assert c.state.state == "offline" and c.panel.banner.isVisibleTo(c.panel)
    assert "第 1 次" in c.panel.banner.detail.text()
    c.panel.composer.set_text("草稿还在")
    assert c.panel.composer.blocked and not c.panel.composer.button.isEnabled()
    c.panel.composer._submit()
    assert c.panel.composer.text() == "草稿还在"
    n = len(c.backend.chats)
    c._link_changed("connected", 0, None)
    c._link_up("http://127.0.0.1:2")
    assert len(c.backend.chats) == n + 1 and c.backend.chats[-1][0] == "那就这样吧"
    c.backend.turn_accepted.emit(c.backend.last_turn())
    assert "已重发" in users(c)[-1].meta_label.text()
    assert not c.panel.banner.isVisibleTo(c.panel)


def test_cancel_while_thinking(ctl):
    c = ctl
    c.send("慢慢想")
    turn = c.backend.last_turn()
    c.stop()
    assert ("cancel", turn) in c.backend.calls
    assert c.panel.chat.thinking is None
    assert "可能仍会回复" in users(c)[-1].meta_label.text()
    c.backend.chat_done.emit(turn, "迟到的回复。")
    assert not replies(c)


def test_sending_while_thinking_waits_for_the_reply(ctl):
    c = ctl
    c.send("第一条")
    first = c.backend.last_turn()
    c.send("第二条")
    assert len(c.backend.chats) == 1
    assert users(c)[-1].meta_label.text() == "等她回答后再发"
    c.backend.chat_done.emit(first, "好。")
    assert len(c.backend.chats) == 1  # still reading the answer aloud
    c.backend.audio.emit(c.backend.generation, "a", 0, "好。", b"x", False)
    c.player.play_next()
    c.player.finish()
    assert c.backend.chats[-1][0] == "第二条"


def test_speech_failure_keeps_text_and_offers_replay(ctl):
    c = ctl
    c.send("说话")
    c.backend.chat_done.emit(c.backend.last_turn(), "一。二。")
    gen = c.backend.generation
    c.backend.generation += 1
    c.backend.speech_error.emit(gen, 0)
    b = replies(c)[-1]
    assert b.mode == "speech_failed" and b.foot_button.text() == "重新朗读"
    assert c.state.state == "idle"
    b.foot_button.click()
    assert c.backend.speech[-1][1] == 0


def test_typing_while_speaking_stops_her(ctl):
    c = ctl
    c.send("hi")
    c.backend.chat_done.emit(c.backend.last_turn(), "嗯。")
    c.backend.audio.emit(c.backend.generation, "a", 0, "嗯。", b"x", False)
    c.player.play_next()
    c.panel.composer.set_text("等")
    assert c.state.state == "interrupted"


def test_periodic_observation_needs_consent_first(ctl):
    c = ctl
    c.panel.show()
    c.set_periodic(True)
    dialogs = c.panel.findChildren(ConsentDialog)
    assert dialogs and not c.periodic
    assert not any(a == "start" for _, a in c.backend.screens[1:])
    dialogs[-1].yes.click()
    assert c.periodic and c.prefs.get("consent_periodic")
    gen, action = c.backend.screens[-1]
    assert action == "start"
    c.backend.screen_result.emit(gen, {"state": "enabled"})
    assert c.obs_timer.isActive()
    c._observe_periodic()
    assert c.state.state == "observing"
    c.backend.screen_result.emit(gen, {"state": "observed"})
    assert c.state.state == "idle" and "已观察" in c.panel.popover.last_chip.text
    with patch("companion_desktop.client.controller.desktop_locked", return_value=True):
        c._guard()
    assert not c.periodic and c.backend.screens[-1][1] == "stop"
    assert "锁屏" in c.panel.chat.items[-1].chip.text
    c.panel.hide()


def test_consent_declined_keeps_switch_off(ctl):
    c = ctl
    c.panel.show()
    c.set_periodic(True)
    c.panel.findChildren(ConsentDialog)[-1].no.click()
    assert not c.periodic and not c.panel.popover.periodic.isChecked()
    c.panel.hide()


def test_look_now_is_one_shot(ctl):
    c = ctl
    c.look_now()
    gen, action = c.backend.screens[-1]
    assert action == "start" and c.state.state == "observing"
    c.backend.screen_result.emit(gen, {"state": "enabled"})
    assert c.backend.screens[-1] == (gen, "observe")
    c.backend.screen_result.emit(gen, {"state": "observed"})
    assert c.state.state == "idle" and not c.periodic
    assert c.one_shot_stop.isActive() and not c.obs_timer.isActive()
    c.one_shot_stop.timeout.emit()
    assert c.backend.screens[-1][1] == "stop"


def test_autonomous_switch_stays_off_with_todays_server(ctl):
    c = ctl
    c.set_autonomous(True)
    assert not c.panel.popover.autonomous.isChecked()
    assert not c.panel.popover.autonomous.isEnabled()
