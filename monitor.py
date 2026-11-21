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
    deadline: float
    retry_if_missing: bool


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

    def schedule(self, thread_id, *, since, retry_if_missing):
        old_check = self.pending.get(thread_id)
        if old_check is not None:
            since = max(since, old_check.since)
            retry_if_missing = retry_if_missing or old_check.retry_if_missing
        self.pending[thread_id] = Check(
            since, asyncio.get_running_loop().time() + HISTORY_TIMEOUT, retry_if_missing,
        )
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
            self.schedule(thread_id, since=since, retry_if_missing=True)

    async def sync_loaded(self, reconnecting):
        previously_known = self.loaded | self.active_since.keys() | self.pending.keys()
        current = set()
        async for rows in pages(self.rpc, "thread/loaded/list", {}):
            if any(not isinstance(row, str) or not row for row in rows):
                raise AppServerError("malformed thread/loaded/list thread ID")
            current.update(rows)
        for thread_id, state in self.loading.items():
            if state == "notLoaded":
                current.discard(thread_id)
            else:
                current.add(thread_id)
        # Keep runtime changes that arrived while the snapshot was paginated.
        self.loading = None
        self.loaded = current
        for thread_id in current | previously_known | self.pending.keys():
            since = self.active_since.get(thread_id, self.started_at)
            self.schedule(thread_id, since=since, retry_if_missing=reconnecting)
        self.active_since = {
            thread_id: since for thread_id, since in self.active_since.items()
            if thread_id in current
        }

    async def run(self):
        self.slots = asyncio.Semaphore(8)
        delay = 1
        reconnecting = False
        try:
            while True:
                self.loading = {}
                try:
                    async with self.transport.open(self.on_status, self.settings.timeout_seconds) as rpc:
                        self.rpc = rpc
                        try:
                            await self.sync_loaded(reconnecting)
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
                reconnecting = True
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)
        finally:
            await self.notifier.drain()

    async def check_history(self, thread_id):
        try:
            while True:
                check = self.pending[thread_id]
                failure = None
                try:
                    async with self.slots:
                        retry = await self.scan_history(thread_id, check)
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
                remaining = check.deadline - asyncio.get_running_loop().time()
                if not retry or remaining <= 0:
                    if failure:
                        report = log.error if self.settings.on_lookup_failure == "error" else log.warning
                        report("Turn history unavailable (%s)", failure)
                    elif retry:
                        log.info("Stopped waiting for turn history after %s s", HISTORY_TIMEOUT)
                    self.pending.pop(thread_id)
                    return
                await asyncio.sleep(min(POLL_INTERVAL, remaining))
        finally:
            self.scans.pop(thread_id, None)

    async def scan_history(self, thread_id, check):
        confirmed = False
        unresolved = False
        candidates = []
        async for rows in pages(self.rpc, "thread/turns/list", {
            "threadId": thread_id, "sortDirection": "desc", "itemsView": "summary", "limit": 20,
        }, deadline=check.deadline):
            reached_startup = False
            for turn in rows:
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
                    raise AppServerError("malformed thread/turns/list turn")
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
                    if started is None or started >= check.since or status == "inProgress":
                        unresolved = True
                    continue
                if status == "completed" and not isinstance(turn.get("items"), list):
                    unresolved = True
                    continue
                candidates.append((turn, completed_at))
            if reached_startup:
                # The history is ordered newest-first.
                break

        for turn, completed_at in reversed(candidates):
            key = (thread_id, turn["id"])
            if key in self.processed:
                continue
            if turn["status"] == "completed":
                await self.process_turn(thread_id, turn)
            self.processed.add(key)
            confirmed |= completed_at >= check.since
        return unresolved or (check.retry_if_missing and not confirmed)

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
