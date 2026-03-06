from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .agent import AgentRunner
from .errors import ConfigValidationError, TrackerError
from .logging_utils import log_kv
from .models import CodexTotals, Issue, RetryEntry, RunningEntry, RunOutcome, ServiceConfig, normalize_state, to_iso
from .tracker import build_tracker_client
from .workflow import WorkflowLoader
from .workspace import WorkspaceManager, sanitize_workspace_key


class Orchestrator:
    def __init__(self, workflow_loader: WorkflowLoader, logger: logging.Logger, port_override: int | None = None):
        self.workflow_loader = workflow_loader
        self.logger = logger
        self.port_override = port_override
        self.config: ServiceConfig | None = None
        self.tracker_client: Any = None
        self.workspace_manager: WorkspaceManager | None = None
        self.running: Dict[str, RunningEntry] = {}
        self.claimed: set[str] = set()
        self.retry_attempts: Dict[str, RetryEntry] = {}
        self.completed: set[str] = set()
        self.codex_totals = CodexTotals()
        self.codex_rate_limits: Optional[Dict[str, Any]] = None
        self._stop_event = asyncio.Event()
        self._refresh_event = asyncio.Event()
        self._workflow_watch_task: Optional[asyncio.Task] = None
        self._status_server: Any = None

    @property
    def cfg(self) -> ServiceConfig:
        assert self.config is not None
        return self.config

    @property
    def wm(self) -> WorkspaceManager:
        assert self.workspace_manager is not None
        return self.workspace_manager

    async def start(self, status_server_factory: Any = None) -> None:
        loaded = self.workflow_loader.load()
        self._apply_loaded_workflow(loaded.config)
        self.workflow_loader.validate_dispatch_config()
        await self._startup_terminal_workspace_cleanup()
        if status_server_factory is not None:
            port = self.port_override if self.port_override is not None else self.cfg.server.port
            if port is not None:
                self._status_server = status_server_factory(self, port, self.logger)
                self._status_server.start()
        self._workflow_watch_task = asyncio.create_task(self._watch_workflow())
        log_kv(self.logger, logging.INFO, "service_started", workflow_path=str(self.workflow_loader.workflow_path))
        while not self._stop_event.is_set():
            await self.on_tick()
            try:
                await asyncio.wait_for(self._refresh_event.wait(), timeout=self.cfg.polling.interval_ms / 1000)
                self._refresh_event.clear()
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop_event.set()
        if self._workflow_watch_task:
            self._workflow_watch_task.cancel()
        if self._status_server:
            self._status_server.stop()
        for issue_id in list(self.running.keys()):
            await self._terminate_running_issue(issue_id, cleanup_workspace=False, stop_reason="shutdown")

    async def on_tick(self) -> None:
        await self.reconcile_running_issues()
        try:
            reloaded = self.workflow_loader.reload_if_changed()
            if reloaded is not None:
                self._apply_loaded_workflow(reloaded.config)
                log_kv(self.logger, logging.INFO, "workflow_reloaded", workflow_path=str(self.workflow_loader.workflow_path))
            self.workflow_loader.validate_dispatch_config()
        except Exception as exc:  # noqa: BLE001
            log_kv(self.logger, logging.ERROR, "validation_failed", error=str(exc))
            return
        try:
            issues = await asyncio.to_thread(self.tracker_client.fetch_candidate_issues)
        except Exception as exc:  # noqa: BLE001
            log_kv(self.logger, logging.ERROR, "candidate_fetch_failed", error=str(exc))
            return
        for issue in self._sorted_issues(issues):
            if not self._has_available_slots():
                break
            if self._should_dispatch(issue):
                self._dispatch_issue(issue, attempt=None)

    async def reconcile_running_issues(self) -> None:
        if not self.running:
            return
        stall_timeout_ms = self.cfg.selected_provider.stall_timeout_ms
        if stall_timeout_ms > 0:
            now = datetime.now(timezone.utc)
            for issue_id, entry in list(self.running.items()):
                last = entry.live.last_codex_timestamp or entry.started_at
                elapsed_ms = (now - last).total_seconds() * 1000
                if elapsed_ms > stall_timeout_ms:
                    log_kv(self.logger, logging.WARNING, "session_stalled", issue_id=issue_id, issue_identifier=entry.identifier)
                    await self._terminate_running_issue(issue_id, cleanup_workspace=False, stop_reason="stalled")
        running_ids = list(self.running.keys())
        if not running_ids:
            return
        try:
            refreshed = await asyncio.to_thread(self.tracker_client.fetch_issue_states_by_ids, running_ids)
        except Exception as exc:  # noqa: BLE001
            log_kv(self.logger, logging.WARNING, "reconcile_refresh_failed", error=str(exc))
            return
        refreshed_map = {issue.id: issue for issue in refreshed}
        for issue_id, entry in list(self.running.items()):
            current = refreshed_map.get(issue_id)
            if current is None:
                continue
            if current.normalized_state in self.cfg.terminal_state_set:
                await self._terminate_running_issue(issue_id, cleanup_workspace=True, stop_reason="terminal")
            elif current.normalized_state in self.cfg.active_state_set:
                entry.issue = current
            else:
                await self._terminate_running_issue(issue_id, cleanup_workspace=False, stop_reason="non_active")

    async def request_refresh(self) -> None:
        self._refresh_event.set()

    def snapshot(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        seconds_running = self.codex_totals.ended_seconds_running + sum((now - entry.started_at).total_seconds() for entry in self.running.values())
        running_rows = []
        for issue_id, entry in self.running.items():
            provider_summary = entry.last_error or entry.live.last_codex_message or ""
            running_rows.append(
                {
                    "issue_id": issue_id,
                    "issue_identifier": entry.identifier,
                    "provider": {"name": entry.provider_name, "kind": entry.provider_kind},
                    "provider_summary": provider_summary,
                    "state": entry.issue.state,
                    "session_id": entry.live.session_id,
                    "turn_count": entry.live.turn_count,
                    "last_event": entry.live.last_codex_event,
                    "last_message": entry.live.last_codex_message,
                    "started_at": to_iso(entry.started_at),
                    "last_event_at": to_iso(entry.live.last_codex_timestamp),
                    "tokens": {
                        "input_tokens": entry.live.codex_input_tokens,
                        "output_tokens": entry.live.codex_output_tokens,
                        "total_tokens": entry.live.codex_total_tokens,
                    },
                }
            )
        retry_rows = [
            {
                "issue_id": retry.issue_id,
                "issue_identifier": retry.identifier,
                "attempt": retry.attempt,
                "due_at": to_iso(datetime.fromtimestamp(retry.due_at_ms / 1000, tz=timezone.utc)),
                "error": retry.error,
                "provider_summary": retry.error or "",
            }
            for retry in self.retry_attempts.values()
        ]
        return {
            "generated_at": to_iso(now),
            "provider": {
                "selected": self.cfg.provider_name,
                "kind": self.cfg.selected_provider.kind,
                "available": sorted(self.cfg.providers.keys()),
            },
            "counts": {"running": len(running_rows), "retrying": len(retry_rows)},
            "running": running_rows,
            "retrying": retry_rows,
            "codex_totals": {
                "input_tokens": self.codex_totals.input_tokens,
                "output_tokens": self.codex_totals.output_tokens,
                "total_tokens": self.codex_totals.total_tokens,
                "seconds_running": round(seconds_running, 3),
            },
            "rate_limits": self.codex_rate_limits,
        }

    def issue_snapshot(self, issue_identifier: str) -> Optional[Dict[str, Any]]:
        for issue_id, entry in self.running.items():
            if entry.identifier == issue_identifier:
                return {
                    "issue_identifier": issue_identifier,
                    "issue_id": issue_id,
                    "status": "running",
                    "workspace": {"path": str(entry.workspace_path)},
                    "attempts": {
                        "restart_count": 0 if entry.retry_attempt is None else entry.retry_attempt,
                        "current_retry_attempt": 0 if entry.retry_attempt is None else entry.retry_attempt,
                    },
                    "provider": {"name": entry.provider_name, "kind": entry.provider_kind},
                    "provider_summary": entry.last_error or entry.live.last_codex_message,
                    "running": {
                        "session_id": entry.live.session_id,
                        "turn_count": entry.live.turn_count,
                        "state": entry.issue.state,
                        "started_at": to_iso(entry.started_at),
                        "last_event": entry.live.last_codex_event,
                        "last_message": entry.live.last_codex_message,
                        "last_event_at": to_iso(entry.live.last_codex_timestamp),
                        "tokens": {
                            "input_tokens": entry.live.codex_input_tokens,
                            "output_tokens": entry.live.codex_output_tokens,
                            "total_tokens": entry.live.codex_total_tokens,
                        },
                    },
                    "retry": None,
                    "recent_events": list(entry.recent_events[-20:]),
                    "last_error": entry.last_error,
                    "tracked": {},
                }
        for retry in self.retry_attempts.values():
            if retry.identifier == issue_identifier:
                return {
                    "issue_identifier": issue_identifier,
                    "issue_id": retry.issue_id,
                    "status": "retrying",
                    "workspace": {"path": str(self.cfg.workspace.root / sanitize_workspace_key(issue_identifier))},
                    "attempts": {"restart_count": retry.attempt, "current_retry_attempt": retry.attempt},
                    "provider": {"name": self.cfg.provider_name, "kind": self.cfg.selected_provider.kind},
                    "provider_summary": retry.error,
                    "running": None,
                    "retry": {
                        "attempt": retry.attempt,
                        "due_at": to_iso(datetime.fromtimestamp(retry.due_at_ms / 1000, tz=timezone.utc)),
                        "error": retry.error,
                    },
                    "recent_events": [],
                    "last_error": retry.error,
                    "tracked": {},
                }
        return None

    def _apply_loaded_workflow(self, config: ServiceConfig) -> None:
        self.config = config
        self.tracker_client = build_tracker_client(config)
        self.workspace_manager = WorkspaceManager(config.workspace.root, config.hooks, self.logger)

    def _sorted_issues(self, issues: List[Issue]) -> List[Issue]:
        def sort_key(issue: Issue) -> Any:
            created = issue.created_at or datetime.max.replace(tzinfo=timezone.utc)
            priority = issue.priority if issue.priority is not None else 999
            return (priority, created, issue.identifier)

        return sorted(issues, key=sort_key)

    def _should_dispatch(self, issue: Issue) -> bool:
        if not all([issue.id, issue.identifier, issue.title, issue.state]):
            return False
        if issue.id in self.running or issue.id in self.claimed:
            return False
        state = issue.normalized_state
        if state not in self.cfg.active_state_set or state in self.cfg.terminal_state_set:
            return False
        if not self._state_slot_available(state):
            return False
        if state == normalize_state("Todo"):
            for blocker in issue.blocked_by:
                if blocker.normalized_state and blocker.normalized_state not in self.cfg.terminal_state_set:
                    return False
        return True

    def _has_available_slots(self) -> bool:
        return len(self.running) < self.cfg.agent.max_concurrent_agents

    def _state_slot_available(self, normalized_state: str) -> bool:
        limit = self.cfg.agent.max_concurrent_agents_by_state.get(normalized_state, self.cfg.agent.max_concurrent_agents)
        current = sum(1 for entry in self.running.values() if entry.issue.normalized_state == normalized_state)
        return current < limit

    def _dispatch_issue(self, issue: Issue, attempt: int | None) -> None:
        async def on_event(payload: Dict[str, Any]) -> None:
            await self._handle_agent_event(issue.id, payload)

        runner = AgentRunner(self.cfg, self.tracker_client, self.wm, self.logger)
        runner.prompt_template = self.workflow_loader.definition.prompt_template
        task = asyncio.create_task(runner.run_attempt(issue, attempt, on_event))
        workspace_path = (self.cfg.workspace.root / sanitize_workspace_key(issue.identifier)).resolve()
        entry = RunningEntry(
            issue=issue,
            identifier=issue.identifier,
            provider_name=self.cfg.provider_name,
            provider_kind=self.cfg.selected_provider.kind,
            workspace_path=workspace_path,
            task=task,
            retry_attempt=attempt,
        )
        self.running[issue.id] = entry
        self.claimed.add(issue.id)
        retry_entry = self.retry_attempts.pop(issue.id, None)
        if retry_entry and retry_entry.timer_handle:
            retry_entry.timer_handle.cancel()
        task.add_done_callback(lambda finished, issue_id=issue.id: asyncio.create_task(self._on_worker_done(issue_id, finished)))
        asyncio.create_task(self._update_issue_state_safe(issue.id, "In Progress", context="dispatch"))
        log_kv(self.logger, logging.INFO, "issue_dispatched", issue_id=issue.id, issue_identifier=issue.identifier, attempt=attempt)

    async def _handle_agent_event(self, issue_id: str, payload: Dict[str, Any]) -> None:
        entry = self.running.get(issue_id)
        if entry is None:
            return
        event_name = payload.get("event")
        entry.live.last_codex_event = event_name
        entry.live.last_codex_timestamp = _parse_iso(payload.get("timestamp")) or datetime.now(timezone.utc)
        entry.live.last_codex_message = payload.get("message")
        if payload.get("session_id"):
            entry.live.session_id = payload.get("session_id")
            entry.live.thread_id = payload.get("thread_id")
            entry.live.turn_id = payload.get("turn_id")
            entry.live.codex_app_server_pid = payload.get("codex_app_server_pid")
        if event_name == "session_started":
            entry.live.turn_count += 1
        usage = payload.get("usage") or {}
        if usage:
            input_total = int(usage.get("input_tokens", 0))
            output_total = int(usage.get("output_tokens", 0))
            total_total = int(usage.get("total_tokens", 0))
            delta_input = max(0, input_total - entry.live.last_reported_input_tokens)
            delta_output = max(0, output_total - entry.live.last_reported_output_tokens)
            delta_total = max(0, total_total - entry.live.last_reported_total_tokens)
            self.codex_totals.input_tokens += delta_input
            self.codex_totals.output_tokens += delta_output
            self.codex_totals.total_tokens += delta_total
            entry.live.last_reported_input_tokens = input_total
            entry.live.last_reported_output_tokens = output_total
            entry.live.last_reported_total_tokens = total_total
            entry.live.codex_input_tokens = input_total
            entry.live.codex_output_tokens = output_total
            entry.live.codex_total_tokens = total_total
        if payload.get("rate_limits") is not None:
            self.codex_rate_limits = payload.get("rate_limits")
        entry.recent_events.append({"at": payload.get("timestamp"), "event": event_name, "message": payload.get("message")})
        if len(entry.recent_events) > 50:
            entry.recent_events = entry.recent_events[-50:]

    async def _on_worker_done(self, issue_id: str, task: asyncio.Task) -> None:
        entry = self.running.pop(issue_id, None)
        if entry is None:
            return
        self.codex_totals.ended_seconds_running += max(0.0, (datetime.now(timezone.utc) - entry.started_at).total_seconds())
        stop_reason = entry.stop_reason
        if entry.cleanup_on_exit:
            try:
                await asyncio.to_thread(self.wm.remove_for_issue, entry.identifier)
            except Exception as exc:  # noqa: BLE001
                log_kv(self.logger, logging.WARNING, "workspace_cleanup_failed", issue_id=issue_id, issue_identifier=entry.identifier, error=str(exc))
        if task.cancelled():
            if stop_reason in {"terminal", "non_active", "shutdown"}:
                self.claimed.discard(issue_id)
                return
            await self._schedule_retry(issue_id, entry, error=stop_reason or "worker_cancelled", continuation=False)
            return
        outcome: RunOutcome = task.result()
        if stop_reason in {"terminal", "non_active", "shutdown"}:
            self.claimed.discard(issue_id)
            return
        if stop_reason == "stalled":
            await self._schedule_retry(issue_id, entry, error="session_stalled", continuation=False)
            return
        if outcome.status == "normal":
            self.completed.add(issue_id)
            if outcome.continuation:
                await self._schedule_retry(issue_id, entry, error=None, continuation=True)
            else:
                await self._update_issue_state_safe(issue_id, "Done", context="completed")
                self.claimed.discard(issue_id)
        else:
            entry.last_error = outcome.error
            await self._schedule_retry(issue_id, entry, error=outcome.error or "worker_failed", continuation=False)

    async def _update_issue_state_safe(self, issue_id: str, state_name: str, context: str) -> None:
        try:
            await asyncio.to_thread(self.tracker_client.update_issue_state, issue_id, state_name)
        except Exception as exc:  # noqa: BLE001
            log_kv(
                self.logger,
                logging.WARNING,
                "issue_state_update_failed",
                issue_id=issue_id,
                state=state_name,
                context=context,
                error=str(exc),
            )

    async def _schedule_retry(self, issue_id: str, entry: RunningEntry, error: str | None, continuation: bool) -> None:
        current = self.retry_attempts.pop(issue_id, None)
        if current and current.timer_handle:
            current.timer_handle.cancel()
        if not continuation:
            await self._update_issue_state_safe(issue_id, "Terminated", context="retry")
        attempt = 1 if continuation else ((entry.retry_attempt or 0) + 1)
        delay_ms = 1000 if continuation else min(10000 * (2 ** max(attempt - 1, 0)), self.cfg.agent.max_retry_backoff_ms)
        loop = asyncio.get_running_loop()
        due_at_ms = loop.time() * 1000 + delay_ms
        identifier = entry.identifier
        handle = loop.call_later(delay_ms / 1000, lambda: asyncio.create_task(self._on_retry_timer(issue_id)))
        self.retry_attempts[issue_id] = RetryEntry(issue_id=issue_id, identifier=identifier, attempt=attempt, due_at_ms=due_at_ms, error=error, timer_handle=handle)
        log_kv(self.logger, logging.INFO, "retry_scheduled", issue_id=issue_id, issue_identifier=identifier, attempt=attempt, delay_ms=delay_ms, error=error)

    async def _on_retry_timer(self, issue_id: str) -> None:
        retry_entry = self.retry_attempts.pop(issue_id, None)
        if retry_entry is None:
            return
        try:
            issues = await asyncio.to_thread(self.tracker_client.fetch_candidate_issues)
        except Exception as exc:  # noqa: BLE001
            entry = RunningEntry(
                issue=Issue(id=issue_id, identifier=retry_entry.identifier, title=retry_entry.identifier, description=None, priority=None, state=""),
                identifier=retry_entry.identifier,
                provider_name=self.cfg.provider_name,
                provider_kind=self.cfg.selected_provider.kind,
                workspace_path=self.cfg.workspace.root / sanitize_workspace_key(retry_entry.identifier),
                task=None,
                retry_attempt=retry_entry.attempt,
            )
            await self._schedule_retry(issue_id, entry, error="retry poll failed", continuation=False)
            log_kv(self.logger, logging.WARNING, "retry_poll_failed", issue_id=issue_id, issue_identifier=retry_entry.identifier, error=str(exc))
            return
        match = next((issue for issue in issues if issue.id == issue_id), None)
        if match is None:
            self.claimed.discard(issue_id)
            return
        if not self._should_retry_issue(match):
            self.claimed.discard(issue_id)
            return
        if not self._has_available_slots() or not self._state_slot_available(match.normalized_state):
            entry = RunningEntry(
                issue=match,
                identifier=match.identifier,
                provider_name=self.cfg.provider_name,
                provider_kind=self.cfg.selected_provider.kind,
                workspace_path=self.cfg.workspace.root / sanitize_workspace_key(match.identifier),
                task=None,
                retry_attempt=retry_entry.attempt,
            )
            await self._schedule_retry(issue_id, entry, error="no available orchestrator slots", continuation=False)
            return
        self._dispatch_issue(match, attempt=retry_entry.attempt)

    def _should_retry_issue(self, issue: Issue) -> bool:
        state = issue.normalized_state
        return state in self.cfg.active_state_set and state not in self.cfg.terminal_state_set

    async def _terminate_running_issue(self, issue_id: str, cleanup_workspace: bool, stop_reason: str) -> None:
        entry = self.running.get(issue_id)
        if entry is None:
            return
        entry.cleanup_on_exit = cleanup_workspace
        entry.stop_reason = stop_reason
        entry.task.cancel()

    async def _startup_terminal_workspace_cleanup(self) -> None:
        try:
            terminal_issues = await asyncio.to_thread(self.tracker_client.fetch_issues_by_states, self.cfg.tracker.terminal_states)
        except Exception as exc:  # noqa: BLE001
            log_kv(self.logger, logging.WARNING, "startup_cleanup_failed", error=str(exc))
            return
        for issue in terminal_issues:
            try:
                await asyncio.to_thread(self.wm.remove_for_issue, issue.identifier)
            except FileNotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001
                log_kv(self.logger, logging.WARNING, "startup_cleanup_issue_failed", issue_id=issue.id, issue_identifier=issue.identifier, error=str(exc))

    async def _watch_workflow(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(1)
            try:
                reloaded = self.workflow_loader.reload_if_changed()
            except Exception as exc:  # noqa: BLE001
                log_kv(self.logger, logging.ERROR, "workflow_reload_failed", error=str(exc))
                continue
            if reloaded is not None:
                try:
                    self._apply_loaded_workflow(reloaded.config)
                    log_kv(self.logger, logging.INFO, "workflow_reloaded", workflow_path=str(self.workflow_loader.workflow_path))
                except Exception as exc:  # noqa: BLE001
                    log_kv(self.logger, logging.ERROR, "workflow_apply_failed", error=str(exc))


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
