"""Consented media sessions and local reaction scheduling."""

import asyncio
from time import monotonic
from uuid import uuid4

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal

from ..contracts.lines import parse_reply
from ..contracts.wire import ProtocolError, utterance
from .media_sources import LocalPlayer, SystemMedia, TimedText, wire_text


SYNC_SECONDS = 60
TRACK_GAP_SECONDS = 4
MIN_GAP_MS = 1800
SYSTEM_TIMELINE_DRIFT_MS = 150
SESSION_ERRORS = {"unauthorized", "stale_session", "disabled", "unsupported_protocol"}
RETRY_ERRORS = {"connection_lost", "stale_media", "stale_revision", "invalid_response"}


class SharedMedia(QObject):
    state_changed = pyqtSignal(object)
    reaction = pyqtSignal(object)
    reaction_cancelled = pyqtSignal(str)
    tier = pyqtSignal(object)
    item_changed = pyqtSignal(object)
    sources_changed = pyqtSignal(object)
    error = pyqtSignal(str)
    _system_changed = pyqtSignal(str, str)

    def __init__(self, loop, request, granted_session, parent=None, *, clock=monotonic):
        super().__init__(parent)
        self.loop, self._request, self._granted_session, self.clock = loop, request, granted_session, clock
        self.local_player = LocalPlayer(self)
        self.local_player.changed.connect(self._read_local)
        self._system = SystemMedia()
        self._system_lock = asyncio.Lock()
        self._system_changed.connect(self._system_event, Qt.QueuedConnection)
        self._system_token = None
        self._system_serial = 0
        self._system_pending = False
        self._sources = ()
        self._poll_task = self._sync_task = self._comment_task = None
        self._timer = QTimer(self, interval=150)
        self._timer.timeout.connect(self._tick)
        self._ref = self._remote_id = self._desired_id = None
        self._uncertain = self._dirty = self._armed = False
        self._seq = -1
        self._epoch = 0
        self._source = self._kind = self._item = None
        self._loading_local = False
        self._seek_until, self._seek_origin = 0, None
        self._item_at = self._changed_at = self._sync_at = 0
        self._timeline = self._timeline_item = None
        self._pending = self._speech = None
        self._last_comment = self._last_movie_l1 = float("-inf")
        self._seen_reaction = None
        self._hot_at = float("-inf")
        self._pose = ("neutral", 0)
        self._credits = self._owner_speaking = False
        self.can_present = lambda: False
        self._state = {"type": "shared_media.state", "status": "idle", "media_id": None, "seq": -1}

    @property
    def state(self):
        return dict(self._state)

    @property
    def sources(self):
        return ({"id": "local_player", "source": "local_player", "label": "本地播放器"},
                *(dict(item) for item in self._sources))

    @property
    def current_item(self):
        return self._snapshot() if self._item is not None else None

    @property
    def active(self):
        """True between a successful ``start()`` and the end of sharing."""
        return self._armed

    @property
    def current_line(self):
        """The timed-text line at the current position, for local display only."""
        return self._context()[0] if self._armed else None

    def load_local(self, path, timed_text=None):
        timeline = TimedText.load(timed_text) if timed_text else None
        self._loading_local = True
        try:
            self.local_player.load(path)
            self._timeline, self._timeline_item = timeline, None
            self._seek_until = 0
        finally:
            self._loading_local = False
        self._read_local()

    def set_timed_text(self, path=None):
        self._timeline = TimedText.load(path) if path else None
        self._timeline_item = self._item.key if self._item else None
        self.interrupt_reaction()

    def seek_local(self, position_ms):
        player = self.local_player.player
        if self._armed and self._source == "local_player":
            self.interrupt_reaction()
            self.local_player.reset_level()
            self._seek_origin = player.position()
            self._seek_until = self.clock() + 0.75
        player.setPosition(position_ms)
        self._read_local()

    def refresh_sources(self):
        self._poll_system(discover=True)

    def select_system(self, source_id):
        if source_id not in {item["id"] for item in self._sources}:
            raise ValueError("unknown_media_source")
        if source_id != self._system_token:
            if self._armed and self._source == "system_media":
                self.stop()
            self._timeline = self._timeline_item = None
            self._system_token = source_id

    def start(self, source, kind):
        """Start after explicit user consent. The selected player keeps its own controls."""
        if source not in {"local_player", "system_media"} or kind not in {"music", "movie"}:
            raise ValueError("invalid_media_source_or_kind")
        if self._armed:
            return False
        ref = self._granted_session()
        if ref is None:
            self.error.emit("unauthorized")
            return False
        if ((source == "local_player" and self.local_player.snapshot() is None)
                or (source == "system_media" and self._system_token not in {item["id"] for item in self._sources})):
            self.error.emit("no_media")
            return False
        if ref != self._ref:
            self._adopt_session(ref)
        self._source, self._kind, self._armed = source, kind, True
        self._item = None
        self._owner_speaking = False
        if source == "system_media":
            self._system_serial += 1
            self._system_pending = True
            try:
                self._system.watch(self._system_token, self._system_changed.emit)
            except (OSError, RuntimeError):
                self.stop()
                self.error.emit("system_media_unavailable")
                return False
        self._timer.start()
        self._tick()
        return True

    def stop(self):
        """End sharing immediately. The UI decides whether to stop local playback."""
        if not self._armed and self._desired_id is None:
            return
        self._armed = False
        self._desired_id = self._item = None
        self._seek_until = 0
        self._system_pending = False
        self._timer.stop()
        self._system.unwatch()
        self.interrupt_reaction()
        self._set_pose("neutral", 0)
        self._idle()
        self.item_changed.emit(None)
        self._schedule_sync()

    async def close(self):
        tasks = tuple(task for task in (self._poll_task, self._sync_task, self._comment_task) if task is not None)
        self._adopt_session(None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._system.close()

    def session_changed(self):
        ref = self._granted_session()
        if ref != self._ref:
            self._adopt_session(ref)

    def _adopt_session(self, ref):
        self._ref = ref
        if self._sync_task is not None:
            self._sync_task.cancel()
            self._sync_task = None
        self._remote_id, self._seq, self._uncertain = None, -1, False
        self._seen_reaction = None
        self.stop()
        self._idle()

    def interrupt_reaction(self):
        self._epoch += 1
        self._pending = None
        if self._comment_task is not None:
            self._comment_task.cancel()
            self._comment_task = None
        if self._speech is not None:
            reaction_id = self._speech[0]
            self._speech = None
            self.reaction_cancelled.emit(reaction_id)

    def set_owner_speaking(self, active):
        if not self._armed or bool(active) == self._owner_speaking:
            return
        self._owner_speaking = bool(active)
        self.interrupt_reaction()
        if not active and self._kind == "movie":
            self._offer("L2", "owner_speech")

    def mark_credits(self):
        if self._armed and self._kind == "movie" and not self._credits:
            self._credits = True
            self._offer("L2", "credits")

    def _idle(self):
        self._set_state({"status": "idle", "media_id": None, "seq": self._seq})

    def _set_state(self, state):
        event = {**state, "type": "shared_media.state"}
        if self._state != event:
            self._state = event
            self.state_changed.emit(dict(event))

    def _tick(self):
        self.session_changed()
        if not self._armed:
            return
        if self._source == "local_player":
            self._read_local()
        else:
            self._poll_system(discover=False)
        if self._speech is not None and not self._system_pending and not self._valid(*self._speech[1:]):
            self.interrupt_reaction()
        self._schedule_sync()

    def _read_local(self):
        if self._armed and self._source == "local_player" and not self._loading_local:
            if self._seek_until:
                if self.local_player.player.position() == self._seek_origin and self.clock() < self._seek_until:
                    return
                self._seek_until = 0
                self._dirty = True
            self._observe(self.local_player.snapshot())

    def _poll_system(self, *, discover):
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = self.loop.create_task(self._read_system(discover=discover))

    async def _read_system(self, *, discover=False):
        async with self._system_lock:
            await self._read_system_locked(discover)

    async def _read_system_locked(self, discover):
        ref, token = self._ref, self._system_token
        try:
            if discover:
                found = await self._system.discover()
                self._sources = tuple({**item, "source": "system_media"} for item in found)
                self.sources_changed.emit(self.sources)
            if self._armed and self._source == "system_media":
                serial = self._system_serial
                item = await self._system.snapshot(token)
                if (self._armed and ref == self._ref and token == self._system_token
                        and serial == self._system_serial):
                    self._system_pending = False
                    self._observe(item)
        except (ImportError, OSError, RuntimeError, TimeoutError):
            if discover:
                self.error.emit("system_media_unavailable")
            elif self._armed and ref == self._ref and token == self._system_token:
                self._system_pending = False
                self._observe(None)

    def _system_event(self, token, kind):
        if not self._armed or self._source != "system_media" or self._system_token != token:
            return
        self._system_serial += 1
        self._system_pending = True
        if kind == "media":
            self.interrupt_reaction()
        self._poll_system(discover=False)

    def _observe(self, item):
        previous, self._item = self._item, item
        now = self.clock()
        changed = (previous.key if previous else None) != (item.key if item else None)
        paused = previous is not None and item is not None and previous.status != item.status
        seeked = False
        if not changed and not paused and previous is not None and item is not None:
            if item.position_ms is not None and previous.position_ms is not None:
                seeked = self._seeked(previous, item, now)
        if changed or paused or seeked or (previous and item and item.ended != previous.ended):
            self.interrupt_reaction()
            self._dirty = True
            if item is not None and (paused or seeked) and self._source == "local_player":
                self.local_player.reset_level()
            if changed:
                self._item_at = now
                self._credits = False
                self._hot_at = float("-inf")
                self._desired_id = uuid4().hex if item else None
                if item is None:
                    self._idle()
                if item is not None and self._timeline_item is None:
                    self._timeline_item = item.key
                if (item and self._kind == "music"
                        and (previous or (item.position_ms is not None and item.position_ms < 4000))):
                    self._offer("L1", "track_change")
            elif paused and item.status == "paused":
                self._offer("L2" if self._kind == "movie" else "L1", "pause")
            elif paused and self._kind == "music" and item.position_ms is not None and item.position_ms < 1000:
                self._offer("L1", "track_change")
        self._changed_at = now
        if item is not None:
            cue, gap, credits = self._context()
            if credits or (self._kind == "movie" and item.ended):
                self.mark_credits()
            if self._kind == "movie" and not self._credits and item.status == "playing":
                if ((item.loudness is not None and item.loudness >= 0.25)
                        or (cue and any(marker in cue.lower() for marker in ("[screams", "[gasps", "尖叫", "惊呼")))):
                    self._hot_at = now
                if (gap >= MIN_GAP_MS and now - self._hot_at < 6 and now - self._last_movie_l1 >= 300
                        and self._pending is None and self._comment_task is None):
                    self._offer("L1", "subtitle_gap")
        self._animate()
        if previous != item:
            self.item_changed.emit(self.current_item)
        self._schedule_sync()

    def _seeked(self, previous, item, now):
        delta = item.position_ms - previous.position_ms
        if item.status == "paused":
            return abs(delta) > 30
        elapsed_ms = (now - self._changed_at) * 1000
        if self._source != "system_media":
            return delta < -250 or delta > max(1000, 2 * elapsed_ms + 500)
        if item.sampled_at is not None and previous.sampled_at is not None:
            elapsed_ms = max(0, item.sampled_at - previous.sampled_at) * 1000
        expected = previous.position_ms + elapsed_ms * previous.playback_rate
        if item.duration_ms is not None:
            expected = min(expected, item.duration_ms)
        drift = item.position_ms - expected
        anchor_changed = item.timeline_key != previous.timeline_key
        return (delta < -30 or drift > SYSTEM_TIMELINE_DRIFT_MS
                or (anchor_changed and drift < -SYSTEM_TIMELINE_DRIFT_MS))

    def _context(self):
        if self._item and self._timeline and self._timeline_item == self._item.key:
            return self._timeline.context(self._item.position_ms)
        return None, 0, False

    def _snapshot(self):
        item = self._item
        media = {"kind": self._kind, "source": self._source, "status": item.status, "title": item.title,
                 "position_ms": item.position_ms, "duration_ms": item.duration_ms}
        if item.artist is not None:
            media["artist"] = item.artist
        if item.loudness is not None:
            media["loudness"] = item.loudness
        return media

    async def _send(self, operation, ref, payload):
        wire = {} if operation == "status" else {key: payload[key] for key in ("media_id", "seq")}
        if operation in ("start", "update"):
            item = payload["media"]
            media = {key: item[key] for key in ("kind", "source", "status")}
            media["title"] = wire_text(item["title"], 160) or "媒体"
            for key in ("position_ms", "duration_ms"):
                value = item.get(key)
                media[key] = value if type(value) is int and 0 <= value <= 2**53 - 1 else None
            if media["source"] == "system_media" and media["kind"] == "music":
                artist = wire_text(item.get("artist"), 120)
                if artist:
                    media["artist"] = artist
            if operation == "update" and media["source"] == "local_player":
                level = item.get("loudness")
                if type(level) in (int, float) and 0 <= level <= 1:
                    media["loudness"] = level
            wire["media"] = media
        elif operation == "comment":
            cue = wire_text(payload.get("cue"), 300)
            if cue:
                wire["cue"] = cue
        return await self._request(operation, ref, wire)

    def _set_pose(self, cue, strength):
        if (cue, strength) != self._pose:
            self._pose = cue, strength
            self.tier.emit({"type": "shared_media.tier", "tier": "L0", "cue": cue, "strength": strength})

    def _animate(self):
        item = self._item
        if not item or item.status != "playing" or not self.can_present() or self._owner_speaking:
            self._set_pose("neutral", 0)
        elif self._kind == "music":
            energy = min(1, round((item.loudness or 0) * 10) / 2)
            self._set_pose("nod", energy) if energy else self._set_pose("neutral", 0)
        else:
            cue, _, _ = self._context()
            cue = (cue or "").lower()
            if any(marker in cue for marker in ("laugh", "笑声", "大笑")):
                self._set_pose("smile", 0.7)
            elif self._credits or any(marker in cue for marker in ("sobs", "哭泣", "抽泣")):
                self._set_pose("solemn", 0.6)
            elif self.clock() - self._hot_at < 3:
                self._set_pose("lean_in", 0.65)
            else:
                self._set_pose("neutral", 0)

    def _offer(self, tier, reason):
        now = self.clock()
        if self._pending is None or tier == "L2":
            self._pending = self._epoch, tier, reason, now
            self.tier.emit({"type": "shared_media.tier", "tier": tier, "cue": reason, "strength": None})
            self._schedule_sync()

    def _eligible(self, tier, reason, at, *, presenting=False):
        item = self._item
        if (item is None or self._owner_speaking or self._seek_until
                or (self._source == "system_media" and self._system_pending)):
            return False
        age = self.clock() - at
        _, gap, _ = self._context()
        needed_gap = 250 if presenting else MIN_GAP_MS
        if self._kind == "music":
            if item.status == "paused":
                return presenting or (reason == "pause" and age < TRACK_GAP_SECONDS)
            if age >= TRACK_GAP_SECONDS or reason != "track_change":
                return False
            if self._timeline and self._timeline_item == item.key and item.position_ms is not None:
                return item.position_ms < 4000 and gap >= needed_gap
            return (item.position_ms is None or item.position_ms < 4000) and self.clock() - self._item_at < 4
        if tier == "L1":
            return item.status == "playing" and gap >= needed_gap and (presenting or age < 6)
        if reason == "pause":
            return item.status == "paused"
        return presenting or age < 12

    def _valid(self, seq, media_id, epoch, tier, reason, at):
        return (self._armed and self._ref == self._granted_session() and self._seq == seq
                and self._desired_id == media_id and self._epoch == epoch
                and self._eligible(tier, reason, at, presenting=True))

    def allow_audio(self, reaction_id):
        if self._source == "local_player":
            self._read_local()
        return (self._speech is not None and self._speech[0] == reaction_id
                and self._valid(*self._speech[1:]))

    def speech_finished(self, reaction_id):
        if self._speech is not None and self._speech[0] == reaction_id:
            self._speech = None

    def _schedule_sync(self):
        if self._ref is None or self._ref != self._granted_session():
            return
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = self.loop.create_task(self._sync(self._ref))

    async def _sync(self, ref):
        recovery = self._uncertain
        try:
            await self._sync_state(ref)
        except (ProtocolError, ConnectionError, TimeoutError) as exc:
            if ref != self._ref:
                return
            reason = str(exc) or "connection_lost"
            if reason in RETRY_ERRORS and not recovery:
                self._uncertain = True
                self.loop.call_soon(self._schedule_sync)
            else:
                self.error.emit(reason)
                self._uncertain = reason in RETRY_ERRORS
                self.stop()

    async def _sync_state(self, ref):
        while ref == self._ref == self._granted_session():
            if self._seek_until:
                return
            pending = self._pending
            if pending is not None and self.clock() - pending[-1] >= (4 if self._kind == "music" else 12):
                pending = self._pending = None
            comment = (pending is not None and self._comment_task is None and self._speech is None
                       and self.can_present() and self._eligible(*pending[1:])
                       and self.clock() - self._last_comment >= 30)
            operation, payload = None, {}
            desired, epoch = self._desired_id, self._epoch
            if self._uncertain:
                operation = "status"
            elif self._remote_id is not None and self._remote_id != desired:
                operation = "stop"
                payload = {"media_id": self._remote_id}
            elif desired is not None and self._remote_id is None:
                operation = "start"
                payload = {"media_id": desired, "media": self._snapshot()}
                self._remote_id = desired
            elif desired is not None and (self._dirty or comment or (
                    self._comment_task is None and self._speech is None and self.clock() >= self._sync_at)):
                operation = "update"
                payload = {"media_id": desired, "media": self._snapshot()}
            if operation is None:
                return
            if operation != "status":
                self._seq += 1
                payload["seq"] = self._seq
            data = await self._send(operation, ref, payload)
            if ref != self._ref or ref != self._granted_session():
                return
            state = self._response_state(data)
            remote = state["media_id"]
            if operation == "status":
                if remote is not None and remote != self._remote_id:
                    raise ProtocolError("busy")
                self._seq = max(self._seq, state["seq"])
                self._uncertain = False
            elif (state["seq"] != self._seq
                  or remote != (payload["media_id"] if operation != "stop" else None)):
                raise ProtocolError("invalid_response")
            self._remote_id = remote
            if operation in {"start", "update"}:
                self._sync_at = self.clock() + SYNC_SECONDS
            if desired != self._desired_id or epoch != self._epoch:
                continue
            if operation == "update":
                self._dirty = False
            if self._armed and remote == self._desired_id:
                self._set_state(state)
            elif operation == "stop" and self._desired_id is None:
                self._set_state(state)
            if (operation == "update" and comment and self._pending is pending
                    and self._eligible(*pending[1:]) and self.can_present()):
                self._pending = None
                if self._kind == "movie" or self._item.status == "playing":
                    self._last_comment = self.clock()
                if self._kind == "movie" and pending[1] == "L1":
                    self._last_movie_l1 = self._last_comment
                self._comment_task = self.loop.create_task(self._comment(ref, remote, self._seq, *pending))

    @staticmethod
    def _response_state(data):
        state = data.get("state") if isinstance(data, dict) else None
        if (not isinstance(state, dict) or type(state.get("seq")) is not int or state["seq"] < -1
                or state.get("status") not in ("idle", "playing", "paused")
                or "media_id" not in state or (state["media_id"] is None) != (state["status"] == "idle")
                or (state["media_id"] is not None and not isinstance(state["media_id"], str))
                or "reaction" not in data):
            raise ProtocolError("invalid_response")
        return state

    async def _comment(self, ref, media_id, seq, epoch, tier, reason, at):
        task = asyncio.current_task()
        context = seq, media_id, epoch, tier, reason, at
        try:
            if self._source == "local_player":
                self._read_local()
            if not self._valid(*context) or not self._eligible(tier, reason, at) or not self.can_present():
                return
            cue, _, _ = self._context()
            payload = {"media_id": media_id, "seq": seq}
            if cue:
                payload["cue"] = cue
            data = await self._send("comment", ref, payload)
            state = self._response_state(data)
            if state["media_id"] != media_id or state["seq"] != seq:
                return
            if self._source == "system_media":
                await self._read_system()
            else:
                self._read_local()
            if not self._valid(*context) or not self.can_present():
                return
            self._set_state(state)
            reaction = data["reaction"]
            if reaction is None:
                return
            event = self._reaction_event(reaction, media_id, seq)
            if event is not None and self._seen_reaction != event["reaction_id"]:
                self._seen_reaction = event["reaction_id"]
                self._speech = event["reaction_id"], *context
                self.reaction.emit(event)
        except (ProtocolError, ConnectionError, TimeoutError) as exc:
            reason = str(exc) or "connection_lost"
            if reason in SESSION_ERRORS and ref == self._ref:
                self.error.emit(reason)
                self.stop()
            elif reason in RETRY_ERRORS and self._valid(*context):
                self._uncertain = True
                self._schedule_sync()
        finally:
            if task is self._comment_task:
                self._comment_task = None

    @staticmethod
    def _reaction_event(reaction, media_id, seq):
        if (not isinstance(reaction, dict) or reaction.get("media_id") != media_id
                or type(reaction.get("seq")) is not int or reaction["seq"] != seq
                or not isinstance(reaction.get("reaction_id"), str) or not reaction["reaction_id"]
                or not isinstance(reaction.get("text"), str) or not isinstance(reaction.get("segments"), list)):
            return None
        reaction_id = reaction["reaction_id"]
        clean, _ = parse_reply(reaction["text"])
        if not clean:
            return None
        segments = [segment for segment in reaction["segments"]
                    if utterance(segment) and segment["turn_id"] == segment["utterance_id"] == reaction_id]
        return {"type": "shared_media.reaction", "reaction_id": reaction_id, "media_id": media_id, "seq": seq,
                "text": clean, "segments": segments}
