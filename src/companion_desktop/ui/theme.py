"""Design tokens and the application stylesheet.

Single source for colours, type, shape and motion. QSS, the painted widgets and the
subtitle page all take their values from here.
"""

from dataclasses import dataclass

from PyQt5.QtGui import QFont

# ---------------------------------------------------------------- colour
C = {
    "bg": "#F3EEE5",          # window paper
    "surface": "#FCFAF6",     # cards, companion bubble, composer
    "sunken": "#ECE5D9",      # inset areas, disabled, settings nav
    "line": "#E0D6C6",        # decorative hairlines
    "line_strong": "#968872",  # input borders (>= 3:1 on surface)
    "ink": "#2B2722",
    "ink2": "#5A5147",
    "ink3": "#6E6457",
    "ink_disabled": "#8A8073",
    "user_bubble": "#E8DDCB",
    "accent": "#2F5D50",
    "accent_hover": "#274F44",
    "accent_press": "#1F4038",
    "accent_soft": "#DDE8E1",
    "on_accent": "#FFFFFF",
    "avatar_fill": "#EFE5D5",
}

# Presence states. ring = state colour, fg/soft = chip text/background (fg on soft >= 4.9:1).
STATE = {
    "connecting":  {"label": "正在连接",   "ring": "#A79C8C", "fg": "#5A5147", "soft": "#ECE5D9"},
    "idle":        {"label": "在这儿",     "ring": "#8FA596", "fg": "#2F5D50", "soft": "#DDE8E1"},
    "listening":   {"label": "在听你说",   "ring": "#4E8A73", "fg": "#2F5D50", "soft": "#DDE8E1"},
    "thinking":    {"label": "在想",       "ring": "#C08A33", "fg": "#8A5A12", "soft": "#F5E9D2"},
    "speaking":    {"label": "正在说",     "ring": "#2F5D50", "fg": "#2F5D50", "soft": "#DDE8E1"},
    "interrupted": {"label": "停下了",     "ring": "#8C8378", "fg": "#5F574E", "soft": "#ECE7E0"},
    "offline":     {"label": "连接断开",   "ring": "#B0503A", "fg": "#9A3F2B", "soft": "#F7E3DC"},
    "observing":   {"label": "正在看屏幕", "ring": "#3F6496", "fg": "#34557F", "soft": "#E1E8F3"},
}

# Error text colour (never the success/accent colour).
ERROR_FG = STATE["offline"]["fg"]
ERROR_SOFT = STATE["offline"]["soft"]

# ---------------------------------------------------------------- type
UI_FONT = "Microsoft YaHei UI"
UI_FALLBACKS = ["Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC", "PingFang SC", "sans-serif"]
VOICE_FONT = "方正新书宋简体"
VOICE_FALLBACKS = ["Noto Serif SC", "Noto Serif CJK SC", "Source Han Serif SC", "SimSun"]

# name: (px at 100 %, weight)
TYPE = {
    "caption": (12, 400),
    "label": (13, 400),
    "body": (14, 400),
    "message": (15, 400),
    "name": (20, 400),   # voice font
    "title": (17, 700),
}

SPACE = [0, 4, 8, 12, 16, 20, 24, 32]
RADIUS = {"xs": 6, "sm": 10, "md": 14, "lg": 18}


@dataclass(frozen=True)
class Motion:
    press: int = 90
    fast: int = 140
    base: int = 200
    slow: int = 320
    thinking_cycle: int = 1200
    thinking_hint_after: int = 3000
    thinking_slow_after: int = 20000
    subtitle_in: int = 160
    subtitle_hold_after_end: int = 1800
    subtitle_out: int = 400
    interrupt_out: int = 200
    interrupt_chip: int = 1500
    reconnected_toast: int = 3000
    frame_hz: int = 30


MOTION = Motion()

_voice_family = [VOICE_FONT]


def set_voice_font(family):
    """Local config may choose a different voice font; keep the fallbacks."""
    if family:
        _voice_family[0] = family


def font(kind="body", voice=False, bold=None):
    px, weight = TYPE[kind]
    f = QFont(_voice_family[0] if voice else UI_FONT)
    families = ([_voice_family[0]] + VOICE_FALLBACKS + [UI_FONT]) if voice else [UI_FONT] + UI_FALLBACKS
    if hasattr(f, "setFamilies"):
        f.setFamilies(families)
    f.setPixelSize(px)
    b = (weight >= 700) if bold is None else bold
    # The voice font has a single weight: never synthesise bold for it.
    f.setWeight(QFont.Bold if (b and not voice) else QFont.Normal)
    f.setHintingPreference(QFont.PreferNoHinting if voice else QFont.PreferDefaultHinting)
    return f


def qss():
    """Application stylesheet. No font-family here: it would override per-widget setFont()."""
    return f"""
    QWidget {{ color: {C['ink']}; }}
    QWidget#Panel, QWidget#SettingsWindow {{ background: {C['bg']}; }}
    QScrollArea#Chat, QWidget#ChatBody {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 6px 2px; }}
    QScrollBar::handle:vertical {{ background: {C['line']}; border-radius: 3px; min-height: 28px; }}
    QScrollBar::handle:vertical:hover {{ background: #CFC3B0; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QFrame#Composer {{ background: {C['surface']}; border: 1px solid {C['line_strong']}; border-radius: {RADIUS['md']}px; }}
    QFrame#Composer[focus="true"] {{ border: 2px solid {C['accent']}; }}
    QFrame#Composer[blocked="true"] {{ background: {C['sunken']}; border: 1px solid {C['line']}; }}
    QPlainTextEdit#Entry {{ background: transparent; border: none; padding: 0;
        selection-background-color: {C['accent_soft']}; selection-color: {C['ink']}; }}
    QPushButton[kind="primary"] {{ background: {C['accent']}; color: {C['on_accent']}; border: none;
        border-radius: {RADIUS['sm']}px; padding: 0 18px; min-height: 36px; font-size: 14px; font-weight: 700; }}
    QPushButton[kind="primary"]:hover {{ background: {C['accent_hover']}; }}
    QPushButton[kind="primary"]:pressed {{ background: {C['accent_press']}; }}
    QPushButton[kind="primary"]:disabled {{ background: {C['sunken']}; color: {C['ink_disabled']}; }}
    QPushButton[kind="secondary"] {{ background: {C['surface']}; color: {C['ink']}; border: 1px solid {C['line_strong']};
        border-radius: {RADIUS['sm']}px; padding: 0 16px; min-height: 36px; font-size: 14px; }}
    QPushButton[kind="secondary"]:hover {{ background: {C['sunken']}; }}
    QPushButton[kind="secondary"]:disabled {{ color: {C['ink_disabled']}; border-color: {C['line']}; }}
    QPushButton[kind="ghost"] {{ background: transparent; color: {C['accent']}; border: none;
        border-radius: {RADIUS['xs']}px; padding: 0 8px; min-height: 26px; font-size: 13px; }}
    QPushButton[kind="ghost"]:hover {{ background: {C['accent_soft']}; }}
    QPushButton[kind="ghost"]:disabled {{ color: {C['ink_disabled']}; }}
    QPushButton[kind="segment"] {{ background: transparent; color: {C['ink2']}; border: none; border-radius: 8px;
        padding: 0 14px; min-height: 32px; font-size: 13px; }}
    QPushButton[kind="segment"][on="true"] {{ background: {C['surface']}; color: {C['ink']}; font-weight: 700; }}
    QPushButton[kind="nav"] {{ text-align: left; padding: 0 12px; min-height: 34px; border: none; border-radius: 8px;
        font-size: 14px; color: {C['ink2']}; background: transparent; }}
    QPushButton[kind="nav"]:hover {{ background: {C['bg']}; }}
    QPushButton[kind="nav"][on="true"] {{ color: {C['accent']}; background: {C['surface']}; font-weight: 700; }}
    QPushButton:focus {{ outline: none; }}
    QPushButton[kind="primary"]:focus {{ border: 2px solid {C['accent_press']}; }}
    QPushButton[kind="secondary"]:focus {{ border: 2px solid {C['accent']}; }}
    QPushButton[kind="ghost"]:focus {{ background: {C['accent_soft']}; }}
    QMenu {{ background: {C['surface']}; border: 1px solid {C['line']}; border-radius: 12px; padding: 6px; }}
    QMenu::item {{ padding: 7px 28px 7px 14px; border-radius: 8px; font-size: 13px; color: {C['ink']}; }}
    QMenu::item:selected {{ background: {C['accent_soft']}; }}
    QMenu::item:disabled {{ color: {C['ink3']}; }}
    QMenu::separator {{ height: 1px; background: {C['line']}; margin: 5px 8px; }}
    QSlider::groove:horizontal {{ height: 4px; background: {C['line']}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {C['accent']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {C['surface']}; border: 2px solid {C['accent']};
        width: 14px; height: 14px; margin: -7px 0; border-radius: 9px; }}
    QSlider::handle:horizontal:disabled {{ border-color: {C['line_strong']}; }}
    QLineEdit {{ background: {C['surface']}; border: 1px solid {C['line_strong']}; border-radius: 8px;
        padding: 0 10px; min-height: 32px; font-size: 13px; }}
    QLineEdit:focus {{ border: 2px solid {C['accent']}; }}
    QToolTip {{ background: {C['ink']}; color: {C['surface']}; border: none; padding: 5px 8px; border-radius: 6px; }}
    """
