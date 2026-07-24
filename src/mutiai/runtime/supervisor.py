"""Background delivery of Codex terminal events into product orchestration."""

from __future__ import annotations

import logging
from threading import RLock, Thread
from typing import Protocol

from mutiai.runtime.codex import (
    CodexRuntimeAdapter,
    CodexTurnCancelledError,
    CodexTurnFailedError,
    CodexTurnLostError,
)

logger = logging.getLogger(__name__)


class RuntimeCompletionSink(Protocol):
    """Receives normalized terminal events without owning Runtime internals."""

    def complete_runtime_execution(
        self,
        *,
        execution_id: str,
        runtime_event_id: str,
        summary: str,
        runtime_job_id: str | None = None,
        last_event_position: str | None = None,
    ) -> object: ...

    def fail_runtime_execution(
        self,
        *,
        execution_id: str,
        runtime_event_id: str,
        terminal_status: str,
        error: str,
        runtime_job_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        reason: str = "runtime_terminal_failure",
    ) -> object: ...

    def cancel_runtime_execution(
        self,
        *,
        execution_id: str,
        runtime_event_id: str,
        terminal_status: str,
        runtime_job_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        reason: str = "runtime_cancelled",
    ) -> object: ...

    def record_runtime_watch_error(
        self,
        *,
        execution_id: str,
        error: str,
    ) -> None: ...


class CodexRuntimeSupervisor:
    """Wait outside LangGraph and deliver normalized completion events."""

    def __init__(
        self,
        adapter: CodexRuntimeAdapter,
        orchestrator: RuntimeCompletionSink,
    ) -> None:
        self.adapter = adapter
        self.orchestrator = orchestrator
        self._threads: dict[str, tuple[str | None, Thread]] = {}
        self._errors: dict[str, str] = {}
        self._lock = RLock()
        self._closed = False

    def watch(self, execution_id: str) -> None:
        """Start at most one completion worker for an active execution."""

        with self._lock:
            if self._closed:
                raise RuntimeError("Codex Runtime supervisor is closed")
            active_turn_id = self.adapter.active_turn_id(execution_id)
            existing = self._threads.get(execution_id)
            if existing is not None:
                watched_turn_id, _ = existing
                if active_turn_id is None or watched_turn_id == active_turn_id:
                    return
            self._errors.pop(execution_id, None)
            worker = Thread(
                target=self._wait_and_resume,
                args=(execution_id, active_turn_id),
                name=f"mutiai-codex-runtime-{execution_id}",
                daemon=True,
            )
            self._threads[execution_id] = (active_turn_id, worker)
            worker.start()

    def error_for(self, execution_id: str) -> str | None:
        """Return a sanitized worker error for diagnostics."""

        with self._lock:
            return self._errors.get(execution_id)

    def close(self) -> None:
        """Stop owned App Server processes and join local worker threads."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            workers = [worker for _, worker in self._threads.values()]
        self.adapter.close()
        for worker in workers:
            worker.join(timeout=2)

    def _wait_and_resume(
        self,
        execution_id: str,
        expected_turn_id: str | None,
    ) -> None:
        try:
            completion = self.adapter.wait_for_completion(
                execution_id,
                expected_turn_id=expected_turn_id,
            )
            summary = completion.result.summary
            if not summary:
                raise RuntimeError("Codex completion did not contain a summary")
            with self._lock:
                if self._closed:
                    return
                self.orchestrator.complete_runtime_execution(
                    execution_id=execution_id,
                    runtime_event_id=completion.runtime_event_id,
                    summary=summary,
                    runtime_job_id=completion.result.runtime_job_id,
                    last_event_position=completion.result.last_event_position,
                )
        except CodexTurnCancelledError as exc:
            with self._lock:
                if self._closed:
                    return
            self.orchestrator.cancel_runtime_execution(
                execution_id=execution_id,
                runtime_event_id=exc.runtime_event_id,
                terminal_status=exc.status,
                runtime_job_id=exc.turn_id,
                thread_id=exc.thread_id,
                turn_id=exc.turn_id,
                reason=exc.reason,
            )
        except CodexTurnLostError as exc:
            with self._lock:
                if self._closed:
                    return
            self._record_runtime_failure(execution_id, exc)
        except CodexTurnFailedError as exc:
            self._record_runtime_failure(execution_id, exc)
        except Exception as exc:  # noqa: BLE001 - supervisor boundary
            message = str(exc)[:1000]
            with self._lock:
                self._errors[execution_id] = message
            try:
                self.orchestrator.record_runtime_watch_error(
                    execution_id=execution_id,
                    error=message,
                )
            except Exception:
                # The in-memory diagnostic remains available if persistence is
                # impossible during shutdown or a database failure.
                logger.exception(
                    "failed to persist Codex Runtime supervisor error",
                    extra={"execution_id": execution_id},
                )
        finally:
            self.adapter.close_execution(
                execution_id,
                expected_turn_id=expected_turn_id,
            )

    def _record_runtime_failure(
        self,
        execution_id: str,
        failure: CodexTurnFailedError,
    ) -> None:
        message = str(failure)[:1000]
        with self._lock:
            self._errors[execution_id] = message
        self.adapter.close_execution(
            execution_id,
            expected_turn_id=failure.turn_id,
        )
        try:
            self.orchestrator.fail_runtime_execution(
                execution_id=execution_id,
                runtime_event_id=failure.runtime_event_id,
                terminal_status=failure.status,
                error=message,
                runtime_job_id=failure.turn_id,
                thread_id=failure.thread_id,
                turn_id=failure.turn_id,
                reason=failure.reason,
            )
        except Exception:
            logger.exception(
                "failed to persist terminal Codex Runtime failure",
                extra={"execution_id": execution_id},
            )
            try:
                self.orchestrator.record_runtime_watch_error(
                    execution_id=execution_id,
                    error=message,
                )
            except Exception:
                logger.exception(
                    "failed to persist Codex Runtime failure fallback",
                    extra={"execution_id": execution_id},
                )
