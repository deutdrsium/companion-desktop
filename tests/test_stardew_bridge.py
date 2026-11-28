import asyncio
import json
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest

from companion_desktop.contracts.types import PROTOCOL
from companion_desktop.contracts.wire import CLIENT_TYPES, GAME_STATE_OFFER, ProtocolError, message
from companion_desktop.games.stardew.adapter import StardewBridge
from companion_desktop.games.stardew.mcp_client import McpClient, McpError
from companion_desktop.session.client import DesktopSession
from plugins.astrbot_plugin_desktop_access.session import Binding, SessionHost, TurnResult


REF = {"device_id": "device-demo", "desktop_session_id": "session-demo", "generation": 1}


def player(identifier, name, x, *, host=False, local=False):
    return {"farmerId": identifier, "farmerName": name, "isHost": host, "isLocal": local,
            "map": "Farm", "x": x, "y": 4}


def chat(seq, identifier, text, channel="public"):
    return {"seq": seq, "farmerId": identifier, "channel": channel, "text": text}


class MockMcp:
    def __init__(self):
        self.calls = []
        self.tools = ["get_coop_state", "map_info", "get_state", "move_to", "use_tool", "emote",
                      "say_public", "say", "cancel_action", "buy", "drop_item", "use_bomb", "toggle_ai"]
        self.block_action = False
        self.accept_async = False
        self.fail_action = False
        self.ignore_move = False
        self.reads_blocked = False
        self.release_action = Event()
        self.state = {
            "version": 1, "worldReady": True, "isMultiplayer": True, "playerInControl": False,
            "gameSessionId": "upstream-demo", "actionRunning": False,
            "farmers": [player("42", "Example", 4, local=True), player("7", "Example", 6, host=True),
                        player("12", "Example-2", 7), player("18446744073709551615", "Other", 8)],
            "chatEvents": [], "gap": False,
            "wateringTiles": [{"x": 6, "y": 5, "watered": False}, {"x": 7, "y": 5, "watered": False}],
            "privateNote": "PRIVATE_FIXTURE_MARKER",
        }

    def reply(self, request):
        method, params = request["method"], request.get("params", {})
        if method == "initialize":
            self.calls.append((method, params))
            return {"protocolVersion": "2025-06-18"}
        if method == "tools/list":
            self.calls.append((method, params))
            return {"tools": [{"name": name} for name in self.tools]}
        name, args = params["name"], params["arguments"]
        self.calls.append((name, args))
        if name == "get_coop_state":
            state = deepcopy(self.state)
            if self.reads_blocked:
                self.release_action.wait(2)
            return {"structuredContent": state}
        if name == "map_info":
            return {"content": [{"type": "text", "text": "地点：Farm，室外，地图大小 50x50"}]}
        if name == "cancel_action":
            self.state["actionRunning"] = False
            self.release_action.set()
            return {}
        if self.fail_action:
            return None
        if self.accept_async:
            self.state["actionRunning"] = True
            return {"content": [{"type": "text", "text": "accepted"}]}
        if self.block_action:
            self.state["actionRunning"] = True
            self.release_action.wait(2)
            return {}
        local = self.state["farmers"][0]
        if name == "move_to" and not self.ignore_move:
            local.update(args)
        if name == "emote":
            local["emote"] = args["emote"]
        if name == "use_tool":
            next(tile for tile in self.state["wateringTiles"] if (tile["x"], tile["y"]) ==
                 (args["x"], args["y"]))["watered"] = True
        if name == "say_public":
            events = self.state["chatEvents"]
            events.append(chat(max((event["seq"] for event in events), default=0) + 1, "42", args["text"]))
        return {"content": [{"type": "text", "text": "untrusted result"}]}


@pytest.fixture
def mcp():
    mock = MockMcp()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            assert self.path == "/mcp"
            assert self.headers["Accept"] == "application/json, text/event-stream"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert request["jsonrpc"] == "2.0"
            method = request["method"]
            if method != "initialize":
                assert self.headers["Mcp-Session-Id"] == "mock-session"
                assert self.headers["MCP-Protocol-Version"] == "2025-06-18"
            if method == "notifications/initialized":
                assert "id" not in request
                mock.calls.append((method, {}))
                self.send_response(202)
                self.end_headers()
                return
            streaming = method == "tools/call" and request["params"]["name"] not in {"get_coop_state", "map_info"}
            if streaming:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b": heartbeat\r\n\r\nevent: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"ping\"}\n\n")
                self.wfile.flush()
            result = mock.reply(request)
            if result is None:
                return
            encoded = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
            if streaming:
                encoded = b"event: message\ndata: " + encoded + b"\n\n"
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if method == "initialize":
                    self.send_header("Mcp-Session-Id", "mock-session")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    mock.url = f"http://127.0.0.1:{server.server_port}/mcp"
    try:
        yield mock
    finally:
        mock.release_action.set()
        server.shutdown()
        server.server_close()
        thread.join(1)


@pytest.mark.parametrize("url", [
    "http://example.invalid/mcp", "http://192.0.2.1/mcp", "http://127.0.0.1.example.invalid/mcp",
    "http://user@localhost/mcp", "http://localhost/mcp?redirect=1", "file:///mcp",
    "http://127.1/mcp", "http://[::ffff:192.0.2.1]/mcp", "http://[::1]:0/mcp",
])
def test_rejects_non_loopback_and_ambiguous_urls(url):
    with pytest.raises(ValueError, match="invalid_mcp_url"):
        McpClient(url)


def test_streamable_http_handshake_and_json_sse_tools(mcp, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:1")

    async def run():
        client = McpClient(mcp.url.replace("127.0.0.1", "localhost"))
        assert "emote" in await client.initialize()
        assert (await client.call("get_coop_state", {"after_seq": 4}))["structuredContent"]["worldReady"]
        assert (await client.call("emote", {"emote": "hi"}))["content"][0]["type"] == "text"
        await client.cancel_action()

    asyncio.run(run())
    assert [name for name, _ in mcp.calls] == [
        "initialize", "notifications/initialized", "tools/list", "get_coop_state", "emote", "cancel_action"
    ]


def test_transport_timeout_has_mcp_error(mcp):
    async def run():
        client = McpClient(mcp.url)
        await client.initialize()
        mcp.reads_blocked = True
        with pytest.raises(McpError, match="mcp_timeout"):
            await client.call("get_coop_state", {"after_seq": 0}, timeout=0.05)

    asyncio.run(run())


async def wait_for(condition):
    for _ in range(400):
        value = condition()
        if value:
            return value
        await asyncio.sleep(0.01)
    pytest.fail("condition did not become true")


class Rig:
    def __init__(self, mcp, *, grants=("speak", "game"), clock=None, host_type=SessionHost):
        clocks = {} if clock is None else {"clock": clock}
        self.bridge = StardewBridge(mcp.url, [[6, 5]], poll_interval=0.02, **clocks)
        self.binding = Binding("device-demo", "binding-demo", frozenset(grants))
        self.host = host_type(**clocks)
        self.frames, self.receipts, self.registrations, self.exchanges = [], {}, [], []
        self.disconnect = False
        self.desktop = DesktopSession(self.transport, permissions={"game", "speak"}, game=self.bridge, game_state=True,
                                      poll_interval=0.02, **clocks)

    async def transport(self, operation, payload):
        if operation == "register":
            self.registrations.append(payload)
        if operation == "exchange":
            self.exchanges.append(payload)
        if self.disconnect:
            raise ConnectionError("connection_lost")
        for frame in payload.get("messages", ()):
            if frame["type"] == "receipt":
                self.receipts[frame["body"]["task_id"]] = frame["body"]
        return self.host.handle(self.binding, operation, payload, on_message=self.frames.append)

    async def __aenter__(self):
        self.desktop.start()
        await wait_for(lambda: self.desktop.connection["state"] == "connected")
        return self

    async def __aexit__(self, *_):
        await self.bridge.close()
        await self.desktop.close()

    def intent(self, action, params, *, task_id=None, ttl_ms=5000):
        state = self.frames[0]["body"]
        return {"scope": "game", "action": action, "params": params, "task_id": task_id or action,
                "capability_revision": 1, "ttl_ms": ttl_ms,
                **{field: state[field] for field in ("game_session_id", "window_id", "window_revision")}}

    def send(self, action, params, **kwargs):
        return self.host.send_intent(self.desktop.session, self.intent(action, params, **kwargs))

    async def receipt(self, task_id, status):
        return await wait_for(lambda: self.receipts.get(task_id) if self.receipts.get(task_id, {}).get("status") ==
                              status else None)


def test_state_projection_chat_filter_dedup_and_gap(mcp):
    async def run():
        mcp.state["chatEvents"] = [chat(1, "7", "OLD_WINDOW_MARKER")]
        async with Rig(mcp) as rig:
            assert rig.bridge.open(rig.desktop)
            initial = (await wait_for(lambda: rig.frames))[0]["body"]
            assert initial["chat_events"] == []
            assert len(initial["snapshot"]["farmers"]) == 4
            assert "Example" not in str(initial)
            assert "PRIVATE_FIXTURE_MARKER" not in str(initial)
            mcp.state["chatEvents"] += [
                chat(3, "7", "Example ready"), chat(4, "42", "Ready now"),
                chat(5, "12", "Example-2 ready"), chat(6, "42", "PRIVATE_CHAT_MARKER", "private"),
                chat(7, "99", "UNKNOWN_ID_MARKER"), chat(5, "12", "Example-2 ready"),
            ]
            next_frame = await wait_for(lambda: rig.frames[1] if len(rig.frames) > 1 else None)
            body = next_frame["body"]
            assert body["gap"]
            assert [(event["seq"], event["farmer_id"]) for event in body["chat_events"]] == [
                (3, "7"), (4, "42"), (5, "12")
            ]
            assert body["chat_events"][0]["text"] == "Farmer ready"
            assert body["chat_events"][2]["text"] == "Farmer 2 ready"
            assert "PRIVATE_CHAT_MARKER" not in str(body)
            assert "UNKNOWN_ID_MARKER" not in str(body)
            await wait_for(lambda: ("get_coop_state", {"after_seq": 7}) in mcp.calls)
            assert len(rig.frames) == 2
            assert body["seq"] > initial["seq"]

    asyncio.run(run())


def test_legacy_tool_list_never_reads_or_sends_chat(mcp):
    async def run():
        mcp.tools.remove("get_coop_state")
        async with Rig(mcp) as rig:
            assert rig.bridge.open(rig.desktop)
            await wait_for(lambda: rig.bridge.status["state"] == "observe_only")
            assert rig.bridge.status["map"] == "Farm"
            assert not rig.bridge.status["public_chat"]
            assert not rig.frames and not rig.desktop.device_capabilities["games"]
            assert [name for name, _ in mcp.calls] == [
                "initialize", "notifications/initialized", "tools/list", "map_info"
            ]

    asyncio.run(run())


def test_opt_in_without_game_grant_never_contacts_mcp(mcp):
    async def run():
        async with Rig(mcp, grants=("speak",)) as rig:
            assert "game" in rig.registrations[0]["permissions"]
            assert not rig.bridge.open(rig.desktop)
            assert not mcp.calls and not rig.frames

    asyncio.run(run())


class LegacyV1Host(SessionHost):
    def _register(self, binding, payload):
        assert PROTOCOL in payload["protocols"]
        return super()._register(binding, {**payload, "protocols": [PROTOCOL]})

    def _update_capabilities(self, connection, body):
        if connection.capabilities is not None and body["revision"] <= connection.capabilities["revision"]:
            if body != connection.capabilities:
                raise ProtocolError("stale_capabilities")
        super()._update_capabilities(connection, body)

    def _game_state(self, connection, frame, on_message):
        raise ProtocolError("invalid_body")


def test_legacy_v1_host_preserves_generation_and_conversation(mcp):
    async def run():
        turn_texts = []

        class Chat:
            async def run(self, turn_id, text):
                turn_texts.append(text)
                return TurnResult("completed", "Test reply")

        class HostWithChat(LegacyV1Host):
            def handle(self, binding, operation, payload, **kwargs):
                return super().handle(binding, operation, payload, turns=Chat(), **kwargs)

        async with Rig(mcp, host_type=HostWithChat) as rig:
            ref = rig.desktop.session
            events = []
            rig.desktop.subscribe(events.append)
            assert rig.registrations[0]["protocols"] == [PROTOCOL, GAME_STATE_OFFER]
            assert rig.desktop.granted_session("game") == ref
            assert not rig.bridge.open(rig.desktop)
            rig.desktop.publish_games([{"game_session_id": "game-demo", "capability_revision": 1, "actions": []}])
            assert not rig.desktop.report_game_state(None, session=ref)
            rig.desktop.send_message("local-chat", "Fixture message")
            await wait_for(lambda: any(event["type"] == "turn.completed" for event in events))
            assert rig.desktop.session == ref and len(rig.registrations) == 1
            assert turn_texts == ["Fixture message"] and not mcp.calls
            assert all(frame["type"] != "game.state" for payload in rig.exchanges for frame in payload["messages"])
            assert not rig.host.capabilities(ref)["games"]

    asyncio.run(run())


def test_unstructured_chat_disables_public_tool_even_if_listed(mcp):
    async def run():
        mcp.state["chatEvents"] = [{"seq": 1, "farmerName": "Example", "text": "PRIVATE_CHAT_MARKER"}]
        async with Rig(mcp) as rig:
            assert rig.bridge.open(rig.desktop)
            await wait_for(lambda: rig.frames)
            assert not rig.bridge.status["public_chat"]
            assert not rig.frames[0]["body"]["chat_events"]
            assert rig.send("say_public", {"category": "greet"})["status"] == "unsupported"
            assert all(name != "say_public" for name, _ in mcp.calls)

    asyncio.run(run())


def test_white_list_and_readback_over_local_mcp(mcp):
    async def run():
        async with Rig(mcp) as rig:
            assert rig.bridge.open(rig.desktop)
            await wait_for(lambda: rig.frames)
            for name in ("buy", "drop_item", "use_bomb", "toggle_ai", "say"):
                assert rig.send(name, {})["status"] == "unsupported"
            rig.send("say_public", {"category": "hello arbitrary", "text": "PRIVATE_CHAT_MARKER"}, task_id="bad-chat")
            assert (await rig.receipt("bad-chat", "rejected"))["reason_code"] == "invalid_config"
            rig.send("use_tool", {"map": "Farm", "x": 7, "y": 5, "tool": "watering_can"}, task_id="outside")
            await rig.receipt("outside", "rejected")
            rig.send("move_to", {"map": "Farm", "x": 5, "y": 4})
            assert (await rig.receipt("move_to", "completed"))["evidence"]["source"] == "stardew_readback"
            rig.send("use_tool", {"map": "Farm", "x": 6, "y": 5, "tool": "watering_can"})
            await rig.receipt("use_tool", "completed")
            rig.send("emote", {"emote": "hi"})
            await rig.receipt("emote", "completed")
            rig.send("say_public", {"category": "greet"})
            await rig.receipt("say_public", "completed")
            assert ("say_public", {"text": "Hi!"}) in mcp.calls
            assert ("use_tool", {"x": 6, "y": 5, "tool": "watering_can", "times": 1}) in mcp.calls
            assert sum(name == "use_tool" for name, _ in mcp.calls) == 1
            assert all(name not in {"buy", "drop_item", "use_bomb", "toggle_ai", "say"} for name, _ in mcp.calls)
            mcp.ignore_move = True
            rig.send("move_to", {"map": "Farm", "x": 7, "y": 4}, task_id="no-effect")
            assert (await rig.receipt("no-effect", "unknown"))["reason_code"] == "result_unknown"

    asyncio.run(run())


@pytest.mark.parametrize("event", ["complete", "cancel_intent", "cancel_action", "stop", "revoke", "disconnect", "expire"])
def test_acknowledged_running_action_stays_owned_until_finished_or_cancelled(mcp, event):
    async def run():
        mcp.accept_async = True
        async with Rig(mcp) as rig:
            assert rig.bridge.open(rig.desktop)
            await wait_for(lambda: rig.frames)
            ref = rig.desktop.session
            rig.send("move_to", {"map": "Farm", "x": 5, "y": 4}, ttl_ms=500 if event == "expire" else 5000)
            await wait_for(lambda: rig.bridge._work and rig.bridge._work.running_state)
            await rig.receipt("move_to", "running")
            rig.send("emote", {"emote": "hi"})
            assert (await rig.receipt("emote", "rejected"))["reason_code"] == "busy"
            if event == "complete":
                mcp.state["farmers"][0]["x"] = 5
                mcp.state["actionRunning"] = False
                await rig.receipt("move_to", "completed")
                assert ("cancel_action", {}) not in mcp.calls
                return
            if event == "cancel_intent":
                rig.host.cancel_intent(ref, task_id="move_to")
                rig.host.cancel_intent(ref, task_id="move_to")
            elif event == "cancel_action":
                rig.send("cancel_action", {})
            elif event == "stop":
                rig.bridge.stop()
            elif event == "revoke":
                rig.binding = Binding("device-demo", "binding-demo", frozenset({"speak"}))
            elif event == "disconnect":
                rig.disconnect = True
            await wait_for(lambda: ("cancel_action", {}) in mcp.calls)
            if event != "disconnect":
                await rig.receipt("move_to", "unknown")
            if event == "cancel_action":
                await rig.receipt("cancel_action", "completed")
            if event == "expire":
                assert rig.bridge.status["state"] == "expired"
            rig.bridge.stop()
            await rig.bridge.close()
            assert sum(name == "move_to" for name, _ in mcp.calls) == 1
            assert sum(name == "cancel_action" for name, _ in mcp.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("cause", ["reconnect", "revocation", "takeover", "cancel", "mcp_drop", "ttl"])
def test_interrupts_inflight_once_and_marks_unknown(mcp, cause):
    async def run():
        mcp.block_action = cause != "mcp_drop"
        mcp.fail_action = cause == "mcp_drop"
        async with Rig(mcp) as rig:
            assert rig.bridge.open(rig.desktop)
            await wait_for(lambda: rig.frames)
            ref = rig.desktop.session
            rig.send("move_to", {"map": "Farm", "x": 5, "y": 4}, ttl_ms=350 if cause == "ttl" else 5000)
            await wait_for(lambda: ("move_to", {"x": 5, "y": 4}) in mcp.calls)
            if cause == "reconnect":
                rig.disconnect = True
            elif cause == "revocation":
                rig.binding = Binding("device-demo", "binding-demo", frozenset({"speak"}))
            elif cause == "takeover":
                mcp.state["playerInControl"] = True
            elif cause == "cancel":
                await wait_for(lambda: rig.bridge._window.state.action_running is True)
                rig.send("cancel_action", {})
            await wait_for(lambda: ("cancel_action", {}) in mcp.calls)
            assert rig.bridge.status["last_result"] == "unknown"
            if cause == "reconnect":
                rig.disconnect = False
                await wait_for(lambda: rig.desktop.session is not None and rig.desktop.session != ref)
                assert not rig.desktop.device_capabilities["games"]
            else:
                await rig.receipt("move_to", "unknown")
            if cause == "cancel":
                await rig.receipt("cancel_action", "completed")
            if cause == "ttl":
                assert rig.bridge.status["state"] == "expired"
                assert not rig.desktop.device_capabilities["games"]
            if cause == "mcp_drop":
                assert rig.bridge.status["state"] == "mcp_unavailable"
            assert sum(name == "move_to" for name, _ in mcp.calls) == 1
            assert sum(name == "cancel_action" for name, _ in mcp.calls) == 1

    asyncio.run(run())


def test_monotonic_expiry_before_mcp_send(mcp):
    async def run():
        now = [10.0]
        async with Rig(mcp, clock=lambda: now[0]) as rig:
            assert rig.bridge.open(rig.desktop, duration=4)
            await wait_for(lambda: rig.frames)
            mcp.reads_blocked = True
            started = len(mcp.calls)
            rig.send("move_to", {"map": "Farm", "x": 5, "y": 4}, ttl_ms=2500)
            await wait_for(lambda: len(mcp.calls) > started)
            now[0] += 5
            mcp.release_action.set()
            await wait_for(lambda: rig.bridge.status["state"] == "expired")
            assert all(name not in {"move_to", "cancel_action"} for name, _ in mcp.calls)

    asyncio.run(run())


def test_host_drops_stale_frames_and_requires_game_grant(mcp):
    async def run():
        now = [10.0]
        async with Rig(mcp, clock=lambda: now[0]) as rig:
            assert rig.bridge.open(rig.desktop, duration=10)
            await wait_for(lambda: rig.frames)
            frame = rig.frames[0]
            ref, body = frame["session"], frame["body"]

            def exchange(overrides, binding=rig.binding):
                rig.host.handle(binding, "exchange", {
                    "session": ref, "turns": [], "messages": [message("game.state", ref, {**body, **overrides})],
                }, on_message=rig.frames.append)

            exchange({})
            exchange({"seq": 2, "window_id": "wrong-window"})
            assert len(rig.frames) == 1
            now[0] += 11
            exchange({"seq": 2, "window_ttl_ms": 10000})
            assert len(rig.frames) == 1
            exchange({"seq": 3}, Binding("device-demo", "binding-demo", frozenset({"speak"})))
            exchange({"seq": 4})
            assert len(rig.frames) == 1

    asyncio.run(run())


def test_wire_limits_and_closed_frame():
    sample = {"game_session_id": "game-demo", "window_id": "window-demo", "window_revision": 1,
              "window_ttl_ms": 0, "seq": 1, "snapshot": None, "chat_events": [], "gap": False}
    assert "game.state" in CLIENT_TYPES
    assert message("game.state", REF, sample)["protocol"] == PROTOCOL
    for override in ({"gap": True}, {"seq": True}, {"window_id": "x" * 65}, {"window_ttl_ms": 60001},
                     {"snapshot": {}}, {"chat_events": [None] * 33}):
        with pytest.raises(ProtocolError, match="invalid_body"):
            message("game.state", REF, {**sample, **override})


def test_optional_opaque_farm_ref_passes_and_unsafe_value_is_rejected(mcp):
    async def run():
        mcp.state["farmRef"] = "farm-demo-opaque"
        async with Rig(mcp) as rig:
            assert rig.bridge.open(rig.desktop)
            frame = (await wait_for(lambda: rig.frames))[0]
            body, snapshot = frame["body"], frame["body"]["snapshot"]
            assert snapshot["farm_ref"] == "farm-demo-opaque"
            unsafe = {**snapshot, "farm_ref": "../Saves/demo"}
            with pytest.raises(ProtocolError, match="invalid_body"):
                message("game.state", frame["session"], {**body, "snapshot": unsafe})
            rig.bridge.stop()
            mcp.state.pop("farmRef")
            assert rig.bridge.open(rig.desktop)
            fresh = await wait_for(lambda: next((item["body"] for item in rig.frames
                                                if item["body"]["snapshot"] is not None
                                                and item["body"]["game_session_id"] != body["game_session_id"]), None))
            assert "farm_ref" not in fresh["snapshot"]

    asyncio.run(run())


def test_default_off_does_not_request_or_open_game(mcp, monkeypatch, tmp_path):
    from PyQt5.QtCore import QCoreApplication
    from companion_desktop.client.config import DEFAULTS
    from companion_desktop.client.device_backend import DeviceBackend

    app = QCoreApplication.instance()
    if app is None:
        from PyQt5.QtWidgets import QApplication

        monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
        app = QApplication([])
    cfg = {**deepcopy(DEFAULTS), "stardew": {"enabled": False, "mcp_url": mcp.url}}
    backend = DeviceBackend("http://127.0.0.1", "fixture-token", cfg, tmp_path)
    registrations = []

    async def transport(operation, payload):
        if operation == "register":
            registrations.append(payload)
            return {"session": REF, "protocol": PROTOCOL, "permissions": ["speak", "game"]}
        return {"messages": [], "permissions": ["speak", "game"]}

    async def run():
        backend.session._transport = transport
        await backend._start()
        await wait_for(lambda: backend.session.connection["state"] == "connected")
        assert backend.stardew_status()["state"] == "disabled"
        assert not backend.start_stardew()
        assert backend.session.dispatcher.outputs["game"] is None
        assert not backend.session.device_capabilities["games"]
        assert backend.session.granted_session("game") is None
        assert registrations[0]["permissions"] == ["speak"]
        assert not mcp.calls
        await backend.session.close()

    try:
        backend.loop.run_until_complete(run())
    finally:
        backend.loop.close()
