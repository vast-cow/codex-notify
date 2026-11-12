# How It Works

[Back to README](../README.md)

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

