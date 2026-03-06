import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from symphony.errors import TemplateRenderError, WorkflowFrontMatterNotAMapError
from symphony.models import DEFAULT_ACTIVE_STATES
from symphony.template import render_prompt
from symphony.workflow import WorkflowLoader, build_service_config, load_workflow_definition
from symphony.models import Issue


class WorkflowTests(unittest.TestCase):
    def test_loads_front_matter_and_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow_path = Path(tmp) / "WORKFLOW.md"
            workflow_path.write_text(
                textwrap.dedent(
                    """
                    ---
                    tracker:
                      kind: linear
                      project_slug: demo
                      api_key: $LINEAR_API_KEY
                    polling:
                      interval_ms: 1234
                    ---
                    Hello {{ issue.identifier }}
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            os.environ["LINEAR_API_KEY"] = "token"
            loader = WorkflowLoader(workflow_path)
            loaded = loader.load()
            self.assertEqual(loaded.config.polling.interval_ms, 1234)
            self.assertEqual(loaded.config.tracker.api_key, "token")
            self.assertEqual(loaded.definition.prompt_template, "Hello {{ issue.identifier }}")

    def test_front_matter_must_be_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow_path = Path(tmp) / "WORKFLOW.md"
            workflow_path.write_text("---\n- bad\n---\nbody\n", encoding="utf-8")
            with self.assertRaises(WorkflowFrontMatterNotAMapError):
                load_workflow_definition(workflow_path)

    def test_render_prompt_is_strict(self):
        issue = Issue(id="1", identifier="ABC-1", title="Demo", description=None, priority=None, state="Todo")
        with self.assertRaises(TemplateRenderError):
            render_prompt("{{ issue.missing }}", issue, None)

    def test_defaults_apply(self):
        config = build_service_config({"tracker": {"kind": "linear"}})
        self.assertEqual(config.tracker.active_states, DEFAULT_ACTIVE_STATES)

    def test_legacy_codex_provider_is_available(self):
        config = build_service_config({"tracker": {"kind": "linear"}})
        self.assertEqual(config.provider_name, "codex")
        self.assertEqual(config.selected_provider.kind, "codex")
        self.assertEqual(config.selected_provider.command, "codex app-server")

    def test_parses_openai_compatible_provider(self):
        os.environ["BIGMODEL_API_KEY"] = "glm-token"
        config = build_service_config(
            {
                "tracker": {"kind": "linear"},
                "model": {"provider": "glm"},
                "providers": {
                    "glm": {
                        "kind": "openai-compatible",
                        "model": "glm-5",
                        "base_url": "https://open.bigmodel.cn/api/paas/v4",
                        "api_key": "$BIGMODEL_API_KEY",
                        "api_style": "chat_completions",
                    }
                },
            }
        )
        self.assertEqual(config.provider_name, "glm")
        self.assertEqual(config.selected_provider.kind, "openai-compatible")
        self.assertEqual(config.selected_provider.api_key, "glm-token")

    def test_parses_command_style_providers(self):
        config = build_service_config(
            {
                "tracker": {"kind": "linear"},
                "model": {"provider": "claudecode"},
                "providers": {
                    "claudecode": {
                        "command": "claude -p {prompt_shell}",
                        "prompt_mode": "template",
                    },
                    "copilot": {
                        "kind": "copilot",
                        "command": "copilot explain {prompt_shell}",
                        "prompt_mode": "template",
                    },
                },
            }
        )
        self.assertEqual(config.providers["claudecode"].kind, "claudecode")
        self.assertEqual(config.providers["copilot"].kind, "copilot")
        self.assertEqual(config.selected_provider.command, "claude -p {prompt_shell}")

    def test_env_override_provider_selection(self):
        os.environ["SYMPHONY_MODEL_PROVIDER"] = "glm"
        os.environ["BIGMODEL_API_KEY"] = "glm-token"
        try:
            config = build_service_config(
                {
                    "tracker": {"kind": "linear"},
                    "model": {"provider": "codex"},
                    "providers": {
                        "glm": {
                            "kind": "openai-compatible",
                            "model": "glm-5",
                            "base_url": "https://open.bigmodel.cn/api/paas/v4",
                            "api_key": "$BIGMODEL_API_KEY",
                        }
                    },
                }
            )
            self.assertEqual(config.provider_name, "glm")
        finally:
            os.environ.pop("SYMPHONY_MODEL_PROVIDER", None)


if __name__ == "__main__":
    unittest.main()
