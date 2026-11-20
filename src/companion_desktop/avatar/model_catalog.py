"""Load local model bindings and intersect them with the current VTS catalog."""

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Mapping


class MappingError(ValueError):
    """Invalid local model configuration."""


@dataclass(frozen=True)
class InputBinding:
    input_id: str
    parameter: str
    input_range: tuple[float, float]
    output_range: tuple[float, float]


@dataclass(frozen=True)
class EventBinding:
    hotkey_id: str
    file: str
    duration_ms: int


@dataclass(frozen=True)
class ModelReadback:
    """One coherent read of the VTS model and its resources.

    The output adapter rotates epoch on reconnect or model reload and allocates
    an evidence ref for each read. Asset version comes from the local installation.
    Parameters are Live2D outputs, separate from the set of tracking inputs.
    """

    epoch: str
    ref: str
    model_id: str | None
    asset_version: str | None
    inputs: frozenset[str]
    parameters: Mapping[str, float]
    expressions: Mapping[str, bool]
    animation_hotkeys: Mapping[str, str]

    @classmethod
    def from_vts(cls, *, epoch, ref, model, asset_version, inputs, parameters, expressions, hotkeys):
        """Normalize results from the current-model, input, output, expression and hotkey reads."""
        return cls(
            epoch=epoch,
            ref=ref,
            model_id=model["modelID"] if model["modelLoaded"] else None,
            asset_version=asset_version,
            inputs=frozenset(row["name"] for row in inputs),
            parameters={row["name"]: row["value"] for row in parameters},
            expressions={row["file"]: row["active"] for row in expressions},
            animation_hotkeys={
                row["hotkeyID"]: row["file"]
                for row in hotkeys if row["type"] == "TriggerAnimation"
            },
        )


@dataclass(frozen=True)
class ModelCatalog:
    model_id: str
    asset_version: str
    inputs: Mapping[str, InputBinding]
    defaults: Mapping[str, float]
    expression_files: Mapping[str, Mapping[str, float]]
    activities: Mapping[str, str]
    outfits: Mapping[str, str]
    accessories: Mapping[str, str]
    expressions: Mapping[str, str]
    expression_overrides: Mapping[str, tuple[str, ...]]
    events: Mapping[str, EventBinding]

    def matches(self, readback: ModelReadback) -> bool:
        return (self.model_id, self.asset_version) == (readback.model_id, readback.asset_version)

    def expression_available(self, file: str, readback: ModelReadback) -> bool:
        return file in readback.expressions and self.expression_files[file].keys() <= readback.parameters.keys()

    def event_available(self, event: EventBinding, readback: ModelReadback) -> bool:
        return readback.animation_hotkeys.get(event.hotkey_id) == event.file

    def avatar_capabilities(self, readback: ModelReadback) -> dict:
        """P0 avatar payload. P1 supplies model identity, revision and permissions."""
        actions = []
        inputs = []
        hotkeys = []
        if self.matches(readback):
            inputs = sorted({
                binding.input_id for binding in self.inputs.values()
                if binding.input_id in readback.inputs and binding.parameter in readback.parameters
            })
            for category, bindings in (
                ("activity", self.activities), ("outfit", self.outfits),
                ("accessory", self.accessories), ("expression", self.expressions),
            ):
                actions.extend(
                    {"name": f"{category}.{name}", "binding": file}
                    for name, file in sorted(bindings.items()) if self.expression_available(file, readback)
                )
            for name, event in sorted(self.events.items()):
                if self.event_available(event, readback):
                    actions.append({"name": f"event.{name}", "binding": event.hotkey_id})
                    hotkeys.append(event.hotkey_id)
        return {"actions": actions, "inputs": inputs, "hotkeys": sorted(set(hotkeys))}


def _require(condition, field):
    if not condition:
        raise MappingError(f"Invalid model mapping field: {field}")


def _object(value, field, keys=None):
    _require(isinstance(value, dict) and all(_text(key) for key in value), field)
    if keys is not None:
        _require(value.keys() == keys, field)
    return value


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _range(value, field, *, ascending=False):
    _require(isinstance(value, list) and len(value) == 2 and all(map(_number, value)), field)
    if ascending:
        _require(value[0] < value[1], field)
    return tuple(value)


def _file(value, suffix):
    return _text(value) and value.endswith(suffix) and not any(char in value for char in "/\\:")


def _unique_object(pairs):
    result = dict(pairs)
    _require(len(result) == len(pairs), "duplicate key")
    return result


def load_mapping(path: str | Path) -> ModelCatalog:
    """Load a version 1 mapping. Errors name schema fields without logging local IDs."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError):
        raise MappingError("Model mapping is not valid UTF-8 JSON") from None
    fields = {
        "schema_version", "model_id", "asset_version", "inputs", "defaults", "expression_files",
        "activities", "outfits", "accessories", "expressions", "expression_overrides", "events",
    }
    _object(data, "root", fields)
    _require(type(data["schema_version"]) is int and data["schema_version"] == 1, "schema_version")
    for key in ("model_id", "asset_version"):
        _require(_text(data[key]), key)

    inputs = {}
    for channel, row in _object(data["inputs"], "inputs").items():
        _object(row, "inputs", {"id", "parameter", "input_range", "output_range"})
        _require(_text(row["id"]) and _text(row["parameter"]), "inputs")
        inputs[channel] = InputBinding(
            row["id"], row["parameter"],
            _range(row["input_range"], "inputs.input_range", ascending=True),
            _range(row["output_range"], "inputs.output_range"),
        )

    defaults = _object(data["defaults"], "defaults")
    _require(all(map(_number, defaults.values())), "defaults")
    files = _object(data["expression_files"], "expression_files")
    for file, values in files.items():
        _require(_file(file, ".exp3.json"), "expression_files")
        _object(values, "expression_files")
        _require(bool(values) and values.keys() <= defaults.keys(), "expression_files")
        _require(all(map(_number, values.values())), "expression_files")

    group_files = set()
    group_parameters = set()
    for group in ("activities", "outfits", "accessories", "expressions"):
        bindings = _object(data[group], group)
        _require(all(_text(file) and file in files for file in bindings.values()), group)
        used = set(bindings.values())
        parameters = {parameter for file in used for parameter in files[file]}
        _require(not (used & group_files or parameters & group_parameters), group)
        group_files.update(used)
        group_parameters.update(parameters)

    effects = data["expressions"]
    overrides = {}
    for effect, lower in _object(data["expression_overrides"], "expression_overrides").items():
        _require(effect in effects and isinstance(lower, list) and bool(lower), "expression_overrides")
        _require(all(_text(name) and name in effects for name in lower), "expression_overrides")
        _require(len(set(lower)) == len(lower), "expression_overrides")
        overrides[effect] = tuple(lower)
    _require(all(overrides.keys().isdisjoint(lower) for lower in overrides.values()), "expression_overrides")

    events = {}
    for name, row in _object(data["events"], "events").items():
        _object(row, "events", {"hotkey_id", "file", "duration_ms"})
        _require(_text(row["hotkey_id"]) and _file(row["file"], ".motion3.json"), "events")
        duration = row["duration_ms"]
        _require(type(duration) is int and duration > 0, "events.duration_ms")
        events[name] = EventBinding(row["hotkey_id"], row["file"], duration)

    return ModelCatalog(
        data["model_id"], data["asset_version"], inputs, defaults, files,
        data["activities"], data["outfits"], data["accessories"], effects, overrides, events,
    )
