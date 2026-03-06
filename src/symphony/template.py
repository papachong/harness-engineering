from __future__ import annotations

from jinja2 import Environment, StrictUndefined, TemplateError, TemplateSyntaxError

from .errors import TemplateParseError, TemplateRenderError
from .models import Issue


def render_prompt(template_text: str, issue: Issue, attempt: int | None) -> str:
    source = template_text.strip() or "You are working on an issue from Linear."
    env = Environment(undefined=StrictUndefined, autoescape=False, trim_blocks=False, lstrip_blocks=False)
    try:
        template = env.from_string(source)
    except TemplateSyntaxError as exc:
        raise TemplateParseError(str(exc)) from exc
    try:
        rendered = template.render(issue=issue.to_template_dict(), attempt=attempt)
    except TemplateError as exc:
        raise TemplateRenderError(str(exc)) from exc
    return rendered.strip()


def continuation_prompt(issue: Issue, turn_number: int, max_turns: int) -> str:
    return (
        f"Continue working on issue {issue.identifier}: {issue.title}. "
        f"This is continuation turn {turn_number} of {max_turns}. "
        "Reuse the existing thread context, avoid repeating the original task prompt, "
        "inspect the current workspace state, continue implementation, validation, and handoff work, "
        "then stop when the issue is no longer active or the work is ready for the next handoff state."
    )
