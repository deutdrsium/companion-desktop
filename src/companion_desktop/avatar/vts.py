"""Frame adapter for an authenticated, local VTube Studio API connection."""

from uuid import uuid4

from .model_actions import StaleOutputError
from .model_catalog import ModelReadback
from .motion import NEUTRAL, unit


# Semantic channels use the existing catalog's tracking input/ranges, never
# the Cubism parameter ID as an injection ID. Order resolves shared inputs:
# Stage4's head axes also drive the body unless separate inputs are mapped.
CHANNELS = {
    "head_yaw": "ParamAngleX", "head_pitch": "ParamAngleY", "head_roll": "ParamAngleZ",
    "body_yaw": "ParamBodyAngleX", "body_roll": "ParamBodyAngleZ", "breath": "ParamBreath",
    "look_x": "ParamEyeBallX", "look_y": "ParamEyeBallY",
    "eye_left": "ParamEyeLOpen", "eye_right": "ParamEyeROpen", "eye_smile": "ParamEyeSmile",
    "brow_left": "ParamBrowLY", "brow_right": "ParamBrowRY",
    "mouth_form": "ParamMouthForm", "mouth_open": "ParamMouthOpenY",
}


class VTSOutput:
    """Resolve owned inputs on bind, then send at most one batch per frame.

    The connection owner supplies async request(kind, data, *, is_current),
    rechecks the guard at socket send, and raises OSError on an unconfirmed
    request (including a VTS APIError). It forwards load/click events and calls
    invalidate on socket loss. Rebind only with a fresh P4 readback. VTS has no
    atomic model precondition on injection; the guard uses the last observed
    VTS model/connection. Release stops writes so the VTS input lease expires.
    """

    def __init__(self, request, *, owned):
        self._request = request
        self._owned = frozenset(owned)
        self.observed = self.catalog = None
        self.bindings = {}
        self.generation = 0

    def invalidate(self):
        self.generation += 1
        self.observed = self.catalog = None
        self.bindings = {}

    def bind(self, catalog, observed):
        self.invalidate()
        if not catalog.matches(observed):
            return
        persistent = {parameter for values in catalog.expression_files.values() for parameter in values}
        # Even an unusual mapping must not alias a tracking input that also
        # drives a persistent expression parameter.
        reserved = {item.input_id for item in catalog.inputs.values() if item.parameter in persistent}
        by_parameter = {item.parameter: item for item in catalog.inputs.values()}
        for channel, parameter in CHANNELS.items():
            binding = catalog.inputs.get(channel) or by_parameter.get(parameter)
            if (channel in self._owned and binding is not None and binding.input_id not in reserved
                    and binding.input_id in observed.inputs and binding.parameter in observed.parameters
                    and binding.output_range[0] != binding.output_range[1]):
                self.bindings[channel] = binding
                reserved.add(binding.input_id)
        self.observed, self.catalog = observed, catalog

    async def subscribe_events(self):
        generation = self.generation
        for event, config in (("ModelLoadedEvent", {}), ("ModelMovedEvent", {}),
                              ("ModelClickedEvent", {"onlyClicksOnModel": True})):
            await self._request("EventSubscriptionRequest", {
                "eventName": event, "subscribe": True, "config": config,
            }, is_current=lambda: self.generation == generation)

    async def read_model(self):
        observed = self.observed
        if observed is None:
            raise OSError("No mapped VTS model")
        current = lambda: self.observed is observed
        try:
            expressions = await self._request("ExpressionStateRequest", {"details": False}, is_current=current)
            hotkeys = await self._request("HotkeysInCurrentModelRequest", {}, is_current=current)
            parameters = await self._request("Live2DParameterListRequest", {}, is_current=current)
        except StaleOutputError as error:
            raise OSError("VTS model changed") from error
        if not current() or not all(
            ModelReadback.is_loaded(reply, observed.model_id) for reply in (expressions, hotkeys, parameters)
        ):
            raise OSError("VTS model changed")
        return ModelReadback.from_vts(
            epoch=observed.epoch, ref=uuid4().hex, model=parameters, asset_version=observed.asset_version,
            inputs=({"name": name} for name in observed.inputs), parameters=parameters["parameters"],
            expressions=expressions["expressions"], hotkeys=hotkeys["availableHotkeys"],
        )

    async def set_expressions(self, observed, changes, *, is_current):
        def current():
            return self.observed is not None and self.observed.epoch == observed.epoch and is_current()

        for index, (file, active) in enumerate(changes):
            try:
                await self._request("ExpressionActivationRequest", {
                    "expressionFile": file, "active": active, "fadeTime": 0,
                }, is_current=current)
            except StaleOutputError as error:
                if index:
                    raise OSError("VTS expression batch interrupted") from error
                raise

    async def trigger_animation(self, observed, hotkey_id, *, is_current):
        def current():
            return self.observed is not None and self.observed.epoch == observed.epoch and is_current()

        result = await self._request("HotkeyTriggerRequest", {"hotkeyID": hotkey_id}, is_current=current)
        if result.get("hotkeyID") != hotkey_id or not current():
            raise OSError("VTS hotkey acknowledgement unconfirmed")
        return uuid4().hex

    async def write(self, frame, *, is_current):
        observed = self.observed
        if observed is None or not self.bindings or not is_current():
            raise StaleOutputError
        values = []
        for channel, binding in self.bindings.items():
            in_min, in_max = binding.input_range
            out_start, out_end = binding.output_range
            position = unit((frame.get(channel, NEUTRAL[channel]) - out_start) / (out_end - out_start))
            values.append({"id": binding.input_id, "value": in_min + (in_max - in_min) * position})
        generation = self.generation
        current = lambda: self.generation == generation and is_current()
        await self._request("InjectParameterDataRequest", {
            "mode": "set", "parameterValues": values,
        }, is_current=current)
        if not current():
            raise StaleOutputError
