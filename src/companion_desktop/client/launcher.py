"""Entry point: launch card first, connect in a worker thread, then the panel.

    python -m companion_desktop [--config PATH] [--steam-autostart] [--no-helpers]
"""

import argparse
import logging
from logging.handlers import RotatingFileHandler
import sys
import threading

from PyQt5.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QGuiApplication
from PyQt5.QtWidgets import QApplication, QMessageBox

from .. import __version__
from ..ui import theme
from ..ui.paint import Character
from ..ui.settings import Prefs
from ..ui.splash import LaunchCard
from ..ui.tray import Tray
from . import config as config_mod
from .connection import Link, TunnelError
from .legacy_backend import LegacyBackend, probe

log = logging.getLogger("companion_desktop")


def setup_logging(folder):
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / "client.log", maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class SingleInstance:
    def __init__(self, name):
        self.handle = None
        if sys.platform != "win32":
            self.ok = True
            return
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel = kernel
        ctypes.set_last_error(0)
        self.handle = kernel.CreateMutexW(None, False, name)
        self.ok = bool(self.handle) and ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS

    def release(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def explain(error):
    """Plain-language launch failure: (step, title, detail)."""
    if isinstance(error, config_mod.ConfigError):
        return 0, "本机设置还没准备好", str(error)
    if isinstance(error, TunnelError):
        if str(error) == "timeout":
            return 1, "暂时连不上服务器", "10 秒内没能建立加密连接。可能是网络不通，或服务器正在重启。"
        return 1, "暂时连不上服务器", "加密连接被拒绝了。请确认这台电脑的 SSH 登录仍然可用。"
    text = str(error)
    if "401" in text or "403" in text or "未授权" in text:
        return 1, "服务器没认出这台电脑", "桌面连接的授权失效了，需要重新做一次桌面连接设置。"
    return 1, "暂时连不上服务器", "服务器没有正常响应。可能正在重启，稍后再试。"


class Launch(QObject):
    connected = pyqtSignal(str, object)
    failed = pyqtSignal(object)
    step = pyqtSignal(int)

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.cfg = None
        self.card = None
        self.controller = None
        self.link = None
        self.subtitles = None
        self._key = None
        self.connected.connect(self._connected)
        self.failed.connect(self._failed)
        self.step.connect(lambda i: self.card.set_step(i))

    def begin(self, cfg, character):
        self.cfg, self.character = cfg, character
        self.card = LaunchCard(character, cfg["title"], quiet=self.args.steam_autostart)
        self.card.retry.connect(self.retry)
        self.card.quit.connect(QApplication.instance().quit)
        self.card.open_logs.connect(self._open_logs)
        self.card.center_on_screen()
        self.card.show()
        self.run()

    def _open_logs(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(config_mod.data_dir(self.cfg) / "logs")))

    def retry(self):
        try:
            self.cfg = config_mod.load(self.args.config)
        except config_mod.ConfigError as error:
            self._failed(error)
            return
        self.run()

    def run(self):
        self.card.set_step(0)
        cfg = self.cfg
        key_box = {}
        if self.link is not None:
            self.link.close()
        # created on the GUI thread so its timers live there; only start_blocking runs in the worker
        self.link = Link(cfg["server"], lambda base: probe(base, key_box["key"], cfg["server"]["api_path"]))
        link = self.link

        def work():
            try:
                config_mod.validate(cfg)
                key_box["key"] = self._key = config_mod.api_key(cfg)
                if not self.args.no_helpers:
                    from .services import start_helpers
                    try:
                        start_helpers(cfg.get("helpers"))
                    except Exception:
                        log.exception("helpers failed")
                self.step.emit(1)
                base, process = link.start_blocking()
                self.connected.emit(base, process)
            except Exception as error:
                log.warning("launch failed: %s", type(error).__name__)
                self.failed.emit(error)
        threading.Thread(target=work, daemon=True, name="launch").start()

    def _failed(self, error):
        step, title, detail = explain(error)
        self.card.fail(step, title, detail)
        self.card.show()
        if not self.args.steam_autostart:
            self.card.raise_()

    def _connected(self, base, process):
        from .audio import Player
        from .controller import Controller
        from .services import RhythmService, VoiceKeyService
        from .subtitle_server import SubtitleServer
        cfg = self.cfg
        self.link.adopt(base, process)
        self.card.set_step(2)
        folder = config_mod.data_dir(cfg)
        device = cfg["server"].get("device_session") is True
        prefs = Prefs()
        sub = cfg["subtitle"]
        if sub.get("enabled", True):
            try:
                token = sub.get("token_file") or str(folder / "subtitle-token")
                self.subtitles = SubtitleServer(token, int(sub.get("port", 18676)), self.character.name,
                                                cfg["character"].get("voice_font") or None)
                (folder / "subtitle-item-url").write_text(self.subtitles.item_url, encoding="utf-8")
            except Exception:
                log.exception("subtitle server unavailable")
                self.subtitles = None
        try:
            player = Player(local_levels=device)
        except Exception:
            log.exception("audio unavailable")
            player = None
        barge_in = None
        if device:
            from .device_backend import DeviceBackend
            backend = DeviceBackend(base, self._key, cfg, folder, tts=player is not None,
                                    subtitle=self.subtitles is not None)
            if player is not None and player.supports_pcm():
                backend.enable_pcm()
            if player is not None and cfg.get("headset_barge_in") is True:
                from .barge_in import BargeIn
                barge_in = BargeIn()
        else:
            backend = LegacyBackend(base, self._key, cfg)
        rhythm = RhythmService(cfg["rhythm_command"]) if cfg.get("rhythm_command") else None
        voice = VoiceKeyService(cfg["voice_key_command"]) if cfg.get("voice_key_command") else None
        c = Controller(self.character, cfg["title"], prefs, backend, self.link, player, self.subtitles,
                       rhythm, voice, folder / "logs", None, __version__, device=device, barge_in=barge_in)
        c.attach_tray(Tray(self.character, cfg["title"], c.state))
        self.controller = c
        geometry = prefs.store.value("geometry")
        if geometry is None or not c.panel.restoreGeometry(geometry):
            c.panel.default_size()
        c.panel.set_pinned(prefs.get("pinned"))
        backend.history.connect(self._ready)
        backend.history_failed.connect(self._ready)
        c.start()

    def _ready(self, *_):
        if self.card is None or not self.card.isVisible():
            return
        self.card.set_step(3)
        c = self.controller
        c.tray.show()
        if self.args.steam_autostart:
            c.panel.setAttribute(Qt.WA_ShowWithoutActivating, True)
        c.panel.show()
        self.card.hide()

    def shutdown(self):
        if self.link is not None:
            self.link.close()
        if self.subtitles is not None:
            self.subtitles.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="companion_desktop")
    parser.add_argument("--config")
    parser.add_argument("--steam-autostart", action="store_true")
    parser.add_argument("--no-helpers", action="store_true", help="do not start the configured helpers")
    args = parser.parse_args(argv)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as error:
        cfg = dict(config_mod.DEFAULTS)
        early_error = error
    else:
        early_error = None
    theme.set_voice_font(cfg["character"].get("voice_font"))
    app.setStyleSheet(theme.qss())
    app.setFont(theme.font("body"))
    app.setApplicationDisplayName(cfg["title"])
    character = Character.from_config(cfg["character"])
    lock = SingleInstance(cfg["instance_mutex"])
    if not lock.ok:
        if not args.steam_autostart:
            QMessageBox.information(None, cfg["title"], "已经在运行了，可以在托盘里找到它。")
        return 0
    if early_error is None:
        setup_logging(config_mod.data_dir(cfg) / "logs")
    launch = Launch(args)
    try:
        if early_error is not None:
            launch.cfg, launch.character = cfg, character
            launch.card = LaunchCard(character, cfg["title"])
            launch.card.retry.connect(launch.retry)
            launch.card.quit.connect(app.quit)
            launch.card.center_on_screen()
            launch.card.show()
            launch._failed(early_error)
        else:
            launch.begin(cfg, character)
        return app.exec_()
    finally:
        launch.shutdown()
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
