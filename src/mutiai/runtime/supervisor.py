"""Background delivery of Codex terminal events into product orchestration."""

from __future__ import annotations

import logging
from threading import RLock, Thread
from typing import Protocol

from mutiai.runtime.codex import CodexRuntimeAdapter


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
        self._threads: dict[str, Thread] = {}
        self._errors: dict[str, str] = {}
        self._lock = RLock()
        self._closed = False

    def watch(self, execution_id: str) -> None:
        """Start at most one completion worker for an active execution."""

        with self._lock:
            if self._closed:
                raise RuntimeError("Codex Runtime supervisor is closed")
            existing = self._threads.get(execution_id)
            if existing is not None:
                # Keep the completed worker marker so a late duplicate event
                # cannot start a second waiter for an already closed execution.
                return
            worker = Thread(
                target=self._wait_and_resume,
                args=(execution_id,),
                name=f"mutiai-codex-runtime-{execution_id}",
                daemon=True,
            )
            self._threads[execution_id] = worker
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
            workers = list(self._threads.values())
        self.adapter.close()
        for worker in workers:
            worker.join(timeout=2)

    def _wait_and_resume(self, execution_id: str) -> None:
        try:
            completion = self.adapter.wait_for_completion(execution_id)
            summary = completion.result.summary
            if not summary:
                raise RuntimeError("Codex completion did not contain a summary")
            self.orchestrator.complete_runtime_execution(
                execution_id=execution_id,
                runtime_event_id=completion.runtime_event_id,
                summary=summary,
                runtime_job_id=completion.result.runtime_job_id,
                last_event_position=completion.result.last_event_position,
            )
        except Exception as exc:
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
            self.adapter.close_execution(execution_id)
