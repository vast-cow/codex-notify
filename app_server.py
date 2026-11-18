"""Bounded, read-only metadata lookups against a configured Codex app-server."""

import asyncio
import inspect
import json
import logging
import math
import os
import socket
from concurrent.futures import Future
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import Thread
from typing import List, Optional
from urllib.parse import urlsplit


# An omitted or empty sourceKinds filter only returns interactive sources.
SOURCE_KINDS = (
    "cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
    "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown",
)


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    endpoint: str = "unix:///tmp/codex-app-server.sock"
    skip_ephemeral: bool = True
    skip_archived: bool = True
    timeout_seconds: float = 1.5
    on_lookup_failure: str = "notify"

    @classmethod
    def from_config(cls, section):
        if not isinstance(section, dict):
            raise ValueError("app_server must be an object")
        defaults = cls()
        values = {key: section.get(key, value) for key, value in vars(defaults).items()}
        for key in ("enabled", "skip_ephemeral", "skip_archived"):
            if type(values[key]) is not bool:
                raise ValueError(f"app_server.{key} must be true or false")
        _validate_endpoint(values["endpoint"])
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
    ephemeral: Optional[bool] = None
    archived: Optional[bool] = None
    failures: List[str] = field(default_factory=list)


class AppServerError(Exception):
    """A lookup error with a diagnostic safe to log."""


class ThreadIdentityError(AppServerError):
    pass


class RpcError(AppServerError):
    def __init__(self, method, code, message):
        self.code = code
        if code == -32601:
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


async def _getaddrinfo(*args, **kwargs):
    # The system resolver cannot be cancelled. Keep it out of asyncio.run's
    # default executor so interpreter shutdown cannot extend the lookup deadline.
    future = Future()

    def resolve():
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(socket.getaddrinfo(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)

    Thread(target=resolve, daemon=True).start()
    return await asyncio.wrap_future(future)


@asynccontextmanager
async def _connect(endpoint):
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
    except ImportError:
        raise AppServerError(
            "optional dependency missing; install 'websockets>=13.1,<17' with the hook's Python"
        ) from None

    class DirectConnect(connect):
        def process_redirect(self, exc):
            return exc

    options = {
        "open_timeout": None,
        "close_timeout": 0,
        "ping_interval": None,
        "compression": None,
        # Wire-level logs can contain arbitrary peer data.
        "logger": logging.Logger("codex-notify.app-server", logging.CRITICAL + 1),
    }
    if "proxy" in inspect.signature(connect).parameters:
        options["proxy"] = None
    uri = endpoint
    if endpoint.startswith("unix://"):
        if not hasattr(socket, "AF_UNIX"):
            raise AppServerError("Unix domain sockets are unavailable on this platform")
        options.update(unix=True, path=endpoint[len("unix://"):])
        uri = "ws://localhost/"

    try:
        connection = await DirectConnect(uri, **options)
        try:
            yield connection
            await connection.close()
        finally:
            connection.transport.abort()
    except InvalidStatus as exc:
        raise AppServerError(f"WebSocket upgrade rejected (HTTP {exc.response.status_code})") from None
    except ConnectionClosed:
        raise AppServerError("WebSocket connection closed during lookup") from None
    except WebSocketException:
        raise AppServerError("WebSocket protocol or handshake error") from None


class RpcClient:
    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline
        self.next_id = 1

    def check_deadline(self):
        if asyncio.get_running_loop().time() >= self.deadline:
            raise asyncio.TimeoutError

    async def send(self, message):
        self.check_deadline()
        await self.connection.send(json.dumps(message))

    async def request(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            self.check_deadline()
            raw = await self.connection.recv()
            try:
                message = json.loads(raw) if isinstance(raw, str) else None
            except (ValueError, RecursionError):
                raise AppServerError("malformed JSON-RPC response") from None
            if not isinstance(message, dict) or message.get("jsonrpc", "2.0") != "2.0":
                raise AppServerError("malformed JSON-RPC response")
            self.check_deadline()
            if isinstance(message.get("method"), str) and not (
                "result" in message or "error" in message
            ):
                if "id" in message:
                    # This read-only client has no handlers for server requests.
                    raise AppServerError("unexpected JSON-RPC request from app-server")
                continue
            if type(message.get("id")) is not int or message["id"] != request_id:
                raise AppServerError("unexpected JSON-RPC response ID")
            if "method" in message or ("result" in message) == ("error" in message):
                raise AppServerError("malformed JSON-RPC response")
            if "error" in message:
                error = message["error"]
                if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(
                    error.get("message"), str
                ):
                    raise AppServerError("malformed JSON-RPC error")
                raise RpcError(method, error["code"], error["message"])
            return message["result"]


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


async def _query(settings, thread_id, event_cwd, metadata, deadline):
    async with _connect(settings.endpoint) as connection:
        rpc = RpcClient(connection, deadline)
        initialized = await rpc.request("initialize", {
            "clientInfo": {"name": "codex-notify", "version": "1.0"},
        })
        if not isinstance(initialized, dict) or not isinstance(
            initialized.get("userAgent"), str
        ) or not initialized["userAgent"].strip():
            raise AppServerError("incompatible initialize response (missing userAgent)")
        await rpc.send({"method": "initialized"})

        read_cwd = None
        if settings.skip_ephemeral:
            try:
                result = await rpc.request("thread/read", {
                    "threadId": thread_id, "includeTurns": False,
                })
            except RpcError as exc:
                metadata.failures.append(str(exc))
            else:
                thread = result.get("thread") if isinstance(result, dict) else None
                if isinstance(thread, dict):
                    read_cwd = _check_thread(thread, thread_id, event_cwd)
                    ephemeral = thread.get("ephemeral")
                    if type(ephemeral) is bool:
                        metadata.ephemeral = ephemeral
                else:
                    metadata.failures.append("malformed thread/read result")
            if metadata.ephemeral is True:
                return

        if settings.skip_archived:
            for archived in (True, False):
                try:
                    result = await rpc.request("thread/list", {
                        "archived": archived,
                        "includedThreadIds": [thread_id],
                        "sourceKinds": list(SOURCE_KINDS),
                        "modelProviders": [],
                        "limit": 1,
                    })
                except RpcError as exc:
                    metadata.failures.append(str(exc))
                    # An unsupported method or rejected parameters apply to both queries.
                    if exc.code in (-32601, -32602):
                        break
                    continue
                rows = result.get("data") if isinstance(result, dict) else None
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    metadata.failures.append("malformed thread/list result")
                    continue
                # Older servers ignore includedThreadIds and may return unrelated rows.
                matches = [row for row in rows if row.get("id") == thread_id]
                if matches:
                    for row in matches:
                        _check_thread(row, thread_id, event_cwd, read_cwd)
                    metadata.archived = archived
                    return


async def _lookup(settings, thread_id, cwd, metadata):
    loop = asyncio.get_running_loop()
    if settings.endpoint.startswith("ws://"):
        loop.getaddrinfo = _getaddrinfo
    deadline = loop.time() + settings.timeout_seconds
    await asyncio.wait_for(
        _query(settings, thread_id, cwd, metadata, deadline),
        timeout=settings.timeout_seconds,
    )


def lookup_thread(settings, event):
    """Return verified properties, with None for each unavailable property."""
    metadata = ThreadMetadata()
    thread_id = event.get("thread-id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        metadata.failures.append("missing thread ID")
        return metadata
    cwd = event.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else None
    try:
        asyncio.run(_lookup(settings, thread_id, cwd, metadata))
    except ThreadIdentityError as exc:
        return ThreadMetadata(failures=[str(exc)])
    except (asyncio.TimeoutError, TimeoutError):
        metadata.failures.append("lookup timeout")
    except ConnectionRefusedError:
        metadata.failures.append("connection refused")
    except FileNotFoundError:
        metadata.failures.append("Unix socket not found")
    except AppServerError as exc:
        metadata.failures.append(str(exc))
    except (OSError, ValueError, EOFError, NotImplementedError):
        metadata.failures.append("could not communicate with the configured endpoint")
    return metadata
