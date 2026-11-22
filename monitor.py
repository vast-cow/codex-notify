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


@dataclass(frozen=True)
class GoalLookup:
    check: Optional[Check] = None
    failure: Optional[str] = None


@dataclass
class GoalPeriod:
    start: int
    end: int
    awaiting_stop: Optional[int] = None


@dataclass
class GoalExecution:
    goal: Optional[Goal] = None
    active_since: Optional[int] = None
    cleared_at: Optional[int] = None
    periods: list = field(default_factory=list)
    turn_ids: set = field(default_factory=set)
    notification_goal: Optional[Goal] = None
    notified_stops: set = field(default_factory=set)

    def is_stale(self, goal):
        if self.goal is None:
            return self.cleared_at is not None and goal.updated_at < self.cleared_at
        return goal.is_older_than(self.goal)

    def clear_may_precede_snapshot(self):
        previous = self.notification_goal
        return self.goal is not None and (
            self.cleared_at is not None
            or (previous is not None and previous.created_at < self.goal.created_at)
        )

    def update_notification_state(self, goal):
        previous = self.notification_goal
        if goal is not None and previous is not None and goal.is_older_than(previous):
            return False
        if goal is None or previous is None or goal.created_at != previous.created_at or goal.status == "active":
            self.notified_stops.clear()
        self.notification_goal = goal
        return True

    def update(self, goal, observed_at, uncertain_since):
        previous = self.goal
        same_goal = previous is not None and goal is not None and previous.created_at == goal.created_at
        active = goal is not None and goal.status == "active"
        if self.active_since is not None and (not active or not same_goal):
            awaiting_stop = None
            if goal is None:
                end = self.cleared_at if self.cleared_at is not None else observed_at
            else:
                end = goal.updated_at if same_goal else goal.created_at
                if not same_goal:
                    awaiting_stop = previous.created_at
            self.periods.append(GoalPeriod(self.active_since, max(self.active_since, end), awaiting_stop))
            self.active_since = None
        if active and self.active_since is None:
            self.active_since = goal.updated_at if same_goal else goal.created_at
            if same_goal and uncertain_since is not None:
                self.active_since = min(self.active_since, max(previous.updated_at, uncertain_since))
        elif goal is not None and not same_goal:
            self.periods.append(GoalPeriod(goal.created_at, goal.updated_at))
        elif goal is not None and uncertain_since is not None and not active and previous.status != "active":
            # A changed snapshot may cover a resume and another stop before the live updates arrive.
            if goal.updated_at > previous.updated_at:
                start = min(goal.updated_at, max(previous.updated_at, uncertain_since))
                self.periods.append(GoalPeriod(start, goal.updated_at))
        self.goal = goal
        if goal is not None:
            self.cleared_at = None

    def confirm_end(self, goal):
        for period in self.periods:
            if period.awaiting_stop == goal.created_at:
                period.end = max(period.start, min(period.end, goal.updated_at))
                period.awaiting_stop = None

    def turn_membership(self, turn):
        """Return None while the turn might be waiting for a Goal stop update."""
        if turn["id"] in self.turn_ids:
            return True
        started = timestamp(turn.get("startedAt"))
        started = math.floor(started) if started is not None else None
        completed = math.floor(turn["completedAt"])
        duration = turn.get("durationMs")
        if started is None and type(duration) is int and duration >= 0:
            # Allow for both the second-resolution completion and the rounded duration.
            started = completed - duration // 1000 - 1
        uncertain = False
        for period in self.periods:
            if completed >= period.start and (started is None or started <= period.end):
                if period.awaiting_stop is None:
                    return True
                uncertain = True
        if self.active_since is not None and completed >= self.active_since:
            # The row can become visible before the stop update establishes the boundary.
            if completed <= self.goal.updated_at:
                return True
            return None
        return None if uncertain else False


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
        self.goal_events_since = math.floor(started_at)
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
        execution = self.goals.get(thread_id)
        stale = goal is not None and execution is not None and execution.is_stale(goal)
        if not stale:
            lookup = self.goal_lookups.get(thread_id)
            if cleared and execution is not None and execution.clear_may_precede_snapshot():
                # A clear carries no Goal identity. Keep the newer snapshot until another observation.
                if execution.cleared_at is None:
                    execution.cleared_at = math.floor(time.time())
            else:
                uncertain_since = self.disconnected_at if lookup is None else None
                execution = self.record_goal(thread_id, goal, uncertain_since, cleared=cleared)
            self.goal_lookups[thread_id] = GoalLookup(lookup.check if lookup else None)
        if turn_id is not None:
            execution.turn_ids.add(turn_id)
        if goal is None:
            execution.update_notification_state(None)
            return
        known_goals = (execution.goal, execution.notification_goal)
        if not any(known is not None and known.created_at == goal.created_at for known in known_goals):
            return
        # History snapshots may already reflect a later state while live transitions are queued.
        if not execution.update_notification_state(goal):
            return
        if goal.status != "active":
            execution.confirm_end(goal)
        if goal.status not in GOAL_STOP_STATUSES or goal.status in execution.notified_stops:
            return
        # Codex also broadcasts the persisted Goal without a turn ID when a client resumes a thread.
        if turn_id is None and goal.updated_at < self.goal_events_since:
            return
        execution.notified_stops.add(goal.status)
        deadline = asyncio.get_running_loop().time() + HISTORY_TIMEOUT
        task = asyncio.create_task(self.process_goal(thread_id, goal, turn_id, deadline))
        self.goal_notifications.add(task)
        task.add_done_callback(self.goal_notifications.discard)

    def record_goal(self, thread_id, goal, uncertain_since, *, cleared=False):
        observed_at = math.floor(time.time())
        execution = self.goals.setdefault(thread_id, GoalExecution())
        if cleared and execution.cleared_at is None:
            execution.cleared_at = observed_at
        execution.update(goal, observed_at, uncertain_since)
        return execution

    async def sync_goal(self, thread_id, check):
        execution = self.goals.get(thread_id)
        previous = execution.goal if execution else None
        uncertain_since = previous.updated_at if previous is not None else None
        reconnecting = thread_id not in self.goal_lookups and self.disconnected_at is not None
        if reconnecting:
            uncertain_since = self.disconnected_at
        # A live update replaces this marker and takes precedence over the RPC response.
        pending = GoalLookup(check)
        self.goal_lookups[thread_id] = pending
        try:
            goal = await lookup_goal(self.rpc, thread_id)
        except ConnectionLost:
            raise
        except (AppServerError, asyncio.TimeoutError) as exc:
            if self.goal_lookups[thread_id] is pending:
                self.goal_lookups[thread_id] = GoalLookup(check, str(exc) or "thread/goal/get timed out")
        else:
            if self.goal_lookups[thread_id] is pending:
                if goal is None or execution is None or not execution.is_stale(goal):
                    execution = self.record_goal(thread_id, goal, uncertain_since)
                    if reconnecting and (goal is None or goal.status == "active"):
                        execution.update_notification_state(goal)
            elif goal is not None and previous is None:
                cleared_at = self.goals[thread_id].cleared_at
                if cleared_at is not None and goal.updated_at <= cleared_at:
                    # The snapshot supplies evidence of a Goal before the clear without reopening it.
                    end = cleared_at if goal.status == "active" else goal.updated_at
                    self.goals[thread_id].periods.append(GoalPeriod(goal.created_at, end))

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
                if self.disconnected_at is not None:
                    self.goal_events_since = math.floor(time.time())
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
                        if check is not last_check:
                            last_check = check
                            # Queueing behind other threads must not consume the first attempt.
                            deadline = loop.time() + HISTORY_TIMEOUT
                            confirmed = False
                            log.debug("Turn history check started (reconcile=%s)", check.reconcile, extra=context)
                        observed, unresolved = await self.scan_history(
                            thread_id, check, deadline, incomplete_turns,
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

    async def scan_history(self, thread_id, check, deadline, incomplete_turns):
        since = check.since
        confirmed = False
        unresolved = False
        malformed = False
        goal_checked = False
        async for rows in turn_history(self.rpc, thread_id, deadline):
            lookup = self.goal_lookups.get(thread_id)
            if not goal_checked and (lookup is None or lookup.check is not check):
                await self.sync_goal(thread_id, check)
                goal_checked = True
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
                    if not goal_checked:
                        # New history may appear before the corresponding live Goal update.
                        await self.sync_goal(thread_id, check)
                        goal_checked = True
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
        elif self.allow_notification(thread_id, turn["id"], metadata, goal_failure=self.goal_lookups[thread_id].failure):
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
