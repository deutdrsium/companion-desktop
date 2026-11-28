"""Project the versioned mod snapshot and check side effects against fresh state."""

import json
import re
from dataclasses import dataclass

from ...contracts.wire import (
    MAX_GAME_CHAT_EVENTS, MAX_GAME_CHAT_TEXT, MAX_GAME_SEQUENCE, farmer_id, game_chat_text, game_id,
    game_sequence, tile_coordinate,
)
from .mcp_client import McpError


@dataclass
class CoopState:
    upstream_session: str
    snapshot: dict
    local: dict
    chat_events: list
    chat_seq: int
    gap: bool
    chat_safe: bool
    action_running: bool | None
    emote: str | None


def legacy_map(result):
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return None
    text = content[0].get("text")
    if not isinstance(text, str):
        return None
    match = re.match(r"地点：([A-Za-z0-9_-]{1,64})[（，]", text)
    return match[1] if match else None


def structured_result(result):
    state = result.get("structuredContent")
    if isinstance(state, dict):
        return state
    content = result.get("content")
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
        text = content[0].get("text")
        if content[0].get("type") == "text" and isinstance(text, str):
            try:
                state = json.loads(text)
            except (ValueError, RecursionError):
                pass
    if not isinstance(state, dict):
        raise McpError("invalid_coop_state")
    return state


def _roster(raw_farmers, aliases):
    if not isinstance(raw_farmers, list) or not 1 <= len(raw_farmers) <= 4:
        raise McpError("invalid_coop_roster")
    roster, names, emotes = {}, {}, {}
    for raw in raw_farmers:
        if not isinstance(raw, dict):
            raise McpError("invalid_coop_roster")
        key, name = raw.get("farmerId"), raw.get("farmerName")
        location = raw.get("map", raw.get("location"))
        x, y = raw.get("x"), raw.get("y")
        host, local = raw.get("isHost"), raw.get("isLocal")
        if (not farmer_id(key) or key in roster or not isinstance(name, str) or len(name) > 128
                or not game_id(location) or not tile_coordinate(x) or not tile_coordinate(y)
                or type(host) is not bool or type(local) is not bool):
            raise McpError("invalid_coop_roster")
        roster[key] = {"farmer_id": key, "map": location, "x": x, "y": y, "is_host": host, "is_local": local}
        if name:
            names.setdefault(name, []).append(key)
        emote = raw.get("emote")
        emotes[key] = emote if game_id(emote) else None
    if sum(item["is_host"] for item in roster.values()) != 1 or sum(item["is_local"] for item in roster.values()) != 1:
        raise McpError("invalid_coop_roster")
    current = {key: alias for key, alias in aliases.items() if key in roster}
    available = iter(number for number in range(1, 5) if number not in current.values())
    for key in sorted(roster, key=int):
        if key not in current:
            current[key] = next(available)
        roster[key]["alias"] = f"Farmer {current[key]}"
    aliases.clear()
    aliases.update(current)
    local = next(item for item in roster.values() if item["is_local"])
    return roster, names, local, emotes[local["farmer_id"]]


def _public_events(state, after_seq, roster, names):
    raw_events = state.get("chatEvents")
    chat_safe = (isinstance(raw_events, list) and len(raw_events) <= 256
                 and type(state.get("gap")) is bool and state.get("publicChatEnabled", True) is True)
    if not chat_safe:
        return [], after_seq, False, False
    candidates = {}
    for raw in raw_events:
        if (not isinstance(raw, dict) or not game_sequence(raw.get("seq"))
                or not farmer_id(raw.get("farmerId")) or raw.get("channel") not in ("public", "private", "system")):
            return [], after_seq, False, False
        seq = raw["seq"]
        if seq > after_seq:
            if seq in candidates and candidates[seq] != raw:
                return [], after_seq, False, False
            candidates[seq] = raw
    gap = state["gap"]
    events, cursor = [], after_seq
    replacements = {name: roster[ids[0]]["alias"] if len(ids) == 1 else "Farmer" for name, ids in names.items()}
    names_pattern = None
    if replacements:
        names_pattern = re.compile("|".join(re.escape(name) for name in sorted(replacements, key=len, reverse=True)))
    for seq, raw in sorted(candidates.items()):
        gap |= bool(cursor and seq > cursor + 1)
        cursor = seq
        key, text = raw["farmerId"], raw.get("text")
        if raw["channel"] != "public":
            continue
        if key not in roster or not game_chat_text(text):
            gap = True
            continue
        if names_pattern is not None:
            text = names_pattern.sub(lambda match: replacements[match[0]], text)
        events.append({"seq": seq, "farmer_id": key, "channel": "public", "text": text[:MAX_GAME_CHAT_TEXT]})
    latest = state.get("chatSeq", cursor)
    if type(latest) is not int or not cursor <= latest <= MAX_GAME_SEQUENCE:
        return [], after_seq, False, False
    gap |= latest > cursor or len(events) > MAX_GAME_CHAT_EVENTS
    return events[-MAX_GAME_CHAT_EVENTS:], latest, gap, True


def parse_state(result, after_seq, aliases, allowed_tiles, *, upstream_session=None):
    state = structured_result(result)
    if type(state.get("version")) is not int or state["version"] != 1:
        raise McpError("invalid_coop_state")
    if (state.get("worldReady") is not True or state.get("isMultiplayer") is not True
            or state.get("playerInControl") is not False or state.get("agentRunning", False) is not False):
        return None
    source = state.get("gameSessionId")
    if not game_id(source):
        raise McpError("invalid_coop_session")
    if upstream_session is not None and source != upstream_session:
        return None
    roster, names, local, emote = _roster(state.get("farmers"), aliases)
    events, cursor, gap, chat_safe = _public_events(state, after_seq, roster, names)
    watering = {}
    raw_tiles = state.get("wateringTiles")
    if local["map"] == "Farm" and isinstance(raw_tiles, list) and len(raw_tiles) <= 1024:
        for tile in raw_tiles:
            if (isinstance(tile, dict) and tile_coordinate(tile.get("x")) and tile_coordinate(tile.get("y"))
                    and type(tile.get("watered")) is bool and tile.get("map", "Farm") == "Farm"):
                point = tile["x"], tile["y"]
                if point in allowed_tiles:
                    watering[point] = tile["watered"] or watering.get(point, False)
    snapshot = {"game": "stardew", "farmers": sorted(roster.values(), key=lambda item: item["alias"]),
                "watering_tiles": [{"x": x, "y": y, "watered": watered}
                                   for (x, y), watered in sorted(watering.items())]}
    farm_ref = state.get("farmRef")
    if game_id(farm_ref) and farm_ref not in names:
        snapshot["farm_ref"] = farm_ref
    running = state.get("actionRunning")
    return CoopState(source, snapshot, local, events, cursor, gap, chat_safe,
                     running if type(running) is bool else None, emote)


def confirmed(action, params, before, after, *, public_text=None):
    if (after is None or before.local["farmer_id"] != after.local["farmer_id"]
            or before.upstream_session != after.upstream_session):
        return False
    if action == "move_to":
        return (before.local["map"] == after.local["map"] == params["map"]
                and (after.local["x"], after.local["y"]) == (params["x"], params["y"]))
    if action == "use_tool":
        return before.local["map"] == after.local["map"] == "Farm" and any(
            tile == {"x": params["x"], "y": params["y"], "watered": True}
            for tile in after.snapshot["watering_tiles"]
        )
    if action == "emote":
        return before.emote != params["emote"] and after.emote == params["emote"]
    if action == "say_public":
        return after.chat_safe and any(event["farmer_id"] == after.local["farmer_id"]
                                       and event["seq"] > before.chat_seq and event["text"] == public_text
                                       for event in after.chat_events)
    if action == "cancel_action":
        return before.action_running is True and after.action_running is False
    return False
