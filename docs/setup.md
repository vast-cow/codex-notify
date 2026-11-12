# Setup Guide

[Back to README](../README.md)

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

From the repository root, copy the script and example configuration:

```bash
cp main.py ~/.codex/hooks/notify.py
cp config.example.json ~/.codex/hooks/config.json
```

Edit `config.json` with your channel credentials. See the [configuration reference](configuration.md) and [security guidance](security.md).

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

