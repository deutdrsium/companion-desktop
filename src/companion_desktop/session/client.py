"""UI-facing session events, shared by real and in-memory transports."""

import asyncio
import json
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from math import ceil
from time import monotonic
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from companion_desktop.contracts.types import PERMISSIONS, PROTOCOL, Connection
from companion_desktop.contracts.wire import (
    HOST_TYPES, ProtocolError, array, fields, message, obj, observation_state, parse, parse_ref,
)
from .dispatcher import Dispatcher


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

    def _observe_state(self, body):
        if not observation_state(body):
            raise ProtocolError("invalid_observation_state")
        self._emit("observe.state", **body)

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

    def observe_state(self, body):
        self._observe_state(body)

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


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    """POST to the authenticated plugin route using an injected existing credential."""

    def __init__(self, endpoint, credential, *, timeout=5):
        self.endpoint = endpoint
        self._credential = credential
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def _call(self, operation, payload):
        data = json.dumps({"operation": operation, "payload": payload}, ensure_ascii=False).encode("utf-8")
        req = Request(self.endpoint, data=data,
                      headers={"Authorization": "Bearer " + self._credential, "Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise ProtocolError({404: "unsupported_protocol"}.get(exc.code, "unauthorized")) from None
            raise ConnectionError("connection_lost") from None
        except (OSError, URLError, ValueError) as exc:
            raise ConnectionError("connection_lost") from exc
        if not isinstance(result, dict):
            raise ProtocolError("invalid_response")
        if result.get("status") != "ok":
            raise ProtocolError(result.get("reason") or "unsupported_protocol")
        return result.get("data")

    async def __call__(self, operation, payload):
        return await asyncio.to_thread(self._call, operation, payload)


class DesktopSession(_SessionEvents):
    """One event-loop-owned device session. Transport and outputs are injectable."""

    def __init__(self, transport, *, permissions=(), avatar=None, game=None, poll_interval=0.25,
                 clock=monotonic):
        super().__init__()
        self._transport = transport
        self._permissions = frozenset(permissions)
        self._clock = clock
        self._poll_interval = poll_interval
        self.dispatcher = Dispatcher(self._output_report, avatar=avatar, game=game, clock=clock)
        self.session = None
        self._grant = frozenset()
        self._local_caps = {"revision": 1, "model_id": None, "asset_version": None,
                            "avatar": {"actions": [], "inputs": [], "hotkeys": []},
                            "observations": [], "games": []}
        self._published_revision = 0
        self._reports = deque()
        self._sends = {}
        self._active_turns = {}
        self._cancels = {}
        self._settled = {}
        self._wake = asyncio.Event()
        self._runner = None
        self._closing = False
        self._reconfigure = False

    @property
    def device_capabilities(self):
        return deepcopy(self._local_caps)

    def start(self):
        if self._runner is None or self._runner.done():
            self._closing = False
            self._runner = asyncio.create_task(self._run())
        return self._runner

    async def close(self):
        if self._runner is not None:
            self._closing = True
            self.dispatcher.disconnect()
            self._wake.set()
            await self._runner

    def publish_capabilities(self, capabilities):
        body = deepcopy(capabilities)
        message("capabilities", {"device_id": "local", "desktop_session_id": "local", "generation": 1}, body)
        if body == self._local_caps:
            return
        if body["revision"] <= self._local_caps["revision"] and self._published_revision:
            raise ProtocolError("stale_capabilities")
        self._local_caps = body
        if self.session is not None:
            self.dispatcher.configure(body, self._grant)
        self._wake.set()

    def set_permissions(self, permissions):
        permissions = frozenset(permissions)
        if permissions != self._permissions:
            self._permissions = permissions
            self._grant &= permissions
            self.dispatcher.configure(self._local_caps, self._grant)
            self._reconfigure = self.session is not None
            self._wake.set()

    def set_game_window(self, game_session_id, window_id, window_revision, *, deadline):
        self.dispatcher.set_game_window(game_session_id, window_id, window_revision, deadline=deadline)

    def send_message(self, client_msg_id, text):
        previous = self._sends.get(client_msg_id) if isinstance(client_msg_id, str) else None
        if (not isinstance(client_msg_id, str) or not client_msg_id.strip() or len(client_msg_id) > 128
                or not isinstance(text, str) or not text.strip() or len(text) > 4000):
            reason = "invalid_request"
        elif previous is not None and previous != text:
            reason = "invalid_config"
        elif self._connection["state"] != "connected" or self._closing:
            reason = "disconnected"
        elif len(self._sends) >= 32 and previous is None:
            reason = "busy"
        else:
            self._sends[client_msg_id] = text
            self._wake.set()
            return
        self._emit("turn.failed", client_msg_id=client_msg_id, turn_id=None, reason=reason)

    def cancel_turn(self, turn_id):
        if self._connection["state"] != "connected" or self._closing:
            self._emit("turn.cancelled", turn_id=turn_id, status="unknown", reason="result_unknown")
            if turn_id not in self._active_turns.values() or self._closing:
                return
        self._cancels[turn_id] = None
        self._wake.set()

    def observe_state(self, body, *, session):
        if self.session is None or session != self.session or "observe" not in self._grant:
            return False
        self._observe_state(body)
        return True

    def report(self, kind, body, *, session):
        scope = {"wardrobe_applied": "avatar", "observation": "observe"}.get(kind)
        if self.session is None or session != self.session or scope not in self._grant:
            return False
        self._output_report(kind, body)
        if kind == "observation":
            self.observe_state({"request_id": body["request_id"], "source": body["trigger"], "phase": "done",
                                "result": body["status"], "captured_at": body["captured_at"],
                                "license_generation": body["permission_generation"]}, session=session)
        return True

    def _output_report(self, kind, body):
        if self.session is not None:
            self._reports.append(message(kind, self.session, deepcopy(body)))
            self._wake.set()

    async def _wait(self, seconds):
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _register(self):
        requested = self._permissions
        response = await self._transport("register", {"protocols": [PROTOCOL], "permissions": sorted(requested)})
        if not fields(response, {"protocol": lambda item: item == PROTOCOL, "session": obj,
                                 "permissions": array(lambda item: item in PERMISSIONS)}):
            raise ProtocolError("invalid_response")
        self.session = parse_ref(response["session"])
        self._reconfigure = requested != self._permissions
        self._grant = frozenset(response["permissions"]) & self._permissions
        self._published_revision = 0
        self.dispatcher.bind(self.session, self._grant)
        self.dispatcher.configure(self._local_caps, self._grant)

    def _drop(self, reason):
        self.dispatcher.disconnect(reason)
        self.session = None
        self._reports.clear()
        self._grant = frozenset()
        self._set_capabilities()

    async def _exchange(self, operation="exchange", *, attempt=0):
        ref = self.session
        frames = []
        count = min(len(self._reports), 32)
        for _ in range(count):
            frames.append(self._reports.popleft())
        caps = self._local_caps
        if operation == "exchange" and caps["revision"] != self._published_revision and len(frames) < 32:
            frames.append(message("capabilities", ref, caps))
        for kind, work in (("turn.cancel", self._cancels), ("turn.send", self._sends)):
            if operation != "exchange":
                break
            for key, value in work.items():
                if len(frames) >= 32:
                    break
                body = {"turn_id": key} if kind == "turn.cancel" else {"client_msg_id": key, "text": value}
                frames.append(message(kind, ref, body))
        before = self._clock()
        response = await self._transport(operation, {"session": ref, "messages": frames,
                                          "turns": list(self._active_turns)[:32] if operation == "exchange" else []})
        if operation == "disconnect" or self._closing or self._reconfigure:
            return
        if not fields(response, {"messages": array(obj), "permissions": array(lambda item: item in PERMISSIONS)}):
            raise ProtocolError("invalid_response")
        elapsed_ms = ceil((self._clock() - before) * 1000)
        incoming = [parse(frame) for frame in response["messages"]]
        if any(frame["session"] != ref or frame["type"] not in HOST_TYPES for frame in incoming):
            raise ProtocolError("invalid_message_direction_or_session")
        if any(frame["type"] == "capabilities" for frame in frames):
            self._published_revision = caps["revision"]
        self._grant = frozenset(response["permissions"]) & self._permissions
        self.dispatcher.configure(self._local_caps, self._grant)
        self._set_capabilities(tts=caps.get("tts", False) and "speak" in self._grant,
                               mouth=caps.get("mouth", False) and "avatar" in self._grant,
                               subtitle=caps.get("subtitle", False),
                               observe_scopes=caps["observations"] if "observe" in self._grant else ())
        if self._connection["state"] != "connected":
            self._set_connection("connected", attempt=attempt, generation=ref["generation"])
        if "speak" not in self._grant:
            for client_id, turn_id in tuple(self._active_turns.items()):
                self._receive_turn("turn.failed", {"client_msg_id": client_id, "turn_id": turn_id,
                                                   "reason": "result_unknown"})
        for frame in incoming:
            body = frame["body"]
            if "ttl_ms" in body:
                body = {**body, "ttl_ms": max(0, body["ttl_ms"] - elapsed_ms)}
                frame = {**frame, "body": body}
            kind = frame["type"]
            if kind.startswith("turn."):
                self._receive_turn(kind, body)
            elif kind == "observe":
                if "observe" in self._grant and body["ttl_ms"] and body["range"] in self._local_caps["observations"]:
                    self.observe_state({"request_id": body["request_id"], "source": body["trigger"],
                                        "phase": "requested", "result": None, "captured_at": None,
                                        "license_generation": body["permission_generation"]}, session=ref)
                    self._emit(kind, session=ref, **body)
            elif kind == "utterance" and "speak" not in self._grant:
                continue
            elif not self.dispatcher.receive(frame) or (kind == "cancel" and "utterance_id" in body):
                self._emit(kind, session=ref, **body)

    def _receive_turn(self, kind, body):
        client_id, turn_id = body.get("client_msg_id"), body["turn_id"]
        if kind == "turn.accepted":
            self._sends.pop(client_id, None)
            if client_id in self._active_turns or turn_id in self._settled:
                return
            self._active_turns[client_id] = turn_id
        elif kind in {"turn.completed", "turn.failed"}:
            self._sends.pop(client_id, None)
            if kind == "turn.completed" or body["reason"] not in {"busy", "invalid_config", "invalid_request", "unauthorized"}:
                if turn_id in self._settled:
                    return
                self._active_turns.pop(client_id, None)
                self._settled[turn_id] = None
        elif kind == "turn.cancelled":
            self._cancels.pop(turn_id, None)
            if body["status"] == "cancelled":
                if turn_id in self._settled:
                    return
                client_id = next((key for key, value in self._active_turns.items() if value == turn_id), None)
                self._active_turns.pop(client_id, None)
                self._settled[turn_id] = None
        if len(self._settled) > 256:
            del self._settled[next(iter(self._settled))]
        self._emit(kind, **body)

    async def _run(self):
        attempt, close_reason = 0, None
        try:
            self._set_connection("connecting")
            while not self._closing:
                try:
                    if self._reconfigure:
                        self._reconfigure = False
                        self._drop("permissions_changed")
                        self._set_connection("connecting")
                    if self.session is None:
                        await self._register()
                    if self._closing:
                        break
                    if self._reconfigure:
                        continue
                    self.dispatcher.expire()
                    await self._exchange(attempt=attempt)
                    attempt = 0
                except (ProtocolError, ConnectionError, TimeoutError) as exc:
                    generation = self.session["generation"] if self.session else self._connection["generation"]
                    reason = str(exc) or "connection_lost"
                    self._drop(reason)
                    if reason in {"unauthorized", "unsupported_protocol", "disabled"}:
                        close_reason = reason
                        break
                    attempt += 1
                    retry_ms = min(500 * 2 ** min(attempt - 1, 5), 15_000)
                    self._set_connection("reconnecting", attempt=attempt, next_retry_ms=retry_ms,
                                         generation=generation, reason=reason)
                    self._wake.clear()
                    await self._wait(retry_ms / 1000)
                    continue
                if not self._closing:
                    await self._wait(self._poll_interval)
        finally:
            if self.session is not None:
                self.dispatcher.disconnect()
                await self._best_effort_disconnect()
            self._drop("closed")
            for client_id in tuple(self._sends):
                self._emit("turn.failed", client_msg_id=client_id, turn_id=None, reason="disconnected")
            self._sends.clear()
            for turn_id in tuple(self._cancels):
                self._emit("turn.cancelled", turn_id=turn_id, status="unknown", reason="result_unknown")
            self._cancels.clear()
            if close_reason is not None:
                for client_id, turn_id in tuple(self._active_turns.items()):
                    self._receive_turn("turn.failed", {"client_msg_id": client_id, "turn_id": turn_id,
                                                       "reason": "result_unknown"})
            self._set_connection("closed", reason=close_reason)

    async def _best_effort_disconnect(self):
        try:
            await self._exchange("disconnect")
        except (ProtocolError, ConnectionError, TimeoutError):
            pass
