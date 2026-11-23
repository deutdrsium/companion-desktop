"""B04/B05 desk entry replay with synthetic player samples and VTS readback."""

from array import array
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
from companion_desktop.contracts.wire import message
from companion_desktop.ui.paint import Character
from fakes import FakePlayer, FakeSubtitles
from test_desktop_animation import Rig
from test_controller import replies
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
