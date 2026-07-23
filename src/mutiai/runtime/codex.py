"""Codex Runtime adapter built on the local App Server protocol."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, RLock
from typing import Any

from mutiai.runtime.base import RuntimeResult
from mutiai.runtime.codex_app_server import CodexAppServerError, CodexAppServerSession
from mutiai.runtime.workspaces import WorkspaceManager


@dataclass(frozen=True, slots=True)
class RuntimeWorkspaceBinding:
    """Product-owned identity and path selected for one Runtime execution."""

    workspace_id: str
    path: Path


@dataclass(frozen=True, slots=True)
class CodexCompletion:
    """Normalized terminal result ready for product persistence and graph resume."""

    execution_id: str
    runtime_event_id: str
    result: RuntimeResult


@dataclass(slots=True)
class _ActiveExecution:
    session: CodexAppServerSession
    waiting_result: RuntimeResult
    completion_lock: Lock = field(default_factory=Lock)
    completion: CodexCompletion | None = None


class CodexRuntimeAdapter:
    """Submit bounded assignments without waiting inside a LangGraph node."""

    provider = "codex"

    def __init__(
        self,
        *,
        workspace_manager: WorkspaceManager,
        resolve_workspace: Callable[[str], RuntimeWorkspaceBinding],
        codex_home: str | Path,
        command: Sequence[str] = ("codex", "app-server", "--listen", "stdio://"),
        model: str | None = None,
        approval_policy: str = "on-request",
        output_schema: Mapping[str, Any] | None = None,
    ) -> None:
        self.workspace_manager = workspace_manager
        self.resolve_workspace = resolve_workspace
        self.codex_home = self.workspace_manager.canonicalize(codex_home)
        if not self.codex_home.is_dir():
            raise CodexAppServerError("Codex home must be a managed directory")
        self.command = tuple(command)
        self.model = model
        self.approval_policy = approval_policy
        self.output_schema = dict(output_schema) if output_schema is not None else None
        self._active: dict[str, _ActiveExecution] = {}
        self._lock = RLock()

    def execute(
        self,
        *,
        execution_id: str,
        role_key: str,
        instructions: str,
    ) -> RuntimeResult:
        """Start one Thread and Turn, then return their durable identities."""

        del role_key
        with self._lock:
            existing = self._active.get(execution_id)
            if existing is not None:
                if existing.completion is not None:
                    return existing.completion.result
                return existing.waiting_result

            binding = self.resolve_workspace(execution_id)
            workspace = self.workspace_manager.canonicalize(binding.path)
            session = CodexAppServerSession(
                cwd=workspace,
                command=self.command,
                env={"CODEX_HOME": str(self.codex_home)},
            )
            try:
                session.connect()
                thread_response = session.start_thread(
                    model=self.model,
                    approval_policy=self.approval_policy,
                )
                thread_id = self._require_id(
                    thread_response,
                    container="thread",
                    label="thread",
                )
                self._require_cwd(thread_response, workspace)
                turn_response = session.start_turn(
                    thread_id=thread_id,
                    instructions=instructions,
                    execution_id=execution_id,
                    output_schema=self.output_schema,
                    approval_policy=self.approval_policy,
                )
                turn_id = self._require_id(
                    turn_response,
                    container="turn",
                    label="turn",
                )
                waiting_result = RuntimeResult(
                    status="waiting",
                    runtime_job_id=turn_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    workspace_id=binding.workspace_id,
                )
                self._active[execution_id] = _ActiveExecution(
                    session=session,
                    waiting_result=waiting_result,
                )
                return waiting_result
            except Exception:
                session.close()
                raise

    def wait_for_completion(
        self,
        execution_id: str,
        *,
        timeout: float | None = None,
    ) -> CodexCompletion:
        """Wait outside LangGraph and normalize one terminal Turn notification."""

        with self._lock:
            active = self._active.get(execution_id)
        if active is None:
            raise LookupError(f"Codex execution '{execution_id}' is not active")
        with active.completion_lock:
            if active.completion is not None:
                return active.completion
            waiting = active.waiting_result
            if waiting.thread_id is None or waiting.turn_id is None:
                raise CodexAppServerError(
                    "active Codex execution has no Thread or Turn ID"
                )
            turn = active.session.wait_for_turn(
                thread_id=waiting.thread_id,
                turn_id=waiting.turn_id,
                timeout=timeout,
            )
            status = turn.get("status")
            if status != "completed":
                raise CodexAppServerError(
                    f"Codex turn '{waiting.turn_id}' ended with status '{status}'"
                )
            summary = self._last_agent_message(turn)
            result = RuntimeResult(
                status="completed",
                runtime_job_id=waiting.runtime_job_id,
                summary=summary,
                thread_id=waiting.thread_id,
                turn_id=waiting.turn_id,
                workspace_id=waiting.workspace_id,
            )
            active.completion = CodexCompletion(
                execution_id=execution_id,
                runtime_event_id=(
                    f"codex:{waiting.thread_id}:{waiting.turn_id}:completed"
                ),
                result=result,
            )
            return active.completion

    def close_execution(self, execution_id: str) -> None:
        """Close the owned App Server process for one execution."""

        with self._lock:
            active = self._active.pop(execution_id, None)
        if active is not None:
            active.session.close()

    def close(self) -> None:
        """Close all App Server processes owned by this adapter."""

        with self._lock:
            active_executions = list(self._active.values())
            self._active.clear()
        for active in active_executions:
            active.session.close()

    @staticmethod
    def _require_id(
        response: Mapping[str, Any],
        *,
        container: str,
        label: str,
    ) -> str:
        value = response.get(container)
        identifier = value.get("id") if isinstance(value, dict) else None
        if not isinstance(identifier, str) or not identifier:
            raise CodexAppServerError(f"Codex App Server returned no {label} ID")
        return identifier

    @staticmethod
    def _require_cwd(response: Mapping[str, Any], expected: Path) -> None:
        returned = response.get("cwd")
        if not isinstance(returned, str):
            raise CodexAppServerError("Codex App Server returned no canonical cwd")
        try:
            canonical = Path(returned).expanduser().resolve(strict=True)
        except OSError as exc:
            raise CodexAppServerError(
                "Codex App Server returned an invalid cwd"
            ) from exc
        if canonical != expected:
            raise CodexAppServerError("Codex Thread cwd does not match its workspace")

    @staticmethod
    def _last_agent_message(turn: Mapping[str, Any]) -> str:
        items = turn.get("items")
        if not isinstance(items, list):
            raise CodexAppServerError("completed Codex turn has no items")
        messages = [
            item.get("text")
            for item in items
            if isinstance(item, dict) and item.get("type") == "agentMessage"
        ]
        summary = next(
            (message for message in reversed(messages) if isinstance(message, str)),
            None,
        )
        if not summary:
            raise CodexAppServerError("completed Codex turn has no agent summary")
        return summary
