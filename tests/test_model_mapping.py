import asyncio
import json
from dataclasses import replace
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from companion_desktop.avatar import model_actions
from companion_desktop.avatar.local_vts import LocalVTS
from companion_desktop.avatar.model_actions import ModelSelection, StaleOutputError
from companion_desktop.avatar.model_catalog import MappingError, ModelReadback, load_mapping
from companion_desktop.avatar.procedural import ProceduralAvatar
from companion_desktop.avatar.vts import VTSOutput
from companion_desktop.contracts.wire import message
from companion_desktop.session.client import DesktopSession
from plugins.astrbot_plugin_desktop_access.session import Binding, SessionHost


apply_state = partial(model_actions.apply_state, is_current=lambda: True)
trigger_event = partial(model_actions.trigger_event, is_current=lambda: True)
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "model-capabilities.example.json"


class MemoryOutput:
    def __init__(self, catalog):
        self.catalog = catalog
        self.expression_writes = []
        self.animation_writes = []
        self.epoch = "epoch-demo-1"
        self.model_id = catalog.model_id
        self.asset_version = catalog.asset_version
        self.input_ids = {binding.input_id for binding in catalog.inputs.values()}
        self.input_defaults = {binding.parameter: 0 for binding in catalog.inputs.values()}
        self.expressions = dict.fromkeys(catalog.expression_files, False)
        self.hotkeys = {event.hotkey_id: event.file for event in catalog.events.values()}
        self.read_count = 0

    async def read_model(self):
        self.read_count += 1
        values = {**self.input_defaults, **self.catalog.defaults}
        for file, active in self.expressions.items():
            if active:
                values.update(self.catalog.expression_files[file])
        return ModelReadback(
            self.epoch, f"read-demo-{self.read_count}", self.model_id, self.asset_version,
            frozenset(self.input_ids), values, self.expressions.copy(), self.hotkeys.copy(),
        )

    async def set_expressions(self, observed, changes, *, is_current):
        if not is_current() or observed.epoch != self.epoch:
            raise StaleOutputError
        self.expression_writes.append(changes)
        self.expressions.update(changes)

    async def trigger_animation(self, observed, hotkey_id, *, is_current):
        if not is_current() or observed.epoch != self.epoch:
            raise StaleOutputError
        self.animation_writes.append(hotkey_id)
        return "ack-demo-1"


class MappingTests(unittest.TestCase):
    def test_example_preserves_reversed_output_axis(self):
        catalog = load_mapping(EXAMPLE)
        self.assertEqual(catalog.inputs["look_x"].output_range, (1, -1))
        self.assertEqual(catalog.activities["sketching"], catalog.activities["drawing"])

    def test_rejects_bad_resource_reference_and_cross_group_write(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mapping.json"
            for resource in ("demo-missing.exp3.json", "demo-sketch.exp3.json"):
                with self.subTest(resource=resource):
                    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
                    data["outfits"]["jacket"] = resource
                    path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaisesRegex(MappingError, "outfits"):
                        load_mapping(path)

    def test_rejects_non_finite_and_duplicate_fields(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mapping.json"
            sample = EXAMPLE.read_text(encoding="utf-8")
            for invalid in (sample.replace('"DemoPose": 0', '"DemoPose": NaN'),
                            sample.replace('"DemoPose": 0', '"DemoPose": 0, "DemoPose": 1')):
                path.write_text(invalid, encoding="utf-8")
                with self.assertRaises(MappingError):
                    load_mapping(path)

    def test_runtime_only_uses_animation_hotkeys(self):
        result = ModelReadback.from_vts(
            epoch="epoch-demo-1", ref="read-demo-1",
            model={"modelLoaded": True, "modelID": "model-demo"}, asset_version="demo-v1",
            inputs=[{"name": "DemoPitchInput"}],
            parameters=[{"name": "DemoPose", "value": 1}],
            expressions=[{"file": "demo-sketch.exp3.json", "active": True}],
            hotkeys=[
                {"type": "ToggleExpression", "file": "demo-sketch.exp3.json", "hotkeyID": "toggle-demo"},
                {"type": "TriggerAnimation", "file": "demo-spark.motion3.json", "hotkeyID": "animate-demo"},
            ],
        )
        self.assertEqual(result.animation_hotkeys, {"animate-demo": "demo-spark.motion3.json"})
        self.assertEqual(result.parameters, {"DemoPose": 1})


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = load_mapping(EXAMPLE)
        self.output = MemoryOutput(self.catalog)

    async def test_replaces_pose_and_keeps_it_on_outfit_update(self):
        self.output.expressions["demo-idle.exp3.json"] = True
        result = await apply_state(self.catalog, ModelSelection(activity="drawing"), self.output)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.evidence["ref"], "read-demo-2")
        self.assertEqual(self.output.expression_writes, [(
            ("demo-idle.exp3.json", False), ("demo-sketch.exp3.json", True),
        )])
        result = await apply_state(self.catalog, ModelSelection(outfit="jacket"), self.output)
        self.assertEqual(result.status, "completed")
        self.assertTrue(self.output.expressions["demo-sketch.exp3.json"])
        self.assertEqual(self.output.expression_writes[-1], (("demo-jacket.exp3.json", True),))

    async def test_effect_override_is_reversible_and_preserves_default(self):
        for effects, value in (({"dim", "glitter"}, 0.25), ({"dim"}, 0), (set(), 1)):
            result = await apply_state(
                self.catalog, ModelSelection(expressions=frozenset(effects)), self.output,
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual((await self.output.read_model()).parameters["DemoShine"], value)

    async def test_capabilities_and_receipt_reflect_missing_resources(self):
        self.output.expressions.pop("demo-sketch.exp3.json")
        self.output.input_ids.remove("DemoPitchInput")
        caps = self.catalog.avatar_capabilities(await self.output.read_model())
        self.assertEqual(caps["inputs"], ["DemoGazeInput"])
        names = {action["name"] for action in caps["actions"]}
        self.assertIn("activity.idle", names)
        self.assertNotIn("activity.drawing", names)
        missing = await apply_state(self.catalog, ModelSelection(activity="drawing"), self.output)
        self.assertEqual((missing.status, missing.reason_code, missing.detail),
                         ("unsupported", "missing_dependency", "missing_activity_resource"))
        self.assertEqual(self.output.expression_writes, [])

    async def test_wrong_model_and_old_outfit_leave_existing_state(self):
        self.output.model_id = "model-other-demo"
        caps = self.catalog.avatar_capabilities(await self.output.read_model())
        self.assertEqual(caps, {"actions": [], "inputs": [], "hotkeys": []})
        wrong = await apply_state(self.catalog, ModelSelection(outfit="jacket"), self.output)
        self.assertEqual(wrong.detail, "model_or_asset_mismatch")
        self.output.model_id = self.catalog.model_id
        self.output.expressions["demo-jacket.exp3.json"] = True
        absent = await apply_state(self.catalog, ModelSelection(outfit="old-layered-outfit"), self.output)
        self.assertEqual(absent.detail, "unmapped_outfit")
        self.assertTrue(self.output.expressions["demo-jacket.exp3.json"])
        self.assertEqual(self.output.expression_writes, [])

    async def test_reconnect_restores_latest_persistent_state_without_events(self):
        event = await trigger_event(self.catalog, "spark", self.output)
        self.assertEqual(event.status, "completed")
        self.assertEqual(event.evidence["source"], "vts_hotkey_ack")
        self.output.epoch = "epoch-demo-2"
        latest = ModelSelection(activity="drawing", outfit="jacket", expressions=frozenset({"glitter"}))
        restored = await apply_state(self.catalog, latest, self.output)
        self.assertEqual(restored.status, "completed")
        already_current = await apply_state(self.catalog, latest, self.output)
        self.assertEqual(already_current.status, "completed")
        self.assertEqual(len(self.output.expression_writes), 1)
        self.assertEqual(self.output.animation_writes, ["hotkey-demo-spark"])

    async def test_lost_event_ack_stays_unknown_and_does_not_retry(self):
        async def lost_ack(observed, hotkey_id, *, is_current):
            self.output.animation_writes.append(hotkey_id)
            raise TimeoutError

        self.output.trigger_animation = lost_ack
        result = await trigger_event(self.catalog, "spark", self.output)
        self.assertEqual((result.status, result.reason_code), ("unknown", "result_unknown"))
        await apply_state(self.catalog, ModelSelection(activity="drawing"), self.output)
        self.assertEqual(self.output.animation_writes, ["hotkey-demo-spark"])

    async def test_ack_and_stale_epoch_cannot_prove_state(self):
        set_expressions = self.output.set_expressions

        async def reconnect(observed, changes, *, is_current):
            await set_expressions(observed, changes, is_current=is_current)
            self.output.epoch = "epoch-demo-2"

        self.output.set_expressions = reconnect
        result = await apply_state(self.catalog, ModelSelection(activity="drawing"), self.output)
        self.assertEqual(result.detail, "model_changed_before_verification")
        self.assertEqual(result.status, "unknown")

    async def test_dispatcher_revocation_before_event_write(self):
        checks = iter((True, False))
        result = await trigger_event(self.catalog, "spark", self.output, is_current=lambda: next(checks))
        self.assertEqual((result.status, result.reason_code), ("rejected", "stale_output"))
        self.assertEqual(self.output.animation_writes, [])

    async def test_active_expression_without_matching_parameter_is_unknown(self):
        read = self.output.read_model

        async def conflicting_read():
            model = await read()
            return replace(model, parameters={**model.parameters, "DemoPose": 0})

        self.output.read_model = conflicting_read
        result = await apply_state(self.catalog, ModelSelection(activity="drawing"), self.output)
        self.assertEqual((result.status, result.detail), ("unknown", "parameter_state_unconfirmed"))

    async def test_role_activity_restores_last_confirmed_pose_after_superseding_a_write(self):
        state = self.output
        output = VTSOutput(None, owned=())
        output.bind(self.catalog, await state.read_model())
        output.read_model = state.read_model
        avatar = ProceduralAvatar(output)
        idle = {"activity": "idle", "energy": None, "sleepiness": None, "emotion": None}

        async def set_expressions(observed, changes, *, is_current):
            await state.set_expressions(observed, changes, is_current=is_current)
            if ("demo-sketch.exp3.json", True) in changes:
                avatar.snapshot("role_state", idle, deadline=avatar.clock() + 5)

        output.set_expressions = set_expressions
        avatar.snapshot("role_state", idle, deadline=avatar.clock() + 5)
        await avatar.tick()
        await avatar._wardrobe_task
        self.assertTrue(state.expressions["demo-idle.exp3.json"])

        avatar.snapshot("role_state", {**idle, "activity": "drawing"}, deadline=avatar.clock() + 5)
        await avatar.tick()
        await avatar._wardrobe_task
        self.assertTrue(state.expressions["demo-sketch.exp3.json"])
        await avatar.tick()
        self.assertIsNotNone(avatar._wardrobe_task)
        await avatar._wardrobe_task
        self.assertTrue(state.expressions["demo-idle.exp3.json"])
        self.assertFalse(state.expressions["demo-sketch.exp3.json"])
        self.assertEqual(len(state.expression_writes), 3)

    async def test_device_snapshot_uses_vts_and_reports_readback_across_model_reload(self):
        state = self.output
        state.expressions.update({"demo-casual.exp3.json": True, "demo-sketch.exp3.json": True,
                                  "demo-pin.exp3.json": True})
        activations = []
        hotkey_writes = []
        elapsed = 0

        def clock():
            return elapsed

        async def request(kind, data, *, is_current=lambda: True):
            if not is_current():
                raise StaleOutputError
            model = await state.read_model()
            identity = {"modelLoaded": True, "modelID": model.model_id}
            if kind == "CurrentModelRequest":
                return identity
            if kind == "InputParameterListRequest":
                return {**identity, "defaultParameters": [{"name": name} for name in model.inputs], "customParameters": []}
            if kind == "ExpressionStateRequest":
                return {**identity, "expressions": [{"file": file, "active": active}
                                                    for file, active in model.expressions.items()]}
            if kind == "HotkeysInCurrentModelRequest":
                return {**identity, "availableHotkeys": [{"file": file, "type": "TriggerAnimation", "hotkeyID": hotkey}
                                                         for hotkey, file in model.animation_hotkeys.items()]}
            if kind == "Live2DParameterListRequest":
                return {**identity, "parameters": [{"name": key, "value": value} for key, value in model.parameters.items()]}
            if kind == "HotkeyTriggerRequest":
                hotkey_writes.append(data["hotkeyID"])
                return data
            assert kind == "ExpressionActivationRequest" and data["fadeTime"] == 0
            file, active = data["expressionFile"], data["active"]
            activations.append((file, active))
            state.expressions[file] = active

        local = LocalVTS(8001, None)
        local.request = request
        output = VTSOutput(request, owned=())  # wardrobe does not require any continuous inputs
        observed = await local._read_model(self.catalog, "epoch-demo-1")
        self.assertEqual(observed.animation_hotkeys, state.hotkeys)
        output.bind(self.catalog, observed)
        avatar = ProceduralAvatar(output, clock=clock, wardrobe_report=lambda body: desktop.report(
            "wardrobe_applied", body, session=desktop.session))
        avatar.set_enabled(False)
        host = SessionHost(clock=clock)
        binding = Binding("device-demo", "binding-demo", frozenset({"avatar"}))
        desired = {"revision": 1, "model_id": state.model_id, "asset_version": state.asset_version,
                   "selection": {"outfit": "jacket"}, "mapping": None, "updated_at": "2026-01-01T12:00:00Z"}

        async def transport(operation, payload):
            return host.handle(binding, operation, payload,
                               snapshots=lambda caps: [("wardrobe", desired)] if caps["model_id"] == desired["model_id"] else [])

        desktop = DesktopSession(transport, permissions=binding.permissions, avatar=avatar, clock=clock)
        self.addAsyncCleanup(desktop.close)

        def publish(revision):
            desktop.publish_capabilities({**avatar.capabilities(revision), "observations": [], "games": []})

        publish(1)
        capabilities = avatar.capabilities(1)["avatar"]
        self.assertEqual([action["name"] for action in capabilities["actions"] if action["name"].startswith("outfit.")],
                         ["outfit.casual", "outfit.jacket"])
        self.assertEqual(capabilities["hotkeys"], ["hotkey-demo-spark"])
        await desktop._register()
        await desktop._exchange()
        avatar.set_enabled(True)
        await avatar.tick()
        await avatar._wardrobe_task
        await desktop._exchange()
        receipt = host.wardrobe_applied(desktop.session)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["evidence"]["source"], "vts_model_readback")
        self.assertEqual(activations, [("demo-casual.exp3.json", False), ("demo-jacket.exp3.json", True)])
        self.assertTrue(state.expressions["demo-sketch.exp3.json"])
        self.assertTrue(state.expressions["demo-pin.exp3.json"])

        intent = {"task_id": "event-demo", "scope": "avatar", "action": "event.spark", "params": {},
                  "ttl_ms": 5000, "capability_revision": 1, "model_id": state.model_id}
        host.send_intent(desktop.session, intent)
        await desktop._exchange()
        await avatar._action_task
        await desktop._exchange()
        receipt = host.receipt(desktop.session, intent["task_id"])
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["evidence"]["source"], "vts_hotkey_ack")
        elapsed += 6
        self.assertEqual(host.receipt(desktop.session, intent["task_id"]), receipt)
        desktop.dispatcher.expire()
        await desktop._exchange()
        self.assertEqual(host.send_intent(desktop.session, intent), receipt)
        desktop.dispatcher.receive(message("intent", desktop.session, intent))
        self.assertEqual(hotkey_writes, ["hotkey-demo-spark"])

        avatar.handle_vts_event("ModelLoadedEvent", {})
        publish(2)
        await desktop._exchange()
        self.assertIsNone(host.wardrobe_applied(desktop.session))
        state.expressions["demo-jacket.exp3.json"] = False
        output.bind(self.catalog, await local._read_model(self.catalog, "epoch-demo-2"))
        publish(3)
        await desktop._exchange()
        await avatar.tick()
        await avatar._wardrobe_task
        await desktop._exchange()
        self.assertEqual(host.wardrobe_applied(desktop.session)["status"], "completed")
        self.assertEqual(activations[2:], [("demo-jacket.exp3.json", True)])

        desired.update(revision=2, selection={"outfit": "unmapped-demo"})
        await desktop._exchange()
        await avatar.tick()
        await avatar._wardrobe_task
        await desktop._exchange()
        receipt = host.wardrobe_applied(desktop.session)
        self.assertEqual((receipt["status"], receipt["reason_code"]), ("unsupported", "unmapped_outfit"))
        self.assertEqual(len(activations), 3)
        self.assertEqual(hotkey_writes, ["hotkey-demo-spark"])

        state.hotkeys.clear()
        host.send_intent(desktop.session, {**intent, "task_id": "missing-demo", "capability_revision": 3})
        await desktop._exchange()
        await avatar._action_task
        await desktop._exchange()
        self.assertEqual(host.receipt(desktop.session, "missing-demo")["status"], "unsupported")
        self.assertEqual(hotkey_writes, ["hotkey-demo-spark"])

        role = {"activity": "idle", "energy": None, "sleepiness": None, "emotion": None}
        avatar.snapshot("role_state", role, deadline=avatar.clock() + 5)
        await avatar.tick()
        await avatar._wardrobe_task
        self.assertEqual(activations[-2:], [("demo-sketch.exp3.json", False), ("demo-idle.exp3.json", True)])
        self.assertTrue(state.expressions["demo-jacket.exp3.json"])

        desired.update(revision=3, selection={"accessories": [], "expressions": ["warm"]})
        await desktop._exchange()
        await avatar.tick()
        await avatar._wardrobe_task
        await desktop._exchange()
        self.assertEqual(host.wardrobe_applied(desktop.session)["status"], "completed")
        self.assertEqual(activations[-2:], [("demo-pin.exp3.json", False), ("demo-cheeks.exp3.json", True)])
        self.assertTrue(state.expressions["demo-jacket.exp3.json"])

    async def test_vts_socket_ack_waits_for_live2d_readback_and_checks_model_identity(self):
        local = LocalVTS(8001, None)
        requests = []
        model_id = self.catalog.model_id
        parameter_reads = 0
        active = False

        class Socket:
            def __init__(socket):
                socket.replies = asyncio.Queue()
                socket.parameter_model = model_id

            def __aiter__(socket):
                return socket

            async def __anext__(socket):
                return await socket.replies.get()

            async def send(socket, raw):
                nonlocal parameter_reads, active
                frame = json.loads(raw)
                kind = frame["messageType"]
                requests.append(kind)
                identity = {"modelLoaded": True, "modelID": model_id}
                if kind == "CurrentModelRequest":
                    data = identity
                elif kind == "InputParameterListRequest":
                    data = {**identity, "defaultParameters": [], "customParameters": []}
                elif kind == "ExpressionStateRequest":
                    data = {**identity, "expressions": [{"file": "demo-sketch.exp3.json", "active": active}]}
                elif kind == "HotkeysInCurrentModelRequest":
                    data = {**identity, "availableHotkeys": [
                        {"hotkeyID": "hotkey-demo-spark", "file": "demo-spark.motion3.json", "type": "TriggerAnimation"}
                    ]}
                elif kind == "Live2DParameterListRequest":
                    parameter_reads += 1
                    value = int(active and parameter_reads >= 4)
                    data = {"modelLoaded": True, "modelID": socket.parameter_model,
                            "parameters": [{"name": "DemoPose", "value": value}]}
                elif kind == "ExpressionActivationRequest":
                    active = True
                    data = {}
                else:
                    assert kind == "HotkeyTriggerRequest"
                    data = {"hotkeyID": "different-demo-hotkey"}
                await socket.replies.put(json.dumps({"requestID": frame["requestID"],
                    "messageType": kind.removesuffix("Request") + "Response", "data": data}))

        local.socket = Socket()
        listener = asyncio.create_task(local._listen(local.socket, None))

        async def stop_listener():
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

        self.addAsyncCleanup(stop_listener)
        output = VTSOutput(local.request, owned=())
        output.bind(self.catalog, await local._read_model(self.catalog, "epoch-demo"))
        result = await apply_state(self.catalog, ModelSelection(activity="drawing"), output)
        self.assertEqual(result.status, "completed")
        self.assertEqual(parameter_reads, 4)
        self.assertEqual(requests.count("ExpressionActivationRequest"), 1)
        self.assertEqual((await trigger_event(self.catalog, "spark", output)).status, "unknown")
        self.assertEqual(requests.count("HotkeyTriggerRequest"), 1)

        local.socket.parameter_model = "model-other-demo"
        self.assertIsNone(await local._read_model(self.catalog, "other-epoch-demo"))
        self.assertEqual((await trigger_event(self.catalog, "spark", output)).detail, "readback_unavailable")
        self.assertEqual(requests.count("HotkeyTriggerRequest"), 1)
