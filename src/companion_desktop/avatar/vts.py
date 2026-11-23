"""Frame adapter for an authenticated, local VTube Studio API connection."""

from .model_actions import StaleOutputError
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
        self.observed = None
        self.bindings = {}
        self.generation = 0

    def invalidate(self):
        self.generation += 1
        self.observed = None
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
            binding = by_parameter.get(parameter)
            if (channel in self._owned and binding is not None and binding.input_id not in reserved
                    and binding.input_id in observed.inputs and parameter in observed.parameters
                    and binding.output_range[0] != binding.output_range[1]):
                self.bindings[channel] = binding
                reserved.add(binding.input_id)
        self.observed = observed

    async def subscribe_events(self):
        generation = self.generation
        for event, config in (("ModelLoadedEvent", {}), ("ModelClickedEvent", {"onlyClicksOnModel": True})):
            await self._request("EventSubscriptionRequest", {
                "eventName": event, "subscribe": True, "config": config,
            }, is_current=lambda: self.generation == generation)

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
