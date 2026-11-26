"""One settings window with a left nav. Replaces the debounce dialog and the voice-key prompt."""

import threading
import time

from PyQt5.QtCore import QObject, QRectF, QSettings, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QLinearGradient, QPainter, QPen
from PyQt5.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLineEdit, QSlider, QStackedWidget,
                             QVBoxLayout, QWidget)

from . import theme
from .paint import StateAvatar, Toggle
from .theme import C, STATE
from .widgets import button, hline, label, set_label_color

PAGES = ("外观", "声音", "字幕", "回复节奏", "观察", "连接")

# Reply-rhythm presets: (min wait, max wait, total cap) in ms.
RHYTHM_PRESETS = [("快", 300, 1000, 3000), ("适中", 600, 1500, 4000), ("从容", 1500, 3000, 8000)]
RHYTHM_KEYS = ("private_debounce_min_ms", "private_debounce_max_ms", "private_debounce_total_ms")


class Prefs(QObject):
    """Local UI preferences (QSettings). Everything else lives on the server."""

    changed = pyqtSignal(str, object)

    DEFAULTS = {"pinned": True, "close_to_tray": True, "voice_enabled": True, "volume": 100,
                "subtitle_style": "plate", "subtitle_size": 100, "subtitle_hold_ms": 1800,
                "subtitle_nameplate": True, "consent_periodic": False, "consent_autonomous": False,
                "tray_hint_shown": False}

    def __init__(self, org="CompanionDesktop", app="Panel"):
        super().__init__()
        self.store = QSettings(org, app)

    def get(self, key):
        default = self.DEFAULTS[key]
        return self.store.value(key, default, type=type(default))

    def set(self, key, value):
        self.store.setValue(key, value)
        self.changed.emit(key, value)


class _Worker(QObject):
    done = pyqtSignal(str, object)
    failed = pyqtSignal(str)

    def run(self, action, fn, *args):
        def work():
            try:
                result = fn(*args)
                self.done.emit(action, result)
            except Exception:
                self.failed.emit(action)
        threading.Thread(target=work, daemon=True).start()


def _row(title, widget, desc=None):
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    h = QHBoxLayout()
    h.addWidget(label(title, "body", bold=True))
    h.addStretch(1)
    h.addWidget(widget)
    v.addLayout(h)
    if desc:
        v.addWidget(label(desc, "caption", "ink3", wrap=True))
    return w


class Segmented(QWidget):
    picked = pyqtSignal(int)

    def __init__(self, names):
        super().__init__()
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("Segmented")
        self.setStyleSheet(f"QWidget#Segmented {{ background: {C['sunken']}; border-radius: 10px; }}")
        h = QHBoxLayout(self)
        h.setContentsMargins(3, 3, 3, 3)
        h.setSpacing(2)
        self.buttons = []
        for i, n in enumerate(names):
            b = button(n, "segment")
            b.clicked.connect(lambda _=False, i=i: (self.select(i), self.picked.emit(i)))
            h.addWidget(b)
            self.buttons.append(b)
        self.index = -1

    def select(self, index):
        self.index = index
        for i, b in enumerate(self.buttons):
            b.setProperty("on", "true" if i == index else "false")
            b.style().unpolish(b)
            b.style().polish(b)


class SubtitlePreview(QWidget):
    """Painted preview of the VTS subtitle item (no web engine needed)."""

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.style_name, self.size, self.nameplate = "plate", 100, True
        self.setFixedHeight(120)

    def set(self, style=None, size=None, nameplate=None):
        if style is not None:
            self.style_name = style
        if size is not None:
            self.size = size
        if nameplate is not None:
            self.nameplate = nameplate
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        g = QLinearGradient(0, 0, 0, r.height())
        g.setColorAt(0, QColor("#3A3E4E"))
        g.setColorAt(1, QColor("#1E1F28"))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(r, 12, 12)
        text = "这是一句字幕预览。"
        f = theme.font("message", voice=True)
        f.setPixelSize(max(10, int(20 * self.size / 100)))
        p.setFont(f)
        tw = p.fontMetrics().horizontalAdvance(text)
        th = p.fontMetrics().height()
        plate = QRectF((r.width() - tw) / 2 - 14, r.height() - th - 30, tw + 28, th + 14)
        if self.style_name == "plate":
            p.setBrush(QColor(43, 39, 34, 209))
            p.drawRoundedRect(plate, 10, 10)
            p.setBrush(QColor(255, 249, 238, 46))
            p.drawRoundedRect(QRectF(plate.left() + 14, plate.bottom() - 6, plate.width() - 28, 2), 1, 1)
            p.setBrush(QColor("#9CC7B4"))
            p.drawRoundedRect(QRectF(plate.left() + 14, plate.bottom() - 6, (plate.width() - 28) * 0.46, 2), 1, 1)
            p.setPen(QColor("#FFF9EE"))
        else:
            p.setPen(QPen(QColor("#2B2722"), 3))
            path_color = QColor("#FFFFFF")
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                p.setPen(QColor("#2B2722"))
                p.drawText(plate.translated(dx, dy - 3), Qt.AlignCenter, text)
            p.setPen(path_color)
        p.drawText(plate.translated(0, -3), Qt.AlignCenter, text)
        if self.nameplate:
            nf = theme.font("caption", bold=True)
            p.setFont(nf)
            nw = p.fontMetrics().horizontalAdvance(self.name) + 20
            nr = QRectF((r.width() - nw) / 2, plate.top() - 26, nw, 20)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(43, 39, 34, 209 if self.style_name == "plate" else 140))
            p.drawRoundedRect(nr, 10, 10)
            p.setPen(QColor("#CFE3D8"))
            p.drawText(nr, Qt.AlignCenter, self.name)


class VoiceKeyDialog(QDialog):
    """Password field; the key goes to the configured setup command over stdin and is not kept."""

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("配置声音密钥")
        self.setObjectName("SettingsWindow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(380)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 20, 22, 18)
        v.setSpacing(10)
        v.addWidget(label("配置声音密钥", "title"))
        v.addWidget(label("密钥只用于配置服务器，经加密连接一次性传过去，桌面不会保存。", "body", "ink2", wrap=True))
        self.field = QLineEdit()
        self.field.setEchoMode(QLineEdit.Password)
        self.field.setPlaceholderText("粘贴语音服务的 API Key")
        self.field.setAccessibleName("声音密钥")
        v.addWidget(self.field)
        self.status = label("", "caption", "ink3", wrap=True)
        v.addWidget(self.status)
        h = QHBoxLayout()
        h.addStretch(1)
        self.cancel = button("取消", "secondary")
        self.ok = button("保存到服务器", "primary")
        h.addWidget(self.cancel)
        h.addWidget(self.ok)
        v.addLayout(h)
        self.worker = _Worker()
        self.worker.done.connect(self._done)
        self.worker.failed.connect(lambda _a: self._done("submit", False))
        self.cancel.clicked.connect(self.reject)
        self.ok.clicked.connect(self._submit)

    def _submit(self):
        key = self.field.text().strip()
        if not key:
            self.status.setText("请先粘贴密钥。")
            return
        self.field.clear()
        self.ok.setEnabled(False)
        self.status.setText("正在保存…")
        set_label_color(self.status, "ink3")
        self.worker.run("submit", self.service.submit, key)
        del key

    def _done(self, _action, ok):
        self.ok.setEnabled(True)
        if ok:
            self.status.setText("服务器已保存。稍后发一句话试试声音。")
            set_label_color(self.status, "accent")
        else:
            self.status.setText("没能保存。请检查连接，或服务器上是否已有同名配置。")
            set_label_color(self.status, theme.ERROR_FG)


class RhythmPage(QWidget):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self._setting = True  # sliders emit while being built
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(14)
        v.addWidget(label("回复节奏", "title"))
        v.addWidget(label("你连着发几句时，她会等你说完再一起回。只影响桌面窗口，QQ 里不变。", "body", "ink2", wrap=True))
        self.presets = Segmented([f"{n} · {lo / 1000:g} 秒" for n, lo, _, _ in RHYTHM_PRESETS] + ["自定义"])
        self.presets.picked.connect(self._preset)
        row = QHBoxLayout()
        row.addWidget(self.presets)
        row.addStretch(1)
        v.addLayout(row)
        self.sliders = []
        self.custom = QWidget()
        cv = QVBoxLayout(self.custom)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(12)
        for title, desc, hi in (("至少等", "每发一句后至少等这么久，看你是不是还要说。", 5000),
                                ("最多等", "单句之后最长的等待。", 10000),
                                ("最多一共等", "连续输入时总等待不超过这个时间。", 30000)):
            box = QVBoxLayout()
            box.setSpacing(4)
            t = QHBoxLayout()
            t.addWidget(label(title, "body", bold=True))
            t.addStretch(1)
            value = label("", "body", "accent", bold=True)
            t.addWidget(value)
            box.addLayout(t)
            s = QSlider(Qt.Horizontal)
            s.setRange(100, hi)
            s.setSingleStep(100)
            s.setPageStep(500)
            s.setAccessibleName(title)
            s.valueChanged.connect(lambda val, lab=value: lab.setText(f"{val / 1000:.1f} 秒"))
            s.valueChanged.connect(self._slid)
            box.addWidget(s)
            box.addWidget(label(desc, "caption", "ink3"))
            cv.addLayout(box)
            self.sliders.append(s)
        v.addWidget(self.custom)
        self.skeleton = QWidget()
        sk = QVBoxLayout(self.skeleton)
        sk.setContentsMargins(0, 0, 0, 0)
        sk.setSpacing(16)
        for w in (220, 300, 260):
            bar = QWidget()
            bar.setFixedSize(w, 14)
            bar.setAttribute(Qt.WA_StyledBackground, True)
            bar.setStyleSheet(f"background: {C['sunken']}; border-radius: 7px;")
            sk.addWidget(bar)
        v.addWidget(self.skeleton)
        v.addStretch(1)
        self.status = label("", "caption", "ink3", wrap=True)
        v.addWidget(self.status)
        btns = QHBoxLayout()
        self.reset = button("恢复为角色默认", "ghost")
        self.save = button("保存", "primary")
        btns.addWidget(self.reset)
        btns.addStretch(1)
        btns.addWidget(self.save)
        v.addLayout(btns)
        self.worker = _Worker()
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.save.clicked.connect(self._save)
        self.reset.clicked.connect(lambda: self._run("reset", self.service.reset))
        self.loaded = False
        self._setting = False
        self._busy(True)
        if service is None:
            self.status.setText("这台电脑还没配置回复节奏的服务器命令（见 config/client.example.json）。")
            self.skeleton.hide()
            self.presets.setEnabled(False)

    def load(self):
        if self.service is not None and not self.loaded:
            self._run("load", self.service.load)

    def _busy(self, busy, text="正在读取…"):
        self.save.setEnabled(not busy and self.loaded)
        self.reset.setEnabled(not busy and self.loaded)
        self.presets.setEnabled(not busy)
        for s in self.sliders:
            s.setEnabled(not busy)
        if busy:
            self.status.setText(text)
            set_label_color(self.status, "ink3")
        self.skeleton.setVisible(busy and not self.loaded)
        self.custom.setVisible(self.loaded)

    def _run(self, action, fn, *args):
        self._busy(True, "正在读取…" if action == "load" else "正在保存…")
        self.worker.run(action, fn, *args)

    def values(self):
        return tuple(s.value() for s in self.sliders)

    def set_values(self, values):
        self._setting = True
        for s, val in zip(self.sliders, values):
            s.setValue(int(val))
        self._setting = False
        for i, (_, lo, hi, total) in enumerate(RHYTHM_PRESETS):
            if tuple(values) == (lo, hi, total):
                self.presets.select(i)
                break
        else:
            self.presets.select(len(RHYTHM_PRESETS))

    def _preset(self, i):
        if i < len(RHYTHM_PRESETS):
            _, lo, hi, total = RHYTHM_PRESETS[i]
            self.set_values((lo, hi, total))

    def _slid(self, _):
        if self._setting:
            return
        a, b, c = self.values()
        # keep min <= max <= total while dragging
        if b < a:
            self.sliders[1].setValue(a)
        if c < max(a, b):
            self.sliders[2].setValue(max(a, b))
        self.presets.select(len(RHYTHM_PRESETS))

    def _save(self):
        a, b, c = self.values()
        if not a <= b <= c:
            self.status.setText("请让“至少等”≤“最多等”≤“最多一共等”。")
            set_label_color(self.status, theme.ERROR_FG)
            return
        self._run("save", self.service.save, (a, b, c))

    def _done(self, action, data):
        self.loaded = True
        try:
            self.set_values(tuple(int(data[k]) for k in RHYTHM_KEYS))
        except (KeyError, TypeError, ValueError):
            self._failed(action)
            return
        self._busy(False)
        stamp = time.strftime("%H:%M")
        self.status.setText({"load": f"已从服务器读取 · {stamp}", "save": f"已保存 · {stamp}",
                             "reset": f"已恢复为角色默认 · {stamp}"}[action])
        set_label_color(self.status, "ink3")

    def _failed(self, _action):
        self._busy(False)
        self.save.setEnabled(self.loaded)
        self.reset.setEnabled(self.loaded)
        self.status.setText("读取或保存失败。请检查服务器连接，再试一次。")
        set_label_color(self.status, theme.ERROR_FG)


class SettingsWindow(QWidget):
    periodic_toggled = pyqtSignal(bool)
    autonomous_toggled = pyqtSignal(bool)
    look_now = pyqtSignal()
    reconnect = pyqtSignal()
    open_logs = pyqtSignal()
    copy_subtitle_url = pyqtSignal()

    def __init__(self, character, prefs, state, subtitle_url="", rhythm_service=None, voice_service=None,
                 version="0.2"):
        super().__init__()
        self.character, self.prefs, self.state = character, prefs, state
        self.voice_service = voice_service
        self.setObjectName("SettingsWindow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle("设置")
        self.resize(760, 560)
        self.setMinimumSize(680, 500)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        nav = QWidget()
        nav.setAttribute(Qt.WA_StyledBackground, True)
        nav.setObjectName("Nav")
        nav.setStyleSheet(f"QWidget#Nav {{ background: {C['sunken']}; }}")
        nav.setFixedWidth(190)
        nv = QVBoxLayout(nav)
        nv.setContentsMargins(12, 18, 12, 12)
        nv.setSpacing(2)
        head = QHBoxLayout()
        self.avatar = StateAvatar(character, state.state, 34)
        head.addWidget(self.avatar)
        head.addWidget(label("设置", "title"), 1)
        nv.addLayout(head)
        nv.addSpacing(10)
        self.nav_buttons = []
        for i, name in enumerate(PAGES):
            b = button(name, "nav")
            b.clicked.connect(lambda _=False, i=i: self.show_page(i))
            nv.addWidget(b)
            self.nav_buttons.append(b)
        nv.addStretch(1)
        self.version = label("", "caption", "ink3")
        self.version_text = f"版本 {version}"
        nv.addWidget(self.version)
        h.addWidget(nav)
        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)
        self.rhythm = RhythmPage(rhythm_service)
        self.subtitle_url = subtitle_url
        for build in (self._appearance, self._voice, self._subtitle, lambda: self.rhythm,
                      self._observe, self._connection):
            page = QWidget()
            pv = QVBoxLayout(page)
            pv.setContentsMargins(28, 24, 28, 20)
            pv.setSpacing(14)
            content = build()
            if isinstance(content, QWidget):
                pv.addWidget(content, 1)
            else:
                for item in content:
                    if isinstance(item, QWidget):
                        pv.addWidget(item)
                    else:
                        pv.addLayout(item)
                pv.addStretch(1)
            self.stack.addWidget(page)
        state.changed.connect(self._state)
        self._state(state)
        self.show_page(0)

    # ------------------------------------------------------------ pages
    def _toggle(self, key, name):
        t = Toggle(self.prefs.get(key), name=name)
        t.toggled.connect(lambda on: self.prefs.set(key, on))
        return t

    def _appearance(self):
        return [label("外观", "title"),
                _row("窗口置顶", self._toggle("pinned", "窗口置顶"), "面板总在其他窗口上面。"),
                _row("关闭窗口时缩到托盘", self._toggle("close_to_tray", "关闭窗口时缩到托盘"),
                     "退出请用托盘菜单或“更多”里的“退出”。")]

    def _voice(self):
        vol = QSlider(Qt.Horizontal)
        vol.setRange(0, 100)
        vol.setValue(self.prefs.get("volume"))
        vol.setFixedWidth(220)
        vol.setAccessibleName("音量")
        vol.valueChanged.connect(lambda v: self.prefs.set("volume", v))
        key = button("配置声音密钥…", "secondary")
        key.setEnabled(self.voice_service is not None)
        key.clicked.connect(self._voice_key)
        items = [label("声音", "title"),
                 _row("朗读回复", self._toggle("voice_enabled", "朗读回复"), "关掉后只显示文字，不再请求语音。"),
                 _row("音量", vol), hline(), _row("声音密钥", key, "首次配置或更换语音服务的密钥时使用。")]
        if self.voice_service is None:
            items.append(label("这台电脑还没配置声音密钥的服务器命令（见 config/client.example.json）。",
                               "caption", "ink3", wrap=True))
        return items

    def _voice_key(self):
        VoiceKeyDialog(self.voice_service, self).exec_()

    def _subtitle(self):
        self.preview = SubtitlePreview(self.character.name)
        self.preview.set(self.prefs.get("subtitle_style"), self.prefs.get("subtitle_size"),
                         self.prefs.get("subtitle_nameplate"))
        style = Segmented(["底板", "描边"])
        style.select(0 if self.prefs.get("subtitle_style") == "plate" else 1)
        style.picked.connect(lambda i: (self.prefs.set("subtitle_style", ("plate", "outline")[i]),
                                        self.preview.set(style=("plate", "outline")[i])))
        size = QSlider(Qt.Horizontal)
        size.setRange(60, 140)
        size.setSingleStep(10)
        size.setValue(self.prefs.get("subtitle_size"))
        size.setFixedWidth(200)
        size.setAccessibleName("字号")
        size_label = label(f"{size.value()}%", "body", "accent", bold=True)
        size.valueChanged.connect(lambda v: (size_label.setText(f"{v}%"), self.prefs.set("subtitle_size", v),
                                             self.preview.set(size=v)))
        size_row = QWidget()
        sh = QHBoxLayout(size_row)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.addWidget(size)
        sh.addWidget(size_label)
        hold = QSlider(Qt.Horizontal)
        hold.setRange(5, 50)
        hold.setValue(self.prefs.get("subtitle_hold_ms") // 100)
        hold.setFixedWidth(200)
        hold.setAccessibleName("说完后保留")
        hold_label = label(f"{hold.value() / 10:.1f} 秒后淡出", "body", "ink2")
        hold.valueChanged.connect(lambda v: (hold_label.setText(f"{v / 10:.1f} 秒后淡出"),
                                             self.prefs.set("subtitle_hold_ms", v * 100)))
        hold_row = QWidget()
        hh = QHBoxLayout(hold_row)
        hh.setContentsMargins(0, 0, 0, 0)
        hh.addWidget(hold)
        hh.addWidget(hold_label)
        plate = self._toggle("subtitle_nameplate", "显示名牌")
        plate.toggled.connect(lambda on: self.preview.set(nameplate=on))
        url = QLineEdit(self._masked(self.subtitle_url))
        url.setReadOnly(True)
        url.setAccessibleName("字幕地址（已遮住令牌）")
        copy = button("复制", "secondary", tip="复制完整地址")
        copy.clicked.connect(self.copy_subtitle_url)
        url_row = QHBoxLayout()
        url_row.addWidget(url, 1)
        url_row.addWidget(copy)
        return [label("字幕", "title"),
                label("面板里的当前句高亮总是开启；这里设置 VTube Studio 里的字幕道具。", "body", "ink2", wrap=True),
                self.preview, _row("样式", style), _row("字号", size_row), _row("说完后保留", hold_row),
                _row(f"显示“{self.character.name}”名牌", plate), hline(),
                label("VTube Studio 字幕地址（只在这台电脑有效，别分享）", "body", bold=True), url_row,
                label("在 VTS 添加 Web Item → 粘贴地址 → 透明背景 → 尺寸 1024 × 200。", "caption", "ink3", wrap=True)]

    @staticmethod
    def _masked(url):
        if not url:
            return "字幕服务未启动"
        head, _, rest = url.partition("://")
        host, _, _token = rest.partition("/")
        return f"{head}://{host}/••••••••/"

    def _observe(self):
        self.obs_periodic = Toggle(name="定期看看")
        self.obs_autonomous = Toggle(name="让她自己决定看一眼")
        self.obs_autonomous.setEnabled(False)
        self.obs_periodic.toggled.connect(self.periodic_toggled)
        self.obs_autonomous.toggled.connect(self.autonomous_toggled)
        now = button("现在看一下当前窗口", "secondary")
        now.clicked.connect(self.look_now)
        return [label("观察", "title"),
                label("只看当前活动窗口。每次启动都默认关闭；锁屏或 VTube Studio 退出时自动关闭。", "body", "ink2", wrap=True),
                _row("定期看看", self.obs_periodic, "约每 5 分钟一次。"),
                _row(f"让{self.character.name}自己决定看一眼", self.obs_autonomous,
                     "需要新版桌面会话，暂不可用。"),
                hline(), now]

    def set_observe_switches(self, periodic, autonomous):
        for t, v in ((self.obs_periodic, periodic), (self.obs_autonomous, autonomous)):
            t.blockSignals(True)
            t.setChecked(v)
            t.blockSignals(False)
            t.update()

    def _connection(self):
        self.conn_status = label("", "body", wrap=True)
        again = button("重新连接", "secondary")
        again.clicked.connect(self.reconnect)
        logs = button("打开日志文件夹", "ghost")
        logs.clicked.connect(self.open_logs)
        h = QHBoxLayout()
        h.addWidget(again)
        h.addWidget(logs)
        h.addStretch(1)
        return [label("连接", "title"),
                label("面板通过加密连接与服务器通信；断开后会自动重连。", "body", "ink2", wrap=True),
                self.conn_status, h]

    # ------------------------------------------------------------ behaviour
    def show_page(self, index):
        self.stack.setCurrentIndex(index)
        for i, b in enumerate(self.nav_buttons):
            b.setProperty("on", "true" if i == index else "false")
            b.style().unpolish(b)
            b.style().polish(b)
        if PAGES[index] == "回复节奏":
            self.rhythm.load()

    def _state(self, state):
        self.avatar.set_state(state.state)
        conn = {"connecting": "正在连接", "connected": "已连接", "offline": "已断开"}[state.connection]
        self.version.setText(f"{self.version_text} · {conn}")
        if state.connection == "offline":
            retry = state.retry_in_s
            text = state.status_text()
            if not state.connection_closed and state.attempt:
                text += f"（第 {state.attempt} 次" + (f"，{retry} 秒后）" if retry is not None else "）")
            color = theme.ERROR_FG
        elif state.connection == "connecting":
            text, color = "正在连接服务器…", STATE["connecting"]["fg"]
        else:
            text, color = "已连接。", "accent"
            if state.permissions is not None:
                speak = "已授权" if "speak" in state.permissions else "未授权"
                avatar = "已授权" if "avatar" in state.permissions else "未授权"
                text += f" 对话{speak}，模型控制{avatar}。"
        self.conn_status.setText(text)
        set_label_color(self.conn_status, color)


def copy_text(text):
    QApplication.clipboard().setText(text)
