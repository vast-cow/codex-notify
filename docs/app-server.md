# App-Server Filtering

[Back to README](../README.md)

Enable this feature to suppress Telegram and Slack notifications for ephemeral threads, archived threads, or threads with active goals. The notifier connects to an app-server you already run, using the endpoint in `config.json`.

## Setup

Install the optional WebSocket client with the same Python interpreter used in the Codex `notify` command. If you use a virtual environment, put its Python path in the hook command.

```bash
python3 -m pip install 'websockets>=13.1,<17'
```

Merge the following section into `config.json` and choose one of the listener endpoints below.

```json
{
  "app_server": {
    "enabled": true,
    "endpoint": "ws://127.0.0.1:8765",
    "skip_ephemeral": true,
    "skip_archived": true,
    "timeout_seconds": 1.5,
    "on_lookup_failure": "notify"
  }
}
```

### TCP WebSocket

Run the app-server with a loopback listener and set `app_server.endpoint` to `ws://127.0.0.1:8765`.

```bash
codex app-server --listen ws://127.0.0.1:8765
```

For a supported terminal client, use `codex --remote ws://127.0.0.1:8765` to run the turn on that server.

### Unix domain socket

On systems with Unix domain socket support, create a private directory and listen on a socket there.

```bash
mkdir -m 700 -p "$HOME/.codex-notify-sockets"
codex app-server --listen "unix://$HOME/.codex-notify-sockets/app-server.sock"
```

Set `app_server.endpoint` to the expanded absolute path, for example `unix:///home/alice/.codex-notify-sockets/app-server.sock`. For a supported terminal client, use `codex --remote "unix://$HOME/.codex-notify-sockets/app-server.sock"`. Keep the listener running and configure the `notify` hook in the server's Codex configuration.

## Filtering decisions

The notifier checks project exclusions and minimum turn duration before connecting. When `app_server.enabled` and at least one filter are enabled, the notifier uses one app-server connection per eligible completion event and shares the result across both channels. Goal checks require `notification.skip_active_goals`, which defaults to `true`.

- A confirmed ephemeral or archived state suppresses both channels if its corresponding filter is enabled. The notifier checks these states first and exits with status `0` for a confirmed exclusion, even if another state is unknown.
- A confirmed active goal also suppresses both channels with status `0`. With a zero minimum duration, an initially active goal triggers a wait for the turn's completion record and a second goal request. The second response determines the goal state, even if the wait expired. See [active goal filtering](configuration.md#active-goal-filtering).
- When all enabled states are confirmed false, delivery proceeds. Disabled filters do not affect the decision.
- If an enabled state is unknown, `notify` proceeds with delivery, `skip` suppresses both channels with status `0`, and `error` suppresses both channels with status `1`.

Connection setup, initialization, and thread metadata requests share an `app_server.timeout_seconds` deadline. Each goal request gets its own deadline of the same length. Diagnostics report confirmed exclusions, lookup errors, unavailable states, and the resulting delivery decision. Run `main.py` directly with a completion event to see its output if your Codex client discards hook stdout and stderr.

## Thread identity and compatibility

The client requests `thread/read` with `includeTurns: false` for the ephemeral flag. Archive state requires an exact thread ID match in an archived or active `thread/list` result. Empty results and unrelated list rows leave the state unknown. The client checks the working directory when present on both sides and treats inconsistencies as lookup failures.

The client sends the event's thread ID as `threadId` to `thread/goal/get`. An explicit `result.goal` of `null` means the thread has no goal. A goal object must contain the matching `threadId` and a valid status. Unsupported methods and invalid replies leave the goal state unknown. [Codex goal response schema](https://github.com/openai/codex/blob/rust-v0.170.0/codex-rs/app-server-protocol/schema/typescript/v2/ThreadGoalGetResponse.ts)

These checks verify thread identity. The completion event has no process identifier to correlate with the listener. The initialization response describes the server environment, and even the experimental `server/diagnostics` process ID is local to the server and has no matching field in the event. A server sharing durable history may be able to read the same saved thread. Ephemeral threads live in the app-server instance that owns them and may be unavailable from a separate listener. Use the listener that handles your turns. Unavailable threads follow the configured failure policy. [Codex app-server protocol](https://developers.openai.com/codex/app-server)

Use a Codex version that supports your chosen listener and the methods for your enabled filters. Some older versions, including Codex CLI 0.169.0, ignore `includedThreadIds`. Archive membership can still be established by an exact match on the first returned page of the corresponding list. A miss follows the failure policy, as do unavailable methods or metadata, unindexed threads, and threads hidden from ordinary listings. [Thread list ID filters](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#thread-list-id-filters)

The listener must have Codex's `features.goals` enabled for goal lookups. If Codex rejects the request because Goals is disabled, the notifier applies `app_server.on_lookup_failure`. Set `notification.skip_active_goals` to `false` to use the thread filters alone on that listener. [Codex goal request handler](https://github.com/openai/codex/blob/rust-v0.170.0/codex-rs/app-server/src/request_processors/thread_goal_processor.rs)

## Security

Use a private Unix socket directory or a trusted loopback TCP listener. Plain `ws://` is unencrypted, and app-server clients can invoke operations beyond metadata reads. Keep the listener off untrusted networks. The notifier supports direct connections through these transports without authentication headers. It sends the thread ID to the server and keeps prompts, responses, and credentials out of filter diagnostics.
