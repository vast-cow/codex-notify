"""Persistent WebSocket transport and read-only app-server lookups."""

import asyncio
import inspect
import json
import logging
import math
import os
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit


# An omitted or empty sourceKinds filter only returns interactive sources.
SOURCE_KINDS = (
    "cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
    "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown",
)
GOAL_STATUSES = ("active", "paused", "blocked", "usageLimited", "budgetLimited", "complete")


@dataclass(frozen=True)
class Settings:
    endpoint: str
    skip_ephemeral: bool = True
    skip_archived: bool = True
    timeout_seconds: float = 1.5
    on_lookup_failure: str = "notify"

    @classmethod
    def from_config(cls, section):
        if not isinstance(section, dict):
            raise ValueError("app_server must be an object")
        endpoint = section.get("endpoint")
        _validate_endpoint(endpoint)
        defaults = cls(endpoint)
        values = {key: section.get(key, value) for key, value in vars(defaults).items()}
        for key in ("skip_ephemeral", "skip_archived"):
            if type(values[key]) is not bool:
                raise ValueError(f"app_server.{key} must be true or false")
        timeout = values["timeout_seconds"]
        try:
            valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("app_server.timeout_seconds must be a finite positive number")
        if values["on_lookup_failure"] not in ("notify", "skip", "error"):
            raise ValueError("app_server.on_lookup_failure must be notify, skip, or error")
        return cls(**values)


def _validate_endpoint(endpoint):
    if isinstance(endpoint, str) and not any(ord(char) < 32 or ord(char) == 127 for char in endpoint):
        if endpoint.startswith("unix:///") and endpoint != "unix:///" and not any(
            char in endpoint for char in ("?", "#")
        ):
            return
        if endpoint.startswith("ws://") and not any(
            char.isspace() or char in "\\?#" for char in endpoint
        ):
            try:
                url = urlsplit(endpoint)
                if url.hostname and url.port and url.username is None and url.path in ("", "/"):
                    return
            except ValueError:
                pass
    raise ValueError(
        "app_server.endpoint must be ws://host:port or unix:///absolute/socket/path "
        "without credentials, a query, or a fragment"
    )


@dataclass
class ThreadMetadata:
    cwd: Optional[str] = None
    ephemeral: Optional[bool] = None
    archived: Optional[bool] = None
    failures: List[str] = field(default_factory=list)
    active_goal: Optional[bool] = None


class AppServerError(Exception):
    """A lookup error with a diagnostic safe to log."""


class ConnectionLost(AppServerError):
    pass


class ThreadIdentityError(AppServerError):
    pass


class RpcError(AppServerError):
    def __init__(self, method, code, message):
        self.code = code
        self.ephemeral_history = (
            method == "thread/turns/list" and code == -32600
            and message == "ephemeral threads do not support thread/turns/list"
        )
        if self.ephemeral_history:
            reason = "thread history is unavailable for ephemeral threads"
        elif code == -32601:
            reason = f"{method} is unsupported"
        elif code == -32602:
            reason = f"{method} rejected its parameters"
        elif method == "thread/read" and code == -32600 and message.lower().startswith(
            ("thread not found", "thread not loaded", "no rollout found")
        ):
            reason = "thread not found"
        else:
            reason = f"{method} failed (JSON-RPC error {code})"
        super().__init__(reason)


class WebSocketClient:
    def __init__(self, settings):
        try:
            from websockets.asyncio.client import connect
            from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
        except ImportError:
            raise ImportError("install the WebSocket client with 'python3 -m pip install -r requirements.txt'") from None

        class DirectConnect(connect):
            def process_redirect(self, exc):
                return exc

        self.connect = DirectConnect
        self.connection_closed = ConnectionClosed
        self.invalid_status = InvalidStatus
        self.websocket_error = WebSocketException
        endpoint = settings.endpoint
        self.uri = endpoint
        self.options: Dict[str, Any] = {
            "open_timeout": settings.timeout_seconds,
            "close_timeout": 1,
            "compression": None,
            # Match the Codex remote client's limit for history responses.
            "max_size": 128 * 1024 * 1024,
            "logger": logging.Logger("codex-notify.app-server", logging.CRITICAL + 1),
        }
        if "proxy" in inspect.signature(connect).parameters:
            self.options["proxy"] = None
        if endpoint.startswith("unix://"):
            if not hasattr(socket, "AF_UNIX"):
                raise ValueError("Unix domain sockets are unavailable on this platform")
            self.uri = "ws://localhost/"
            self.options.update(unix=True, path=endpoint[len("unix://"):])

    @asynccontextmanager
    async def open(self, on_status, timeout):
        try:
            async with self.connect(self.uri, **self.options) as connection:
                rpc = RpcClient(connection, self.connection_closed, on_status, timeout)
                try:
                    await rpc.initialize()
                    yield rpc
                finally:
                    rpc.reader.cancel()
                    await asyncio.gather(rpc.reader, return_exceptions=True)
        except self.invalid_status as exc:
            raise AppServerError(
                f"WebSocket upgrade rejected (HTTP {exc.response.status_code})"
            ) from None
        except self.connection_closed:
            raise ConnectionLost("WebSocket connection closed") from None
        except self.websocket_error:
            raise AppServerError("WebSocket protocol or handshake error") from None


class RpcClient:
    def __init__(self, connection, connection_closed, on_status, timeout):
        self.connection = connection
        self.connection_closed = connection_closed
        self.on_status = on_status
        self.timeout = timeout
        self.next_id = 1
        self.pending = {}
        self.failure = None
        self.reader = asyncio.create_task(self.receive())

    async def initialize(self):
        result = await self.request("initialize", {
            "clientInfo": {"name": "codex-notify", "version": "2.0"},
            "capabilities": {"experimentalApi": True},
        })
        if not isinstance(result, dict) or not isinstance(result.get("userAgent"), str):
            raise AppServerError("incompatible initialize response (missing userAgent)")
        await asyncio.wait_for(self.send({"method": "initialized"}), self.timeout)

    async def send(self, message):
        try:
            await self.connection.send(json.dumps(message))
        except self.connection_closed:
            raise ConnectionLost("WebSocket connection closed") from None

    async def request(self, method, params, *, timeout=None):
        deadline = self.timeout if timeout is None else min(self.timeout, timeout)
        return await asyncio.wait_for(self.exchange(method, params), deadline)

    async def exchange(self, method, params):
        if self.failure is not None:
            raise self.failure
        request_id = self.next_id
        self.next_id += 1
        response = asyncio.get_running_loop().create_future()
        self.pending[request_id] = response
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            message = await response
        finally:
            self.pending.pop(request_id)
        if message is None:
            assert self.failure is not None
            raise self.failure
        if "error" in message:
            error = message["error"]
            if (
                not isinstance(error, dict) or type(error.get("code")) is not int
                or not isinstance(error.get("message"), str)
            ):
                raise AppServerError("malformed JSON-RPC error")
            raise RpcError(method, error["code"], error["message"])
        return message["result"]

    async def receive(self):
        try:
            async for raw in self.connection:
                try:
                    message = json.loads(raw)
                except (ValueError, RecursionError):
                    raise ConnectionLost("malformed JSON-RPC message") from None
                if not isinstance(message, dict) or message.get("jsonrpc", "2.0") != "2.0":
                    raise ConnectionLost("malformed JSON-RPC message")
                if "method" in message:
                    # Status notifications arrive independently of outstanding history requests.
                    if "id" not in message and message["method"] == "thread/status/changed":
                        self.on_status(message.get("params"))
                    continue
                request_id = message.get("id")
                response = self.pending.get(request_id) if type(request_id) is int else None
                if response is not None and not response.done():
                    if ("result" in message) == ("error" in message):
                        raise ConnectionLost("malformed JSON-RPC response")
                    response.set_result(message)
        except self.connection_closed:
            pass
        except ConnectionLost as exc:
            self.failure = exc
        finally:
            if self.failure is None:
                self.failure = ConnectionLost("WebSocket connection closed")
            for response in self.pending.values():
                if not response.done():
                    response.set_result(None)
        return self.failure

    async def wait_closed(self):
        raise await self.reader


async def pages(rpc, method, params, *, deadline=None):
    cursors = set()
    while True:
        remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
        result = await rpc.request(method, params, timeout=remaining)
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise AppServerError(f"malformed {method} result")
        yield result["data"]
        cursor = result.get("nextCursor")
        if cursor is None:
            return
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise AppServerError(f"invalid {method} cursor")
        cursors.add(cursor)
        params = {**params, "cursor": cursor}


def _check_thread(thread, thread_id, *expected_cwds):
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        raise ThreadIdentityError("thread identity mismatch")
    cwd = thread.get("cwd")
    if cwd is None:
        return None
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
        raise ThreadIdentityError("thread identity mismatch (invalid working directory)")
    cwd = os.path.normpath(cwd)
    if any(cwd != os.path.normpath(expected) for expected in expected_cwds if expected):
        raise ThreadIdentityError("thread identity mismatch (working directory)")
    return cwd


async def lookup_thread(rpc, settings, thread_id, *, check_goal):
    metadata = ThreadMetadata()

    async def lookup(method, params):
        try:
            return await rpc.request(method, params)
        except ConnectionLost:
            raise
        except asyncio.TimeoutError:
            metadata.failures.append(f"{method} timed out")
        except AppServerError as exc:
            metadata.failures.append(str(exc))

    result = await lookup("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = result.get("thread") if isinstance(result, dict) else None
    try:
        if isinstance(thread, dict):
            metadata.cwd = _check_thread(thread, thread_id)
            ephemeral = thread.get("ephemeral")
            if type(ephemeral) is bool:
                metadata.ephemeral = ephemeral
        elif result is not None:
            metadata.failures.append("malformed thread/read result")
        if settings.skip_ephemeral and metadata.ephemeral is True:
            return metadata

        if settings.skip_archived or metadata.cwd is None:
            for archived in (True, False):
                result = await lookup("thread/list", {
                    "archived": archived,
                    "includedThreadIds": [thread_id],
                    "sourceKinds": list(SOURCE_KINDS),
                    "modelProviders": [],
                    "limit": 1,
                })
                rows = result.get("data") if isinstance(result, dict) else None
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    if result is not None:
                        metadata.failures.append("malformed thread/list result")
                    continue
                # Older servers may ignore includedThreadIds. Require an exact match.
                for row in rows:
                    if row.get("id") == thread_id:
                        listed_cwd = _check_thread(row, thread_id, metadata.cwd)
                        metadata.cwd = metadata.cwd or listed_cwd
                        metadata.archived = archived
                        break
                if metadata.archived is not None:
                    break
    except ThreadIdentityError as exc:
        return ThreadMetadata(failures=[str(exc)])

    if check_goal and not (settings.skip_archived and metadata.archived is True):
        result = await lookup("thread/goal/get", {"threadId": thread_id})
        if isinstance(result, dict) and "goal" in result:
            goal = result["goal"]
            if goal is None:
                metadata.active_goal = False
            elif (
                isinstance(goal, dict) and goal.get("status") in GOAL_STATUSES
                and goal.get("threadId") == thread_id
            ):
                metadata.active_goal = goal["status"] == "active"
        if result is not None and metadata.active_goal is None:
            metadata.failures.append("malformed or mismatched thread/goal/get result")
    return metadata
