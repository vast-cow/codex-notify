import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

import main


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
        message = main.format_message(event(**{
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
                self.assertEqual(main.truncate(text, limit), expected)
                self.assertLessEqual(len(main.truncate(text, limit)), limit)

    def test_explicit_limit_and_joined_inputs(self):
        message = main.format_message(event(**{
            "input-messages": ["ab", "cd"], "last-assistant-message": "xy",
        }), {"notification": {"truncate_enabled": True, "max_output_length": 4}})
        self.assertEqual(self.bodies(message), ("ab\n…", "xy"))

    def test_disabled_truncation_keeps_full_bodies(self):
        message = main.format_message(event(**{
            "input-messages": ["r" * 200], "last-assistant-message": "s" * 300,
        }), {"notification": {"truncate_enabled": False, "max_output_length": 1}})
        self.assertEqual(self.bodies(message), ("r" * 200, "s" * 300))

    def test_url_and_other_sections_are_never_truncated(self):
        for enabled in (True, False):
            with self.subTest(truncate_enabled=enabled):
                message = main.format_message(event(), {"notification": {
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
                message = main.format_message(event(**{"thread-id": thread_id}), {
                    "notification": {"thread_url_template": template}})
                self.assertNotIn("🔗", message)

    def test_substitution_is_literal(self):
        message = main.format_message(event(), {"notification": {
            "thread_url_template": "https://example.test/{other}/{thread_id}/{thread_id}"}})
        expected = "https://example.test/{other}/" + THREAD_ID + "/" + THREAD_ID
        self.assertIn(expected, message)


class NotificationFlowTests(unittest.TestCase):
    def run_notification(self, payload, options=None):
        config = {
            "telegram": {"enabled": True}, "slack": {"enabled": True},
            "notification": options or {},
        }
        with patch.object(main, "load_config", return_value=config), \
                patch.object(main.sys, "argv", ["main.py", json.dumps(payload)]), \
                patch.object(main, "send_telegram") as telegram, \
                patch.object(main, "send_slack") as slack, \
                contextlib.redirect_stdout(io.StringIO()):
            status = main.main()
        self.assertEqual(status, 0)
        return telegram, slack

    def test_completion_delivers_to_both_channels(self):
        telegram, slack = self.run_notification(event())
        telegram.assert_called_once_with({"enabled": True}, main.format_message(event(), {}), 10)
        slack.assert_called_once_with({"enabled": True}, main.format_message(event(), {}), 10)

    def test_completion_without_thread_id_still_delivers(self):
        payload = event()
        del payload["thread-id"]
        telegram, slack = self.run_notification(payload, {"thread_url_template": TEMPLATE})
        telegram.assert_called_once()
        slack.assert_called_once()
        self.assertNotIn("🔗", telegram.call_args.args[1])

    def test_other_event_types_do_not_notify(self):
        telegram, slack = self.run_notification(event(type="other-event"))
        telegram.assert_not_called()
        slack.assert_not_called()

    def test_both_channels_receive_same_formatted_message(self):
        telegram, slack = self.run_notification(event(**{
            "input-messages": ["r" * 200], "last-assistant-message": "s" * 200,
        }), {"thread_url_template": TEMPLATE})
        self.assertEqual(telegram.call_args.args[1], slack.call_args.args[1])
        self.assertIn("r" * 139 + "…", telegram.call_args.args[1])
        self.assertIn(TEMPLATE.replace("{thread_id}", THREAD_ID), slack.call_args.args[1])

    def test_excluded_paths_skip_formatting_and_both_channels(self):
        for cwd, paths in [
            ("/work/app", ["/work/app"]),
            ("/work/app/", ["/work/app"]),
            ("/work/app", ["/work/app/"]),
            ("/work/app///", ["/work/./app/"]),
            ("/work/other/../app", ["/work/app"]),
            ("/work/app", ["/work/other", "/work/app"]),
            ("/", ["/"]),
        ]:
            for include_project in (True, False):
                with self.subTest(cwd=cwd, paths=paths, include_project=include_project), \
                        patch.object(main, "format_message") as formatter:
                    telegram, slack = self.run_notification(event(cwd=cwd), {
                        "excluded_project_paths": paths,
                        "include_project": include_project,
                    })
                    formatter.assert_not_called()
                    telegram.assert_not_called()
                    slack.assert_not_called()

    def test_nonmatching_paths_deliver_to_both_channels(self):
        for cwd in ("/work/app-other", "/work/app/subdir", "/other/app", "/work/App"):
            for include_project in (True, False):
                with self.subTest(cwd=cwd, include_project=include_project):
                    telegram, slack = self.run_notification(event(cwd=cwd), {
                        "excluded_project_paths": ["/work/app"],
                        "include_project": include_project,
                    })
                    telegram.assert_called_once()
                    slack.assert_called_once()

    def test_omitted_or_empty_exclusions_deliver(self):
        for options in ({}, {"excluded_project_paths": []}):
            with self.subTest(options=options):
                telegram, slack = self.run_notification(event(), options)
                telegram.assert_called_once()
                slack.assert_called_once()

    def test_home_directory_expands_on_both_sides(self):
        with patch.dict(os.environ, {"HOME": "/home/test-user"}):
            for cwd, path in [
                ("/home/test-user/projects/app/", "~/projects/app"),
                ("~/projects/app", "/home/test-user/projects/app/"),
                ("~/projects/app/", "~/projects/app"),
            ]:
                with self.subTest(cwd=cwd, path=path):
                    telegram, slack = self.run_notification(event(cwd=cwd), {
                        "excluded_project_paths": [path],
                    })
                    telegram.assert_not_called()
                    slack.assert_not_called()

    def test_missing_or_empty_cwd_is_not_excluded(self):
        missing = event()
        del missing["cwd"]
        for payload in (missing, event(cwd=""), event(cwd=None)):
            with self.subTest(payload=payload):
                telegram, slack = self.run_notification(payload, {
                    "excluded_project_paths": [os.getcwd()],
                })
                telegram.assert_called_once()
                slack.assert_called_once()

    def test_invalid_exclusions_are_configuration_errors(self):
        for paths in (None, "", "/work/app", {}, 123, True,
                      [""], ["  "], [None], [123], [True], [[]],
                      ["/work/app", ""]):
            for cwd in ("/work/app", ""):
                with self.subTest(paths=paths, cwd=cwd), \
                        patch.object(main, "load_config", return_value={
                            "notification": {"excluded_project_paths": paths},
                        }), \
                        patch.object(main.sys, "argv", [
                            "main.py", json.dumps(event(cwd=cwd)),
                        ]), \
                        patch.object(main, "format_message") as formatter, \
                        patch.object(main, "send_telegram") as telegram, \
                        patch.object(main, "send_slack") as slack, \
                        contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(main.main(), 1)
                    self.assertIn("Configuration error:", stderr.getvalue())
                    self.assertIn("notification.excluded_project_paths", stderr.getvalue())
                    formatter.assert_not_called()
                    telegram.assert_not_called()
                    slack.assert_not_called()

    def test_senders_preserve_plain_text_url(self):
        message = main.format_message(event(), {"notification": {
            "thread_url_template": TEMPLATE}})
        with patch.object(main, "post_json", return_value=(200, b'{"ok":true}')) as post:
            main.send_telegram({"enabled": True, "bot_token": "fake", "chat_id": "fake"}, message, 10)
            self.assertEqual(post.call_args.args[1]["text"], message)
            main.send_slack({"enabled": True, "webhook_url": "https://example.test"}, message, 10)
            self.assertEqual(post.call_args.args[1]["text"], message)


if __name__ == "__main__":
    unittest.main()
