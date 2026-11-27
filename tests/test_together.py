"""Listen / watch together UI, driven by a synthetic shared_media (offscreen, no audio, no network)."""

from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtWidgets import QWidget

from companion_desktop.ui.chat_view import ReactionBubble
from companion_desktop.ui.paint import Character
from companion_desktop.ui.panel import Panel
from companion_desktop.ui.session_state import SessionState
from companion_desktop.ui.settings import PAGES, SettingsWindow
from companion_desktop.ui.together import PRIVACY, Together, app_name, progress_text
from companion_desktop.ui.tray import Tray
from fakes import FakeMedia


class FakeVideo(QWidget):
    made = []

    def __init__(self, title):
        super().__init__()
        self.title, self.video, self.on_close = title, QWidget(self), None
        FakeVideo.made.append(self)


def make(prefs, media=None, unavailable=None, presented=None, connected=True, permissions=None):
    state = SessionState()
    if connected:
        state.set_connection("connected")
    if permissions is not None:
        state.set_permissions(permissions)
    panel = Panel(Character("测试"), "测试面板", state)
    panel.resize(440, 760)
    picks = []
    t = Together(media, panel, prefs, state, "测试", unavailable, presented, video_window=FakeVideo,
                 pick=lambda _title, _filter, done: done(picks.pop(0) if picks else None))
    return t, panel, picks


def shown(w, root):
    return w.isVisibleTo(root)


def cleanup(panel):
    panel.close()
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_flag_off_explains_why_in_picker(qapp, prefs):
    t, panel, _ = make(prefs, None, "一起听/看还没开启：本机设置里的 shared_media_enabled 是关闭的。")
    panel.show()
    panel.header.together.click()
    p = t.picker
    assert p.isVisible() and shown(p.blocked, p) and not shown(p.form, p)
    assert "shared_media_enabled" in p.blocked_text.text()
    assert not p.go.isEnabled()
    assert not t.begin({"kind": "music", "source": "local_player", "path": "a.mp3", "system_id": None})
    cleanup(panel)


def test_missing_speak_permission_and_offline_and_pref_off(qapp, prefs):
    media = FakeMedia()
    t, panel, _ = make(prefs, media, permissions={"avatar"})
    assert "speak" in t.reason() and "授权" in t.reason()
    t.state.set_permissions({"speak"})
    assert t.reason() is None
    t.state.set_connection("offline", 1, 3000)
    assert "连上服务器" in t.reason()
    t.state.set_connection("connected")
    prefs.set("together_enabled", False)
    assert "设置" in t.picker.blocked_text.text()
    assert t.reason(ignore_pref=True) is None
    cleanup(panel)


def test_controller_without_device_session_says_so(ctl):
    ctl.panel.show()
    ctl.panel.header.together.click()
    assert "device_session" in ctl.together.picker.blocked_text.text()
    ctl.open_settings(PAGES.index("一起听看"))
    assert "device_session" in ctl.settings.together_note.text()
    ctl.settings.close()


def test_local_music_flow(qapp, prefs):
    media = FakeMedia()
    t, panel, picks = make(prefs, media)
    panel.show()
    t.open()
    assert media.refreshed == 1 and t.picker.isVisible()
    assert not t.picker.go.isEnabled()          # no file chosen yet
    picks.extend(["C:/音乐/晚风.mp3", "C:/音乐/晚风.lrc"])
    t.picker.choose.click()
    t.picker.choose_text.click()
    assert t.picker.go.isEnabled() and t.picker.go.text() == "一起听"
    t.picker.go.click()
    assert media.loaded == ("C:/音乐/晚风.mp3", "C:/音乐/晚风.lrc")
    assert media.started == [("local_player", "music")]
    assert not t.picker.isVisible() and shown(t.card, panel)
    assert t.card.title.full == "正在准备…"
    assert media.local_player.player.calls == ["play"] and not FakeVideo.made
    media.current_line = "晚风吹过山岗"
    media.push()
    assert t.card.title.full == "晚风" and t.card.time.text() == "0:30 / 3:00"
    assert t.card.line.text() == "晚风吹过山岗" and shown(t.card.line, panel)
    assert t.card.bar.fraction == 30_000 / 180_000
    assert shown(t.card.play, panel) and t.card.play.toolTip() == "暂停"
    t.card.play.click()
    assert media.local_player.player.calls[-1] == "pause" and t.card.status.text() == "已暂停"
    t.card.halt.click()
    assert media.local_player.player.calls[-1] == "stop" and media.active
    assert panel.header.together.active
    t.card.close_button.click()
    assert not media.active and not shown(t.card, panel) and not panel.header.together.active
    assert prefs.get("together_source") == "local_player"
    panel.grab()
    cleanup(panel)


def test_bad_file_shows_inline_error(qapp, prefs):
    media = FakeMedia()
    t, panel, picks = make(prefs, media)
    panel.show()
    t.open()
    picks.append("C:/x.bad")
    t.picker.choose.click()
    t.picker.go.click()
    assert t.picker.isVisible() and "打不开" in t.picker.error.text() and not media.started
    cleanup(panel)


def test_system_media_shows_state_only_and_unknown_progress(qapp, prefs):
    media = FakeMedia()
    prefs.set("together_source", "system_media")
    t, panel, _ = make(prefs, media)
    panel.show()
    t.open()
    p = t.picker
    assert shown(p.system, p) and not shown(p.local, p)
    assert [b.text() for b in p.choices] == ["Spotify"]
    assert p.go.isEnabled()                      # a single source is preselected
    p.go.click()
    assert media.selected == "s1" and media.started == [("system_media", "music")]
    media.push(position_ms=None, duration_ms=None, artist="某乐队")
    c = t.card
    assert c.time.text() == "进度未知" and c.bar.fraction is None
    assert not shown(c.play, panel) and not shown(c.halt, panel)
    assert "在原来的软件里控制" in c.status.text() and c.source_label.text() == "Spotify"
    assert c.title.full == "晚风 — 某乐队"
    c.close_button.click()
    assert media.local_player.player.calls == []   # never touches another app's playback
    cleanup(panel)


def test_empty_system_list_message(qapp, prefs):
    media = FakeMedia()
    media.system = []
    prefs.set("together_source", "system_media")
    t, panel, _ = make(prefs, media)
    panel.show()
    t.open()
    assert shown(t.picker.empty, t.picker) and not t.picker.go.isEnabled()
    cleanup(panel)


def test_movie_is_minimal_and_reactions_are_small_bubbles(qapp, prefs):
    FakeVideo.made.clear()
    media = FakeMedia()
    current = {"id": None}
    t, panel, picks = make(prefs, media, presented=lambda rid: rid == current["id"])
    panel.show()
    t.open()
    t.picker.kind.buttons[1].click()
    picks.append("C:/片/夜航.mp4")
    t.picker.choose.click()
    assert t.picker.go.text() == "一起看" and t.picker.choose_text.text() == "加字幕（可选）"
    t.picker.go.click()
    assert media.started == [("local_player", "movie")]
    assert len(FakeVideo.made) == 1 and media.local_player.player.output is FakeVideo.made[0].video
    assert t.card.collapsed and not shown(t.card.detail, panel) and shown(t.card.compact_title, panel)
    media.push(title="夜航", position_ms=65_000, duration_ms=5_400_000)
    assert t.card.compact_time.text() == "1:05 / 1:30:00"
    before = len(panel.chat.items)
    current["id"] = "r1"
    media.reaction.emit({"reaction_id": "r1", "text": "这里好安静。", "segments": []})
    bubble = panel.chat.items[-1]
    assert isinstance(bubble, ReactionBubble) and bubble.meta.text().startswith("一起看")
    assert bubble.body.width() < panel.chat.viewport().width() * 0.7
    media.reaction.emit({"reaction_id": "r2", "text": "没被播出的", "segments": []})
    assert len(panel.chat.items) == before + 1      # not presented by the controller -> not shown
    # nothing pops up over the video
    assert not t.picker.isVisible() and not panel.popover.isVisible()
    media.reaction_cancelled.emit("r1")
    assert bubble not in panel.chat.items and len(panel.chat.items) == before
    t.card.fold.click()
    assert shown(t.card.detail, panel)
    FakeVideo.made[0].on_close()                     # closing the video window ends sharing
    assert not media.active and not shown(t.card, panel)
    assert media.local_player.player.calls[-1] == "stop"
    cleanup(panel)


def test_errors_flash_and_end_cleanly(qapp, prefs):
    media = FakeMedia()
    t, panel, picks = make(prefs, media)
    panel.show()
    t.open()
    picks.append("C:/a.mp3")
    t.picker.choose.click()
    t.picker.go.click()
    media.push()
    media.reaction.emit({"reaction_id": "r1", "text": "好听。", "segments": []})
    media.stop()
    media.error.emit("connection_lost")
    assert not shown(t.card, panel)
    assert panel.toast.isVisible() and "断了" in panel.toast.text
    assert not any(isinstance(i, ReactionBubble) for i in panel.chat.items)
    cleanup(panel)


def test_start_refused_keeps_picker_open(qapp, prefs):
    media = FakeMedia(start_ok=False)
    t, panel, picks = make(prefs, media)
    panel.show()
    t.open()
    picks.append("C:/a.mp3")
    t.picker.choose.click()
    t.picker.go.click()
    assert t.picker.isVisible() and t.picker.error.text() and not shown(t.card, panel)
    cleanup(panel)


def test_turning_off_in_settings_ends_session(qapp, prefs):
    media = FakeMedia()
    t, panel, picks = make(prefs, media)
    t.begin({"kind": "music", "source": "local_player", "path": "C:/a.mp3", "system_id": None})
    assert media.active
    prefs.set("together_enabled", False)
    assert not media.active and not t.active
    cleanup(panel)


def test_tray_and_settings_entries(ctl):
    tray = Tray(ctl.character, ctl.title, ctl.state)
    ctl.attach_tray(tray)
    tray.a_together.trigger()
    assert ctl.together.picker.isVisible()
    ctl.together.active = True
    ctl._together_active(True)
    assert tray.a_together.text().startswith("正在一起听")
    w = SettingsWindow(ctl.character, ctl.prefs, ctl.state)
    w.show_page(PAGES.index("一起听看"))
    texts = [lab.text() for lab in w.stack.currentWidget().findChildren(type(w.version))]
    assert PRIVACY in texts and "一起听看" in texts
    w.together_source.buttons[1].click()
    assert ctl.prefs.get("together_source") == "system_media"
    assert PAGES[-1] == "连接"
    tray.hide()


def test_helpers():
    assert progress_text({"position_ms": None}) == "进度未知"
    assert progress_text({"position_ms": 5_000, "duration_ms": None}) == "0:05"
    assert app_name("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic") == "ZuneMusic"
    assert app_name("Spotify.exe") == "Spotify" and app_name("") == "未知软件"


def test_binds_to_real_shared_media(qapp, prefs, monkeypatch, tmp_path):
    import asyncio

    from companion_desktop.client import shared_media
    from test_shared_media import REF, LocalSource, MediaHost, settle

    class Source(LocalSource):
        def load(self, path):
            self.set_item(position_ms=0, status="paused")

        def play(self):
            self.set_item(status="playing")

        def pause(self):
            self.set_item(status="paused")

        def stop(self):
            self.set_item(status="paused", position_ms=0)

    monkeypatch.setattr(shared_media, "LocalPlayer", Source)
    lyrics = tmp_path / "fixture.lrc"
    lyrics.write_text("[00:03]First words\n[00:08]\n[00:10]Last words", encoding="utf-8")
    media_file = tmp_path / "fixture.mp3"
    media_file.write_bytes(b"")

    async def run():
        host = MediaHost()
        media = shared_media.SharedMedia(asyncio.get_running_loop(), host, lambda: REF, clock=lambda: 0)
        media.can_present = lambda: True
        t, panel, _ = make(prefs, media)
        panel.show()
        assert t.begin({"kind": "music", "source": "local_player", "path": str(media_file),
                        "timed_text": str(lyrics), "system_id": None})
        await settle()
        assert t.active and shown(t.card, panel)
        assert t.card.title.full == "First" and t.card.time.text() == "0:00 / 1:30"
        bubbles = [i for i in panel.chat.items if isinstance(i, ReactionBubble)]
        assert [b.text for b in bubbles] == ["Listen!"]
        media.local_player.set_item(position_ms=3500)
        media._tick()
        assert bubbles[0] not in panel.chat.items          # the seek cancelled it
        assert t.card.line.text() == "First words"
        t.card.close_button.click()
        assert not media.active and not shown(t.card, panel)
        await settle()
        assert host.calls[-1][0] == "stop"
        await media.close()
        cleanup(panel)

    asyncio.run(run())


def test_deleting_panel_mid_film_stops_player_and_disconnects(qapp, prefs):
    from PyQt5 import sip
    FakeVideo.made.clear()
    media = FakeMedia()
    t, panel, _ = make(prefs, media)
    panel.show()
    assert t.begin({"kind": "movie", "source": "local_player", "path": "C:/片/夜航.mp4", "system_id": None})
    media.push(title="夜航")
    video = FakeVideo.made[0]
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert sip.isdeleted(panel)
    assert media.local_player.player.calls[-1] == "stop" and not media.active
    assert media.local_player.player.output is None and t.video is None
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert sip.isdeleted(video)
    for signal in (media.state_changed, media.item_changed, media.sources_changed, media.reaction,
                   media.reaction_cancelled, media.error):
        assert media.receivers(signal) == 0
    calls = []
    t._sync = t._item = t._reaction = lambda *a: calls.append(a)
    media.push(title="later")
    media.reaction.emit({"reaction_id": "late", "text": "x", "segments": []})
    media.error.emit("connection_lost")
    assert calls == []


def _together_with_bubble(prefs):
    media = FakeMedia()
    t, panel, _ = make(prefs, media)
    panel.show()
    t.begin({"kind": "music", "source": "local_player", "path": "C:/a.mp3", "system_id": None})
    media.reaction.emit({"reaction_id": "r1", "text": "好听。", "segments": []})
    bubble = t.reactions["r1"]
    assert bubble in panel.chat.items
    return t, panel, media, bubble


def test_cancel_from_shared_media_first_then_controller(qapp, prefs):
    t, panel, media, bubble = _together_with_bubble(prefs)
    media.reaction_cancelled.emit("r1")
    assert bubble not in panel.chat.items
    t.cancel_reaction("r1")                      # the controller's later notice is a no-op
    assert bubble not in panel.chat.items and t.cancelled == ["r1"]
    media.reaction.emit({"reaction_id": "r1", "text": "好听。", "segments": []})
    assert not any(isinstance(i, ReactionBubble) for i in panel.chat.items)
    cleanup(panel)


def test_cancel_from_controller_first_without_shared_media_signal(qapp, prefs):
    t, panel, media, bubble = _together_with_bubble(prefs)
    t.cancel_reaction("r1")                      # presentation tick saw the lyric boundary first
    assert bubble not in panel.chat.items
    media.reaction_cancelled.emit("r1")
    assert t.cancelled == ["r1"]
    cleanup(panel)


def test_cancel_before_show_ignores_late_reaction(qapp, prefs):
    media = FakeMedia()
    t, panel, _ = make(prefs, media)
    t.begin({"kind": "music", "source": "local_player", "path": "C:/a.mp3", "system_id": None})
    t.cancel_reaction("r9")
    media.reaction.emit({"reaction_id": "r9", "text": "迟到的", "segments": []})
    assert not any(isinstance(i, ReactionBubble) for i in panel.chat.items)
    cleanup(panel)


def test_controller_cancel_path_reaches_the_panel(ctl):
    ctl._cancel_media_reaction("r1")
    assert "r1" in ctl.together.cancelled


def test_file_dialog_does_not_block_timers(qapp, prefs):
    from PyQt5.QtCore import QEventLoop, QTimer
    media = FakeMedia()
    state = SessionState()
    state.set_connection("connected")
    panel = Panel(Character("测试"), "测试面板", state)
    t = Together(media, panel, prefs, state, "测试", video_window=FakeVideo)   # real file dialog
    panel.show()
    t.open()
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    t.picker.choose.click()                      # returns at once; the dialog stays open
    dialog = t.picker.dialog
    assert dialog is not None and dialog.isVisible()
    loop = QEventLoop()
    QTimer.singleShot(200, loop.quit)
    loop.exec_()
    assert len(ticks) >= 5 and dialog.isVisible()
    dialog.fileSelected.emit("C:/音乐/晚风.mp3")
    assert t.picker.path == "C:/音乐/晚风.mp3" and t.picker.go.isEnabled()
    dialog.close()
    timer.stop()
    cleanup(panel)


def test_privacy_line_lists_what_is_sent():
    for word in ("标题", "音乐还是电影", "播放来源", "播放或暂停", "进度", "已去掉网址", "声音大小的数值", "歌手名"):
        assert word in PRIVACY
