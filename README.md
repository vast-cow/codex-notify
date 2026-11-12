# Codex CLI Completion Notifier

A lightweight Python notification hook for [OpenAI Codex CLI](https://github.com/openai/codex) that sends task completion notifications to **Telegram** and **Slack**.

The notifier uses Python's standard library, requires no third-party dependencies, and stores all notification settings in `config.json`.

## Features

- Send notifications to Telegram using the Bot API
- Send notifications to Slack using Incoming Webhooks
- Enable or disable each notification channel independently
- Include the project name, working directory, user request, and Codex response
- Configure message truncation and HTTP timeouts
- Handle notification failures independently
- Ignore events other than `agent-turn-complete`
- No external Python packages required

## Requirements

- Python 3.8 or later
- OpenAI Codex CLI with `notify` hook support
- A Telegram Bot Token and Chat ID (for Telegram notifications)
- A Slack Incoming Webhook URL (for Slack notifications)

## Installation

### 1. Create the hook directory

```bash
mkdir -p ~/.codex/hooks
```

### 2. Add the files

Create the following directory structure:

```text
~/.codex/
├── config.toml
└── hooks/
    ├── notify.py
    └── config.json
```

Place the Python notification script in `notify.py` and the notification configuration in `config.json`.

### 3. Configure Codex CLI

Edit `~/.codex/config.toml` and add:

```toml
notify = ["python3", "/home/YOUR_USER/.codex/hooks/notify.py"]
```

Replace `/home/YOUR_USER` with your actual home directory.

For macOS:

```toml
notify = ["python3", "/Users/YOUR_USER/.codex/hooks/notify.py"]
```

**Important:** The `notify` setting must be defined at the top level of `config.toml`, not inside another TOML section.

## Configuration

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

### Configuration Reference

| Setting | Description | Default |
|---|---|---|
| `telegram.enabled` | Enable Telegram notifications | `true` |
| `telegram.bot_token` | Telegram Bot API token | Required if enabled |
| `telegram.chat_id` | Destination Telegram chat ID | Required if enabled |
| `slack.enabled` | Enable Slack notifications | `true` |
| `slack.webhook_url` | Slack Incoming Webhook URL | Required if enabled |
| `notification.title` | Notification title | `Codex CLI` |
| `notification.include_project` | Include project name and working directory | `true` |
| `notification.include_input` | Include the user request | `true` |
| `notification.include_output` | Include the final assistant response | `true` |
| `notification.max_output_length` | Maximum characters for each input/output section | `1000` |
| `notification.timeout_seconds` | HTTP request timeout in seconds | `10` |

### Disable a Notification Channel

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

## Telegram Setup

1. Open Telegram and search for `@BotFather`.
2. Send `/newbot` and follow the instructions.
3. Copy the generated Bot Token.
4. Open your newly created bot and send it a message.
5. Retrieve your Chat ID by visiting:

   ```text
   https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
   ```

6. Find the `chat.id` value in the JSON response.
7. Add the Bot Token and Chat ID to `config.json`.

For group notifications, add the bot to the group and obtain the group's Chat ID.

## Slack Setup

1. Visit the [Slack Incoming Webhooks documentation](https://api.slack.com/messaging/webhooks).
2. Create a Slack App or select an existing one.
3. Enable **Incoming Webhooks**.
4. Click **Add New Webhook to Workspace**.
5. Select the destination channel.
6. Copy the generated Webhook URL.
7. Add the URL to `config.json`.

## Testing

You can test notifications without running Codex CLI.

Execute:

```bash
python3 ~/.codex/hooks/notify.py '{
  "type": "agent-turn-complete",
  "thread-id": "test-session",
  "turn-id": "test-turn",
  "cwd": "/home/user/projects/my-app",
  "input-messages": [
    "Implement the REST API and run all tests."
  ],
  "last-assistant-message": "API implementation completed. All tests passed."
}'
```

Expected console output:

```text
[notify] Telegram: processed
[notify] Slack: processed
```

Both enabled channels should receive a notification.

### Example Notification

```text
✅ Codex CLI - Task Completed

📁 Project: my-app
📂 Path: /home/user/projects/my-app

📝 Request:
Implement the REST API and run all tests.

📋 Result:
API implementation completed. All tests passed.
```

**Note:** The provided `notify.py` uses Japanese text for the completion heading and section labels. The example above is an English translation. To produce English notifications, update the corresponding strings in `format_message()`.

## How It Works

Codex CLI executes the configured `notify` command when an `agent-turn-complete` event occurs.

The event payload is passed to the Python script as a JSON command-line argument.

Example event:

```json
{
  "type": "agent-turn-complete",
  "thread-id": "session-id",
  "turn-id": "turn-id",
  "cwd": "/path/to/project",
  "input-messages": [
    "Implement a new feature"
  ],
  "last-assistant-message": "The feature has been implemented."
}
```

The notifier processes the event as follows:

1. Parse the JSON event received from Codex CLI.
2. Verify that the event type is `agent-turn-complete`.
3. Load notification settings from `config.json`.
4. Format the completion message.
5. Send the message to Telegram if enabled.
6. Send the message to Slack if enabled.
7. Report notification errors to standard error.

Telegram and Slack are handled independently. A failure in one channel does not prevent the other from being attempted.

## Event Behavior

The `agent-turn-complete` event indicates that a Codex agent turn has finished.

**It does not guarantee that the task completed successfully.**

For example, notifications may still be sent when:

- Tests fail
- Codex cannot complete a requested task
- The agent reports an implementation error
- The agent finishes a turn without completing the overall task

Additionally, a single Codex session may generate multiple notifications because each completed agent turn can trigger the hook.

## Troubleshooting

### Telegram notifications are not delivered

Verify that:

- The Bot Token is correct.
- The Chat ID is correct.
- You have started a conversation with the bot.
- The bot has permission to send messages to the destination chat.
- `telegram.enabled` is set to `true`.

### Slack notifications are not delivered

Verify that:

- The Webhook URL is correct.
- Incoming Webhooks are enabled for the Slack App.
- The webhook is associated with the intended channel.
- `slack.enabled` is set to `true`.

### Codex does not trigger notifications

Verify that:

- `notify` is configured at the top level of `~/.codex/config.toml`.
- The Python script path is absolute and correct.
- Python 3 is installed and available.
- `config.json` exists next to `notify.py`.
- The Codex CLI version supports the `notify` configuration.
- The current Codex agent turn has completed.

You can also manually run the test command to distinguish hook configuration problems from notification delivery problems.

## Security

`config.json` contains sensitive credentials, including Telegram Bot Tokens and Slack Webhook URLs.

Restrict access to the configuration file:

```bash
chmod 700 ~/.codex/hooks
chmod 600 ~/.codex/hooks/config.json
```

If the files are stored in a Git repository, add the following to `.gitignore`:

```gitignore
config.json
__pycache__/
*.pyc
```

Avoid committing actual credentials to version control.

**Privacy consideration:** User prompts and Codex responses may contain source code, credentials, private project details, or other sensitive information.

To prevent prompt and response contents from being sent to external services, set:

```json
{
  "notification": {
    "include_input": false,
    "include_output": false
  }
}
```

These values can be merged into your existing `notification` configuration.

## Dependencies

This project uses only Python standard-library modules:

- `json`
- `sys`
- `pathlib`
- `urllib.request`
- `urllib.error`

No `requirements.txt` or `pip install` is necessary.

## Limitations

- Notifications are sent synchronously.
- There is no automatic retry mechanism.
- Only `agent-turn-complete` events are processed.
- Task success and failure are not determined automatically.
- Notification text may be truncated according to configuration.
- Telegram imposes message length limits; use a suitable `max_output_length` to avoid oversized messages.
- The script reports HTTP failures without exposing notification credentials.

## License

No license has been specified for this project.
