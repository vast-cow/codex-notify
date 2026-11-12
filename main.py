#!/usr/bin/env python3
"""Codex CLI completion notifier for Telegram and Slack."""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


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
    max_length = options.get("max_output_length", 1000)

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
                + truncate(input_text, max_length)
            )

    if options.get("include_output", True):
        output = event.get("last-assistant-message", "")
        if output:
            lines.append(
                "\n📋 Result:\n"
                + truncate(output, max_length)
            )

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
