"""Local VTube Studio connection for the procedural output."""

import asyncio
import json
import logging
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .model_actions import StaleOutputError
from .model_catalog import ModelReadback

log = logging.getLogger(__name__)
_PLUGIN = {"pluginName": "Companion Desktop", "pluginDeveloper": "Companion Desktop"}


class LocalVTS:
    def __init__(self, port, token_path):
        self.port = port
        self.token_path = token_path
        self.socket = None
        self.pending = {}
        self.changed = asyncio.Event()
        self.token_requested = False
        self.model_position = None

    async def request(self, kind, data, *, is_current=lambda: True, timeout=3):
        if self.socket is None:
            raise OSError("VTS disconnected")
        request_id = uuid4().hex
        reply = self.pending[request_id] = asyncio.get_running_loop().create_future()
        try:
            if not is_current():
                raise StaleOutputError
            await self.socket.send(json.dumps({"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
                                               "requestID": request_id, "messageType": kind, "data": data}))
            frame = await asyncio.wait_for(reply, timeout)
            if frame["messageType"] == "APIError":
                raise OSError("VTS rejected the request")
            if (frame["messageType"] != kind.removesuffix("Request") + "Response"
                    or not isinstance(frame.get("data"), dict)):
                raise OSError("Unexpected VTS response")
            return frame["data"]
        except (asyncio.TimeoutError, WebSocketException) as error:
            raise OSError("VTS connection failed") from error
        finally:
            self.pending.pop(request_id, None)

    async def _listen(self, socket, event):
        try:
            async for message in socket:
                frame = json.loads(message)
                reply = self.pending.get(frame.get("requestID"))
                if reply is not None and not reply.done():
                    reply.set_result(frame)
                elif frame["messageType"] in {"ModelLoadedEvent", "ModelClickedEvent"}:
                    event(frame["messageType"], frame["data"])
                    if frame["messageType"] == "ModelLoadedEvent":
                        self.model_position = None
                        self.changed.set()
                elif frame["messageType"] == "ModelMovedEvent":
                    data = frame["data"]
                    self.model_position = (data["modelID"], data["modelPosition"])
        finally:
            for reply in self.pending.values():
                if not reply.done():
                    reply.set_exception(OSError("VTS disconnected"))
            self.changed.set()

    async def _authenticate(self):
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = ""
        if token:
            result = await self.request("AuthenticationRequest", {**_PLUGIN, "authenticationToken": token})
            if result["authenticated"]:
                return True
        if self.token_requested:
            return False
        self.token_requested = True
        token = (await self.request("AuthenticationTokenRequest", _PLUGIN, timeout=60))["authenticationToken"]
        result = await self.request("AuthenticationRequest", {**_PLUGIN, "authenticationToken": token})
        if result["authenticated"]:
            self.token_path.write_text(token, encoding="utf-8")
        return result["authenticated"]

    async def _read_model(self, catalog, epoch):
        model = await self.request("CurrentModelRequest", {})
        if not model["modelLoaded"] or model["modelID"] != catalog.model_id:
            return None
        if self.model_position is None and model.get("modelPosition") is not None:
            self.model_position = (model["modelID"], model["modelPosition"])
        inputs = await self.request("InputParameterListRequest", {})
        expressions = await self.request("ExpressionStateRequest", {"details": False})
        hotkeys = await self.request("HotkeysInCurrentModelRequest", {})
        parameters = await self.request("Live2DParameterListRequest", {})
        if not all(ModelReadback.is_loaded(reply, catalog.model_id)
                   for reply in (inputs, expressions, hotkeys, parameters)):
            return None
        return ModelReadback.from_vts(epoch=epoch, ref=uuid4().hex, model=model, asset_version=catalog.asset_version,
                                      inputs=inputs["defaultParameters"] + inputs["customParameters"],
                                      parameters=parameters["parameters"], expressions=expressions["expressions"],
                                      hotkeys=hotkeys["availableHotkeys"])

    async def run(self, catalog, avatar, publish):
        output = avatar.output
        while True:
            try:
                async with connect(f"ws://127.0.0.1:{self.port}", proxy=None, open_timeout=2, close_timeout=1) as socket:
                    self.socket = socket
                    listener = asyncio.create_task(self._listen(socket, avatar.handle_vts_event))
                    try:
                        if not await self._authenticate():
                            return
                        await output.subscribe_events()
                        while not listener.done():
                            self.changed.clear()
                            observed = await self._read_model(catalog, uuid4().hex)
                            if self.changed.is_set():
                                continue
                            if observed is not None:
                                output.bind(catalog, observed)
                            elif output.observed is not None:
                                output.invalidate()
                            publish()
                            # Also recover if the frame writer invalidated a timed-out connection.
                            generation = output.generation
                            while not self.changed.is_set() and output.generation == generation:
                                await asyncio.sleep(0.25)
                        await listener
                    finally:
                        listener.cancel()
                        await asyncio.gather(listener, return_exceptions=True)
            except (OSError, WebSocketException, StaleOutputError, KeyError, ValueError):
                log.debug("Local VTS session unavailable")
            finally:
                self.socket = None
                self.model_position = None
                output.invalidate()
                publish()
            await asyncio.sleep(2)
