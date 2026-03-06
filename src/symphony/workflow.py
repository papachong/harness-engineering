from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .errors import ConfigValidationError, MissingWorkflowFileError, WorkflowFrontMatterNotAMapError, WorkflowParseError
from .models import (
    DEFAULT_ACTIVE_STATES,
    DEFAULT_HOOK_TIMEOUT_MS,
    DEFAULT_LINEAR_ENDPOINT,
    DEFAULT_MAX_CONCURRENT_AGENTS,
    DEFAULT_MAX_RETRY_BACKOFF_MS,
    DEFAULT_MAX_TURNS,
    DEFAULT_POLL_INTERVAL_MS,
    DEFAULT_PROVIDER_NAME,
    DEFAULT_READ_TIMEOUT_MS,
    DEFAULT_STALL_TIMEOUT_MS,
    DEFAULT_TERMINAL_STATES,
    DEFAULT_TURN_TIMEOUT_MS,
    DEFAULT_WORKSPACE_DIRNAME,
    AgentConfig,
    CodexConfig,
    HooksConfig,
    ModelProviderConfig,
    PollingConfig,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    WorkflowDefinition,
    WorkspaceConfig,
    normalize_state,
)


@dataclass
class LoadedWorkflow:
    definition: WorkflowDefinition
    config: ServiceConfig
    mtime_ns: int


class WorkflowLoader:
    def __init__(self, workflow_path: str | Path | None = None):
        self.workflow_path = Path(workflow_path or Path.cwd() / "WORKFLOW.md").expanduser()
        self._loaded: Optional[LoadedWorkflow] = None
        self.last_error: Optional[Exception] = None

    @property
    def definition(self) -> WorkflowDefinition:
        return self.require_loaded().definition

    @property
    def config(self) -> ServiceConfig:
        return self.require_loaded().config

    def require_loaded(self) -> LoadedWorkflow:
        if self._loaded is None:
            raise MissingWorkflowFileError(f"Workflow not loaded: {self.workflow_path}")
        return self._loaded

    def load(self) -> LoadedWorkflow:
        definition, mtime_ns = load_workflow_definition(self.workflow_path)
        config = build_service_config(definition.config)
        loaded = LoadedWorkflow(definition=definition, config=config, mtime_ns=mtime_ns)
        self._loaded = loaded
        self.last_error = None
        return loaded

    def reload_if_changed(self) -> LoadedWorkflow | None:
        try:
            mtime_ns = self.workflow_path.stat().st_mtime_ns
        except FileNotFoundError as exc:
            self.last_error = exc
            if self._loaded is None:
                raise MissingWorkflowFileError(str(self.workflow_path)) from exc
            return None
        if self._loaded and self._loaded.mtime_ns == mtime_ns:
            return None
        try:
            return self.load()
        except Exception as exc:  # noqa: BLE001
            self.last_error = exc
            if self._loaded is None:
                raise
            return None

    def validate_dispatch_config(self) -> None:
        config = self.config
        if not config.tracker.kind:
            raise ConfigValidationError("tracker.kind is required")
        if config.tracker.kind != "linear":
            raise ConfigValidationError("tracker.kind must be 'linear'")
        if not config.tracker.api_key:
            raise ConfigValidationError("tracker.api_key is required")
        if not config.tracker.project_slug:
            raise ConfigValidationError("tracker.project_slug is required")
        provider = config.selected_provider
        if provider.kind == "codex" and not (provider.command or "").strip():
            raise ConfigValidationError("selected codex provider requires command")
        if provider.kind == "openai-compatible":
            if not provider.base_url:
                raise ConfigValidationError("selected openai-compatible provider requires base_url")
            if not provider.model:
                raise ConfigValidationError("selected openai-compatible provider requires model")
            if not provider.api_key:
                raise ConfigValidationError("selected openai-compatible provider requires api_key")
        if provider.kind in {"command", "claudecode", "copilot"} and not (provider.command or "").strip():
            raise ConfigValidationError("selected command provider requires command")


def load_workflow_definition(path: Path) -> tuple[WorkflowDefinition, int]:
    if not path.exists():
        raise MissingWorkflowFileError(str(path))
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MissingWorkflowFileError(str(path)) from exc
    try:
        config, prompt = _split_front_matter(content)
    except WorkflowFrontMatterNotAMapError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WorkflowParseError(str(exc)) from exc
    return WorkflowDefinition(config=config, prompt_template=prompt.strip()), path.stat().st_mtime_ns


def _split_front_matter(content: str) -> tuple[Dict[str, Any], str]:
    if not content.startswith("---"):
        return {}, content
    lines = content.splitlines()
    end_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break
    if end_index is None:
        raise WorkflowParseError("Unterminated YAML front matter")
    yaml_text = "\n".join(lines[1:end_index])
    body = "\n".join(lines[end_index + 1 :])
    try:
        parsed = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
    except yaml.YAMLError as exc:
        raise WorkflowParseError(str(exc)) from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise WorkflowFrontMatterNotAMapError("Workflow front matter must decode to a map")
    return parsed, body


def build_service_config(config_map: Dict[str, Any]) -> ServiceConfig:
    tracker_map = _as_dict(config_map.get("tracker"))
    polling_map = _as_dict(config_map.get("polling"))
    workspace_map = _as_dict(config_map.get("workspace"))
    hooks_map = _as_dict(config_map.get("hooks"))
    agent_map = _as_dict(config_map.get("agent"))
    codex_map = _as_dict(config_map.get("codex"))
    model_map = _as_dict(config_map.get("model"))
    providers_map = _as_dict(config_map.get("providers"))
    server_map = _as_dict(config_map.get("server"))

    tracker_kind = str(tracker_map.get("kind") or "").strip()
    tracker = TrackerConfig(
        kind=tracker_kind,
        endpoint=str(tracker_map.get("endpoint") or DEFAULT_LINEAR_ENDPOINT).strip() or DEFAULT_LINEAR_ENDPOINT,
        api_key=_resolve_secret(tracker_map.get("api_key"), canonical_env="LINEAR_API_KEY"),
        project_slug=_none_if_blank(tracker_map.get("project_slug")),
        active_states=_coerce_states(tracker_map.get("active_states"), DEFAULT_ACTIVE_STATES),
        terminal_states=_coerce_states(tracker_map.get("terminal_states"), DEFAULT_TERMINAL_STATES),
    )

    workspace_root = _resolve_path_like(workspace_map.get("root"))
    if workspace_root is None:
        workspace_root = Path(tempfile.gettempdir()) / DEFAULT_WORKSPACE_DIRNAME

    hooks_timeout = _coerce_positive_int(hooks_map.get("timeout_ms"), DEFAULT_HOOK_TIMEOUT_MS)

    agent = AgentConfig(
        max_concurrent_agents=_coerce_positive_int(agent_map.get("max_concurrent_agents"), DEFAULT_MAX_CONCURRENT_AGENTS),
        max_turns=_coerce_positive_int(agent_map.get("max_turns"), DEFAULT_MAX_TURNS),
        max_retry_backoff_ms=_coerce_positive_int(agent_map.get("max_retry_backoff_ms"), DEFAULT_MAX_RETRY_BACKOFF_MS),
        max_concurrent_agents_by_state=_coerce_state_limits(agent_map.get("max_concurrent_agents_by_state")),
    )

    codex = CodexConfig(
        command=str(codex_map.get("command") or "codex app-server"),
        approval_policy=codex_map.get("approval_policy"),
        thread_sandbox=codex_map.get("thread_sandbox"),
        turn_sandbox_policy=codex_map.get("turn_sandbox_policy"),
        turn_timeout_ms=_coerce_positive_int(codex_map.get("turn_timeout_ms"), DEFAULT_TURN_TIMEOUT_MS),
        read_timeout_ms=_coerce_positive_int(codex_map.get("read_timeout_ms"), DEFAULT_READ_TIMEOUT_MS),
        stall_timeout_ms=_coerce_int(codex_map.get("stall_timeout_ms"), DEFAULT_STALL_TIMEOUT_MS),
    )

    env_provider_name = _none_if_blank(os.environ.get("SYMPHONY_MODEL_PROVIDER")) or _none_if_blank(
        os.environ.get("SYMPHONY_PROVIDER")
    )
    configured_provider_name = (
        _none_if_blank(model_map.get("provider"))
        or _none_if_blank(config_map.get("provider"))
        or DEFAULT_PROVIDER_NAME
    )
    providers = _build_provider_map(providers_map, codex)
    provider_name = env_provider_name if env_provider_name in providers else configured_provider_name
    if provider_name not in providers:
        raise ConfigValidationError(f"Unknown provider: {provider_name}")

    return ServiceConfig(
        tracker=tracker,
        polling=PollingConfig(interval_ms=_coerce_positive_int(polling_map.get("interval_ms"), DEFAULT_POLL_INTERVAL_MS)),
        workspace=WorkspaceConfig(root=workspace_root),
        hooks=HooksConfig(
            after_create=_none_if_blank(hooks_map.get("after_create")),
            before_run=_none_if_blank(hooks_map.get("before_run")),
            after_run=_none_if_blank(hooks_map.get("after_run")),
            before_remove=_none_if_blank(hooks_map.get("before_remove")),
            timeout_ms=hooks_timeout,
        ),
        agent=agent,
        codex=codex,
        provider_name=provider_name,
        providers=providers,
        server=ServerConfig(port=_coerce_optional_port(server_map.get("port"))),
    )


def _build_provider_map(raw_providers: Dict[str, Any], legacy_codex: CodexConfig) -> Dict[str, ModelProviderConfig]:
    providers: Dict[str, ModelProviderConfig] = {
        DEFAULT_PROVIDER_NAME: ModelProviderConfig(
            name=DEFAULT_PROVIDER_NAME,
            kind="codex",
            command=legacy_codex.command,
            approval_policy=legacy_codex.approval_policy,
            thread_sandbox=legacy_codex.thread_sandbox,
            turn_sandbox_policy=legacy_codex.turn_sandbox_policy,
            turn_timeout_ms=legacy_codex.turn_timeout_ms,
            read_timeout_ms=legacy_codex.read_timeout_ms,
            stall_timeout_ms=legacy_codex.stall_timeout_ms,
        )
    }
    for provider_name, raw in raw_providers.items():
        provider_map = _as_dict(raw)
        kind = _infer_provider_kind(provider_name, provider_map.get("kind"))
        existing = providers.get(provider_name)
        providers[provider_name] = ModelProviderConfig(
            name=provider_name,
            kind=kind,
            command=_none_if_blank(provider_map.get("command")) or (existing.command if existing else None),
            prompt_mode=_none_if_blank(provider_map.get("prompt_mode")) or "stdin",
            model=_none_if_blank(provider_map.get("model")),
            base_url=_none_if_blank(provider_map.get("base_url")),
            api_key=_resolve_secret(provider_map.get("api_key"), canonical_env=_none_if_blank(provider_map.get("api_key_env"))),
            api_style=_none_if_blank(provider_map.get("api_style")) or ("chat_completions" if kind == "openai-compatible" else "chat_completions"),
            headers=_coerce_string_map(provider_map.get("headers"), resolve_env=True),
            env=_coerce_string_map(provider_map.get("env"), resolve_env=True),
            approval_policy=provider_map.get("approval_policy") if "approval_policy" in provider_map else (existing.approval_policy if existing else None),
            thread_sandbox=provider_map.get("thread_sandbox") if "thread_sandbox" in provider_map else (existing.thread_sandbox if existing else None),
            turn_sandbox_policy=provider_map.get("turn_sandbox_policy") if "turn_sandbox_policy" in provider_map else (existing.turn_sandbox_policy if existing else None),
            turn_timeout_ms=_coerce_positive_int(provider_map.get("turn_timeout_ms"), existing.turn_timeout_ms if existing else DEFAULT_TURN_TIMEOUT_MS),
            read_timeout_ms=_coerce_positive_int(provider_map.get("read_timeout_ms"), existing.read_timeout_ms if existing else DEFAULT_READ_TIMEOUT_MS),
            stall_timeout_ms=_coerce_int(provider_map.get("stall_timeout_ms"), existing.stall_timeout_ms if existing else DEFAULT_STALL_TIMEOUT_MS),
        )
    return providers


def _infer_provider_kind(provider_name: str, raw_kind: Any) -> str:
    kind = _none_if_blank(raw_kind)
    if kind:
        return kind
    lowered = provider_name.strip().lower()
    if lowered == "codex":
        return "codex"
    if lowered in {"glm", "openai", "openai-compatible"}:
        return "openai-compatible"
    if lowered in {"claudecode", "copilot"}:
        return lowered
    return "command"


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_states(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        return [part for part in parts if part]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return list(default)


def _coerce_state_limits(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, int] = {}
    for key, raw in value.items():
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            continue
        if parsed <= 0:
            continue
        result[normalize_state(str(key))] = parsed
    return result


def _coerce_positive_int(value: Any, default: int) -> int:
    parsed = _coerce_int(value, default)
    return parsed if parsed > 0 else default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_port(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _coerce_string_map(value: Any, resolve_env: bool = False) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, str] = {}
    for key, raw in value.items():
        text = _none_if_blank(raw)
        if text is None:
            continue
        if resolve_env and text.startswith("$"):
            text = os.environ.get(text[1:], "").strip()
            if not text:
                continue
        result[str(key)] = text
    return result


def _none_if_blank(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_secret(value: Any, canonical_env: str | None = None) -> Optional[str]:
    if value is None and canonical_env:
        resolved = os.environ.get(canonical_env, "").strip()
        return resolved or None
    text = _none_if_blank(value)
    if text is None:
        return None
    if text.startswith("$"):
        resolved = os.environ.get(text[1:], "").strip()
        return resolved or None
    return text


def _resolve_path_like(value: Any) -> Optional[Path]:
    text = _none_if_blank(value)
    if text is None:
        return None
    if text.startswith("$"):
        text = os.environ.get(text[1:], "").strip()
        if not text:
            return None
    if text.startswith("~"):
        return Path(text).expanduser()
    if "/" in text or os.sep in text or text in {".", ".."}:
        return Path(text).expanduser()
    return Path(text)
