import asyncio
import json
import logging
import socket
import tempfile
import threading
import time
import unittest
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from websockets.asyncio.server import serve, unix_serve

from app_server import AppServerError, Settings, WebSocketClient
from monitor import Monitor
from notifications import Notifier


def completion(turn_id, *, status="completed", completed_at=None, duration=60_000):
    return {
        "id": turn_id,
        "status": status,
        "completedAt": completed_at,
        "durationMs": duration,
        "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "request " + turn_id}]},
            {"type": "agentMessage", "text": "response " + turn_id},
        ],
    }


class Fixture:
    def __init__(self):
        self.clients = asyncio.Queue()
        self.requests = Counter()
        self.loaded_pages = [[]]
        self.histories = {}
        self.metadata = {}

    async def serve(self, connection):
        async for raw in connection:
            request = json.loads(raw)
            method = request["method"]
            self.requests[method] += 1
            if method == "initialized":
                await self.clients.put(connection)
                continue
            try:
                result = {"result": self.answer(method, request["params"])}
            except AppServerError as exc:
                result = {"error": {"code": -32600, "message": str(exc)}}
            await connection.send(json.dumps({"id": request["id"], **result}))

    def answer(self, method, params):
        if method == "initialize":
            return {"userAgent": "codex-fixture"}
        if method == "thread/loaded/list":
            index = int(params.get("cursor", 0))
            return self.page(self.loaded_pages, index)
        thread_id = params["threadId"] if "threadId" in params else params["includedThreadIds"][0]
        properties = self.metadata.get(thread_id, {})
        cwd = properties.get("cwd", "/projects/example")
        if method == "thread/turns/list":
            if properties.get("history_unavailable"):
                raise AppServerError("ephemeral threads do not support thread/turns/list")
            if params["itemsView"] != "summary" or params["sortDirection"] != "desc":
                raise AssertionError(f"unexpected history params {params}")
            index = int(params.get("cursor", 0))
            return self.page(self.histories.get(thread_id, [[]]), index)
        if method == "thread/read":
            if properties.get("read_failure"):
                raise AppServerError("private information from the server")
            return {"thread": {"id": thread_id, "cwd": cwd, "ephemeral": properties.get("ephemeral", False)}}
        if method == "thread/list":
            if properties.get("list_missing"):
                return {"data": []}
            matches = params["archived"] == properties.get("archived", False)
            return {"data": [{"id": thread_id, "cwd": cwd}] if matches else []}
        if method == "thread/goal/get":
            if properties.get("goal_failure"):
                raise AppServerError("private goal")
            status = properties.get("goal")
            return {"goal": {"threadId": thread_id, "status": status} if status else None}
        raise AssertionError(f"unexpected request {method}")

    @staticmethod
    def page(pages, index):
        return {"data": pages[index], "nextCursor": str(index + 1) if index + 1 < len(pages) else None}

    @staticmethod
    async def status(client, thread_id, state):
        await client.send(json.dumps({
            "method": "thread/status/changed", "params": {"threadId": thread_id, "status": {"type": state}},
        }))


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = Fixture()
        self.messages = asyncio.Queue()
        self.monitor = None
        self.startup = time.time() - 20
        self.addCleanup(patch.stopall)
        patch("monitor.POLL_INTERVAL", 0.02).start()
        patch("monitor.HISTORY_TIMEOUT", 1).start()
        self.send = patch("notifications.send_slack", side_effect=self.record_delivery).start()
        self.logger = logging.getLogger("notify")
        patch.object(self.logger, "handlers", [logging.NullHandler()]).start()
        patch.object(self.logger, "propagate", False).start()

    def record_delivery(self, _config, message, _timeout):
        self.loop.call_soon_threadsafe(self.messages.put_nowait, message)

    def create_monitor(self, endpoint, *, notification=None, server_overrides=None, telegram=None):
        self.loop = asyncio.get_running_loop()
        settings = Settings.from_config({
            "endpoint": endpoint, "timeout_seconds": 0.15, **(server_overrides or {}),
        })
        notifier = Notifier({
            "slack": {"enabled": True}, "telegram": telegram or {}, "notification": notification or {},
        })
        self.monitor = Monitor(settings, WebSocketClient(settings), notifier, self.startup)
        return self.monitor

    @asynccontextmanager
    async def running_monitor(self, *, unix=None, **options):
        if unix is None:
            server = serve(self.fixture.serve, "127.0.0.1", 0)
        else:
            server = unix_serve(self.fixture.serve, path=unix)
        async with server as listener:
            endpoint = (
                "unix://" + str(unix) if unix is not None
                else f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
            )
            monitor = self.create_monitor(endpoint, **options)
            task = asyncio.create_task(monitor.run())
            try:
                yield await asyncio.wait_for(self.fixture.clients.get(), 3)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def wait_for(self, check):
        async def wait():
            while not check():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), 4)

    def recent(self, turn_id, **kwargs):
        return completion(turn_id, completed_at=time.time(), **kwargs)

    async def test_checks_delayed_history_and_ignores_older_and_unsuccessful_turns(self):
        previous = completion("old", completed_at=self.startup)
        self.fixture.loaded_pages = [["thread"]]
        self.fixture.histories["thread"] = [[previous]]
        async with self.running_monitor() as client:
            await self.wait_for(lambda: self.fixture.requests["thread/turns/list"] == 1)
            await self.fixture.status(client, "thread", "active")
            await self.fixture.status(client, "thread", "idle")
            await self.wait_for(lambda: self.fixture.requests["thread/turns/list"] >= 3)
            self.fixture.histories["thread"] = [[completion("new")], [previous]]
            await self.wait_for(lambda: self.fixture.requests["thread/turns/list"] >= 5)
            self.fixture.histories["thread"] = [
                [self.recent("failed", status="failed"), self.recent("interrupted", status="interrupted")],
                [self.recent("new"), previous],
            ]
            message = await asyncio.wait_for(self.messages.get(), 3)
            self.assertIn("📝 Request:\nrequest new", message)
            self.assertIn("📋 Result:\nresponse new", message)
            await self.wait_for(lambda: ("thread", "failed") in self.monitor.processed)
        self.assertEqual(self.send.call_count, 1)

    async def test_reconnect_recovers_previous_and_newly_loaded_threads_once(self):
        self.fixture.loaded_pages = [["gone"], ["still"]]
        first = self.recent("first")
        self.fixture.histories["still"] = [[first]]
        async with self.running_monitor() as client:
            self.assertIn("request first", await asyncio.wait_for(self.messages.get(), 3))
            await self.fixture.status(client, "gone", "active")
            await self.wait_for(lambda: "gone" in self.monitor.active_since)
            await client.close()
            self.fixture.loaded_pages = [["still"], ["new"]]
            self.fixture.histories["still"] = [[first], [first]]
            self.fixture.histories["gone"] = [[self.recent("missed-from-gone")]]
            self.fixture.histories["new"] = [[self.recent("missed-from-new")]]
            reconnected = await asyncio.wait_for(self.fixture.clients.get(), 3)
            for _ in range(2):
                await self.fixture.status(reconnected, "still", "idle")
                await self.fixture.status(reconnected, "new", "idle")
            results = [await asyncio.wait_for(self.messages.get(), 3) for _ in range(2)]
            await self.wait_for(lambda: self.fixture.requests["thread/loaded/list"] >= 4)
            self.assertCountEqual(
                [message.split("request ")[1].splitlines()[0] for message in results],
                ["missed-from-gone", "missed-from-new"],
            )
        self.assertEqual(self.send.call_count, 3)

    async def test_filters_and_metadata_error_do_not_stop_other_turns(self):
        self.fixture.loaded_pages = [["short", "excluded", "archive", "ephemeral", "temp", "goal", "bad", "ok"]]
        self.fixture.metadata.update({
            "excluded": {"cwd": "/projects/hidden"},
            "archive": {"archived": True},
            "ephemeral": {"ephemeral": True},
            "temp": {"history_unavailable": True},
            "goal": {"goal": "active"},
            "bad": {"goal_failure": True},
        })
        self.fixture.histories = {name: [[self.recent(name)]] for name in self.fixture.loaded_pages[0]}
        self.fixture.histories["short"] = [[self.recent("short", duration=59_999)]]
        with self.assertLogs("notify", level="ERROR") as logs:
            async with self.running_monitor(
                notification={"excluded_project_paths": ["/projects/hidden/"]},
                server_overrides={"on_lookup_failure": "error"},
            ) as client:
                await self.wait_for(lambda: len(self.monitor.processed) == 7)
                self.assertIn("request ok", await asyncio.wait_for(self.messages.get(), 3))
                self.fixture.histories["later"] = [[self.recent("later")]]
                await self.fixture.status(client, "later", "idle")
                self.assertIn("request later", await asyncio.wait_for(self.messages.get(), 3))
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("on_lookup_failure=error", "\n".join(logs.output))
        self.assertNotIn("private goal", "\n".join(logs.output))

    async def test_other_lookup_policies_and_disabled_filters(self):
        for policy, expected in (("notify", 1), ("skip", 0), ("error", 0)):
            with self.subTest(policy=policy):
                before = self.send.call_count
                self.fixture.loaded_pages = [["archived", "missing"]]
                self.fixture.metadata = {
                    "archived": {"archived": True, "ephemeral": True, "goal": "active"},
                    "missing": {"read_failure": True, "list_missing": True, "goal_failure": True},
                }
                self.fixture.histories = {name: [[self.recent(name)]] for name in self.fixture.loaded_pages[0]}
                async with self.running_monitor(
                    notification={"skip_active_goals": False},
                    server_overrides={
                        "skip_ephemeral": False, "skip_archived": False, "on_lookup_failure": policy,
                    },
                ):
                    await self.wait_for(lambda: len(self.monitor.processed) == 2)
                    await self.monitor.notifier.drain()
                self.assertEqual(self.send.call_count - before, expected + 1)
        self.assertEqual(self.fixture.requests["thread/goal/get"], 0)

    async def test_slow_delivery_does_not_block_events_or_another_destination(self):
        release = threading.Event()
        started = asyncio.Event()

        def slow_telegram(_config, _message, _timeout):
            self.loop.call_soon_threadsafe(started.set)
            release.wait(3)

        with patch("notifications.send_telegram", side_effect=slow_telegram):
            async with self.running_monitor(telegram={"enabled": True}) as client:
                try:
                    self.fixture.histories["first"] = [[self.recent("first")]]
                    await self.fixture.status(client, "first", "idle")
                    await asyncio.wait_for(started.wait(), 3)
                    self.fixture.histories["second"] = [[self.recent("second")]]
                    await self.fixture.status(client, "second", "idle")
                    self.assertIn("request first", await asyncio.wait_for(self.messages.get(), 3))
                    self.assertIn("request second", await asyncio.wait_for(self.messages.get(), 3))
                    self.assertFalse(release.is_set())
                finally:
                    release.set()

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix sockets unavailable")
    async def test_unix_websocket_receives_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            async with self.running_monitor(unix=Path(directory) / "app.sock") as client:
                self.fixture.histories["thread"] = [[self.recent("unix")]]
                await self.fixture.status(client, "thread", "idle")
                self.assertIn("request unix", await asyncio.wait_for(self.messages.get(), 3))


class BackoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_failures_grow_backoff_to_thirty_seconds(self):
        @asynccontextmanager
        async def refused(*args):
            raise OSError("offline")
            yield

        class Stopped(Exception):
            pass

        transport = type("OfflineTransport", (), {"open": staticmethod(refused)})()
        notifier = Notifier({})
        monitor = Monitor(Settings("ws://127.0.0.1:8765"), transport, notifier, 0)
        sleep = AsyncMock(side_effect=[None] * 6 + [Stopped()])
        with patch("monitor.asyncio.sleep", sleep), self.assertLogs("notify", "WARNING"):
            with self.assertRaises(Stopped):
                await monitor.run()
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2, 4, 8, 16, 30, 30])
