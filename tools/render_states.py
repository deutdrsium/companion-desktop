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
from fakes import FakeBackend, FakePlayer, FakeSubtitles  # noqa: E402

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
    m = c.panel.more_menu()
    m.adjustSize()
    save(m, "more-menu")
    _ = ConsentDialog


if __name__ == "__main__":
    main()
