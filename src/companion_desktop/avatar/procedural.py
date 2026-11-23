"""P1 avatar output and local 30 Hz driver for procedural animation."""

import asyncio
from collections import deque
from dataclasses import dataclass
from math import isfinite
from time import monotonic
from typing import Callable

from companion_desktop.session.dispatcher import OutputResult
from .model_actions import StaleOutputError
from .motion import FRAME_PERIOD, GESTURE_SECONDS, LAUGH, PAUSE_GAP, Motion, unit


@dataclass
class _Gesture:
    task_id: str
    started: float
    duration: float
    strength: float
    deadline: float
    is_current: Callable[[], bool]
    report: Callable[[OutputResult], None]
    rendered: bool = False


def _effect_ref(task_id):
    # Evidence of this scheduler's curve, not a claim about the rendered image.
    return {"source": "local_motion", "ref": task_id}


class ProceduralAvatar:
    """One event-loop-owned local writer, explicitly attached by the desktop.

    The host lease controls gestures/snapshots, not local idle. Local takeover
    disables all injection; cancel/stop closes owned mouth on the next batch.
    No wardrobe, activity expression, or physics writes originate here.
    """

    def __init__(self, output, *, clock=monotonic, seed=None):
        self.output = output
        self.clock = clock
        self.motion = Motion(seed=seed)
        self.timings = deque(maxlen=900)  # (observed frame interval, batch time or None)
        self._gestures = {}
        self._generation = output.generation
        self._last_frame = None
        self._revision = 0
        self._enabled = True
        self._client_id = self._turn_id = None
        self._turn_deadline = 0.0

    def _supported(self):
        bindings = self.output.bindings
        actions = {}
        if "mouth_form" in bindings and (LAUGH.keys() - {"mouth_form"}) & bindings.keys():
            actions["hold_laugh"] = bindings["mouth_form"].input_id
        if "head_pitch" in bindings:
            actions["nod"] = bindings["head_pitch"].input_id
        return actions

    def capabilities(self, revision):
        observed = self.output.observed
        return {
            "revision": revision, "model_id": observed.model_id if observed else None,
            "asset_version": observed.asset_version if observed else None,
            "avatar": {
                "inputs": sorted(binding.input_id for binding in self.output.bindings.values()),
                "actions": [{"name": name, "binding": binding} for name, binding in self._supported().items()],
                "hotkeys": [],
            },
            "mouth": "mouth_open" in self.output.bindings,
        }

    def start(self, intent, *, deadline, is_current, report):
        self._sync_model()
        action = intent["action"]
        # P3 must align cue-bound motions to playback, not arrival time.
        if (action not in self._supported() or not self._enabled
                or intent.get("utterance_id") is not None or intent.get("segment_id") is not None):
            report(OutputResult("unsupported", "missing_dependency"))
            return
        if intent["model_id"] != self.output.observed.model_id:
            report(OutputResult("rejected", "stale_model"))
            return
        if action in self._gestures:
            report(OutputResult("rejected", "busy"))
            return
        intensity = intent["params"].get("intensity", 1.0)
        if type(intensity) not in (int, float) or not 0 < intensity <= 1:
            report(OutputResult("rejected", "invalid_config"))
            return
        now = self.clock()
        duration = GESTURE_SECONDS[action]
        if now + duration >= deadline:
            report(OutputResult("expired", "no_time_window"))
            return
        self._gestures[action] = _Gesture(intent["task_id"], now, duration, intensity, deadline, is_current, report)
        if action == "hold_laugh":
            self.motion.reply_smile_until = 0.0
        self._revision += 1
        report(OutputResult("accepted"))

    def cancel(self, intent, reason):
        action = intent["action"]
        gesture = self._gestures.get(action)
        if gesture is None or gesture.task_id != intent["task_id"]:
            return None
        del self._gestures[action]
        self._revision += 1
        return OutputResult("cancelled", reason, _effect_ref(gesture.task_id))

    def _stop_gestures(self, reason):
        gestures, self._gestures = self._gestures, {}
        for gesture in gestures.values():
            gesture.report(OutputResult("cancelled", reason, _effect_ref(gesture.task_id)))

    def release(self, reason):
        self._revision += 1
        self._stop_gestures(reason)
        self._client_id = self._turn_id = None
        self.motion.clear_reflexes()
        if reason in {"unauthorized", "permissions_changed"}:
            self.motion.role_state = None

    def set_enabled(self, enabled):
        """Local handoff. No new writes are issued while another owner has control."""
        if self._enabled != enabled:
            self._enabled = enabled
            self.release("local_takeover")
            self.motion.reset_pose()

    def snapshot(self, kind, body, *, deadline):
        if kind == "role_state":
            self.motion.role_state = (body, deadline)

    def _sync_model(self):
        if self._generation != self.output.generation:
            self._generation = self.output.generation
            self.release("model_changed")
            self.motion.reset_pose()

    def handle_vts_event(self, kind, data):
        if kind == "ModelLoadedEvent":
            self.output.invalidate()
            self._sync_model()
        elif kind == "ModelClickedEvent" and self.output.observed is not None:
            if data["modelWasClicked"] and data["loadedModelID"] == self.output.observed.model_id:
                self.motion.click()

    def pointer(self, position):
        self._sync_model()
        self.motion.pointer(position)

    def set_typing(self, active):
        self._sync_model()
        self.motion.typing = bool(active)

    def mouth_level(self, level):
        """P3 supplies a normalized envelope of the currently playing clip."""
        self._sync_model()
        if self._enabled and isfinite(level):
            self.motion.mouth_target = unit(level)

    def stop_mouth(self):
        """Bypass smoothing on a playback stop or interruption."""
        self._revision += 1
        self.motion.stop_mouth()

    def message_sent(self, client_msg_id):
        self._sync_model()
        self.stop_mouth()
        self._client_id, self._turn_id = client_msg_id, None
        self._turn_deadline = self.clock() + 240
        self.motion.reply_smile_until = 0.0
        self.motion.thinking, self.motion.typing = True, False

    def session_event(self, event):
        kind = event["type"]
        self._sync_model()
        if kind == "connection" and event["state"] != "connected":
            self.release("disconnected")
        elif kind == "turn.accepted" and self._client_id is not None and event["client_msg_id"] == self._client_id:
            self._turn_id = event["turn_id"]
        elif kind in {"turn.completed", "turn.failed"}:
            if self._client_id is not None and event.get("client_msg_id") == self._client_id:
                self._client_id = None
                self.motion.thinking = False
                if kind == "turn.completed" and event["text"].strip():
                    if "hold_laugh" not in self._gestures:
                        self.motion.reply_smile_until = self.clock() + GESTURE_SECONDS["hold_laugh"]
                else:
                    self._turn_id = None
                    self.stop_mouth()
        elif kind == "turn.cancelled":
            self.cancel_turn(event["turn_id"])

    def cancel_turn(self, turn_id):
        if self._turn_id is not None and self._turn_id == turn_id:
            self._client_id = self._turn_id = None
            self.motion.thinking = False
            self.motion.reply_smile_until = 0.0
            self.stop_mouth()

    async def tick(self):
        """Sample once. Calls are serialized by run(), never queued to catch up."""
        self._sync_model()
        now = self.clock()
        elapsed = 0.0 if self._last_frame is None else now - self._last_frame
        self._last_frame = now
        if elapsed >= PAUSE_GAP:
            self._stop_gestures("frame_gap")
        if self._client_id is not None and now >= self._turn_deadline:
            self._client_id = self._turn_id = None
            self.motion.thinking = False
        for action, gesture in tuple(self._gestures.items()):
            if not gesture.is_current() or now >= gesture.deadline:
                del self._gestures[action]
        gestures = self._gestures.copy()
        frame = self.motion.advance(elapsed, now, gestures)
        batch_time = None
        if self._enabled and self.output.bindings:
            revision = self._revision
            generation = self.output.generation

            def current():
                return (self._revision == revision
                        and self.clock() - now < PAUSE_GAP
                        and all(gesture.is_current() for gesture in gestures.values()))

            before = self.clock()
            try:
                await self.output.write(frame, is_current=current)
            except StaleOutputError:
                pass
            except OSError:
                if self.output.generation == generation:
                    self.output.invalidate()
                self._sync_model()
            else:
                for action, gesture in gestures.items():
                    if self._gestures.get(action) is not gesture:
                        continue
                    age = now - gesture.started
                    if age >= gesture.duration:
                        del self._gestures[action]
                        status = "completed" if gesture.rendered else "cancelled"
                        reason = None if gesture.rendered else "frame_gap"
                        gesture.report(OutputResult(status, reason, _effect_ref(gesture.task_id)))
                    elif age >= FRAME_PERIOD and not gesture.rendered:
                        gesture.rendered = True
                        gesture.report(OutputResult("running"))
            finally:
                batch_time = self.clock() - before
        self.timings.append((elapsed, batch_time))
        return frame

    async def run(self):
        try:
            while True:
                start = self.clock()
                await self.tick()
                await asyncio.sleep(max(0, FRAME_PERIOD - (self.clock() - start)))
        finally:
            self.release("stopped")


class AnimatedSession:
    """UI-facing P1 Session wrapper; local input is not a new wire event."""

    def __init__(self, session, avatar):
        self.session, self.avatar = session, avatar
        self.detach = session.subscribe(avatar.session_event)

    @property
    def connection(self):
        return self.session.connection

    @property
    def capabilities(self):
        return self.session.capabilities

    def subscribe(self, listener):
        return self.session.subscribe(listener)

    def send_message(self, client_msg_id, text):
        self.avatar.message_sent(client_msg_id)
        self.session.send_message(client_msg_id, text)

    def cancel_turn(self, turn_id):
        self.avatar.cancel_turn(turn_id)
        self.session.cancel_turn(turn_id)
