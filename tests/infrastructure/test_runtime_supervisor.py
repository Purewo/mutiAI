import sys
import time
from pathlib import Path
from threading import Event, Lock

import pytest

from mutiai.runtime import (
    CodexRuntimeAdapter,
    CodexRuntimeSupervisor,
    RuntimeWorkspaceBinding,
    WorkspaceManager,
)


FAKE_APP_SERVER = (
    Path(__file__).resolve().parents[1] / "support" / "fake_codex_app_server.py"
)


class RecordingOrchestrator:
    def __init__(self) -> None:
        self.completions: list[dict] = []
        self.errors: list[dict] = []
        self.completion_recorded = Event()
        self.error_recorded = Event()
        self._lock = Lock()

    def complete_runtime_execution(self, **payload) -> None:
        with self._lock:
            self.completions.append(payload)
        self.completion_recorded.set()

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
