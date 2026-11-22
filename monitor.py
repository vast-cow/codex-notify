"""Monitor live waiting states, Goal updates, and turn completions."""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

from app_server import (
    AppServerError, ConnectionLost, Goal, RpcError, ThreadMetadata, lookup_goal, lookup_thread, pages, timestamp,
)
from notifications import WAITING_LABELS, event_from_summary


HISTORY_TIMEOUT = 30
POLL_INTERVAL = 1
MAX_RECONNECT_DELAY = 30
TERMINAL_STATUSES = ("completed", "failed", "interrupted")
GOAL_STOP_STATUSES = ("complete", "blocked", "usageLimited", "budgetLimited")
log = logging.getLogger("notify")


def turn_history(rpc, thread_id, deadline):
    return pages(rpc, "thread/turns/list", {
        "threadId": thread_id, "sortDirection": "desc", "itemsView": "summary", "limit": 20,
    }, deadline=deadline)


@dataclass(frozen=True)
class Check:
    since: float
    reconcile: bool


@dataclass
class GoalExecution:
    goal: Optional[Goal] = None
    active_since: Optional[int] = None
    periods: list = field(default_factory=list)
    turn_ids: set = field(default_factory=set)
    notified_stops: set = field(default_factory=set)

    def update(self, goal, observed_at, disconnected_at):
        previous = self.goal
        same_goal = previous is not None and goal is not None and previous.created_at == goal.created_at
        active = goal is not None and goal.status == "active"
        if self.active_since is not None and (not active or not same_goal):
            if goal is None:
                end = observed_at
            else:
                end = goal.updated_at if same_goal else goal.created_at
            self.periods.append((self.active_since, max(self.active_since, end)))
            self.active_since = None
        if active and self.active_since is None:
            self.active_since = goal.updated_at if same_goal else goal.created_at
            if same_goal and disconnected_at is not None:
                self.active_since = min(self.active_since, max(previous.updated_at, disconnected_at))
        elif goal is not None and not same_goal:
            self.periods.append((goal.created_at, goal.updated_at))
        elif goal is not None and disconnected_at is not None and not active and previous.status != "active":
            # A changed snapshot may cover a resume and another stop during the outage.
            if goal.updated_at > previous.updated_at:
                start = min(goal.updated_at, max(previous.updated_at, disconnected_at))
                self.periods.append((start, goal.updated_at))
        if not same_goal or active:
            self.notified_stops.clear()
        self.goal = goal

    def turn_membership(self, turn):
        """Return None while the turn might be waiting for a Goal stop update."""
        if turn["id"] in self.turn_ids:
            return True
        started = timestamp(turn.get("startedAt"))
        started = math.floor(started) if started is not None else None
        completed = math.floor(turn["completedAt"])
        for start, end in self.periods:
            if completed >= start and (started is None or started <= end):
                return True
        if self.active_since is not None and completed >= self.active_since:
            # The row can become visible before the stop update establishes the boundary.
            if completed <= self.goal.updated_at:
                return True
            return None
        return False


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
        self.waiting_flags = {}
        self.pending_actions = {}
        self.action_checks = {}
        self.goals = {}
        self.goal_lookups = {}
        self.goal_notifications = set()
        self.disconnected_at = None
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

    def on_notification(self, method, params):
        if method == "thread/status/changed":
            self.on_status(params)
        elif method in ("thread/goal/updated", "thread/goal/cleared"):
            self.on_goal(params, cleared=method == "thread/goal/cleared")

    def on_goal(self, params, *, cleared):
        if not isinstance(params, dict):
            return
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or not thread_id:
            return
        try:
            goal = None if cleared else Goal.parse(params.get("goal"), thread_id)
        except AppServerError as exc:
            log.debug("Ignored Goal update (%s)", exc, extra={"thread_id": thread_id})
            return
        turn_id = params.get("turnId") if not cleared else None
        if not isinstance(turn_id, str) or not turn_id:
            turn_id = None
        execution = self.record_goal(thread_id, goal, cleared=cleared)
        if turn_id is not None:
            execution.turn_ids.add(turn_id)
        if goal is not None and goal.status in GOAL_STOP_STATUSES and goal.status not in execution.notified_stops:
            execution.notified_stops.add(goal.status)
            deadline = asyncio.get_running_loop().time() + HISTORY_TIMEOUT
            task = asyncio.create_task(self.process_goal(thread_id, goal, turn_id, deadline))
            self.goal_notifications.add(task)
            task.add_done_callback(self.goal_notifications.discard)

    def record_goal(self, thread_id, goal, *, cleared=False):
        observed_at = math.floor(time.time())
        execution = self.goals.get(thread_id)
        if execution is None:
            execution = self.goals[thread_id] = GoalExecution()
            if cleared:
                # A clear has no timestamp or Goal details when it beats the initial snapshot.
                execution.periods.append((math.floor(self.started_at), observed_at))
        disconnected_at = self.disconnected_at if thread_id not in self.goal_lookups else None
        execution.update(goal, observed_at, disconnected_at)
        self.goal_lookups[thread_id] = None
        return execution

    async def sync_goal(self, thread_id):
        if thread_id in self.goal_lookups:
            return
        try:
            goal = await lookup_goal(self.rpc, thread_id)
        except ConnectionLost:
            raise
        except (AppServerError, asyncio.TimeoutError) as exc:
            self.goal_lookups.setdefault(thread_id, str(exc) or "thread/goal/get timed out")
        else:
            # A live update received while the snapshot was in flight takes precedence.
            if thread_id not in self.goal_lookups:
                self.record_goal(thread_id, goal)

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
        self.update_waiting_flags(thread_id, status.get("activeFlags") if state == "active" else [])
        log.debug("Thread status changed: state=%s waiting_flags=%s",
                  state, sorted(self.waiting_flags.get(thread_id, ())), extra={"thread_id": thread_id})
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

    def update_waiting_flags(self, thread_id, active_flags):
        if not isinstance(active_flags, list):
            return
        previous = self.waiting_flags.pop(thread_id, set())
        pending = self.pending_actions.pop(thread_id, set())
        flags = {flag for flag in WAITING_LABELS if flag in active_flags}
        if flags:
            self.waiting_flags[thread_id] = flags
        # Keep only unresolved flags and add flags that became active in this event.
        pending = (pending & flags) | (flags - previous)
        if pending:
            self.pending_actions[thread_id] = pending
            self.schedule_action_check(thread_id)

    def schedule_action_check(self, thread_id):
        if self.rpc is not None and thread_id not in self.action_checks:
            self.action_checks[thread_id] = asyncio.create_task(self.check_action_required(thread_id))

    async def check_action_required(self, thread_id):
        try:
            async with self.action_slots:
                if thread_id not in self.pending_actions:
                    return
                metadata = await self.notification_metadata(thread_id)
            # Status updates can clear or add flags while the metadata is being fetched.
            flags = self.pending_actions.pop(thread_id, set())
            if flags and metadata is not None:
                self.notifier.notify_action_required(thread_id, metadata.cwd, flags)
        except ConnectionLost:
            pass
        finally:
            self.action_checks.pop(thread_id, None)

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
        self.action_slots = asyncio.Semaphore(8)
        delay = 1
        try:
            while True:
                status_changes = self.loading = {}
                try:
                    async with self.transport.open(self.on_notification, self.settings.timeout_seconds) as rpc:
                        self.rpc = rpc
                        try:
                            for thread_id in self.pending_actions:
                                self.schedule_action_check(thread_id)
                            await self.sync_loaded(status_changes)
                            delay = 1
                            log.info("Monitoring shared app-server")
                            await rpc.wait_closed()
                        finally:
                            self.rpc = None
                            self.disconnected_at = math.floor(time.time())
                            checks = (*self.scans.values(), *self.action_checks.values())
                            for check in checks:
                                check.cancel()
                            await asyncio.gather(*checks, return_exceptions=True)
                            self.scans.clear()
                            self.action_checks.clear()
                except (AppServerError, OSError, asyncio.TimeoutError) as exc:
                    reason = str(exc) if isinstance(exc, AppServerError) else type(exc).__name__
                    log.warning("App-server unavailable (%s). Reconnecting in %s s", reason, delay)
                finally:
                    self.goal_lookups.clear()
                    self.waiting_flags.clear()
                    self.pending_actions.clear()
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)
        finally:
            tasks = tuple(self.goal_notifications)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.notifier.drain()

    async def check_history(self, thread_id):
        loop = asyncio.get_running_loop()
        context = {"thread_id": thread_id}
        last_check = None
        confirmed = False
        try:
            while True:
                failure = None
                incomplete_turns = {}
                try:
                    async with self.slots:
                        check = self.pending[thread_id]
                        await self.sync_goal(thread_id)
                        if check is not last_check:
                            last_check = check
                            # Queueing behind other threads must not consume the first attempt.
                            deadline = loop.time() + HISTORY_TIMEOUT
                            confirmed = False
                            log.debug("Turn history check started (reconcile=%s)", check.reconcile, extra=context)
                        observed, unresolved = await self.scan_history(
                            thread_id, check.since, deadline, incomplete_turns,
                        )
                    confirmed |= observed
                    retry = check.reconcile or unresolved or not confirmed
                except ConnectionLost as exc:
                    log.debug("Turn history check interrupted (%s)", exc, extra=context)
                    return
                except RpcError as exc:
                    retry = not (exc.ephemeral_history or exc.code in (-32601, -32602))
                    if exc.ephemeral_history:
                        log.info("Skipped turn history (%s)", exc, extra=context)
                    else:
                        failure = str(exc)
                except (AppServerError, asyncio.TimeoutError) as exc:
                    retry = True
                    failure = str(exc) or "request timed out"

                # A new completion signal can arrive while a history request is in flight.
                if self.pending[thread_id] is not check:
                    log.debug("Turn history check superseded by a new signal", extra=context)
                    continue
                finished = not retry or deadline - loop.time() <= POLL_INTERVAL
                report = log.error if self.settings.on_lookup_failure == "error" else log.warning
                report_issue = report if finished else log.debug
                for turn_id, reason in incomplete_turns.items():
                    report_issue("Turn history is incomplete (%s)", reason,
                                 extra={"thread_id": thread_id, "turn_id": turn_id})
                if finished:
                    if failure:
                        report("Turn history unavailable (%s)", failure, extra=context)
                    elif retry and not check.reconcile and not incomplete_turns:
                        log.info("Turn history did not confirm the completion within the retry window",
                                 extra=context)
                    self.pending.pop(thread_id)
                    return
                log.debug("Retrying turn history check (%s)", failure or "checking for more completions",
                          extra=context)
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            self.scans.pop(thread_id, None)
            log.debug("Turn history check ended", extra=context)

    async def scan_history(self, thread_id, since, deadline, incomplete_turns):
        confirmed = False
        unresolved = False
        malformed = False
        async for rows in turn_history(self.rpc, thread_id, deadline):
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
                turn_id = turn["id"]
                key = (thread_id, turn_id)
                if key in self.processed:
                    continue
                status = turn.get("status")
                if completed_at is None or status not in TERMINAL_STATUSES:
                    started = timestamp(turn.get("startedAt"))
                    if started is None or started >= since or status == "inProgress":
                        unresolved = True
                        if status == "completed":
                            incomplete_turns[turn_id] = "missing or invalid completion timestamp"
                    continue
                if status == "completed" and not isinstance(turn.get("items"), list):
                    incomplete_turns[turn_id] = "missing or invalid item summary"
                    unresolved = True
                    continue
                log.debug("Turn history found a terminal turn (status=%s)", status,
                          extra={"thread_id": thread_id, "turn_id": turn_id})
                if status == "completed":
                    if await self.process_turn(thread_id, turn) is False:
                        unresolved = True
                        if deadline - asyncio.get_running_loop().time() > POLL_INTERVAL:
                            continue
                        self.skip_goal_turn(thread_id, turn_id)
                self.processed.add(key)
                incomplete_turns.pop(turn_id, None)
                confirmed |= completed_at >= since
            if reached_startup:
                # The history is ordered newest-first.
                break

        if malformed:
            raise AppServerError("malformed thread/turns/list turn")
        return confirmed, unresolved

    async def process_turn(self, thread_id, turn):
        membership = self.goal_membership(thread_id, turn)
        if membership is True:
            self.skip_goal_turn(thread_id, turn["id"])
            return
        if membership is None:
            return False
        metadata = await lookup_thread(self.rpc, self.settings, thread_id)
        # Live updates can change the classification during the metadata lookup.
        membership = self.goal_membership(thread_id, turn)
        if membership is True:
            self.skip_goal_turn(thread_id, turn["id"])
        elif membership is None:
            return False
        elif self.allow_notification(thread_id, turn["id"], metadata, goal_failure=self.goal_lookups.get(thread_id)):
            self.notifier.notify(thread_id, metadata.cwd, turn)

    def goal_membership(self, thread_id, turn):
        execution = self.goals.get(thread_id)
        return execution.turn_membership(turn) if execution else False

    @staticmethod
    def skip_goal_turn(thread_id, turn_id):
        log.info("Skipped completion notification for a Goal turn",
                 extra={"thread_id": thread_id, "turn_id": turn_id})

    async def process_goal(self, thread_id, goal, turn_id, deadline):
        loop = asyncio.get_running_loop()
        context = {"thread_id": thread_id, "turn_id": turn_id}
        metadata = None
        output = ""
        while loop.time() < deadline:
            rpc = self.rpc
            if rpc is not None:
                try:
                    if metadata is None:
                        metadata = await asyncio.wait_for(
                            lookup_thread(rpc, self.settings, thread_id), deadline - loop.time(),
                        )
                        if not self.allow_notification(thread_id, turn_id, metadata):
                            return
                    if not (self.notifier.include_output and turn_id):
                        break
                    output = await self.find_goal_output(rpc, thread_id, turn_id, deadline)
                    if output:
                        break
                except RpcError as exc:
                    if exc.ephemeral_history or exc.code in (-32601, -32602):
                        log.debug("Goal turn history unavailable (%s)", exc, extra=context)
                        break
                except (AppServerError, asyncio.TimeoutError):
                    pass
            await asyncio.sleep(max(0, min(POLL_INTERVAL, deadline - loop.time())))

        if metadata is None:
            metadata = ThreadMetadata(failures=["Goal notification metadata lookup timed out"])
            if not self.allow_notification(thread_id, turn_id, metadata):
                return
        self.notifier.notify_goal(thread_id, metadata.cwd, goal, turn_id, output)

    async def find_goal_output(self, rpc, thread_id, turn_id, deadline):
        async for rows in turn_history(rpc, thread_id, deadline):
            for turn in rows:
                if isinstance(turn, dict) and turn.get("id") == turn_id:
                    if turn.get("status") in TERMINAL_STATUSES and isinstance(turn.get("items"), list):
                        return event_from_summary(thread_id, None, turn)["last-assistant-message"]
                    return ""
        return ""

    async def notification_metadata(self, thread_id):
        try:
            metadata = await lookup_thread(self.rpc, self.settings, thread_id)
        except ConnectionLost as exc:
            log.debug("App-server lookup interrupted (%s)", exc, extra={"thread_id": thread_id})
            raise
        if self.allow_notification(thread_id, None, metadata):
            return metadata

    def allow_notification(self, thread_id, turn_id, metadata, *, goal_failure=None):
        context = {"thread_id": thread_id, "turn_id": turn_id}
        if self.notifier.is_project_excluded(metadata.cwd):
            log.info("Skipped notification from an excluded project", extra=context)
            return

        states = {}
        if self.settings.skip_ephemeral:
            states["ephemeral thread"] = metadata.ephemeral
        if self.settings.skip_archived:
            states["archived thread"] = metadata.archived
        for name, state in states.items():
            if state is True:
                log.info("Skipped notification due to %s", name, extra=context)
                return

        unknown = [name for name, state in states.items() if state is None]
        if metadata.cwd is None:
            unknown.append("working directory")
        failures = metadata.failures
        if goal_failure is not None:
            unknown.append("Goal state")
            failures = [*failures, goal_failure]
        if unknown:
            action = self.settings.on_lookup_failure
            report = log.error if action == "error" else log.warning
            report(
                "App-server lookup incomplete for %s (on_lookup_failure=%s%s)",
                ", ".join(unknown), action,
                "; " + ", ".join(dict.fromkeys(failures)) if failures else "",
                extra=context,
            )
            if action != "notify":
                return
        return True
