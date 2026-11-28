"""Version negotiation and validation of desktop v1 JSON messages."""

import json
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import cast

from .types import PROTOCOL, RECEIPT_STATUSES, Message, SessionRef


MAX_REQUEST_BYTES = 262_144
GAME_STATE_FEATURE = "game.state"
GAME_STATE_OFFER = f"{PROTOCOL}+{GAME_STATE_FEATURE}"
MAX_GAME_WINDOW_MS = 60_000
MAX_GAME_CHAT_EVENTS = 32
MAX_GAME_CHAT_TEXT = 240
MAX_GAME_TILES = 32
MAX_GAME_SEQUENCE = 2**53 - 1


class ProtocolError(ValueError):
    """The message or version is invalid for this desktop protocol."""


def encode_request(operation, payload):
    return json.dumps({"operation": operation, "payload": payload}, ensure_ascii=False).encode("utf-8")


def now_utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def negotiate(protocols):
    if isinstance(protocols, list) and PROTOCOL in protocols:
        return PROTOCOL
    raise ProtocolError("unsupported_protocol")


def string(value):
    return isinstance(value, str) and bool(value.strip())


def uint(value):
    return type(value) is int and value >= 0


def positive(value):
    return uint(value) and value > 0


def utc(value):
    try:
        return (isinstance(value, str) and "T" in value and
                datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() == timedelta(0))
    except (ValueError, AttributeError):
        return False


def unit(value):
    return type(value) in (float, int) and 0 <= value <= 1


def nullable(check):
    return lambda value: value is None or check(value)


def array(check):
    return lambda value: isinstance(value, list) and all(check(item) for item in value)


def bounded_array(limit, check):
    return lambda value: isinstance(value, list) and len(value) <= limit and all(check(item) for item in value)


def one_of(*options):
    return lambda value: value in options


def obj(value):
    return isinstance(value, dict)


def fields(value, required, optional=None):
    optional = optional or {}
    return (obj(value) and required.keys() <= value.keys() <= required.keys() | optional.keys()
            and all(check(value[key]) for key, check in required.items())
            and all(check(value[key]) for key, check in optional.items() if key in value))


def shape(required, optional=None):
    return lambda value: fields(value, required, optional)


def parse_ref(value) -> SessionRef:
    if not fields(value, {"device_id": string, "desktop_session_id": string, "generation": positive}):
        raise ProtocolError("invalid_session")
    return cast(SessionRef, value)


_RECEIPT = {"task_id": string, "reason_code": nullable(string),
            "status": one_of(*RECEIPT_STATUSES), "occurred_at": utc,
            "evidence": nullable(shape({"source": string, "ref": string}))}
_TURN = {"turn_id": string, "client_msg_id": string}
_TRIGGERS = ("periodic", "autonomous", "user")
_OBSERVATION_RESULTS = ("observed", "skipped", "unchanged", "unavailable", "failed", "cancelled")
_OBSERVE = {"request_id": string, "trigger": one_of(*_TRIGGERS),
            "range": one_of("active_window"), "permission_generation": positive}


def observation_state(body):
    return (fields(body, {"request_id": string, "source": one_of(*_TRIGGERS),
                          "phase": one_of("requested", "capturing", "done"),
                          "license_generation": positive, "captured_at": nullable(utc),
                          "result": nullable(one_of(*_OBSERVATION_RESULTS))})
            and (body["result"] is not None) == (body["phase"] == "done"))


_MODEL = {"model_id": nullable(string), "asset_version": nullable(string), "revision": positive}
_INTENT = {"task_id": string, "scope": one_of("avatar", "game"), "action": string,
           "params": obj, "ttl_ms": uint, "capability_revision": positive}


def intent(body):
    if body.get("scope") == "avatar":
        return fields(body, {**_INTENT, "model_id": nullable(string)},
                      {"utterance_id": nullable(string), "segment_id": nullable(string)})
    return fields(body, {**_INTENT, "game_session_id": string, "window_id": string,
                         "window_revision": positive})


def result_valid(body):
    return ((body["status"] != "completed" or body["evidence"] is not None)
            and (body["status"] != "unknown" or body["reason_code"] == "result_unknown"))


def receipt(body):
    return fields(body, _RECEIPT) and result_valid(body)


def wardrobe_applied(body):
    return (fields(body, {**_MODEL, **{key: check for key, check in _RECEIPT.items() if key != "task_id"}})
            and result_valid(body))


def cancel(body):
    return (fields(body, {"task_id": string, "reason_code": string}) or
            fields(body, {"utterance_id": string, "reason_code": string}))


def utterance(body):
    cue = shape({"task_id": string, "action": string, "params": obj, "text_offset": uint})
    return (fields(body, {"turn_id": string, "utterance_id": string, "segment_id": string,
                          "text": lambda value: isinstance(value, str), "audio_id": nullable(string),
                          "cues": array(cue)})
            and all(item["text_offset"] <= len(body["text"]) for item in body["cues"]))


def game_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None


def farmer_id(value):
    return (isinstance(value, str) and re.fullmatch(r"(?:0|-?[1-9][0-9]{0,19})", value) is not None
            and -(2**63) <= int(value) < 2**64)


def game_sequence(value):
    return positive(value) and value <= MAX_GAME_SEQUENCE


def tile_coordinate(value):
    return uint(value) and value <= 1023


def game_chat_text(value):
    return (string(value) and len(value) <= MAX_GAME_CHAT_TEXT
            and all(unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value))


_BOOL = lambda value: type(value) is bool
_TILE = {"x": tile_coordinate, "y": tile_coordinate}
_FARMER = shape({**_TILE, "farmer_id": farmer_id, "alias": one_of("Farmer 1", "Farmer 2", "Farmer 3", "Farmer 4"),
                 "map": game_id, "is_host": _BOOL, "is_local": _BOOL})
_GAME_SNAPSHOT = shape({
    "game": one_of("stardew"), "farmers": bounded_array(4, _FARMER),
    "watering_tiles": bounded_array(MAX_GAME_TILES, shape({**_TILE, "watered": _BOOL})),
}, {"farm_ref": game_id})
_GAME_CHAT = shape({"seq": game_sequence, "farmer_id": farmer_id, "channel": one_of("public"), "text": game_chat_text})


def game_state(body):
    if not fields(body, {
        "game_session_id": game_id, "window_id": game_id, "window_revision": game_sequence,
        "window_ttl_ms": lambda value: uint(value) and value <= MAX_GAME_WINDOW_MS,
        "seq": game_sequence, "snapshot": nullable(_GAME_SNAPSHOT),
        "chat_events": bounded_array(MAX_GAME_CHAT_EVENTS, _GAME_CHAT), "gap": _BOOL,
    }):
        return False
    if not body["window_ttl_ms"]:
        return body["snapshot"] is None and not body["chat_events"] and not body["gap"]
    if body["snapshot"] is None:
        return False
    farmers = body["snapshot"]["farmers"]
    ids = {item["farmer_id"] for item in farmers}
    events = body["chat_events"]
    return (len(ids) == len(farmers) == len({item["alias"] for item in farmers})
            and sum(item["is_local"] for item in farmers) == 1
            and sum(item["is_host"] for item in farmers) == 1
            and all(item["farmer_id"] in ids for item in events)
            and all(earlier["seq"] < later["seq"] for earlier, later in zip(events, events[1:])))


_CHECKS = {
    "capabilities": shape({**_MODEL,
        "avatar": shape({"actions": array(shape({"name": string, "binding": string})),
                         "inputs": array(string), "hotkeys": array(string)}),
        "observations": array(one_of("active_window")),
        "games": array(shape({"game_session_id": string, "capability_revision": positive,
                              "actions": array(shape({"name": string, "schema": nullable(obj)}))}))},
        {name: lambda value: type(value) is bool for name in ("tts", "mouth", "subtitle")}),
    "role_state": shape({"role_ref": string, "revision": positive, "source": string,
                         "observed_at": utc, "ttl_ms": uint, "activity": nullable(string),
                         "energy": nullable(unit), "sleepiness": nullable(unit), "emotion": nullable(string)}),
    "intent": intent,
    "utterance": utterance,
    "cancel": cancel,
    "receipt": receipt,
    "observe": shape({**_OBSERVE, "ttl_ms": uint}),
    "observation": shape({**_OBSERVE, "status": one_of(*_OBSERVATION_RESULTS),
                           "captured_at": nullable(utc), "source": string,
                           "result_ref": nullable(string)}),
    "wardrobe": shape({**_MODEL, "selection": nullable(obj), "mapping": nullable(obj), "updated_at": utc}),
    "wardrobe_applied": wardrobe_applied,
    "turn.send": shape({"client_msg_id": string, "text": string}),
    "turn.accepted": shape(_TURN),
    "turn.failed": shape({**_TURN, "reason": string}),
    "turn.completed": shape({**_TURN, "text": lambda value: isinstance(value, str)}),
    "turn.cancel": shape({"turn_id": string}),
    "turn.cancelled": shape({"turn_id": string, "status": one_of("cancelled", "unknown"), "reason": string}),
    "turn.text": shape({"turn_id": string, "seq": positive, "text": string}),
    "game.state": game_state,
}

CLIENT_TYPES = frozenset(("capabilities", "receipt", "observation", "wardrobe_applied", "turn.send", "turn.cancel",
                          "game.state"))
HOST_TYPES = frozenset(_CHECKS) - CLIENT_TYPES


def _json(value):
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_json(item) for item in value)
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or type(value) in (str, bool, int)


def parse(value) -> Message:
    if not fields(value, {"protocol": one_of(PROTOCOL), "type": string, "session": obj, "body": obj}):
        raise ProtocolError("invalid_envelope")
    parse_ref(value["session"])
    check = _CHECKS.get(value["type"])
    try:
        valid = check is not None and check(value["body"]) and _json(value["body"])
    except RecursionError:
        valid = False
    if not valid:
        raise ProtocolError("invalid_body")
    return cast(Message, value)


def message(kind, session, body) -> Message:
    return parse({"protocol": PROTOCOL, "type": kind, "session": session, "body": body})


def decode(payload) -> Message:
    try:
        return parse(json.loads(payload))
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise ProtocolError("invalid_json") from exc


def encode(value) -> str:
    return json.dumps(parse(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
