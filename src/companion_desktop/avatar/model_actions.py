"""Plan and verify model state on the desktop output side of P0's contract."""

import asyncio
from dataclasses import dataclass
from math import isclose
from typing import Callable, Protocol

from .model_catalog import ModelCatalog, ModelReadback


class StaleOutputError(Exception):
    """The output adapter rejected a stale task or model before any write."""


class ModelOutput(Protocol):
    """Local output seam for the desktop dispatcher and VTS channel owner.

    P1's AvatarOutput.start passes its is_current callback through to intent
    work. AvatarOutput.snapshot can reconcile the latest persistent selection
    with a guard for the current session and snapshot. The caller serializes
    both paths. The adapter checks the guard and observed epoch/model immediately
    before each VTS write. Failures with uncertain writes (including partial
    batches) surface as OSError. StaleOutputError promises zero writes.
    """

    async def read_model(self) -> ModelReadback: ...

    async def set_expressions(
        self, observed: ModelReadback, changes: tuple[tuple[str, bool], ...], *,
        is_current: Callable[[], bool],
    ) -> None:
        """Apply in order with explicit activation and zero fade, through the VTS owner."""
        ...

    async def trigger_animation(
        self, observed: ModelReadback, hotkey_id: str, *, is_current: Callable[[], bool],
    ) -> str:
        """Dispatch once and return the evidence ref for the VTS acknowledgement."""
        ...


@dataclass(frozen=True)
class ModelSelection:
    """Persistent state. None leaves that group untouched. An empty set clears a group."""

    activity: str | None = None
    outfit: str | None = None
    accessories: frozenset[str] | None = None
    expressions: frozenset[str] | None = None


@dataclass(frozen=True)
class ActionResult:
    """Output result for the dispatcher to attach to its task or wardrobe receipt."""

    status: str
    reason_code: str | None = None
    evidence: dict[str, str] | None = None
    detail: str | None = None


def _unsupported(detail: str, readback: ModelReadback | None = None) -> ActionResult:
    evidence = None if readback is None else {"source": "vts_capabilities", "ref": readback.ref}
    return ActionResult("unsupported", "missing_dependency", evidence, detail)


def _unknown(detail: str) -> ActionResult:
    return ActionResult("unknown", "result_unknown", detail=detail)


def _stale() -> ActionResult:
    return ActionResult("rejected", "stale_output", detail="task_or_model_changed_before_write")


async def apply_state(
    catalog: ModelCatalog, selection: ModelSelection, output: ModelOutput, *,
    is_current: Callable[[], bool],
) -> ActionResult:
    """Reconcile desired persistent state against the observed model.

    On reconnect, call with the latest host selection and a fresh output epoch.
    Outfit-only updates and temporary animation release need no activity writes.
    One-shots are outside the selection and cannot be replayed by reconciliation.
    """
    try:
        before = await output.read_model()
    except OSError:
        return _unsupported("readback_unavailable")
    if not is_current():
        return _stale()
    if not catalog.matches(before):
        return _unsupported("model_or_asset_mismatch", before)
    if selection == ModelSelection():
        return ActionResult("rejected", "invalid_config", detail="empty_selection")

    expected_expressions = {}
    expected_parameters = {}
    enabled_parameters = {}
    groups = (
        ("activity", catalog.activities, None if selection.activity is None else {selection.activity}),
        ("outfit", catalog.outfits, None if selection.outfit is None else {selection.outfit}),
        ("accessory", catalog.accessories, selection.accessories),
        ("expression", catalog.expressions, selection.expressions),
    )
    for group, bindings, requested in groups:
        if requested is None:
            continue
        if not requested <= bindings.keys():
            return _unsupported(f"unmapped_{group}", before)
        if group == "expression":
            suppressed = {
                lower for name in requested if catalog.expression_available(bindings[name], before)
                for lower in catalog.expression_overrides.get(name, ())
            }
            requested = requested - suppressed
        desired = {bindings[name] for name in requested}
        if any(not catalog.expression_available(file, before) for file in desired):
            return _unsupported(f"missing_{group}_resource", before)

        for file in set(bindings.values()) & before.expressions.keys():
            expected_expressions[file] = file in desired
            for parameter, value in catalog.expression_files[file].items():
                if parameter in before.parameters:
                    expected_parameters[parameter] = catalog.defaults[parameter]
                if file in desired:
                    if enabled_parameters.setdefault(parameter, value) != value:
                        return ActionResult("rejected", "invalid_config", detail="overlapping_expressions")
    expected_parameters.update(enabled_parameters)

    # Disable old expressions first. Enabling a new pose alone can leave two
    # overwrite expressions competing for the same Cubism output.
    changes = tuple(sorted(
        ((file, active) for file, active in expected_expressions.items() if before.expressions[file] != active),
        key=lambda change: (change[1], change[0]),
    ))
    def verify(after):
        if not is_current() or after.epoch != before.epoch or not catalog.matches(after):
            return _unknown("model_changed_before_verification")
        if any(after.expressions.get(file) is not active for file, active in expected_expressions.items()):
            return _unknown("expression_state_unconfirmed")
        if any(
            parameter not in after.parameters or not isclose(after.parameters[parameter], value, rel_tol=0, abs_tol=0.02)
            for parameter, value in expected_parameters.items()
        ):
            return _unknown("parameter_state_unconfirmed")
        return ActionResult("completed", evidence={"source": "vts_model_readback", "ref": after.ref})

    try:
        if changes:
            await output.set_expressions(before, changes, is_current=is_current)
        result = verify(before)
        for delay in (0.05, 0.05, 0.2, 0.5, 1.25):
            if result.detail not in {"expression_state_unconfirmed", "parameter_state_unconfirmed"}:
                break
            await asyncio.sleep(delay)
            if not is_current():
                return _unknown("model_changed_before_verification")
            result = verify(await output.read_model())
        return result
    except StaleOutputError:
        return _stale()
    except OSError:
        return _unknown("write_or_readback_lost")


async def trigger_event(
    catalog: ModelCatalog, name: str, output: ModelOutput, *, is_current: Callable[[], bool],
) -> ActionResult:
    """Send a current event once. P1 owns the task ledger, including lost acknowledgements."""
    try:
        current = await output.read_model()
    except OSError:
        return _unsupported("readback_unavailable")
    if not is_current():
        return _stale()
    if not catalog.matches(current):
        return _unsupported("model_or_asset_mismatch", current)
    event = catalog.events.get(name)
    if event is None or not catalog.event_available(event, current):
        return _unsupported("missing_event_hotkey", current)
    try:
        ref = await output.trigger_animation(current, event.hotkey_id, is_current=is_current)
    except StaleOutputError:
        return _stale()
    except OSError:
        return _unknown("event_acknowledgement_lost")
    return ActionResult("accepted", evidence={"source": "vts_hotkey_ack", "ref": ref})
