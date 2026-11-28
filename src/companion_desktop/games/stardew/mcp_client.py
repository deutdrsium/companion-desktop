"""The loopback-only Streamable HTTP transport used by the Stardew bridge."""

import asyncio
import json
import socket
from concurrent.futures import Future
from http.client import HTTPConnection, HTTPException
from ipaddress import ip_address
from threading import Event, Thread
from urllib.parse import urlsplit


DEFAULT_MCP_URL = "http://127.0.0.1:18081/mcp"
_VERSION = "2025-06-18"
_MAX_RESPONSE = 256 * 1024


class McpError(Exception):
    pass


def loopback_endpoint(url):
    if not isinstance(url, str) or any(ord(char) <= 32 for char in url):
        raise ValueError("invalid_mcp_url")
    try:
        endpoint = urlsplit(url)
        host = "127.0.0.1" if endpoint.hostname == "localhost" else endpoint.hostname
        if (endpoint.scheme == "http" and endpoint.path == "/mcp" and endpoint.username is None
                and not any(char in url for char in "?#%\\") and ip_address(host).is_loopback
                and endpoint.port != 0):
            return host, endpoint.port or 80
    except ValueError:
        pass
    raise ValueError("invalid_mcp_url")


class _Connection(HTTPConnection):
    def __init__(self, host, port, timeout, before_write, body):
        super().__init__(host, port, timeout=timeout)
        self.before_write, self.body = before_write, body
        self.stopped = Event()
        self.live_socket = None

    def send(self, data):
        if self.sock is None:
            self.connect()
            self.live_socket = self.sock
        if self.stopped.is_set() or not self.before_write(data is self.body):
            raise McpError("request_cancelled")
        super().send(data)

    def stop(self):
        self.stopped.set()
        if self.live_socket is not None:
            try:
                self.live_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _read_sse(response, request_id):
    data = []
    size = 0
    while line := response.readline(_MAX_RESPONSE + 1):
        size += len(line)
        if size > _MAX_RESPONSE:
            raise McpError("response_too_large")
        line = line.rstrip(b"\r\n")
        if not line and data:
            frame = json.loads(b"\n".join(data))
            data.clear()
            if isinstance(frame, dict) and frame.get("id") == request_id:
                return frame
        elif line.startswith(b"data:"):
            data.append(line[5:].removeprefix(b" "))
    raise McpError("missing_response")


class McpClient:
    def __init__(self, url=DEFAULT_MCP_URL):
        self.host, self.port = loopback_endpoint(url)
        self._next_id = 0
        self._session_id = None
        self._version = None
        self.tools = frozenset()

    async def initialize(self):
        if self._version is not None:
            raise McpError("already_initialized")
        result = await self._request("initialize", {
            "protocolVersion": _VERSION, "capabilities": {},
            "clientInfo": {"name": "companion-desktop", "version": "1"},
        })
        version = result.get("protocolVersion")
        if version not in (_VERSION, "2025-03-26"):
            raise McpError("unsupported_mcp_version")
        self._version = version
        await self._request("notifications/initialized", notification=True)
        names, cursors = set(), set()
        params = {}
        for _ in range(8):
            page = await self._request("tools/list", params)
            tools = page.get("tools")
            if not isinstance(tools, list) or any(
                not isinstance(tool, dict) or not isinstance(tool.get("name"), str) for tool in tools
            ):
                raise McpError("invalid_tool_list")
            names.update(tool["name"] for tool in tools)
            cursor = page.get("nextCursor")
            if cursor is None:
                self.tools = frozenset(names)
                return self.tools
            if not isinstance(cursor, str) or not 0 < len(cursor) <= 1024 or cursor in cursors:
                break
            cursors.add(cursor)
            params = {"cursor": cursor}
        raise McpError("invalid_tool_list")

    async def call(self, name, arguments, *, timeout=2, before_write=lambda writing_body: True):
        if name not in self.tools:
            raise McpError("missing_tool")
        result = await self._request("tools/call", {"name": name, "arguments": arguments},
                                     timeout=timeout, before_write=before_write)
        if result.get("isError") is True:
            raise McpError("tool_error")
        return result

    async def cancel_action(self):
        if "cancel_action" in self.tools:
            await self.call("cancel_action", {})

    async def _request(self, method, params=None, *, notification=False, timeout=2,
                       before_write=lambda writing_body: True):
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = request_id
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self._version is not None:
            headers["MCP-Protocol-Version"] = self._version
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        body = json.dumps(payload).encode("utf-8")
        connection = _Connection(self.host, self.port, timeout, before_write, body)
        future = Future()

        def run():
            if not future.set_running_or_notify_cancel():
                return
            try:
                connection.request("POST", "/mcp", body, headers)
                with connection.getresponse() as response:
                    if notification and response.status == 202:
                        future.set_result((None, None))
                        return
                    if response.status != 200 or notification:
                        raise McpError("mcp_http_error")
                    session_id = response.getheader("Mcp-Session-Id") if method == "initialize" else None
                    content_type = response.headers.get_content_type()
                    if content_type == "text/event-stream":
                        frame = _read_sse(response, request_id)
                    elif content_type == "application/json":
                        data = response.read(_MAX_RESPONSE + 1)
                        if len(data) > _MAX_RESPONSE:
                            raise McpError("response_too_large")
                        frame = json.loads(data)
                    else:
                        raise McpError("invalid_content_type")
                    future.set_result((frame, session_id))
            except McpError as error:
                future.set_exception(error)
            except (OSError, ValueError, HTTPException, RecursionError):
                future.set_exception(McpError("invalid_or_disconnected_mcp"))
            finally:
                connection.close()

        pending = asyncio.wrap_future(future)
        Thread(target=run, daemon=True, name="stardew-mcp-http").start()
        try:
            frame, session_id = await asyncio.wait_for(pending, timeout)
        finally:
            connection.stop()
        if notification:
            return None
        if (not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0"
                or type(frame.get("id")) is not int or frame["id"] != request_id
                or "error" in frame or not isinstance(frame.get("result"), dict)):
            raise McpError("invalid_rpc_result")
        if session_id is not None:
            if not 0 < len(session_id) <= 256 or any(not 33 <= ord(char) <= 126 for char in session_id):
                raise McpError("invalid_mcp_session")
            self._session_id = session_id
        return frame["result"]
