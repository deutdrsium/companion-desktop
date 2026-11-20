"""Local desktop runtime for an AstrBot companion character."""

import importlib.util
import os
import sys

__version__ = "0.2.0"


def _fix_qt_plugin_path():
    """Qt 5 hangs in QApplication() when its plugin folder has non-ASCII characters.

    On Windows, point Qt at the 8.3 short path instead. Must run before PyQt5 is imported.
    """
    if sys.platform != "win32" or "QT_PLUGIN_PATH" in os.environ:
        return
    spec = importlib.util.find_spec("PyQt5")
    if spec is None or not spec.submodule_search_locations:
        return
    plugins = os.path.join(list(spec.submodule_search_locations)[0], "Qt5", "plugins")
    if plugins.isascii() or not os.path.isdir(plugins):
        return
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(plugins, buf, len(buf)):
            os.environ["QT_PLUGIN_PATH"] = buf.value
    except Exception:
        pass


_fix_qt_plugin_path()
