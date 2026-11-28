"""Own one explicit, short-lived Stardew co-op decision window."""

import asyncio
from _thread import LockType
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from uuid import uuid4

from ...contracts.wire import MAX_GAME_TILES, MAX_GAME_WINDOW_MS, fields, game_id, one_of, tile_coordinate
from ...session.dispatcher import OutputResult
from .mcp_client import McpClient, McpError, McpWriteCancelled, loopback_endpoint
from .readback import CoopState, confirmed, legacy_map, parse_state


_PUBLIC = {"greet": "Hi!", "acknowledge": "Got it.", "ready": "I'm ready.", "thanks": "Thanks!"}
_EMOTES = ("happy", "heart", "yes", "no", "hi")
_POINT = {"map": game_id, "x": tile_coordinate, "y": tile_coordinate}
_PARAMS = {
    "move_to": _POINT,
    "use_tool": {**_POINT, "map": one_of("Farm"), "tool": one_of("watering_can")},
    "emote": {"emote": one_of(*_EMOTES)},
    "say_public": {"category": one_of(*_PUBLIC)},
    "cancel_action": {},
}
_ENUMS = {
    "tool": ["watering_can"], "emote": list(_EMOTES), "category": list(_PUBLIC),
}
_UNKNOWN = OutputResult("unknown", "result_unknown")
_ACTION_REQUEST_TIMEOUT = MAX_GAME_WINDOW_MS / 1000 + 2


@dataclass
class _Window:
    binding: dict
    client: McpClient
    deadline: float
    game_id: str = field(default_factory=lambda: uuid4().hex)
    window_id: str = field(default_factory=lambda: uuid4().hex)
    aliases: dict = field(default_factory=dict)
    state: CoopState | None = None
    actions: tuple[str, ...] = ()
    seq: int = 0


@dataclass
class _Work:
    task_id: str
    window: _Window
    report: Callable[[OutputResult], None]
    task: asyncio.Task | None = None
    timer: asyncio.TimerHandle | None = None
    running_state: CoopState | None = None
    lock: LockType = field(default_factory=Lock)
    sent: bool = False
    blocked: bool = False


def _schema(action):
    properties = {}
    for key in _PARAMS[action]:
        if key in ("x", "y"):
            properties[key] = {"type": "integer", "minimum": 0, "maximum": 1023}
        else:
            properties[key] = {"type": "string"}
            if key in _ENUMS:
                properties[key]["enum"] = _ENUMS[key]
    if action == "use_tool":
        properties["map"]["enum"] = ["Farm"]
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


class StardewBridge:
    def __init__(self, url, watering_tiles=(), *, clock=monotonic, poll_interval=0.25):
        loopback_endpoint(url)
        if (not isinstance(watering_tiles, (list, tuple)) or len(watering_tiles) > MAX_GAME_TILES
                or any(not isinstance(tile, (list, tuple)) or len(tile) != 2
                       or not all(tile_coordinate(coord) for coord in tile) for tile in watering_tiles)):
            raise ValueError("invalid_watering_tiles")
        self.url = url
        self.allowed_tiles = {tuple(tile) for tile in watering_tiles}
        self.clock, self.poll_interval = clock, poll_interval
        self.session = None
        self._window = None
        self._watcher = self._timer = self._work = self._cleanup = None
        self._status = "idle"
        self._last_result = None
        self._legacy_map = None

    @property
    def status(self):
        window = self._window
        state = window.state if window is not None else None
        return {"state": self._status, "public_chat": bool(state and state.chat_safe and "say_public" in window.actions),
                "last_result": self._last_result, "map": state.local["map"] if state else self._legacy_map}

    def open(self, session, *, duration=MAX_GAME_WINDOW_MS / 1000):
        binding = session.game_state_session()
        if (binding is None or self._window is not None or self._cleanup is not None
                or not 0 < duration <= MAX_GAME_WINDOW_MS / 1000):
            return False
        self.session = session
        self._legacy_map = None
        window = self._window = _Window(dict(binding), McpClient(self.url), self.clock() + duration)
        self._status = "connecting"
        self._timer = asyncio.get_running_loop().call_later(duration, self.stop, "expired")
        self._watcher = asyncio.create_task(self._observe(window))
        return True

    def _current(self, window):
        return (self._window is window and self.clock() < window.deadline
                and self.session.game_state_session() == window.binding)

    async def _read(self, window, after_seq, *, update_aliases=False):
        result = await window.client.call("get_coop_state", {"after_seq": after_seq})
        aliases = window.aliases if update_aliases else dict(window.aliases)
        previous = window.state
        state = parse_state(result, after_seq, aliases, self.allowed_tiles,
                            upstream_session=previous.upstream_session if previous else None)
        if previous is not None and state is not None:
            if state.snapshot.get("farm_ref") != previous.snapshot.get("farm_ref"):
                return None
        return state

    def _publish(self, window, *, events=(), gap=False, closed=False):
        ttl = 0 if closed else max(0, int((window.deadline - self.clock()) * 1000))
        if not closed and not ttl:
            return
        window.seq += 1
        self.session.report_game_state({
            "game_session_id": window.game_id, "window_id": window.window_id, "window_revision": 1,
            "window_ttl_ms": ttl, "seq": window.seq, "snapshot": None if closed else window.state.snapshot,
            "chat_events": list(events), "gap": gap,
        }, session=window.binding)

    async def _observe(self, window):
        try:
            tools = await window.client.initialize()
            if not self._current(window):
                return
            if "get_coop_state" not in tools:
                if "map_info" in tools:
                    result = await window.client.call("map_info", {"section": "exits"})
                    if not self._current(window):
                        return
                    self._legacy_map = legacy_map(result)
                self.stop("observe_only")
                return
            state = await self._read(window, 0, update_aliases=True)
            if not self._current(window):
                return
            if state is None:
                self.stop("game_unavailable")
                return
            window.state = state
            actions = [name for name in _PARAMS if name in tools and "cancel_action" in tools]
            if not state.chat_safe and "say_public" in actions:
                actions.remove("say_public")
            if not self.allowed_tiles and "use_tool" in actions:
                actions.remove("use_tool")
            window.actions = tuple(actions)
            self._status = "active"
            self.session.publish_games([{"game_session_id": window.game_id, "capability_revision": 1,
                                         "actions": [{"name": name, "schema": _schema(name)} for name in actions]}])
            self.session.set_game_window(window.game_id, window.window_id, 1, deadline=window.deadline)
            self._publish(window)
            next_snapshot = self.clock() + 2
            while self._current(window):
                await asyncio.sleep(self.poll_interval)
                if not self._current(window):
                    break
                previous = window.state
                state = await self._read(window, previous.chat_seq, update_aliases=True)
                if not self._current(window):
                    break
                if state is None or state.local["farmer_id"] != previous.local["farmer_id"]:
                    self.stop("game_unavailable")
                    return
                window.state = state
                if state.chat_events or state.gap or self.clock() >= next_snapshot:
                    self._publish(window, events=state.chat_events, gap=state.gap)
                    next_snapshot = self.clock() + 2
        except McpError:
            if self._window is window:
                self.stop("mcp_unavailable")
        finally:
            if self._window is window:
                self.stop("expired" if self.clock() >= window.deadline else "disconnected")

    def _intent_current(self, window, intent, deadline, is_current):
        return (self._current(window) and self.clock() < deadline and is_current()
                and intent["game_session_id"] == window.game_id and intent["window_id"] == window.window_id
                and intent["window_revision"] == intent["capability_revision"] == 1)

    def start(self, intent, *, deadline, is_current, report):
        window, action, params = self._window, intent["action"], intent["params"]
        if window is None or not self._intent_current(window, intent, deadline, is_current):
            report(OutputResult("expired", "no_time_window"))
            return
        if action not in window.actions:
            report(OutputResult("unsupported", "missing_dependency"))
            return
        if not fields(params, _PARAMS[action]):
            report(OutputResult("rejected", "invalid_config"))
            return
        if action == "cancel_action":
            if self._work is None:
                report(OutputResult("rejected", "missing_dependency"))
            else:
                work = self._work
                work.report(self.cancel({"task_id": work.task_id}, "cancelled") or _UNKNOWN)
                if self._cleanup is None:
                    report(OutputResult("completed", evidence={"source": "stardew_queue", "ref": work.task_id}))
                else:
                    asyncio.create_task(self._report_cancel(self._cleanup, report))
            return
        if self._work is not None or self._cleanup is not None:
            report(OutputResult("rejected", "busy"))
            return
        work = self._work = _Work(intent["task_id"], window, report)
        deadline = min(deadline, window.deadline)
        work.timer = asyncio.get_running_loop().call_later(max(0, deadline - self.clock()), self._expire_work, work)
        work.task = asyncio.create_task(self._execute(window, work, intent, deadline, is_current))

    def _expire_work(self, work):
        if self._work is work:
            self.stop("expired")

    def _finish_work(self, work, result):
        work.timer.cancel()
        self._work = None
        self._last_result = result.status
        work.report(result)

    async def _report_cancel(self, cleanup, report):
        report(await asyncio.shield(cleanup))

    def _arguments(self, state, action, params):
        if state.action_running is True:
            return None
        if action == "say_public":
            return {"text": _PUBLIC[params["category"]]} if state.chat_safe else None
        if action == "emote":
            return params
        x, y = params["x"], params["y"]
        local = state.local
        distance = abs(local["x"] - x) + abs(local["y"] - y)
        if local["map"] != params["map"] or distance > 12:
            return None
        if action == "use_tool":
            if not any(tile == {"x": x, "y": y, "watered": False} for tile in state.snapshot["watering_tiles"]):
                return None
            return {"x": x, "y": y, "tool": "watering_can", "times": 1}
        return {"x": x, "y": y} if distance else None

    async def _execute(self, window, work, intent, deadline, is_current):
        action, params = intent["action"], intent["params"]

        def current():
            return self._intent_current(window, intent, deadline, is_current)

        def before_write(writing_body):
            with work.lock:
                if work.blocked or not current():
                    return False
                if writing_body:
                    work.sent = True
                return True

        try:
            before = await self._read(window, window.state.chat_seq)
            if not current():
                return
            if before is None or before.local["farmer_id"] != window.state.local["farmer_id"]:
                self.stop("game_unavailable")
                return
            arguments = self._arguments(before, action, params)
            if arguments is None:
                self._finish_work(work, OutputResult("rejected", "invalid_config"))
                return
            work.report(OutputResult("running"))
            await window.client.call(action, arguments, timeout=_ACTION_REQUEST_TIMEOUT, before_write=before_write)
            while current():
                after = await self._read(window, before.chat_seq)
                if not current():
                    return
                if after is None or after.local["farmer_id"] != before.local["farmer_id"]:
                    self.stop("game_unavailable")
                    return
                verified = confirmed(action, params, before, after, public_text=arguments.get("text"))
                stopped = after.action_running is False
                synchronous = after.action_running is None and work.running_state is None and verified
                if stopped or synchronous:
                    result = (OutputResult("completed", evidence={"source": "stardew_readback", "ref": uuid4().hex})
                              if verified else _UNKNOWN)
                    self._finish_work(work, result)
                    return
                if after.action_running is True:
                    work.running_state = after
                await asyncio.sleep(self.poll_interval)
        except McpError as error:
            if self._window is window:
                if isinstance(error, McpWriteCancelled) and self.clock() >= deadline:
                    self.stop("expired")
                else:
                    self.stop("mcp_unavailable")

    def cancel(self, intent, reason):
        work = self._work
        if work is None or work.task_id != intent["task_id"]:
            return None
        self._work = None
        self._cancel_work(work.window, work)
        if reason == "no_time_window" and self._window is work.window:
            self.stop("expired")
        if work.sent:
            self._last_result = "unknown"
            return None
        self._last_result = "cancelled"
        return OutputResult("cancelled", reason, {"source": "stardew_queue", "ref": work.task_id})

    def _cancel_work(self, window, work):
        with work.lock:
            work.blocked = True
        work.timer.cancel()
        if work.task is not asyncio.current_task():
            work.task.cancel()
        if work.sent:
            self._cleanup = asyncio.create_task(self._cancel_and_read(window, work.running_state))

    async def _cancel_and_read(self, window, running_state):
        try:
            before = running_state or window.state
            await window.client.cancel_action()
            if self._current(window):
                after = await self._read(window, before.chat_seq)
                if confirmed("cancel_action", {}, before, after):
                    return OutputResult("completed", evidence={"source": "stardew_readback", "ref": uuid4().hex})
        except McpError:
            pass
        finally:
            self._cleanup = None
        return _UNKNOWN

    def stop(self, reason="idle", *, retiring=False):
        window = self._window
        if reason != "observe_only":
            self._legacy_map = None
        if window is None:
            if reason == "idle":
                self._status = reason
            return
        self._window = None
        watcher, self._watcher = self._watcher, None
        if watcher is not asyncio.current_task():
            watcher.cancel()
        self._timer.cancel()
        self._timer = None
        self._status = reason
        if window.seq and not retiring:
            self._publish(window, closed=True)
        self.session.set_game_window(window.game_id, None, 1, deadline=0)
        if self._work is not None and self._work.window is window:
            self.cancel({"task_id": self._work.task_id}, reason)
        if window.state is not None:
            if retiring:
                asyncio.get_running_loop().call_soon(self._withdraw)
            else:
                self._withdraw()

    def _withdraw(self):
        if self._window is None:
            self.session.publish_games([])

    def release(self, reason):
        self.stop(reason, retiring=True)

    async def close(self):
        self.stop()
        if self._cleanup is not None:
            await asyncio.shield(self._cleanup)
