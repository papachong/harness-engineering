from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


DEFAULT_LINEAR_ENDPOINT = "https://api.linear.app/graphql"
DEFAULT_ACTIVE_STATES = ["Todo", "In Progress"]
DEFAULT_TERMINAL_STATES = ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]
DEFAULT_POLL_INTERVAL_MS = 30000
DEFAULT_WORKSPACE_DIRNAME = "symphony_workspaces"
DEFAULT_HOOK_TIMEOUT_MS = 60000
DEFAULT_MAX_CONCURRENT_AGENTS = 10
DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_RETRY_BACKOFF_MS = 300000
DEFAULT_CODEX_COMMAND = "codex app-server"
DEFAULT_TURN_TIMEOUT_MS = 3600000
DEFAULT_READ_TIMEOUT_MS = 5000
DEFAULT_STALL_TIMEOUT_MS = 300000
DEFAULT_PROVIDER_NAME = "codex"
DEFAULT_OPENAI_COMPATIBLE_STYLE = "chat_completions"


@dataclass
class BlockerRef:
    id: Optional[str] = None
    identifier: Optional[str] = None
    state: Optional[str] = None

    @property
    def normalized_state(self) -> str:
        return normalize_state(self.state)


@dataclass
class Issue:
    id: str
    identifier: str
    title: str
    description: Optional[str]
    priority: Optional[int]
    state: str
    branch_name: Optional[str] = None
    url: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    blocked_by: List[BlockerRef] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def normalized_state(self) -> str:
        return normalize_state(self.state)

    def to_template_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "identifier": self.identifier,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "state": self.state,
            "branch_name": self.branch_name,
            "url": self.url,
            "labels": list(self.labels),
            "blocked_by": [
                {"id": blocker.id, "identifier": blocker.identifier, "state": blocker.state}
                for blocker in self.blocked_by
            ],
            "created_at": to_iso(self.created_at),
            "updated_at": to_iso(self.updated_at),
        }


@dataclass
class WorkflowDefinition:
    config: Dict[str, Any]
    prompt_template: str


@dataclass
class TrackerConfig:
    kind: str
    endpoint: str = DEFAULT_LINEAR_ENDPOINT
    api_key: Optional[str] = None
    project_slug: Optional[str] = None
    active_states: List[str] = field(default_factory=lambda: list(DEFAULT_ACTIVE_STATES))
    terminal_states: List[str] = field(default_factory=lambda: list(DEFAULT_TERMINAL_STATES))


@dataclass
class PollingConfig:
    interval_ms: int = DEFAULT_POLL_INTERVAL_MS


@dataclass
class WorkspaceConfig:
    root: Path


@dataclass
class HooksConfig:
    after_create: Optional[str] = None
    before_run: Optional[str] = None
    after_run: Optional[str] = None
    before_remove: Optional[str] = None
    timeout_ms: int = DEFAULT_HOOK_TIMEOUT_MS


@dataclass
class AgentConfig:
    max_concurrent_agents: int = DEFAULT_MAX_CONCURRENT_AGENTS
    max_turns: int = DEFAULT_MAX_TURNS
    max_retry_backoff_ms: int = DEFAULT_MAX_RETRY_BACKOFF_MS
    max_concurrent_agents_by_state: Dict[str, int] = field(default_factory=dict)


@dataclass
class CodexConfig:
    command: str = DEFAULT_CODEX_COMMAND
    approval_policy: Optional[Any] = None
    thread_sandbox: Optional[Any] = None
    turn_sandbox_policy: Optional[Any] = None
    turn_timeout_ms: int = DEFAULT_TURN_TIMEOUT_MS
    read_timeout_ms: int = DEFAULT_READ_TIMEOUT_MS
    stall_timeout_ms: int = DEFAULT_STALL_TIMEOUT_MS


@dataclass
class ModelProviderConfig:
    name: str
    kind: str
    command: Optional[str] = None
    prompt_mode: str = "stdin"
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_style: str = DEFAULT_OPENAI_COMPATIBLE_STYLE
    headers: Dict[str, str] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict)
    approval_policy: Optional[Any] = None
    thread_sandbox: Optional[Any] = None
    turn_sandbox_policy: Optional[Any] = None
    turn_timeout_ms: int = DEFAULT_TURN_TIMEOUT_MS
    read_timeout_ms: int = DEFAULT_READ_TIMEOUT_MS
    stall_timeout_ms: int = DEFAULT_STALL_TIMEOUT_MS


@dataclass
class ServerConfig:
    port: Optional[int] = None


@dataclass
class ServiceConfig:
    tracker: TrackerConfig
    polling: PollingConfig
    workspace: WorkspaceConfig
    hooks: HooksConfig
    agent: AgentConfig
    codex: CodexConfig
    provider_name: str = DEFAULT_PROVIDER_NAME
    providers: Dict[str, ModelProviderConfig] = field(default_factory=dict)
    server: ServerConfig = field(default_factory=ServerConfig)

    @property
    def active_state_set(self) -> set[str]:
        return {normalize_state(value) for value in self.tracker.active_states}

    @property
    def terminal_state_set(self) -> set[str]:
        return {normalize_state(value) for value in self.tracker.terminal_states}

    @property
    def selected_provider(self) -> ModelProviderConfig:
        return self.providers[self.provider_name]


@dataclass
class WorkspaceHandle:
    path: Path
    workspace_key: str
    created_now: bool


@dataclass
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int
    due_at_ms: float
    error: Optional[str] = None
    timer_handle: Any = None


@dataclass
class CodexTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    ended_seconds_running: float = 0.0


@dataclass
class LiveSession:
    session_id: Optional[str] = None
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    codex_app_server_pid: Optional[str] = None
    last_codex_event: Optional[str] = None
    last_codex_timestamp: Optional[datetime] = None
    last_codex_message: Optional[str] = None
    codex_input_tokens: int = 0
    codex_output_tokens: int = 0
    codex_total_tokens: int = 0
    last_reported_input_tokens: int = 0
    last_reported_output_tokens: int = 0
    last_reported_total_tokens: int = 0
    turn_count: int = 0


@dataclass
class RunningEntry:
    issue: Issue
    identifier: str
    provider_name: str
    provider_kind: str
    workspace_path: Path
    task: Any
    retry_attempt: Optional[int]
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    cleanup_on_exit: bool = False
    stop_reason: Optional[str] = None
    live: LiveSession = field(default_factory=LiveSession)
    last_error: Optional[str] = None
    recent_events: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class RunOutcome:
    status: str
    error: Optional[str] = None
    continuation: bool = False


@dataclass
class WorkerControl:
    cancel: Callable[[], None]


def normalize_state(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def to_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
