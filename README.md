# Codex CLI Completion Notifier

A Python hook that sends Codex CLI turn completion notifications to Telegram and Slack. Each channel can be enabled independently; messages can include the project, request, and response.

## Requirements

- Python 3.8 or later. The optional app-server filter uses the `websockets` package.
- Codex CLI with `notify` hook support
- A Telegram bot token and chat ID, or a Slack Incoming Webhook URL

## Quick Start

Clone the repository into any directory you choose. For example, `~/projects/codex-notify` is an optional installation directory:

```bash
mkdir -p ~/projects
git clone https://github.com/vast-cow/codex-notify.git ~/projects/codex-notify
cd ~/projects/codex-notify
cp config.example.json config.json
```

Edit `config.json` in the repository root with your credentials and channel settings, and disable any unused channel. See the [setup guide](docs/setup.md) for Telegram and Slack setup.

Add this at the top level of `~/.codex/config.toml`:

```toml
notify = ["python3", "/absolute/path/to/codex-notify/main.py"]
```

Replace `/absolute/path/to/codex-notify` with the actual absolute path to your local checkout. For the example above, run `pwd` from the repository root to find that path, such as `/home/alice/projects/codex-notify` on Linux or `/Users/alice/projects/codex-notify` on macOS.

Codex CLI runs `main.py` directly from the checkout. Keep `config.json` alongside `main.py` in the repository root and protect its credentials as described in the [security guide](docs/security.md).

Notifications indicate a completed agent turn; they do not guarantee task success. See [testing and event behavior](docs/usage.md) for a manual test.

By default, Telegram and Slack notify for turns that took at least one minute and have a recorded duration. See [turn duration filtering](docs/configuration.md#minimum-turn-duration) to change the threshold or disable the filter.

You can optionally suppress notifications for ephemeral or archived threads by connecting the notifier to an existing Codex app-server. App-server filtering is disabled by default.

## Documentation

- [Setup guide](docs/setup.md)
- [Configuration reference](docs/configuration.md)
- [App-server filtering](docs/app-server.md)
- [Testing and event behavior](docs/usage.md)
- [How it works and limitations](docs/internals.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Security and privacy](docs/security.md)
