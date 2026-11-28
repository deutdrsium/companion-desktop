"""UI-facing session events, shared by real and in-memory transports."""

import asyncio
import json
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from copy import deepcopy
from functools import partial
from http.client import HTTPConnection, HTTPSConnection
from math import ceil
from threading import Thread
from time import monotonic
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from companion_desktop.contracts.types import PERMISSIONS, PROTOCOL, Connection
from companion_desktop.contracts.wire import (
    HOST_TYPES, MAX_GAME_CHAT_EVENTS, MAX_REQUEST_BYTES, ProtocolError, array, encode_request, fields, message, obj,
    observation_state, parse, parse_ref,
)
from .dispatcher import Dispatcher


_GAME_GRANT_SECONDS = 10


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
        self._capabilities = {"tts": False, "mouth": False, "subtitle": False, "observe_scopes": [],
                              "permissions": None, "published_revision": None}

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

    def _set_capabilities(self, *, tts=False, mouth=False, subtitle=False, observe_scopes=(),
                          permissions=None, published_revision=None):
        capabilities = {"tts": tts, "mouth": mouth, "subtitle": subtitle,
                        "observe_scopes": list(observe_scopes), "permissions": permissions,
                        "published_revision": published_revision}
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


class _GuardedConnection:
    def __init__(self, host, *, body, before_send, **kwargs):
        super().__init__(host, **kwargs)
        self.body = body
        self.before_send = before_send

    def send(self, data):
        if self.sock is None:
            if not self.before_send(False):
                raise ProtocolError("stale_glance")
            self.connect()
        # Opt-out blocks subsequent writes. A write already inside sendall can finish;
        # the host rejects stale revisions or disabled glances.
        if not self.before_send(data is self.body):
            raise ProtocolError("stale_glance")
        super().send(data)


class _GuardedHTTPConnection(_GuardedConnection, HTTPConnection):
    pass


class _GuardedHTTPSConnection(_GuardedConnection, HTTPSConnection):
    pass


class _GuardedHTTPHandler(HTTPHandler):
    def http_open(self, req):
        return self.do_open(partial(_GuardedHTTPConnection, body=req.data, before_send=req.before_send), req)


class _GuardedHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        connection = partial(_GuardedHTTPSConnection, body=req.data, before_send=req.before_send)
        return self.do_open(connection, req, context=self._context)


class HttpTransport:
    """POST to the authenticated plugin route without making exit wait for socket I/O."""

    def __init__(self, endpoint, credential, *, timeout=5):
        self.endpoint = endpoint
        self._credential = credential
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

    def _call(self, data, before_send=None):
        headers = {"Authorization": "Bearer " + self._credential, "Content-Type": "application/json"}
        req = Request(self.endpoint, data=data, headers=headers)
        opener = self._opener
        if before_send is not None:
            req.before_send = before_send
            handlers = [_NoRedirect(), _GuardedHTTPHandler(), _GuardedHTTPSHandler()]
            if req.type == "http":
                handlers.append(ProxyHandler({}))
            opener = build_opener(*handlers)
        try:
            with opener.open(req, timeout=self.timeout) as response:
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
        return await self.request(encode_request(operation, payload))

    async def request(self, data, *, before_send=None):
        result = Future()

        def run():
            if not result.set_running_or_notify_cancel():
                return
            try:
                result.set_result(self._call(data) if before_send is None else self._call(data, before_send))
            except Exception as error:
                result.set_exception(error)

        pending = asyncio.wrap_future(result)
        Thread(target=run, daemon=True, name="desktop-session-http").start()
        return await pending


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
        self._published_games = []
        self._game_state = None
        self._game_granted_at = None
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

    def granted_session(self, permission):
        if (permission in self._grant and self._connection["state"] == "connected"
                and not self._closing and not self._reconfigure):
            if permission == "game" and (self._game_granted_at is None
                                         or self._clock() - self._game_granted_at >= _GAME_GRANT_SECONDS):
                return None
            return self.session
        return None

    def capabilities_published(self, revision):
        return self.session is not None and self._published_revision == revision

    def start(self):
        if self._runner is None or self._runner.done():
            self._closing = False
            self._runner = asyncio.create_task(self._run())
        return self._runner

    def reconnect(self):
        if self.session is not None:
            self._reconfigure = True
        self._wake.set()
        self.start()

    def request_close(self):
        if not self._closing:
            self._closing = True
            if self._runner is not None:
                self._runner.cancel()

    async def close(self):
        self.request_close()
        runner = self._runner
        if runner is None:
            return
        try:
            await asyncio.shield(runner)
        except asyncio.CancelledError:
            if not runner.cancelled():
                raise

    def publish_capabilities(self, capabilities):
        body = deepcopy(capabilities)
        ref = self.session or {"device_id": "local", "desktop_session_id": "local", "generation": 1}
        frame = message("capabilities", ref, body)
        if len(encode_request("exchange", {"session": ref, "messages": [frame], "turns": []})) > MAX_REQUEST_BYTES:
            raise ProtocolError("invalid_request")
        if body == self._local_caps:
            return
        game_only = body.keys() == self._local_caps.keys() and all(
            value == self._local_caps[key] for key, value in body.items() if key != "games"
        )
        if body["revision"] <= self._local_caps["revision"] and self._published_revision and not game_only:
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

    def publish_games(self, games):
        self.publish_capabilities({**self._local_caps, "games": games})

    def report_game_state(self, body, *, session):
        if session is None or session != self.granted_session("game"):
            return False
        body = message("game.state", session, deepcopy(body))["body"]
        pending = self._game_state
        if (pending is not None and body["window_ttl_ms"] and pending["window_ttl_ms"]
                and body["game_session_id"] == pending["game_session_id"]
                and body["window_id"] == pending["window_id"] and body["window_revision"] == pending["window_revision"]):
            roster = {farmer["farmer_id"] for farmer in body["snapshot"]["farmers"]}
            events = {event["seq"]: event for event in pending["chat_events"] + body["chat_events"]}
            kept = [event for _, event in sorted(events.items()) if event["farmer_id"] in roster]
            body = {**body, "chat_events": kept[-MAX_GAME_CHAT_EVENTS:],
                    "gap": body["gap"] or pending["gap"] or len(events) != min(len(kept), MAX_GAME_CHAT_EVENTS)}
        self._game_state = body
        self._wake.set()
        return True

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
        response = await self._transport("register", {"protocols": [PROTOCOL], "permissions": sorted(requested),
                                                     "activate_on_exchange": True})
        if not fields(response, {"protocol": lambda item: item == PROTOCOL, "session": obj,
                                 "permissions": array(lambda item: item in PERMISSIONS)}):
            raise ProtocolError("invalid_response")
        self.session = parse_ref(response["session"])
        self._reconfigure = requested != self._permissions
        self._grant = frozenset(response["permissions"]) & self._permissions
        self._published_revision = 0
        self._published_games = []
        self.dispatcher.bind(self.session, self._grant)
        self.dispatcher.configure(self._local_caps, self._grant)

    def _drop(self, reason):
        self.dispatcher.disconnect(reason)
        self.session = None
        self._reports.clear()
        self._game_state = None
        self._game_granted_at = None
        self._grant = frozenset()
        self._set_capabilities()

    def _exchange_payload(self, operation, ref, caps):
        frames = []
        pending = list(self._active_turns)[:32] if operation == "exchange" else []
        payload = {"session": ref, "messages": frames, "turns": pending}

        def append(frame):
            # True queues the frame, False defers it, and None means it cannot fit alone.
            if len(frames) >= 32:
                return False
            frames.append(frame)
            if len(encode_request(operation, payload)) <= MAX_REQUEST_BYTES:
                return True
            if len(frames) > 1:
                frames.pop()
                return False
            if pending and len(encode_request(operation, {**payload, "turns": []})) <= MAX_REQUEST_BYTES:
                pending.clear()
                return True
            frames.pop()
            return None

        while self._reports:
            if append(self._reports[0]) is False:
                break
            self._reports.popleft()
        if operation == "exchange":
            game_state = self._game_state
            if game_state is not None and self.granted_session("game") != ref:
                self._game_state = game_state = None
            if game_state is not None and not game_state["window_ttl_ms"]:
                if append(message("game.state", ref, game_state)):
                    self._game_state = None
                game_state = None
            caps_pending = caps["revision"] != self._published_revision or caps["games"] != self._published_games
            caps_ready = not caps_pending or append(message("capabilities", ref, caps)) is True
            for kind, work in (("turn.cancel", self._cancels), ("turn.send", self._sends)):
                for key, value in tuple(work.items()):
                    body = {"turn_id": key} if kind == "turn.cancel" else {"client_msg_id": key, "text": value}
                    queued = append(message(kind, ref, body))
                    if queued is False:
                        break
                    if queued is None:
                        if kind == "turn.send":
                            self._sends.pop(key, None)
                            self._emit("turn.failed", client_msg_id=key, turn_id=None, reason="invalid_request")
                        else:
                            self._receive_turn("turn.cancelled", {"turn_id": key, "status": "unknown",
                                                                  "reason": "result_unknown"})
            if game_state is not None and caps_ready:
                window = self.dispatcher.game_windows.get(game_state["game_session_id"])
                if window is None or window[:2] != (game_state["window_id"], game_state["window_revision"]):
                    self._game_state = None
                else:
                    ttl = int((window[2] - self._clock()) * 1000)
                    if ttl > 0 and append(message("game.state", ref, {**game_state, "window_ttl_ms": ttl})):
                        self._game_state = None
        return payload

    async def _exchange(self, operation="exchange", *, attempt=0):
        ref, caps = self.session, self._local_caps
        payload = self._exchange_payload(operation, ref, caps)
        frames = payload["messages"]
        before = self._clock()
        response = await self._transport(operation, payload)
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
            self._published_games = caps["games"]
        self._grant = frozenset(response["permissions"]) & self._permissions
        self._game_granted_at = self._clock() if "game" in self._grant else None
        self.dispatcher.configure(self._local_caps, self._grant)
        self._set_capabilities(tts=caps.get("tts", False) and "speak" in self._grant,
                               mouth=caps.get("mouth", False) and "avatar" in self._grant,
                               subtitle=caps.get("subtitle", False),
                               observe_scopes=caps["observations"] if "observe" in self._grant else (),
                               permissions=sorted(self._grant), published_revision=self._published_revision)
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
        except asyncio.CancelledError:
            if not self._closing:
                raise
        finally:
            if self.session is not None:
                self.dispatcher.disconnect()
                await self._best_effort_disconnect()
            self._drop("closed")
            # Failure callbacks may try to dispatch the next queued message.
            self._set_connection("closed", reason=close_reason)
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

    async def _best_effort_disconnect(self):
        try:
            await asyncio.wait_for(self._exchange("disconnect"), timeout=0.5)
        except (ProtocolError, ConnectionError, TimeoutError, asyncio.TimeoutError):
            pass
