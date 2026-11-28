import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from threading import Event, get_ident
from time import sleep
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from companion_desktop.client import shared_media
from companion_desktop.client.media_sources import LocalPlayer, MediaItem, SystemMedia, TimedText
from companion_desktop.contracts.wire import ProtocolError


REF = {"device_id": "device", "desktop_session_id": "session", "generation": 1}


class LocalSource(QObject):
    changed = pyqtSignal()
    frame_sampled = pyqtSignal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self.player = self
        self.frame_sampling = False
        self.item = MediaItem("first", "First", "playing", 0, 90_000, loudness=0.4)

    def snapshot(self):
        return self.item

    def position(self):
        return self.item.position_ms

    def setPosition(self, position):
        self.set_item(position_ms=position)

    def set_item(self, **kwargs):
        self.item = replace(self.item, **kwargs)
        self.changed.emit()

    def reset_level(self):
        pass

    def set_frame_sampling(self, enabled):
        self.frame_sampling = enabled


class MediaHost:
    def __init__(self):
        self.state = {"status": "idle", "media_id": None, "seq": -1}
        self.calls = []
        self.reply = asyncio.Event()
        self.hold_comment = False
        self.lose_start = False

    async def __call__(self, operation, ref, payload, *, before_send=None):
        assert ref == REF
        if before_send is not None and not before_send(True):
            raise ProtocolError("stale_glance")
        self.calls.append((operation, payload))
        if operation == "status":
            assert payload == {}
        elif operation == "glance":
            assert payload["seq"] == self.state["seq"]
            assert payload["media_id"] == self.state["media_id"]
        elif operation == "comment":
            assert payload["seq"] == self.state["seq"]
            result = self.state.copy()
            if self.hold_comment:
                try:
                    await self.reply.wait()
                except asyncio.CancelledError:
                    await self.reply.wait()
            return {"state": result, "reaction": {
                "media_id": payload["media_id"], "seq": payload["seq"], "reaction_id": "reaction",
                "text": "Listen!", "segments": [],
            }}
        else:
            assert payload["seq"] > self.state["seq"]
            media_id = payload["media_id"]
            assert len(media_id) == 32 and int(media_id, 16) >= 0
            if operation == "start":
                assert self.state["media_id"] is None
            else:
                assert self.state["media_id"] == media_id
            if operation == "stop":
                self.state = {"status": "idle", "media_id": None, "seq": payload["seq"]}
            else:
                self.state = {**payload["media"], "media_id": media_id, "seq": payload["seq"]}
            if operation == "start" and self.lose_start:
                self.lose_start = False
                raise ConnectionError("connection_lost")
        return {"state": self.state.copy(), "reaction": None}


class SystemSource:
    item = MediaItem("first", "First", "playing", None, None, artist="Demo artist")

    async def discover(self):
        return ({"id": "choice", "label": "Synthetic player"},)

    def watch(self, token, notify):
        assert token == "choice"
        self.notify = notify

    def unwatch(self):
        pass

    def close(self):
        pass

    async def snapshot(self, token):
        return self.item


async def settle():
    for _ in range(8):
        await asyncio.sleep(0)


def test_reconcile_heartbeat_track_change_and_permission_revocation(qapp, monkeypatch):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)

    async def run():
        host = MediaHost()
        host.lose_start = True
        clock, grant = [10], [REF]
        media = shared_media.SharedMedia(asyncio.get_running_loop(), host, lambda: grant[0], clock=lambda: clock[0])
        media.local_player.set_item(artist="Embedded artist", position_ms=10_000)
        assert media.start("local_player", "music")
        await settle()
        assert [operation for operation, _ in host.calls] == ["start", "status", "update"]
        request = host.calls[0][1]["media"]
        assert request == {"kind": "music", "source": "local_player", "status": "playing", "title": "First",
                           "position_ms": 10_000, "duration_ms": 90_000}
        assert host.calls[-1][1]["media"]["loudness"] == 0.4
        clock[0] += 61
        media._tick()
        await settle()
        assert host.calls[-1][0] == "update"
        first = media.state["media_id"]
        media.local_player.set_item(key="second", title="Second", position_ms=0)
        await settle()
        assert [operation for operation, _ in host.calls[-3:]] == ["stop", "start", "update"]
        assert media.state["media_id"] != first and media.state["title"] == "Second"
        grant[0] = None
        media.session_changed()
        assert media.state == {"type": "shared_media.state", "status": "idle", "media_id": None, "seq": -1}
        await media.close()

    asyncio.run(run())


def test_seek_rejects_late_comment_without_waiting_for_host(qapp, monkeypatch):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)

    async def run():
        host = MediaHost()
        host.hold_comment = True
        media = shared_media.SharedMedia(asyncio.get_running_loop(), host, lambda: REF, clock=lambda: 0)
        media.can_present = lambda: True
        events = []
        media.reaction.connect(events.append)
        assert media.start("local_player", "music")
        await settle()
        assert [operation for operation, _ in host.calls] == ["start", "update", "comment"]
        assert set(host.calls[-1][1]) == {"media_id", "seq"}
        media.seek_local(100)
        await settle()
        assert host.calls[-1][0] == "update"
        assert host.calls[-1][1]["media"]["position_ms"] == 100
        host.reply.set()
        await settle()
        assert events == []
        media.stop()
        assert media.state["status"] == "idle"
        await settle()
        assert host.calls[-1][0] == "stop"
        await media.close()

    asyncio.run(run())


def test_delivered_reaction_matches_session_item_revision_and_gap(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)
    lyrics = tmp_path / "fixture.lrc"
    lyrics.write_text("[00:03]First words\n[00:08]\n[00:10]Last words", encoding="utf-8")

    async def run():
        clock = [0]
        host = MediaHost()
        media = shared_media.SharedMedia(asyncio.get_running_loop(), host, lambda: REF, clock=lambda: clock[0])
        media.can_present = lambda: True
        cancelled, events = [], []
        media.reaction.connect(events.append)
        media.reaction_cancelled.connect(cancelled.append)
        media.set_timed_text(lyrics)
        assert media.start("local_player", "music")
        await settle()
        assert [(event["type"], event["seq"]) for event in events] == [("shared_media.reaction", 1)]
        assert "cue" not in host.calls[-1][1]
        assert media.allow_audio("reaction")
        clock[0] = 2.9
        media.local_player.set_item(position_ms=2900)
        media._tick()
        assert not media.allow_audio("reaction")
        assert cancelled == ["reaction"]
        await media.close()

    asyncio.run(run())


def test_movie_pause_owner_and_credits_send_playback_triggers(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)
    captions = tmp_path / "credits.srt"
    captions.write_text("1\n00:01:02,000 --> 00:01:04,000\n[End credits]\n", encoding="utf-8")

    async def run():
        clock = [0]
        host = MediaHost()

        async def silent_movie(operation, ref, payload):
            result = await host(operation, ref, payload)
            return {**result, "reaction": None}

        media = shared_media.SharedMedia(asyncio.get_running_loop(), silent_movie, lambda: REF, clock=lambda: clock[0])
        media.can_present = lambda: True
        events, tiers = [], []
        media.reaction.connect(events.append)
        media.tier.connect(tiers.append)
        media.set_timed_text(captions)
        assert media.start("local_player", "movie")
        await settle()
        media.local_player.set_item(status="paused")
        await settle()
        clock[0] = 31
        media.local_player.set_item(status="playing")
        media.set_owner_speaking(True)
        media.set_owner_speaking(False)
        await settle()
        clock[0] = 62
        media.local_player.set_item(position_ms=62_000)
        await settle()
        comments = [payload for operation, payload in host.calls if operation == "comment"]
        triggers = ["pause", "owner_speech", "credits"]
        assert [payload["trigger"] for payload in comments] == triggers
        assert all("subtitle_gap_ms" not in payload for payload in comments)
        assert comments[-1]["cue"] == "[End credits]"
        assert [event["cue"] for event in tiers if event["tier"] == "L2"] == triggers
        assert events == []
        await media.close()

    asyncio.run(run())


@pytest.fixture
def movie_captions(tmp_path):
    captions = tmp_path / "movie.srt"
    captions.write_text("1\n00:00:00,000 --> 00:00:01,000\n[Screams]\n\n"
                        "2\n00:00:05,000 --> 00:00:06,000\nWhat's that?\n", encoding="utf-8")
    return captions


@pytest.mark.parametrize(("request_position", "update_delay", "expected_gap"), [
    (1400, 1, 3600), (2000, 1, 3000), (2001, 1, None), (1400, 11, None),
])
def test_movie_short_reaction_syncs_peak_and_reads_subtitle_gap_at_request(
        qapp, monkeypatch, movie_captions, request_position, update_delay, expected_gap):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)

    async def run():
        clock = [0]
        host = MediaHost()

        async def advance_during_update(operation, ref, payload):
            result = await host(operation, ref, payload)
            if operation == "update" and payload["media"]["position_ms"] == 1000:
                clock[0] += update_delay
                if update_delay > 1:
                    media.local_player.set_item(position_ms=request_position, loudness=0.7)
                else:
                    media.local_player.item = replace(media.local_player.item, position_ms=request_position)
            return result

        media = shared_media.SharedMedia(asyncio.get_running_loop(), advance_during_update, lambda: REF,
                                         clock=lambda: clock[0])
        media.can_present = lambda: True
        media.set_timed_text(movie_captions)
        try:
            assert media.start("local_player", "movie")
            await settle()
            clock[0] = 600
            media.local_player.set_item(loudness=0.8)
            await settle()
            assert all(operation != "comment" for operation, _ in host.calls)
            clock[0] += 1
            media.local_player.set_item(position_ms=1000, loudness=None)
            await settle()
            _, update = next(call for call in reversed(host.calls) if call[0] == "update")
            assert update["media"]["status"] == "playing"
            assert update["media"]["loudness"] == 0.8
            assert update["media"]["position_ms"] == 1000
            comments = [payload for operation, payload in host.calls if operation == "comment"]
            expected = {"media_id": update["media_id"], "seq": update["seq"], "trigger": "subtitle_gap",
                        "subtitle_gap_ms": expected_gap}
            assert comments == ([expected] if expected_gap is not None else [])
        finally:
            await media.close()

    asyncio.run(run())


@pytest.mark.parametrize(("level", "elapsed", "position", "timed_text"), [
    (0.64, 1, 1000, True),
    (0.8, 12, 1000, True),
    (0.8, 1, 2001, True),
    (0.8, 7, 7000, True),
    (0.8, 1, 1000, False),
])
def test_movie_short_reaction_requires_fresh_peak_and_known_gap(
        qapp, monkeypatch, movie_captions, level, elapsed, position, timed_text):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)

    async def run():
        clock = [0]
        host = MediaHost()
        media = shared_media.SharedMedia(asyncio.get_running_loop(), host, lambda: REF, clock=lambda: clock[0])
        media.can_present = lambda: True
        if timed_text:
            media.set_timed_text(movie_captions)
        try:
            assert media.start("local_player", "movie")
            await settle()
            clock[0] = 600
            media.local_player.set_item(loudness=level)
            await settle()
            clock[0] += elapsed
            media.local_player.set_item(position_ms=position, loudness=None)
            await settle()
            assert all(operation != "comment" for operation, _ in host.calls)
            media.local_player.set_item(status="paused", ended=True)
            await settle()
            assert all(operation != "comment" for operation, _ in host.calls)
        finally:
            await media.close()

    asyncio.run(run())


def test_system_media_without_position_only_comments_at_observed_track_change(qapp, monkeypatch):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)
    monkeypatch.setattr(shared_media, "SystemMedia", SystemSource)

    async def run():
        clock = [0]
        host = MediaHost()
        host.hold_comment = True
        media = shared_media.SharedMedia(asyncio.get_running_loop(), host, lambda: REF, clock=lambda: clock[0])
        media.can_present = lambda: True
        events = []
        media.reaction.connect(events.append)
        media.refresh_sources()
        await settle()
        assert media.sources[1]["id"] == "choice"
        media.select_system("choice")
        assert media.start("system_media", "music")
        await settle()
        assert [operation for operation, _ in host.calls] == ["start", "update"]
        assert host.state["position_ms"] is None and host.state["duration_ms"] is None
        assert host.state["artist"] == "Demo artist" and "album" not in host.state
        media._system.item = replace(media._system.item, key="second", title="Second")
        media._tick()
        await settle()
        assert [operation for operation, _ in host.calls[-4:]] == ["stop", "start", "update", "comment"]
        clock[0] = 4.1
        host.reply.set()
        await settle()
        assert events == []
        await media.close()

    asyncio.run(run())


def test_windows_timeline_jump_cancels_held_reaction_and_advances_seq(qapp, monkeypatch):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)
    monkeypatch.setattr(shared_media, "SystemMedia", SystemSource)

    async def run():
        clock = [0]
        host = MediaHost()
        host.hold_comment = True
        media = shared_media.SharedMedia(asyncio.get_running_loop(), host, lambda: REF, clock=lambda: clock[0])
        media.can_present = lambda: True
        events = []
        media.reaction.connect(events.append)
        try:
            media.refresh_sources()
            await settle()
            media.select_system("choice")
            media._system.item = MediaItem("first", "First", "playing", 0, 90_000, timeline_key="first anchor")
            assert media.start("system_media", "music")
            await settle()
            assert host.calls[-1][0] == "comment"
            held_seq = host.state["seq"]
            clock[0] = 0.2
            media._system.item = replace(media._system.item, position_ms=200)
            media._system_event("choice", "timeline")
            await settle()
            assert host.state["seq"] == held_seq
            clock[0] = 0.4
            media._system.item = replace(media._system.item, position_ms=600)
            host.reply.set()
            await settle()
            assert host.state["seq"] > held_seq and host.state["position_ms"] == 600
            assert events == []
        finally:
            await media.close()

    asyncio.run(run())


def test_windows_slow_native_getter_leaves_qt_timer_responsive(qapp):
    started, finished = Event(), Event()
    qt_thread = get_ident()
    ticks = []
    timer = QTimer(interval=10)
    timer.timeout.connect(lambda: ticks.append(started.is_set() and not finished.is_set()))

    def playback():
        assert get_ident() != qt_thread
        started.set()
        sleep(0.3)
        finished.set()
        return SimpleNamespace(playback_status=SimpleNamespace(name="PAUSED"))

    async def props():
        return SimpleNamespace(title="Film", artist="", track_number=0)

    async def read():
        source = SystemMedia()
        session = SimpleNamespace(get_playback_info=playback, get_timeline_properties=lambda: None,
                                  try_get_media_properties_async=props)
        source.manager = SimpleNamespace(get_sessions=lambda: (session,))
        source.sessions["choice"] = session
        task = asyncio.create_task(source.snapshot("choice"))
        timer.start()
        try:
            while not task.done():
                qapp.processEvents()
                await asyncio.sleep(0.005)
            assert (await task).title == "Film"
        finally:
            timer.stop()
            source.close()

    asyncio.run(asyncio.wait_for(read(), 3))
    assert sum(ticks) >= 3


def test_paused_subtitle_and_title_strip_links_and_addresses(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)
    captions = tmp_path / "example.srt"
    captions.write_text("1\n00:00:01,000 --> 00:00:05,000\n"
                        "Stay here. youtu.be/demo https://example.org/path contact@example.org example.net\n",
                        encoding="utf-8")

    async def run():
        host = MediaHost()
        media = shared_media.SharedMedia(asyncio.get_running_loop(), host, lambda: REF, clock=lambda: 0)
        media.can_present = lambda: True
        media.local_player.set_item(title="Demo podcast youtu.be/link email&#64;example.org", position_ms=2000)
        media.set_timed_text(captions)
        try:
            assert media.start("local_player", "movie")
            await settle()
            media.local_player.set_item(status="paused")
            await settle()
            assert host.state["title"] == "Demo podcast"
            assert host.calls[-1][0] == "comment"
            assert host.calls[-1][1]["cue"] == "Stay here."
        finally:
            await media.close()

    asyncio.run(run())


def test_send_point_filters_snapshot_fields_and_local_reads_only_title(qapp, monkeypatch):
    tags = []
    player = SimpleNamespace(NoMedia=0, InvalidMedia=1, EndOfMedia=2, PlayingState=3, mediaStatus=lambda: 3,
                             state=lambda: 3, duration=lambda: 1000, position=lambda: 150,
                             metaData=lambda key: (tags.append(key), "Demo")[1])
    source = SimpleNamespace(player=player, _key="local", _title="Fallback", _level_at=0, _level=0.3)
    item = LocalPlayer.snapshot(source)
    assert tags == ["Title"] and item.artist is None

    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)

    async def run():
        captured = []

        async def send(_, __, payload):
            captured.append(payload["media"])

        media = shared_media.SharedMedia(asyncio.get_running_loop(), send, lambda: REF)
        item = {"kind": "music", "source": "local_player", "title": "Demo", "status": "playing",
                "position_ms": 150, "duration_ms": 1000, "loudness": 0.3, "artist": "Unknown",
                "album": "Private album", "author": "Embedded author", "composer": "Embedded composer",
                "comment": "Embedded comment", "genre": "Jazz", "synopsis": "Text", "scene_cut": True}
        for source, kind in (("local_player", "music"), ("system_media", "movie"), ("system_media", "music")):
            await media._send("update", REF, {"media_id": "media", "seq": 1,
                                             "media": {**item, "source": source, "kind": kind}})
        base = {"kind", "source", "title", "status", "position_ms", "duration_ms"}
        assert [set(snapshot) for snapshot in captured] == [base | {"loudness"}, base, base | {"artist"}]
        await media.close()

    asyncio.run(run())


def test_timed_text_tracks_overlaps_gaps_and_credits(tmp_path):
    captions = tmp_path / "fixture.srt"
    captions.write_text(
        "1\n00:00:01,000 --> 00:00:06,000\n<i>Long line</i>\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nShort line\n\n"
        "3\n00:00:08,000 --> 00:00:09,000\n[End credits]\n", encoding="utf-8-sig")
    timeline = TimedText.load(captions)
    assert timeline.context(None) == (None, 0, False)
    assert timeline.context(2500) == ("Short line", 0, False)
    assert timeline.context(4000) == ("Long line", 0, False)
    assert timeline.context(6500) == (None, 1500, False)
    assert timeline.context(8500) == ("[End credits]", 0, True)


def test_system_stop_only_marks_end_with_timeline_evidence():
    timeline = SimpleNamespace(start_time=timedelta(), end_time=timedelta(minutes=8),
                               position=timedelta(minutes=2), last_updated_time=datetime.now(timezone.utc))
    source = SystemMedia()
    source.sessions["selection"] = SimpleNamespace(
        get_playback_info=lambda: SimpleNamespace(playback_status=SimpleNamespace(name="STOPPED")),
        get_timeline_properties=lambda: timeline,
    )
    props = SimpleNamespace(title="Example film", artist="", track_number=0)
    item = source.readback("selection", props)
    assert item.status == "paused" and not item.ended
    timeline.position = timeline.end_time - timedelta(milliseconds=500)
    assert source.readback("selection", props).ended


def test_shared_media_http_uses_existing_authenticated_transport(monkeypatch):
    from companion_desktop.client import device_backend

    sent = []

    class CaptureTransport:
        def __init__(self, endpoint, credential, *, timeout):
            self.endpoint = endpoint
            assert credential == "synthetic"

        async def request(self, body, *, before_send=None):
            if before_send is not None:
                assert before_send(True)
            sent.append((self.endpoint, json.loads(body)))
            return {"state": {"status": "idle", "seq": -1, "media_id": None}, "reaction": None}

    monkeypatch.setattr(device_backend, "HttpTransport", CaptureTransport)
    backend = SimpleNamespace(base="http://127.0.0.1", api="/desktop", key="synthetic", media_transport=None)
    async def run():
        request = device_backend.DeviceBackend._shared_media_request
        await request(backend, "status", REF, {})
        payload = {"media_id": "0" * 32, "seq": 7, "reason": "pause", "image": "A" * 106_668}
        await request(backend, "glance", REF, payload, before_send=lambda _: True)
        with pytest.raises(ProtocolError, match="invalid_request"):
            await request(backend, "comment", REF, {"cue": "A" * 8192})
        with pytest.raises(ProtocolError, match="invalid_request"):
            await request(backend, "glance", REF, {**payload, "image": "A" * (128 * 1024)}, before_send=lambda _: True)
        backend.base = "http://192.0.2.1"
        with pytest.raises(ProtocolError, match="glance_unavailable"):
            await request(backend, "glance", REF, payload, before_send=lambda _: True)
        return payload

    payload = asyncio.run(run())
    assert sent == [("http://127.0.0.1/desktop/shared-media",
                     {"operation": "status", "session": REF, "payload": {}}),
                    ("http://127.0.0.1/desktop/shared-media/glance",
                     {"operation": "glance", "session": REF, "payload": payload})]


def test_controller_presents_without_chat_and_drops_late_synthesis(qapp, prefs, monkeypatch):
    from companion_desktop.client.controller import Controller
    from companion_desktop.ui.paint import Character
    from fakes import FakeBackend, FakePlayer, FakeSubtitles

    class Device(FakeBackend):
        connection = pyqtSignal(object)
        permissions_changed = pyqtSignal(object)
        voice_revoked = pyqtSignal()
        utterance_cancelled = pyqtSignal(str)
        pcm = pyqtSignal(int, object)
        pcm_lost = pyqtSignal(int, object)
        avatar = None
        can_speak = True

    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)

    async def run():
        backend = Device()
        backend.shared_media = shared_media.SharedMedia(asyncio.get_running_loop(), MediaHost(), lambda: REF)
        player, subtitles = FakePlayer(), FakeSubtitles()
        prefs.set("voice_enabled", True)
        controller = Controller(Character("Test"), "Test", prefs, backend, player=player,
                                subtitles=subtitles, device=True)
        media = controller.shared_media
        try:
            assert not media._frame_glance
            prefs.set("together_frame_glance", True)
            assert media._frame_glance
            assert media.start("local_player", "music")
            assert not media.local_player.frame_sampling
            await settle()
            assert backend.speech == [(["Listen!"], 0, 0)]
            assert backend.chats == [] and controller.active_turn is None
            backend.audio.emit(0, "first", 0, "Listen!", b"", False)
            player.play_next()
            assert subtitles.states[-1] == ("speaking", "Listen!")
            media.seek_local(15_000)
            assert not player.active and subtitles.states[-1][0] == "interrupted"
            backend.audio.emit(0, "late", 0, "Listen!", b"", False)
            assert not player.active
        finally:
            await media.close()
            controller.quitting = True
            controller.panel.close()

    asyncio.run(run())
