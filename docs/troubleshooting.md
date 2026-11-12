# Troubleshooting

[Back to README](../README.md)

## Telegram notifications are not delivered

Verify that:

- The Bot Token is correct.
- The Chat ID is correct.
- You have started a conversation with the bot.
- The bot has permission to send messages to the destination chat.
- `telegram.enabled` is set to `true`.

## Slack notifications are not delivered

Verify that:

- The Webhook URL is correct.
- Incoming Webhooks are enabled for the Slack App.
- The webhook is associated with the intended channel.
- `slack.enabled` is set to `true`.

## Codex does not trigger notifications

Verify that:

- `notify` is configured at the top level of `~/.codex/config.toml`.
- The Python script path is absolute and correct.
- Python 3 is installed and available.
- `config.json` exists next to `notify.py`.
- The Codex CLI version supports the `notify` configuration.
- The current Codex agent turn has completed.

You can also manually run the [test command](usage.md#testing) to distinguish hook configuration problems from notification delivery problems.

