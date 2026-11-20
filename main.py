#!/usr/bin/env python3
"""Codex CLI completion notifier for Telegram and Slack."""

import json
import math
import os
import sys
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import partial
from pathlib import Path
from uuid import UUID

import app_server
import session_logs


CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
MIN_TURN_DURATION_KEY = "min_turn_duration_minutes"


def _decode_config_object(options):
    # Keep the threshold's decimal precision and preserve float behavior for other settings.
    for key, value in options.items():
        if key != MIN_TURN_DURATION_KEY and isinstance(value, Decimal):
            options[key] = float(value)
    return options


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        try:
            return json.load(f, parse_float=Decimal, object_hook=_decode_config_object)
        except InvalidOperation as error:
            raise ValueError("invalid number in config.json") from error


def is_project_excluded(event, config):
    paths = config.get("notification", {}).get("excluded_project_paths", [])
    if not isinstance(paths, list) or any(
        not isinstance(path, str) or not path.strip() for path in paths
    ):
        raise ValueError(
            "notification.excluded_project_paths must be an array of non-empty path strings"
        )

    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return False

    def normalize(path):
        return os.path.abspath(os.path.expanduser(path))

    project_path = normalize(cwd)
    return any(project_path == normalize(path) for path in paths)


def is_turn_duration_filtered(event, config):
    minutes = config.get("notification", {}).get(MIN_TURN_DURATION_KEY, 1)
    if (
        type(minutes) not in (int, float, Decimal)
        or (type(minutes) is float and not math.isfinite(minutes))
        or (type(minutes) is Decimal and not minutes.is_finite())
        or minutes < 0
    ):
        raise ValueError("notification.min_turn_duration_minutes must be a finite non-negative number")
    if minutes == 0:
        return False

    try:
        duration_ms = session_logs.turn_duration_ms(event)
    except session_logs.DurationUnavailable as error:
        print(f"[notify] skipped: turn duration is unknown ({error})")
        return True

    # One millisecond meets any positive threshold below 0.00001 minutes.
    # The bounds avoid constructing fractions for extreme exponents.
    if duration_ms == 0 or minutes > duration_ms or (
        minutes >= Decimal("0.00001") and duration_ms < Fraction(str(minutes)) * 60_000
    ):
        print(f"[notify] skipped: turn duration {duration_ms} ms is below the {minutes}-minute minimum")
        return True
    return False


def _wait_for_turn_completion(event):
    # The hook can run before Codex finalizes the goal and records the turn completion.
    try:
        session_logs.turn_completion(event)
    except session_logs.DurationUnavailable:
        pass


def app_server_exit_status(event, config, *, skip_active_goals):
    """Return an exit status to suppress delivery, or None to continue."""
    settings = app_server.Settings.from_config(config.get("app_server", {}))
    if not settings.enabled or not (
        settings.skip_ephemeral or settings.skip_archived or skip_active_goals
    ):
        return None

    wait_for_turn = None
    if skip_active_goals and config.get("notification", {}).get(MIN_TURN_DURATION_KEY) == 0:
        wait_for_turn = partial(_wait_for_turn_completion, event)
    metadata = app_server.lookup_thread(
        settings, event, check_goal=skip_active_goals, wait_for_turn=wait_for_turn,
    )
    required = {}
    if settings.skip_ephemeral:
        required["ephemeral"] = metadata.ephemeral
    if settings.skip_archived:
        required["archived"] = metadata.archived

    for name, state in required.items():
        if state is True:
            # Only log thread IDs in Codex's UUID format.
            thread_id = event.get("thread-id")
            suffix = ""
            if isinstance(thread_id, str):
                try:
                    suffix = f" (thread-id={UUID(thread_id)})"
                except ValueError:
                    pass
            print(f"[notify] skipped: {name} thread{suffix}")
            return 0

    if skip_active_goals:
        if metadata.active_goal is True:
            print("[notify] skipped: active goal")
            return 0
        required["goal"] = metadata.active_goal

    unknown = [name for name, state in required.items() if state is None]
    if not unknown:
        print("[notify] app-server check passed; proceeding with notification")
        return None

    for reason in dict.fromkeys(metadata.failures):
        print(f"[notify] app-server lookup failed: {reason}", file=sys.stderr)
    for name in unknown:
        print(f"[notify] app-server lookup unknown: {name} state unavailable", file=sys.stderr)
    if settings.on_lookup_failure == "notify":
        print("[notify] app-server lookup inconclusive; proceeding with notification", file=sys.stderr)
        return None
    if settings.on_lookup_failure == "skip":
        print("[notify] skipped: app-server lookup inconclusive (on_lookup_failure=skip)", file=sys.stderr)
        return 0
    print("[notify] skipped: app-server lookup inconclusive (on_lookup_failure=error)", file=sys.stderr)
    return 1


def post_json(url, payload, timeout=10):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8"
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read()
            return response.status, response_body

    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from e

    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


def truncate(text, max_length):
    text = str(text or "")
    max_length = max(1, int(max_length))

    if len(text) <= max_length:
        return text

    return text[: max_length - 1] + "…"


def format_message(event, config):
    options = config.get("notification", {})

    title = options.get("title", "Codex CLI")
    max_length = options.get("max_output_length", 140)

    def body_text(text):
        if options.get("truncate_enabled", True):
            return truncate(text, max_length)
        return str(text or "")

    lines = [f"✅ {title} - Task done"]

    if options.get("include_project", True):
        cwd = event.get("cwd", "")
        if cwd:
            lines.append(f"\n📁 Project: {Path(cwd).name}")
            lines.append(f"📂 Path: {cwd}")

    if options.get("include_input", True):
        inputs = event.get("input-messages", [])
        if isinstance(inputs, list) and inputs:
            input_text = "\n".join(str(item) for item in inputs)
            lines.append(
                "\n📝 Request:\n"
                + body_text(input_text)
            )

    if options.get("include_output", True):
        output = event.get("last-assistant-message", "")
        if output:
            lines.append(
                "\n📋 Result:\n"
                + body_text(output)
            )

    template = options.get("thread_url_template", "")
    thread_id = event.get("thread-id")
    if (
        isinstance(template, str)
        and template.strip()
        and isinstance(thread_id, str)
        and thread_id.strip()
    ):
        url = template.replace("{thread_id}", thread_id)
        lines.append(f"\n🔗 Thread: {url}")

    return "\n".join(lines)


def send_telegram(config, message, timeout):
    if not config.get("enabled", False):
        return

    token = config.get("bot_token", "")
    chat_id = config.get("chat_id", "")

    if not token or not chat_id:
        raise ValueError("Telegram bot_token or chat_id is missing")

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    status, body = post_json(
        url,
        {
            "chat_id": chat_id,
            "text": message,
        },
        timeout=timeout,
    )

    result = json.loads(body)

    if status != 200 or not result.get("ok"):
        raise RuntimeError("Telegram API returned an error")


def send_slack(config, message, timeout):
    if not config.get("enabled", False):
        return

    webhook_url = config.get("webhook_url", "")

    if not webhook_url:
        raise ValueError("Slack webhook_url is missing")

    status, _ = post_json(
        webhook_url,
        {"text": message},
        timeout=timeout,
    )

    if status != 200:
        raise RuntimeError(f"Slack returned HTTP {status}")


def main():
    if len(sys.argv) != 2:
        print(
            "Usage: notify.py '<CODEX_EVENT_JSON>'",
            file=sys.stderr,
        )
        return 1

    try:
        event = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(f"Invalid event JSON: {e}", file=sys.stderr)
        return 1

    if not isinstance(event, dict):
        print("Event must be a JSON object", file=sys.stderr)
        return 1

    if event.get("type") != "agent-turn-complete":
        return 0

    try:
        config = load_config()
        options = config.get("notification", {})
        skip_active_goals = options.get("skip_active_goals", True)
        if not isinstance(skip_active_goals, bool):
            raise ValueError("notification.skip_active_goals must be a boolean")
        if is_project_excluded(event, config):
            return 0
        if is_turn_duration_filtered(event, config):
            return 0
        status = app_server_exit_status(event, config, skip_active_goals=skip_active_goals)
        if status is not None:
            return status
        message = format_message(event, config)
        timeout = float(
            config.get("notification", {}).get(
                "timeout_seconds", 10
            )
        )
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")

    except (OSError, ValueError, TypeError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    failed = False

    targets = [
        ("Telegram", send_telegram),
        ("Slack", send_slack),
    ]

    for name, sender in targets:
        try:
            sender(
                config.get(name.lower(), {}),
                message,
                timeout,
            )
            print(f"[notify] {name}: processed")

        except Exception as e:
            print(
                f"[notify] {name}: failed ({type(e).__name__})",
                file=sys.stderr,
            )
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
