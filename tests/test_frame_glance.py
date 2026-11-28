import asyncio
from base64 import b64decode
from random import Random

from PyQt5.QtGui import QColor, QImage

from companion_desktop.client import shared_media
from companion_desktop.client.frame_glance import jpeg_base64, thumbnail
from companion_desktop.client.media_sources import TimedText
from test_shared_media import REF, LocalSource, MediaHost, SystemSource, settle


def frame(color):
    image = QImage(1024, 576, QImage.Format_RGB32)
    image.fill(QColor(color))
    return image


def glances(host):
    return [payload for operation, payload in host.calls if operation == "glance"]


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
            await settle()
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
        await settle()
        assert [item["reason"] for item in glances(host)] == ["pause"]
        media.stop()
        await settle()
        assert media.start("local_player", "movie")
        await settle()
        media.local_player.frame_sampled.emit(frame("blue"))
        for at, count in ((179, 1), (180, 2), (599, 2), (600, 3)):
            clock[0] = at
            media.set_owner_speaking(True)
            media.set_owner_speaking(False)
            await settle()
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

        async def send(operation, ref, payload):
            if operation == "update" and payload["media"]["status"] == "paused":
                in_update.set()
                await release_update.wait()
            return await host(operation, ref, payload)

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


def test_noisy_frame_jpeg_fits_limit_and_contains_no_image_text(qapp):
    pixels = Random(0).randbytes(512 * 512 * 3)
    image = QImage(pixels, 512, 512, 512 * 3, QImage.Format_RGB888)
    image.setText("Comment", "Synthetic input metadata")
    data = b64decode(jpeg_base64(thumbnail(image)), validate=True)
    decoded = QImage.fromData(data, "JPEG")
    assert len(data) <= 80_000 and not decoded.textKeys()
    assert (decoded.width(), decoded.height()) == (512, 512)
