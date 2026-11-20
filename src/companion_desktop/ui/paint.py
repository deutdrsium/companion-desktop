"""Painted controls: icons, avatar with state ring, switch, chips, waveform, thinking dots.

Everything is drawn with QPainter on float coordinates, so nothing depends on the
Qt SVG image plugin (the old switch indicators rendered blank when it was missing).
"""

import math
import sys

from PyQt5.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import QAbstractButton, QWidget

from . import theme
from .theme import C, STATE


def reduced_motion():
    """True when Windows client-area animations are switched off."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        value = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0)  # SPI_GETCLIENTAREAANIMATION
        return bool(ok) and not value.value
    except Exception:
        return False


class Character:
    """Display identity. Real name/avatar come from local config; defaults are generic."""

    def __init__(self, name="小伴", glyph=None, avatar=None):
        self.name = name or "小伴"
        self.glyph = (glyph or self.name[:1] or "?")[:2]
        self.avatar = avatar  # QPixmap or None

    @classmethod
    def from_config(cls, cfg):
        pix = None
        path = (cfg or {}).get("avatar_image")
        if path:
            candidate = QPixmap(str(path))
            pix = None if candidate.isNull() else candidate
        return cls((cfg or {}).get("name"), (cfg or {}).get("glyph"), pix)


# ---------------------------------------------------------------- icons
def draw_icon(p, name, rect, color, width=1.6):
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    cx, cy, s = rect.center().x(), rect.center().y(), min(rect.width(), rect.height())
    u = s / 20.0
    if name in ("eye", "eye_on"):
        path = QPainterPath(QPointF(cx - 8 * u, cy))
        path.quadTo(cx, cy - 8 * u, cx + 8 * u, cy)
        path.quadTo(cx, cy + 8 * u, cx - 8 * u, cy)
        p.drawPath(path)
        if name == "eye_on":
            p.setBrush(QColor(color))
        p.drawEllipse(QPointF(cx, cy), 2.6 * u, 2.6 * u)
    elif name in ("pin", "pin_on"):
        p.translate(cx, cy)
        p.rotate(35)
        body = QPainterPath()
        body.moveTo(-3.5 * u, -7 * u)
        body.lineTo(3.5 * u, -7 * u)
        body.lineTo(3 * u, -1.5 * u)
        body.lineTo(5.5 * u, 1.5 * u)
        body.lineTo(-5.5 * u, 1.5 * u)
        body.lineTo(-3 * u, -1.5 * u)
        body.closeSubpath()
        if name == "pin_on":
            p.setBrush(QColor(color))
        p.drawPath(body)
        p.drawLine(QPointF(0, 1.5 * u), QPointF(0, 8 * u))
    elif name == "more":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        for dx in (-6, 0, 6):
            p.drawEllipse(QPointF(cx + dx * u, cy), 1.5 * u, 1.5 * u)
    elif name == "send":
        p.drawLine(QPointF(cx, cy + 6 * u), QPointF(cx, cy - 6 * u))
        p.drawLine(QPointF(cx - 5 * u, cy - 1 * u), QPointF(cx, cy - 6 * u))
        p.drawLine(QPointF(cx + 5 * u, cy - 1 * u), QPointF(cx, cy - 6 * u))
    elif name == "stop":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(cx - 4.5 * u, cy - 4.5 * u, 9 * u, 9 * u), 2 * u, 2 * u)
    elif name == "close":
        p.drawLine(QPointF(cx - 5 * u, cy - 5 * u), QPointF(cx + 5 * u, cy + 5 * u))
        p.drawLine(QPointF(cx + 5 * u, cy - 5 * u), QPointF(cx - 5 * u, cy + 5 * u))
    elif name == "check":
        p.drawPolyline(QPointF(cx - 5 * u, cy), QPointF(cx - 1.5 * u, cy + 4 * u), QPointF(cx + 5.5 * u, cy - 4 * u))
    elif name == "cross":
        p.drawLine(QPointF(cx - 4 * u, cy - 4 * u), QPointF(cx + 4 * u, cy + 4 * u))
        p.drawLine(QPointF(cx + 4 * u, cy - 4 * u), QPointF(cx - 4 * u, cy + 4 * u))
    elif name == "pending":
        p.drawEllipse(QPointF(cx, cy), 4.5 * u, 4.5 * u)
    elif name == "doing":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        for dx in (-5, 0, 5):
            p.drawEllipse(QPointF(cx + dx * u, cy), 1.4 * u, 1.4 * u)
    p.restore()


class IconButton(QAbstractButton):
    def __init__(self, icon, tip="", size=34, tint=None, parent=None):
        super().__init__(parent)
        self.icon, self.tint, self.active, self.badge = icon, tint, False, None
        self.setFixedSize(size, size)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)

    def set_look(self, icon=None, active=None, badge=False):
        if icon is not None:
            self.icon = icon
        if active is not None:
            self.active = active
        if badge is not False:
            self.badge = badge
        self.update()

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        tint = self.tint or C["accent"]
        if self.active:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(STATE["observing"]["soft"] if self.icon.startswith("eye") else C["accent_soft"]))
            p.drawRoundedRect(r, 9, 9)
        elif self.underMouse() or self.isDown():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["sunken"]))
            p.drawRoundedRect(r, 9, 9)
        if self.hasFocus():
            p.setPen(QPen(QColor(C["accent"]), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, 9, 9)
        draw_icon(p, self.icon, QRectF(self.rect()).adjusted(7, 7, -7, -7), tint if self.active else C["ink2"])
        if self.badge:
            p.setPen(QPen(QColor(C["bg"]), 2))
            p.setBrush(QColor(self.badge))
            p.drawEllipse(QPointF(r.right() - 6, r.top() + 6), 4, 4)


class Toggle(QAbstractButton):
    """Painted switch; replaces the SVG checkbox indicators."""

    def __init__(self, checked=False, name="", parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(40, 24)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(name)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        track = QRectF(2, 2, 36, 20)
        if self.hasFocus():
            p.setPen(QPen(QColor(C["accent"]), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(0.5, 0.5, 39, 23), 12, 12)
        on = self.isChecked()
        if not self.isEnabled():
            p.setPen(QPen(QColor(C["line"]), 1))
            p.setBrush(QColor(C["sunken"]))
        else:
            p.setPen(Qt.NoPen if on else QPen(QColor(C["line_strong"]), 1))
            p.setBrush(QColor(C["accent"] if on else C["sunken"]))
        p.drawRoundedRect(track, 10, 10)
        x = track.right() - 10 if on else track.left() + 10
        p.setPen(Qt.NoPen if on else QPen(QColor(C["line_strong"]), 0.8))
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QPointF(x, track.center().y()), 7.5, 7.5)


class Ticker:
    """One 30 Hz clock shared by all looping animations; runs only while a subscriber is visible."""

    _timer = None
    _subs = set()
    phase_ms = 0

    @classmethod
    def subscribe(cls, widget):
        cls._subs.add(widget)
        if cls._timer is None:
            cls._timer = QTimer()
            cls._timer.setInterval(1000 // theme.MOTION.frame_hz)
            cls._timer.timeout.connect(cls._tick)
        if not cls._timer.isActive():
            cls._timer.start()

    @classmethod
    def unsubscribe(cls, widget):
        cls._subs.discard(widget)
        if not cls._subs and cls._timer is not None:
            cls._timer.stop()

    @classmethod
    def _tick(cls):
        cls.phase_ms = (cls.phase_ms + cls._timer.interval()) % 120000
        for w in list(cls._subs):
            try:
                if w.isVisible():
                    w.tick(cls.phase_ms)
            except RuntimeError:  # widget deleted
                cls._subs.discard(w)


class Animated(QWidget):
    """Mixin base: animate() turns the shared clock on/off for this widget."""

    reduced = None

    def __init__(self, parent=None):
        super().__init__(parent)
        if Animated.reduced is None:
            Animated.reduced = reduced_motion()
        self._wants = False

    def animate(self, on):
        self._wants = bool(on) and not Animated.reduced
        if self._wants and self.isVisible():
            Ticker.subscribe(self)
        else:
            Ticker.unsubscribe(self)

    def showEvent(self, e):
        if self._wants:
            Ticker.subscribe(self)
        super().showEvent(e)

    def hideEvent(self, e):
        Ticker.unsubscribe(self)
        super().hideEvent(e)

    def tick(self, ms):
        self.update()


def paint_avatar(p, size, state, character, phase=0.25, level=0.0, still=False, small=False):
    """Avatar disc + state ring. Shared by the header, cards and tray icons."""
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    s = float(size)
    st = STATE[state]
    ring = QColor(st["ring"])
    stroke = max(1.6, s * 0.055)
    pad = stroke / 2 + 0.5
    rr = QRectF(pad, pad, s - 2 * pad, s - 2 * pad)
    inner = rr.adjusted(stroke + 1.5, stroke + 1.5, -stroke - 1.5, -stroke - 1.5)
    if small:
        inner = rr.adjusted(stroke + 0.5, stroke + 0.5, -stroke - 0.5, -stroke - 0.5)
    if state == "speaking" and level > 0 and not small:
        halo = QColor(ring)
        halo.setAlphaF(0.18)
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        g = 1 + 2.5 * level
        p.drawEllipse(inner.adjusted(-g, -g, g, g))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(C["avatar_fill"]))
    p.drawEllipse(inner)
    if character is not None and character.avatar is not None and not small:
        clip = QPainterPath()
        clip.addEllipse(inner)
        p.save()
        p.setClipPath(clip)
        pm = character.avatar.scaled(int(inner.width() * 2), int(inner.height() * 2),
                                     Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        p.drawPixmap(inner, pm, QRectF(0, 0, pm.width(), pm.height()))
        p.restore()
    elif not small:
        p.setPen(QColor(C["accent"]))
        f = theme.font("name", voice=True)
        f.setPixelSize(max(8, int(s * 0.40)))
        p.setFont(f)
        p.drawText(inner, Qt.AlignCenter, character.glyph if character else "?")
    else:  # tray at 16-20 px: glyph is illegible, draw a state dot instead
        p.setBrush(ring)
        d = inner.width() * 0.42
        p.drawEllipse(inner.center(), d / 2, d / 2)
    if state == "thinking" and not still:
        faint = QColor(ring)
        faint.setAlphaF(0.25)
        p.setPen(QPen(faint, stroke))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(rr)
        p.setPen(QPen(ring, stroke, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rr, int(-phase * 360 * 16), 100 * 16)
    else:
        pen = QPen(ring, stroke)
        if state in ("offline", "connecting"):
            pen.setStyle(Qt.DashLine)
            pen.setDashPattern([2.2, 2.0])
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(rr)
    if state in ("offline", "observing") and not small:
        p.setPen(QPen(QColor(C["bg"]), 2))
        p.setBrush(ring)
        r = max(3.0, s * 0.11)
        p.drawEllipse(QPointF(s - r - 1.5, s - r - 1.5), r, r)


class StateAvatar(Animated):
    """Presence: avatar + state ring. thinking = rotating arc, speaking = halo by audio level."""

    def __init__(self, character, state="idle", size=46, parent=None):
        super().__init__(parent)
        self.character, self.state, self.phase, self.level = character, state, 0.25, 0.0
        self.setFixedSize(size, size)
        self.setAccessibleName((character.name if character else "") + " 状态")

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.animate(state in ("thinking", "speaking"))
            self.update()

    def set_level(self, level):
        self.level = max(0.0, min(1.0, level))

    def tick(self, ms):
        self.phase = (ms % 1400) / 1400.0
        if self.state == "speaking" and self.level <= 0:
            self.level = 0.35 + 0.25 * math.sin(ms / 180.0)  # placeholder until P2/P3 send the envelope
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        paint_avatar(p, self.width(), self.state, self.character, self.phase, self.level,
                     still=bool(Animated.reduced))


def tray_icon(state, character):
    icon = QIcon()
    for px in (16, 20, 24, 32, 48):
        pm = QPixmap(px, px)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        paint_avatar(p, px, state, character, still=True, small=px < 24)
        p.end()
        icon.addPixmap(pm)
    return icon


class Chip(QWidget):
    def __init__(self, state, text=None, small=False, parent=None):
        super().__init__(parent)
        self.small = small
        self.set(state, text)

    def set(self, state, text=None):
        st = STATE[state]
        self.fg, self.soft, self.dot = QColor(st["fg"]), QColor(st["soft"]), QColor(st["ring"])
        self.text = text or st["label"]
        self.f = theme.font("caption" if self.small else "label")
        fm = QFontMetrics(self.f)
        self.setFixedSize(fm.horizontalAdvance(self.text) + 32, 24 if self.small else 26)
        self.setAccessibleName(self.text)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(self.soft)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setBrush(self.dot)
        p.drawEllipse(QPointF(12, r.center().y()), 3.5, 3.5)
        p.setPen(self.fg)
        p.setFont(self.f)
        p.drawText(r.adjusted(21, 0, -8, 0), Qt.AlignVCenter | Qt.AlignLeft, self.text)


class Dot(QWidget):
    def __init__(self, color, size=7, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.setFixedSize(size, size)

    def set_color(self, color):
        self.color = QColor(color)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(QRectF(self.rect()))


class Waveform(Animated):
    """Speaking level bars. U1 animates a placeholder; U2 feeds the shared RMS envelope."""

    def __init__(self, color=None, bars=14, parent=None):
        super().__init__(parent)
        self.color = QColor(color or C["accent"])
        self.levels = [0.3 + 0.55 * abs(math.sin(i * 0.9)) for i in range(bars)]  # still frame
        self.envelope = None
        self.setFixedSize(bars * 4 + 2, 18)
        self.animate(True)

    def push(self, level):
        self.envelope = True
        self.levels = self.levels[1:] + [max(0.0, min(1.0, level))]
        self.update()

    def tick(self, ms):
        if self.envelope is None:
            n = len(self.levels)
            self.levels = [0.25 + 0.6 * abs(math.sin(ms / 230.0 + i * 0.7)) * (0.6 + 0.4 * math.sin(ms / 510.0 + i))
                           for i in range(n)]
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self.color)
        h = self.height()
        for i, lv in enumerate(self.levels):
            bh = max(2.0, min(1.0, lv) * (h - 2))
            p.drawRoundedRect(QRectF(1 + i * 4, (h - bh) / 2, 2.4, bh), 1.2, 1.2)


class Dots(Animated):
    """Thinking dots; 1200 ms breathing loop, static when animations are reduced."""

    def __init__(self, color=None, parent=None):
        super().__init__(parent)
        self.phase = 0.3
        self.color = QColor(color or STATE["thinking"]["ring"])
        self.setFixedSize(40, 18)
        self.animate(True)

    def tick(self, ms):
        self.phase = (ms % theme.MOTION.thinking_cycle) / float(theme.MOTION.thinking_cycle)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        for i in range(3):
            t = (self.phase - i * 0.18) % 1.0
            k = 0.5 - 0.5 * math.cos(t * 2 * math.pi)
            if Animated.reduced:
                k = 0.6
            c = QColor(self.color)
            c.setAlphaF(0.35 + 0.65 * k)
            p.setBrush(c)
            p.drawEllipse(QPointF(8 + i * 12, 9 - 2.5 * k), 3.6, 3.6)


class SendButton(QAbstractButton):
    """Send arrow; becomes a dark stop square while the companion thinks or speaks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = "send"
        self.setFixedSize(38, 38)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.set_mode("send")

    def set_mode(self, mode):
        self.mode = mode
        tip = "发送（Enter）" if mode == "send" else "停下（Esc）"
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.update()

    def sizeHint(self):
        return QSize(38, 38)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            bg, fg = C["sunken"], C["ink_disabled"]
        elif self.mode == "stop":
            bg, fg = C["ink"], "#FFFFFF"
        else:
            bg, fg = (C["accent_press"] if self.isDown() else C["accent"]), "#FFFFFF"
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(bg))
        p.drawEllipse(r)
        if self.hasFocus():
            p.setPen(QPen(QColor(C["accent"]), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(r.adjusted(-0.5, -0.5, 0.5, 0.5))
        draw_icon(p, "stop" if self.mode == "stop" else "send", QRectF(self.rect()).adjusted(9, 9, -9, -9), fg, 2.0)


class StepIcon(QWidget):
    """Launch-card step marker: done / doing / fail / todo."""

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.kind = "todo"
        self.setFixedSize(18, 18)

    def set_kind(self, kind):
        self.kind = kind
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        color = {"done": C["accent"], "fail": STATE["offline"]["fg"], "doing": STATE["thinking"]["fg"],
                 "todo": C["ink3"]}[self.kind]
        icon = {"done": "check", "fail": "cross", "doing": "doing", "todo": "pending"}[self.kind]
        draw_icon(p, icon, QRectF(self.rect()), color, 2.0)
