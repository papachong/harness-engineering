---
tracker:
  kind: linear
  endpoint: https://api.linear.app/graphql
  api_key: $LINEAR_API_KEY
  project_slug: f2912a5619df
  active_states:
    - Todo
    - In Progress
  terminal_states:
    - Done
    - Closed
    - Cancelled
    - Terminated
polling:
  interval_ms: 30000
workspace:
  root: ./.symphony_workspaces
agent:
  max_concurrent_agents: 4
  max_turns: 20
  max_retry_backoff_ms: 300000
model:
  provider: copilot
codex:
  command: codex app-server
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 300000
providers:
  codex:
    kind: codex
    command: codex app-server
    turn_timeout_ms: 3600000
    read_timeout_ms: 5000
    stall_timeout_ms: 300000
  glm:
    kind: openai-compatible
    model: glm-5
    base_url: https://open.bigmodel.cn/api/paas/v4
    api_key: $BIGMODEL_API_KEY
    api_style: chat_completions
    turn_timeout_ms: 3600000
    read_timeout_ms: 30000
    stall_timeout_ms: 300000
    headers:
      X-Workspace: symphony
  claudecode:
    kind: claudecode
    command: claude -p {prompt_shell} --output-format text --permission-mode bypassPermissions
    prompt_mode: template
    turn_timeout_ms: 3600000
    stall_timeout_ms: 300000
  copilot:
    kind: copilot
    command: "'/Users/gallenma/Library/Application Support/Code/User/globalStorage/github.copilot-chat/copilotCli/copilot' -p {prompt_shell} --output-format text --allow-all-tools --allow-all-paths --allow-all-urls --no-ask-user -s --model gpt-5.3-codex"
    prompt_mode: template
    turn_timeout_ms: 3600000
    stall_timeout_ms: 300000
server:
  port: 0
---
# Symphony Workflow

You are operating inside Symphony for issue {{ issue.identifier }}: {{ issue.title }}.

Issue state: {{ issue.state }}
Priority: {{ issue.priority }}
Description:
{{ issue.description or "(no description)" }}

Rules:

- Work only inside the issue workspace.
- Validate the repository before handing off.
- Use tracker tooling available in the environment for ticket updates when appropriate.
- If this is a retry or continuation, `attempt` is {{ attempt }}.
- You can switch the active model provider with `SYMPHONY_MODEL_PROVIDER` without editing this file.
