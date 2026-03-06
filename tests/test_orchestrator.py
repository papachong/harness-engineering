import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace

from symphony.models import Issue, RunOutcome, RunningEntry
from symphony.orchestrator import Orchestrator


class OrchestratorSortTests(unittest.TestCase):
    def test_sort_order_is_priority_then_created_then_identifier(self):
        orchestrator = Orchestrator.__new__(Orchestrator)
        older = datetime.now(timezone.utc) - timedelta(days=2)
        newer = datetime.now(timezone.utc) - timedelta(days=1)
        issues = [
            Issue(id="2", identifier="B-2", title="two", description=None, priority=2, state="Todo", created_at=newer),
            Issue(id="1", identifier="A-1", title="one", description=None, priority=1, state="Todo", created_at=newer),
            Issue(id="3", identifier="A-0", title="zero", description=None, priority=1, state="Todo", created_at=older),
        ]
        sorted_issues = orchestrator._sorted_issues(issues)
        self.assertEqual([issue.id for issue in sorted_issues], ["3", "1", "2"])


class _FakeTask:
    def __init__(self, outcome: RunOutcome, cancelled: bool = False):
        self._outcome = outcome
        self._cancelled = cancelled

    def cancelled(self) -> bool:
        return self._cancelled

    def result(self) -> RunOutcome:
        return self._outcome


class OrchestratorWorkerDoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_completion_without_continuation_releases_claim(self):
        orchestrator = Orchestrator.__new__(Orchestrator)
        issue = Issue(id="1", identifier="A-1", title="one", description=None, priority=1, state="In Progress")
        entry = RunningEntry(
            issue=issue,
            identifier=issue.identifier,
            provider_name="copilot",
            provider_kind="copilot",
            workspace_path=Path("/tmp/a-1"),
            task=None,
            retry_attempt=None,
        )
        orchestrator.running = {issue.id: entry}
        orchestrator.claimed = {issue.id}
        orchestrator.completed = set()
        orchestrator.retry_attempts = {}
        orchestrator.codex_totals = type("Totals", (), {"ended_seconds_running": 0.0})()
        orchestrator.logger = SimpleNamespace(log=lambda *args, **kwargs: None)
        orchestrator._schedule_retry = AsyncMock()
        orchestrator._update_issue_state_safe = AsyncMock()

        task = _FakeTask(RunOutcome(status="normal", continuation=False))
        await orchestrator._on_worker_done(issue.id, task)

        self.assertIn(issue.id, orchestrator.completed)
        self.assertNotIn(issue.id, orchestrator.claimed)
        orchestrator._schedule_retry.assert_not_awaited()

    async def test_normal_completion_without_continuation_updates_done_state(self):
        orchestrator = Orchestrator.__new__(Orchestrator)
        issue = Issue(id="3", identifier="A-3", title="three", description=None, priority=1, state="In Progress")
        entry = RunningEntry(
            issue=issue,
            identifier=issue.identifier,
            provider_name="copilot",
            provider_kind="copilot",
            workspace_path=Path("/tmp/a-3"),
            task=None,
            retry_attempt=None,
        )
        orchestrator.running = {issue.id: entry}
        orchestrator.claimed = {issue.id}
        orchestrator.completed = set()
        orchestrator.retry_attempts = {}
        orchestrator.codex_totals = type("Totals", (), {"ended_seconds_running": 0.0})()
        orchestrator.logger = SimpleNamespace(log=lambda *args, **kwargs: None)
        orchestrator._schedule_retry = AsyncMock()
        orchestrator._update_issue_state_safe = AsyncMock()

        task = _FakeTask(RunOutcome(status="normal", continuation=False))
        await orchestrator._on_worker_done(issue.id, task)

        orchestrator._update_issue_state_safe.assert_awaited_once_with(issue.id, "Done", context="completed")

    async def test_normal_completion_with_continuation_schedules_retry(self):
        orchestrator = Orchestrator.__new__(Orchestrator)
        issue = Issue(id="2", identifier="A-2", title="two", description=None, priority=1, state="In Progress")
        entry = RunningEntry(
            issue=issue,
            identifier=issue.identifier,
            provider_name="copilot",
            provider_kind="copilot",
            workspace_path=Path("/tmp/a-2"),
            task=None,
            retry_attempt=None,
        )
        orchestrator.running = {issue.id: entry}
        orchestrator.claimed = {issue.id}
        orchestrator.completed = set()
        orchestrator.retry_attempts = {}
        orchestrator.codex_totals = type("Totals", (), {"ended_seconds_running": 0.0})()
        orchestrator.logger = SimpleNamespace(log=lambda *args, **kwargs: None)
        orchestrator._schedule_retry = AsyncMock()

        task = _FakeTask(RunOutcome(status="normal", continuation=True))
        await orchestrator._on_worker_done(issue.id, task)

        self.assertIn(issue.id, orchestrator.completed)
        self.assertIn(issue.id, orchestrator.claimed)
        orchestrator._schedule_retry.assert_awaited_once_with(issue.id, entry, error=None, continuation=True)

    async def test_failed_completion_updates_terminated_state_before_retry(self):
        orchestrator = Orchestrator.__new__(Orchestrator)
        issue = Issue(id="5", identifier="A-5", title="five", description=None, priority=1, state="In Progress")
        entry = RunningEntry(
            issue=issue,
            identifier=issue.identifier,
            provider_name="copilot",
            provider_kind="copilot",
            workspace_path=Path("/tmp/a-5"),
            task=None,
            retry_attempt=None,
        )
        orchestrator.running = {issue.id: entry}
        orchestrator.claimed = {issue.id}
        orchestrator.completed = set()
        orchestrator.retry_attempts = {}
        orchestrator.codex_totals = type("Totals", (), {"ended_seconds_running": 0.0})()
        orchestrator.logger = SimpleNamespace(log=lambda *args, **kwargs: None)
        orchestrator._schedule_retry = AsyncMock()
        orchestrator._update_issue_state_safe = AsyncMock()

        task = _FakeTask(RunOutcome(status="failed", error="boom", continuation=False))
        await orchestrator._on_worker_done(issue.id, task)

        orchestrator._schedule_retry.assert_awaited_once_with(issue.id, entry, error="boom", continuation=False)


class OrchestratorRetryStateUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_retry_updates_terminated_state(self):
        orchestrator = Orchestrator.__new__(Orchestrator)
        issue = Issue(id="6", identifier="A-6", title="six", description=None, priority=1, state="In Progress")
        entry = RunningEntry(
            issue=issue,
            identifier=issue.identifier,
            provider_name="copilot",
            provider_kind="copilot",
            workspace_path=Path("/tmp/a-6"),
            task=None,
            retry_attempt=None,
        )
        orchestrator.retry_attempts = {}
        orchestrator.logger = SimpleNamespace(log=lambda *args, **kwargs: None)
        orchestrator._update_issue_state_safe = AsyncMock()
        orchestrator.config = SimpleNamespace(
            agent=SimpleNamespace(max_retry_backoff_ms=300000),
        )

        await orchestrator._schedule_retry(issue.id, entry, error="boom", continuation=False)

        orchestrator._update_issue_state_safe.assert_awaited_once_with(issue.id, "Terminated", context="retry")
        self.assertIn(issue.id, orchestrator.retry_attempts)

    async def test_continuation_retry_does_not_update_terminated_state(self):
        orchestrator = Orchestrator.__new__(Orchestrator)
        issue = Issue(id="7", identifier="A-7", title="seven", description=None, priority=1, state="In Progress")
        entry = RunningEntry(
            issue=issue,
            identifier=issue.identifier,
            provider_name="copilot",
            provider_kind="copilot",
            workspace_path=Path("/tmp/a-7"),
            task=None,
            retry_attempt=None,
        )
        orchestrator.retry_attempts = {}
        orchestrator.logger = SimpleNamespace(log=lambda *args, **kwargs: None)
        orchestrator._update_issue_state_safe = AsyncMock()
        orchestrator.config = SimpleNamespace(
            agent=SimpleNamespace(max_retry_backoff_ms=300000),
        )

        await orchestrator._schedule_retry(issue.id, entry, error=None, continuation=True)

        orchestrator._update_issue_state_safe.assert_not_awaited()
        self.assertIn(issue.id, orchestrator.retry_attempts)


class OrchestratorDispatchStateUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_updates_in_progress_state(self):
        orchestrator = Orchestrator.__new__(Orchestrator)
        issue = Issue(id="4", identifier="A-4", title="four", description=None, priority=1, state="Todo")
        orchestrator.running = {}
        orchestrator.claimed = set()
        orchestrator.retry_attempts = {}
        orchestrator.config = SimpleNamespace(
            provider_name="copilot",
            selected_provider=SimpleNamespace(kind="copilot"),
            workspace=SimpleNamespace(root=Path("/tmp")),
        )
        orchestrator.workspace_manager = object()
        orchestrator.logger = SimpleNamespace(log=lambda *args, **kwargs: None)
        orchestrator.workflow_loader = SimpleNamespace(definition=SimpleNamespace(prompt_template=""))
        orchestrator.tracker_client = None
        orchestrator._on_worker_done = AsyncMock()
        orchestrator._update_issue_state_safe = AsyncMock()

        class _FakeAgentRunner:
            def __init__(self, cfg, tracker_client, wm, logger):
                self.prompt_template = ""

            async def run_attempt(self, issue, attempt, on_event):
                return RunOutcome(status="normal")

        import symphony.orchestrator as orchestrator_module

        original_runner = orchestrator_module.AgentRunner
        original_create_task = orchestrator_module.asyncio.create_task
        fake_tasks = []

        class _TaskStub:
            def add_done_callback(self, _callback):
                return None

        def _fake_create_task(coro):
            fake_tasks.append(coro)
            return _TaskStub()

        try:
            orchestrator_module.AgentRunner = _FakeAgentRunner
            orchestrator_module.asyncio.create_task = _fake_create_task
            orchestrator._dispatch_issue(issue, attempt=None)
        finally:
            orchestrator_module.AgentRunner = original_runner
            orchestrator_module.asyncio.create_task = original_create_task

        self.assertIn(issue.id, orchestrator.running)
        self.assertIn(issue.id, orchestrator.claimed)
        self.assertGreaterEqual(len(fake_tasks), 2)
        if hasattr(fake_tasks[0], "close"):
            fake_tasks[0].close()
        await fake_tasks[1]
        orchestrator._update_issue_state_safe.assert_awaited_once_with(issue.id, "In Progress", context="dispatch")


if __name__ == "__main__":
    unittest.main()
