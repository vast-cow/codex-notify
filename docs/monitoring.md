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

## Notification filters

The monitor confirms a turn's result using `thread/turns/list`. It uses the text from the persisted user messages and the last non-commentary assistant message in the summary. Formatting, truncation, thread links, and destination settings use the same configuration keys as v1. See [config.example.json](../config.example.json) for the full set of options.

| Setting | Default | Behavior |
| --- | --- | --- |
| `notification.min_turn_duration_minutes` | `1` | Uses `durationMs`. Suppresses turns below the threshold or with an unknown duration. Set to `0` to disable the duration filter. |
| `notification.excluded_project_paths` | `[]` | Suppresses exact working-directory matches after expanding `~` and normalizing paths. |
| `app_server.skip_ephemeral` | `true` | Suppresses known ephemeral threads. A turn needs readable history even when this filter is disabled. |
| `app_server.skip_archived` | `true` | Suppresses archived threads. Archive membership requires an exact thread ID match in `thread/list`. |
| `notification.skip_active_goals` | `true` | Suppresses turns whose Goal is `active` when checked after history confirms completion. Enable `features.goals` in the app-server's Codex configuration or turn off this filter. |
| `app_server.on_lookup_failure` | `notify` | If the working directory or an enabled filter state is unknown, `notify` logs the issue and proceeds, `skip` suppresses the turn, and `error` suppresses the turn and logs an error. Monitoring continues. A confirmed exclusion takes precedence. |
| `app_server.timeout_seconds` | `1.5` | Timeout for connection setup and each app-server request. |
| `notification.timeout_seconds` | `10` | HTTP timeout for each enabled destination. |

Telegram and Slack send independently, so a failure on one leaves the other delivery running. Each eligible turn is attempted once per enabled destination. Delivery failures are logged.

## History and reconnects

- `thread/status/changed` signals a history check when a thread becomes idle, unloads, or reports a system error. The monitor requires `status=completed` and `completedAt` strictly later than its startup time. Codex reports timestamps to the second, so a turn in the startup second can fall outside the cutoff. [Codex Turn schema](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/app-server-protocol/schema/typescript/v2/Turn.ts)
- When a completion is signaled but history has yet to confirm it, the monitor rechecks for up to 30 seconds. Startup checks the threads returned by `thread/loaded/list`. Reconnects check currently loaded threads, previously known threads, and pending checks.
- Reconnect delays increase from 1 second to a maximum of 30 seconds and reset after a successful loaded-thread check. A thread that starts and unloads entirely during a disconnect can be missed.
- Deduplication uses the pair of thread ID and turn ID in memory for the lifetime of the process. Use a single monitor per set of destinations. Stopping the monitor clears its deduplication state, and restarting establishes a new startup cutoff.
