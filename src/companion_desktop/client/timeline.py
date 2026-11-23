"""Map playback positions to local mouth levels and sentence motion cues."""

from array import array
import io
from math import sqrt
import re
import sys
import wave


LEVEL_INTERVAL_MS = 20


def wav_levels(content):
    """Read a short RMS envelope from the same PCM WAV sent to the player."""
    try:
        with wave.open(io.BytesIO(content), "rb") as audio:
            width, rate = audio.getsampwidth(), audio.getframerate()
            if width not in (1, 2, 3, 4) or rate <= 0:
                return ()
            window = max(1, rate * LEVEL_INTERVAL_MS // 1000)
            scale = 1 << (8 * width - 1)
            levels = []
            for _ in range(0, audio.getnframes(), window):
                data = audio.readframes(window)
                if width == 1:
                    samples = [value - 128 for value in data]
                elif width == 3:
                    samples = [int.from_bytes(data[i:i + 3], "little", signed=True) for i in range(0, len(data), 3)]
                else:
                    samples = array("h" if width == 2 else "i", data)
                    if sys.byteorder != "little":
                        samples.byteswap()
                if not samples:
                    break
                rms = sqrt(sum(value * value for value in samples) / len(samples)) / scale
                levels.append(min(1.0, max(0.0, (rms - 0.015) * 5.0)))
            return tuple(levels)
    except (wave.Error, EOFError, ValueError):
        return ()


class CueTrack:
    """One presentation pass. Resume/replay can reuse audio without replaying cues."""

    def __init__(self, segments):
        self.segments = {int(segment["segment_id"]): segment for segment in segments
                         if segment["segment_id"].isascii() and segment["segment_id"].isdecimal()
                         and len(segment["segment_id"]) <= 6}
        self.consumed = set()
        self.last_accompaniment = float("-inf")

    def advance(self, index, text, audio_id, position_ms, duration_ms, now):
        segment = self.segments.get(index)
        explicit = segment["cues"] if segment and segment["text"] == text else ()
        pending = []
        spoken = text.strip() if audio_id is not None else text
        leading = len(text) - len(text.lstrip()) if audio_id is not None else 0
        for cue in explicit:
            params = cue["params"]
            time_ms = params.get("audio_offset_ms")
            timestamp = (audio_id is not None and segment["audio_id"] == audio_id
                         and type(time_ms) is int and 0 <= time_ms <= duration_ms)
            offset = min(len(spoken), max(0, cue["text_offset"] - leading))
            due = time_ms if timestamp else offset * duration_ms / max(1, len(spoken))
            pending.append((due, cue, "timestamp" if timestamp else "character_estimate"))
        # Punctuation accompaniment is deliberately sparse beside explicit cues.
        if not explicit:
            punctuation = re.search(r"([!?！？])[!?！？…]*[”」』）)]*\s*$", text)
            if punctuation and now - self.last_accompaniment >= 2:
                action = "head_tilt" if punctuation[1] in "?？" else "nod"
                pending.append((duration_ms * 0.8, {"task_id": f"companion-{index}", "action": action,
                                                   "params": {"intensity": 0.35}}, "punctuation"))
        for due, cue, source in sorted(pending, key=lambda item: item[0]):
            task_id = cue["task_id"]
            if position_ms >= due and task_id not in self.consumed:
                self.consumed.add(task_id)
                if source == "punctuation":
                    self.last_accompaniment = now
                yield cue, source, position_ms - due
