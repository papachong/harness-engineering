from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .errors import InvalidWorkspaceCwdError, WorkspaceError
from .logging_utils import log_kv
from .models import HooksConfig, WorkspaceHandle


def sanitize_workspace_key(identifier: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in identifier)


class WorkspaceManager:
    def __init__(self, root: Path, hooks: HooksConfig, logger: logging.Logger):
        self.root = root.expanduser().resolve()
        self.hooks = hooks
        self.logger = logger
        self.root.mkdir(parents=True, exist_ok=True)

    def create_for_issue(self, identifier: str) -> WorkspaceHandle:
        key = sanitize_workspace_key(identifier)
        workspace_path = (self.root / key).resolve()
        self._ensure_within_root(workspace_path)
        created_now = False
        if workspace_path.exists() and not workspace_path.is_dir():
            raise WorkspaceError(f"Workspace path exists and is not a directory: {workspace_path}")
        if not workspace_path.exists():
            workspace_path.mkdir(parents=True, exist_ok=False)
            created_now = True
            self.run_hook("after_create", self.hooks.after_create, workspace_path, fatal=True)
        return WorkspaceHandle(path=workspace_path, workspace_key=key, created_now=created_now)

    def remove_for_issue(self, identifier: str) -> None:
        key = sanitize_workspace_key(identifier)
        workspace_path = (self.root / key).resolve()
        self._ensure_within_root(workspace_path)
        if not workspace_path.exists():
            return
        self.run_hook("before_remove", self.hooks.before_remove, workspace_path, fatal=False)
        shutil.rmtree(workspace_path, ignore_errors=False)

    def run_before_run(self, workspace_path: Path) -> None:
        self._ensure_within_root(workspace_path.resolve())
        self.run_hook("before_run", self.hooks.before_run, workspace_path, fatal=True)

    def run_after_run(self, workspace_path: Path) -> None:
        self._ensure_within_root(workspace_path.resolve())
        self.run_hook("after_run", self.hooks.after_run, workspace_path, fatal=False)

    def validate_workspace_cwd(self, workspace_path: Path) -> None:
        workspace_path = workspace_path.expanduser().resolve()
        self._ensure_within_root(workspace_path)
        if workspace_path.parent != self.root and self.root not in workspace_path.parents:
            raise InvalidWorkspaceCwdError(str(workspace_path))

    def _ensure_within_root(self, workspace_path: Path) -> None:
        if workspace_path != self.root and self.root not in workspace_path.parents:
            raise InvalidWorkspaceCwdError(f"Workspace path escapes root: {workspace_path}")

    def run_hook(self, name: str, script: str | None, cwd: Path, fatal: bool) -> None:
        if not script:
            return
        log_kv(self.logger, logging.INFO, "hook_start", hook=name, cwd=str(cwd))
        try:
            completed = subprocess.run(
                ["sh", "-lc", script],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=max(self.hooks.timeout_ms / 1000, 1),
                check=False,
            )
            if completed.returncode != 0:
                output = (completed.stderr or completed.stdout or "").strip()[:400]
                raise WorkspaceError(f"hook {name} failed: {output}")
        except subprocess.TimeoutExpired as exc:
            message = f"hook {name} timed out"
            if fatal:
                raise WorkspaceError(message) from exc
            log_kv(self.logger, logging.WARNING, "hook_timeout", hook=name, cwd=str(cwd), error=message)
            return
        except Exception as exc:  # noqa: BLE001
            if fatal:
                raise
            log_kv(self.logger, logging.WARNING, "hook_failed", hook=name, cwd=str(cwd), error=str(exc))
