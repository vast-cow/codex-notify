# Configuration and monitoring

## Shared app-server

The monitor connects to the listener that runs the turns. Use a compatible version of Codex, such as 0.171.0. The monitor initializes one WebSocket per connection and uses read-only thread methods. [Codex app-server protocol](https://learn.chatgpt.com/docs/app-server#threads)

Use a trusted loopback TCP listener or a private Unix socket directory. The monitor connects directly, without authentication headers. For a Unix socket, start the listener and connect Codex as follows.

```bash
mkdir -p -m 700 "$HOME/.codex-notify-sockets"
codex app-server --listen "unix://$HOME/.codex-notify-sockets/app-server.sock"
codex --remote "unix://$HOME/.codex-notify-sockets/app-server.sock"
```

Set `app_server.endpoint` to the expanded absolute socket path, such as `unix:///home/alice/.codex-notify-sockets/app-server.sock`. The endpoint is required. The legacy `app_server.enabled` setting is ignored.

## Starting the monitor and reading logs

Run `python3 main.py` from the project directory to see `INFO`, `WARNING`, and `ERROR` messages. Use `--debug` to also see live thread states, recognized waiting flags, and history check starts, retries, terminal turns, and exits.

```bash
python3 main.py
python3 main.py --debug
python3 main.py --debug 2>> monitor.log
```

Monitor logs and configuration errors go to standard error. Each entry starts with a local ISO 8601 timestamp with milliseconds and a time zone offset, followed by `[notify]`, the log level, and the diagnostic message.

```text
2026-11-21T20:53:47.900+09:00 [notify] DEBUG Thread status changed: state=active waiting_flags=['waitingOnUserInput'] thread_id='thread-123'
2026-11-21T20:53:48.123+09:00 [notify] INFO Slack notification sent thread_id='thread-123' turn_id='turn-456' notification_type='completion'
2026-11-21T20:53:48.210+09:00 [notify] ERROR Telegram delivery failed (RuntimeError) thread_id='thread-789' notification_type='action_required'
```

Use `thread_id` to follow history checks and lookup failures for a thread. Use `turn_id` to narrow completion and exclusion logs to one turn. IDs appear when available. Action-required delivery logs identify the thread, and each delivery result names the destination and records the notification type. Debug logs include diagnostic state and IDs. Notification text and destination credentials stay out of delivery and debug logs.

## Action-required notifications

During a turn, the monitor watches `activeFlags` in live `thread/status/changed` events. In Codex 0.171.0, `waitingOnUserInput` signals a Plan mode question. `waitingOnApproval` can signal an MCP input request or a permission approval, including for commands and file changes. [Codex event handling](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/app-server/src/bespoke_event_handling.rs#L875-L998)

The monitor sends `🔔 Codex CLI - Action required` to the enabled Telegram and Slack destinations. The message includes the waiting state, project information when enabled, and a configured thread link. Respond or approve in the original Codex thread.

Each flag alerts once while it stays active. Once it clears, a later occurrence can alert again. Metadata lookups run alongside history checks. If the flags change during a lookup, the monitor sends one message for the newly observed flags that are still active when the lookup finishes.

## Notification filters

The monitor confirms a turn's result using `thread/turns/list`. It uses the text from the persisted user messages and the last non-commentary assistant message in the summary. Formatting, truncation, thread links, and destination settings use the same configuration keys as v1. See [config.example.json](../config.example.json) for the full set of options.

| Setting | Default | Behavior |
| --- | --- | --- |
| `notification.min_turn_duration_minutes` | `1` | Applies to completion notifications. Uses `durationMs` and suppresses turns below the threshold or with an unknown duration. Set to `0` to disable the duration filter. |
| `notification.excluded_project_paths` | `[]` | Suppresses exact working-directory matches after expanding `~` and normalizing paths. |
| `app_server.skip_ephemeral` | `true` | Suppresses known ephemeral threads. Completion notifications require readable history even when this filter is disabled. |
| `app_server.skip_archived` | `true` | Suppresses archived threads. Archive membership requires an exact thread ID match in `thread/list`. |
| `notification.skip_active_goals` | `true` | Suppresses completion notifications when the Goal is `active` after history confirms completion. Enable `features.goals` in the app-server's Codex configuration or turn off this filter. |
| `app_server.on_lookup_failure` | `notify` | If the working directory or an enabled filter state is unknown, `notify` logs the issue and proceeds, `skip` suppresses the notification, and `error` suppresses it and logs an error. Monitoring continues. A confirmed exclusion takes precedence. |
| `app_server.timeout_seconds` | `1.5` | Timeout for connection setup and each app-server request. |
| `notification.timeout_seconds` | `10` | HTTP timeout for each enabled destination. |

Telegram and Slack send independently, so a failure on one leaves the other delivery running. Each eligible notification is attempted once per enabled destination. Delivery failures are logged.

## History and reconnects

- `thread/status/changed` signals a history check when a thread becomes idle, unloads, or reports a system error. The monitor requires `status=completed` and `completedAt` strictly later than its startup time. Codex reports timestamps to the second, so a turn in the startup second can fall outside the cutoff. [Codex Turn schema](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/app-server-protocol/schema/typescript/v2/Turn.ts)
- The monitor rechecks history for up to 30 seconds from the first lookup when a completion has yet to appear or completion signals overlap. Startup and reconnects also recheck during this window because a completion can be delayed even when an earlier turn is already visible. Startup uses the threads returned by `thread/loaded/list`. Reconnects use currently loaded threads, previously known threads, and pending checks.
- A disconnect cancels pending waiting-state lookups and clears the observed flags. Startup and reconnection snapshots are used for completion history only. Action-required notifications resume when the new connection receives live waiting flags.
- Reconnect delays increase from 1 second to a maximum of 30 seconds and reset after a successful loaded-thread check. A thread that starts and unloads entirely during a disconnect can be missed.
- Completion deduplication uses the pair of thread ID and turn ID in memory for the lifetime of the process. Use a single monitor per set of destinations. Stopping the monitor clears its deduplication state, and restarting establishes a new startup cutoff.
