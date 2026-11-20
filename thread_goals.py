"""Read the goal status for a notification event's thread."""

import os
from contextlib import closing
from pathlib import Path
from stat import S_ISREG

import session_logs


def _goal_status(thread_id):
    try:
        import sqlite3
    except ImportError:
        return None

    try:
        sqlite_home = Path(
            os.environ.get("CODEX_SQLITE_HOME") or os.environ.get("CODEX_HOME") or "~/.codex"
        ).expanduser()
        try:
            path = (sqlite_home / "goals_1.sqlite").resolve(strict=True)
        except FileNotFoundError:
            path = (sqlite_home / "state_5.sqlite").resolve(strict=True)

        if not path.is_file():
            return None
        # SQLite opens companion files itself, and opening a named pipe can block.
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                mode = path.with_name(path.name + suffix).stat().st_mode
            except FileNotFoundError:
                continue
            if not S_ISREG(mode):
                return None

        uri = f"{path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0)) as connection:
            row = connection.execute(
                "SELECT status FROM thread_goals WHERE thread_id = ?", (thread_id,),
            ).fetchone()
        return row[0] if row else None
    except (sqlite3.Error, OSError, ValueError, RuntimeError):
        return None


def is_active(event, *, wait_for_completion):
    thread_id = event.get("thread-id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        return False
    if _goal_status(thread_id) != "active":
        return False
    if not wait_for_completion:
        return True

    # The hook can run before Codex finalizes goal status and records turn completion.
    try:
        session_logs.turn_completion(event)
    except session_logs.DurationUnavailable:
        pass
    return _goal_status(thread_id) == "active"
