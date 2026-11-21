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
from websockets.exceptions import ConnectionClosedOK

from app_server import AppServerError, Settings, WebSocketClient
from main import MonitorFormatter
from monitor import Monitor
from notifications import Notifier


USER_INPUT = "waitingOnUserInput"
APPROVAL = "waitingOnApproval"


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
        self.deferred = {}

    async def serve(self, connection):
        async for raw in connection:
            request = json.loads(raw)
            method = request["method"]
            self.requests[method] += 1
            if method == "initialized":
                await self.clients.put(connection)
                continue
            params = request["params"]
            try:
                result = {"result": self.answer(method, params)}
            except AppServerError as exc:
                result = {"error": {"code": -32600, "message": str(exc)}}
            response = json.dumps({"id": request["id"], **result})
            queue = self.deferred.get((method, params.get("threadId")))
            if queue is not None:
                queue.put_nowait(response)
                continue
            try:
                await connection.send(response)
            except ConnectionClosedOK:
                return

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
            return {"thread": {
                "id": thread_id, "cwd": cwd, "ephemeral": properties.get("ephemeral", False),
                "status": properties.get("status", {"type": "idle"}),
            }}
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
        if isinstance(pages[index], AppServerError):
            raise pages[index]
        return {"data": pages[index], "nextCursor": str(index + 1) if index + 1 < len(pages) else None}

    @staticmethod
    async def status(client, thread_id, state, *flags):
        status = {"type": state}
        if state == "active":
            status["activeFlags"] = list(flags)
        await client.send(json.dumps({
            "method": "thread/status/changed", "params": {"threadId": thread_id, "status": status},
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

    async def test_sends_input_and_approval_alerts_before_completion(self):
        options = {"min_turn_duration_minutes": 2, "thread_url_template": "codex://threads/{thread_id}"}
        input_label = "Waiting for user input"
        approval_label = "Waiting for approval or MCP input"
        cases = (
            ("input", (USER_INPUT,), input_label),
            ("approval", (APPROVAL,), approval_label),
            ("both", (APPROVAL, USER_INPUT), f"{input_label}\n{approval_label}"),
        )
        async with self.running_monitor(notification=options) as client:
            for thread_id, flags, labels in cases:
                with self.subTest(thread_id=thread_id):
                    self.fixture.metadata[thread_id] = {"goal": "active"}
                    self.fixture.histories[thread_id] = [[completion(thread_id, status="inProgress")]]
                    await self.fixture.status(client, thread_id, "active", *flags)
                    self.assertEqual(
                        await asyncio.wait_for(self.messages.get(), 1),
                        f"🔔 Codex CLI - Action required\n\n{labels}\n\n"
                        f"📁 Project: example\n📂 Path: /projects/example\n\n🔗 Thread: codex://threads/{thread_id}",
                    )
            self.assertEqual(self.fixture.requests["thread/goal/get"], 0)
            self.fixture.metadata["both"]["goal"] = None
            self.fixture.histories["both"] = [[self.recent("both", duration=120_000)]]
            await self.fixture.status(client, "both", "idle")
            self.assertIn("✅ Codex CLI - Task done", await asyncio.wait_for(self.messages.get(), 1))
        self.assertEqual(self.send.call_count, 4)

    async def test_suppresses_repeated_flags_until_each_resolves(self):
        async with self.running_monitor() as client:
            await self.fixture.status(client, "thread", "active", USER_INPUT)
            self.assertIn("Waiting for user input", await asyncio.wait_for(self.messages.get(), 1))
            await self.fixture.status(client, "thread", "active", USER_INPUT, USER_INPUT)
            await self.fixture.status(client, "thread", "active", USER_INPUT, APPROVAL, "unknownFlag")
            message = await asyncio.wait_for(self.messages.get(), 1)
            self.assertIn("Waiting for approval", message)
            self.assertNotIn("Waiting for user input", message)
            await self.fixture.status(client, "thread", "active", APPROVAL)
            await self.fixture.status(client, "thread", "active", USER_INPUT, APPROVAL)
            message = await asyncio.wait_for(self.messages.get(), 1)
            self.assertIn("Waiting for user input", message)
            self.assertNotIn("Waiting for approval", message)
            await self.fixture.status(client, "thread", "active")
            await self.fixture.status(client, "thread", "active", APPROVAL)
            self.assertIn("Waiting for approval", await asyncio.wait_for(self.messages.get(), 1))
        self.assertEqual(self.send.call_count, 4)
        self.assertEqual(self.fixture.requests["thread/read"], 4)

    async def test_combines_new_flags_during_a_lookup_while_history_is_pending(self):
        history = asyncio.Queue()
        self.fixture.loaded_pages = [["thread"] + [f"other-{index}" for index in range(7)]]
        for thread_id in self.fixture.loaded_pages[0]:
            self.fixture.deferred["thread/turns/list", thread_id] = history
        metadata = self.fixture.deferred["thread/read", "thread"] = asyncio.Queue()
        with patch("monitor.HISTORY_TIMEOUT", 5):
            async with self.running_monitor(server_overrides={"timeout_seconds": 5}) as client:
                for _ in self.fixture.loaded_pages[0]:
                    await asyncio.wait_for(history.get(), 1)
                await self.fixture.status(client, "thread", "active", USER_INPUT)
                response = await asyncio.wait_for(metadata.get(), 1)
                await self.fixture.status(client, "thread", "active", APPROVAL, USER_INPUT)
                await self.wait_for(lambda: self.monitor.waiting_flags.get("thread") == {USER_INPUT, APPROVAL})
                await client.send(response)
                message = await asyncio.wait_for(self.messages.get(), 1)
                self.assertIn("Waiting for user input\nWaiting for approval", message)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.fixture.requests["thread/read"], 1)

    async def test_omits_flags_that_resolve_during_a_lookup(self):
        metadata = self.fixture.deferred["thread/read", "thread"] = asyncio.Queue()
        async with self.running_monitor(server_overrides={"timeout_seconds": 3}) as client:
            for remaining in ((APPROVAL,), ()):
                await self.fixture.status(client, "thread", "active", USER_INPUT, APPROVAL)
                response = await asyncio.wait_for(metadata.get(), 1)
                state = "active" if remaining else "idle"
                await self.fixture.status(client, "thread", state, *remaining)
                await self.wait_for(lambda: self.monitor.waiting_flags.get("thread", set()) == set(remaining))
                await client.send(response)
                await self.wait_for(lambda: not self.monitor.action_checks)
            message = await asyncio.wait_for(self.messages.get(), 1)
            self.assertIn("Waiting for approval", message)
            self.assertNotIn("Waiting for user input", message)
            await self.fixture.status(client, "thread", "active", USER_INPUT)
            await client.send(await asyncio.wait_for(metadata.get(), 1))
            self.assertIn("Waiting for user input", await asyncio.wait_for(self.messages.get(), 1))
        self.assertEqual(self.send.call_count, 2)

    async def test_malformed_active_status_does_not_clear_a_pending_flag(self):
        metadata = self.fixture.deferred["thread/read", "thread"] = asyncio.Queue()
        async with self.running_monitor(server_overrides={"timeout_seconds": 3}) as client:
            await self.fixture.status(client, "thread", "active", USER_INPUT)
            response = await asyncio.wait_for(metadata.get(), 1)
            await client.send(json.dumps({
                "method": "thread/status/changed",
                "params": {"threadId": "thread", "status": {"type": "active", "activeFlags": None}},
            }))
            await client.send(response)
            self.assertIn("Waiting for user input", await asyncio.wait_for(self.messages.get(), 1))
            await self.fixture.status(client, "thread", "active", USER_INPUT)
            await self.fixture.status(client, "thread", "active", USER_INPUT, APPROVAL)
            await client.send(await asyncio.wait_for(metadata.get(), 1))
            message = await asyncio.wait_for(self.messages.get(), 1)
            self.assertIn("Waiting for approval", message)
            self.assertNotIn("Waiting for user input", message)
        self.assertEqual(self.send.call_count, 2)

    async def test_reconnect_cancels_lookups_and_uses_only_live_waiting_events(self):
        self.fixture.loaded_pages = [["thread"]]
        self.fixture.histories["thread"] = [[completion("ongoing", status="inProgress")]]
        self.fixture.metadata["thread"] = {"status": {"type": "active", "activeFlags": [USER_INPUT, APPROVAL]}}
        async with self.running_monitor(server_overrides={"timeout_seconds": 3}) as client:
            await self.wait_for(lambda: self.fixture.requests["thread/turns/list"] >= 1)
            self.assertEqual(self.fixture.requests["thread/read"], 0)
            self.assertEqual(self.send.call_count, 0)

            await self.fixture.status(client, "thread", "active", USER_INPUT)
            self.assertIn("Waiting for user input", await asyncio.wait_for(self.messages.get(), 1))
            metadata = self.fixture.deferred["thread/read", "thread"] = asyncio.Queue()
            await self.fixture.status(client, "thread", "active", USER_INPUT, APPROVAL)
            await asyncio.wait_for(metadata.get(), 1)
            check = self.monitor.action_checks["thread"]
            rpc = self.monitor.rpc
            await client.close()
            await self.wait_for(lambda: not self.monitor.action_checks and not self.monitor.pending_actions)
            self.assertTrue(check.done())
            self.assertFalse(rpc.pending)
            history_count = self.fixture.requests["thread/turns/list"]

            reconnected = await asyncio.wait_for(self.fixture.clients.get(), 3)
            await self.wait_for(lambda: self.fixture.requests["thread/turns/list"] > history_count)
            self.assertEqual(self.fixture.requests["thread/read"], 2)
            self.assertEqual(self.send.call_count, 1)
            del self.fixture.deferred["thread/read", "thread"]
            await self.fixture.status(reconnected, "thread", "active", USER_INPUT, APPROVAL)
            message = await asyncio.wait_for(self.messages.get(), 1)
            self.assertIn("Waiting for user input\nWaiting for approval", message)
        self.assertEqual(self.send.call_count, 2)

    async def test_action_alerts_apply_project_archive_and_ephemeral_filters(self):
        self.fixture.metadata = {
            "excluded": {"cwd": "/projects/hidden", "list_missing": True},
            "archived": {"archived": True},
            "ephemeral": {"ephemeral": True},
        }
        options = {"excluded_project_paths": ["/projects/hidden/"], "thread_url_template": "codex://{thread_id}"}
        async with self.running_monitor(notification=options) as client:
            for thread_id in ("excluded", "archived", "ephemeral", "allowed"):
                await self.fixture.status(client, thread_id, "active", APPROVAL)
            await self.wait_for(lambda: len(self.monitor.waiting_flags) == 4 and not self.monitor.action_checks)
            self.assertIn("codex://allowed", await asyncio.wait_for(self.messages.get(), 1))
        self.assertEqual(self.send.call_count, 1)

    async def test_action_alerts_share_lookup_policy_and_delivery_settings(self):
        self.fixture.metadata = {
            "missing": {"read_failure": True, "list_missing": True},
            "temporary": {"archived": True, "ephemeral": True},
        }
        options = {
            "include_project": False, "title": "Codex Work", "thread_url_template": "codex://threads/{thread_id}",
        }
        for policy in ("notify", "skip", "error"):
            with self.subTest(policy=policy):
                level = "ERROR" if policy == "error" else "WARNING"
                with self.assertLogs("notify", level) as logs, patch("notifications.send_telegram") as telegram:
                    async with self.running_monitor(
                        notification=options,
                        server_overrides={"on_lookup_failure": policy, "skip_ephemeral": False, "skip_archived": False},
                        telegram={"enabled": True},
                    ) as client:
                        for thread_id in ("missing", "temporary"):
                            await self.fixture.status(client, thread_id, "active", USER_INPUT)
                        await self.wait_for(lambda: len(self.monitor.waiting_flags) == 2 and not self.monitor.action_checks)
                    links = ["temporary", "missing"] if policy == "notify" else ["temporary"]
                    expected = [
                        f"🔔 Codex Work - Action required\n\nWaiting for user input\n\n🔗 Thread: codex://threads/{name}"
                        for name in links
                    ]
                    self.assertCountEqual([telegram_call.args[1] for telegram_call in telegram.call_args_list], expected)
                    self.assertEqual(self.messages.qsize(), len(expected))
                    self.assertCountEqual([self.messages.get_nowait() for _ in expected], expected)
                self.assertIn(f"on_lookup_failure={policy}", "\n".join(logs.output))

    async def test_checks_delayed_history_and_ignores_older_and_unsuccessful_turns(self):
        previous = completion("old", completed_at=self.startup)
        self.fixture.loaded_pages = [["thread"]]
        self.fixture.histories["thread"] = [[previous]]
        async with self.running_monitor() as client:
            await self.wait_for(lambda: self.fixture.requests["thread/turns/list"] >= 1)
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

    async def test_startup_recovers_delayed_history_without_a_status_event(self):
        previous = completion("old", completed_at=self.startup - 1)
        self.fixture.loaded_pages = [["thread"]]
        self.fixture.histories["thread"] = [[previous]]
        async with self.running_monitor():
            await self.wait_for(lambda: self.fixture.requests["thread/turns/list"] >= 1)
            self.fixture.histories["thread"] = [[self.recent("startup"), previous]]
            self.assertIn("request startup", await asyncio.wait_for(self.messages.get(), 0.5))
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

    async def test_reconnect_recovers_later_visible_turn_after_delivering_an_earlier_one(self):
        self.fixture.loaded_pages = [[]]
        async with self.running_monitor() as client:
            await self.wait_for(lambda: self.fixture.requests["thread/loaded/list"] >= 1)
            await client.close()
            self.fixture.loaded_pages = [["thread"]]
            first = self.recent("already-visible")
            self.fixture.histories["thread"] = [[first]]
            await asyncio.wait_for(self.fixture.clients.get(), 3)
            self.assertIn("request already-visible", await asyncio.wait_for(self.messages.get(), 3))
            self.fixture.histories["thread"] = [[self.recent("visible-later"), first]]
            self.assertIn("request visible-later", await asyncio.wait_for(self.messages.get(), 0.5))
        self.assertEqual(self.send.call_count, 2)

    async def test_overlapping_status_events_can_refer_to_two_completions_in_one_second(self):
        async with self.running_monitor():
            await self.wait_for(lambda: self.monitor.loading is None)
            for state in ("active", "idle", "active", "idle"):
                self.monitor.on_status({"threadId": "thread", "status": {"type": state}})
            completed_at = time.time()
            first = completion("same-second-first", completed_at=completed_at)
            self.fixture.histories["thread"] = [[first]]
            self.assertIn("request same-second-first", await asyncio.wait_for(self.messages.get(), 2))
            second = completion("same-second-second", completed_at=completed_at)
            self.fixture.histories["thread"] = [[second, first]]
            self.assertIn("request same-second-second", await asyncio.wait_for(self.messages.get(), 2))
        self.assertEqual(self.send.call_count, 2)

    async def test_available_completion_survives_a_failure_fetching_an_older_page(self):
        self.fixture.histories["thread"] = [
            [self.recent("available")], AppServerError("older page unavailable"),
        ]
        with self.assertLogs("notify", "WARNING") as logs, patch("monitor.HISTORY_TIMEOUT", 0.15):
            async with self.running_monitor() as client:
                await self.fixture.status(client, "thread", "idle")
                self.assertIn("request available", await asyncio.wait_for(self.messages.get(), 0.5))
                await self.wait_for(lambda: "thread" not in self.monitor.scans)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual([MonitorFormatter().format(record) for record in logs.records], [
            "Turn history unavailable (thread/turns/list failed (JSON-RPC error -32600)) thread_id='thread'",
        ])

    async def test_large_summary_does_not_disconnect_the_monitor(self):
        turn = self.recent("large")
        turn["items"][0]["content"][0]["text"] += "a" * (1024 * 1024)
        self.fixture.histories["thread"] = [[turn]]
        async with self.running_monitor() as client:
            await self.fixture.status(client, "thread", "idle")
            self.assertIn("request large", await asyncio.wait_for(self.messages.get(), 0.5))
            self.assertEqual(self.fixture.requests["initialize"], 1)

    async def test_busy_history_workers_do_not_expire_a_turn_before_its_first_lookup(self):
        occupied = set()
        release = asyncio.Event()
        process_turn = Monitor.process_turn

        async def block_other_turns(monitor, thread_id, turn):
            if thread_id == "queued":
                await process_turn(monitor, thread_id, turn)
            else:
                occupied.add(thread_id)
                await release.wait()

        with patch.object(Monitor, "process_turn", block_other_turns), patch("monitor.HISTORY_TIMEOUT", 0.3):
            async with self.running_monitor() as client:
                for index in range(8):
                    thread_id = f"busy-{index}"
                    self.fixture.histories[thread_id] = [[self.recent(thread_id)]]
                    await self.fixture.status(client, thread_id, "idle")
                await self.wait_for(lambda: len(occupied) == 8)
                self.fixture.histories["queued"] = [[self.recent("queued")]]
                await self.fixture.status(client, "queued", "idle")
                await self.wait_for(lambda: "queued" in self.monitor.pending)
                await asyncio.sleep(0.35)
                release.set()
                self.assertIn("request queued", await asyncio.wait_for(self.messages.get(), 2))

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
        self.fixture.histories = {name: [[self.recent(f"turn-{name}")]] for name in self.fixture.loaded_pages[0]}
        self.fixture.histories["short"] = [[self.recent("turn-short", duration=59_999)]]
        with self.assertLogs("notify", level="INFO") as logs:
            async with self.running_monitor(
                notification={"excluded_project_paths": ["/projects/hidden/"]},
                server_overrides={"on_lookup_failure": "error"},
            ) as client:
                await self.wait_for(lambda: len(self.monitor.processed) == 7)
                self.assertIn("request turn-ok", await asyncio.wait_for(self.messages.get(), 3))
                self.fixture.histories["later"] = [[self.recent("later")]]
                await self.fixture.status(client, "later", "idle")
                self.assertIn("request later", await asyncio.wait_for(self.messages.get(), 3))
        self.assertEqual(self.send.call_count, 2)
        diagnostics = [MonitorFormatter().format(record) for record in logs.records
                       if record.getMessage().startswith(("Skipped", "App-server lookup incomplete"))]
        self.assertCountEqual(diagnostics, [
            "Skipped turn with duration 59999 ms below the 1-minute minimum thread_id='short' turn_id='turn-short'",
            "Skipped notification from an excluded project thread_id='excluded' turn_id='turn-excluded'",
            "Skipped notification due to archived thread thread_id='archive' turn_id='turn-archive'",
            "Skipped notification due to ephemeral thread thread_id='ephemeral' turn_id='turn-ephemeral'",
            "Skipped turn history (thread history is unavailable for ephemeral threads) thread_id='temp'",
            "Skipped notification due to active goal thread_id='goal' turn_id='turn-goal'",
            "App-server lookup incomplete for active goal (on_lookup_failure=error; "
            "thread/goal/get failed (JSON-RPC error -32600)) thread_id='bad' turn_id='turn-bad'",
        ])
        self.assertNotIn("private goal", "\n".join(logs.output))

    async def test_other_lookup_policies_and_disabled_filters(self):
        for policy, expected in (("notify", 1), ("skip", 0), ("error", 0)):
            with self.subTest(policy=policy):
                before = self.send.call_count
                self.fixture.loaded_pages = [["archived", "missing", "temporary"]]
                self.fixture.metadata = {
                    "archived": {"archived": True, "ephemeral": True, "goal": "active"},
                    "missing": {"read_failure": True, "list_missing": True, "goal_failure": True},
                    "temporary": {"history_unavailable": True},
                }
                self.fixture.histories = {name: [[self.recent(name)]] for name in self.fixture.loaded_pages[0]}
                async with self.running_monitor(
                    notification={"skip_active_goals": False},
                    server_overrides={
                        "skip_ephemeral": False, "skip_archived": False, "on_lookup_failure": policy,
                    },
                ):
                    await self.wait_for(
                        lambda: len(self.monitor.processed) == 2 and "temporary" not in self.monitor.pending
                    )
                    await self.monitor.notifier.drain()
                self.assertEqual(self.send.call_count - before, expected + 1)
        self.assertEqual(self.fixture.requests["thread/goal/get"], 0)

    async def test_slow_delivery_does_not_block_events_or_another_destination(self):
        release = threading.Event()
        started = asyncio.Event()
        self.fixture.histories["done"] = [[self.recent("completed-turn")]]
        self.fixture.histories["excluded"] = [[self.recent("excluded-turn")]]
        self.fixture.metadata["excluded"] = {"archived": True}

        def slow_telegram(_config, message, _timeout):
            if "Action required" not in message:
                raise RuntimeError("private delivery details")
            self.loop.call_soon_threadsafe(started.set)
            release.wait(3)

        with self.assertLogs("notify") as logs, patch("notifications.send_telegram", side_effect=slow_telegram):
            async with self.running_monitor(telegram={"enabled": True}) as client:
                try:
                    await self.fixture.status(client, "waiting", "active", USER_INPUT)
                    await asyncio.wait_for(started.wait(), 3)
                    self.assertIn("Action required", await asyncio.wait_for(self.messages.get(), 3))
                    await self.fixture.status(client, "done", "idle")
                    await self.fixture.status(client, "excluded", "active", APPROVAL)
                    await self.wait_for(lambda: "excluded" in self.monitor.waiting_flags
                                        and "excluded" not in self.monitor.action_checks)
                    await self.fixture.status(client, "excluded", "idle")
                    self.assertIn("request completed-turn", await asyncio.wait_for(self.messages.get(), 3))
                    await self.wait_for(lambda: ("excluded", "excluded-turn") in self.monitor.processed)
                    await self.wait_for(lambda: any("Telegram delivery failed" in entry for entry in logs.output))
                finally:
                    release.set()
        diagnostics = [MonitorFormatter().format(record) for record in logs.records
                       if record.getMessage() != "Monitoring shared app-server"]
        self.assertCountEqual(diagnostics, [
            "Slack notification sent thread_id='waiting' notification_type='action_required'",
            "Telegram notification sent thread_id='waiting' notification_type='action_required'",
            "Slack notification sent thread_id='done' turn_id='completed-turn' notification_type='completion'",
            "Telegram delivery failed (RuntimeError) thread_id='done' turn_id='completed-turn' "
            "notification_type='completion'",
            "Skipped notification due to archived thread thread_id='excluded'",
            "Skipped notification due to archived thread thread_id='excluded' turn_id='excluded-turn'",
        ])

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
