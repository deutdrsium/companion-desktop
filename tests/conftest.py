import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import companion_desktop  # noqa: E402,F401  (sets the Qt plugin path before PyQt5 loads)
import pytest  # noqa: E402
from PyQt5.QtCore import QSettings  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="session")
def qapp(tmp_path_factory):
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path_factory.mktemp("settings")))
    app = QApplication.instance() or QApplication([])
    from companion_desktop.ui import theme
    from companion_desktop.ui.paint import Animated
    Animated.reduced = True  # deterministic frames, no looping timers in tests
    app.setStyleSheet(theme.qss())
    app.setFont(theme.font("body"))
    yield app


@pytest.fixture
def prefs(qapp):
    from companion_desktop.ui.settings import Prefs
    p = Prefs("CompanionDesktopTest", "Panel")
    p.store.clear()
    return p


@pytest.fixture
def ctl(qapp, prefs):
    from companion_desktop.client.controller import Controller
    from companion_desktop.ui.paint import Character
    from fakes import FakeBackend, FakePlayer, FakeSubtitles
    c = Controller(Character("测试"), "测试面板", prefs, FakeBackend(), None, FakePlayer(), FakeSubtitles())
    c.panel.resize(440, 760)
    c.start()
    yield c
    c.quitting = True
    c.panel.close()
    c.panel.deleteLater()
