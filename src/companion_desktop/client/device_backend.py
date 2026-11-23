"""Adapt the P1 device session to the existing Qt desktop controller."""

import asyncio
import logging

from PyQt5.QtCore import QTimer, pyqtSignal

from ..avatar.local_vts import LocalVTS
from ..avatar.model_catalog import load_mapping
from ..avatar.procedural import ProceduralAvatar
from ..avatar.vts import VTSOutput
from ..session.client import DesktopSession, HttpTransport
from .legacy_backend import LegacyBackend

log = logging.getLogger(__name__)


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
        self.cursor_timer.setInterval(80)
        self.cursor_timer.timeout.connect(self._cursor)
        self.avatar = self.vts = self.catalog = None
        self.revision = 1
        self.cues = {}
        self.turn_ids = {}
        self.cancel_pending = set()
        self.tasks = []
        mapping = cfg["avatar"].get("mapping")
        if mapping and cfg["avatar"]["inputs"]:
            try:
                self.catalog = load_mapping(mapping)
                self.vts = LocalVTS(int(cfg["avatar"]["port"]), folder / "vts-token")
                output = VTSOutput(self.vts.request, owned=cfg["avatar"]["inputs"])
                self.avatar = ProceduralAvatar(output)
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
            # The host loop can stall for seconds under load; a short timeout
            # turns that into a reconnect that cancels the reply in flight.
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

    async def _frames(self):
        from ..avatar.motion import FRAME_PERIOD

        while True:
            before = self.loop.time()
            self.avatar.set_enabled(self.session.connection["state"] == "connected"
                                    and "avatar" in self.session.dispatcher.permissions
                                    and self.session.capabilities_published(self.revision)
                                    and bool(self.avatar.output.bindings))
            await self.avatar.tick()
            await asyncio.sleep(max(0, FRAME_PERIOD - (self.loop.time() - before)))

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
            reason = "interrupted" if accepted else "rejected"
            self.chat_failed.emit(client_id, reason, "")
        elif kind == "turn.cancelled" and event["status"] == "cancelled":
            for client_id, turn_id in tuple(self.turn_ids.items()):
                if turn_id == event["turn_id"]:
                    self.turn_ids.pop(client_id, None)
                    self.cues.pop(turn_id, None)

    def _complete(self, event):
        self.chat_done.emit(event["client_msg_id"], event["text"])
        self.turn_ids.pop(event["client_msg_id"], None)
        self.cues.pop(event["turn_id"], None)

    def take_cues(self, client_id):
        turn_id = self.turn_ids.get(client_id)
        return turn_id, self.cues.pop(turn_id, ())

    def _cursor(self):
        """Feed the desktop cursor, relative to its screen centre, to gaze following."""
        from PyQt5.QtGui import QCursor, QGuiApplication

        position = QCursor.pos()
        screen = QGuiApplication.screenAt(position)
        if screen is None:
            return
        rect = screen.geometry()
        x = (position.x() - rect.center().x()) / max(1, rect.width() / 2)
        y = (position.y() - rect.center().y()) / max(1, rect.height() / 2)
        self.avatar.pointer((max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y))))

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
