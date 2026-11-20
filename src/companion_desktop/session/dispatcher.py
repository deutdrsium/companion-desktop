"""Validate and route scoped intents to the local avatar and game executors."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import json
from time import monotonic
from typing import Protocol

from companion_desktop.contracts.types import Evidence, TERMINAL_STATUSES
from companion_desktop.contracts.wire import now_utc, parse, receipt


@dataclass(frozen=True)
class OutputResult:
    status: str
    reason_code: str | None = None
    evidence: Evidence | None = None


class Output(Protocol):
    def start(self, intent: dict, *, deadline: float, is_current: Callable[[], bool],
              report: Callable[[OutputResult], None]) -> None:
        """Enqueue work. Check is_current immediately before any side effect."""

    def cancel(self, intent: dict, reason: str) -> OutputResult | None:
        """Stop queued work or return evidence of the effect's actual disposition."""

    def release(self, reason: str) -> None:
        """Release owned output on disconnect, revocation, or capability replacement."""


class AvatarOutput(Output, Protocol):
    def snapshot(self, kind: str, body: dict, *, deadline: float | None) -> None:
        """Consume current role state or the selected wardrobe for the current model."""


@dataclass
class _Task:
    intent: dict
    deadline: float
    result: dict
    started: bool = False
    active: bool = True


_FORWARD = {"queued": 0, "accepted": 1, "running": 2}


def result_body(task_id, result):
    return {"task_id": task_id, "status": result.status, "reason_code": result.reason_code,
            "evidence": result.evidence, "occurred_at": now_utc()}


def task_parameters(intent):
    body = {key: value for key, value in intent.items() if key != "ttl_ms"}
    return json.dumps(body, sort_keys=True, allow_nan=False)


def unavailable(intent, capabilities, permissions, game_windows, now):
    """Shared pre-delivery and pre-execution checks for an already parsed intent."""
    scope = intent["scope"]
    if scope not in permissions:
        return OutputResult("rejected", "unauthorized")
    if capabilities is None or intent["capability_revision"] != capabilities["revision"]:
        return OutputResult("rejected", "stale_capabilities")
    if scope == "avatar":
        if intent["model_id"] != capabilities["model_id"]:
            return OutputResult("rejected", "stale_model")
        actions = capabilities["avatar"]["actions"]
    else:
        game_id = intent["game_session_id"]
        game = next((item for item in capabilities["games"] if item["game_session_id"] == game_id), None)
        if game is None:
            return OutputResult("unsupported", "missing_dependency")
        window = game_windows.get(game_id)
        if (window is None or window[:2] != (intent["window_id"], intent["window_revision"])
                or now >= window[2]):
            return OutputResult("expired", "no_time_window")
        actions = game["actions"]
    if not any(item["name"] == intent["action"] for item in actions):
        return OutputResult("unsupported", "missing_dependency")
    return None


class Dispatcher:
    def __init__(self, emit, *, avatar: AvatarOutput | None = None, game: Output | None = None,
                 clock=monotonic):
        self.emit, self.clock = emit, clock
        self.outputs = {"avatar": avatar, "game": game}
        self.session = None
        self.permissions = frozenset()
        self.capabilities = None
        self.game_windows = {}
        self._tasks = {}
        self._snapshots = {}
        self._role_snapshot = None

    def bind(self, session, permissions):
        self.disconnect("session_replaced")
        self.session = deepcopy(session)
        self.permissions = frozenset(permissions)

    def configure(self, capabilities, permissions):
        permissions = frozenset(permissions)
        old = self.capabilities
        revision_changed = old is not None and old["revision"] != capabilities["revision"]
        changed = set(self.permissions - permissions)
        if revision_changed:
            changed.update(self.outputs)
            self.game_windows.clear()
        for scope in changed & self.outputs.keys():
            self._release(scope, "capabilities_changed" if revision_changed else "unauthorized")
        self.capabilities = deepcopy(capabilities)
        role = self._role_snapshot
        if "avatar" in permissions and ("avatar" not in self.permissions or revision_changed):
            avatar = self.outputs["avatar"]
            if avatar is not None and role is not None and self.clock() < role[1]:
                avatar.snapshot("role_state", deepcopy(role[0]), deadline=role[1])
        self.permissions = permissions
        if revision_changed or changed:
            self._snapshots.clear()

    def set_game_window(self, game_session_id, window_id, window_revision, *, deadline):
        if window_id is None:
            self.game_windows.pop(game_session_id, None)
        else:
            self.game_windows[game_session_id] = (window_id, window_revision, deadline)
        for task in tuple(self._tasks.values()):
            intent = task.intent
            if (task.active and intent.get("game_session_id") == game_session_id
                    and not self._current(task)):
                self._cancel_task(task, "no_time_window")

    def _current(self, task):
        return (task.active and self.session is not None and self.clock() < task.deadline
                and unavailable(task.intent, self.capabilities, self.permissions,
                                self.game_windows, self.clock()) is None)

    def _publish(self, task, result):
        body = result_body(task.intent["task_id"], result)
        if not receipt(body):
            raise ValueError("invalid output result")
        task.result = body
        if result.status in TERMINAL_STATUSES:
            task.active = False
        self.emit("receipt", deepcopy(body))

    def _report(self, task, result):
        if self._tasks.get(task.intent["task_id"]) is not task or not self._current(task):
            return
        if result.status in _FORWARD and _FORWARD[result.status] <= _FORWARD[task.result["status"]]:
            return
        if task.started and result.status == "cancelled" and result.evidence is None:
            result = OutputResult("unknown", "result_unknown")
        self._publish(task, result)

    def _start(self, intent):
        task_id = intent["task_id"]
        previous = self._tasks.get(task_id)
        if previous is not None:
            if task_parameters(previous.intent) == task_parameters(intent):
                self.emit("receipt", deepcopy(previous.result))
            else:
                self.emit("receipt", result_body(task_id, OutputResult("rejected", "invalid_config")))
            return
        task = _Task(deepcopy(intent), self.clock() + intent["ttl_ms"] / 1000, {})
        self._tasks[task_id] = task
        blocked = unavailable(intent, self.capabilities, self.permissions, self.game_windows, self.clock())
        output = self.outputs[intent["scope"]]
        if blocked is not None:
            self._publish(task, blocked)
        elif not self._current(task):
            self._publish(task, OutputResult("expired", "no_time_window"))
        elif output is None:
            self._publish(task, OutputResult("unsupported", "missing_dependency"))
        else:
            self._publish(task, OutputResult("queued"))
            task.started = True
            try:
                output.start(deepcopy(intent), deadline=task.deadline, is_current=lambda: self._current(task),
                             report=lambda result: self._report(task, result))
            except Exception:
                if task.active:
                    self._cancel_task(task, "output_failed")

    def _cancel_task(self, task, reason):
        task.active = False
        status = "expired" if reason == "no_time_window" else "cancelled"
        result = OutputResult(status, reason)
        if task.started:
            try:
                result = self.outputs[task.intent["scope"]].cancel(deepcopy(task.intent), reason)
            except Exception:
                result = None
            if (result is None or result.status not in {"completed", "cancelled"}
                    or result.evidence is None):
                result = OutputResult("unknown", "result_unknown")
        self._publish(task, result)

    def _release(self, scope, reason):
        for task in tuple(self._tasks.values()):
            if task.active and task.intent["scope"] == scope:
                self._cancel_task(task, reason)
        output = self.outputs[scope]
        if output is not None:
            output.release(reason)

    def expire(self):
        for task in tuple(self._tasks.values()):
            if task.active and self.clock() >= task.deadline:
                self._cancel_task(task, "no_time_window")

    def disconnect(self, reason="disconnected"):
        if self.session is not None:
            self.session = None
            for scope in self.outputs:
                self._release(scope, reason)
        self.capabilities = None
        self.permissions = frozenset()
        self.game_windows.clear()
        self._tasks.clear()
        self._snapshots.clear()
        self._role_snapshot = None

    def _snapshot(self, kind, body):
        avatar = self.outputs["avatar"]
        if kind == "wardrobe":
            capabilities = self.capabilities
            if (capabilities is None or (body["model_id"], body["asset_version"]) !=
                    (capabilities["model_id"], capabilities["asset_version"])):
                return
        if body["revision"] <= self._snapshots.get(kind, 0):
            return
        self._snapshots[kind] = body["revision"]
        deadline = None
        if kind == "role_state":
            deadline = self.clock() + body["ttl_ms"] / 1000
            self._role_snapshot = (deepcopy(body), deadline)
        if avatar is not None and "avatar" in self.permissions:
            avatar.snapshot(kind, deepcopy(body), deadline=deadline)

    def receive(self, frame):
        frame = parse(frame)
        if frame["session"] != self.session:
            return False
        kind, body = frame["type"], frame["body"]
        if kind == "intent":
            self._start(body)
        elif kind == "cancel":
            for task in tuple(self._tasks.values()):
                if (("task_id" in body and body["task_id"] == task.intent["task_id"])
                        or ("utterance_id" in body and body["utterance_id"] == task.intent.get("utterance_id"))):
                    if task.active:
                        self._cancel_task(task, body["reason_code"])
                    else:
                        self.emit("receipt", deepcopy(task.result))
        elif kind in {"role_state", "wardrobe"}:
            self._snapshot(kind, body)
        else:
            return False
        return True
