"""Clean one completed reply and locate presentation cues in Unicode code points."""

from dataclasses import dataclass
import json
import re


_ACTIONS = {
    "点头": "nod", "nod": "nod", "忍笑": "hold_laugh", "hold_laugh": "hold_laugh",
    "开心": "hold_laugh", "happy": "hold_laugh", "好奇": "head_tilt", "curious": "head_tilt",
    "挥手": "wave", "蹦跳": "jump", "手到脸": "hand_to_face",
}
_HIDDEN = r"soullink|wardrobe|expression|pc_tts|tts|voice|record|send_message_to_user|语音|l2d"
_BRACKET = "|".join(map(re.escape, _ACTIONS))
_CONTROLS = re.compile(
    rf"<(?P<tag>{_HIDDEN})(?=[\s:/><]|$)"
    r"(?:(?P<attrs>[^<>]*)/>|(?P<colon>\s*:[^<>]*)>|[^<>]*>(?P<body>.*?)</(?P=tag)\s*>|[^<>]*(?:>|$).*\Z)"
    rf"|</(?:{_HIDDEN})\s*>|\[(?P<bracket>{_BRACKET})\]",
    re.I | re.S,
)


@dataclass(frozen=True)
class Anchor:
    offset: int
    action: str
    intensity: float = 1.0


def parse_reply(text):
    """Parse assembled stream chunks once. Unclosed hidden tails stay hidden."""
    pieces, anchors = [], []
    cursor = length = 0
    for match in _CONTROLS.finditer(text):
        part = text[cursor:match.start()]
        pieces.append(part)
        length += len(part)
        cursor = match.end()
        bracket, tag = match["bracket"], (match["tag"] or "").lower()
        if bracket:
            anchors.append(Anchor(length, _ACTIONS[bracket.lower()]))
        elif tag == "l2d":
            attrs = (match["attrs"] or "").lstrip()
            raw = match["colon"] or match["body"] or (attrs if attrs.startswith(":") else "")
            for label in re.split(r"[\s,，、|/]+", raw.lstrip(" :").strip()):
                if label.lower() not in {"", "none", "无", "null", "no"}:
                    anchors.append(Anchor(length, _ACTIONS.get(label.lower(), "unsupported")))
        elif tag == "soullink" and match["body"]:
            try:
                intent = json.loads(match["body"])
            except ValueError:
                continue
            if isinstance(intent, dict):
                emotion = str(intent.get("emotion", "")).strip().lower()
                action = _ACTIONS.get(emotion)
                strength = intent.get("intensity", 0.7)
                if action and type(strength) in (float, int) and 0 < strength <= 1:
                    anchors.append(Anchor(length, action, strength))
    pieces.append(text[cursor:])
    clean = "".join(pieces)
    leading = len(clean) - len(clean.lstrip())
    clean = clean.strip()
    # A legacy tail and an inline hint may describe the same motion. Keep the
    # first explicit occurrence per offset and prefer inline timing to the tail.
    unique = {}
    for cue in anchors:
        offset = min(len(clean), max(0, cue.offset - leading))
        if offset == len(clean) and any(action == cue.action for _, action in unique):
            continue
        unique.setdefault((offset, cue.action), Anchor(offset, cue.action, cue.intensity))
    return clean, tuple(unique.values())


def sentence_spans(clean):
    """Return nonempty spans of at most 1000 code points for the speech API."""
    spans = []
    for match in re.finditer(r".*?(?:[。！？!?；;…]+[”」』）)]*|\n+|$)", clean, re.S):
        start, end = match.span()
        while end - start > 1000:
            piece = clean[start:start + 1000]
            cut = max(piece.rfind("，"), piece.rfind(","), piece.rfind(" "))
            cut = cut + 1 if cut > 0 else 1000
            if clean[start:start + cut].strip():
                spans.append((start, start + cut))
            start += cut
        if clean[start:end].strip():
            spans.append((start, end))
        elif start < end and spans and end - spans[-1][0] <= 1000:
            spans[-1] = (spans[-1][0], end)
    return spans


def utterance_segments(turn_id, text, anchors):
    """Build v1 utterance frames for sentences carrying explicit cues."""
    start_index = 0
    spans = sentence_spans(text)
    for index, (start, end) in enumerate(spans):
        cues = []
        limit = end if index < len(spans) - 1 else len(text) + 1
        while start_index < len(anchors) and anchors[start_index].offset < limit:
            cue = anchors[start_index]
            cues.append({"task_id": f"{turn_id}-cue-{start_index}", "action": cue.action,
                         "params": {"intensity": cue.intensity},
                         "text_offset": min(end - start, max(0, cue.offset - start))})
            start_index += 1
        if cues:
            yield {"turn_id": turn_id, "utterance_id": turn_id, "segment_id": str(index),
                   "text": text[start:end], "audio_id": None, "cues": cues}
