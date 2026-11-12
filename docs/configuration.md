# Configuration

[Back to README](../README.md)

Create `~/.codex/hooks/config.json`:

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
    "max_output_length": 1000,
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
| `notification.max_output_length` | Maximum characters for each input/output section | `1000` |
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

