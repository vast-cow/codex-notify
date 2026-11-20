"""Read turn completion timings from local Codex session logs."""

import json
import os
import time
from contextlib import ExitStack
from pathlib import Path


LOG_TIMEOUT_SECONDS = 1
COMPLETION_TIMEOUT_SECONDS = 30
POLL_INTERVAL_SECONDS = 0.1
THREAD_ID_OFFSET = len("rollout-YYYY-MM-DDTHH-MM-SS-")


class DurationUnavailable(Exception):
    """The matching turn has no readable duration."""


def _raise_walk_error(error):
    if not isinstance(error, FileNotFoundError):
        raise error


def _session_paths(codex_home, thread_id, deadline):
    paths = []
    for subdirectory in ("sessions", "archived_sessions"):
        for root, _, files in os.walk(codex_home / subdirectory, onerror=_raise_walk_error):
            if time.monotonic() >= deadline:
                return paths
            for name in files:
                if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                    continue
                # Reverted threads append a distinct rollout ID after the thread ID.
                file_thread_id = name[THREAD_ID_OFFSET:-len(".jsonl")].partition("_")[0]
                if file_thread_id == thread_id:
                    path = Path(root) / name
                    if path.is_file():
                        paths.append(path)
    return sorted(paths, key=lambda path: path.name, reverse=True)


def _wait(deadline):
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))


def _read_completion(log, thread_id, turn_id, deadline):
    while time.monotonic() < deadline:
        offset = log.tell()
        line = log.readline()
        if not line:
            return None
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if not line.endswith(b"\n"):
                log.seek(offset)
                return None
            continue

        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            continue
        payload = record["payload"]
        if offset == 0 and record.get("type") == "session_meta" and payload.get("id") != thread_id:
            log.close()
            return None
        if (
            record.get("type") == "event_msg"
            and payload.get("type") in ("task_complete", "turn_complete")
            and payload.get("turn_id") == turn_id
        ):
            return payload
    return None


def turn_duration_ms(event):
    thread_id, turn_id = event.get("thread-id"), event.get("turn-id")
    if not all(isinstance(value, str) and value.strip() for value in (thread_id, turn_id)):
        raise DurationUnavailable("missing thread or turn ID")

    try:
        codex_home = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
        deadline = time.monotonic() + LOG_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            paths = _session_paths(codex_home, thread_id, deadline)
            if paths:
                break
            _wait(deadline)
        else:
            raise DurationUnavailable(f"session log not found within {LOG_TIMEOUT_SECONDS:g} second")

        deadline = time.monotonic() + COMPLETION_TIMEOUT_SECONDS
        with ExitStack() as stack:
            logs = [stack.enter_context(path.open("rb")) for path in paths]
            while time.monotonic() < deadline:
                for log in logs:
                    if log.closed:
                        continue
                    completion = _read_completion(log, thread_id, turn_id, deadline)
                    if completion is not None:
                        duration_ms = completion.get("duration_ms")
                        if type(duration_ms) is not int or duration_ms < 0:
                            raise DurationUnavailable("completed record has no valid duration_ms")
                        return duration_ms
                if all(log.closed for log in logs):
                    raise DurationUnavailable("session log belongs to a different thread")
                _wait(deadline)
    except (OSError, ValueError, RuntimeError) as error:
        raise DurationUnavailable("could not read the session log") from error

    raise DurationUnavailable(f"completed record not found within {COMPLETION_TIMEOUT_SECONDS:g} seconds")
