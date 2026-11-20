"""Host-side desktop protocol. The embedding host authenticates every request."""

import asyncio
import hashlib
import json
import sqlite3
from collections import OrderedDict, deque
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol
from uuid import uuid4

from companion_desktop.contracts.types import PERMISSIONS, TERMINAL_STATUSES
from companion_desktop.contracts.wire import CLIENT_TYPES, ProtocolError, array, fields, message, negotiate, obj, parse, parse_ref, string
from companion_desktop.session.dispatcher import OutputResult, result_body, task_parameters, unavailable

BATCH_LIMIT = 32
LEASE_SECONDS = 20


@dataclass(frozen=True)
class Binding:
    device_id: str
    key: str
    permissions: frozenset[str]


@dataclass(frozen=True)
class TurnResult:
    status: str
    text: str = ""


class TurnBackend(Protocol):
    async def run(self, turn_id: str, text: str) -> TurnResult: ...

    async def cancel(self, turn_id: str) -> None:
        """Request a stop. Only run() can confirm termination."""


class _Claims:
    """Durable idempotency claims. Chat content belongs to the host conversation."""

    def __init__(self, path):
        self.path = path
        self.memory = {}

    def claim(self, binding, client_msg_id, fingerprint, turn_id):
        return self._record(binding, client_msg_id, (fingerprint, turn_id))

    def lookup(self, binding, client_msg_id):
        return self._record(binding, client_msg_id)

    def _record(self, binding, client_msg_id, proposed=None):
        if self.path is None:
            key = (binding, client_msg_id)
            return self.memory.setdefault(key, proposed) if proposed is not None else self.memory.get(key)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS desktop_turns (binding TEXT, client_id TEXT, "
                       "fingerprint TEXT NOT NULL, turn_id TEXT NOT NULL, PRIMARY KEY (binding, client_id))")
            if proposed is not None:
                db.execute("INSERT OR IGNORE INTO desktop_turns VALUES (?, ?, ?, ?)",
                           (binding, client_msg_id, *proposed))
            return db.execute("SELECT fingerprint, turn_id FROM desktop_turns WHERE binding=? AND client_id=?",
                              (binding, client_msg_id)).fetchone()


@dataclass
class _Turn:
    client_msg_id: str
    turn_id: str
    backend: TurnBackend
    started: bool = False
    stop_requested: bool = False
    result: TurnResult | None = None


@dataclass
class _Intent:
    body: dict
    deadline: float
    result: dict
    sent: bool = False


@dataclass
class _Connection:
    binding: Binding
    ref: dict
    permissions: frozenset[str]
    expires: float
    lease: asyncio.TimerHandle | None = None
    capabilities: dict | None = None
    windows: dict = field(default_factory=dict)
    intents: dict = field(default_factory=dict)
    observations: dict = field(default_factory=dict)
    pending: deque = field(default_factory=deque)
    snapshots: dict = field(default_factory=dict)
    wardrobe_receipt: dict | None = None


class SessionHost:
    def __init__(self, *, turn_claims_path=None, clock=monotonic, lease_seconds=LEASE_SECONDS):
        self.clock = clock
        self.lease_seconds = lease_seconds
        self._claims = _Claims(turn_claims_path)
        self._connections = {}
        self._retired_receipts = OrderedDict()
        self._generation = 0
        self._turns = OrderedDict()
        self._tasks = set()

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _refresh(self, connection):
        if connection.lease is not None:
            connection.lease.cancel()
        connection.expires = self.clock() + self.lease_seconds
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        connection.lease = loop.call_later(self.lease_seconds, self._retire, connection, "lease_expired")

    def _retire(self, connection, reason):
        if self._connections.get(connection.binding.device_id) is not connection:
            return
        if connection.lease is not None:
            connection.lease.cancel()
        del self._connections[connection.binding.device_id]
        for intent in connection.intents.values():
            self._invalidate(intent, reason)
        self._retired_receipts[self._ref_key(connection.ref)] = {
            task_id: intent.result for task_id, intent in connection.intents.items()}
        if len(self._retired_receipts) > 8:
            self._retired_receipts.popitem(last=False)
        connection.pending.clear()

    @staticmethod
    def _ref_key(ref):
        return ref["device_id"], ref["desktop_session_id"], ref["generation"]

    def _active(self, ref):
        connection = self._connections.get(ref["device_id"])
        if connection is None or connection.ref != ref:
            raise ProtocolError("stale_session")
        if self.clock() >= connection.expires:
            self._retire(connection, "lease_expired")
            raise ProtocolError("stale_session")
        return connection

    def _bound(self, binding, ref):
        connection = self._active(ref)
        if binding.key != connection.binding.key or binding.device_id != ref["device_id"]:
            raise ProtocolError("stale_session")
        permissions = binding.permissions & connection.binding.permissions
        removed = connection.permissions - permissions
        if removed:
            self._invalidate_scopes(connection, removed, "unauthorized")
            if "observe" in removed:
                connection.observations.clear()
            if "avatar" in removed:
                connection.wardrobe_receipt = None
            connection.snapshots.clear()
            if "speak" in removed:
                for (key, _), turn in self._turns.items():
                    if key == binding.key:
                        self._stop_turn(turn)
        connection.permissions = permissions
        return connection

    def revoke(self, device_id):
        connection = self._connections.get(device_id)
        if connection is not None:
            key = connection.binding.key
            self._retire(connection, "unauthorized")
            for (owner, _), turn in self._turns.items():
                if owner == key:
                    self._stop_turn(turn)

    def _register(self, binding, payload):
        if not fields(payload, {"protocols": array(string), "permissions": array(lambda item: item in PERMISSIONS)}):
            raise ProtocolError("invalid_request")
        protocol = negotiate(payload["protocols"])
        granted = binding.permissions & frozenset(payload["permissions"])
        previous = self._connections.get(binding.device_id)
        if previous is not None:
            if previous.binding.key != binding.key:
                self.revoke(binding.device_id)
            else:
                self._retire(previous, "session_replaced")
        if "speak" not in granted:
            for (key, _), turn in self._turns.items():
                if key == binding.key:
                    self._stop_turn(turn)
        self._generation += 1
        ref = {"device_id": binding.device_id, "desktop_session_id": uuid4().hex,
               "generation": self._generation}
        connection = _Connection(Binding(binding.device_id, binding.key, granted), ref, granted, 0)
        self._connections[binding.device_id] = connection
        self._refresh(connection)
        return {"protocol": protocol, "session": dict(ref), "permissions": sorted(granted)}

    def _update_capabilities(self, connection, body):
        current = connection.capabilities
        if current is not None and body["revision"] <= current["revision"]:
            if body != current:
                raise ProtocolError("stale_capabilities")
            return
        self._invalidate_scopes(connection, {"avatar", "game"}, "capabilities_changed")
        connection.capabilities = deepcopy(body)
        connection.windows.clear()
        if current is not None and body["observations"] != current["observations"]:
            connection.observations.clear()
        connection.snapshots.clear()
        connection.wardrobe_receipt = None

    def _invalidate(self, intent, reason, *, expired=False):
        if intent.result["status"] in TERMINAL_STATUSES:
            return
        if intent.sent:
            result = OutputResult("unknown", "result_unknown")
        else:
            result = OutputResult("expired" if expired else "cancelled", reason)
        intent.result = result_body(intent.body["task_id"], result)

    def _invalidate_scopes(self, connection, scopes, reason):
        for intent in connection.intents.values():
            if intent.body["scope"] in scopes:
                self._invalidate(intent, reason)

    def set_game_window(self, ref, game_session_id, window_id, window_revision, *, deadline):
        connection = self._active(ref)
        if window_id is None:
            connection.windows.pop(game_session_id, None)
        else:
            connection.windows[game_session_id] = (window_id, window_revision, deadline)
        for intent in connection.intents.values():
            if (intent.body.get("game_session_id") == game_session_id
                    and unavailable(intent.body, connection.capabilities, connection.permissions,
                                    connection.windows, self.clock()) is not None):
                self._invalidate(intent, "no_time_window", expired=True)
                if intent.sent and intent.result["status"] == "unknown":
                    self.cancel_intent(ref, task_id=intent.body["task_id"], reason_code="no_time_window")

    def current_session(self, device_id):
        connection = self._connections.get(device_id)
        if connection is None:
            return None
        try:
            return dict(self._active(connection.ref).ref)
        except ProtocolError:
            return None

    def wardrobe_applied(self, ref):
        return deepcopy(self._active(ref).wardrobe_receipt)

    def capabilities(self, ref):
        connection = self._active(ref)
        capabilities = deepcopy(connection.capabilities)
        if capabilities is None:
            return None
        if "avatar" not in connection.permissions:
            capabilities["avatar"] = {"actions": [], "inputs": [], "hotkeys": []}
            capabilities["mouth"] = False
        if "game" not in connection.permissions:
            capabilities["games"] = []
        if "observe" not in connection.permissions:
            capabilities["observations"] = []
        if "speak" not in connection.permissions:
            capabilities["tts"] = False
        return capabilities

    def send_intent(self, ref, body):
        body = deepcopy(message("intent", ref, body)["body"])
        connection = self._active(ref)
        task_id = body["task_id"]
        previous = connection.intents.get(task_id)
        if previous is not None:
            if task_parameters(previous.body) == task_parameters(body):
                return deepcopy(previous.result)
            return result_body(task_id, OutputResult("rejected", "invalid_config"))
        if len(connection.pending) >= BATCH_LIMIT or len(connection.intents) >= 1024:
            return result_body(task_id, OutputResult("rejected", "busy"))
        now = self.clock()
        blocked = unavailable(body, connection.capabilities, connection.permissions, connection.windows, now)
        if body["ttl_ms"] == 0 and blocked is None:
            blocked = OutputResult("expired", "no_time_window")
        result = result_body(task_id, blocked or OutputResult("queued"))
        connection.intents[task_id] = _Intent(body, now + body["ttl_ms"] / 1000, result)
        if blocked is None:
            connection.pending.append(("intent", task_id))
        return deepcopy(result)

    def cancel_intent(self, ref, *, reason_code="cancelled", **target):
        body = message("cancel", ref, {**target, "reason_code": reason_code})["body"]
        connection = self._active(ref)
        needs_delivery = False
        for intent in connection.intents.values():
            if not all(intent.body.get(key) == value for key, value in target.items()):
                continue
            if not intent.sent:
                self._invalidate(intent, reason_code)
            elif intent.result["status"] not in TERMINAL_STATUSES or intent.result["status"] == "unknown":
                needs_delivery = True
        if "utterance_id" in target:
            connection.pending = deque((kind, item) for kind, item in connection.pending
                                       if kind != "utterance" or item["body"]["utterance_id"] != target["utterance_id"])
            needs_delivery = "speak" in connection.permissions or needs_delivery
        if needs_delivery and ("cancel", body) not in connection.pending:
            if len(connection.pending) >= BATCH_LIMIT:
                raise ProtocolError("busy")
            connection.pending.appendleft(("cancel", body))

    def send_utterance(self, ref, body):
        frame = message("utterance", ref, deepcopy(body))
        connection = self._active(ref)
        if "speak" not in connection.permissions:
            raise ProtocolError("unauthorized")
        if len(connection.pending) >= BATCH_LIMIT:
            raise ProtocolError("busy")
        connection.pending.append(("utterance", frame))

    def send_observe(self, ref, body):
        frame = message("observe", ref, deepcopy(body))
        connection = self._active(ref)
        if "observe" not in connection.permissions:
            raise ProtocolError("unauthorized")
        if connection.capabilities is None or body["range"] not in connection.capabilities["observations"]:
            raise ProtocolError("missing_dependency")
        if len(connection.pending) >= BATCH_LIMIT:
            raise ProtocolError("busy")
        request_id = body["request_id"]
        if request_id in connection.observations:
            raise ProtocolError("invalid_config")
        deadline = self.clock() + body["ttl_ms"] / 1000
        connection.pending.append(("observe", (frame, deadline)))

    def _receipt(self, connection, body):
        intent = connection.intents.get(body["task_id"])
        if intent is None or not intent.sent:
            return
        current, incoming = intent.result["status"], body["status"]
        if current == "unknown":
            if incoming not in {"completed", "cancelled"} or body["evidence"] is None:
                return
        elif current in TERMINAL_STATUSES:
            return
        progress = {"queued": 0, "accepted": 1, "running": 2}
        if incoming in progress and progress[incoming] <= progress[current]:
            return
        if incoming == "cancelled" and body["evidence"] is None:
            return
        intent.result = deepcopy(body)

    def receipt(self, ref, task_id):
        try:
            connection = self._active(ref)
        except ProtocolError:
            return deepcopy(self._retired_receipts.get(self._ref_key(ref), {}).get(task_id))
        intent = connection.intents.get(task_id)
        if intent is None:
            return None
        if self.clock() >= intent.deadline:
            self._invalidate(intent, "no_time_window", expired=True)
        return deepcopy(intent.result)

    def _drain(self, connection):
        frames = []
        while connection.pending:
            kind, payload = connection.pending.popleft()
            if kind == "cancel":
                frames.append(message(kind, connection.ref, payload))
                continue
            if kind == "utterance":
                if "speak" in connection.permissions:
                    frames.append(payload)
                continue
            if kind == "observe":
                frame, deadline = payload
                remaining = int((deadline - self.clock()) * 1000)
                body = frame["body"]
                if (remaining > 0 and "observe" in connection.permissions
                        and body["range"] in connection.capabilities["observations"]
                        and body["request_id"] not in connection.observations):
                    connection.observations[body["request_id"]] = (body, deadline)
                    frames.append({**frame, "body": {**body, "ttl_ms": remaining}})
                continue
            intent = connection.intents[payload]
            if intent.result["status"] in TERMINAL_STATUSES:
                continue
            remaining = int((intent.deadline - self.clock()) * 1000)
            if remaining <= 0:
                self._invalidate(intent, "no_time_window", expired=True)
                continue
            intent.sent = True
            frames.append(message(kind, connection.ref, {**intent.body, "ttl_ms": remaining}))
        return frames

    def _trim_turns(self):
        while len(self._turns) > 256:
            old = next((key for key, value in self._turns.items() if value.result is not None), None)
            if old is None:
                return
            del self._turns[old]

    async def _run_turn(self, turn, text):
        if turn.stop_requested:
            turn.result = TurnResult("cancelled")
        else:
            turn.started = True
            try:
                turn.result = await turn.backend.run(turn.turn_id, text)
            except Exception:
                turn.result = TurnResult("unknown" if turn.stop_requested else "failed")
        self._trim_turns()

    async def _cancel_backend(self, turn):
        try:
            await turn.backend.cancel(turn.turn_id)
        except Exception:
            pass  # A stop request is not termination evidence.

    def _stop_turn(self, turn):
        if turn.result is None and not turn.stop_requested:
            turn.stop_requested = True
            if turn.started:
                self._spawn(self._cancel_backend(turn))

    def _send_turn(self, connection, body, backend):
        ref, key = connection.ref, connection.binding.key
        client_id, text = body["client_msg_id"], body["text"]
        if backend is None or "speak" not in connection.permissions or len(client_id) > 128 or len(text) > 4000:
            reason = "unauthorized" if backend is None or "speak" not in connection.permissions else "invalid_request"
            return message("turn.failed", ref, {"client_msg_id": client_id, "turn_id": client_id, "reason": reason})
        proposed = uuid4().hex
        fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        claim = self._claims.lookup(key, client_id)
        if claim is None:
            if any(owner == key and turn.result is None for (owner, _), turn in self._turns.items()):
                return message("turn.failed", ref, {"client_msg_id": client_id, "turn_id": client_id, "reason": "busy"})
            claim = self._claims.claim(key, client_id, fingerprint, proposed)
        original, turn_id = claim
        ids = {"client_msg_id": client_id, "turn_id": turn_id}
        if original != fingerprint:
            return message("turn.failed", ref, {**ids, "reason": "invalid_config"})
        if turn_id == proposed:
            turn = _Turn(client_id, turn_id, backend)
            self._turns[key, client_id] = turn
            self._spawn(self._run_turn(turn, text))
        return message("turn.accepted", ref, ids)

    def _turn_frame(self, connection, client_id):
        ref, key = connection.ref, connection.binding.key
        turn = self._turns.get((key, client_id))
        if turn is None:
            claim = self._claims.lookup(key, client_id)
            if claim is None:
                return None
            ids = {"client_msg_id": client_id, "turn_id": claim[1]}
            result = TurnResult("unknown")
        elif turn.result is None:
            return None
        else:
            ids = {"client_msg_id": client_id, "turn_id": turn.turn_id}
            result = turn.result
        if result.status == "completed":
            return message("turn.completed", ref, {**ids, "text": result.text})
        if result.status == "cancelled":
            return self._cancel_frame(ref, turn.turn_id, "cancelled")
        return message("turn.failed", ref, {**ids, "reason": "result_unknown" if result.status == "unknown" else "generation_failed"})

    @staticmethod
    def _cancel_frame(ref, turn_id, status):
        return message("turn.cancelled", ref, {"turn_id": turn_id, "status": status,
                       "reason": "cancelled" if status == "cancelled" else "result_unknown"})

    def _cancel_turn(self, connection, turn_id):
        turn = next((value for (key, _), value in self._turns.items()
                     if key == connection.binding.key and value.turn_id == turn_id), None)
        if turn is None or turn.result is not None:
            status = "cancelled" if turn is not None and turn.result.status == "cancelled" else "unknown"
            return self._cancel_frame(connection.ref, turn_id, status)
        self._stop_turn(turn)
        return self._cancel_frame(connection.ref, turn_id, "unknown")

    def _snapshots(self, connection, read):
        frames = []
        for kind, body in read(self.capabilities(connection.ref)):
            if kind not in {"wardrobe", "role_state"}:
                raise ProtocolError("invalid_snapshot")
            if kind == "wardrobe" and "avatar" not in connection.permissions:
                continue
            previous = connection.snapshots.get(kind, 0)
            frame = message(kind, connection.ref, body)
            if body["revision"] > previous:
                connection.snapshots[kind] = body["revision"]
                if kind == "wardrobe":
                    connection.wardrobe_receipt = None
                frames.append(frame)
        return frames

    def handle(self, binding, operation, payload, *, turns=None, snapshots=None, on_message=None):
        if operation == "register":
            return self._register(binding, payload)
        if operation not in {"exchange", "disconnect"} or not fields(payload, {
            "session": obj, "messages": array(obj), "turns": array(string),
        }) or max(len(payload["messages"]), len(payload["turns"])) > BATCH_LIMIT:
            raise ProtocolError("invalid_request")
        ref = parse_ref(payload["session"])
        connection = self._bound(binding, ref)
        incoming = [parse(frame) for frame in payload["messages"]]
        if any(frame["session"] != ref or frame["type"] not in CLIENT_TYPES for frame in incoming):
            raise ProtocolError("invalid_message_direction_or_session")
        replies = []
        requested = set(payload["turns"])
        for frame in incoming:
            kind, body = frame["type"], frame["body"]
            if kind == "receipt":
                self._receipt(connection, body)
            elif kind == "capabilities" and operation == "exchange":
                self._update_capabilities(connection, body)
            elif kind == "turn.send" and operation == "exchange":
                response = self._send_turn(connection, body, turns)
                replies.append(response)
                if response["type"] == "turn.accepted":
                    requested.add(body["client_msg_id"])
            elif kind == "turn.cancel" and operation == "exchange":
                replies.append(self._cancel_turn(connection, body["turn_id"]))
            elif kind == "wardrobe_applied" and "avatar" in connection.permissions:
                caps = connection.capabilities
                if (caps is not None and body["revision"] == connection.snapshots.get("wardrobe")
                        and all(body[key] == caps[key] for key in ("model_id", "asset_version"))):
                    connection.wardrobe_receipt = deepcopy(body)
            elif kind == "observation" and "observe" in connection.permissions:
                pending = connection.observations.get(body["request_id"])
                if pending is not None:
                    original, deadline = pending
                    if self.clock() < deadline and all(body[key] == original[key] for key in
                                                       ("trigger", "range", "permission_generation")):
                        del connection.observations[body["request_id"]]
                        if on_message is not None:
                            on_message(frame)
        if operation == "disconnect":
            self._retire(connection, "disconnected")
            return {}
        self._refresh(connection)
        now = self.clock()
        for request_id, (_, deadline) in tuple(connection.observations.items()):
            if now >= deadline:
                del connection.observations[request_id]
        for client_id in sorted(requested) if "speak" in connection.permissions else ():
            frame = self._turn_frame(connection, client_id)
            if frame is not None:
                replies.append(frame)
        if connection.capabilities is not None and snapshots is not None:
            replies.extend(self._snapshots(connection, snapshots))
        replies.extend(self._drain(connection))
        return {"messages": replies, "permissions": sorted(connection.permissions)}
