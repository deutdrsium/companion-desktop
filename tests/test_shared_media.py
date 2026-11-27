import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from PyQt5.QtCore import QObject, pyqtSignal

from companion_desktop.client import shared_media
from companion_desktop.client.media_sources import MediaItem, SystemMedia, TimedText


REF = {"device_id": "device", "desktop_session_id": "session", "generation": 1}


class LocalSource(QObject):
    changed = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self.player = self
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


class MediaHost:
    def __init__(self):
        self.state = {"status": "idle", "media_id": None, "seq": -1}
        self.calls = []
        self.reply = asyncio.Event()
        self.hold_comment = False
        self.lose_start = False

    async def __call__(self, operation, ref, payload):
        assert ref == REF
        self.calls.append((operation, payload))
        if operation == "status":
            assert payload == {}
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
        media.local_player.set_item(artist="Public artist", album=r"C:\private\album", position_ms=10_000)
        assert media.start("local_player", "music", genre="Jazz", synopsis="A https://example.test/private link")
        await settle()
        assert [operation for operation, _ in host.calls] == ["start", "status", "update"]
        request = host.calls[0][1]["media"]
        assert request == {"kind": "music", "source": "local_player", "status": "playing", "title": "First",
                           "position_ms": 10_000, "duration_ms": 90_000, "artist": "Public artist", "genre": "Jazz"}
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


def test_movie_pause_owner_and_credits_send_comments_without_new_fields(qapp, monkeypatch):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)

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
        media.mark_credits()
        await settle()
        comments = [payload for operation, payload in host.calls if operation == "comment"]
        assert len(comments) == 3
        assert all(set(payload) == {"media_id", "seq"} for payload in comments)
        assert [event["cue"] for event in tiers if event["tier"] == "L2"] == ["pause", "owner_speech", "credits"]
        assert events == []
        await media.close()

    asyncio.run(run())


def test_system_media_without_position_only_comments_at_observed_track_change(qapp, monkeypatch):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)

    class SystemSource:
        sessions = {"choice": object()}
        item = MediaItem("first", "First", "playing", None, None, "Demo artist", "Demo album")

        async def discover(self):
            return ({"id": "choice", "label": "Synthetic player"},)

        def watch(self, token, notify):
            assert token == "choice"

        def unwatch(self):
            pass

        async def snapshot(self, token):
            return self.item

        def cached_snapshot(self, token):
            return self.item

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
        assert host.state["artist"] == "Demo artist" and host.state["album"] == "Demo album"
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
    props = SimpleNamespace(title="Example film", artist="", album_title="", track_number=0)
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

        async def request(self, body):
            sent.append((self.endpoint, json.loads(body)))
            return {"state": {"status": "idle", "seq": -1, "media_id": None}, "reaction": None}

    monkeypatch.setattr(device_backend, "HttpTransport", CaptureTransport)
    backend = SimpleNamespace(base="http://127.0.0.1", api="/desktop", key="synthetic", media_transport=None)
    asyncio.run(device_backend.DeviceBackend._shared_media_request(backend, "status", REF, {}))
    assert sent == [("http://127.0.0.1/desktop/shared-media",
                     {"operation": "status", "session": REF, "payload": {}})]


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
            assert media.start("local_player", "music")
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
