"""B04/B05 desk entry replay with synthetic player samples and VTS readback."""

from array import array
import asyncio
import io
import time
import wave

from PyQt5.QtCore import QObject, pyqtSignal
import pytest

from companion_desktop.client.audio import PCM_RATE, PcmClip
from companion_desktop.client.controller import Controller
from companion_desktop.client.device_backend import DeviceBackend
from companion_desktop.client.timeline import CueTrack, wav_levels
from companion_desktop.contracts.lines import parse_reply, utterance_segments
from companion_desktop.contracts.wire import ProtocolError, message
from companion_desktop.ui.paint import Character
from fakes import FakePlayer, FakeSubtitles
from test_desktop_animation import Rig
from test_controller import replies, users
from test_desktop_session import FakeChat
from plugins.astrbot_plugin_desktop_access.session import Binding, SessionHost, TurnResult


CHUNKS = ["你看🐟[点", "头]这条鱼[示例]！下一句。<l2", "d:点头/>",
          '<soullink>{"emotion":"happy"}</soullink><wardrobe>{}</wardrobe>']
REF = {"device_id": "device-demo", "desktop_session_id": "session-demo", "generation": 1}


def sentence_audio():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(array("h", [0] * 160 + [8000] * 8000).tobytes())
    return buffer.getvalue()


def test_b04_codepoint_alignment_timestamps_and_estimates():
    clean, anchors = parse_reply("".join(CHUNKS))
    assert clean == "你看🐟这条鱼[示例]！下一句。"
    first, second = list(utterance_segments("turn-demo", clean, anchors))
    assert first["cues"][0]["text_offset"] == 3
    assert [cue.action for cue in anchors] == ["nod", "hold_laugh"]
    assert [cue["action"] for cue in second["cues"]] == ["hold_laugh"]
    assert message("utterance", REF, first)["body"] == first
    track = CueTrack([first, second])
    assert not list(track.advance(0, first["text"], "audio-demo", 290, 1100, 0))
    (_, source, delay), = track.advance(0, first["text"], "audio-demo", 315, 1100, 0)
    assert source == "character_estimate" and delay == 15
    assert not list(track.advance(0, first["text"], "audio-demo", 1100, 1100, 0))
    first["audio_id"] = "audio-demo"
    first["cues"][0]["params"]["audio_offset_ms"] = 427
    track = CueTrack([first])
    assert not list(track.advance(0, first["text"], "audio-demo", 420, 1100, 0))
    (_, source, delay), = track.advance(0, first["text"], "audio-demo", 430, 1100, 0)
    assert source == "timestamp" and delay == 3
    assert parse_reply("Keep [note] <l2d>挥手</l2d>.")[0] == "Keep [note] ."
    assert parse_reply("Keep <soullink>incomplete")[0] == "Keep"
    levels = wav_levels(sentence_audio())
    assert levels[0] == 0 and levels[1] > 0.9


class Player(FakePlayer):
    paused = pyqtSignal()

    def enqueue_pcm(self, clip):
        self.queue.append(clip)

    def play_next(self):
        self.queue[0].duration_ms = 1100
        return super().play_next()


class Microphone(QObject):
    speech_started = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.active = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False


@pytest.mark.parametrize("streaming", [False, True])
def test_b04_b05_send_audio_barge_in_text_and_fail(qapp, prefs, tmp_path, streaming):
    rig = Rig()
    cfg = {"server": {"api_path": "/desktop"}, "account": {"username": "demo", "session_id": "demo"},
           "avatar": {"mapping": ""}}
    backend = DeviceBackend("http://127.0.0.1:1", "synthetic-key", cfg, tmp_path)
    backend.avatar = rig.avatar
    backend.history_ready = True
    host = SessionHost()
    binding = Binding("device-demo", "binding-demo", frozenset({"avatar", "speak"}))
    sent = []

    class Chat:
        async def run(self, turn_id, text):
            sent.append(text)
            if text == "fail":
                return TurnResult("failed")
            clean, anchors = parse_reply("".join(CHUNKS))
            for frame in utterance_segments(turn_id, clean, anchors):
                host.send_utterance(backend.session.session, frame)
            return TurnResult("completed", clean)

    async def transport(operation, payload):
        return host.handle(binding, operation, payload, turns=Chat())

    backend.session._transport = transport
    backend.session.set_permissions({"avatar", "speak"})
    player, captions, mic = Player(), FakeSubtitles(), Microphone()
    controller = Controller(Character("Demo"), "Demo", prefs, backend, player=player, subtitles=captions,
                            device=True, barge_in=mic)
    synthesis = []

    def enqueue(segments, start):
        synthesis.append(segments[start:])
        return backend.generation

    backend.enqueue_speech = enqueue
    prefs.set("voice_enabled", True)

    async def exchange():
        await backend.session._exchange()

    def settle_turn():
        backend.loop.run_until_complete(exchange())
        backend.loop.run_until_complete(exchange())
        backend._pump()

    try:
        backend._publish()
        backend.loop.run_until_complete(backend.session._register())
        backend.loop.run_until_complete(exchange())
        controller.panel.composer.set_text("first")
        assert rig.avatar.motion.typing
        controller.panel.composer._submit()
        assert rig.avatar.motion.thinking and controller.state.presence == "thinking"
        settle_turn()
        first, second = synthesis[-1]
        assert first == "你看🐟这条鱼[示例]！" and second == "下一句。"
        assert replies(controller)[-1].text == first + second and rig.avatar.motion.thinking
        generation = backend.generation
        if streaming:
            stream = PcmClip("audio-demo", 0, first)
            stream.pcm.extend(b"\0\0" * (PCM_RATE // 50) + b"\x00\x20" * PCM_RATE)
            backend.pcm.emit(generation, stream)
        else:
            backend.audio.emit(generation, "audio-demo", 0, first, sentence_audio(), True)
            player.queue[0].levels = wav_levels(sentence_audio())
        backend.audio.emit(generation, "audio-next-demo", 1, second, sentence_audio(), True)
        clip = player.play_next()
        assert mic.active and not rig.avatar.motion.thinking
        assert captions.states[-1] == ("speaking", first)
        player.progress.emit(clip, 280, 0.25)
        assert not controller.gestures
        player.progress.emit(clip, 315, 0.29)
        assert len(controller.gestures) == 1 and rig.avatar.motion.mouth_target > 0
        assert controller.level.get() > 0 and controller.panel.header.avatar.source is controller.level
        assert replies(controller)[-1].wave.source is controller.level
        backend.loop.run_until_complete(rig.frames(4))
        assert rig.vts.frames[-1]["mouth_open"] > 0 and rig.avatar.motion.values["head_pitch"] < 0
        player.paused.emit()
        assert rig.avatar.motion.mouth_target == 0
        player.progress.emit(clip, 330, 0.30)
        assert rig.avatar.motion.mouth_target > 0 and len(controller.gestures) == 1
        mic.speech_started.emit()
        assert controller.state.presence == "interrupted" and not player.active and not mic.active
        assert not controller.gestures and rig.avatar.motion.mouth_target == 0
        assert backend.loop.run_until_complete(rig.step())["mouth_open"] == 0
        assert captions.states[-1][0] == "interrupted"
        backend.audio.emit(generation, "stale-audio-demo", 1, second, b"", True)
        if streaming:
            backend.pcm.emit(generation, PcmClip("stale-pcm-demo", 1, second))
        assert not player.active

        prefs.set("voice_enabled", False)
        controller.send("next")
        settle_turn()
        assert sent == ["first", "next"] and len(synthesis) == 1
        assert captions.states[-1] == ("speaking", first)
        assert rig.avatar.motion.mouth_target == 0 and not rig.avatar.motion.thinking
        controller.text_start = time.monotonic() - 0.4
        controller._text_tick()
        assert len(controller.gestures) == 1
        controller.send("fail")
        assert not controller.gestures and controller.text_lines is None
        settle_turn()
        assert controller.state.presence == "idle" and not rig.avatar.motion.thinking
        assert controller.panel.chat.thinking is None

        prefs.set("voice_enabled", True)
        controller.send("read both")
        settle_turn()
        generation = backend.generation
        if streaming:
            backend.enable_pcm()
            backend.pcm.emit(generation, PcmClip("audio-demo", 0, first))
            player.failed.emit(player.play_next())  # Output device fails after the stream is selected.
            assert backend.speech_stream is None and backend.generation != generation
            assert not player.active and not mic.active and rig.avatar.motion.mouth_target == 0
            assert synthesis[-1] == [first, second]
            generation = backend.generation
        backend.audio.emit(generation, "audio-demo", 0, first, sentence_audio(), False)
        backend.audio.emit(generation, "audio-next-demo", 1, second, sentence_audio(), False)
        player.play_next()
        player.finish()
        player.play_next()
        assert captions.states[-1] == ("speaking", second)
        player.finish()
        assert controller.state.presence == "idle" and captions.states[-1] == ("idle", None)
        assert rig.avatar.motion.mouth_target == 0

        controller.send("fail to synthesize")
        settle_turn()
        backend.speech_error.emit(backend.generation, 0)
        assert controller.state.presence == "idle" and not rig.avatar.motion.thinking
        assert replies(controller)[-1].mode == "speech_failed"
    finally:
        controller.stop_speech()
        async def disconnect():
            await backend.session._exchange("disconnect")

        backend.loop.run_until_complete(disconnect())
        backend._request = lambda *args, **kwargs: None
        backend.close(1)
        controller.quitting = True
        controller.panel.close()
        controller.panel.deleteLater()


@pytest.fixture
def device_controller(qapp, prefs, tmp_path):
    cfg = {"server": {"api_path": "/desktop"}, "account": {"username": "demo", "session_id": "demo"},
           "avatar": {"mapping": ""}}
    backend = DeviceBackend("http://127.0.0.1:1", "synthetic-key", cfg, tmp_path)
    backend.history_ready = True
    backend._request = lambda *args, **kwargs: None
    prefs.set("voice_enabled", False)
    controller = Controller(Character("Demo"), "Demo", prefs, backend, device=True)
    yield controller
    controller.quitting = True
    backend.close(1)
    controller.panel.close()
    controller.panel.deleteLater()


async def wait_for(condition):
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0.01)


def test_device_idle_requires_confirmed_grant_and_survives_host_disconnect(device_controller):
    controller = device_controller
    backend = controller.backend
    session = backend.session
    rig = Rig()
    backend.avatar = rig.avatar
    backend.avatar.set_enabled(False)
    session.dispatcher.outputs["avatar"] = backend.avatar
    session.set_permissions({"avatar", "speak"})

    async def exercise():
        host = SessionHost()
        binding = Binding("device-demo", "binding-demo", frozenset())
        failure = None

        async def transport(operation, payload):
            if failure == "connection_lost":
                raise ConnectionError(failure)
            if failure == "unauthorized":
                raise ProtocolError(failure)
            return host.handle(binding, operation, payload)

        session._transport = transport
        session._poll_interval = 0.01
        backend._publish()
        session.start()
        await wait_for(lambda: controller.state.connection == "connected")
        assert not controller.state.can_send
        assert controller.state.status_text() == "设备未获对话授权"
        frame_task = asyncio.create_task(backend._frames())
        try:
            await asyncio.sleep(0.05)
            assert not rig.vts.frames
            binding = Binding("device-demo", "binding-demo", frozenset({"avatar", "speak"}))
            backend.reconnect_session()
            await wait_for(lambda: bool(rig.vts.frames))
            assert controller.state.can_send
            binding = Binding("device-demo", "binding-demo", frozenset({"speak"}))
            await wait_for(lambda: controller.state.permissions == {"speak"})
            newest = len(rig.vts.frames)
            await asyncio.sleep(0.05)
            assert len(rig.vts.frames) == newest
            binding = Binding("device-demo", "binding-demo", frozenset({"avatar", "speak"}))
            backend.reconnect_session()
            await wait_for(lambda: len(rig.vts.frames) > newest)
            failure = "connection_lost"
            await wait_for(lambda: session.connection["state"] == "reconnecting")
            assert not session.dispatcher.permissions
            newest = len(rig.vts.frames)
            await wait_for(lambda: len(rig.vts.frames) > newest)
            failure = "unauthorized"
            backend.reconnect_session()
            await wait_for(lambda: controller.state.connection_closed)
            newest = len(rig.vts.frames)
            await asyncio.sleep(0.05)
            assert len(rig.vts.frames) == newest
            assert controller.state.status_text() == "设备绑定或连接凭据未获授权"
            assert "正在重连" not in controller.panel.banner.detail.text()
        finally:
            frame_task.cancel()
            await asyncio.gather(frame_task, return_exceptions=True)

    backend.loop.run_until_complete(exercise())


@pytest.mark.parametrize("lose_ack", [True, False])
def test_device_turn_delivers_once_after_reconnect(device_controller, lose_ack):
    controller = device_controller
    backend = controller.backend

    async def exercise():
        clock = [0]
        host = SessionHost(clock=lambda: clock[0])
        binding = Binding("device-demo", "binding-demo", frozenset({"speak"}))
        chat = FakeChat()
        sends = []

        async def transport(operation, payload):
            result = host.handle(binding, operation, payload, turns=chat)
            for frame in payload.get("messages", ()):
                if frame["type"] == "turn.send":
                    sends.append(frame["body"]["client_msg_id"])
                    if lose_ack and len(sends) == 1:
                        raise ConnectionError("connection_lost")
            return result

        session = backend.session
        session._transport = transport
        session._poll_interval = 0.01
        session.start()
        try:
            await wait_for(lambda: session.connection["state"] == "connected")
            generation = session.session["generation"]
            controller.send("Example message")
            client_id = controller.active_turn
            bubble = users(controller)[0]
            if not lose_ack:
                await wait_for(lambda: bubble.receipt == "delivered")
                clock[0] += host.lease_seconds  # The next poll finds the generation expired.
            await wait_for(lambda: session.connection["state"] == "reconnecting")
            assert session.connection["reason"] == ("connection_lost" if lose_ack else "stale_session")
            assert controller.active_turn == client_id
            await wait_for(lambda: session.connection["state"] == "connected" and bubble.receipt == "delivered")
            assert session.session["generation"] > generation
            assert controller.state.presence == "thinking"
            chat.result.set_result(TurnResult("completed", "Example reply."))
            await wait_for(lambda: bool(replies(controller)))
            assert [bubble.text for bubble in replies(controller)] == ["Example reply."]
            assert controller.active_turn is None
            assert sends == [client_id] * (2 if lose_ack else 1)
            assert len(chat.calls) == 1 and not chat.cancelled
        finally:
            await session.close()
            if not chat.result.done():
                chat.result.set_result(TurnResult("failed"))

    backend.loop.run_until_complete(exercise())


@pytest.mark.parametrize("retry_offline", [True, False])
def test_device_manual_retry_reuses_original_message_id(device_controller, retry_offline):
    controller = device_controller
    backend = controller.backend
    session = backend.session

    async def exercise():
        host = SessionHost()
        binding = Binding("device-demo", "binding-demo", frozenset({"speak"}))
        chat = FakeChat()
        sends = []

        async def transport(operation, payload):
            response = host.handle(binding, operation, payload, turns=chat)
            for frame in payload.get("messages", ()):
                if frame["type"] == "turn.send":
                    sends.append(frame["body"]["client_msg_id"])
                    if len(sends) == 1:
                        # The host accepted the send, but the client has to reconnect manually.
                        raise ProtocolError("unauthorized")
            return response

        session._transport = transport
        session._poll_interval = 0.01
        session.start()
        await wait_for(lambda: controller.state.can_send)
        controller.send("Example message")
        client_id = controller.active_turn
        await wait_for(lambda: session.connection["state"] == "closed" and bool(chat.calls))
        bubble = users(controller)[0]
        assert bubble.receipt == "failed"
        chat.result.set_result(TurnResult("completed", "Saved reply."))
        if retry_offline:
            bubble.retry_button.click()
            assert bubble.receipt == "queued"
        backend.reconnect_session()
        await wait_for(lambda: controller.state.can_send)
        if not retry_offline:
            bubble.retry_button.click()
        await wait_for(lambda: len(sends) == 2)
        assert sends == [client_id, client_id]
        await wait_for(lambda: bool(replies(controller)))
        assert len(chat.calls) == 1
        assert bubble.receipt == "resent"
        assert [bubble.text for bubble in replies(controller)] == ["Saved reply."]

    backend.loop.run_until_complete(exercise())


@pytest.mark.parametrize("status", ["cancelled", "completed", "failed"])
def test_device_waits_for_stopped_turn_before_sending_next(device_controller, status):
    controller = device_controller
    backend = controller.backend
    session = backend.session

    async def exercise():
        host = SessionHost()
        binding = Binding("device-demo", "binding-demo", frozenset({"speak"}))
        first = FakeChat(stop=False)
        next_chat = FakeChat()
        next_chat.result.set_result(TurnResult("completed", "Next reply."))

        async def transport(operation, payload):
            return host.handle(binding, operation, payload, turns=first if not first.result.done() else next_chat)

        session._transport = transport
        session._poll_interval = 0.01
        session.start()
        try:
            await wait_for(lambda: controller.state.can_send)
            controller.send("First message")
            await wait_for(lambda: bool(first.calls))
            controller.stop()
            await wait_for(lambda: bool(first.cancelled))
            controller.send("Next message")
            first_bubble, next_bubble = users(controller)
            assert next_bubble.receipt == "queued"
            assert not next_chat.calls
            first.result.set_result(TurnResult(status, "Late reply."))
            await wait_for(lambda: bool(replies(controller)))
            if status == "cancelled":
                assert first_bubble.meta_label.text() == "已取消"
            assert next_bubble.receipt == "delivered"
            assert [bubble.text for bubble in replies(controller)] == ["Next reply."]
        finally:
            if not first.result.done():
                first.result.set_result(TurnResult("failed"))

    backend.loop.run_until_complete(exercise())
