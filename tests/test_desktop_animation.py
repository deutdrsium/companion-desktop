"""B01–B03: synthetic Stage4-shaped mappings, P1 messages and VTS batches."""

import asyncio
from dataclasses import replace
from pathlib import Path
from statistics import quantiles
from time import perf_counter
import unittest

from companion_desktop.avatar.model_actions import StaleOutputError
from companion_desktop.avatar.model_catalog import InputBinding, ModelReadback, load_mapping
from companion_desktop.avatar.motion import FRAME_PERIOD, PAUSE_GAP
from companion_desktop.client.device_backend import _relative_cursor
from companion_desktop.avatar.procedural import AnimatedSession, ProceduralAvatar
from companion_desktop.avatar.vts import CHANNELS, VTSOutput
from companion_desktop.contracts.wire import message
from companion_desktop.session.client import DesktopSession, InMemorySession
from companion_desktop.session.dispatcher import Dispatcher
from plugins.astrbot_plugin_desktop_access.session import Binding, SessionHost, TurnResult
from test_desktop_session import FakeChat


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class FakeVTS:
    """Same guarded request seam as a local VTS socket; no VTS process."""

    def __init__(self):
        self.requests = []
        self.frames = []
        self.before_write = None

    async def request(self, kind, data, *, is_current):
        self.requests.append(kind)
        if self.before_write is not None:
            self.before_write()
        if not is_current():
            raise StaleOutputError
        if kind == "InjectParameterDataRequest":
            assert data["mode"] == "set"
            values = data["parameterValues"]
            frame = {item["id"]: item["value"] for item in values}
            assert len(frame) == len(values)  # never two writers for one input
            self.frames.append(frame)


class Rig:
    def __init__(self, *, missing=(), shared_body=False):
        example = Path(__file__).resolve().parents[1] / "config" / "model-capabilities.example.json"
        catalog = load_mapping(example)
        bindings = {}
        for channel, parameter in CHANNELS.items():
            if channel in missing:
                continue
            output_range = {
                "head_pitch": (-18, 12), "look_x": (1, -1),
                "head_roll": (-12, 12), "head_yaw": (-22, 22),
                "body_roll": (-5, 5), "body_yaw": (-10, 10),
            }.get(channel, (0, 1) if channel in {"eye_left", "eye_right", "eye_smile", "mouth_open", "breath"} else (-1, 1))
            input_id, input_range = channel, tuple(sorted(output_range))
            if channel == "head_pitch":
                input_range = (-20, 20)
            if shared_body and channel == "body_yaw":
                input_id = "head_yaw"
            bindings[channel] = InputBinding(input_id, parameter, input_range, output_range)
        bindings["wardrobe"] = InputBinding("wardrobe", "DemoOutfit", (0, 2), (0, 2))
        self.catalog = replace(catalog, inputs=bindings)
        self.model = ModelReadback(
            "epoch-demo", "read-demo", catalog.model_id, catalog.asset_version,
            frozenset(item.input_id for item in bindings.values()),
            {**dict.fromkeys((item.parameter for item in bindings.values()), 0), "DemoOutfit": 1}, {}, {},
        )
        self.vts = FakeVTS()
        self.output = VTSOutput(self.vts.request, owned=bindings)
        self.output.bind(self.catalog, self.model)
        self.clock = Clock()
        self.avatar = ProceduralAvatar(self.output, clock=self.clock, seed=41)

    async def step(self, seconds=FRAME_PERIOD):
        self.clock.now += seconds
        return await self.avatar.tick()

    async def frames(self, count):
        return [await self.step() for _ in range(count)]

    def role(self, revision, *, activity=None, energy=None, sleepiness=None, emotion=None, ttl_ms=60000):
        return {"revision": revision, "activity": activity, "energy": energy, "sleepiness": sleepiness,
                "emotion": emotion, "role_ref": "role-demo", "source": "synthetic",
                "observed_at": "2026-01-01T12:00:00Z", "ttl_ms": ttl_ms}


async def eventually(condition):
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0.005)


class AnimationTests(unittest.IsolatedAsyncioTestCase):
    async def test_b01_seeded_idle_jitter_pause_and_bounded_batch(self):
        regular, irregular = Rig(shared_body=True), Rig(shared_body=True)
        await regular.step(0)
        await irregular.step(0)
        compute_ms = []
        for count in range(3600):  # two minutes, including several weight shifts
            before = perf_counter()
            a = await regular.step()
            compute_ms.append((perf_counter() - before) * 1000)
            if count % 3 == 0:
                await irregular.step(0.011)
                await irregular.step(0.018)
                b = await irregular.step(0.071)
            if count % 3 == 2:
                self.assertAlmostEqual(regular.clock(), irregular.clock(), places=8)
                for channel in a:
                    self.assertAlmostEqual(a[channel], b[channel], delta=0.045, msg=channel)
        for frame in regular.vts.frames:
            for channel, value in frame.items():
                lower, upper = regular.output.bindings[channel].input_range
                self.assertLessEqual(lower, value)
                self.assertLessEqual(value, upper)
        xs = [frame["look_x"] for frame in regular.vts.frames]
        self.assertGreater(max(xs) - min(xs), 0.20)
        self.assertTrue(any(abs(xs[i] - xs[i + 15]) < 0.0001 for i in range(200, 450)))
        self.assertGreater(max(row["head_yaw"] for row in regular.vts.frames), 1)
        self.assertLess(min(row["head_yaw"] for row in regular.vts.frames), -1)
        self.assertNotIn("body_yaw", regular.vts.frames[-1])
        self.assertNotIn("wardrobe", regular.vts.frames[-1])
        self.assertAlmostEqual(regular.vts.frames[0]["head_pitch"], 4.0)  # asymmetric range
        self.assertAlmostEqual(regular.vts.frames[-1]["look_x"], -a["look_x"])

        before = len(regular.vts.frames)
        paused = await regular.step(8)
        self.assertEqual(len(regular.vts.frames), before + 1)
        self.assertEqual(paused, a)
        resumed = await regular.step()
        self.assertLess(abs(resumed["head_yaw"] - paused["head_yaw"]), 0.1)
        self.assertEqual(sum(dt >= PAUSE_GAP for dt, _ in regular.avatar.timings), 1)
        interval = quantiles([dt * 1000 for dt, _ in irregular.avatar.timings], n=100)
        measured = quantiles(compute_ms, n=100)
        print(f"B01 synthetic interval p50/p95/p99={interval[49]:.1f}/{interval[94]:.1f}/{interval[98]:.1f} ms; "
              f"tick + fake write p50/p95/p99={measured[49]:.3f}/{measured[94]:.3f}/{measured[98]:.3f} ms; "
              "1 injected 8000 ms gap, no backlog")

    async def test_b01_hit_scoped_reflexes_dont_take_brows_and_handoff_stops_writes(self):
        rig = Rig()
        await rig.output.subscribe_events()
        self.assertEqual(rig.vts.requests, ["EventSubscriptionRequest"] * 3)
        rig.avatar.message_sent("message-demo")
        settled = (await rig.frames(31))[-1]
        self.assertLess(settled["brow_left"], -0.4)
        self.assertLess(settled["look_x"], -0.6)
        rig.avatar.handle_vts_event("ModelClickedEvent", {"loadedModelID": "other-demo", "modelWasClicked": True})
        self.assertLess((await rig.frames(10))[-1]["look_x"], -0.6)
        rig.avatar.handle_vts_event("ModelClickedEvent", {"loadedModelID": rig.model.model_id, "modelWasClicked": True})
        self.assertGreater((await rig.frames(20))[-1]["look_x"], -0.05)
        rig.avatar.set_enabled(False)
        before = len(rig.vts.frames)
        await rig.frames(10)
        self.assertEqual(len(rig.vts.frames), before)
        rig.avatar.set_enabled(True)
        self.assertGreater((await rig.step())["brow_left"], -0.1)

    async def test_cursor_tracks_successive_positions_and_eases_back(self):
        rig = Rig()
        rig.avatar.message_sent("message-demo")
        await rig.frames(25)
        # The model is left and below this window's centre. Its origin, not the
        # monitor centre, is the zero for pointer deltas.
        def position(x, y):
            return _relative_cursor(x, y, 100, 200, 800, 600, -0.5, -0.5)

        self.assertEqual(position(300, 650), (0, 0))
        rig.avatar.pointer(position(300, 650))
        rig.avatar.pointer(position(600, 410))  # right and up relative to the model
        right_up = (await rig.frames(7))[-1]
        self.assertGreater(right_up["look_x"], 0.65)
        self.assertGreater(right_up["look_y"], 0.6)
        self.assertGreater(right_up["head_yaw"], 3)
        self.assertLess(right_up["brow_left"], -0.4)
        self.assertAlmostEqual(rig.vts.frames[-1]["look_x"], -right_up["look_x"])
        self.assertAlmostEqual(rig.vts.frames[-1]["look_y"], right_up["look_y"])

        # Samples well below the former glance threshold all update the target.
        rightward = []
        for sample in range(8):
            rig.avatar.pointer(position(370 + sample * 16, 580))
            rightward.append((await rig.frames(2))[-1]["look_x"])
        self.assertTrue(all(a < b for a, b in zip(rightward[2:], rightward[3:])))

        rig.avatar.pointer(position(120, 880))  # rapidly reverse both axes
        trajectory = await rig.frames(7)
        self.assertTrue(all(a["look_y"] > b["look_y"] for a, b in zip(trajectory, trajectory[1:])))
        left_down = trajectory[-1]
        self.assertLess(left_down["look_x"], -0.35)
        self.assertLess(left_down["look_y"], -0.6)
        self.assertLess(left_down["head_yaw"], 0)
        self.assertGreater(rig.vts.frames[-1]["look_x"], 0.35)
        self.assertLess(rig.vts.frames[-1]["look_y"], -0.6)

        rig.avatar.end_conversation()
        tail = []
        for _ in range(45):
            rig.avatar.pointer(position(120, 880))  # unchanged samples must not renew follow
            tail.append((await rig.step())["look_y"])
        self.assertLess(tail[0], -0.6)
        self.assertGreater(tail[-1], -0.1)
        self.assertLess(max(abs(a - b) for a, b in zip(tail, tail[1:])), 0.1)

    async def test_b02_typed_turn_smiles_blinks_speaks_and_releases(self):
        rig = Rig()
        session = InMemorySession()
        animated = AnimatedSession(session, rig.avatar)
        self.addCleanup(animated.detach)
        events = []
        session.subscribe(events.append)
        session.connect()
        rig.avatar.snapshot("role_state", rig.role(1, activity="reading"), deadline=rig.clock() + 30)
        rig.avatar.set_typing(True)
        listening = (await rig.frames(48))[-1]
        self.assertAlmostEqual(listening["brow_left"], 0.265, delta=0.01)
        self.assertAlmostEqual(listening["brow_right"], 0.235, delta=0.01)
        self.assertAlmostEqual(listening["head_roll"], 3.15, delta=0.02)
        self.assertAlmostEqual(listening["head_pitch"], 1, delta=0.02)
        animated.send_message("message-demo", "Example message")
        turn_id = events[-1]["turn_id"]
        thinking = (await rig.frames(20))[-1]
        self.assertLess(thinking["brow_left"], -0.4)
        self.assertGreater(thinking["look_y"], 0.3)
        session.reply(turn_id, "Example reply")
        rig.avatar.mouth_level(0.8)
        smiling = (await rig.frames(18))[-1]
        self.assertGreater(smiling["eye_smile"], 0.60)
        self.assertGreater(smiling["mouth_form"], 0.61)
        self.assertGreater(smiling["mouth_open"], 0.78)
        rig.avatar.motion.blink()
        closed = min(await rig.frames(7), key=lambda frame: frame["eye_left"])
        self.assertEqual(closed["eye_left"], 0.0)
        self.assertEqual(closed["eye_right"], 0.0)
        self.assertGreater(closed["eye_smile"], 0.60)
        self.assertGreater(closed["mouth_open"], 0.78)
        rig.avatar.stop_mouth()
        await rig.step()
        self.assertEqual(rig.vts.frames[-1]["mouth_open"], 0.0)
        self.assertGreater(rig.vts.frames[-1]["mouth_form"], 0.61)
        returned = (await rig.frames(100))[-1]
        self.assertLess(abs(returned["mouth_form"]), 0.01)
        self.assertLess(returned["look_y"], -0.3)
        self.assertTrue(all("wardrobe" not in frame for frame in rig.vts.frames))
        self.assertEqual(rig.model.parameters["DemoOutfit"], 1)
        print("B02 fake: listening → thinking → laugh with shut lids and open mouth → immediate mouth close → reading base")

    async def test_b03_real_p1_session_state_intent_and_host_disconnect(self):
        rig = Rig(missing=("eye_smile", "breath"))
        role = rig.role(1, activity="reading")
        offline = False
        block_exchange = asyncio.Event()
        block_exchange.set()
        host = SessionHost(clock=rig.clock)
        chat = FakeChat()
        binding = Binding("device-demo", "binding-demo", frozenset({"speak", "avatar"}))

        async def transport(operation, payload):
            if operation == "exchange":
                await block_exchange.wait()
                if offline:
                    raise ConnectionError("connection_lost")
            return host.handle(binding, operation, payload, turns=chat, snapshots=lambda _: [("role_state", role)])

        session = DesktopSession(transport, permissions=binding.permissions, avatar=rig.avatar,
                                 clock=rig.clock, poll_interval=0.01)
        animated = AnimatedSession(session, rig.avatar)
        self.addCleanup(animated.detach)
        self.addAsyncCleanup(session.close)
        session.publish_capabilities({**rig.avatar.capabilities(1), "observations": [], "games": []})
        events = []
        session.subscribe(events.append)
        session.start()
        await eventually(lambda: rig.avatar.motion.role_state is not None)
        reading = (await rig.frames(60))[-1]
        self.assertLess(reading["look_y"], -0.32)

        # Use the actual P1 send, not a manufactured turn.accepted event. Local
        # thinking appears even while this transport has not admitted the turn.
        block_exchange.clear()
        animated.send_message("message-demo", "Example message")
        thinking = (await rig.frames(24))[-1]
        self.assertEqual(chat.calls, [])
        self.assertLess(thinking["brow_left"], -0.42)
        block_exchange.set()
        await eventually(lambda: any(event["type"] == "turn.accepted" for event in events))
        chat.result.set_result(TurnResult("completed", "Example reply"))
        await eventually(lambda: any(event["type"] == "turn.completed" for event in events))
        self.assertGreater((await rig.frames(18))[-1]["mouth_form"], 0.61)
        self.assertNotIn("eye_smile", rig.vts.frames[-1])

        ref = session.session
        intent = {"task_id": "nod-demo", "scope": "avatar", "action": "nod", "params": {"intensity": 0.7},
                  "ttl_ms": 2000, "model_id": rig.model.model_id, "capability_revision": 1}
        host.send_intent(ref, intent)
        await eventually(lambda: host.receipt(ref, "nod-demo")["status"] == "accepted")
        nod = min(frame["head_pitch"] for frame in await rig.frames(24))
        await eventually(lambda: host.receipt(ref, "nod-demo")["status"] == "completed")
        self.assertLess(nod, reading["head_pitch"] - 2.5)
        self.assertEqual(host.receipt(ref, "nod-demo")["evidence"]["source"], "local_motion")

        async def update(**changes):
            nonlocal role
            role = rig.role(role["revision"] + 1, **changes)
            await eventually(lambda: rig.avatar.motion.role_state[0]["revision"] == role["revision"])

        await update(sleepiness=0.95)
        sleepy = await rig.frames(80)
        self.assertTrue(all(frame["eye_left"] < 0.85 for frame in sleepy[-15:]))
        await update(emotion="happy", ttl_ms=3000)
        happy = (await rig.frames(70))[-1]
        self.assertAlmostEqual(happy["mouth_form"], 0.26, delta=0.02)
        offline = True
        rig.avatar.mouth_level(1.0)
        await eventually(lambda: session.connection["state"] == "reconnecting")
        self.assertEqual((await rig.step())["mouth_open"], 0.0)
        self.assertGreater(rig.vts.frames[-1]["mouth_form"], 0.22)
        after_expiry = (await rig.frames(110))[-1]
        self.assertLess(abs(after_expiry["mouth_form"]), 0.01)
        self.assertIsNone(rig.avatar.motion.role_state)
        self.assertNotEqual(after_expiry["head_yaw"], happy["head_yaw"])
        print("B03 P1 + fake: reading low gaze, sleepy lids, happy mouth form; disconnect kept idle, TTL faded state")

    async def test_explicit_laugh_cancel_releases_only_its_channels(self):
        rig = Rig()
        emitted = []
        ref = {"device_id": "device-demo", "desktop_session_id": "session-demo", "generation": 1}
        dispatcher = Dispatcher(lambda _, body: emitted.append(body), avatar=rig.avatar, clock=rig.clock)
        dispatcher.bind(ref, {"avatar"})
        dispatcher.configure({**rig.avatar.capabilities(1), "observations": [], "games": []}, {"avatar"})
        intent = {"task_id": "laugh-demo", "scope": "avatar", "action": "hold_laugh", "params": {},
                  "ttl_ms": 4000, "model_id": rig.model.model_id, "capability_revision": 1}
        rig.avatar.message_sent("message-demo")
        rig.avatar.mouth_level(0.5)
        dispatcher.receive(message("intent", ref, intent))
        laughing = (await rig.frames(24))[-1]
        self.assertEqual(emitted[-1]["status"], "running")
        self.assertGreater(laughing["mouth_form"], 0.85)
        self.assertLess(laughing["brow_left"], -0.4)
        dispatcher.receive(message("cancel", ref, {"task_id": "laugh-demo", "reason_code": "cancelled"}))
        self.assertEqual(emitted[-1]["status"], "cancelled")
        self.assertEqual(emitted[-1]["evidence"]["source"], "local_motion")
        after_cancel = (await rig.frames(24))[-1]
        self.assertLess(after_cancel["mouth_form"], -0.5)
        self.assertAlmostEqual(after_cancel["mouth_open"], 0.5, places=2)

    async def test_cancel_failure_disconnect_and_model_reload_do_not_replay(self):
        rig = Rig()
        session = InMemorySession()
        animated = AnimatedSession(session, rig.avatar)
        self.addCleanup(animated.detach)
        events = []
        session.subscribe(events.append)
        session.connect()
        animated.send_message("message-demo", "Example message")
        turn_id = events[-1]["turn_id"]
        animated.cancel_turn(turn_id)
        self.assertFalse(rig.avatar.motion.thinking)
        self.assertEqual(rig.avatar.motion.reply_smile_until, 0)
        animated.send_message("message-demo", "Different payload")
        self.assertFalse(rig.avatar.motion.thinking)
        animated.send_message("next-demo", "Example message")
        next_id = events[-1]["turn_id"]
        session.reconnecting()
        session.connect()
        session.reply(next_id, "Late reply")
        self.assertEqual(rig.avatar.motion.reply_smile_until, 0)
        await rig.step()
        before = len(rig.vts.frames)
        # A stop while a batch waits to send invalidates that entire batch.
        rig.avatar.mouth_level(1.0)
        rig.vts.before_write = rig.avatar.stop_mouth
        await rig.step()
        self.assertEqual(len(rig.vts.frames), before)
        rig.vts.before_write = None
        await rig.step()
        self.assertEqual(rig.vts.frames[-1]["mouth_open"], 0.0)
        before += 1
        # Model changes after the frame was composed but before the socket send.
        rig.vts.before_write = lambda: rig.avatar.handle_vts_event("ModelLoadedEvent", {})
        await rig.step()
        self.assertEqual(len(rig.vts.frames), before)
        rig.vts.before_write = None
        rig.output.bind(rig.catalog, replace(rig.model, model_id="other-demo"))
        await rig.step()
        self.assertEqual(len(rig.vts.frames), before)
        rig.output.bind(rig.catalog, replace(rig.model, epoch="next-demo"))
        self.assertEqual((await rig.step())["mouth_open"], 0.0)
        self.assertEqual(len(rig.vts.frames), before + 1)
