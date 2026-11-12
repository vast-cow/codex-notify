# Codex CLI Completion Notifier

A Python hook that sends Codex CLI turn completion notifications to Telegram and Slack. Each channel can be enabled independently; messages can include the project, request, and response.

## Requirements

- Python 3.8 or later; no third-party packages required
- Codex CLI with `notify` hook support
- A Telegram bot token and chat ID, or a Slack Incoming Webhook URL

## Quick Start

From the repository root:

```bash
mkdir -p ~/.codex/hooks
cp main.py ~/.codex/hooks/notify.py
cp config.example.json ~/.codex/hooks/config.json
```

Edit the copied `config.json` with your credentials and disable any unused channel. See the [setup guide](docs/setup.md) for Telegram and Slack setup.

Add this at the top level of `~/.codex/config.toml`, replacing the path with your absolute home directory:

```toml
notify = ["python3", "/home/YOUR_USER/.codex/hooks/notify.py"]
```

On macOS, use `/Users/YOUR_USER` instead. Keep `config.json` next to the installed script and protect its credentials as described in the [security guide](docs/security.md).

Notifications indicate a completed agent turn; they do not guarantee task success. See [testing and event behavior](docs/usage.md) for a manual test.

## Documentation

- [Setup guide](docs/setup.md)
- [Configuration reference](docs/configuration.md)
- [Testing and event behavior](docs/usage.md)
- [How it works and limitations](docs/internals.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Security and privacy](docs/security.md)
