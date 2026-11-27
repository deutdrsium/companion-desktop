"""Render the implemented panel offscreen in each state (placeholder text, synthetic backend).

    python tools/render_states.py OUT_DIR [scale]

Drives the real Controller/SessionState with tests/fakes.py, so the images show exactly
what the client draws. No window is shown and nothing touches the network.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "renders")
SCALE = sys.argv[2] if len(sys.argv) > 2 else "1"
os.environ["QT_SCALE_FACTOR"] = SCALE

import companion_desktop  # noqa: E402,F401
from PyQt5.QtCore import QSettings  # noqa: E402
from PyQt5.QtGui import QFontDatabase  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(OUT / ".settings"))
app = QApplication([])
for f in [Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts/FZXSSJW.TTF",
          Path(r"C:\Windows\Fonts\msyh.ttc"), Path(r"C:\Windows\Fonts\msyhbd.ttc"),
          Path(r"C:\Windows\Fonts\NotoSerifSC-VF.ttf")]:
    if f.exists():
        QFontDatabase.addApplicationFont(str(f))

from companion_desktop.client.controller import Controller  # noqa: E402
from companion_desktop.ui import theme  # noqa: E402
from companion_desktop.ui.observe import ConsentDialog  # noqa: E402
from companion_desktop.ui.paint import Animated, Character  # noqa: E402
from companion_desktop.ui.settings import Prefs, SettingsWindow  # noqa: E402
from companion_desktop.ui.splash import LaunchCard  # noqa: E402
from companion_desktop.ui.panel import Panel  # noqa: E402
from companion_desktop.ui.session_state import SessionState  # noqa: E402
from companion_desktop.ui.together import Together  # noqa: E402
from companion_desktop.ui.tray import Tray  # noqa: E402
from fakes import FakeBackend, FakeMedia, FakePlayer, FakeSubtitles  # noqa: E402

Animated.reduced = True
app.setStyleSheet(theme.qss())
app.setFont(theme.font("body"))
OUT.mkdir(parents=True, exist_ok=True)
TAG = "" if SCALE == "1" else f"@{SCALE}x"
CHAR = Character("小伴", "伴")
HISTORY = [{"role": "user", "text": "今天的报告终于写完了。"},
           {"role": "bot", "text": "辛苦啦。最后那张对比图很清楚。"},
           {"role": "user", "text": "晚上想看会儿书。"},
           {"role": "bot", "text": "好呀。你看你的，有想聊的随时叫我。"}]


def save(w, name):
    QApplication.processEvents()
    w.grab().save(str(OUT / f"{name}{TAG}.png"))
    print("saved", name + TAG)


def controller(size=(440, 760)):
    prefs = Prefs("CompanionDesktopRender", "Panel")
    prefs.store.clear()
    c = Controller(CHAR, "桌面陪伴", prefs, FakeBackend(), None, FakePlayer(), FakeSubtitles())
    c.panel.resize(*size)
    c.panel.show()
    c.start()
    c.backend.history.emit(HISTORY)
    return c


def speaking(c, ask="你看我新养的这条鱼，它一直盯着我。",
             reply="真的诶，它在瞪你。可能是觉得你挡住它的光了。要不要给它起个名字？"):
    c.send(ask)
    t = c.backend.last_turn()
    c.backend.turn_accepted.emit(t)
    c.backend.chat_done.emit(t, reply)
    g = c.backend.generation
    for i, s in enumerate(c.panel.chat.items[-1].segments):
        c.backend.audio.emit(g, f"a{i}", i, s, b"x", False)
    c.player.play_next()
    c.player.finish()
    c.player.play_next()


def together(prefs, unavailable=None, size=(440, 760), media=None):
    state = SessionState()
    state.set_connection("connected")
    panel = Panel(CHAR, "桌面陪伴", state)
    panel.resize(*size)
    panel.show()
    panel.chat.load_history(HISTORY)
    media = media or FakeMedia()
    names = iter(["D:/音乐/晚风.mp3", "D:/音乐/晚风.lrc"])
    t = Together(None if unavailable else media, panel, prefs, state, CHAR.name, unavailable,
                 video_window=None, pick=lambda *_: next(names, None))
    return t, panel, media


def render_together(prefs):
    t, panel, media = together(prefs)
    t.open()
    t.picker.choose.click()
    t.picker.choose_text.click()
    save(panel, "20-together-picker-local")
    t.picker.source.buttons[1].click()
    media.system.append({"id": "s2", "source": "system_media", "label": "Microsoft.ZuneMusic_8wekyb3d8bbwe!App"})
    t.picker.set_sources(media.sources)
    t.picker._pick_system("s1")
    save(panel, "21-together-picker-system")
    t, panel, _ = together(prefs, "一起听/看还没开启：本机设置里的 shared_media_enabled 是关闭的。打开后重启面板。")
    t.open()
    save(panel, "22-together-flag-off")
    t, panel, _ = together(prefs)
    t.state.set_permissions({"avatar"})
    t.open()
    save(panel, "23-together-no-speak")

    t, panel, media = together(prefs)
    t.begin({"kind": "music", "source": "local_player", "path": "D:/音乐/晚风.mp3", "system_id": None})
    media.current_line = "晚风吹过山岗，把灯一盏盏点亮"
    media.push(title="晚风", position_ms=83_000, duration_ms=215_000)
    media.reaction.emit({"reaction_id": "r1", "text": "这段前奏好温柔。", "segments": []})
    save(panel, "24-together-music")
    t, panel, media = together(prefs)
    t.begin({"kind": "music", "source": "system_media", "path": None, "system_id": "s1"})
    media.push(title="晚风", artist="示例乐队", position_ms=None, duration_ms=None, status="paused")
    save(panel, "25-together-system-unknown-progress")
    t, panel, media = together(prefs, size=(360, 540))
    t.begin({"kind": "movie", "source": "local_player", "path": "D:/片/夜航.mp4", "system_id": None})
    media.push(title="夜航", position_ms=3_905_000, duration_ms=5_400_000)
    media.reaction.emit({"reaction_id": "r2", "text": "这里好安静。", "segments": []})
    save(panel, "26-together-movie-minimal")
    media.reaction_cancelled.emit("r2")
    save(panel, "27-together-movie-reaction-cancelled")


def main():
    c = controller()
    save(c.panel, "01-idle")
    c.panel.composer.entry.setFocus()
    c.panel.composer.set_text("你看我新养的这条鱼")
    c.panel.composer._focus(True)
    save(c.panel, "02-typing")
    c.panel.composer.set_text("")
    c.send("你看我新养的这条鱼，它一直盯着我。")
    c.backend.turn_accepted.emit(c.backend.last_turn())
    c.state.thinking_since -= 24
    c.state._emit()
    save(c.panel, "03-thinking-slow")

    c = controller()
    speaking(c)
    save(c.panel, "04-speaking")
    c.stop()
    c.panel.composer.set_text("等等，先别说")
    c.state._set_presence("interrupted")
    c.state._emit()
    save(c.panel, "05-interrupted")

    c = controller()
    c.send("那它就叫小橙吧")
    c.backend.chat_failed.emit(c.backend.last_turn(), "offline", "")
    c._link_changed("offline", 3, 8000)
    c.panel.composer.set_text("顺便问问，鱼一天喂几次？")
    save(c.panel, "07-offline")
    c._link_changed("connected", 0, None)
    c._link_up("http://127.0.0.1:2")
    c.backend.turn_accepted.emit(c.backend.last_turn())
    save(c.panel, "08-reconnected")

    c = controller()
    c.send("说点什么")
    c.backend.chat_done.emit(c.backend.last_turn(), "我在呢。今天过得怎么样？")
    g = c.backend.generation
    c.backend.generation += 1
    c.backend.speech_error.emit(g, 0)
    save(c.panel, "09-speech-failed")

    c = controller()
    c.look_now()
    save(c.panel, "10-observing-now")
    c.backend.screen_result.emit(c.obs_gen, {"state": "enabled"})
    c.backend.screen_result.emit(c.obs_gen, {"state": "observed"})
    c.panel.popover.open()
    save(c.panel, "11-observe-popover")
    c.panel.popover.hide()
    c.set_periodic(True)
    save(c.panel, "12-observe-consent")

    c = controller((360, 540))
    speaking(c)
    save(c.panel, "13-min-size-speaking")

    c.panel.state.set_connection("connecting")
    for name, page in (("settings-rhythm", 3), ("settings-subtitle", 2), ("settings-voice", 1)):
        w = SettingsWindow(CHAR, c.prefs, c.state, FakeSubtitles.item_url)
        w.show()
        w.show_page(page)
        if page == 3:
            w.rhythm.service = object()
            w.rhythm.loaded = True
            w.rhythm._done("load", {"private_debounce_min_ms": 600, "private_debounce_max_ms": 1500,
                                    "private_debounce_total_ms": 4000})
        save(w, name)
    card = LaunchCard(CHAR, "桌面陪伴")
    card.set_step(1)
    card.show()
    save(card, "launch-connecting")
    card.fail(1, "暂时连不上服务器", "10 秒内没能建立加密连接。可能是网络不通，或服务器正在重启。")
    save(card, "launch-failed")
    w = SettingsWindow(CHAR, c.prefs, c.state, FakeSubtitles.item_url)
    w.show()
    w.show_page(5)
    save(w, "settings-together")
    render_together(c.prefs)
    tray = Tray(CHAR, "桌面陪伴", c.state)
    tray.menu.adjustSize()
    save(tray.menu, "tray-menu")
    m = c.panel.more_menu()
    m.adjustSize()
    save(m, "more-menu")
    _ = ConsentDialog


if __name__ == "__main__":
    main()
