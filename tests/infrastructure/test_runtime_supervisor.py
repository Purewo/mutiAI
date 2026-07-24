import sys
import time
from pathlib import Path
from threading import Event, Lock

import pytest

from mutiai.runtime import (
    CodexRuntimeAdapter,
    CodexRuntimeSupervisor,
    CodexTurnCancelledError,
    CodexTurnFailedError,
    RuntimeWorkspaceBinding,
    WorkspaceManager,
)

FAKE_APP_SERVER = (
    Path(__file__).resolve().parents[1] / "support" / "fake_codex_app_server.py"
)


class RecordingOrchestrator:
    def __init__(self) -> None:
        self.completions: list[dict] = []
        self.failures: list[dict] = []
        self.cancellations: list[dict] = []
        self.errors: list[dict] = []
        self.completion_recorded = Event()
        self.failure_recorded = Event()
        self.cancellation_recorded = Event()
        self.error_recorded = Event()
        self._lock = Lock()

    def complete_runtime_execution(self, **payload) -> None:
        with self._lock:
            self.completions.append(payload)
        self.completion_recorded.set()

    def fail_runtime_execution(self, **payload) -> None:
        with self._lock:
            self.failures.append(payload)
        self.failure_recorded.set()

    def cancel_runtime_execution(self, **payload) -> None:
        with self._lock:
            self.cancellations.append(payload)
        self.cancellation_recorded.set()

    def record_runtime_watch_error(self, **payload) -> None:
        with self._lock:
            self.errors.append(payload)
        self.error_recorded.set()


def build_adapter(tmp_path: Path) -> CodexRuntimeAdapter:
    managed_root = tmp_path / "managed"
    codex_home = managed_root / "codex-home"
    workspace = managed_root / "workspaces" / "workspace-1"
    codex_home.mkdir(parents=True)
    workspace.mkdir(parents=True)
    manager = WorkspaceManager(managed_root, protected_roots=())
    return CodexRuntimeAdapter(
        workspace_manager=manager,
        resolve_workspace=lambda execution_id: RuntimeWorkspaceBinding(
            workspace_id=f"workspace:{execution_id}",
            path=workspace,
        ),
        codex_home=codex_home,
        command=(sys.executable, str(FAKE_APP_SERVER)),
    )


def wait_until_execution_closed(
    adapter: CodexRuntimeAdapter,
    execution_id: str,
    *,
    timeout: float = 5,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            adapter.wait_for_completion(execution_id)
        except LookupError:
            return
        except CodexTurnFailedError:
            pass
        except CodexTurnCancelledError:
            pass
        time.sleep(0.01)
    raise AssertionError(f"execution '{execution_id}' App Server was not closed")


def test_supervisor_delivers_once_and_closes_execution(tmp_path) -> None:
    adapter = build_adapter(tmp_path)
    orchestrator = RecordingOrchestrator()
    supervisor = CodexRuntimeSupervisor(adapter, orchestrator)
    execution_id = "execution-supervised-1"

    try:
        adapter.execute(
            execution_id=execution_id,
            role_key="backend",
            instructions="Run the bounded assignment.",
        )
        supervisor.watch(execution_id)
        supervisor.watch(execution_id)

        assert orchestrator.completion_recorded.wait(timeout=5)
        wait_until_execution_closed(adapter, execution_id)
        supervisor.watch(execution_id)
        time.sleep(0.05)

        assert len(orchestrator.completions) == 1
        assert orchestrator.completions[0]["execution_id"] == execution_id
        assert supervisor.error_for(execution_id) is None
        with pytest.raises(LookupError, match="is not active"):
            adapter.wait_for_completion(execution_id)
    finally:
        supervisor.close()


def test_supervisor_delivers_interrupted_turn_as_cancellation(tmp_path) -> None:
    adapter = build_adapter(tmp_path)
    orchestrator = RecordingOrchestrator()
    supervisor = CodexRuntimeSupervisor(adapter, orchestrator)
    execution_id = "execution-cancelled"

    try:
        waiting = adapter.execute(
            execution_id=execution_id,
            role_key="backend",
            instructions=(
                "Complete the task within this responsibility boundary: "
                "Implement backend behavior: hang-runtime-once"
            ),
        )
        supervisor.watch(execution_id)
        assert adapter.cancel(execution_id) is True

        assert orchestrator.cancellation_recorded.wait(timeout=5)
        wait_until_execution_closed(adapter, execution_id)

        assert len(orchestrator.cancellations) == 1
        cancellation = orchestrator.cancellations[0]
        assert cancellation["execution_id"] == execution_id
        assert cancellation["terminal_status"] == "interrupted"
        assert cancellation["reason"] == "runtime_cancelled"
        assert cancellation["thread_id"] == waiting.thread_id
        assert cancellation["turn_id"] == waiting.turn_id
        assert orchestrator.failures == []
        assert orchestrator.errors == []
    finally:
        supervisor.close()


def test_supervisor_keeps_worker_error_queryable_and_deduplicated(tmp_path) -> None:
    adapter = build_adapter(tmp_path)
    orchestrator = RecordingOrchestrator()
    supervisor = CodexRuntimeSupervisor(adapter, orchestrator)
    execution_id = "missing-execution"

    try:
        supervisor.watch(execution_id)
        supervisor.watch(execution_id)

        assert orchestrator.error_recorded.wait(timeout=5)
        error = supervisor.error_for(execution_id)
        assert error is not None
        assert "is not active" in error
        supervisor.watch(execution_id)
        time.sleep(0.05)
        assert len(orchestrator.errors) == 1
        assert orchestrator.errors[0]["execution_id"] == execution_id
    finally:
        supervisor.close()


def test_supervisor_delivers_terminal_turn_failure_once(tmp_path) -> None:
    adapter = build_adapter(tmp_path)
    orchestrator = RecordingOrchestrator()
    supervisor = CodexRuntimeSupervisor(adapter, orchestrator)
    execution_id = "execution-terminal-failure"

    try:
        waiting = adapter.execute(
            execution_id=execution_id,
            role_key="backend",
            instructions="Implement backend behavior: fail-runtime-once",
        )
        supervisor.watch(execution_id)
        supervisor.watch(execution_id)

        assert orchestrator.failure_recorded.wait(timeout=5)
        wait_until_execution_closed(adapter, execution_id)
        supervisor.watch(execution_id)
        time.sleep(0.05)

        assert len(orchestrator.failures) == 1
        failure = orchestrator.failures[0]
        assert failure["execution_id"] == execution_id
        assert failure["terminal_status"] == "failed"
        assert failure["thread_id"] == waiting.thread_id
        assert failure["turn_id"] == waiting.turn_id
        assert failure["runtime_event_id"].endswith(":failed")
        assert "simulated terminal Turn failure" in failure["error"]
        assert orchestrator.errors == []
    finally:
        supervisor.close()


def test_supervisor_converts_lost_app_server_owner_into_retryable_failure(
    tmp_path,
) -> None:
    adapter = build_adapter(tmp_path)
    orchestrator = RecordingOrchestrator()
    supervisor = CodexRuntimeSupervisor(adapter, orchestrator)
    execution_id = "execution-owner-lost"

    try:
        waiting = adapter.execute(
            execution_id=execution_id,
            role_key="backend",
            instructions="Implement backend behavior: crash-runtime-once",
        )
        supervisor.watch(execution_id)

        assert orchestrator.failure_recorded.wait(timeout=5)
        wait_until_execution_closed(adapter, execution_id)

        assert len(orchestrator.failures) == 1
        failure = orchestrator.failures[0]
        assert failure["execution_id"] == execution_id
        assert failure["terminal_status"] == "owner_lost"
        assert failure["reason"] == "runtime_owner_lost"
        assert failure["thread_id"] == waiting.thread_id
        assert failure["turn_id"] == waiting.turn_id
        assert failure["runtime_event_id"].endswith(":owner_lost")
        assert "Codex App Server exited" in failure["error"]
        assert orchestrator.errors == []
    finally:
        supervisor.close()
