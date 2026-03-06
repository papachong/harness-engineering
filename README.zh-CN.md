# Harness Engineering Service

[![CI](https://github.com/papachong/harness-engineering/actions/workflows/ci.yml/badge.svg)](https://github.com/papachong/harness-engineering/actions/workflows/ci.yml)

[English README](README.md)

## 项目简介

该项目是一个需求驱动，由AI Agent全自动化交付软件的服务。它会轮询符合条件的 Linear issue，为每个 issue 创建独立工作区，将任务分发给可配置的大模型或命令行提供方执行，并把运行状态同步回 Linear。项目同时提供工作流加载、轮询编排、失败重试与 continuation 控制、工作区生命周期 hook，以及可选的本地 HTTP 状态面板与 JSON API。

[harness engineering](https://openai.com/index/harness-engineering/) 
工程重点不再只是“要求 agent 更努力”，而是为 agent 构建一个可执行、可验证、可恢复的工作环境。它的目标是让任务上下文更可见、约束更可执行、反馈闭环更紧密、仓库知识对 agent 更可理解；其意义在于提升交付吞吐、增强执行可复现性，并减少 agent 在验证、重试、恢复和交接过程中对人工介入的依赖。

## 主要功能

- 读取 `WORKFLOW.md`，支持 YAML front matter 配置与严格模板渲染
- 提供带默认值和环境变量解析（如 `$VAR`）的类型化配置层
- 集成 Linear，支持候选 issue 拉取、终态清理、状态刷新与 issue 状态回写
- 为每个 issue 管理隔离工作区，并支持创建前后 hook
- 支持多种模型提供方：Codex、OpenAI 兼容接口（如 GLM）、命令式 provider
- 提供轮询编排能力，支持运行中 reconcile、失败退避重试与 continuation 重试
- 提供本地 HTTP 状态页和 JSON API，便于查看运行中与重试中的任务

## 信任模型

当前默认是高信任模式：

- 工作区 hook 会按完全可信的 shell 脚本执行
- 审批请求会被自动批准
- 若 provider 请求用户输入，当前运行会失败
- 不支持的动态工具调用会以结构化失败结果被拒绝

在生产环境使用前，建议结合 `WORKFLOW.md` 和 provider 配置进行收敛与加固。

## 模型提供方

当前选择的 provider 来自 `WORKFLOW.md` 中的 `model.provider`。
也可以通过环境变量 `SYMPHONY_MODEL_PROVIDER`（或 `SYMPHONY_PROVIDER`）覆盖，而无需修改工作流文件。

支持的 provider 类型：

- `codex`：启动 `codex app-server`
- `openai-compatible`：调用 OpenAI 兼容 HTTP API，例如 GLM
- `command`：在 issue 工作区内执行可配置 shell 命令
- `claudecode`：面向 Claude Code CLI 的命令式别名
- `copilot`：面向 Copilot CLI 包装器的命令式别名

状态页与 `/api/v1/state` 也会展示当前选中的 provider，以及每个运行中/重试中 issue 的最近 provider 摘要。

示例：

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

对于命令式 provider，命令模板可使用以下变量：

- `{prompt}` / `{prompt_shell}`
- `{workspace}` / `{workspace_shell}`
- `{issue_identifier}` / `{issue_identifier_shell}`
- `{issue_title}` / `{issue_title_shell}`

## 运行方式

```bash
python -m pip install -e .
export LINEAR_API_KEY=...
export BIGMODEL_API_KEY=...
export SYMPHONY_MODEL_PROVIDER=claudecode
symphony ./WORKFLOW.md --port 8080
```

示例：

- `export SYMPHONY_MODEL_PROVIDER=codex`
- `export SYMPHONY_MODEL_PROVIDER=glm`
- `export SYMPHONY_MODEL_PROVIDER=claudecode`
- `export SYMPHONY_MODEL_PROVIDER=copilot`

## HTTP 接口

- `/`
- `/api/v1/state`（支持可选查询过滤）
- `/api/v1/<issue_identifier>`
- `POST /api/v1/refresh`

### `/api/v1/state` 查询参数

以下过滤参数均为可选，可组合使用：

- `status=running|retrying`（支持逗号分隔）
- `state=<issue-state>`（仅作用于 running issue，支持逗号分隔）
- `provider=<provider-name>`（对 retrying 行会映射为当前选中的 provider）
- `issue_identifier=<identifier>`（或 `issue=<identifier>`，支持逗号分隔）
- `q=<text>`（大小写不敏感，匹配 issue id/identifier、state、provider、summary/error 文本）
