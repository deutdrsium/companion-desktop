"""Listen / watch together: the entry popover, the now-playing card and Ling's short remarks.

Binds to ``Controller.shared_media`` (see README, "Shared media"). Pure view plus the small
amount of glue that turns user choices into ``load_local`` / ``select_system`` / ``start``.
The video of a local film plays in its own window; nothing here is drawn over it.
"""

from pathlib import Path

from PyQt5 import sip
from PyQt5.QtCore import QObject, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFontMetrics, QPainter
from PyQt5.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import theme
from .observe import Layer
from .paint import IconButton
from .settings import Segmented, together_privacy
from .theme import C, RADIUS, STATE
from .widgets import Card, button, label, set_label_color, shadow

KIND = {"music": "音乐", "movie": "电影"}
VERB = {"music": "一起听", "movie": "一起看"}
SOURCE = {"local_player": "桌面播放器", "system_media": "正在播放的软件"}
SOURCES = ("local_player", "system_media")
KINDS = ("music", "movie")
MEDIA_FILTER = "音频或视频 (*.mp3 *.flac *.m4a *.aac *.ogg *.wav *.mp4 *.mkv *.avi *.mov *.webm *.wmv);;所有文件 (*)"
TEXT_FILTER = "字幕或歌词 (*.srt *.lrc)"

# error reason -> what the owner reads
ERRORS = {
    "unauthorized": "这台设备没有对话授权，一起听/看已结束。",
    "no_media": "还没选好要播放的内容。",
    "system_media_unavailable": "读不到 Windows 正在播放的软件。",
    "disabled": "主机没有开启一起听/看。",
    "stale_session": "桌面会话已更换，一起听/看已结束。",
    "unsupported_protocol": "主机版本不支持一起听/看。",
    "busy": "主机那边已经有一段一起听/看在进行。",
    "connection_lost": "和服务器的连接断了，一起听/看已结束。",
}


def clock(ms):
    s = max(0, int(ms or 0) // 1000)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def progress_text(item):
    pos, dur = item.get("position_ms"), item.get("duration_ms")
    if pos is None:
        return "进度未知"
    return f"{clock(pos)} / {clock(dur)}" if dur else clock(pos)


class Elided(QLabel):
    """Single-line label that elides to its width; the full text stays in the tooltip."""

    def __init__(self, text="", kind="body", color="ink", bold=None):
        super().__init__()
        self.full = ""
        self.setStyleSheet(f"color: {C.get(color, color)}; background: transparent;")
        self.setFont(theme.font(kind, bold=bold))
        self.setMinimumWidth(40)
        self.set_text(text)

    def set_text(self, text):
        self.full = text or ""
        self.setToolTip(self.full)
        self.setAccessibleName(self.full)
        self._elide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        super().setText(QFontMetrics(self.font()).elidedText(self.full, Qt.ElideRight, max(40, self.width())))


class Progress(QWidget):
    """Thin painted progress line; a hatched track when the position is unknown."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fraction = None
        self.setFixedHeight(4)

    def set(self, fraction):
        self.fraction = None if fraction is None else max(0.0, min(1.0, fraction))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        r = QRectF(self.rect())
        p.setBrush(QColor(C["line"]))
        p.drawRoundedRect(r, 2, 2)
        if self.fraction is not None:
            p.setBrush(QColor(C["accent"]))
            p.drawRoundedRect(QRectF(r.left(), r.top(), max(r.height(), r.width() * self.fraction), r.height()), 2, 2)


class NowPlayingCard(Card):
    """Title, kind, source, progress and the current line. Collapses to one row for films."""

    play_pause = pyqtSignal()
    stop = pyqtSignal()
    end = pyqtSignal()
    collapsed_changed = pyqtSignal(bool)

    def __init__(self, name="她", parent=None):
        super().__init__(radius=RADIUS["md"], parent=parent)
        self.name = name
        self.collapsed = False
        self.source = None
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 9, 8, 10)
        v.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.kind = label("", "caption", STATE["speaking"]["fg"], bold=True)
        top.addWidget(self.kind)
        self.compact_title = Elided("", "label")
        top.addWidget(self.compact_title, 1)
        self.source_label = label("", "caption", "ink3")
        top.addWidget(self.source_label)
        self.spacer = QWidget()
        top.addWidget(self.spacer, 1)
        self.compact_time = label("", "caption", "ink3")
        top.addWidget(self.compact_time)
        self.fold = IconButton("collapse", "收起", size=28)
        self.fold.clicked.connect(lambda: self.set_collapsed(not self.collapsed, emit=True))
        self.close_button = IconButton("close", "结束", size=28)
        self.close_button.clicked.connect(self.end)
        top.addWidget(self.fold)
        top.addWidget(self.close_button)
        v.addLayout(top)
        self.detail = QWidget()
        dv = QVBoxLayout(self.detail)
        dv.setContentsMargins(0, 0, 6, 0)
        dv.setSpacing(6)
        self.title = Elided("", "body", bold=True)
        dv.addWidget(self.title)
        pr = QHBoxLayout()
        pr.setSpacing(10)
        self.bar = Progress()
        pr.addWidget(self.bar, 1)
        self.time = label("", "caption", "ink3")
        pr.addWidget(self.time)
        dv.addLayout(pr)
        cr = QHBoxLayout()
        cr.setSpacing(4)
        self.play = IconButton("pause", "暂停", size=30)
        self.play.clicked.connect(self.play_pause)
        self.halt = IconButton("stop", "停止播放", size=30)
        self.halt.clicked.connect(self.stop)
        cr.addWidget(self.play)
        cr.addWidget(self.halt)
        self.status = label("", "caption", "ink3")
        cr.addSpacing(4)
        cr.addWidget(self.status, 1)
        dv.addLayout(cr)
        self.line = label("", "caption", "ink3", wrap=True)
        self.line.setFont(theme.font("caption", voice=True))
        dv.addWidget(self.line)
        v.addWidget(self.detail)
        self.compact_bar = Progress()
        self.compact_bar.setFixedHeight(3)
        v.addWidget(self.compact_bar)
        self.setAccessibleName("正在一起听/看")
        self.set_collapsed(False)

    def set_collapsed(self, collapsed, emit=False):
        self.collapsed = collapsed
        self.detail.setVisible(not collapsed)
        self.source_label.setVisible(not collapsed)
        self.spacer.setVisible(not collapsed)
        for w in (self.compact_title, self.compact_time, self.compact_bar):
            w.setVisible(collapsed)
        self.fold.set_look("expand" if collapsed else "collapse")
        tip = "展开" if collapsed else "收起"
        self.fold.setToolTip(tip)
        self.fold.setAccessibleName(tip)
        if emit:
            self.collapsed_changed.emit(collapsed)

    def preparing(self, kind, source, source_label=None):
        self.source = source
        self.kind.setText(f"{VERB[kind]} · {KIND[kind]}")
        self.source_label.setText(source_label or SOURCE[source])
        self.close_button.setToolTip(f"结束{VERB[kind]}")
        self.close_button.setAccessibleName(f"结束{VERB[kind]}")
        for t in (self.title, self.compact_title):
            t.set_text("正在准备…")
        for b in (self.bar, self.compact_bar):
            b.set(None)
        self.time.setText("")
        self.compact_time.setText("")
        self.line.hide()
        self._controls(source, None)

    def set_item(self, item, line=None):
        kind, source = item.get("kind") or "music", item.get("source") or self.source
        self.source = source
        self.kind.setText(f"{VERB[kind]} · {KIND[kind]}")
        title = item.get("title") or "媒体"
        if item.get("artist"):
            title += " — " + item["artist"]
        for t in (self.title, self.compact_title):
            t.set_text(title)
        pos, dur = item.get("position_ms"), item.get("duration_ms")
        fraction = pos / dur if pos is not None and dur else None
        for b in (self.bar, self.compact_bar):
            b.set(fraction)
        text = progress_text(item)
        self.time.setText(text)
        self.compact_time.setText("" if text == "进度未知" else text)
        self.line.setText(line or "")
        self.line.setVisible(bool(line))
        self._controls(source, item.get("status"))
        self.setAccessibleDescription(f"{title}，{text}")

    def _controls(self, source, status):
        local = source == "local_player"
        self.play.setVisible(local)
        self.halt.setVisible(local)
        playing = status == "playing"
        self.play.set_look("pause" if playing else "play")
        tip = "暂停" if playing else "播放"
        self.play.setToolTip(tip)
        self.play.setAccessibleName(tip)
        if status is None:
            text = "正在准备…"
        elif local:
            text = "" if playing else "已暂停"
        else:
            text = ("正在播放" if playing else "已暂停") + " · 在原来的软件里控制播放"
        self.status.setText(text)


class TogetherPicker(Layer):
    """Popover under the header button: what to share, and from where."""

    start = pyqtSignal(object)        # {"kind", "source", "path", "timed_text", "system_id"}
    refresh = pyqtSignal()

    def __init__(self, parent, name="她", anchor=None, pick=None):
        super().__init__(parent)
        self.anchor = anchor
        self.pick = pick or self._pick
        self.dialog = None
        self.path = self.timed_text = self.system_id = None
        self.sources = []
        card = Card(parent=self)
        self.card = card
        card.setFixedWidth(340)
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 14)
        v.setSpacing(10)
        v.addWidget(label("一起听 / 一起看", "title"))
        self.intro = label(f"{name}陪你听歌或看电影，偶尔轻声说一两句。播放仍由你控制。", "caption", "ink3", wrap=True)
        v.addWidget(self.intro)
        self.blocked = QWidget()
        self.blocked.setObjectName("Blocked")
        self.blocked.setAttribute(Qt.WA_StyledBackground, True)
        st = STATE["interrupted"]
        self.blocked.setStyleSheet(f"QWidget#Blocked {{ background: {st['soft']}; border-radius: {RADIUS['sm']}px; }}")
        bv = QVBoxLayout(self.blocked)
        bv.setContentsMargins(12, 9, 12, 9)
        bv.setSpacing(2)
        self.blocked_title = label("现在还不能开始", "label", st["fg"], bold=True)
        self.blocked_text = label("", "caption", st["fg"], wrap=True)
        bv.addWidget(self.blocked_title)
        bv.addWidget(self.blocked_text)
        v.addWidget(self.blocked)
        self.form = QWidget()
        fv = QVBoxLayout(self.form)
        fv.setContentsMargins(0, 0, 0, 0)
        fv.setSpacing(10)
        self.kind = Segmented(["一起听 · 音乐", "一起看 · 电影"])
        self.kind.select(0)
        self.source = Segmented(["桌面播放器", "正在播放的软件"])
        self.source.picked.connect(lambda _i: self._render())
        self.kind.picked.connect(lambda _i: self._render())
        fv.addWidget(self.kind, 0, Qt.AlignLeft)
        fv.addWidget(self.source, 0, Qt.AlignLeft)
        # local file
        self.local = QWidget()
        lv = QVBoxLayout(self.local)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(4)
        fr = QHBoxLayout()
        self.choose = button("选择文件…", "secondary")
        self.choose.clicked.connect(self._choose)
        fr.addWidget(self.choose)
        self.file_label = Elided("还没选文件", "caption", "ink3")
        fr.addWidget(self.file_label, 1)
        lv.addLayout(fr)
        tr = QHBoxLayout()
        self.choose_text = button("加字幕或歌词（可选）", "ghost", tip="选择 SRT 或 LRC 文件")
        self.choose_text.clicked.connect(self._choose_text)
        tr.addWidget(self.choose_text)
        self.text_label = Elided("", "caption", "ink3")
        tr.addWidget(self.text_label, 1)
        lv.addLayout(tr)
        fv.addWidget(self.local)
        # Windows media sessions
        self.system = QWidget()
        sv = QVBoxLayout(self.system)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(4)
        sh = QHBoxLayout()
        sh.addWidget(label("Windows 报告正在播放：", "caption", "ink2"))
        sh.addStretch(1)
        self.refresh_button = button("刷新", "ghost")
        self.refresh_button.clicked.connect(self.refresh)
        sh.addWidget(self.refresh_button)
        sv.addLayout(sh)
        self.list = QVBoxLayout()
        self.list.setSpacing(2)
        sv.addLayout(self.list)
        self.empty = label("现在没有软件在报告播放。先在播放器里开始播放，再点“刷新”。", "caption", "ink3", wrap=True)
        sv.addWidget(self.empty)
        self.choices = []
        fv.addWidget(self.system)
        v.addWidget(self.form)
        self.error = label("", "caption", theme.ERROR_FG, wrap=True)
        self.error.hide()
        v.addWidget(self.error)
        self.privacy = label(together_privacy(False), "caption", "ink3", wrap=True)
        v.addWidget(self.privacy)
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.cancel = button("取消", "secondary")
        self.go = button("开始", "primary")
        btns.addWidget(self.cancel)
        btns.addWidget(self.go)
        v.addLayout(btns)
        shadow(card)
        self.cancel.clicked.connect(self.close_layer)
        self.go.clicked.connect(self._start)
        self.reason = False   # forces the first render
        self.source.select(0)
        self.set_reason(None)

    # ------------------------------------------------------------ state
    def set_reason(self, reason):
        """None = can start; otherwise a plain sentence saying why not."""
        if reason != self.reason:
            self.reason = reason
            self._render()

    def set_source(self, source):
        self.source.select(SOURCES.index(source) if source in SOURCES else 0)
        self._render()

    def set_sources(self, sources):
        self.sources = [s for s in sources if s.get("source") == "system_media"]
        for b in self.choices:
            self.list.removeWidget(b)
            b.deleteLater()
        self.choices = []
        if self.system_id not in {s["id"] for s in self.sources}:
            self.system_id = self.sources[0]["id"] if len(self.sources) == 1 else None
        for s in self.sources:
            b = button(app_name(s.get("label")), "nav")
            b.clicked.connect(lambda _=False, sid=s["id"]: self._pick_system(sid))
            self.list.addWidget(b)
            self.choices.append(b)
        self._render()

    def set_error(self, text):
        self.error.setText(text or "")
        self.error.setVisible(bool(text))
        self.card.adjustSize()

    def values(self):
        return {"kind": KINDS[max(0, self.kind.index)], "source": SOURCES[max(0, self.source.index)],
                "path": self.path, "timed_text": self.timed_text, "system_id": self.system_id}

    def _render(self):
        blocked = self.reason is not None
        self.blocked.setVisible(blocked)
        self.blocked_text.setText(self.reason or "")
        self.form.setVisible(not blocked)
        values = self.values()
        local = values["source"] == "local_player"
        self.local.setVisible(local)
        self.system.setVisible(not local)
        self.choose_text.setText("加歌词（可选）" if values["kind"] == "music" else "加字幕（可选）")
        for b, s in zip(self.choices, self.sources):
            b.setProperty("on", "true" if s["id"] == self.system_id else "false")
            b.style().unpolish(b)
            b.style().polish(b)
        self.empty.setVisible(not self.sources)
        ready = local and self.path is not None or not local and self.system_id is not None
        self.go.setEnabled(not blocked and ready)
        self.go.setText(VERB[values["kind"]])
        if self.isVisible():
            self.place()

    # ------------------------------------------------------------ choices
    def _pick(self, title, filter_, done):
        """Window-modal but non-blocking: the event loop and the session's sync timers keep running."""
        if self.dialog is not None:
            self.dialog.close()
        dialog = QFileDialog(self.window(), title, "", filter_)
        dialog.setFileMode(QFileDialog.ExistingFile)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.fileSelected.connect(lambda path: done(path or None))
        dialog.destroyed.connect(lambda _=None: self._dialog_gone(dialog))
        self.dialog = dialog
        dialog.open()

    def _dialog_gone(self, dialog):
        if self.dialog is dialog:
            self.dialog = None

    def _choose(self):
        self.pick("选择要一起听/看的文件", MEDIA_FILTER, self._chosen)

    def _chosen(self, path):
        if path:
            self.path = path
            self.file_label.set_text(Path(path).name)
            self.set_error(None)
            self._render()

    def _choose_text(self):
        self.pick("选择字幕或歌词", TEXT_FILTER, self._chosen_text)

    def _chosen_text(self, path):
        if path:
            self.timed_text = path
            self.text_label.set_text(Path(path).name)
            self._render()

    def _pick_system(self, source_id):
        self.system_id = source_id
        self.set_error(None)
        self._render()

    def _start(self):
        if self.go.isEnabled():
            self.start.emit(self.values())

    # ------------------------------------------------------------ layer
    def open(self):
        self.set_error(None)
        super().open()

    def place(self):
        self.card.adjustSize()
        x = max(12, self.width() - self.card.width() - 12)
        y = 64
        if self.anchor is not None:
            y = self.anchor.mapTo(self.parent(), self.anchor.rect().bottomLeft()).y() + 6
        self.card.move(x, min(y, max(12, self.height() - self.card.height() - 12)))


def app_name(label_text):
    """Windows reports an app id such as ``Spotify.exe`` or ``Microsoft.ZuneMusic_8wekyb3d8bbwe!App``."""
    text = str(label_text or "").strip() or "未知软件"
    text = text.split("!")[0]
    if "_" in text and "." in text:
        text = text.split("_")[0].split(".")[-1]
    if text.lower().endswith(".exe"):
        text = text[:-4]
    return text or "未知软件"


class VideoWindow(QWidget):
    """A plain window for a local film. The panel never draws on top of it."""

    def __init__(self, title):
        super().__init__()
        from PyQt5.QtMultimediaWidgets import QVideoWidget
        self.setWindowTitle(title)
        self.setObjectName("Video")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("QWidget#Video { background: #000; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.video = QVideoWidget(self)
        v.addWidget(self.video)
        self.resize(960, 540)
        self.on_close = None

    def closeEvent(self, e):
        if self.on_close is not None:
            self.on_close()
        e.accept()


class Together(QObject):
    """Glue between the panel, the tray and ``shared_media``."""

    active_changed = pyqtSignal(bool)

    def __init__(self, media, panel, prefs, state, name="她", unavailable=None, presented=None,
                 video_window=VideoWindow, pick=None):
        super().__init__()   # not a child of the panel: it outlives it to clean up
        self.media, self.panel, self.prefs, self.state, self.name = media, panel, prefs, state, name
        self.unavailable = unavailable
        self.presented = presented or (lambda _rid: True)
        self.video_window = video_window
        self.video = None
        self.kind = self.source = None
        self.reactions = {}           # reaction_id -> bubble
        self.cancelled = []           # recently cancelled reaction ids, newest last
        self.active = False
        self.button = panel.header.together
        self.picker = TogetherPicker(panel, name, anchor=self.button, pick=pick)
        self.picker.privacy.setText(together_privacy(prefs.get("together_frame_glance")))
        panel.layers.append(self.picker)
        self.card = NowPlayingCard(name)
        panel.media_slot.addWidget(self.card)
        self.button.clicked.connect(self.open)
        self.picker.start.connect(self.begin)
        self.picker.refresh.connect(self._refresh)
        self.card.play_pause.connect(self._play_pause)
        self.card.stop.connect(self._halt)
        self.card.end.connect(self.end)
        # (owner, signal name, slot): kept so they can be cut when the panel goes
        self.links = [(prefs, "changed", self._pref), (state, "changed", lambda _s: self._reason())]
        if media is not None:
            self.links += [(media, "state_changed", lambda _s: self._sync()), (media, "item_changed", self._item),
                           (media, "sources_changed", lambda s: self.picker.set_sources(s)),
                           (media, "reaction", self._reaction), (media, "reaction_cancelled", self.cancel_reaction),
                           (media, "error", self._error)]
            self.picker.set_sources(media.sources)
        for owner, name, slot in self.links:
            getattr(owner, name).connect(slot)
        panel.destroyed.connect(self._panel_gone)
        self.picker.set_source(prefs.get("together_source"))
        self._render()

    # ------------------------------------------------------------ availability
    def reason(self, ignore_pref=False):
        if self.unavailable:
            return self.unavailable
        if not ignore_pref and not self.prefs.get("together_enabled"):
            return "一起听/看已在设置里关掉。到“设置 → 一起听看”里打开。"
        if not ignore_pref and self.state.connection != "connected":
            return "连上服务器以后才能开始。"
        if self.state.permissions is not None and "speak" not in self.state.permissions:
            return (f"这台设备还没有对话（speak）授权，{self.name}没法陪你一起听/看。"
                    "请在主机上授权后，到“设置 → 连接”里点“重新连接”。")
        return None

    def _reason(self):
        self.picker.set_reason(self.reason())

    # ------------------------------------------------------------ entry
    def open(self):
        if self.active:
            self.card.set_collapsed(False)
            self.card.show()
            return
        if self.media is not None and self.reason() is None:
            self.media.refresh_sources()
        self._reason()
        self.picker.open()

    def _refresh(self):
        if self.media is not None:
            self.media.refresh_sources()

    def begin(self, choice):
        if self.reason() is not None or self.media is None:
            self._reason()
            return False
        m, kind, source = self.media, choice["kind"], choice["source"]
        try:
            if source == "local_player":
                m.load_local(choice["path"], choice.get("timed_text"))
            else:
                m.select_system(choice["system_id"])
                if choice.get("timed_text"):
                    m.set_timed_text(choice["timed_text"])
        except (OSError, ValueError, UnicodeError):
            self.picker.set_error("这个文件打不开。请换一个本地音频、视频或字幕文件。" if source == "local_player"
                                  else "这个软件已经不在播放了，点“刷新”再选一次。")
            return False
        if not m.start(source, kind):
            if not self.picker.error.text():
                self.picker.set_error(ERRORS["no_media"])
            return False
        self.kind, self.source = kind, source
        self.prefs.set("together_source", source)
        label_text = SOURCE[source]
        if source == "system_media":
            match = next((s for s in self.picker.sources if s["id"] == choice["system_id"]), None)
            label_text = app_name(match and match.get("label"))
        self.card.preparing(kind, source, label_text)
        self.card.set_collapsed(kind == "movie")
        self.picker.close_layer()
        if source == "local_player":
            if kind == "movie" and self.video_window is not None:
                self._open_video(choice["path"])
            m.local_player.player.play()
        self._sync()
        return True

    def _open_video(self, path):
        try:
            self.video = self.video_window(f"{VERB['movie']} · {Path(path).stem}")
        except ImportError:
            self.video = None
            return
        self.media.local_player.set_video_output(self.video.video)
        self.video.on_close = lambda: self.end(from_video=True)
        self.video.show()

    def end(self, from_video=False):
        """End sharing. The desktop player is ours, so it stops too; other apps keep playing."""
        m = self.media
        if m is not None:
            m.stop()
            if self.source == "local_player":
                m.local_player.player.stop()
        self._close_video(from_video)
        self._sync()

    def _close_video(self, from_video=False):
        video, self.video = self.video, None
        if video is not None and self.media is not None and not sip.isdeleted(self.media):
            self.media.local_player.set_video_output(None)
        if video is not None and not sip.isdeleted(video):
            video.on_close = None
            if not from_video:
                video.close()
            video.deleteLater()

    def _panel_gone(self, *_):
        """The panel (with the card, picker and bubbles) is gone: nothing may call back into it."""
        for owner, name, slot in self.links:
            if not sip.isdeleted(owner):   # at interpreter exit the owner may already be gone
                try:
                    getattr(owner, name).disconnect(slot)
                except TypeError:
                    pass
        self.links = []
        self.reactions.clear()
        m, self.panel = self.media, None
        if m is not None and sip.isdeleted(m):
            m = None
        if m is not None and self.active:
            m.stop()
            if self.source == "local_player":
                m.local_player.player.stop()
        self._close_video()
        self.active = False

    def _halt(self):
        if self.media is not None and self.source == "local_player":
            self.media.local_player.player.stop()

    def _play_pause(self):
        if self.media is None or self.source != "local_player":
            return
        item = self.media.current_item
        player = self.media.local_player.player
        if item is not None and item.get("status") == "playing":
            player.pause()
        else:
            player.play()

    # ------------------------------------------------------------ media events
    def _sync(self):
        if self.panel is None:
            return
        active = self.media is not None and self.media.active
        if active != self.active:
            self.active = active
            if not active:
                # sharing ended here or on the host side: the desktop player is ours to stop
                if self.source == "local_player" and self.media is not None:
                    self.media.local_player.player.stop()
                self._close_video()
                for bubble in list(self.reactions.values()):
                    self.panel.chat.remove_item(bubble)
                self.reactions.clear()
            self.active_changed.emit(active)
        self._render()

    def _render(self):
        self.card.setVisible(self.active)
        self.panel.media_wrap.setVisible(self.active)
        verb = VERB.get(self.kind, "一起听")
        self.button.set_look("note_on" if self.active else "note", self.active)
        tip = f"正在{verb}" if self.active else "一起听 / 一起看"
        self.button.setToolTip(tip)
        self.button.setAccessibleName(tip)

    def _item(self, item):
        self._sync()
        if item is not None and self.active:
            self.card.set_item(item, self.media.current_line)

    def _reaction(self, event):
        rid = event.get("reaction_id")
        if (not self.active or not rid or rid in self.cancelled or rid in self.reactions
                or not self.presented(rid)):
            return
        self.reactions[rid] = self.panel.chat.add_reaction(event.get("text", ""), self.kind or "music")

    def cancel_reaction(self, reaction_id):
        """Idempotent; shared_media and the controller both call it, whichever notices first."""
        if not reaction_id:
            return
        if reaction_id not in self.cancelled:
            self.cancelled.append(reaction_id)
            del self.cancelled[:-64]
        bubble = self.reactions.pop(reaction_id, None)
        if bubble is not None and self.panel is not None:
            self.panel.chat.remove_item(bubble)

    def _error(self, reason):
        text = ERRORS.get(reason, "一起听/看遇到问题，已结束。")
        if self.picker.isVisible():
            self.picker.set_error(text)
        else:
            self.panel.flash("offline" if not self.media.active else "interrupted", text)
        self._sync()

    def _pref(self, key, value):
        if key == "together_enabled" and not value and self.active:
            self.end()
        if key == "together_enabled":
            self._reason()
        elif key == "together_source" and not self.picker.isVisible():
            self.picker.set_source(value)
        elif key == "together_frame_glance":
            self.picker.privacy.setText(together_privacy(value))
