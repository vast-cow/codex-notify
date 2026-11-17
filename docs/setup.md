# Setup Guide

[Back to README](../README.md)

## Installation

### 1. Clone the repository

Clone the repository into any directory you choose. The following example uses `~/projects/codex-notify`; this installation directory is optional:

```bash
mkdir -p ~/projects
git clone https://github.com/vast-cow/codex-notify.git ~/projects/codex-notify
cd ~/projects/codex-notify
```

### 2. Create the configuration

From the repository root, copy the example configuration:

```bash
cp config.example.json config.json
```

Edit `config.json` with your channel credentials and settings, and disable any unused channel. See the [configuration reference](configuration.md) and [security guidance](security.md).

Keep `config.json` alongside `main.py` in the repository root:

```text
codex-notify/
├── main.py
├── config.example.json
└── config.json
```

### 3. Configure Codex CLI

Edit `~/.codex/config.toml` and add:

```toml
notify = ["python3", "/absolute/path/to/codex-notify/main.py"]
```

`/absolute/path/to/codex-notify` is a placeholder for the actual absolute path to your local checkout. Run `pwd` from the repository root to find it.

For example, if you cloned into `/home/alice/projects/codex-notify`, use:

```toml
notify = ["python3", "/home/alice/projects/codex-notify/main.py"]
```

On macOS, the example path might be `/Users/alice/projects/codex-notify/main.py`. Use the path to your own checkout regardless of the directory you chose.

**Important:** The `notify` setting must be defined at the top level of `config.toml`, not inside another TOML section. Codex CLI runs `main.py` directly from the checkout, and `config.json` must remain alongside it.

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

