"""Synthetic stand-ins for the backend adapter and the media player (no network, no audio)."""

from types import SimpleNamespace

from PyQt5.QtCore import QObject, pyqtSignal

from companion_desktop.client.audio import Clip


class FakeBackend(QObject):
    history = pyqtSignal(object)
    history_failed = pyqtSignal()
    turn_accepted = pyqtSignal(str)
    chat_done = pyqtSignal(str, str)
    chat_failed = pyqtSignal(str, str, str)
    audio = pyqtSignal(int, str, int, str, bytes, bool)
    speech_error = pyqtSignal(int, int)
    screen_result = pyqtSignal(int, object)
    unreachable = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.generation = 0
        self.calls = []
        self.chats = []
        self.speech = []
        self.screens = []
        self.base = "http://127.0.0.1:1"

    def set_base(self, base):
        self.base = base

    def load_history(self):
        self.calls.append("history")

    def chat(self, text, turn_id):
        self.chats.append((text, turn_id))

    def cancel(self, turn_id):
        self.calls.append(("cancel", turn_id))
        return "unknown"

    def enqueue_speech(self, segments, start=0):
        self.speech.append((list(segments), start, self.generation))
        return self.generation

    def stop_speech(self):
        self.generation += 1

    def sync(self, action, audio_id, position_ms=0):
        self.calls.append(("sync", action, audio_id))

    def screen(self, generation, action):
        self.screens.append((generation, action))

    def close(self, generation):
        self.calls.append(("close", generation))

    # helpers
    def last_turn(self):
        return self.chats[-1][1]


class FakePlayer(QObject):
    started = pyqtSignal(object)
    progress = pyqtSignal(object, int, float)
    finished = pyqtSignal(object)
    failed = pyqtSignal(object)
    drained = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.queue = []
        self.current = None
        self.volume = None
        self.player = SimpleNamespace(position=lambda: 0)

    def set_volume(self, v):
        self.volume = v

    @property
    def active(self):
        return self.current is not None or bool(self.queue)

    def enqueue(self, audio_id, index, text, wav, mouth):
        self.queue.append(Clip(audio_id, index, text, "", mouth))

    def play_next(self):
        self.current = self.queue.pop(0)
        self.started.emit(self.current)
        return self.current

    def finish(self):
        clip, self.current = self.current, None
        self.finished.emit(clip)
        if not self.queue:
            self.drained.emit()

    def stop(self):
        self.queue.clear()
        clip, self.current = self.current, None
        return clip


class FakeSubtitles:
    item_url = "http://127.0.0.1:18676/" + "0" * 48 + "/"

    def __init__(self):
        self.states = []
        self.style = None

    def set_state(self, state, text=None, progress=None):
        self.states.append((state, text))

    def set_style(self, *a):
        self.style = a
