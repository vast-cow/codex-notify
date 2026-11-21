import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import notifications


THREAD_ID = "01a1e7bc-7698-7420-bf3e-34533b790e55"
TEMPLATE = "http://127.0.0.1:5900/#/thread/{thread_id}"


def event(**updates):
    value = {
        "type": "agent-turn-complete",
        "thread-id": THREAD_ID,
        "cwd": "/projects/example",
        "input-messages": ["request"],
        "last-assistant-message": "result",
    }
    value.update(updates)
    return value


class FormattingTests(unittest.TestCase):
    def bodies(self, message):
        request, result = message.split("\n📝 Request:\n", 1)[1].split(
            "\n\n📋 Result:\n", 1
        )
        return request, result

    def test_defaults_limit_each_body_to_140(self):
        message = notifications.format_message(event(**{
            "input-messages": ["r" * 141],
            "last-assistant-message": "s" * 142,
        }), {})
        self.assertEqual(self.bodies(message), ("r" * 139 + "…", "s" * 139 + "…"))
        self.assertNotIn("🔗", message)
        self.assertIn("Project: example", message)

    def test_truncate_boundaries_and_unicode_code_points(self):
        for text, limit, expected in [
            ("", 3, ""), ("ab", 3, "ab"), ("abc", 3, "abc"),
            ("abcd", 3, "ab…"), ("a", 1, "a"), ("ab", 1, "…"),
            ("日本😀語", 3, "日本…"), ("😀e\u0301", 3, "😀e\u0301"),
            ("😀e\u0301x", 3, "😀e…"),
        ]:
            with self.subTest(text=text, limit=limit):
                self.assertEqual(notifications.truncate(text, limit), expected)
                self.assertLessEqual(len(notifications.truncate(text, limit)), limit)

    def test_explicit_limit_and_joined_inputs(self):
        message = notifications.format_message(event(**{
            "input-messages": ["ab", "cd"], "last-assistant-message": "xy",
        }), {"notification": {"truncate_enabled": True, "max_output_length": 4}})
        self.assertEqual(self.bodies(message), ("ab\n…", "xy"))

    def test_disabled_truncation_keeps_full_bodies(self):
        message = notifications.format_message(event(**{
            "input-messages": ["r" * 200], "last-assistant-message": "s" * 300,
        }), {"notification": {"truncate_enabled": False, "max_output_length": 1}})
        self.assertEqual(self.bodies(message), ("r" * 200, "s" * 300))

    def test_url_and_other_sections_are_never_truncated(self):
        for enabled in (True, False):
            with self.subTest(truncate_enabled=enabled):
                message = notifications.format_message(event(), {"notification": {
                    "truncate_enabled": enabled, "max_output_length": 1,
                    "title": "Long title", "thread_url_template": TEMPLATE,
                }})
                self.assertIn("✅ Long title - Task done", message)
                self.assertIn("📂 Path: /projects/example", message)
                self.assertTrue(message.endswith("\n\n🔗 Thread: " + TEMPLATE.replace(
                    "{thread_id}", THREAD_ID)))

    def test_link_requires_template_and_usable_thread_id(self):
        for template, thread_id in [
            ("", THREAD_ID), ("  ", THREAD_ID), (None, THREAD_ID),
            (TEMPLATE, None), (TEMPLATE, ""), (TEMPLATE, "  "), (TEMPLATE, 123),
        ]:
            with self.subTest(template=template, thread_id=thread_id):
                message = notifications.format_message(event(**{"thread-id": thread_id}), {
                    "notification": {"thread_url_template": template}})
                self.assertNotIn("🔗", message)

    def test_substitution_is_literal(self):
        message = notifications.format_message(event(), {"notification": {
            "thread_url_template": "https://example.test/{other}/{thread_id}/{thread_id}"}})
        expected = "https://example.test/{other}/" + THREAD_ID + "/" + THREAD_ID
        self.assertIn(expected, message)


class FilterTests(unittest.TestCase):
    def test_duration_default_and_custom_boundaries(self):
        for minutes, duration_ms, filtered in (
            (1, 59_999, True), (1, 60_000, False), (1, 60_001, False),
            (1.5, 89_999, True), (1.5, 90_000, False),
            (0, None, False), (1, None, True), (1, -1, True), (1, False, True),
            (1, "60001", True), (1, 60001.0, True), (1e-100, 1, False),
        ):
            with self.subTest(minutes=minutes, duration_ms=duration_ms):
                self.assertEqual(notifications.is_turn_duration_filtered(duration_ms, minutes), filtered)
        self.assertEqual(notifications.Notifier({}).min_turn_duration, 1)

    def test_invalid_duration_thresholds(self):
        for value in ("1", True, -1, None, float("inf"), float("nan"), {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "min_turn_duration_minutes"):
                notifications.Notifier({"notification": {"min_turn_duration_minutes": value}})

    def test_config_keeps_decimal_precision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"notification":{"min_turn_duration_minutes":1.000000000000000001}}')
            with patch.object(notifications, "CONFIG_PATH", path):
                notifier = notifications.Notifier(notifications.load_config())
        self.assertTrue(notifications.is_turn_duration_filtered(60_000, notifier.min_turn_duration))
        self.assertFalse(notifications.is_turn_duration_filtered(60_001, notifier.min_turn_duration))

    def test_path_filter_uses_exact_normalized_matches(self):
        notifier = notifications.Notifier({"notification": {
            "excluded_project_paths": ["~/project//", "/projects/demo/../excluded"],
        }})
        for cwd in ("~/project", str(Path.home() / "project"), "/projects/excluded/"):
            self.assertTrue(notifier.is_project_excluded(cwd))
        for cwd in ("~/project/child", "/projects/demo", None, ""):
            self.assertFalse(notifier.is_project_excluded(cwd))

    def test_invalid_path_filters(self):
        for value in (None, "~/project", [None], [""], [" "], [1]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "excluded_project_paths"):
                notifications.Notifier({"notification": {"excluded_project_paths": value}})

    def test_goal_filter_requires_a_boolean(self):
        with self.assertRaisesRegex(ValueError, "skip_active_goals"):
            notifications.Notifier({"notification": {"skip_active_goals": "true"}})

    def test_invalid_numeric_configuration_names_the_setting_without_echoing_the_value(self):
        for setting in ("timeout_seconds", "max_output_length"):
            with self.subTest(setting=setting):
                with self.assertRaisesRegex(ValueError, rf"notification\.{setting} must be") as raised:
                    notifications.Notifier({"notification": {setting: "private config value"}})
                self.assertNotIn("private config value", str(raised.exception))

    def test_extracts_text_from_summary(self):
        def user(*parts):
            return {"type": "userMessage", "content": list(parts)}

        turn = {"items": [
            user({"type": "text", "text": "first "}, {"type": "image", "url": "hidden"},
                 {"type": "text", "text": "request"}),
            {"type": "agentMessage", "text": "earlier answer", "phase": "final_answer"},
            user({"type": "text", "text": "second request"}),
            {"type": "agentMessage", "phase": None, "text": "final answer"},
            {"type": "agentMessage", "phase": "commentary", "text": "progress"},
            {"type": "reasoning", "summary": ["hidden"]},
        ]}
        event = notifications.event_from_summary(THREAD_ID, "/projects/test", turn)
        self.assertEqual(event["input-messages"], ["first request", "second request"])
        self.assertEqual(event["last-assistant-message"], "final answer")


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_destination_can_fail_without_preventing_the_other(self):
        notifier = notifications.Notifier({
            "telegram": {"enabled": True, "bot_token": "test", "chat_id": "123"},
            "slack": {"enabled": True, "webhook_url": "https://example.test/slack"},
        })
        for failed in ("Telegram", "Slack"):
            def post(url, payload, timeout, failed=failed):
                destination = "Telegram" if "api.telegram.org" in url else "Slack"
                if destination == failed:
                    raise RuntimeError("private destination information")
                return 200, json.dumps({"ok": True}).encode()

            with self.subTest(failed=failed), patch("notifications.post_json", side_effect=post) as send:
                with self.assertLogs("notify", level="INFO") as logs:
                    notifier.notify(THREAD_ID, "/projects/test", {"items": []})
                    await notifier.drain()
                self.assertEqual(send.call_count, 2)
                self.assertEqual(len({call.args[1]["text"] for call in send.call_args_list}), 1)
                self.assertIn(f"{failed} delivery failed", "\n".join(logs.output))
                other = "Slack" if failed == "Telegram" else "Telegram"
                self.assertIn(f"{other} notification sent", "\n".join(logs.output))
                self.assertNotIn("private destination information", "\n".join(logs.output))

    async def test_senders_run_independently_and_preserve_plain_text_url(self):
        notifier = notifications.Notifier({
            "telegram": {"enabled": True, "bot_token": "test", "chat_id": "123"},
            "slack": {"enabled": True, "webhook_url": "https://example.test/slack"},
            "notification": {"thread_url_template": TEMPLATE},
        })
        slack_sent = threading.Event()
        requests = []

        def post(url, payload, timeout):
            requests.append((url, payload))
            if "api.telegram.org" in url and not slack_sent.wait(2):
                raise RuntimeError("Slack blocked behind Telegram")
            if "api.telegram.org" not in url:
                slack_sent.set()
            return 200, b'{"ok":true}'

        with patch("notifications.post_json", side_effect=post), self.assertLogs("notify") as logs:
            notifier.notify(THREAD_ID, None, {"items": []})
            await notifier.drain()

        self.assertEqual(len(requests), 2)
        self.assertTrue(all("🔗 Thread: " + TEMPLATE.replace("{thread_id}", THREAD_ID) in
                            payload["text"] for _, payload in requests))
        self.assertTrue(all("notification sent" in entry for entry in logs.output))
