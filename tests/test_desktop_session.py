import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from plugins.astrbot_plugin_desktop_access.session import Binding, SessionHost, TurnResult
from companion_desktop.contracts.types import PROTOCOL
from companion_desktop.contracts.wire import MAX_REQUEST_BYTES, ProtocolError, encode_request, message
from companion_desktop.session.client import DesktopSession
from companion_desktop.session.dispatcher import Dispatcher, OutputResult


DEMO = {"device_id": "device-demo", "desktop_session_id": "session-demo", "generation": 1}
CAPABILITIES = {
    "revision": 1, "model_id": "model-demo", "asset_version": "demo-1",
    "avatar": {"actions": [{"name": "nod", "binding": "axis-demo"}], "inputs": ["axis-demo"], "hotkeys": []},
    "observations": [], "games": [{"game_session_id": "game-demo", "capability_revision": 1,
                                    "actions": [{"name": "walk", "schema": None}]}],
    "tts": True, "mouth": True, "subtitle": True,
}
AVATAR = {"task_id": "task-demo", "scope": "avatar", "action": "nod", "params": {}, "ttl_ms": 1000,
          "capability_revision": 1, "model_id": "model-demo"}
GAME = {"task_id": "game-task-demo", "scope": "game", "action": "walk", "params": {}, "ttl_ms": 1000,
        "capability_revision": 1, "game_session_id": "game-demo", "window_id": "window-demo", "window_revision": 1}
WARDROBE = {"revision": 1, "model_id": "model-demo", "asset_version": "demo-1",
            "selection": {"top": "item-demo"}, "mapping": None, "updated_at": "2026-01-01T12:00:00Z"}
EVIDENCE = {"source": "fake_output", "ref": "result-demo"}


class FakeOutput:
    def __init__(self, *, confirm_stop=True):
        self.started = []
        self.released = []
        self.stopped = []
        self.snapshots = []
        self.confirm_stop = confirm_stop

    def start(self, intent, *, deadline, is_current, report):
        self.started.append((intent, deadline, is_current, report))
        report(OutputResult("accepted"))

    def cancel(self, intent, reason):
        self.stopped.append((intent["task_id"], reason))
        if self.confirm_stop:
            return OutputResult("cancelled", reason, EVIDENCE)
        return None

    def release(self, reason):
        self.released.append(reason)

    def snapshot(self, kind, body, *, deadline):
        self.snapshots.append((kind, body, deadline))


class FakeChat:
    def __init__(self, *, stop=True):
        self.calls = []
        self.cancelled = []
        self.result = asyncio.Future()
        self.stop = stop

    async def run(self, turn_id, text):
        self.calls.append((turn_id, text))
        return await self.result

    async def cancel(self, turn_id):
        self.cancelled.append(turn_id)
        if self.stop and not self.result.done():
            self.result.set_result(TurnResult("cancelled"))


class DispatcherTests(unittest.TestCase):
    def test_model_change_duplicate_conflict_and_late_output(self):
        receipts = []
        output = FakeOutput()
        dispatcher = Dispatcher(lambda kind, body: receipts.append(body), avatar=output, clock=lambda: 1)
        dispatcher.bind(DEMO, {"avatar"})
        dispatcher.configure(CAPABILITIES, {"avatar"})
        dispatcher.receive(message("intent", DEMO, AVATAR))
        dispatcher.receive(message("intent", DEMO, {**AVATAR, "ttl_ms": 200}))
        self.assertEqual(len(output.started), 1)
        self.assertEqual(receipts[-1], receipts[-2])
        dispatcher.receive(message("intent", DEMO, {**AVATAR, "params": {"strength": 0.5}}))
        self.assertEqual(receipts[-1]["reason_code"], "invalid_config")
        _, _, is_current, report = output.started[0]
        dispatcher.configure({**CAPABILITIES, "revision": 2, "model_id": "model-other"}, {"avatar"})
        self.assertFalse(is_current())
        self.assertEqual(receipts[-1]["status"], "cancelled")
        report(OutputResult("completed", evidence=EVIDENCE))
        self.assertEqual(receipts[-1]["status"], "cancelled")
        dispatcher.receive(message("intent", DEMO, {**AVATAR, "task_id": "old-demo"}))
        self.assertEqual(receipts[-1]["reason_code"], "stale_capabilities")
        self.assertFalse(dispatcher.receive(message("intent", {**DEMO, "generation": 2}, AVATAR)))

    def test_takeover_and_expiry_are_rechecked_locally(self):
        clock = [0.0]
        output, receipts = FakeOutput(confirm_stop=False), []
        dispatcher = Dispatcher(lambda _, body: receipts.append(body), game=output, clock=lambda: clock[0])
        dispatcher.bind(DEMO, {"game"})
        dispatcher.configure(CAPABILITIES, {"game"})
        dispatcher.set_game_window("game-demo", "window-demo", 1, deadline=1)
        dispatcher.receive(message("intent", DEMO, GAME))
        _, _, is_current, report = output.started[0]
        dispatcher.set_game_window("game-demo", None, 2, deadline=0)
        self.assertFalse(is_current())
        self.assertEqual(receipts[-1]["status"], "unknown")
        report(OutputResult("completed", evidence=EVIDENCE))
        self.assertEqual(receipts[-1]["status"], "unknown")
        dispatcher.receive(message("intent", DEMO, {**GAME, "task_id": "late-demo"}))
        self.assertEqual(receipts[-1]["status"], "expired")
        dispatcher.configure(CAPABILITIES, set())
        dispatcher.receive(message("intent", DEMO, {**GAME, "task_id": "denied-demo"}))
        self.assertEqual(receipts[-1]["reason_code"], "unauthorized")
        self.assertEqual(len(output.started), 1)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, condition):
        async with asyncio.timeout(2):
            while not condition():
                await asyncio.sleep(0.005)

    async def test_lost_turn_ack_reconnect_snapshot_and_no_intent_replay(self):
        binding = Binding("device-demo", "binding-demo", frozenset({"avatar", "speak"}))
        chat, output, host = FakeChat(), FakeOutput(confirm_stop=False), SessionHost()
        events, snapshots = [], [WARDROBE]
        fault = {"drop_next_send": False}

        async def transport(operation, payload):
            response = host.handle(binding, operation, payload, turns=chat,
                                   snapshots=lambda _: [("wardrobe", snapshots[0])])
            if fault["drop_next_send"] and any(frame["type"] == "turn.send" for frame in payload.get("messages", ())):
                fault["drop_next_send"] = False
                snapshots[0] = {**WARDROBE, "revision": 2, "selection": {"top": "item-next"}}
                raise ConnectionError("connection_lost")
            return response

        desktop = DesktopSession(transport, permissions={"avatar", "speak"}, avatar=output, poll_interval=0.01)
        self.addAsyncCleanup(desktop.close)
        desktop.subscribe(events.append)
        desktop.publish_capabilities(CAPABILITIES)
        desktop.start()
        await self.wait_for(lambda: desktop.connection["state"] == "connected")
        first = desktop.session
        self.assertEqual(output.snapshots[-1][1], WARDROBE)
        self.assertTrue(desktop.capabilities["mouth"])
        self.assertEqual(host.send_intent(first, AVATAR)["status"], "queued")
        self.assertEqual(host.send_intent(first, {**AVATAR, "params": {"strength": 1}})["reason_code"], "invalid_config")
        await self.wait_for(lambda: host.receipt(first, AVATAR["task_id"])["status"] == "accepted")
        fault["drop_next_send"] = True
        desktop.send_message("msg-demo", "Hello")
        await self.wait_for(lambda: desktop.connection["state"] == "reconnecting")
        self.assertEqual(desktop.connection["next_retry_ms"], 500)
        self.assertFalse(output.started[0][2]())
        await self.wait_for(lambda: any(event["type"] == "turn.accepted" for event in events))
        self.assertNotEqual(desktop.session, first)
        self.assertEqual(chat.calls[0][1], "Hello")
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual(len(output.started), 1)
        self.assertEqual(output.snapshots[-1][1]["revision"], 2)
        self.assertEqual(host.receipt(first, AVATAR["task_id"])["status"], "unknown")
        stale = {"session": first, "messages": [], "turns": []}
        with self.assertRaisesRegex(ProtocolError, "stale_session"):
            host.handle(binding, "exchange", stale)
        chat.result.set_result(TurnResult("completed", "Example reply"))
        await self.wait_for(lambda: any(event["type"] == "turn.completed" for event in events))
        desktop.send_message("msg-demo", "Hello")
        await self.wait_for(lambda: not desktop._sends)
        self.assertEqual(sum(event["type"] == "turn.completed" for event in events), 1)
        current = desktop.session
        await desktop.close()
        with self.assertRaisesRegex(ProtocolError, "stale_session"):
            host.capabilities(current)

    async def test_cancel_reports_unknown_until_confirmed(self):
        binding = Binding("device-demo", "binding-demo", frozenset({"speak"}))
        chat, host, events = FakeChat(), SessionHost(), []

        async def transport(operation, payload):
            return host.handle(binding, operation, payload, turns=chat)

        desktop = DesktopSession(transport, permissions={"speak"}, poll_interval=0.01)
        self.addAsyncCleanup(desktop.close)
        desktop.subscribe(events.append)
        desktop.start()
        await self.wait_for(lambda: desktop.connection["state"] == "connected")
        desktop.send_message("msg-demo", "Hello")
        await self.wait_for(lambda: bool(chat.calls))
        turn_id = chat.calls[0][0]
        desktop.cancel_turn(turn_id)
        await self.wait_for(lambda: any(event.get("status") == "cancelled" for event in events))
        self.assertEqual([event["status"] for event in events if event["type"] == "turn.cancelled"], ["unknown", "cancelled"])
        self.assertEqual(chat.cancelled, [turn_id])

    async def test_cancel_can_retry_after_a_lookup_misses_delayed_admission(self):
        binding = Binding("device-demo", "binding-demo", frozenset({"speak"}))
        host = SessionHost()
        chat = FakeChat(stop=False)
        ref = host.handle(binding, "register", {"protocols": [PROTOCOL], "permissions": ["speak"]})["session"]
        self.addCleanup(host.handle, binding, "disconnect", {"session": ref, "messages": [], "turns": []})

        def exchange(*messages, turns=()):
            return host.handle(binding, "exchange", {"session": ref, "messages": list(messages), "turns": list(turns)},
                               turns=chat)["messages"]

        accepted, = exchange(message("turn.send", ref, {"client_msg_id": "msg-demo", "text": "Hello"}))
        await self.wait_for(lambda: bool(chat.calls))
        turn_id = accepted["body"]["turn_id"]
        cancel = message("turn.cancel", ref, {"turn_id": turn_id})
        self.assertEqual(exchange(cancel)[0]["body"]["status"], "unknown")
        await self.wait_for(lambda: chat.cancelled == [turn_id])

        chat.stop = True  # WebChat has now admitted the event for this checkpoint.
        self.assertEqual(exchange(cancel)[0]["body"]["status"], "unknown")
        await self.wait_for(chat.result.done)
        self.assertEqual(chat.cancelled, [turn_id, turn_id])
        self.assertEqual(exchange(turns=["msg-demo"])[0]["body"]["status"], "cancelled")

    async def test_exchange_splits_large_utf8_batch_and_drains_pending_messages(self):
        batches, events = [], []

        async def transport(operation, payload):
            if operation == "register":
                return {"protocol": PROTOCOL, "session": DEMO, "permissions": ["speak"]}
            self.assertLessEqual(len(encode_request(operation, payload)), MAX_REQUEST_BYTES)
            self.assertLessEqual(len(payload["messages"]), 32)
            if operation == "disconnect":
                return {}
            sent = [frame["body"] for frame in payload["messages"] if frame["type"] == "turn.send"]
            if sent:
                batches.append(sent)
            return {"permissions": ["speak"], "messages": [
                message("turn.accepted", DEMO, {"client_msg_id": body["client_msg_id"], "turn_id": body["client_msg_id"]})
                for body in sent
            ] + [message("turn.completed", DEMO, {"client_msg_id": key, "turn_id": key, "text": "Saved"})
                 for key in payload["turns"]]}

        desktop = DesktopSession(transport, permissions={"speak"}, poll_interval=0.01)
        self.addAsyncCleanup(desktop.close)
        desktop.subscribe(events.append)
        desktop.start()
        await self.wait_for(lambda: desktop.connection["state"] == "connected")
        text = "\U00010000" * 4000
        expected = [{"client_msg_id": f"msg-{i}", "text": text} for i in range(32)]
        for body in expected:
            desktop.send_message(**body)
        await self.wait_for(lambda: sum(event["type"] == "turn.completed" for event in events) == len(expected))

        self.assertGreater(len(batches), 1)
        self.assertEqual([body for batch in batches for body in batch], expected)
        self.assertFalse(any(event.get("state") == "reconnecting" for event in events))

    async def test_durable_turn_claim_and_binding_isolation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "claims.sqlite3"
            binding = Binding("device-demo", "binding-demo", frozenset({"speak"}))
            chat = FakeChat()
            refs = []
            turn_ids = []
            for iteration in range(2):
                host = SessionHost(turn_claims_path=path)
                ref = host.handle(binding, "register", {"protocols": [PROTOCOL], "permissions": ["speak"]})["session"]
                refs.append(ref)
                send = message("turn.send", ref, {"client_msg_id": "msg-demo", "text": "Hello"})
                payload = {"session": ref, "messages": [send], "turns": []}
                with self.assertRaisesRegex(ProtocolError, "stale_session"):
                    host.handle(Binding(binding.device_id, "other-binding", binding.permissions), "exchange", payload, turns=chat)
                response = host.handle(binding, "exchange", payload, turns=chat)["messages"]
                turn_ids.append(response[0]["body"]["turn_id"])
                if iteration == 0:
                    await self.wait_for(lambda: bool(chat.calls))
                    chat.result.set_result(TurnResult("completed", "Example reply"))
                    await asyncio.sleep(0)
                else:
                    self.assertEqual(response[1]["body"]["reason"], "result_unknown")
                host.handle(binding, "disconnect", {"session": ref, "messages": [], "turns": []})
            self.assertEqual(turn_ids[0], turn_ids[1])
            self.assertNotEqual(refs[0], refs[1])
            self.assertEqual(len(chat.calls), 1)

    async def test_observation_state_is_bound_to_originating_session(self):
        binding = Binding("device-demo", "binding-demo", frozenset({"observe"}))
        host, events, observations = SessionHost(), [], []

        async def transport(operation, payload):
            return host.handle(binding, operation, payload, on_message=observations.append)

        desktop = DesktopSession(transport, permissions={"observe"}, poll_interval=0.01)
        self.addAsyncCleanup(desktop.close)
        desktop.subscribe(events.append)
        desktop.publish_capabilities({**CAPABILITIES, "observations": ["active_window"]})
        desktop.start()
        await self.wait_for(lambda: desktop.connection["state"] == "connected")
        ref = desktop.session
        request = {"request_id": "observe-demo", "trigger": "user", "range": "active_window",
                   "permission_generation": 1, "ttl_ms": 1000}
        host.send_observe(ref, request)
        await self.wait_for(lambda: any(event["type"] == "observe" for event in events))
        requested = next(event for event in events if event["type"] == "observe.state")
        state = {**requested, "phase": "capturing"}
        del state["type"]
        desktop.observe_state(state, session=ref)
        result = {key: value for key, value in request.items() if key != "ttl_ms"}
        result.update(status="skipped", source="fake_capture", captured_at=None, result_ref=None)
        self.assertFalse(desktop.report("observation", result, session=DEMO))
        self.assertTrue(desktop.report("observation", result, session=ref))
        await self.wait_for(lambda: bool(observations))
        self.assertEqual([event["phase"] for event in events if event["type"] == "observe.state"],
                         ["requested", "capturing", "done"])

    async def test_host_game_window_and_pending_cancel(self):
        binding = Binding("device-demo", "binding-demo", frozenset({"game"}))
        host = SessionHost(clock=lambda: 1)
        ref = host.handle(binding, "register", {"protocols": [PROTOCOL], "permissions": ["game"]})["session"]
        host.handle(binding, "exchange", {"session": ref, "messages": [message("capabilities", ref, CAPABILITIES)], "turns": []})
        self.assertEqual(host.send_intent(ref, GAME)["status"], "expired")
        host.set_game_window(ref, "game-demo", "window-demo", 1, deadline=2)
        task = {**GAME, "task_id": "valid-demo"}
        self.assertEqual(host.send_intent(ref, task)["status"], "queued")
        host.cancel_intent(ref, task_id=task["task_id"])
        result = host.handle(binding, "exchange", {"session": ref, "messages": [], "turns": []})
        self.assertEqual(result["messages"], [])
        self.assertEqual(host.receipt(ref, task["task_id"])["status"], "cancelled")
