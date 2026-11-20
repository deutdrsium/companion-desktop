"""Text and platform helpers shared by the client and its tests."""

import json
import re
import sys

# Inline control tags the host may leave in a reply; never shown or spoken.
_TAGS = r"soullink|wardrobe|expression|pc_tts|tts|voice|record|send_message_to_user|语音"


def visible_text(text):
    value = re.sub(rf"(?is)<({_TAGS})\b[^>]*>.*?</\1\s*>", "", str(text))
    value = re.sub(rf"(?is)<(?:{_TAGS})\b[^>]*>.*$", "", value)
    value = re.sub(r"(?is)<l2d(?::[^>]*)?>", "", value)
    return re.sub(rf"(?is)</?(?:{_TAGS})\b[^>]*>", "", value).strip()


def sentence_spans(clean):
    """Split already-cleaned text into (start, end) code-point spans that cover it in order.

    Concatenating the spans gives back ``clean``; whitespace-only spans are merged into
    the previous sentence so highlighting never lands on an empty piece.
    """
    spans = []
    for m in re.finditer(r".*?(?:[。！？!?；;…]+[”」』）)]*|\n+|$)", clean, re.S):
        s, e = m.span()
        if s == e:
            continue
        while e - s > 1000:
            piece = clean[s:s + 1000]
            cut = max(piece.rfind("，"), piece.rfind(","), piece.rfind(" "))
            cut = cut + 1 if cut > 0 else 1000
            spans.append((s, s + cut))
            s += cut
        if not clean[s:e].strip():
            if spans:
                spans[-1] = (spans[-1][0], e)
            continue
        spans.append((s, e))
    return spans


def sentences(text):
    """Speakable sentences of a reply (same pieces the bubble highlights)."""
    clean = visible_text(text)
    return [clean[s:e] for s, e in sentence_spans(clean)]


def read_sse(lines):
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                yield json.loads(payload)


def vts_running():
    """Check the Windows process snapshot without opening every process."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("process_id", wintypes.DWORD), ("heap_id", ctypes.c_size_t),
                    ("module_id", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent_id", wintypes.DWORD), ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260)]
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        kernel.Process32FirstW.restype = wintypes.BOOL
        kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        kernel.Process32NextW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            return False
        try:
            entry = ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while found:
                if entry.name.casefold() == "vtube studio.exe":
                    return True
                found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
            return False
        finally:
            kernel.CloseHandle(snapshot)
    except OSError:
        return False


def desktop_locked():
    """True when the input desktop is not the normal one (locked / UAC). Fails closed."""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        handle = user32.OpenInputDesktop(0, False, 1)
        if not handle:
            return True
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            ok = user32.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed))
            return not ok or name.value.casefold() != "default"
        finally:
            user32.CloseDesktop(handle)
    except OSError:
        return True
