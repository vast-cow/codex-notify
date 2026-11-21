"""Notification formatting, filtering, and delivery for Telegram and Slack."""

import asyncio
import json
import logging
import math
import os
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
MIN_TURN_DURATION_KEY = "min_turn_duration_minutes"
WAITING_LABELS = {
    "waitingOnUserInput": "Waiting for user input",
    "waitingOnApproval": "Waiting for approval or MCP input",
}
log = logging.getLogger("notify")


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


def excluded_paths(options):
    paths = options.get("excluded_project_paths", [])
    if not isinstance(paths, list) or any(
        not isinstance(path, str) or not path.strip() for path in paths
    ):
        raise ValueError(
            "notification.excluded_project_paths must be an array of non-empty path strings"
        )

    return {normalize_path(path) for path in paths}


def normalize_path(path):
    return os.path.abspath(os.path.expanduser(path))


def min_turn_duration(options):
    minutes = options.get(MIN_TURN_DURATION_KEY, 1)
    if (
        type(minutes) not in (int, float, Decimal)
        or (type(minutes) is float and not math.isfinite(minutes))
        or (type(minutes) is Decimal and not minutes.is_finite())
        or minutes < 0
    ):
        raise ValueError("notification.min_turn_duration_minutes must be a finite non-negative number")
    return minutes


def is_turn_duration_filtered(duration_ms, minutes, *, log_context=None):
    if minutes == 0:
        return False
    if type(duration_ms) is not int or duration_ms < 0:
        log.info("Skipped turn with unknown duration", extra=log_context)
        return True

    # One millisecond meets any positive threshold below 0.00001 minutes.
    # The bounds avoid constructing fractions for extreme exponents.
    if duration_ms == 0 or minutes > duration_ms or (
        minutes >= Decimal("0.00001") and duration_ms < Fraction(str(minutes)) * 60_000
    ):
        log.info("Skipped turn with duration %s ms below the %s-minute minimum", duration_ms, minutes,
                 extra=log_context)
        return True
    return False


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


def project_lines(cwd, options):
    if options.get("include_project", True) and cwd:
        return [f"\n📁 Project: {Path(cwd).name}", f"📂 Path: {cwd}"]
    return []


def thread_link_lines(thread_id, options):
    template = options.get("thread_url_template", "")
    if (
        isinstance(template, str)
        and template.strip()
        and isinstance(thread_id, str)
        and thread_id.strip()
    ):
        url = template.replace("{thread_id}", thread_id)
        return [f"\n🔗 Thread: {url}"]
    return []


def format_message(event, config):
    options = config.get("notification", {})

    title = options.get("title", "Codex CLI")
    max_length = options.get("max_output_length", 140)

    def body_text(text):
        if options.get("truncate_enabled", True):
            return truncate(text, max_length)
        return str(text or "")

    lines = [f"✅ {title} - Task done"]
    lines.extend(project_lines(event.get("cwd"), options))

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

    lines.extend(thread_link_lines(event.get("thread-id"), options))
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


def event_from_summary(thread_id, cwd, turn):
    inputs = []
    output = ""
    for item in turn["items"]:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "userMessage" and isinstance(item.get("content"), list):
            text = "".join(
                part["text"] for part in item["content"]
                if isinstance(part, dict) and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            )
            if text:
                inputs.append(text)
        elif (
            item.get("type") == "agentMessage" and item.get("phase") != "commentary"
            and isinstance(item.get("text"), str) and item["text"]
        ):
            output = item["text"]
    return {
        "thread-id": thread_id,
        "cwd": cwd,
        "input-messages": inputs,
        "last-assistant-message": output,
    }


class Notifier:
    def __init__(self, config):
        if not isinstance(config, dict):
            raise ValueError("config.json must contain an object")
        for section in ("notification", "telegram", "slack"):
            if not isinstance(config.get(section, {}), dict):
                raise ValueError(f"{section} must be an object")
        options = config.get("notification", {})
        self.config = config
        self.excluded_paths = excluded_paths(options)
        self.min_turn_duration = min_turn_duration(options)
        self.skip_active_goals = options.get("skip_active_goals", True)
        if not isinstance(self.skip_active_goals, bool):
            raise ValueError("notification.skip_active_goals must be a boolean")
        self.timeout = float(options.get("timeout_seconds", 10))
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("notification.timeout_seconds must be finite and positive")
        if options.get("truncate_enabled", True):
            int(options.get("max_output_length", 140))
        self.deliveries = set()

    def is_project_excluded(self, cwd):
        return bool(cwd and normalize_path(cwd) in self.excluded_paths)

    def notify(self, thread_id, cwd, turn):
        message = format_message(event_from_summary(thread_id, cwd, turn), self.config)
        self.send(message, notification_type="completion", thread_id=thread_id, turn_id=turn.get("id"))

    def notify_action_required(self, thread_id, cwd, flags):
        options = self.config.get("notification", {})
        title = options.get("title", "Codex CLI")
        lines = [f"🔔 {title} - Action required", ""]
        lines.extend(label for flag, label in WAITING_LABELS.items() if flag in flags)
        lines.extend(project_lines(cwd, options))
        lines.extend(thread_link_lines(thread_id, options))
        self.send("\n".join(lines), notification_type="action_required", thread_id=thread_id)

    def send(self, message, *, notification_type, thread_id, turn_id=None):
        context = {"thread_id": thread_id, "turn_id": turn_id, "notification_type": notification_type}
        for name, sender in (("Telegram", send_telegram), ("Slack", send_slack)):
            target = self.config.get(name.lower(), {})
            if target.get("enabled", False):
                task = asyncio.create_task(self.deliver(name, sender, target, message, context))
                self.deliveries.add(task)
                task.add_done_callback(self.deliveries.discard)

    async def deliver(self, name, sender, target, message, context):
        try:
            await asyncio.get_running_loop().run_in_executor(None, sender, target, message, self.timeout)
        except Exception as exc:
            log.error("%s delivery failed (%s)", name, type(exc).__name__, extra=context)
        else:
            log.info("%s notification sent", name, extra=context)

    async def drain(self):
        await asyncio.gather(*self.deliveries)
