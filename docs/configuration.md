# Configuration

[Back to README](../README.md)

Copy `config.example.json` to `config.json` in the repository root and edit it with your credentials and channel settings. Keep `config.json` alongside `main.py` in your local checkout:

```json
{
  "telegram": {
    "enabled": true,
    "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
    "chat_id": "YOUR_CHAT_ID"
  },
  "slack": {
    "enabled": true,
    "webhook_url": "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
  },
  "notification": {
    "title": "Codex CLI",
    "include_project": true,
    "include_input": true,
    "include_output": true,
    "truncate_enabled": true,
    "max_output_length": 140,
    "thread_url_template": "",
    "timeout_seconds": 10
  }
}
```

## Configuration Reference

Defaults below apply when a setting is omitted. The example configuration explicitly enables both channels.

| Setting | Description | Default |
|---|---|---|
| `telegram.enabled` | Enable Telegram notifications | `false` |
| `telegram.bot_token` | Telegram Bot API token | Required if enabled |
| `telegram.chat_id` | Destination Telegram chat ID | Required if enabled |
| `slack.enabled` | Enable Slack notifications | `false` |
| `slack.webhook_url` | Slack Incoming Webhook URL | Required if enabled |
| `notification.title` | Notification title | `Codex CLI` |
| `notification.include_project` | Include project name and working directory | `true` |
| `notification.include_input` | Include the user request | `true` |
| `notification.include_output` | Include the final assistant response | `true` |
| `notification.truncate_enabled` | Truncate the Request and Result bodies | `true` |
| `notification.max_output_length` | Maximum Unicode code points per Request or Result body, including any ellipsis | `140` |
| `notification.thread_url_template` | Full thread URL with optional literal `{thread_id}` substitution | `""` (no link) |
| `notification.timeout_seconds` | HTTP request timeout in seconds | `10` |

## Disable a Notification Channel

To use only Telegram:

```json
{
  "telegram": {
    "enabled": true,
    "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
    "chat_id": "YOUR_CHAT_ID"
  },
  "slack": {
    "enabled": false,
    "webhook_url": ""
  }
}
```

To use only Slack, set `telegram.enabled` to `false` and `slack.enabled` to `true`.

## Request and Result Length

By default, the Request and Result bodies are each limited to 140 Unicode code points (Python string characters, not UTF-8 bytes or grapheme clusters). Multiple input messages are joined with newlines before applying the Request limit. Text within the limit is unchanged; longer text ends with `…`, counted within the limit. For example, a limit of `4` turns `abcde` into `abc…`. The existing minimum effective limit is one character.

Set `notification.truncate_enabled` to `false` to send both bodies in full. An explicitly configured `max_output_length` continues to take precedence over the default when truncation is enabled. Titles, labels, project names, paths, and thread URLs are never truncated. Delivery services still impose their own message size limits.

## Thread Links

Set the full URL template, including any scheme, host, port, path, or fragment:

```json
{
  "notification": {
    "thread_url_template": "http://127.0.0.1:5900/#/thread/{thread_id}"
  }
}
```

For event `thread-id` `01a1e7bc-7698-7420-bf3e-34533b790e55`, this adds a separate line with the complete URL:

```text
🔗 Thread: http://127.0.0.1:5900/#/thread/01a1e7bc-7698-7420-bf3e-34533b790e55
```

This hostname is only an example; configure your own reachable thread viewer. Telegram and Slack recognize the standalone URL as a clickable link. Substitution replaces only the literal `{thread_id}` text; other braces are unchanged. The default empty template adds no link, and missing, non-string, or blank thread IDs also omit the link. Request and Result truncation never shortens this URL.
