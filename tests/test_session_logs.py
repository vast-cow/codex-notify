import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

try:
    import sqlite3
except ImportError:
    sqlite3 = None

import main
import session_logs
from app_server import ThreadMetadata


THREAD_ID = "01a1e7bc-7698-7420-bf3e-34533b790e55"
OTHER_THREAD_ID = "01a1e7bc-7698-7420-bf3e-34533b790e56"
TURN_ID = "test-turn"
EVENT = {"type": "agent-turn-complete", "thread-id": THREAD_ID, "turn-id": TURN_ID,
         "cwd": "/projects/example"}


def completion(duration_ms, turn_id=TURN_ID, event_type="task_complete"):
    return {"type": "event_msg", "payload": {
        "type": event_type, "turn_id": turn_id, "duration_ms": duration_ms,
    }}


class LocalFilterTestCase(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config_path = self.directory / "config.json"
        self.codex_home = self.directory / "codex"
        self.elapsed = 0

    def write_log(self, *records, thread_id=THREAD_ID, initial_thread_id=None,
                  archived=False, rollout_id=None):
        directory = self.codex_home / ("archived_sessions" if archived else "sessions/2026/11/20")
        directory.mkdir(parents=True, exist_ok=True)
        filename_id = f"{thread_id}_{rollout_id}" if rollout_id else thread_id
        path = directory / f"rollout-2026-11-20T10-00-00-{filename_id}.jsonl"
        header = {"type": "session_meta", "payload": {"id": initial_thread_id or thread_id}}
        path.write_text("".join(json.dumps(record) + "\n" for record in (header, *records)),
                        encoding="utf-8")
        return path

    def run_notification(self, *, options=None, payload=EVENT, on_sleep=None,
                         codex_home=None, sqlite_home=""):
        config = {
            "notification": options or {},
            "telegram": {"enabled": True, "bot_token": "test", "chat_id": "test"},
            "slack": {"enabled": True, "webhook_url": "https://example.test/slack"},
            "app_server": {"enabled": True},
        }
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.elapsed = 0

        def sleep(seconds):
            self.elapsed += seconds
            if on_sleep:
                on_sleep(self.elapsed)

        clock = SimpleNamespace(monotonic=lambda: self.elapsed, sleep=sleep)
        environment = {
            "CODEX_HOME": str(self.codex_home) if codex_home is None else codex_home,
            "CODEX_SQLITE_HOME": str(sqlite_home),
        }
        with patch.dict(os.environ, environment), \
                patch.object(session_logs, "time", clock), \
                patch.object(main, "CONFIG_PATH", self.config_path), \
                patch.object(sys, "argv", ["main.py", json.dumps(payload)]), \
                patch.object(main.app_server, "lookup_thread", return_value=ThreadMetadata(False, False)) as lookup, \
                patch.object(main, "format_message", wraps=main.format_message) as formatter, \
                patch.object(main, "post_json", return_value=(200, b'{"ok":true}')) as post, \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            status = main.main()
        return status, lookup, formatter, post, stdout.getvalue() + stderr.getvalue()

    def assert_skipped(self, result, reason, status=0):
        actual_status, lookup, formatter, post, output = result
        self.assertEqual(actual_status, status)
        self.assertIn(reason, output)
        lookup.assert_not_called()
        formatter.assert_not_called()
        post.assert_not_called()

    def assert_delivered(self, result):
        status, lookup, formatter, post, output = result
        self.assertEqual(status, 0, output)
        lookup.assert_called_once()
        formatter.assert_called_once()
        self.assertEqual([call.args[0] for call in post.call_args_list], [
            "https://api.telegram.org/bottest/sendMessage", "https://example.test/slack",
        ])


class DurationFilterTests(LocalFilterTestCase):
    def test_default_threshold_for_both_channels(self):
        for duration_ms in (59_999, 60_000, 60_001):
            with self.subTest(duration_ms=duration_ms):
                self.write_log(completion(duration_ms))
                result = self.run_notification()
                if duration_ms == 59_999:
                    self.assert_skipped(result, "turn duration 59999 ms is below the 1-minute minimum")
                else:
                    self.assert_delivered(result)

    def test_custom_thresholds(self):
        for minutes, duration_ms, delivered in (
            (2, 60_000, False), (2, 120_000, True),
            (0.5, 29_999, False), (0.5, 30_000, True),
            (0.03315, 1_989, True), (0.0331501, 1_989, False),
        ):
            with self.subTest(minutes=minutes, duration_ms=duration_ms):
                self.write_log(completion(duration_ms))
                result = self.run_notification(options={"min_turn_duration_minutes": minutes})
                if delivered:
                    self.assert_delivered(result)
                else:
                    self.assert_skipped(result, "is below")

    def test_zero_disables_log_lookup(self):
        with patch.object(session_logs, "turn_duration_ms", side_effect=AssertionError):
            self.assert_delivered(self.run_notification(
                options={"min_turn_duration_minutes": 0}, payload={"type": EVENT["type"]},
            ))

    def test_invalid_thresholds(self):
        for minutes in (-1, -0.5, "1", True, False, None, [], {},
                        float("nan"), float("inf"), float("-inf")):
            with self.subTest(minutes=minutes):
                self.assert_skipped(
                    self.run_notification(options={"min_turn_duration_minutes": minutes}),
                    "Configuration error: notification.min_turn_duration_minutes", status=1,
                )
                self.assertEqual(self.elapsed, 0)

    def test_threshold_preserves_decimal_precision_from_config(self):
        for literal, duration_ms, skipped in (
            ("1.00000000000000001", 60_000, True),
            ("1e-400", 0, True), ("1e-400", 1, False),
        ):
            with self.subTest(literal=literal, duration_ms=duration_ms), \
                    patch.object(main, "CONFIG_PATH", self.config_path), \
                    patch.object(session_logs, "turn_duration_ms", return_value=duration_ms), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.config_path.write_text(
                    '{"notification": {"min_turn_duration_minutes": ' + literal + '}}',
                    encoding="utf-8",
                )
                self.assertEqual(main.is_turn_duration_filtered(EVENT, main.load_config()), skipped)

        self.config_path.write_text(
            '{"notification": {"min_turn_duration_minutes": -1e-400}}', encoding="utf-8",
        )
        with patch.object(main, "CONFIG_PATH", self.config_path):
            with self.assertRaisesRegex(ValueError, "notification.min_turn_duration_minutes"):
                main.is_turn_duration_filtered(EVENT, main.load_config())

    def test_project_exclusion_precedes_log_lookup(self):
        with patch.object(session_logs, "turn_duration_ms", side_effect=AssertionError):
            self.assert_skipped(self.run_notification(options={
                "excluded_project_paths": [EVENT["cwd"]],
            }), "")

    def test_ignores_completions_from_other_threads(self):
        self.write_log(completion(120_000), thread_id=OTHER_THREAD_ID, rollout_id=THREAD_ID)
        self.assert_skipped(self.run_notification(), "session log not found within 1 second")
        self.assertAlmostEqual(self.elapsed, 1)
        self.write_log(completion(120_000), initial_thread_id=OTHER_THREAD_ID)
        self.assert_skipped(self.run_notification(), "session log belongs to a different thread")

    def test_ignores_other_turns_and_noncompletion_events(self):
        self.write_log(completion(120_000, turn_id="other-turn"),
                       completion(120_000, event_type="task_started"))
        self.assert_skipped(self.run_notification(), "completed record not found within 30 seconds")
        self.assertAlmostEqual(self.elapsed, 30)

    def test_waits_for_log_and_for_completion_appended_in_parts(self):
        data = json.dumps(completion(60_000, event_type="turn_complete")) + "\n"
        log = None
        finished = False

        def write_delayed_record(elapsed):
            nonlocal log, finished
            if log is None and elapsed >= 0.2:
                log = self.write_log(completion(5, turn_id="other-turn"))
                with log.open("a", encoding="utf-8") as writer:
                    writer.write(data[:30])
            elif not finished and elapsed >= 3:
                with log.open("a", encoding="utf-8") as writer:
                    writer.write(data[30:])
                finished = True

        self.assert_delivered(self.run_notification(on_sleep=write_delayed_record))
        self.assertGreaterEqual(self.elapsed, 3)

    def test_missing_or_invalid_duration(self):
        for duration_ms in (None, -1, True, "60000", 60_000.5):
            with self.subTest(duration_ms=duration_ms):
                record = completion(duration_ms)
                if duration_ms is None:
                    del record["payload"]["duration_ms"]
                self.write_log(record)
                self.assert_skipped(self.run_notification(), "completed record has no valid duration_ms")

    def test_read_error_skips_both_channels(self):
        self.write_log(completion(60_000))
        with patch.object(session_logs, "_read_completion", side_effect=PermissionError):
            self.assert_skipped(self.run_notification(), "could not read the session log")

    def test_default_codex_home_with_archived_reverted_rollout(self):
        self.codex_home = self.directory / ".codex"
        self.write_log(completion(60_000), archived=True, rollout_id=OTHER_THREAD_ID)
        with patch.dict(os.environ, {"HOME": str(self.directory)}):
            self.assert_delivered(self.run_notification(codex_home=""))


@unittest.skipIf(sqlite3 is None, "Python was built without SQLite support")
class ActiveGoalFilterTests(LocalFilterTestCase):
    def write_goals(self, *rows, path=None):
        if path is None:
            path = self.codex_home / "goals_1.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS thread_goals (thread_id TEXT PRIMARY KEY, status TEXT)"
            )
            connection.execute("DELETE FROM thread_goals")
            connection.executemany("INSERT INTO thread_goals VALUES (?, ?)", rows)
        return path

    def test_only_active_goal_in_event_thread_suppresses_delivery_by_default(self):
        self.write_log(completion(60_000))
        for status in ("active", "complete", "paused", "blocked", "budget_limited", "usage_limited"):
            with self.subTest(status=status):
                self.write_goals((THREAD_ID, status), (OTHER_THREAD_ID, "active"))
                result = self.run_notification()
                if status == "active":
                    self.assert_skipped(result, "skipped: active goal")
                else:
                    self.assert_delivered(result)
        self.write_goals((OTHER_THREAD_ID, "active"))
        self.assert_delivered(self.run_notification())

    def test_disabled_filter_does_not_open_database_or_wait(self):
        self.write_goals((THREAD_ID, "active"))
        with patch.object(sqlite3, "connect", side_effect=AssertionError), \
                patch.object(session_logs, "turn_completion", side_effect=AssertionError):
            self.assert_delivered(self.run_notification(options={
                "min_turn_duration_minutes": 0, "skip_active_goals": False,
            }))

    def test_nonboolean_setting_is_a_configuration_error(self):
        for value in (None, 0, 1, 0.0, "true", "false", [], {}):
            with self.subTest(value=value), patch.object(sqlite3, "connect", side_effect=AssertionError):
                self.assert_skipped(self.run_notification(options={
                    "min_turn_duration_minutes": 0, "skip_active_goals": value,
                }), "Configuration error: notification.skip_active_goals", status=1)

    def test_project_and_duration_filters_precede_database_lookup(self):
        self.write_goals((THREAD_ID, "active"))
        self.write_log(completion(59_999))
        with patch.object(sqlite3, "connect", side_effect=AssertionError):
            self.assert_skipped(self.run_notification(options={
                "excluded_project_paths": [EVENT["cwd"]],
            }), "")
            self.assert_skipped(self.run_notification(), "is below")

    def test_missing_corrupt_or_unrecognized_database_allows_delivery(self):
        options = {"min_turn_duration_minutes": 0}
        self.codex_home.mkdir()
        path = self.codex_home / "goals_1.sqlite"
        self.assert_delivered(self.run_notification(options=options))
        self.assertEqual(list(self.codex_home.iterdir()), [])

        # A corrupt current database takes precedence over the legacy file.
        self.write_goals((THREAD_ID, "active"), path=self.codex_home / "state_5.sqlite")
        path.write_bytes(b"not a SQLite database")
        self.assert_delivered(self.run_notification(options=options))
        path.unlink()
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE other_table (id TEXT)")
        self.assert_delivered(self.run_notification(options=options))
        self.assertEqual(self.elapsed, 0)

    def test_legacy_database_used_when_current_database_is_missing(self):
        self.write_log(completion(60_000))
        self.write_goals((THREAD_ID, "active"), path=self.codex_home / "state_5.sqlite")
        self.assert_skipped(self.run_notification(), "skipped: active goal")
        self.assertFalse((self.codex_home / "goals_1.sqlite").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes are unavailable")
    def test_named_pipes_at_database_paths_allow_delivery_without_opening(self):
        current = self.codex_home / "goals_1.sqlite"
        legacy = self.write_goals((THREAD_ID, "active"), path=self.codex_home / "state_5.sqlite")
        options = {"min_turn_duration_minutes": 0}
        with patch.object(sqlite3, "connect", side_effect=AssertionError):
            os.mkfifo(current)
            self.assert_delivered(self.run_notification(options=options))

            current.unlink()
            legacy.unlink()
            os.mkfifo(legacy)
            self.assert_delivered(self.run_notification(options=options))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes are unavailable")
    def test_named_pipes_at_database_companion_paths_allow_delivery_without_opening(self):
        database = self.write_goals((THREAD_ID, "active"))
        options = {"min_turn_duration_minutes": 0}
        with patch.object(sqlite3, "connect", side_effect=AssertionError):
            for suffix in ("-journal", "-wal"):
                with self.subTest(suffix=suffix):
                    companion = database.with_name(database.name + suffix)
                    os.mkfifo(companion)
                    try:
                        self.assert_delivered(self.run_notification(options=options))
                    finally:
                        companion.unlink()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes are unavailable")
    def test_unused_shared_memory_pipe_preserves_active_goal_suppression(self):
        database = self.write_goals((THREAD_ID, "active"))
        with contextlib.closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=DELETE").fetchone(), ("delete",))
        self.write_log(completion(60_000))
        os.mkfifo(database.with_name(database.name + "-shm"))
        self.assert_skipped(self.run_notification(), "skipped: active goal")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes are unavailable")
    def test_shared_memory_pipe_with_wal_allows_delivery_without_opening(self):
        for mode in ("wal", "delete"):
            with self.subTest(mode=mode):
                self.codex_home = self.directory / mode
                database = self.write_goals((THREAD_ID, "active"))
                with contextlib.closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(connection.execute(f"PRAGMA journal_mode={mode}").fetchone(), (mode,))
                if mode == "delete":
                    database.with_name(database.name + "-wal").write_bytes(b"existing WAL file")
                os.mkfifo(database.with_name(database.name + "-shm"))
                with patch.object(sqlite3, "connect", side_effect=AssertionError):
                    self.assert_delivered(self.run_notification(options={"min_turn_duration_minutes": 0}))

    def test_sqlite_home_takes_precedence_over_codex_home_and_default(self):
        self.codex_home = self.directory / ".codex"
        self.write_goals((THREAD_ID, "active"))
        self.write_log(completion(60_000))
        custom_home = self.directory / "sqlite #1"
        self.write_goals((THREAD_ID, "paused"), path=custom_home / "goals_1.sqlite")
        with patch.dict(os.environ, {"HOME": str(self.directory)}):
            self.assert_skipped(self.run_notification(codex_home=""), "skipped: active goal")
            self.assert_delivered(self.run_notification(sqlite_home=custom_home))

    def test_zero_minimum_rechecks_goal_after_matching_turn_completion(self):
        sqlite_home = self.directory / "sqlite"
        database = self.write_goals((THREAD_ID, "active"), path=sqlite_home / "goals_1.sqlite")
        log = self.write_log(completion(60_000, turn_id="other-turn"))
        record = completion(0, event_type="turn_complete")
        del record["payload"]["duration_ms"]
        finished = False

        def finish_turn(elapsed):
            nonlocal finished
            if elapsed >= 0.3 and not finished:
                self.write_goals((THREAD_ID, "blocked"), path=database)
                with log.open("a", encoding="utf-8") as writer:
                    writer.write(json.dumps(record) + "\n")
                finished = True

        self.assert_delivered(self.run_notification(
            options={"min_turn_duration_minutes": 0}, on_sleep=finish_turn, sqlite_home=sqlite_home,
        ))
        self.assertTrue(finished)
        self.assertAlmostEqual(self.elapsed, 0.3)

    def test_zero_minimum_finds_log_archived_during_lookup(self):
        self.write_goals((THREAD_ID, "active"))
        log = self.write_log()
        archived_log = self.codex_home / "archived_sessions" / log.name
        archived_log.parent.mkdir()
        find_paths = session_logs._session_paths

        def archive_after_discovery(*args):
            paths = find_paths(*args)
            if log in paths:
                log.rename(archived_log)
            return paths

        def finish_turn(_):
            self.write_goals((THREAD_ID, "blocked"))
            with archived_log.open("a", encoding="utf-8") as writer:
                writer.write(json.dumps(completion(60_000)) + "\n")

        with patch.object(session_logs, "_session_paths", side_effect=archive_after_discovery):
            self.assert_delivered(self.run_notification(
                options={"min_turn_duration_minutes": 0}, on_sleep=finish_turn,
            ))
        self.assertAlmostEqual(self.elapsed, session_logs.POLL_INTERVAL_SECONDS)

    def test_zero_minimum_still_suppresses_active_goal_after_wait_limit(self):
        self.write_goals((THREAD_ID, "active"))
        self.write_log(completion(60_000, turn_id="other-turn"))
        self.assert_skipped(self.run_notification(options={"min_turn_duration_minutes": 0}),
                            "skipped: active goal")
        self.assertAlmostEqual(self.elapsed, session_logs.COMPLETION_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
