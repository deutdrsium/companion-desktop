"""Adapt the P1 device session to the existing Qt desktop controller."""

import asyncio
import logging
import sys

from PyQt5.QtCore import QTimer, pyqtSignal

from ..avatar.local_vts import LocalVTS
from ..avatar.model_catalog import load_mapping
from ..avatar.motion import FRAME_PERIOD, cursor_offset
from ..avatar.procedural import ProceduralAvatar
from ..avatar.vts import VTSOutput
from ..session.client import DesktopSession, HttpTransport
from .legacy_backend import LegacyBackend

log = logging.getLogger(__name__)


def _vts_cursor(model_position):
    if sys.platform != "win32":
        return None
    from ctypes import byref, windll, wintypes

    user32 = windll.user32
    user32.FindWindowW.restype = wintypes.HWND
    handle = user32.FindWindowW(None, "VTube Studio")
    if not handle:
        return None
    window = wintypes.HWND(handle)
    rect, origin, cursor = wintypes.RECT(), wintypes.POINT(), wintypes.POINT()
    if (not user32.IsIconic(window) and user32.GetClientRect(window, byref(rect))
            and rect.right and rect.bottom and user32.ClientToScreen(window, byref(origin))
            and user32.GetCursorPos(byref(cursor))):
        # Read both the cursor and rectangle in the same native DPI coordinate space.
        return cursor_offset(cursor.x, cursor.y, origin.x, origin.y, rect.right, rect.bottom,
                             model_position["positionX"], model_position["positionY"])
    return None


class DeviceBackend(LegacyBackend):
    connection = pyqtSignal(object)
    utterance_cancelled = pyqtSignal(str)
    voice_revoked = pyqtSignal()

    def __init__(self, base, key, cfg, folder, *, tts=True, subtitle=False):
        super().__init__(base, key, cfg)
        self.loop = asyncio.new_event_loop()
        self.timer = QTimer(self)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self._pump)
        self.cursor_timer = QTimer(self)
        self.cursor_timer.setInterval(40)
        self.cursor_timer.timeout.connect(self._cursor)
        self.avatar = self.vts = self.catalog = None
        self.revision = 1
        self.cues = {}
        self.turn_ids = {}
        self.cancel_pending = set()
        self.tasks = []
        mapping = cfg["avatar"].get("mapping")
        if mapping:
            try:
                self.catalog = load_mapping(mapping)
                self.vts = LocalVTS(int(cfg["avatar"]["port"]), folder / "vts-token")
                output = VTSOutput(self.vts.request, owned=cfg["avatar"]["inputs"])
                self.avatar = ProceduralAvatar(output, wardrobe_report=self._report_wardrobe)
                self.avatar.set_enabled(False)
            except (OSError, ValueError):
                log.warning("Local avatar mapping unavailable")
        self.tts, self.subtitle = tts, subtitle
        self.had_voice = False
        self.history_ready = False
        self.history.connect(self._history_loaded)
        self.history_failed.connect(self._history_loaded)
        self.transport = None
        self.session = DesktopSession(self._transport, permissions=("speak", "avatar") if self.avatar else ("speak",),
                                      avatar=self.avatar)
        self.session.subscribe(self._event)

    async def _transport(self, operation, payload):
        endpoint = self.base + self.api + "/session"
        if self.transport is None or self.transport.endpoint != endpoint:
            # Allow for multi-second stalls on the host event loop.
            self.transport = HttpTransport(endpoint, self.key, timeout=30)
        return await self.transport(operation, payload)

    def _publish(self):
        self.revision += 1
        avatar = self.avatar.capabilities(self.revision) if self.avatar else {
            "revision": self.revision, "model_id": None, "asset_version": None,
            "avatar": {"actions": [], "inputs": [], "hotkeys": []}, "mouth": False,
        }
        self.session.publish_capabilities({**avatar, "observations": [], "games": [],
                                           "tts": self.tts, "subtitle": self.subtitle})

    def _report_wardrobe(self, body):
        self.session.report("wardrobe_applied", body, session=self.session.session)

    async def _frames(self):
        try:
            while True:
                before = self.loop.time()
                self.avatar.set_enabled(self.session.connection["state"] == "connected"
                                        and "avatar" in self.session.dispatcher.permissions
                                        and self.session.capabilities_published(self.revision)
                                        and self.avatar.output.observed is not None)
                await self.avatar.tick()
                await asyncio.sleep(max(0, FRAME_PERIOD - (self.loop.time() - before)))
        finally:
            await self.avatar.stop_wardrobe()

    def _pump(self):
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()

    async def _start(self):
        self._publish()
        self.session.start()
        if self.avatar:
            self.tasks.append(asyncio.create_task(self.vts.run(self.catalog, self.avatar, self._publish)))
            self.tasks.append(asyncio.create_task(self._frames()))

    def load_history(self):
        self.loop.create_task(self._start())
        self.timer.start()
        if self.avatar:
            self.cursor_timer.start()
        super().load_history()

    def reconnect_session(self):
        self.loop.call_soon(self.session.start)

    def _history_loaded(self, *_):
        self.history_ready = True
        if self.session.connection["state"] == "connected":
            self.connection.emit({"type": "connection", **self.session.connection})

    @property
    def can_speak(self):
        return self.session.capabilities["tts"]

    def chat(self, text, turn_id):
        self.cues.clear()
        self.turn_ids[turn_id] = None
        self.session.send_message(turn_id, text)

    def cancel(self, turn_id):
        if turn_id in self.turn_ids:
            server_id = self.turn_ids[turn_id]
            if server_id is None:
                self.cancel_pending.add(turn_id)
            else:
                self.session.cancel_turn(server_id)
        return "unknown"

    def _event(self, event):
        kind = event["type"]
        if kind == "connection":
            if event["state"] != "connected":
                self.cues.clear()
            if self.history_ready or event["state"] != "connected":
                self.connection.emit(event)
        elif kind == "capabilities":
            had_voice, self.had_voice = self.had_voice, event["tts"]
            if had_voice and not self.had_voice:
                self.voice_revoked.emit()
        elif kind == "utterance":
            if event["turn_id"] in self.turn_ids.values():
                self.cues.setdefault(event["turn_id"], []).append(event)
        elif kind == "cancel":
            self.cues.pop(event["utterance_id"], None)
            self.utterance_cancelled.emit(event["utterance_id"])
        elif kind == "turn.accepted":
            client_id, turn_id = event["client_msg_id"], event["turn_id"]
            self.turn_ids[client_id] = turn_id
            if client_id in self.cancel_pending:
                self.cancel_pending.remove(client_id)
                self.session.cancel_turn(turn_id)
            else:
                self.turn_accepted.emit(client_id)
        elif kind == "turn.completed":
            # P1 emits completion and the cue frames in the same exchange.
            self.loop.call_soon(self._complete, event)
        elif kind == "turn.failed":
            client_id = event["client_msg_id"]
            accepted = self.turn_ids.pop(client_id, None) is not None
            self.cancel_pending.discard(client_id)
            reason = event["reason"]
            if reason != "busy":
                reason = "interrupted" if accepted else "rejected"
            self.chat_failed.emit(client_id, reason, "")
        elif kind == "turn.cancelled" and event["status"] == "cancelled":
            for client_id, turn_id in tuple(self.turn_ids.items()):
                if turn_id == event["turn_id"]:
                    self.turn_ids.pop(client_id, None)
                    self.cues.pop(turn_id, None)
                    self.chat_failed.emit(client_id, "cancelled", "")

    def _complete(self, event):
        self.chat_done.emit(event["client_msg_id"], event["text"])
        self.turn_ids.pop(event["client_msg_id"], None)
        self.cues.pop(event["turn_id"], None)

    def take_cues(self, client_id):
        turn_id = self.turn_ids.get(client_id)
        return turn_id, self.cues.pop(turn_id, ())

    def _cursor(self):
        """Use the live VTS model origin, falling back to the cursor's screen."""
        from PyQt5.QtGui import QCursor, QGuiApplication

        observed = self.avatar.output.observed
        model = self.vts.model_position
        if observed is not None and model is not None and model[0] == observed.model_id:
            aim = _vts_cursor(model[1])
            if aim is not None:
                self.avatar.pointer(aim)
                return
        position = QCursor.pos()
        screen = QGuiApplication.screenAt(position)
        if screen is None:
            self.avatar.pointer(None)
            return
        rect = screen.geometry()
        self.avatar.pointer(cursor_offset(position.x(), position.y(), rect.x(), rect.y(),
                                          rect.width(), rect.height()))

    def close(self, generation):
        self.timer.stop()
        self.cursor_timer.stop()
        if not self.loop.is_closed():
            async def finish():
                for task in self.tasks:
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
                await self.session.close()

            self.loop.run_until_complete(finish())
            self.loop.close()
        super().close(generation)
