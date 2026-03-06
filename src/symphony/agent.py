from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import (
    CodexNotFoundError,
    ResponseTimeoutError,
    TurnCancelledError,
    TurnFailedError,
    TurnInputRequiredError,
    TurnTimeoutError,
)
from .logging_utils import log_kv
from .models import Issue, ModelProviderConfig, RunOutcome, ServiceConfig
from .template import continuation_prompt, render_prompt

EventCallback = Callable[[Dict[str, Any]], Awaitable[None]]


class CodexAppServerClient:
    def __init__(self, provider: ModelProviderConfig, workspace_path: Path, logger: logging.Logger, event_cb: EventCallback):
        self.provider = provider
        self.workspace_path = workspace_path
        self.logger = logger
        self.event_cb = event_cb
        self.process: Optional[asyncio.subprocess.Process] = None
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self.thread_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self._closed = False
        self._active_turn_id: Optional[str] = None

    async def start(self) -> None:
        if shutil.which("bash") is None:
            raise CodexNotFoundError("bash not found")
        try:
            self.process = await asyncio.create_subprocess_exec(
                "bash",
                "-lc",
                self.provider.command or "codex app-server",
                cwd=str(self.workspace_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CodexNotFoundError(self.provider.command or "codex app-server") from exc
        self._stdout_task = asyncio.create_task(self._stdout_reader())
        self._stderr_task = asyncio.create_task(self._stderr_reader())
        await self._request("initialize", {"clientInfo": {"name": "symphony", "version": "1.0"}, "capabilities": {}})
        await self._notify("initialized", {})
        result = await self._request(
            "thread/start",
            {
                "approvalPolicy": self.provider.approval_policy,
                "sandbox": self.provider.thread_sandbox,
                "cwd": str(self.workspace_path),
            },
        )
        self.thread_id = self._extract_nested(result, ["thread", "id"]) or result.get("threadId") or result.get("thread_id")
        if not self.thread_id:
            raise TurnFailedError("thread/start missing thread id")

    async def run_turn(self, issue: Issue, prompt: str) -> Dict[str, Any]:
        if not self.thread_id:
            raise TurnFailedError("thread not initialized")
        result = await self._request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": prompt}],
                "cwd": str(self.workspace_path),
                "title": f"{issue.identifier}: {issue.title}",
                "approvalPolicy": self.provider.approval_policy,
                "sandboxPolicy": self.provider.turn_sandbox_policy,
            },
        )
        turn_id = self._extract_nested(result, ["turn", "id"]) or result.get("turnId") or result.get("turn_id")
        if not turn_id:
            raise TurnFailedError("turn/start missing turn id")
        self._active_turn_id = str(turn_id)
        self.session_id = f"{self.thread_id}-{self._active_turn_id}"
        await self._emit(
            {
                "event": "session_started",
                "timestamp": _utc_now(),
                "session_id": self.session_id,
                "thread_id": self.thread_id,
                "turn_id": self._active_turn_id,
                "codex_app_server_pid": str(self.process.pid) if self.process else None,
            }
        )
        try:
            return await asyncio.wait_for(self._consume_turn_stream(), timeout=self.provider.turn_timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            raise TurnTimeoutError("turn_timeout") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process and self.process.returncode is None:
            self.process.terminate()
            with contextlib.suppress(ProcessLookupError, asyncio.TimeoutError):
                await asyncio.wait_for(self.process.wait(), timeout=5)
        for task in (self._stdout_task, self._stderr_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for future in self._pending.values():
            if not future.done():
                future.cancel()

    async def _consume_turn_stream(self) -> Dict[str, Any]:
        while True:
            if self.process and self.process.returncode is not None:
                raise TurnFailedError("port_exit")
            message = await self._wait_for_stream_message()
            event_name = self._event_name(message)
            summary = self._summarize_message(message)
            await self._emit(
                {
                    "event": event_name or "other_message",
                    "timestamp": _utc_now(),
                    "session_id": self.session_id,
                    "codex_app_server_pid": str(self.process.pid) if self.process else None,
                    "message": summary,
                    "usage": _extract_usage(message),
                    "rate_limits": _extract_rate_limits(message),
                }
            )
            if self._is_user_input_request(message):
                raise TurnInputRequiredError("turn_input_required")
            if self._is_approval_request(message):
                await self._send_json({"id": message.get("id"), "result": {"approved": True}})
                await self._emit({"event": "approval_auto_approved", "timestamp": _utc_now(), "session_id": self.session_id})
                continue
            if self._is_tool_call(message):
                await self._handle_tool_call(message)
                continue
            if event_name == "turn/completed":
                return {"status": "completed", "turn_id": self._active_turn_id}
            if event_name == "turn/failed":
                raise TurnFailedError(summary or "turn_failed")
            if event_name == "turn/cancelled":
                raise TurnCancelledError(summary or "turn_cancelled")

    async def _wait_for_stream_message(self) -> Dict[str, Any]:
        future = asyncio.get_running_loop().create_future()
        self._pending[-id(future)] = future
        try:
            return await future
        finally:
            self._pending.pop(-id(future), None)

    async def _stdout_reader(self) -> None:
        assert self.process and self.process.stdout
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                await self._emit({"event": "malformed", "timestamp": _utc_now(), "message": line.decode("utf-8", errors="replace")[:400]})
                continue
            if "id" in message and ("result" in message or "error" in message):
                response_id = int(message["id"])
                future = self._pending.pop(response_id, None)
                if future and not future.done():
                    future.set_result(message)
                continue
            for key, future in list(self._pending.items()):
                if key < 0 and not future.done():
                    future.set_result(message)
                    break

    async def _stderr_reader(self) -> None:
        assert self.process and self.process.stderr
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            log_kv(self.logger, logging.INFO, "provider_stderr", provider=self.provider.name, message=line.decode("utf-8", errors="replace")[:400].strip())

    async def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send_json({"id": request_id, "method": method, "params": _compact(params)})
        try:
            response = await asyncio.wait_for(future, timeout=self.provider.read_timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise ResponseTimeoutError(f"{method} response timeout") from exc
        if response.get("error"):
            raise TurnFailedError(str(response["error"]))
        return response.get("result") or {}

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        await self._send_json({"method": method, "params": _compact(params)})

    async def _send_json(self, message: Dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise TurnFailedError("codex process not running")
        encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        self.process.stdin.write(encoded)
        await self.process.stdin.drain()

    async def _emit(self, payload: Dict[str, Any]) -> None:
        await self.event_cb(payload)

    async def _handle_tool_call(self, message: Dict[str, Any]) -> None:
        request_id = message.get("id")
        params = message.get("params") or {}
        tool_name = params.get("name") or params.get("toolName") or "unknown"
        await self._send_json({"id": request_id, "result": {"success": False, "error": "unsupported_tool_call", "tool": tool_name}})
        await self._emit({"event": "unsupported_tool_call", "timestamp": _utc_now(), "session_id": self.session_id, "message": str(tool_name)})

    def _event_name(self, message: Dict[str, Any]) -> Optional[str]:
        return message.get("method") or message.get("event")

    def _is_tool_call(self, message: Dict[str, Any]) -> bool:
        return "tool/call" in (self._event_name(message) or "")

    def _is_approval_request(self, message: Dict[str, Any]) -> bool:
        method = (self._event_name(message) or "").lower()
        return "approval" in method and "request" in method and message.get("id") is not None

    def _is_user_input_request(self, message: Dict[str, Any]) -> bool:
        method = (self._event_name(message) or "").lower()
        if "requestuserinput" in method or "input_required" in method:
            return True
        params = message.get("params") or {}
        return bool(params.get("inputRequired") or params.get("userInputRequired"))

    def _extract_nested(self, source: Dict[str, Any], path: List[str]) -> Any:
        current: Any = source
        for key in path:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def _summarize_message(self, message: Dict[str, Any]) -> str:
        for key in ("message", "text", "summary", "status"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:400]
        params = message.get("params")
        if isinstance(params, dict):
            for key in ("message", "text", "summary"):
                value = params.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:400]
        return json.dumps(message, ensure_ascii=False)[:400]


class OpenAICompatibleClient:
    def __init__(self, provider: ModelProviderConfig, workspace_path: Path, logger: logging.Logger, event_cb: EventCallback):
        self.provider = provider
        self.workspace_path = workspace_path
        self.logger = logger
        self.event_cb = event_cb
        self.thread_id = uuid.uuid4().hex
        self.history: List[Dict[str, Any]] = []
        self._closed = False

    async def start(self) -> None:
        return None

    async def run_turn(self, issue: Issue, prompt: str) -> Dict[str, Any]:
        turn_id = uuid.uuid4().hex
        session_id = f"{self.thread_id}-{turn_id}"
        await self.event_cb(
            {
                "event": "session_started",
                "timestamp": _utc_now(),
                "session_id": session_id,
                "thread_id": self.thread_id,
                "turn_id": turn_id,
            }
        )
        result = await asyncio.wait_for(asyncio.to_thread(self._invoke_model, prompt), timeout=self.provider.turn_timeout_ms / 1000)
        text = result["text"]
        self.history.append({"role": "user", "content": prompt})
        self.history.append({"role": "assistant", "content": text})
        await self.event_cb(
            {
                "event": "notification",
                "timestamp": _utc_now(),
                "session_id": session_id,
                "message": text[:400],
                "usage": result.get("usage"),
            }
        )
        await self.event_cb(
            {
                "event": "turn_completed",
                "timestamp": _utc_now(),
                "session_id": session_id,
                "message": "completed",
                "usage": result.get("usage"),
            }
        )
        return {"status": "completed", "turn_id": turn_id}

    async def close(self) -> None:
        self._closed = True

    def _invoke_model(self, prompt: str) -> Dict[str, Any]:
        base_url = (self.provider.base_url or "").rstrip("/")
        headers = {"Content-Type": "application/json", **self.provider.headers}
        if self.provider.api_key:
            headers.setdefault("Authorization", f"Bearer {self.provider.api_key}")
        if self.provider.api_style == "responses":
            url = f"{base_url}/responses"
            body = {"model": self.provider.model, "input": prompt}
        else:
            url = f"{base_url}/chat/completions"
            body = {"model": self.provider.model, "messages": [*self.history, {"role": "user", "content": prompt}]}
        request = Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=max(self.provider.read_timeout_ms / 1000, 1)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise TurnFailedError(f"provider_http_{exc.code}: {detail}") from exc
        except URLError as exc:
            raise TurnFailedError(f"provider_request_error: {exc}") from exc
        text = _extract_model_text(payload)
        if not text:
            raise TurnFailedError("provider returned no assistant text")
        return {"text": text, "usage": _extract_openai_usage(payload)}


class CommandProviderClient:
    def __init__(self, provider: ModelProviderConfig, workspace_path: Path, logger: logging.Logger, event_cb: EventCallback):
        self.provider = provider
        self.workspace_path = workspace_path
        self.logger = logger
        self.event_cb = event_cb
        self.thread_id = uuid.uuid4().hex

    async def start(self) -> None:
        return None

    async def run_turn(self, issue: Issue, prompt: str) -> Dict[str, Any]:
        turn_id = uuid.uuid4().hex
        session_id = f"{self.thread_id}-{turn_id}"
        await self.event_cb(
            {
                "event": "session_started",
                "timestamp": _utc_now(),
                "session_id": session_id,
                "thread_id": self.thread_id,
                "turn_id": turn_id,
            }
        )
        command, stdin_data = _prepare_command(self.provider, self.workspace_path, issue, prompt)
        env = os.environ.copy()
        env.update(self.provider.env)
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            command,
            cwd=str(self.workspace_path),
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin_data.encode("utf-8") if stdin_data is not None else None),
                timeout=self.provider.turn_timeout_ms / 1000,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise TurnTimeoutError("turn_timeout") from exc
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        summary = (stdout_text or stderr_text or "completed")[:400]
        if process.returncode != 0:
            raise TurnFailedError(summary)
        await self.event_cb({"event": "notification", "timestamp": _utc_now(), "session_id": session_id, "message": summary})
        await self.event_cb({"event": "turn_completed", "timestamp": _utc_now(), "session_id": session_id, "message": "completed"})
        return {"status": "completed", "turn_id": turn_id}

    async def close(self) -> None:
        return None


class AgentRunner:
    def __init__(self, config: ServiceConfig, tracker_client: Any, workspace_manager: Any, logger: logging.Logger):
        self.config = config
        self.tracker_client = tracker_client
        self.workspace_manager = workspace_manager
        self.logger = logger
        self.prompt_template = ""

    async def run_attempt(self, issue: Issue, attempt: int | None, on_event: EventCallback) -> RunOutcome:
        workspace = await asyncio.to_thread(self.workspace_manager.create_for_issue, issue.identifier)
        client = build_provider_client(self.config, workspace.path, self.logger, on_event)
        try:
            await asyncio.to_thread(self.workspace_manager.run_before_run, workspace.path)
            await client.start()
            turn_number = 1
            needs_continuation = False
            while True:
                prompt = render_prompt(self.prompt_template, issue, attempt) if turn_number == 1 else continuation_prompt(issue, turn_number, self.config.agent.max_turns)
                await client.run_turn(issue, prompt)
                refreshed = await asyncio.to_thread(self.tracker_client.fetch_issue_states_by_ids, [issue.id])
                if not refreshed:
                    break
                issue = refreshed[0]
                if issue.normalized_state not in self.config.active_state_set:
                    break
                if turn_number >= self.config.agent.max_turns:
                    needs_continuation = True
                    break
                turn_number += 1
            return RunOutcome(status="normal", continuation=needs_continuation)
        except TurnInputRequiredError as exc:
            return RunOutcome(status="failed", error=str(exc))
        except asyncio.TimeoutError:
            return RunOutcome(status="failed", error="turn_timeout")
        except TurnTimeoutError as exc:
            return RunOutcome(status="failed", error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return RunOutcome(status="failed", error=str(exc))
        finally:
            with contextlib.suppress(Exception):
                await client.close()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.workspace_manager.run_after_run, workspace.path)


def build_provider_client(config: ServiceConfig, workspace_path: Path, logger: logging.Logger, event_cb: EventCallback) -> Any:
    provider = config.selected_provider
    if provider.kind == "codex":
        return CodexAppServerClient(provider, workspace_path, logger, event_cb)
    if provider.kind == "openai-compatible":
        return OpenAICompatibleClient(provider, workspace_path, logger, event_cb)
    if provider.kind in {"command", "claudecode", "copilot"}:
        return CommandProviderClient(provider, workspace_path, logger, event_cb)
    raise TurnFailedError(f"unsupported provider kind: {provider.kind}")


def _prepare_command(provider: ModelProviderConfig, workspace_path: Path, issue: Issue, prompt: str) -> tuple[str, Optional[str]]:
    command = provider.command or ""
    prompt_mode = (provider.prompt_mode or "stdin").strip().lower()
    template_values = {
        "prompt": prompt,
        "prompt_shell": shlex.quote(prompt),
        "workspace": str(workspace_path),
        "workspace_shell": shlex.quote(str(workspace_path)),
        "issue_identifier": issue.identifier,
        "issue_title": issue.title,
        "issue_identifier_shell": shlex.quote(issue.identifier),
        "issue_title_shell": shlex.quote(issue.title),
    }
    if prompt_mode == "template":
        return command.format_map(template_values), None
    return command, prompt


def _compact(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _extract_usage(message: Dict[str, Any]) -> Optional[Dict[str, int]]:
    candidates = []
    for key in ("usage", "total_token_usage", "tokenUsage", "totalTokenUsage"):
        value = _find_first(message, key)
        if isinstance(value, dict):
            candidates.append(value)
    for candidate in candidates:
        input_tokens = _find_first(candidate, "input_tokens") or _find_first(candidate, "inputTokens") or _find_first(candidate, "input")
        output_tokens = _find_first(candidate, "output_tokens") or _find_first(candidate, "outputTokens") or _find_first(candidate, "output")
        total_tokens = _find_first(candidate, "total_tokens") or _find_first(candidate, "totalTokens") or _find_first(candidate, "total")
        try:
            parsed = {
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
                "total_tokens": int(total_tokens or 0),
            }
        except (TypeError, ValueError):
            continue
        if any(parsed.values()):
            return parsed
    return None


def _extract_rate_limits(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in ("rate_limits", "rateLimits"):
        value = _find_first(message, key)
        if isinstance(value, dict):
            return value
    return None


def _extract_openai_usage(payload: Dict[str, Any]) -> Optional[Dict[str, int]]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    output_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    total_tokens = usage.get("total_tokens") or (int(input_tokens or 0) + int(output_tokens or 0))
    try:
        return {
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "total_tokens": int(total_tokens),
        }
    except (TypeError, ValueError):
        return None


def _extract_model_text(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks = [item.get("text", "") for item in content if isinstance(item, dict)]
            return "".join(chunks).strip()
    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        return output_text.strip()
    output = payload.get("output")
    if isinstance(output, list):
        parts: List[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    parts.append(content["text"])
        return "".join(parts).strip()
    return ""


def _find_first(value: Any, target_key: str) -> Any:
    if isinstance(value, dict):
        if target_key in value:
            return value[target_key]
        for nested in value.values():
            found = _find_first(nested, target_key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_first(nested, target_key)
            if found is not None:
                return found
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
