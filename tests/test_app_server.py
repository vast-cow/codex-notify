import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app_server import RpcError, Settings, SOURCE_KINDS, lookup_thread


ENDPOINT = "ws://127.0.0.1:8765"


class SettingsTests(unittest.TestCase):
    def test_defaults_match_example_and_enabled_is_ignored(self):
        config = json.loads(Path("config.example.json").read_text())
        defaults = Settings.from_config({"endpoint": ENDPOINT})
        self.assertEqual(Settings.from_config(config["app_server"]), defaults)
        self.assertEqual(Settings.from_config({"endpoint": ENDPOINT, "enabled": False}), defaults)
        self.assertEqual(Settings.from_config({"endpoint": ENDPOINT, "enabled": "legacy"}), defaults)

    def test_valid_endpoints(self):
        for endpoint in (ENDPOINT, "ws://[::1]:8765/", "unix:///tmp/codex socket.sock"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(Settings.from_config({"endpoint": endpoint}).endpoint, endpoint)

    def test_invalid_settings(self):
        for section in (
            None, {}, {"endpoint": "unix://relative"}, {"endpoint": "unix:///"},
            {"endpoint": "ws://host"}, {"endpoint": "ws://host:0"},
            {"endpoint": "ws://host:8765?a=b"}, {"endpoint": "ws://user:secret@host:8765"},
            {"endpoint": "wss://host:8765"}, {"endpoint": "ws://host:8765/untrusted"},
            {"endpoint": ENDPOINT, "skip_ephemeral": 0},
            {"endpoint": ENDPOINT, "skip_archived": "false"},
            {"endpoint": ENDPOINT, "timeout_seconds": float("nan")},
            {"endpoint": ENDPOINT, "timeout_seconds": 0},
            {"endpoint": ENDPOINT, "timeout_seconds": True},
            {"endpoint": ENDPOINT, "on_lookup_failure": "fail"},
        ):
            with self.subTest(section=section), self.assertRaises(ValueError) as error:
                Settings.from_config(section)
            self.assertNotIn("secret", str(error.exception))


class MetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_membership_uses_exact_match_and_all_source_kinds(self):
        row = {"id": "thread", "cwd": "/project"}
        rpc = AsyncMock()
        rpc.request.side_effect = [
            {"thread": dict(row, ephemeral=False)}, {"data": [{"id": "different"}]},
            {"data": [row]}, {"goal": None},
        ]
        metadata = await lookup_thread(rpc, Settings(ENDPOINT), "thread", check_goal=True)
        self.assertEqual((metadata.cwd, metadata.ephemeral, metadata.archived, metadata.active_goal),
                         ("/project", False, False, False))
        archive = rpc.request.call_args_list[1].args
        self.assertEqual(archive, ("thread/list", {
            "archived": True, "includedThreadIds": ["thread"], "sourceKinds": list(SOURCE_KINDS),
            "modelProviders": [], "limit": 1,
        }))

    async def test_ephemeral_short_circuits_archive_and_goal(self):
        rpc = AsyncMock()
        rpc.request.return_value = {"thread": {"id": "thread", "cwd": "/project", "ephemeral": True}}
        metadata = await lookup_thread(rpc, Settings(ENDPOINT), "thread", check_goal=True)
        self.assertIs(metadata.ephemeral, True)
        rpc.request.assert_awaited_once_with("thread/read", {"threadId": "thread", "includeTurns": False})

    async def test_unknown_ephemeral_does_not_prevent_confirmed_archive(self):
        rpc = AsyncMock()
        rpc.request.side_effect = [
            RpcError("thread/read", -32601, "unavailable"),
            {"data": [{"id": "thread", "cwd": "/project"}]},
        ]
        metadata = await lookup_thread(rpc, Settings(ENDPOINT), "thread", check_goal=True)
        self.assertIsNone(metadata.ephemeral)
        self.assertIs(metadata.archived, True)
        self.assertEqual(rpc.request.await_count, 2)

    async def test_disabling_metadata_filters_still_looks_up_cwd(self):
        rpc = AsyncMock()
        rpc.request.return_value = {"thread": {"id": "thread", "cwd": "/project"}}
        metadata = await lookup_thread(rpc, Settings(ENDPOINT, False, False), "thread", check_goal=False)
        self.assertEqual(metadata.cwd, "/project")
        rpc.request.assert_awaited_once()

    async def test_working_directory_mismatch_invalidates_metadata(self):
        rpc = AsyncMock()
        rpc.request.side_effect = [
            {"thread": {"id": "thread", "cwd": "/project", "ephemeral": False}},
            {"data": [{"id": "thread", "cwd": "/different"}]},
        ]
        metadata = await lookup_thread(rpc, Settings(ENDPOINT), "thread", check_goal=True)
        self.assertIsNone(metadata.cwd)
        self.assertIsNone(metadata.ephemeral)
        self.assertIn("working directory", metadata.failures[0])

    async def test_goal_states_require_matching_thread(self):
        for goal, expected in (
            (None, False),
            *[({"threadId": "thread", "status": status}, status == "active") for status in
              ("active", "complete", "blocked", "paused", "usageLimited", "budgetLimited")],
            ({"threadId": "other", "status": "active"}, None),
            ({"threadId": "thread", "status": "unknown"}, None),
        ):
            with self.subTest(goal=goal):
                rpc = AsyncMock()
                rpc.request.side_effect = [
                    {"thread": {"id": "thread", "cwd": "/project"}}, {"goal": goal},
                ]
                metadata = await lookup_thread(rpc, Settings(ENDPOINT, False, False), "thread",
                                               check_goal=True)
                self.assertIs(metadata.active_goal, expected)
                rpc.request.assert_awaited_with("thread/goal/get", {"threadId": "thread"})

    async def test_timeout_and_unsupported_metadata_are_unknown(self):
        rpc = AsyncMock()
        rpc.request.side_effect = [
            {"thread": {"id": "thread", "cwd": "/project", "ephemeral": False}},
            asyncio.TimeoutError,
            {"data": [{"id": "different"}]},
            RpcError("thread/goal/get", -32601, "private text"),
        ]
        metadata = await lookup_thread(rpc, Settings(ENDPOINT), "thread", check_goal=True)
        self.assertIsNone(metadata.archived)
        self.assertIsNone(metadata.active_goal)
        self.assertEqual(metadata.failures, ["thread/list timed out", "thread/goal/get is unsupported"])
