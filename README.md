# Symphony Service

[![CI](https://github.com/papachong/harness-engineering/actions/workflows/ci.yml/badge.svg)](https://github.com/papachong/harness-engineering/actions/workflows/ci.yml)

Python implementation of the OpenAI Symphony service specification.

[简体中文版](README.zh-CN.md)

## Overview

The project is a requirement driven service with fully automated software delivery by AI agents. it watches Linear issues, creates isolated per-issue workspaces, dispatches work to configurable model providers, and keeps execution state synchronized back to Linear. It includes workflow loading, polling orchestration, retry/continuation control, workspace lifecycle hooks, and an optional local HTTP status dashboard for observing active and retrying runs.

[harness engineering](https://openai.com/index/harness-engineering/)
this project treats the engineering challenge as designing an environment where agents can work reliably rather than only prompting them to try harder. Its goal is to make tasks legible, constraints enforceable, feedback loops tight, and repository knowledge accessible to the agent. The practical meaning is higher delivery throughput, more reproducible execution, and lower dependence on manual intervention when agents need to validate, retry, recover, and hand work off cleanly.

## Included

- `WORKFLOW.md` loader with YAML front matter and strict prompt rendering
- typed config layer with defaults and `$VAR` resolution
- Linear tracker adapter for candidate fetch, terminal fetch, state refresh, and issue state write-back
- per-issue workspace manager with hooks and root-containment checks
- pluggable model providers: Codex, OpenAI-compatible providers (for example GLM), and command-based providers
- polling orchestrator with reconciliation, retry/backoff, and continuation retry
- optional local HTTP dashboard and JSON API

## Trust posture

Current default posture is high-trust:

- workspace hooks are fully trusted shell scripts
- approval requests are auto-approved
- user input requests fail the run
- unsupported dynamic tools are rejected with structured failure responses

Adjust `WORKFLOW.md` and provider settings before production use.

## Model providers

The selected provider comes from `model.provider` in `WORKFLOW.md`.
You can override it without editing the workflow file by setting `SYMPHONY_MODEL_PROVIDER` (or `SYMPHONY_PROVIDER`).

Supported provider kinds:

- `codex`: launches `codex app-server`
- `openai-compatible`: calls an OpenAI-compatible HTTP API such as GLM
- `command`: runs a configurable shell command in the issue workspace
- `claudecode`: command-style provider alias intended for Claude Code CLI
- `copilot`: command-style provider alias intended for Copilot CLI wrappers

The HTTP dashboard and `/api/v1/state` also surface the selected provider and a recent provider summary for each running or retrying issue.

Example:

```yaml
model:
  provider: glm

providers:
  codex:
    kind: codex
    command: codex app-server

  glm:
    kind: openai-compatible
    model: glm-5
    base_url: https://open.bigmodel.cn/api/paas/v4
    api_key: $BIGMODEL_API_KEY
    api_style: chat_completions

  claudecode:
    kind: claudecode
    command: claude -p {prompt_shell}
    prompt_mode: template

  copilot:
    kind: copilot
    command: my-copilot-wrapper {prompt_shell}
    prompt_mode: template
```

For command-style providers, the command template can use:

- `{prompt}` / `{prompt_shell}`
- `{workspace}` / `{workspace_shell}`
- `{issue_identifier}` / `{issue_identifier_shell}`
- `{issue_title}` / `{issue_title_shell}`

## Run

```bash
python -m pip install -e .
export LINEAR_API_KEY=...
export BIGMODEL_API_KEY=...
export SYMPHONY_MODEL_PROVIDER=claudecode
symphony ./WORKFLOW.md --port 8080
```

Examples:

- `export SYMPHONY_MODEL_PROVIDER=codex`
- `export SYMPHONY_MODEL_PROVIDER=glm`
- `export SYMPHONY_MODEL_PROVIDER=claudecode`
- `export SYMPHONY_MODEL_PROVIDER=copilot`

## HTTP endpoints

- `/`
- `/api/v1/state` (supports optional query filters)
- `/api/v1/<issue_identifier>`
- `POST /api/v1/refresh`

### `/api/v1/state` query filters

All filters are optional and can be combined:

- `status=running|retrying` (comma-separated values allowed)
- `state=<issue-state>` (applies to running issues only; comma-separated values allowed)
- `provider=<provider-name>` (for retrying rows this maps to the selected provider)
- `issue_identifier=<identifier>` (or `issue=<identifier>`, comma-separated values allowed)
- `q=<text>` (case-insensitive substring match across issue id/identifier, state, provider, and summary/error text)
