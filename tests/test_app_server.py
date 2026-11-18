import asyncio
import json
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
from types import ModuleType
from unittest.mock import AsyncMock, Mock, patch

import app_server


THREAD_ID = "01a1e7bc-7698-7420-bf3e-34533b790e55"
CWD = "/projects/example"
EVENT = {"thread-id": THREAD_ID, "cwd": CWD}
INITIALIZE = {"userAgent": "codex/0.169.0", "platformOs": "linux"}
THREAD = {"id": THREAD_ID, "cwd": CWD, "ephemeral": False}


def response(request_id, result):
    return json.dumps({"id": request_id, "result": result})


def rpc_error(request_id, code, message):
    return json.dumps({"id": request_id, "error": {"code": code, "message": message}})


class SettingsTests(unittest.TestCase):
    def test_defaults_match_example(self):
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        with example.open(encoding="utf-8") as file:
            self.assertEqual(app_server.Settings.from_config(json.load(file)["app_server"]),
                             app_server.Settings())

    def test_valid_endpoints(self):
        for endpoint in ("ws://localhost:9000", "ws://[::1]:9000/",
                         "unix:///tmp/codex socket.sock"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(app_server.Settings.from_config({"endpoint": endpoint}).endpoint,
                                 endpoint)

    def test_invalid_settings(self):
        cases = {
            "enabled": [1, "true", None],
            "skip_ephemeral": [0, [], "false"],
            "skip_archived": [0, {}, "false"],
            "endpoint": [
                None, "ws://host", "ws://host:0", "ws://host:65536", "ws://[bad:90",
                "ws://user:secret@host:90", "ws://host:90/?token=secret", "ws://host:90/other",
                "wss://host:90", "unix://relative.sock", "unix:///", "unix:///tmp/x\x00",
            ],
            "timeout_seconds": [None, False, "1", 0, -1, float("inf"), float("nan"), 10**400],
            "on_lookup_failure": [None, "ignore", []],
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value), \
                        self.assertRaisesRegex(ValueError, f"app_server\\.{key}"):
                    app_server.Settings.from_config({key: value})
        with self.assertRaisesRegex(ValueError, "app_server must be an object"):
            app_server.Settings.from_config(None)


class LookupTests(unittest.TestCase):
    def lookup(self, replies, *, event=EVENT, **options):
        connection = Mock(send=AsyncMock(), recv=AsyncMock(side_effect=replies))
        closed = Mock()

        @asynccontextmanager
        async def connect(endpoint):
            try:
                yield connection
            finally:
                closed(endpoint)

        settings = app_server.Settings(enabled=True, **options)
        with patch.object(app_server, "_connect", side_effect=connect):
            metadata = app_server.lookup_thread(settings, event)
        closed.assert_called_once_with(settings.endpoint)
        return metadata, [json.loads(call.args[0]) for call in connection.send.await_args_list]

    def test_initialize_then_read_and_both_archive_memberships(self):
        metadata, sent = self.lookup([
            json.dumps({"method": "account/updated", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": INITIALIZE}),
            response(2, {"thread": {**THREAD, "cwd": CWD + "/./"}}),
            response(3, {"data": []}),
            response(4, {"data": [THREAD]}),
        ])
        self.assertEqual(metadata, app_server.ThreadMetadata(False, False))
        self.assertEqual(sent[:3], [
            {"id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "codex-notify", "version": "1.0"}}},
            {"method": "initialized"},
            {"id": 2, "method": "thread/read",
             "params": {"threadId": THREAD_ID, "includeTurns": False}},
        ])
        for index, archived in enumerate((True, False), 3):
            self.assertEqual(sent[index], {
                "id": index, "method": "thread/list", "params": {
                    "archived": archived, "includedThreadIds": [THREAD_ID],
                    "sourceKinds": [
                        "cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
                        "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown",
                    ],
                    "modelProviders": [], "limit": 1,
                },
            })

    def test_ephemeral_exclusion_short_circuits_before_listing(self):
        for skip_archived in (True, False):
            with self.subTest(skip_archived=skip_archived):
                metadata, sent = self.lookup([
                    response(1, INITIALIZE), response(2, {"thread": {**THREAD, "ephemeral": True}}),
                ], skip_archived=skip_archived)
                self.assertEqual(metadata, app_server.ThreadMetadata(True, None))
                self.assertEqual(sent[-1]["method"], "thread/read")

    def test_archived_only_skips_thread_read(self):
        metadata, sent = self.lookup([
            response(1, INITIALIZE), response(2, {"data": [THREAD]}),
        ], skip_ephemeral=False)
        self.assertEqual(metadata, app_server.ThreadMetadata(None, True))
        self.assertEqual(sent[-1]["method"], "thread/list")
        self.assertIs(sent[-1]["params"]["archived"], True)

    def test_unknown_ephemeral_does_not_prevent_confirmed_archived(self):
        missing = {key: value for key, value in THREAD.items() if key != "ephemeral"}
        for reply in (
            rpc_error(2, -32600, "thread not loaded: " + THREAD_ID),
            response(2, {"thread": {**THREAD, "ephemeral": 0}}),
            response(2, {"thread": missing}),
        ):
            with self.subTest(reply=reply):
                metadata, _ = self.lookup([
                    response(1, INITIALIZE), reply, response(3, {"data": [THREAD]}),
                ])
                self.assertIsNone(metadata.ephemeral)
                self.assertIs(metadata.archived, True)

    def test_absence_in_both_lists_and_failed_read_leave_states_unknown(self):
        metadata, _ = self.lookup([
            response(1, INITIALIZE),
            rpc_error(2, -32600, "thread not loaded: " + THREAD_ID),
            response(3, {"data": []}), response(4, {"data": []}),
        ])
        self.assertEqual(metadata, app_server.ThreadMetadata(failures=["thread not found"]))

    def test_older_server_ignoring_id_filter_requires_exact_membership(self):
        other = {**THREAD, "id": "other-thread"}
        archived_page = {"data": [other], "nextCursor": "opaque cursor"}
        for active_page, expected in (([], None), ([other], None), ([THREAD], False)):
            with self.subTest(active_page=active_page):
                metadata, sent = self.lookup([
                    response(1, INITIALIZE), response(2, archived_page),
                    response(3, {"data": active_page}),
                ], skip_ephemeral=False)
                self.assertIs(metadata.archived, expected)
                self.assertEqual(len(sent), 4)

    def test_unsupported_methods_or_invalid_parameters_are_unknown(self):
        for code, reason in ((-32601, "is unsupported"), (-32602, "rejected its parameters")):
            with self.subTest(code=code):
                metadata, _ = self.lookup([
                    response(1, INITIALIZE), rpc_error(2, code, "private input"),
                    rpc_error(3, code, "private output"),
                ])
                self.assertEqual(metadata, app_server.ThreadMetadata(failures=[
                    f"thread/read {reason}", f"thread/list {reason}",
                ]))

    def test_mismatched_read_identity_invalidates_lookup(self):
        for thread in ({**THREAD, "id": "unrelated", "ephemeral": True},
                       {**THREAD, "cwd": "/other", "ephemeral": True},
                       {**THREAD, "cwd": 42, "ephemeral": True}):
            with self.subTest(thread=thread):
                metadata, _ = self.lookup([
                    response(1, INITIALIZE), response(2, {"thread": thread}),
                ])
                self.assertIsNone(metadata.ephemeral)
                self.assertIsNone(metadata.archived)
                self.assertIn("thread identity mismatch", metadata.failures[0])

    def test_list_cwd_mismatch_invalidates_read_even_without_event_cwd(self):
        for event in (EVENT, {"thread-id": THREAD_ID}):
            with self.subTest(event=event):
                metadata, _ = self.lookup([
                    response(1, INITIALIZE), response(2, {"thread": THREAD}),
                    response(3, {"data": [{**THREAD, "cwd": "/other"}]}),
                ], event=event)
                self.assertEqual(metadata, app_server.ThreadMetadata(
                    failures=["thread identity mismatch (working directory)"],
                ))

    def test_missing_thread_id_does_not_connect(self):
        with patch.object(app_server, "_connect") as connect:
            metadata = app_server.lookup_thread(app_server.Settings(), {"cwd": CWD})
        self.assertEqual(metadata, app_server.ThreadMetadata(failures=["missing thread ID"]))
        connect.assert_not_called()

    def test_connection_refused(self):
        @asynccontextmanager
        async def connect(endpoint):
            raise ConnectionRefusedError("details containing credentials")
            yield

        with patch.object(app_server, "_connect", side_effect=connect):
            metadata = app_server.lookup_thread(app_server.Settings(), EVENT)
        self.assertEqual(metadata, app_server.ThreadMetadata(failures=["connection refused"]))

    def test_timeout_bounds_entire_lookup_and_closes_connection(self):
        closed = Mock()

        async def recv():
            # Each reply is faster than the timeout in isolation.
            await asyncio.sleep(0.035)
            return response(connection.send.await_count, INITIALIZE)

        connection = Mock(send=AsyncMock(), recv=AsyncMock(side_effect=recv))

        @asynccontextmanager
        async def connect(endpoint):
            try:
                yield connection
            finally:
                closed()

        with patch.object(app_server, "_connect", side_effect=connect):
            metadata = app_server.lookup_thread(app_server.Settings(timeout_seconds=0.06), EVENT)
        self.assertEqual(metadata, app_server.ThreadMetadata(failures=["lookup timeout"]))
        closed.assert_called_once()

    def test_slow_dns_does_not_hold_up_lookup_shutdown(self):
        started, release, finished = Event(), Event(), Event()

        def resolve(*args, **kwargs):
            started.set()
            release.wait(timeout=2)
            finished.set()
            return []

        @asynccontextmanager
        async def connect(endpoint):
            await asyncio.get_running_loop().getaddrinfo("example.test", 8765)
            yield

        settings = app_server.Settings(endpoint="ws://example.test:8765", timeout_seconds=0.1)
        try:
            with patch.object(app_server, "_connect", side_effect=connect), \
                    patch.object(app_server.socket, "getaddrinfo", side_effect=resolve):
                metadata = app_server.lookup_thread(settings, EVENT)
            self.assertEqual(metadata, app_server.ThreadMetadata(failures=["lookup timeout"]))
            self.assertTrue(started.is_set())
            self.assertFalse(finished.is_set())
        finally:
            release.set()
            finished.wait(timeout=1)

    def test_malformed_and_unexpected_messages_are_safe_failures(self):
        for raw, expected in (
            ("secret prompt", "malformed JSON-RPC response"),
            (b"{}", "malformed JSON-RPC response"),
            (response(99, {"secret": "value"}), "unexpected JSON-RPC response ID"),
            (response(True, INITIALIZE), "unexpected JSON-RPC response ID"),
            ('{"id": 1, "result": {}, "error": {}}', "malformed JSON-RPC response"),
            ('{"id": 1, "error": {"code": "unknown"}}', "malformed JSON-RPC error"),
            ('{"id": 100, "method": "item/commandExecution/requestApproval"}',
             "unexpected JSON-RPC request from app-server"),
            (response(1, {}), "incompatible initialize response (missing userAgent)"),
        ):
            with self.subTest(raw=raw):
                metadata, sent = self.lookup([raw])
                self.assertEqual(metadata, app_server.ThreadMetadata(failures=[expected]))
                self.assertEqual(len(sent), 1)


class TransportTests(unittest.TestCase):
    def check_transport(self, endpoint, uri, **transport_args):
        connection = Mock(close=AsyncMock())
        opened = Mock()

        class Connect:
            def __init__(self, uri, *, proxy=True, **kwargs):
                opened(uri, proxy=proxy, **kwargs)
                error = RuntimeError("redirect to an unrelated endpoint")
                if self.process_redirect(error) is not error:
                    raise AssertionError("redirect would be followed")

            def __await__(self):
                async def connect():
                    return connection

                return connect().__await__()

        client = ModuleType("websockets.asyncio.client")
        client.connect = Connect
        exceptions = ModuleType("websockets.exceptions")
        exceptions.ConnectionClosed = type("ConnectionClosed", (Exception,), {})
        exceptions.InvalidStatus = type("InvalidStatus", (Exception,), {})
        exceptions.WebSocketException = type("WebSocketException", (Exception,), {})

        async def connect():
            async with app_server._connect(endpoint) as websocket:
                self.assertIs(websocket, connection)

        with patch.dict(sys.modules, {client.__name__: client, exceptions.__name__: exceptions}), \
                patch.object(app_server.socket, "AF_UNIX", create=True):
            asyncio.run(connect())

        opened.assert_called_once()
        self.assertEqual(opened.call_args.args, (uri,))
        self.assertIsNone(opened.call_args.kwargs["proxy"])
        for key in ("unix", "path"):
            self.assertEqual(opened.call_args.kwargs.get(key), transport_args.get(key))
        connection.close.assert_awaited_once()
        connection.transport.abort.assert_called_once()

    def test_tcp_websocket_opens_only_configured_endpoint_and_closes(self):
        endpoint = "ws://127.0.0.1:8765"
        self.check_transport(endpoint, endpoint)

    def test_unix_websocket_opens_only_configured_socket_and_closes(self):
        self.check_transport(
            "unix:///tmp/codex.sock", "ws://localhost/", unix=True, path="/tmp/codex.sock",
        )

    def test_missing_optional_dependency_is_actionable(self):
        with patch.dict(sys.modules, {"websockets.asyncio.client": None}):
            metadata = app_server.lookup_thread(app_server.Settings(), EVENT)
        self.assertIn("install 'websockets", metadata.failures[0])


if __name__ == "__main__":
    unittest.main()
