"""Codex Runtime adapter built on the local App Server protocol."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Literal

from mutiai.runtime.base import (
    RuntimeCapacity,
    RuntimeExecutionConfig,
    RuntimeRecoveryRequest,
    RuntimeResult,
    RuntimeTokenUsage,
)
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


@dataclass(frozen=True, slots=True)
class CodexApprovalRequest:
    """Stable subset of one App Server command or file approval request."""

    execution_id: str
    request_id: str | int
    kind: Literal["command_execution", "file_change"]
    thread_id: str
    turn_id: str
    item_id: str
    reason: str | None
    command: str | None
    cwd: str | None
    details: dict[str, Any]
    runtime_started_at_ms: int | None


class CodexTurnFailedError(CodexAppServerError):
    """Terminal App Server Turn failure with stable Runtime identities."""

    def __init__(
        self,
        *,
        thread_id: str,
        turn_id: str,
        status: str,
        message: str | None,
        reason: str = "runtime_terminal_failure",
    ) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.status = status
        self.reason = reason
        self.runtime_event_id = self._runtime_event_id(
            thread_id=thread_id,
            turn_id=turn_id,
            status=status,
        )
        self.failure_message = message or "Codex Turn ended without an error message"
        super().__init__(
            f"Codex turn '{turn_id}' ended with status '{status}': "
            f"{self.failure_message}"
        )

    @staticmethod
    def _runtime_event_id(*, thread_id: str, turn_id: str, status: str) -> str:
        digest = hashlib.sha256(f"{thread_id}:{turn_id}:{status}".encode()).hexdigest()
        return f"codex:{digest}:{status}"


class CodexTurnLostError(CodexTurnFailedError):
    """An in-flight Turn whose owned App Server connection disappeared."""

    def __init__(
        self,
        *,
        thread_id: str,
        turn_id: str,
        message: str,
    ) -> None:
        super().__init__(
            thread_id=thread_id,
            turn_id=turn_id,
            status="owner_lost",
            message=message,
            reason="runtime_owner_lost",
        )


class CodexProviderRateLimitedError(CodexTurnFailedError):
    """A Codex Turn ended because the Provider rejected more usage."""

    def __init__(
        self,
        *,
        thread_id: str,
        turn_id: str,
        message: str | None,
    ) -> None:
        super().__init__(
            thread_id=thread_id,
            turn_id=turn_id,
            status="failed",
            message=message,
            reason="provider_rate_limited",
        )


class CodexTurnCancelledError(CodexAppServerError):
    """A Turn interrupted through the product cancellation boundary."""

    def __init__(
        self,
        *,
        thread_id: str,
        turn_id: str,
        message: str | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.status = "interrupted"
        self.reason = "runtime_cancelled"
        self.runtime_event_id = CodexTurnFailedError._runtime_event_id(
            thread_id=thread_id,
            turn_id=turn_id,
            status=self.status,
        )
        self.cancellation_message = message or "Codex Turn was interrupted"
        super().__init__(
            f"Codex turn '{turn_id}' was interrupted: "
            f"{self.cancellation_message}"
        )


@dataclass(slots=True)
class _ActiveExecution:
    session: CodexAppServerSession
    waiting_result: RuntimeResult
    completion_lock: Lock = field(default_factory=Lock)
    completion: CodexCompletion | None = None
    recovered_turn: dict[str, Any] | None = None
    cancellation_requested: bool = False
    cancellation_acknowledged: bool = False


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
        app_server_endpoint: str | None = None,
        model: str | None = None,
        approval_policy: str = "on-request",
        output_schema: Mapping[str, Any] | None = None,
        approval_handler: Callable[
            [CodexApprovalRequest], Mapping[str, Any]
        ]
        | None = None,
        capacity_cache_seconds: float = 30.0,
    ) -> None:
        self.workspace_manager = workspace_manager
        self.resolve_workspace = resolve_workspace
        self.codex_home = self.workspace_manager.canonicalize(codex_home)
        if not self.codex_home.is_dir():
            raise CodexAppServerError("Codex home must be a managed directory")
        self.command = tuple(command)
        self.app_server_endpoint = app_server_endpoint
        self.model = model
        self.approval_policy = approval_policy
        self.output_schema = dict(output_schema) if output_schema is not None else None
        self._approval_handler = approval_handler
        self._active: dict[str, _ActiveExecution] = {}
        self._lock = RLock()
        self._capacity_cache_seconds = capacity_cache_seconds
        self._capacity_cache: tuple[float, RuntimeCapacity] | None = None

    def capacity(self) -> RuntimeCapacity:
        """Read and normalize Provider capacity without copying protocol state."""

        with self._lock:
            cached = self._capacity_cache
            if (
                cached is not None
                and time.monotonic() - cached[0] <= self._capacity_cache_seconds
            ):
                return cached[1]

        session = CodexAppServerSession(
            cwd=self.codex_home,
            command=self.command,
            endpoint=self.app_server_endpoint,
            env={"CODEX_HOME": str(self.codex_home)},
        )
        try:
            session.connect()
            capacity = self._normalize_capacity(session.read_account_rate_limits())
        except Exception:  # noqa: BLE001 - custom Providers may not expose this API
            capacity = RuntimeCapacity(
                status="unknown",
                reason="provider_capacity_unavailable",
            )
        finally:
            session.close()
        with self._lock:
            self._capacity_cache = (time.monotonic(), capacity)
        return capacity

    def _mark_rate_limited(self, reason: str) -> None:
        with self._lock:
            self._capacity_cache = (
                time.monotonic(),
                RuntimeCapacity(status="limited", reason=reason),
            )

    def set_approval_handler(
        self,
        handler: Callable[[CodexApprovalRequest], Mapping[str, Any]],
    ) -> None:
        """Register the product-owned blocking approval boundary."""

        with self._lock:
            self._approval_handler = handler

    def execute(
        self,
        *,
        execution_id: str,
        role_key: str,
        instructions: str,
        workspace_id: str | None = None,
        workspace_path: str | None = None,
        thread_id: str | None = None,
        output_schema: Mapping[str, Any] | None = None,
        runtime_config: RuntimeExecutionConfig | None = None,
    ) -> RuntimeResult:
        """Start one Thread and Turn, then return their durable identities."""

        del role_key
        with self._lock:
            existing = self._active.get(execution_id)
            if existing is not None:
                if existing.completion is not None:
                    return existing.completion.result
                return existing.waiting_result

            if workspace_id is not None or workspace_path is not None:
                if workspace_id is None or workspace_path is None:
                    raise CodexAppServerError(
                        "workspace_id and workspace_path must be supplied together"
                    )
                binding = RuntimeWorkspaceBinding(
                    workspace_id=workspace_id,
                    path=Path(workspace_path),
                )
            else:
                binding = self.resolve_workspace(execution_id)
            workspace = self.workspace_manager.canonicalize(binding.path)
            resolved_config = runtime_config or RuntimeExecutionConfig(
                binding_key="adapter-default",
                model=self.model,
                reasoning_effort=None,
                security_mode="workspace_restricted",
                approval_policy=self.approval_policy,
                sandbox_mode="workspace-write",
                network_access=False,
            )
            session = CodexAppServerSession(
                cwd=workspace,
                command=self.command,
                endpoint=self.app_server_endpoint,
                env={"CODEX_HOME": str(self.codex_home)},
            )
            try:
                session.connect()
                if thread_id is None:
                    thread_response = session.start_thread(
                        model=resolved_config.model,
                        approval_policy=resolved_config.approval_policy,
                        sandbox=resolved_config.sandbox_mode,
                    )
                else:
                    thread_response = session.resume_thread(
                        thread_id,
                        model=resolved_config.model,
                        approval_policy=resolved_config.approval_policy,
                        sandbox=resolved_config.sandbox_mode,
                    )
                resolved_thread_id = self._require_id(
                    thread_response,
                    container="thread",
                    label="thread",
                )
                if thread_id is not None and resolved_thread_id != thread_id:
                    raise CodexAppServerError(
                        "Codex App Server resumed a different Thread ID"
                    )
                self._require_cwd(thread_response, workspace)
                turn_response = session.start_turn(
                    thread_id=resolved_thread_id,
                    instructions=instructions,
                    execution_id=execution_id,
                    output_schema=(
                        dict(output_schema)
                        if output_schema is not None
                        else self.output_schema
                    ),
                    approval_policy=resolved_config.approval_policy,
                    model=resolved_config.model,
                    reasoning_effort=resolved_config.reasoning_effort,
                    sandbox_mode=resolved_config.sandbox_mode,
                    network_access=resolved_config.network_access,
                )
                turn_id = self._require_id(
                    turn_response,
                    container="turn",
                    label="turn",
                )
                waiting_result = RuntimeResult(
                    status="waiting",
                    runtime_job_id=turn_id,
                    thread_id=resolved_thread_id,
                    turn_id=turn_id,
                    workspace_id=binding.workspace_id,
                    actual_model=self._reported_model(thread_response),
                )
                self._active[execution_id] = _ActiveExecution(
                    session=session,
                    waiting_result=waiting_result,
                )
                return waiting_result
            except Exception:
                session.close()
                raise

    def recover(self, request: RuntimeRecoveryRequest) -> bool:
        """Reattach to a waiting Turn through an external App Server endpoint."""

        if self.app_server_endpoint is None:
            return False
        with self._lock:
            existing = self._active.get(request.execution_id)
            if existing is not None:
                return True

        workspace = self.workspace_manager.canonicalize(request.workspace_path)
        resolved_config = request.runtime_config or RuntimeExecutionConfig(
            binding_key="adapter-default",
            model=self.model,
            reasoning_effort=None,
            security_mode="workspace_restricted",
            approval_policy=self.approval_policy,
            sandbox_mode="workspace-write",
            network_access=False,
        )
        session = CodexAppServerSession(
            cwd=workspace,
            command=self.command,
            endpoint=self.app_server_endpoint,
            env={"CODEX_HOME": str(self.codex_home)},
        )
        try:
            session.connect()
            thread_response = session.resume_thread(
                request.thread_id,
                model=resolved_config.model,
                approval_policy=resolved_config.approval_policy,
                sandbox=resolved_config.sandbox_mode,
            )
            resolved_thread_id = self._require_id(
                thread_response,
                container="thread",
                label="thread",
            )
            if resolved_thread_id != request.thread_id:
                raise CodexAppServerError(
                    "Codex App Server resumed a different Thread ID"
                )
            self._require_cwd(thread_response, workspace)
            thread = thread_response.get("thread")
            turn = self._find_turn(thread, request.turn_id)
            if turn is None or turn.get("status") in {
                "completed",
                "failed",
                "interrupted",
            }:
                read_response = session.read_thread(
                    request.thread_id,
                    include_turns=True,
                )
                turn = self._find_turn(read_response.get("thread"), request.turn_id)
            if turn is None:
                raise CodexAppServerError(
                    f"Codex Thread has no recorded Turn '{request.turn_id}'"
                )
            waiting_result = RuntimeResult(
                status="waiting",
                runtime_job_id=request.runtime_job_id or request.turn_id,
                thread_id=request.thread_id,
                turn_id=request.turn_id,
                workspace_id=request.workspace_id,
                actual_model=self._reported_model(thread_response),
            )
            duplicate = False
            with self._lock:
                if request.execution_id in self._active:
                    duplicate = True
                else:
                    self._active[request.execution_id] = _ActiveExecution(
                        session=session,
                        waiting_result=waiting_result,
                        recovered_turn=(
                            dict(turn)
                            if turn.get("status")
                            in {"completed", "failed", "interrupted"}
                            else None
                        ),
                    )
            if duplicate:
                session.close()
            return True
        except Exception:
            session.close()
            raise

    def wait_for_completion(
        self,
        execution_id: str,
        *,
        expected_turn_id: str | None = None,
        timeout: float | None = None,
    ) -> CodexCompletion:
        """Wait outside LangGraph and normalize one terminal Turn notification."""

        with self._lock:
            active = self._active.get(execution_id)
            approval_handler = self._approval_handler
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
            if (
                expected_turn_id is not None
                and waiting.turn_id != expected_turn_id
            ):
                raise LookupError(
                    f"Codex execution '{execution_id}' now owns another Turn"
                )
            try:
                turn = active.recovered_turn
                active.recovered_turn = None
                if turn is None:
                    turn = active.session.wait_for_turn(
                        thread_id=waiting.thread_id,
                        turn_id=waiting.turn_id,
                        timeout=timeout,
                        server_request_handler=(
                            (
                                lambda request: self._handle_approval_request(
                                    execution_id=execution_id,
                                    waiting=waiting,
                                    request=request,
                                    handler=approval_handler,
                                )
                            )
                            if approval_handler is not None
                            else None
                        ),
                    )
            except CodexAppServerError as exc:
                if active.cancellation_acknowledged:
                    raise CodexTurnCancelledError(
                        thread_id=waiting.thread_id,
                        turn_id=waiting.turn_id,
                        message=str(exc),
                    ) from exc
                raise CodexTurnLostError(
                    thread_id=waiting.thread_id,
                    turn_id=waiting.turn_id,
                    message=str(exc),
                ) from exc
            status = turn.get("status")
            if status == "interrupted":
                raise CodexTurnCancelledError(
                    thread_id=waiting.thread_id,
                    turn_id=waiting.turn_id,
                    message=self._turn_error_message(turn),
                )
            if status != "completed":
                codex_error_info = self._turn_error_info(turn)
                if codex_error_info == "usageLimitExceeded":
                    self._mark_rate_limited("provider_usage_limit_exceeded")
                    raise CodexProviderRateLimitedError(
                        thread_id=waiting.thread_id,
                        turn_id=waiting.turn_id,
                        message=self._turn_error_message(turn),
                    )
                raise CodexTurnFailedError(
                    thread_id=waiting.thread_id,
                    turn_id=waiting.turn_id,
                    status=str(status),
                    message=self._turn_error_message(turn),
                )
            summary = self._last_agent_message(turn)
            result = RuntimeResult(
                status="completed",
                runtime_job_id=waiting.runtime_job_id,
                summary=summary,
                thread_id=waiting.thread_id,
                turn_id=waiting.turn_id,
                workspace_id=waiting.workspace_id,
                usage=(
                    turn.get("_mutiai_usage")
                    if isinstance(turn.get("_mutiai_usage"), RuntimeTokenUsage)
                    else None
                ),
                context_compactions=self._context_compactions(turn),
                actual_model=waiting.actual_model,
            )
            active.completion = CodexCompletion(
                execution_id=execution_id,
                runtime_event_id=(
                    f"codex:{waiting.thread_id}:{waiting.turn_id}:completed"
                ),
                result=result,
            )
            return active.completion

    def cancel(self, execution_id: str) -> bool:
        """Interrupt one active Turn without closing its event connection."""

        with self._lock:
            active = self._active.get(execution_id)
            if active is None:
                return False
            waiting = active.waiting_result
            if waiting.thread_id is None or waiting.turn_id is None:
                raise CodexAppServerError(
                    "active Codex execution has no Thread or Turn ID"
                )
            active.cancellation_requested = True
            session = active.session
            thread_id = waiting.thread_id
            turn_id = waiting.turn_id

        session.interrupt_turn(thread_id=thread_id, turn_id=turn_id)
        with self._lock:
            current = self._active.get(execution_id)
            if current is active:
                current.cancellation_acknowledged = True
        return True

    def is_active(self, execution_id: str) -> bool:
        """Return whether this adapter currently owns the execution process."""

        with self._lock:
            return execution_id in self._active

    def active_turn_id(self, execution_id: str) -> str | None:
        """Return the Turn currently owned for one execution, if any."""

        with self._lock:
            active = self._active.get(execution_id)
            return active.waiting_result.turn_id if active is not None else None

    def close_execution(
        self,
        execution_id: str,
        *,
        expected_turn_id: str | None = None,
    ) -> None:
        """Close one execution's App Server connection or owned process."""

        with self._lock:
            active = self._active.get(execution_id)
            if (
                active is not None
                and expected_turn_id is not None
                and active.waiting_result.turn_id != expected_turn_id
            ):
                return
            active = self._active.pop(execution_id, None)
        if active is not None:
            active.session.close()

    def close(self) -> None:
        """Close all App Server connections and any processes it owns."""

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
    def _reported_model(response: Mapping[str, Any]) -> str | None:
        model = response.get("model")
        if isinstance(model, str) and model:
            return model
        thread = response.get("thread")
        if isinstance(thread, Mapping):
            model = thread.get("model")
            if isinstance(model, str) and model:
                return model
        return None

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

    @staticmethod
    def _context_compactions(turn: Mapping[str, Any]) -> int:
        observed = turn.get("_mutiai_context_compactions")
        if isinstance(observed, int) and not isinstance(observed, bool):
            return max(0, observed)
        items = turn.get("items")
        if not isinstance(items, list):
            return 0
        return sum(
            1
            for item in items
            if isinstance(item, Mapping)
            and item.get("type") in {"context_compaction", "compaction"}
        )

    @staticmethod
    def _find_turn(
        thread: Mapping[str, Any] | None,
        turn_id: str,
    ) -> dict[str, Any] | None:
        if not isinstance(thread, Mapping):
            return None
        turns = thread.get("turns")
        if not isinstance(turns, list):
            return None
        return next(
            (
                turn
                for turn in turns
                if isinstance(turn, dict) and turn.get("id") == turn_id
            ),
            None,
        )

    @staticmethod
    def _turn_error_message(turn: Mapping[str, Any]) -> str | None:
        error = turn.get("error")
        if not isinstance(error, Mapping):
            return None
        message = error.get("message")
        codex_error_info = error.get("codexErrorInfo")
        if isinstance(message, str) and isinstance(codex_error_info, str):
            return f"{message} ({codex_error_info})"
        return message if isinstance(message, str) else None

    @staticmethod
    def _turn_error_info(turn: Mapping[str, Any]) -> str | None:
        error = turn.get("error")
        if not isinstance(error, Mapping):
            return None
        info = error.get("codexErrorInfo")
        return info if isinstance(info, str) else None

    @staticmethod
    def _normalize_capacity(payload: Mapping[str, Any]) -> RuntimeCapacity:
        snapshots: list[Mapping[str, Any]] = []
        by_limit = payload.get("rateLimitsByLimitId")
        if isinstance(by_limit, Mapping):
            codex = by_limit.get("codex")
            if isinstance(codex, Mapping):
                snapshots.append(codex)
        snapshot = payload.get("rateLimits")
        if isinstance(snapshot, Mapping):
            snapshots.append(snapshot)
        if not snapshots:
            return RuntimeCapacity(
                status="unknown",
                reason="provider_capacity_unavailable",
            )

        for item in snapshots:
            reached = item.get("rateLimitReachedType")
            if isinstance(reached, str) and reached:
                return RuntimeCapacity(
                    status="limited",
                    reason=reached,
                    resets_at=CodexRuntimeAdapter._reset_time(item),
                )
            if item.get("spendControlReached") is True:
                return RuntimeCapacity(
                    status="limited",
                    reason="provider_spend_control_reached",
                    resets_at=CodexRuntimeAdapter._reset_time(item),
                )
            credits = item.get("credits")
            if (
                isinstance(credits, Mapping)
                and credits.get("hasCredits") is False
                and credits.get("unlimited") is False
            ):
                return RuntimeCapacity(
                    status="limited",
                    reason="provider_credits_depleted",
                    resets_at=CodexRuntimeAdapter._reset_time(item),
                )
            for key in ("primary", "secondary"):
                window = item.get(key)
                if (
                    isinstance(window, Mapping)
                    and isinstance(window.get("usedPercent"), int)
                    and window["usedPercent"] >= 100
                ):
                    return RuntimeCapacity(
                        status="limited",
                        reason=f"provider_{key}_window_exhausted",
                        resets_at=CodexRuntimeAdapter._reset_time(item),
                    )
        return RuntimeCapacity(status="available")

    @staticmethod
    def _reset_time(snapshot: Mapping[str, Any]) -> int | None:
        values = [
            snapshot.get("primary"),
            snapshot.get("secondary"),
        ]
        resets = [
            item.get("resetsAt")
            for item in values
            if isinstance(item, Mapping) and isinstance(item.get("resetsAt"), int)
        ]
        return min(resets) if resets else None

    @staticmethod
    def _handle_approval_request(
        *,
        execution_id: str,
        waiting: RuntimeResult,
        request: Mapping[str, Any],
        handler: Callable[[CodexApprovalRequest], Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        method = request.get("method")
        kind_by_method = {
            "item/commandExecution/requestApproval": "command_execution",
            "item/fileChange/requestApproval": "file_change",
        }
        kind = kind_by_method.get(method)
        if kind is None:
            raise CodexAppServerError(
                f"unsupported App Server request '{method}' during Turn"
            )
        request_id = request.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            raise CodexAppServerError("approval request has no valid JSON-RPC ID")
        params = request.get("params")
        if not isinstance(params, Mapping):
            raise CodexAppServerError("approval request has no params object")
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        item_id = params.get("itemId")
        if not all(isinstance(value, str) and value for value in (thread_id, turn_id, item_id)):
            raise CodexAppServerError("approval request has incomplete Runtime IDs")
        if thread_id != waiting.thread_id or turn_id != waiting.turn_id:
            raise CodexAppServerError("approval request does not belong to the active Turn")

        details: dict[str, Any] = {}
        detail_fields = {
            "commandActions": "command_actions",
            "networkApprovalContext": "network_approval_context",
            "proposedExecpolicyAmendment": "proposed_execpolicy_amendment",
            "proposedNetworkPolicyAmendments": (
                "proposed_network_policy_amendments"
            ),
            "grantRoot": "grant_root",
        }
        for wire_name, product_name in detail_fields.items():
            value = params.get(wire_name)
            if value is not None:
                details[product_name] = value

        started_at_ms = params.get("startedAtMs")
        normalized_started_at_ms = (
            started_at_ms
            if isinstance(started_at_ms, int) and not isinstance(started_at_ms, bool)
            else None
        )
        response = handler(
            CodexApprovalRequest(
                execution_id=execution_id,
                request_id=request_id,
                kind=kind,
                thread_id=thread_id,
                turn_id=turn_id,
                item_id=item_id,
                reason=(
                    params.get("reason")
                    if isinstance(params.get("reason"), str)
                    else None
                ),
                command=(
                    params.get("command")
                    if isinstance(params.get("command"), str)
                    else None
                ),
                cwd=(
                    params.get("cwd")
                    if isinstance(params.get("cwd"), str)
                    else None
                ),
                details=details,
                runtime_started_at_ms=normalized_started_at_ms,
            )
        )
        decision = response.get("decision")
        if decision not in {"accept", "decline", "cancel"}:
            raise CodexAppServerError("product returned an unsupported approval decision")
        return {"decision": decision}
