"""UI-facing session events, shared by real and in-memory transports."""

from collections.abc import Callable
from copy import deepcopy
from typing import Protocol
from uuid import uuid4

from companion_desktop.contracts.types import Connection


class Session(Protocol):
    @property
    def connection(self) -> Connection: ...

    @property
    def capabilities(self) -> dict: ...

    def subscribe(self, listener: Callable[[dict], None]) -> Callable[[], None]: ...

    def send_message(self, client_msg_id: str, text: str) -> None: ...

    def cancel_turn(self, turn_id: str) -> None: ...


class _SessionEvents:
    def __init__(self):
        self._listeners = []
        self._connection = {"state": "closed", "attempt": 0, "next_retry_ms": None,
                            "generation": None, "reason": None}
        self._capabilities = {"tts": False, "mouth": False, "subtitle": False, "observe_scopes": []}

    @property
    def connection(self):
        return deepcopy(self._connection)

    @property
    def capabilities(self):
        return deepcopy(self._capabilities)

    def subscribe(self, listener):
        self._listeners.append(listener)
        listener({"type": "connection", **self.connection})
        listener({"type": "capabilities", **self.capabilities})

        def unsubscribe():
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _emit(self, kind, **body):
        for listener in tuple(self._listeners):
            listener({"type": kind, **deepcopy(body)})

    def _set_connection(self, state, *, attempt=0, next_retry_ms=None, generation=None, reason=None):
        self._connection = {"state": state, "attempt": attempt, "next_retry_ms": next_retry_ms,
                            "generation": generation, "reason": reason}
        self._emit("connection", **self._connection)

    def _set_capabilities(self, *, tts=False, mouth=False, subtitle=False, observe_scopes=()):
        capabilities = {"tts": tts, "mouth": mouth, "subtitle": subtitle,
                        "observe_scopes": list(observe_scopes)}
        if capabilities != self._capabilities:
            self._capabilities = capabilities
            self._emit("capabilities", **self._capabilities)


class InMemorySession(_SessionEvents):
    """Deterministic UI fixture. Explicit calls advance replies and connectivity."""

    def __init__(self, *, capabilities=None):
        super().__init__()
        self._generation = 0
        self._turns = {}
        self._set_capabilities(**(capabilities or {}))

    def connect(self):
        self._generation += 1
        self._set_connection("connected", generation=self._generation)

    def reconnecting(self, *, attempt=1, next_retry_ms=1000, reason="connection_lost"):
        self._set_connection("reconnecting", attempt=attempt, next_retry_ms=next_retry_ms,
                             generation=self._generation or None, reason=reason)

    def close(self):
        self._set_connection("closed")

    def send_message(self, client_msg_id, text):
        if self.connection["state"] != "connected":
            self._emit("turn.failed", client_msg_id=client_msg_id, turn_id=None, reason="disconnected")
            return
        turn_id, original, events = self._turns.setdefault(client_msg_id, (uuid4().hex, text, []))
        if original != text:
            self._emit("turn.failed", client_msg_id=client_msg_id, turn_id=turn_id, reason="invalid_config")
            return
        self._emit("turn.accepted", client_msg_id=client_msg_id, turn_id=turn_id)
        for kind, body in events:
            self._emit(kind, **body)

    def _turn(self, turn_id):
        return next(((client_msg_id, events) for client_msg_id, (key, _, events) in self._turns.items()
                     if key == turn_id), (None, None))

    def reply(self, turn_id, text, *, seq=None):
        client_msg_id, events = self._turn(turn_id)
        if events is None or events:
            return
        if seq is not None:
            self._emit("turn.text", turn_id=turn_id, text=text, seq=seq)
            return
        body = {"client_msg_id": client_msg_id, "turn_id": turn_id, "text": text}
        events.append(("turn.completed", body))
        self._emit("turn.completed", **body)

    def cancel_turn(self, turn_id):
        _, events = self._turn(turn_id)
        if events is not None and not events:
            body = {"turn_id": turn_id, "status": "cancelled", "reason": "cancelled"}
            events.append(("turn.cancelled", body))
        elif events and events[-1][0] == "turn.cancelled":
            body = events[-1][1]
        else:
            body = {"turn_id": turn_id, "status": "unknown", "reason": "result_unknown"}
        self._emit("turn.cancelled", **body)
