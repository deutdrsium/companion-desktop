import asyncio
from base64 import b64decode
from io import BytesIO
from random import Random
from threading import Event, get_ident
from time import perf_counter, sleep
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtGui import QColor, QImage
from PyQt5.QtMultimedia import QVideoFrame, QVideoSurfaceFormat

from companion_desktop.client import shared_media
from companion_desktop.client.connection import frame_glance_allowed
from companion_desktop.client.device_backend import DeviceBackend
from companion_desktop.client.frame_glance import FrameSample, jpeg_base64, thumbnail
from companion_desktop.client.media_sources import LocalPlayer, TimedText
from companion_desktop.session import client
from test_shared_media import REF, LocalSource, MediaHost, SystemSource, settle


def frame(color):
    image = QImage(1024, 576, QImage.Format_RGB32)
    image.fill(QColor(color))
    return FrameSample(QVideoFrame(image), False)


def glances(host):
    return [payload for operation, payload in host.calls if operation == "glance"]


async def settle_glance(media):
    await settle()
    for task in (media._glance_signature_task, media._glance_task):
        if task is not None:
            await task
        await settle()
    if media._glance_task is not None:
        await media._glance_task


def test_scene_cut_needs_loudness_peak_or_changed_subtitle(qapp, monkeypatch):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)

    async def run(with_subtitles):
        clock = [0]
        host = MediaHost()
        media = shared_media.SharedMedia(asyncio.get_running_loop(), host, lambda: REF, clock=lambda: clock[0])
        media.set_frame_glance(True)
        if with_subtitles:
            media._timeline = TimedText([(0, 1200, "First line"), (1200, 1800, "Second line")])
        assert media.start("local_player", "movie") and media.local_player.frame_sampling
        await settle()
        for position, color, level in ((0, "black", 0.05), (340, "black", 0.4), (680, "white", 0.4),
                                       (1020, "black", 0.05), (1360, "white", 0.05 if with_subtitles else 0.5)):
            clock[0] = position / 1000
            media.local_player.set_item(position_ms=position, loudness=level)
            media.local_player.frame_sampled.emit(frame(color))
            await settle_glance(media)
            if position < 1360:
                assert not glances(host)
        glimpse, = glances(host)
        assert set(glimpse) == {"media_id", "seq", "reason", "image"}
        assert glimpse["reason"] == ("scene_cut" if with_subtitles else "loudness_peak")
        assert host.calls[-2][0] == "update" and host.calls[-2][1]["seq"] == glimpse["seq"]
        decoded = QImage.fromData(b64decode(glimpse["image"], validate=True), "JPEG")
        assert (decoded.width(), decoded.height()) == (512, 288)
        assert decoded.pixelColor(0, 0).lightness() > 240
        await media.close()

    asyncio.run(run(False))
    asyncio.run(run(True))


def test_pausing_and_owner_speech_respect_glance_budget(qapp, monkeypatch):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)

    async def run():
        clock = [0]
        host = MediaHost()
        media = shared_media.SharedMedia(asyncio.get_running_loop(), host, lambda: REF, clock=lambda: clock[0])
        media.set_frame_glance(True)
        assert media.start("local_player", "movie")
        media.local_player.frame_sampled.emit(frame("blue"))
        media.local_player.set_item(status="paused")
        await settle_glance(media)
        assert [item["reason"] for item in glances(host)] == ["pause"]
        media.stop()
        await settle()
        assert media.start("local_player", "movie")
        await settle()
        for at, count in ((179, 1), (180, 2), (599, 2), (600, 3)):
            clock[0] = at
            media.local_player.frame_sampled.emit(frame("blue"))
            media.set_owner_speaking(True)
            media.set_owner_speaking(False)
            await settle_glance(media)
            assert len(glances(host)) == count
        assert [item["reason"] for item in glances(host)] == ["pause", "owner_speech", "owner_speech"]
        await media.close()

    asyncio.run(run())


def test_glance_is_opt_in_and_cancelled_before_upload(qapp, monkeypatch):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)
    monkeypatch.setattr(shared_media, "SystemMedia", SystemSource)

    async def run():
        host = MediaHost()
        in_update, release_update = asyncio.Event(), asyncio.Event()

        async def send(operation, ref, payload, **kwargs):
            if operation == "update" and payload["media"]["status"] == "paused":
                in_update.set()
                await release_update.wait()
            return await host(operation, ref, payload, **kwargs)

        media = shared_media.SharedMedia(asyncio.get_running_loop(), send, lambda: REF, clock=lambda: 0)
        assert media.start("local_player", "movie")
        assert not media.local_player.frame_sampling
        media.local_player.frame_sampled.emit(frame("white"))
        media.set_owner_speaking(True)
        media.set_owner_speaking(False)
        await settle()
        assert not glances(host)
        media.set_frame_glance(True)
        assert media.local_player.frame_sampling
        media.local_player.frame_sampled.emit(frame("white"))
        media.local_player.set_item(status="paused")
        await asyncio.wait_for(in_update.wait(), 1)
        media.set_frame_glance(False)
        release_update.set()
        await settle()
        assert not media.local_player.frame_sampling and not glances(host)
        media.stop()
        await settle()
        media.set_frame_glance(True)
        media.refresh_sources()
        await settle()
        media.select_system("choice")
        assert media.start("system_media", "movie") and not media.local_player.frame_sampling
        await settle()
        media.local_player.frame_sampled.emit(frame("white"))
        media.set_owner_speaking(True)
        media.set_owner_speaking(False)
        await settle()
        assert not glances(host)
        await media.close()

    asyncio.run(run())


def test_glance_limits_survive_restart(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)
    wall_now = [1_800_000_000]
    monkeypatch.setattr(shared_media, "time", lambda: wall_now[0])
    path = str(tmp_path / "prefs.ini")

    async def relaunch():
        store = QSettings(path, QSettings.IniFormat)
        host = MediaHost()
        media = shared_media.SharedMedia(asyncio.get_running_loop(), host, lambda: REF, clock=lambda: 0)
        media.set_frame_glance(True, store=store)
        assert media.start("local_player", "movie")
        media.local_player.frame_sampled.emit(frame("blue"))
        media.local_player.set_item(status="paused")
        await settle_glance(media)
        await media.close()
        for key in ("frame_glance_last", "frame_glance_budget_at"):
            store.setValue(key, str(store.value(key)))
        store.sync()
        return len(glances(host))

    for elapsed, count in ((0, 1), (179, 0), (1, 1), (419, 0), (1, 1)):
        wall_now[0] += elapsed
        assert asyncio.run(relaunch()) == count


@pytest.mark.parametrize("invalidate", ["opt_out", "stop", "new_revision", "new_media"])
def test_delayed_http_worker_rechecks_before_sending(qapp, monkeypatch, invalidate):
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)
    connecting, release, finished = Event(), Event(), Event()
    writes = []

    class Socket:
        def sendall(self, data):
            writes.append(data)

        def close(self):
            pass

    def connect(connection):
        connecting.set()
        if not release.wait(3):
            raise TimeoutError()
        connection.sock = Socket()

    http_call = client.HttpTransport._call

    def track_call(transport, *args):
        try:
            return http_call(transport, *args)
        finally:
            finished.set()

    monkeypatch.setattr(client.HTTPConnection, "connect", connect)
    monkeypatch.setattr(client.HttpTransport, "_call", track_call)
    backend = SimpleNamespace(base="http://127.0.0.1:1", api="/desktop", key="synthetic", media_transport=None)

    async def run():
        host = MediaHost()

        async def send(operation, ref, payload, **kwargs):
            if operation == "glance":
                return await DeviceBackend._shared_media_request(backend, operation, ref, payload, **kwargs)
            return await host(operation, ref, payload)

        media = shared_media.SharedMedia(asyncio.get_running_loop(), send, lambda: REF, clock=lambda: 0)
        media.set_frame_glance(True)
        assert media.start("local_player", "movie")
        media.local_player.frame_sampled.emit(frame("white"))
        media.local_player.set_item(status="paused")
        try:
            assert await asyncio.to_thread(connecting.wait, 2)
            if invalidate == "opt_out":
                media.set_frame_glance(False)
                media.set_frame_glance(True)
            elif invalidate == "stop":
                media.stop()
            elif invalidate == "new_revision":
                media._seq += 1
            else:
                media._desired_id = "0" * 32
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert not writes
        await media.close()

    asyncio.run(run())


def test_guarded_http_dispatch_writes_authenticated_body(monkeypatch):
    writes = []

    class Socket:
        def sendall(self, data):
            writes.append(data)

        def close(self):
            pass

    class Response(BytesIO):
        code = status = 200
        reason = "OK"
        msg = {}

        def info(self):
            return {}

    def connect(connection):
        connection.sock = Socket()

    monkeypatch.setattr(client.HTTPConnection, "connect", connect)
    monkeypatch.setattr(client.HTTPConnection, "getresponse", lambda _: Response(b'{"status":"ok","data":1}'))
    transport = client.HttpTransport("http://127.0.0.1:1/desktop/shared-media/glance", "synthetic")
    assert asyncio.run(transport.request(b"test body", before_send=lambda: True)) == 1
    assert b"Authorization: Bearer synthetic" in writes[0] and b"".join(writes).endswith(b"test body")


@pytest.mark.parametrize(("url", "allowed"), [
    ("https://example.test", True), ("http://127.0.0.1:6185", True), ("http://[::1]", True),
    ("http://localhost", True), ("http://192.0.2.1", False), ("http://localhost.example.test", False),
    ("http://localhost@192.0.2.1", False),
])
def test_glance_requires_tls_or_loopback(url, allowed):
    assert frame_glance_allowed(url) is allowed


def test_8k_frame_callback_and_glance_processing_leave_qt_responsive(qapp, monkeypatch):
    image = QImage(7680, 4320, QImage.Format_RGB32)
    image.fill(QColor("blue"))
    large_frame = QVideoFrame(image)
    player = LocalPlayer()
    tap = SimpleNamespace(surfaceFormat=QVideoSurfaceFormat)
    player._frame_tap = tap
    monkeypatch.setattr(player.player, "state", lambda: player.player.PlayingState)
    player.set_frame_sampling(True)
    samples = []
    player.frame_sampled.connect(samples.append)
    started = perf_counter()
    player._sample_frame(tap, large_frame)
    assert perf_counter() - started < 0.010 and len(samples) == 1

    qt_thread = get_ident()
    encode = FrameSample.jpeg
    compare = shared_media.changed_scene

    def off_thread(operation):
        def run(*args):
            assert get_ident() != qt_thread
            sleep(0.04)
            return operation(*args)
        return run

    monkeypatch.setattr(FrameSample, "jpeg", off_thread(encode))
    monkeypatch.setattr(shared_media, "changed_scene", off_thread(compare))
    monkeypatch.setattr(shared_media, "LocalPlayer", LocalSource)

    async def run():
        host = MediaHost()
        media = shared_media.SharedMedia(asyncio.get_running_loop(), host, lambda: REF, clock=lambda: 0)
        media.set_frame_glance(True)
        assert media.start("local_player", "movie")
        media.local_player.set_item(loudness=0.05)
        media.local_player.frame_sampled.emit(frame("white"))
        media.local_player.set_item(loudness=0.5)
        media.local_player.frame_sampled.emit(samples[0])
        started = perf_counter()
        await settle()
        assert perf_counter() - started < 0.010
        await settle_glance(media)
        assert [item["reason"] for item in glances(host)] == ["loudness_peak"]
        await media.close()

    asyncio.run(run())


def test_glance_sample_respects_padded_rgb24_rows(qapp):
    image = QImage(65, 40, QImage.Format_RGB888)
    image.fill(QColor("red"))
    sample = FrameSample(QVideoFrame(image), False)
    assert sample.visual() == bytes((255, 0, 0)) * (32 * 18)


def test_noisy_frame_jpeg_fits_limit_and_contains_no_image_text(qapp):
    pixels = Random(0).randbytes(512 * 512 * 3)
    image = QImage(pixels, 512, 512, 512 * 3, QImage.Format_RGB888)
    image.setText("Comment", "Synthetic input metadata")
    data = b64decode(jpeg_base64(thumbnail(image)), validate=True)
    decoded = QImage.fromData(data, "JPEG")
    assert len(data) <= 80_000 and not decoded.textKeys()
    assert (decoded.width(), decoded.height()) == (512, 512)
