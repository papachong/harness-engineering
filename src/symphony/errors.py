class SymphonyError(Exception):
    """Base error for the Symphony service."""


class WorkflowError(SymphonyError):
    code = "workflow_error"


class MissingWorkflowFileError(WorkflowError):
    code = "missing_workflow_file"


class WorkflowParseError(WorkflowError):
    code = "workflow_parse_error"


class WorkflowFrontMatterNotAMapError(WorkflowError):
    code = "workflow_front_matter_not_a_map"


class TemplateParseError(SymphonyError):
    code = "template_parse_error"


class TemplateRenderError(SymphonyError):
    code = "template_render_error"


class ConfigValidationError(SymphonyError):
    code = "config_validation_error"


class TrackerError(SymphonyError):
    code = "tracker_error"


class UnsupportedTrackerKindError(TrackerError):
    code = "unsupported_tracker_kind"


class MissingTrackerApiKeyError(TrackerError):
    code = "missing_tracker_api_key"


class MissingTrackerProjectSlugError(TrackerError):
    code = "missing_tracker_project_slug"


class WorkspaceError(SymphonyError):
    code = "workspace_error"


class InvalidWorkspaceCwdError(WorkspaceError):
    code = "invalid_workspace_cwd"


class AgentError(SymphonyError):
    code = "agent_error"


class ResponseTimeoutError(AgentError):
    code = "response_timeout"


class TurnTimeoutError(AgentError):
    code = "turn_timeout"


class TurnFailedError(AgentError):
    code = "turn_failed"


class TurnCancelledError(AgentError):
    code = "turn_cancelled"


class TurnInputRequiredError(AgentError):
    code = "turn_input_required"


class CodexNotFoundError(AgentError):
    code = "codex_not_found"
