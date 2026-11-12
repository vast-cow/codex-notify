# Testing and Event Behavior

[Back to README](../README.md)

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

Both enabled channels should receive a notification. A `processed` line is also printed for disabled channels, so console output alone does not confirm delivery.

### Example Notification

```text
✅ Codex CLI - Task done

📁 Project: my-app
📂 Path: /home/user/projects/my-app

📝 Request:
Implement the REST API and run all tests.

📋 Result:
API implementation completed. All tests passed.
```

The notification heading and section labels are defined in `format_message()` in [main.py](../main.py).

## Event Behavior

The `agent-turn-complete` event indicates that a Codex agent turn has finished.

**It does not guarantee that the task completed successfully.**

For example, notifications may still be sent when:

- Tests fail
- Codex cannot complete a requested task
- The agent reports an implementation error
- The agent finishes a turn without completing the overall task

Additionally, a single Codex session may generate multiple notifications because each completed agent turn can trigger the hook.

