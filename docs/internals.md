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
4. Validate `notification.excluded_project_paths` and exit successfully if the normalized event `cwd` exactly matches an entry (see [matching rules](configuration.md#exclude-selected-projects)).
5. If [turn duration filtering](configuration.md#minimum-turn-duration) is enabled, read the duration from the matching session and turn record. Exit successfully if the duration is below the threshold or unavailable.
6. If [app-server filtering](app-server.md) is enabled, use one connection for the enabled ephemeral, archive, and active goal checks. Recheck an initially active goal after waiting for turn completion when the minimum duration is zero. Apply the confirmed exclusions or the lookup failure policy to both channels.
7. Format the completion message, limiting each Request and Result body independently when truncation is enabled, and adding an optional thread URL.
8. Send the message to Telegram if enabled.
9. Send the message to Slack if enabled.
10. Report notification errors to standard error.

Telegram and Slack are handled independently. A failure in one channel does not prevent the other from being attempted.

## Dependencies

Notification delivery uses the Python standard library. App-server filtering uses the optional `websockets` package, imported when a lookup connects.

## Limitations

- Notifications are sent synchronously.
- There is no automatic retry mechanism.
- Only `agent-turn-complete` events are processed.
- Task success and failure are not determined automatically.
- Request and Result bodies may be truncated according to configuration.
- Telegram imposes message length limits; use a suitable `max_output_length` to avoid oversized messages.
- The script reports HTTP failures without exposing notification credentials.

