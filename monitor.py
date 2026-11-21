"""Discover, confirm, and deduplicate turn completions."""

import asyncio
import logging
import math
import time
from dataclasses import dataclass

from app_server import AppServerError, ConnectionLost, RpcError, lookup_thread, pages
from notifications import is_turn_duration_filtered


HISTORY_TIMEOUT = 30
POLL_INTERVAL = 1
MAX_RECONNECT_DELAY = 30
TERMINAL_STATUSES = ("completed", "failed", "interrupted")
log = logging.getLogger("notify")


@dataclass(frozen=True)
class Check:
    since: float
    reconcile: bool


def timestamp(value):
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return value
    return None


class Monitor:
    def __init__(self, settings, transport, notifier, started_at):
        self.settings = settings
        self.transport = transport
        self.notifier = notifier
        self.started_at = started_at
        self.loaded = set()
        self.active_since = {}
        self.pending = {}
        self.processed = set()
        self.scans = {}
        self.rpc = None
        self.loading = None

    def schedule(self, thread_id, *, since, reconcile=False):
        old_check = self.pending.get(thread_id)
        if old_check is not None:
            since = max(since, old_check.since)
            # Overlapping signals can refer to different turns with the same timestamp.
            reconcile = True
        self.pending[thread_id] = Check(since, reconcile)
        if self.rpc is not None and thread_id not in self.scans:
            self.scans[thread_id] = asyncio.create_task(self.check_history(thread_id))

    def on_status(self, params):
        if not isinstance(params, dict):
            return
        thread_id = params.get("threadId")
        status = params.get("status")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(status, dict):
            return
        state = status.get("type")
        if state not in ("active", "idle", "systemError", "notLoaded"):
            return
        if self.loading is not None:
            self.loading[thread_id] = state
        if state == "notLoaded":
            self.loaded.discard(thread_id)
        else:
            self.loaded.add(thread_id)
        if state == "active":
            self.active_since.setdefault(thread_id, math.floor(time.time()))
        else:
            since = self.active_since.pop(thread_id, self.started_at)
            self.schedule(thread_id, since=since)

    async def sync_loaded(self, status_changes):
        previously_known = self.loaded | self.active_since.keys() | self.pending.keys()
        current = set()
        async for rows in pages(self.rpc, "thread/loaded/list", {}):
            if any(not isinstance(row, str) or not row for row in rows):
                raise AppServerError("malformed thread/loaded/list thread ID")
            current.update(rows)
        for thread_id, state in status_changes.items():
            if state == "notLoaded":
                current.discard(thread_id)
            else:
                current.add(thread_id)
        # Keep runtime changes that arrived while the snapshot was paginated.
        self.loading = None
        self.loaded = current
        for thread_id in current | previously_known | self.pending.keys():
            since = self.active_since.get(thread_id, self.started_at)
            self.schedule(thread_id, since=since, reconcile=True)
        self.active_since = {
            thread_id: since for thread_id, since in self.active_since.items()
            if thread_id in current
        }

    async def run(self):
        self.slots = asyncio.Semaphore(8)
        delay = 1
        try:
            while True:
                status_changes = self.loading = {}
                try:
                    async with self.transport.open(self.on_status, self.settings.timeout_seconds) as rpc:
                        self.rpc = rpc
                        try:
                            await self.sync_loaded(status_changes)
                            delay = 1
                            log.info("Monitoring shared app-server")
                            await rpc.wait_closed()
                        finally:
                            self.rpc = None
                            for scan in self.scans.values():
                                scan.cancel()
                            await asyncio.gather(*self.scans.values(), return_exceptions=True)
                            self.scans.clear()
                except (AppServerError, OSError, asyncio.TimeoutError) as exc:
                    reason = str(exc) if isinstance(exc, AppServerError) else type(exc).__name__
                    log.warning("App-server unavailable (%s). Reconnecting in %s s", reason, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)
        finally:
            await self.notifier.drain()

    async def check_history(self, thread_id):
        loop = asyncio.get_running_loop()
        last_check = None
        confirmed = False
        try:
            while True:
                failure = None
                try:
                    async with self.slots:
                        check = self.pending[thread_id]
                        if check is not last_check:
                            last_check = check
                            # Queueing behind other threads must not consume the first attempt.
                            deadline = loop.time() + HISTORY_TIMEOUT
                            confirmed = False
                        observed, unresolved = await self.scan_history(thread_id, check.since, deadline)
                    confirmed |= observed
                    retry = check.reconcile or unresolved or not confirmed
                except ConnectionLost:
                    return
                except RpcError as exc:
                    retry = not (exc.ephemeral_history or exc.code in (-32601, -32602))
                    if exc.ephemeral_history:
                        log.info("Skipped turn history (%s)", exc)
                    else:
                        failure = str(exc)
                except (AppServerError, asyncio.TimeoutError) as exc:
                    retry = True
                    failure = str(exc) or "request timed out"

                # A new completion signal can arrive while a history request is in flight.
                if self.pending[thread_id] is not check:
                    continue
                remaining = deadline - loop.time()
                if not retry or remaining <= POLL_INTERVAL:
                    if failure:
                        report = log.error if self.settings.on_lookup_failure == "error" else log.warning
                        report("Turn history unavailable (%s)", failure)
                    elif retry and not check.reconcile:
                        log.info("Turn history did not confirm the completion within the retry window")
                    self.pending.pop(thread_id)
                    return
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            self.scans.pop(thread_id, None)

    async def scan_history(self, thread_id, since, deadline):
        confirmed = False
        unresolved = False
        malformed = False
        async for rows in pages(self.rpc, "thread/turns/list", {
            "threadId": thread_id, "sortDirection": "desc", "itemsView": "summary", "limit": 20,
        }, deadline=deadline):
            reached_startup = False
            # Process each page before requesting the next one so an older page
            # failing cannot suppress completions already confirmed by history.
            for turn in reversed(rows):
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
                    malformed = True
                    continue
                completed_at = timestamp(turn.get("completedAt"))
                if completed_at is not None and completed_at <= self.started_at:
                    reached_startup = True
                    continue
                key = (thread_id, turn["id"])
                if key in self.processed:
                    continue
                status = turn.get("status")
                if completed_at is None or status not in TERMINAL_STATUSES:
                    started = timestamp(turn.get("startedAt"))
                    if started is None or started >= since or status == "inProgress":
                        unresolved = True
                    continue
                if status == "completed" and not isinstance(turn.get("items"), list):
                    unresolved = True
                    continue
                if status == "completed":
                    await self.process_turn(thread_id, turn)
                self.processed.add(key)
                confirmed |= completed_at >= since
            if reached_startup:
                # The history is ordered newest-first.
                break

        if malformed:
            raise AppServerError("malformed thread/turns/list turn")
        return confirmed, unresolved

    async def process_turn(self, thread_id, turn):
        if is_turn_duration_filtered(turn.get("durationMs"), self.notifier.min_turn_duration):
            return
        metadata = await lookup_thread(
            self.rpc, self.settings, thread_id, check_goal=self.notifier.skip_active_goals,
        )
        if self.notifier.is_project_excluded(metadata.cwd):
            log.info("Skipped turn from an excluded project")
            return

        states = {}
        if self.settings.skip_ephemeral:
            states["ephemeral thread"] = metadata.ephemeral
        if self.settings.skip_archived:
            states["archived thread"] = metadata.archived
        if self.notifier.skip_active_goals:
            states["active goal"] = metadata.active_goal
        for name, state in states.items():
            if state is True:
                log.info("Skipped turn due to %s", name)
                return

        unknown = [name for name, state in states.items() if state is None]
        if metadata.cwd is None:
            unknown.append("working directory")
        if unknown:
            action = self.settings.on_lookup_failure
            report = log.error if action == "error" else log.warning
            report(
                "App-server lookup incomplete for %s (on_lookup_failure=%s%s)",
                ", ".join(unknown), action,
                "; " + ", ".join(dict.fromkeys(metadata.failures)) if metadata.failures else "",
            )
            if action != "notify":
                return
        self.notifier.notify(thread_id, metadata.cwd, turn)
