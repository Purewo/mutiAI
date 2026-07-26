import json
import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest
from fastapi.testclient import TestClient
from websockets.datastructures import Headers
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve

from mutiai.config import Settings
from mutiai.main import create_app
from mutiai.runtime import (
    CodexApprovalRequest,
    CodexAppServerError,
    CodexAppServerSession,
    CodexRuntimeAdapter,
    CodexTurnFailedError,
    FakeRuntimeAdapter,
    RuntimeRecoveryRequest,
    RuntimeWorkspaceBinding,
    WorkspaceManager,
    require_codex_app_server_ready,
)
from mutiai.runtime.codex_app_server import strict_output_schema

FAKE_APP_SERVER = (
    Path(__file__).resolve().parents[1] / "support" / "fake_codex_app_server.py"
)


def test_codex_output_schema_is_normalized_for_strict_mode() -> None:
    original = {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "default": "1.0"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "inputs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                        },
                    },
                },
            },
        },
    }

    normalized = strict_output_schema(original)

    assert normalized["required"] == ["schema_version", "steps"]
    assert normalized["additionalProperties"] is False
    nested = normalized["properties"]["steps"]["items"]
    assert nested["required"] == ["name", "inputs"]
    assert nested["additionalProperties"] is False
    assert "default" not in normalized["properties"]["schema_version"]
    assert "default" not in nested["properties"]["inputs"]
    assert "default" in original["properties"]["schema_version"]


@contextmanager
def persistent_fake_endpoint(
    cwd: Path,
    *,
    complete_on_turn_start: bool = False,
) -> Iterator[str]:
    thread_id = "thread-persistent-recovery"
    turn_id = "turn-persistent-recovery"
    state = {"turn_status": "inProgress"}

    def thread_payload() -> dict:
        status = (
            {"type": "active", "activeFlags": []}
            if state["turn_status"] == "inProgress"
            else {"type": "idle"}
        )
        return {
            "id": thread_id,
            "cwd": str(cwd.resolve()),
            "status": status,
            "turns": [
                {
                    "id": turn_id,
                    "status": state["turn_status"],
                    "items": (
                        [
                            {
                                "id": "message-persistent-recovery",
                                "type": "agentMessage",
                                "text": "Recovered Turn completed.",
                            }
                        ]
                        if state["turn_status"] == "completed"
                        else []
                    ),
                }
            ],
        }

    def send_completion(websocket: ServerConnection) -> None:
        state["turn_status"] = "completed"
        websocket.send(
            json.dumps(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {
                            "id": "message-persistent-recovery",
                            "type": "agentMessage",
                            "text": "Recovered Turn completed.",
                        },
                    },
                }
            )
        )
        websocket.send(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {
                            "id": turn_id,
                            "status": "completed",
                            "items": [],
                        },
                    },
                }
            )
        )

    def handler(websocket: ServerConnection) -> None:
        for raw_message in websocket:
            message = json.loads(raw_message)
            method = message.get("method")
            request_id = message.get("id")
            if method == "initialize":
                websocket.send(
                    json.dumps(
                        {"id": request_id, "result": {"platformOs": "windows"}}
                    )
                )
            elif method == "thread/start":
                websocket.send(
                    json.dumps(
                        {
                            "id": request_id,
                            "result": {
                                "thread": {
                                    **thread_payload(),
                                    "status": {"type": "idle"},
                                    "turns": [],
                                },
                                "cwd": str(cwd.resolve()),
                            },
                        }
                    )
                )
            elif method == "account/rateLimits/read":
                websocket.send(
                    json.dumps(
                        {
                            "id": request_id,
                            "result": {
                                "rateLimits": {
                                    "limitId": "codex",
                                    "primary": {"usedPercent": 0},
                                }
                            },
                        }
                    )
                )
            elif method == "turn/start":
                state["turn_status"] = "inProgress"
                websocket.send(
                    json.dumps(
                        {
                            "id": request_id,
                            "result": {
                                "turn": {
                                    "id": turn_id,
                                    "status": "inProgress",
                                    "items": [],
                                }
                            },
                        }
                    )
                )
                if complete_on_turn_start:
                    state["turn_status"] = "completed"
            elif method == "thread/resume":
                websocket.send(
                    json.dumps(
                        {
                            "id": request_id,
                            "result": {
                                "thread": thread_payload(),
                                "cwd": str(cwd.resolve()),
                            },
                        }
                    )
                )
                if state["turn_status"] == "inProgress":
                    send_completion(websocket)
            elif method == "thread/read":
                websocket.send(
                    json.dumps(
                        {
                            "id": request_id,
                            "result": {"thread": thread_payload()},
                        }
                    )
                )

    def process_request(
        websocket: ServerConnection,
        request: Request,
    ) -> Response | None:
        del websocket
        if request.path != "/readyz":
            return None
        body = b"ready"
        return Response(
            200,
            "OK",
            Headers(
                [
                    ("Content-Type", "text/plain"),
                    ("Content-Length", str(len(body))),
                ]
            ),
            body,
        )

    server = serve(
        handler,
        "127.0.0.1",
        0,
        process_request=process_request,
    )
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        port = server.socket.getsockname()[1]
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.shutdown()
        worker.join(timeout=2)


def test_codex_app_server_session_handshake_thread_resume_and_turn(tmp_path) -> None:
    with CodexAppServerSession(
        cwd=tmp_path,
        command=(sys.executable, str(FAKE_APP_SERVER)),
    ) as session:
        account = session.read_account()
        assert account == {"account": None, "requiresOpenaiAuth": True}
        rate_limits = session.read_account_rate_limits()
        assert rate_limits["rateLimits"]["limitId"] == "codex"
        login = session.start_device_code_login()
        assert login["type"] == "chatgptDeviceCode"
        assert login["verificationUrl"] == "https://auth.openai.com/codex/device"
        assert login["userCode"] == "TEST-CODE"
        completed_login = session.wait_for_login(login_id=login["loginId"])
        assert completed_login["success"] is True
        assert session.next_event()["method"] == "account/updated"
        assert session.read_account()["account"]["type"] == "chatgpt"

        thread = session.start_thread()
        thread_id = thread["thread"]["id"]
        assert thread_id.startswith("thread-test-")
        assert session.next_event()["method"] == "thread/started"

        resumed = session.resume_thread(thread_id)
        assert resumed["thread"]["id"] == thread_id
        turn = session.start_turn(
            thread_id=thread_id,
            instructions="Run the bounded assignment.",
            execution_id="execution-test-1",
            output_schema={"type": "object"},
        )
        turn_id = turn["turn"]["id"]
        assert turn_id.startswith("turn-test-")
        completed = session.wait_for_turn(
            thread_id=thread_id,
            turn_id=turn_id,
        )
        assert completed["status"] == "completed"
        assert completed["items"] == [
            {
                "id": "message-test-1",
                "type": "agentMessage",
                "text": "Delivered the bounded assignment.",
            }
        ]
        assert completed["_mutiai_usage"].total_tokens == 16


def test_codex_app_server_supports_role_policy_and_counts_compaction(tmp_path) -> None:
    with CodexAppServerSession(
        cwd=tmp_path,
        command=(sys.executable, str(FAKE_APP_SERVER)),
    ) as session:
        thread = session.start_thread(
            model="role-model-test",
            approval_policy="never",
            sandbox="danger-full-access",
        )
        thread_id = thread["thread"]["id"]
        session.next_event()
        started = session.start_turn(
            thread_id=thread_id,
            instructions="emit-context-compaction",
            execution_id="execution-compaction",
            model="role-model-test",
            reasoning_effort="high",
            approval_policy="never",
            sandbox_mode="danger-full-access",
            network_access=True,
        )
        completed = session.wait_for_turn(
            thread_id=thread_id,
            turn_id=started["turn"]["id"],
        )
        assert completed["_mutiai_context_compactions"] == 1


def test_codex_app_server_emits_exact_role_wire_policy(tmp_path) -> None:
    session = CodexAppServerSession(cwd=tmp_path)
    calls: list[tuple[str, dict]] = []

    def request(method: str, params: dict | None = None, **kwargs):
        del kwargs
        calls.append((method, params or {}))
        if method == "thread/start":
            return {"thread": {"id": "thread-wire"}, "cwd": str(tmp_path)}
        return {"turn": {"id": "turn-wire"}}

    session.request = request  # type: ignore[method-assign]
    session.start_thread(
        model="backend-model",
        approval_policy="never",
        sandbox="danger-full-access",
    )
    session.start_turn(
        thread_id="thread-wire",
        instructions="bounded",
        execution_id="execution-wire",
        model="backend-model",
        reasoning_effort="medium",
        approval_policy="never",
        sandbox_mode="danger-full-access",
        network_access=True,
    )

    assert calls == [
        (
            "thread/start",
            {
                "cwd": str(tmp_path),
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "model": "backend-model",
            },
        ),
        (
            "turn/start",
            {
                "threadId": "thread-wire",
                "input": [{"type": "text", "text": "bounded"}],
                "cwd": str(tmp_path),
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
                "clientUserMessageId": "execution-wire",
                "model": "backend-model",
                "effort": "medium",
            },
        ),
    ]


def test_codex_app_server_session_interrupts_active_turn(tmp_path) -> None:
    with CodexAppServerSession(
        cwd=tmp_path,
        command=(sys.executable, str(FAKE_APP_SERVER)),
    ) as session:
        thread = session.start_thread()
        thread_id = thread["thread"]["id"]
        assert session.next_event()["method"] == "thread/started"
        started = session.start_turn(
            thread_id=thread_id,
            instructions=(
                "Complete the task within this responsibility boundary: "
                "Implement backend behavior: hang-runtime-once"
            ),
            execution_id="execution-interrupt",
        )
        turn_id = started["turn"]["id"]

        assert session.interrupt_turn(
            thread_id=thread_id,
            turn_id=turn_id,
        ) == {}
        interrupted = session.wait_for_turn(
            thread_id=thread_id,
            turn_id=turn_id,
        )

        assert interrupted["status"] == "interrupted"
        assert interrupted["error"]["message"] == (
            "task cancellation interrupted the Turn"
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "ws://example.com:4510",
        "ws://192.0.2.1:4510",
        "unix://relative-codex.sock",
    ],
)
def test_codex_app_server_session_rejects_nonlocal_endpoint(
    tmp_path,
    endpoint: str,
) -> None:
    session = CodexAppServerSession(cwd=tmp_path, endpoint=endpoint)

    with pytest.raises(CodexAppServerError) as captured:
        session.connect()

    assert "failed to connect" in str(captured.value)
    assert isinstance(captured.value.__cause__, CodexAppServerError)


def test_codex_app_server_readiness_uses_local_readyz(tmp_path) -> None:
    with persistent_fake_endpoint(tmp_path) as endpoint:
        require_codex_app_server_ready(endpoint, timeout=1)


def test_create_app_builds_codex_adapter_from_settings(tmp_path) -> None:
    with persistent_fake_endpoint(tmp_path) as endpoint:
        settings = Settings(
            app_env="test",
            database_url=f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}",
            langgraph_checkpoint_path=tmp_path / "cp.db",
            runtime_workspace_root=tmp_path / "runtime",
            runtime_provider="codex",
            codex_app_server_endpoint=endpoint,
            bootstrap_admin_enabled=True,
            bootstrap_admin_username="admin",
            bootstrap_admin_password="123456",
        )
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/api/v1/health").status_code == 200
        assert isinstance(app.state.runtime_adapter, CodexRuntimeAdapter)
        assert app.state.runtime_adapter.app_server_endpoint == endpoint


def test_create_app_rejects_unavailable_codex_sidecar(tmp_path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}",
        langgraph_checkpoint_path=tmp_path / "cp.db",
        runtime_workspace_root=tmp_path / "runtime",
        runtime_provider="codex",
        codex_app_server_endpoint=f"ws://127.0.0.1:{port}",
        codex_app_server_ready_timeout_seconds=0.1,
        bootstrap_admin_enabled=False,
    )
    app = create_app(settings)

    with pytest.raises(
        CodexAppServerError,
        match="endpoint is not ready",
    ), TestClient(app):
        pass


def test_create_app_keeps_explicit_adapter_outside_sidecar_assembly(tmp_path) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}",
        langgraph_checkpoint_path=tmp_path / "cp.db",
        runtime_workspace_root=tmp_path / "runtime",
        runtime_provider="codex",
        codex_app_server_endpoint="ws://127.0.0.1:1",
        bootstrap_admin_enabled=False,
        assistant_runtime_provider="inherit",
    )

    app = create_app(settings, runtime_adapter=FakeRuntimeAdapter())
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
    assert app.state.runtime_adapter.provider == "fake"


def test_codex_runtime_adapter_rejoins_external_app_server_turn(tmp_path) -> None:
    managed_root = tmp_path / "managed"
    codex_home = managed_root / "codex-home"
    workspace = managed_root / "workspaces" / "workspace-1"
    codex_home.mkdir(parents=True)
    workspace.mkdir(parents=True)
    manager = WorkspaceManager(managed_root, protected_roots=())

    def resolve_workspace(execution_id: str) -> RuntimeWorkspaceBinding:
        return RuntimeWorkspaceBinding(workspace_id="workspace-1", path=workspace)

    with persistent_fake_endpoint(workspace) as endpoint:
        first = CodexRuntimeAdapter(
            workspace_manager=manager,
            resolve_workspace=resolve_workspace,
            codex_home=codex_home,
            app_server_endpoint=endpoint,
        )
        waiting = first.execute(
            execution_id="execution-recovery",
            role_key="backend",
            instructions="Keep this Turn in progress.",
        )
        first.close()

        second = CodexRuntimeAdapter(
            workspace_manager=manager,
            resolve_workspace=resolve_workspace,
            codex_home=codex_home,
            app_server_endpoint=endpoint,
        )
        try:
            assert second.recover(
                RuntimeRecoveryRequest(
                    execution_id="execution-recovery",
                    runtime_job_id=waiting.runtime_job_id,
                    thread_id=waiting.thread_id or "",
                    turn_id=waiting.turn_id or "",
                    workspace_id=waiting.workspace_id or "",
                    workspace_path=str(workspace),
                )
            )
            completion = second.wait_for_completion("execution-recovery")
            assert completion.result.summary == "Recovered Turn completed."
            assert completion.result.thread_id == waiting.thread_id
            assert completion.result.turn_id == waiting.turn_id
        finally:
            second.close()


def test_codex_runtime_adapter_recovers_completed_turn_from_history(tmp_path) -> None:
    managed_root = tmp_path / "managed"
    codex_home = managed_root / "codex-home"
    workspace = managed_root / "workspaces" / "workspace-1"
    codex_home.mkdir(parents=True)
    workspace.mkdir(parents=True)
    manager = WorkspaceManager(managed_root, protected_roots=())
    def resolver(execution_id: str) -> RuntimeWorkspaceBinding:
        return RuntimeWorkspaceBinding(workspace_id="workspace-1", path=workspace)

    with persistent_fake_endpoint(
        workspace,
        complete_on_turn_start=True,
    ) as endpoint:
        first = CodexRuntimeAdapter(
            workspace_manager=manager,
            resolve_workspace=resolver,
            codex_home=codex_home,
            app_server_endpoint=endpoint,
        )
        waiting = first.execute(
            execution_id="execution-history-recovery",
            role_key="backend",
            instructions="Complete before the new owner reconnects.",
        )
        first.close()

        second = CodexRuntimeAdapter(
            workspace_manager=manager,
            resolve_workspace=resolver,
            codex_home=codex_home,
            app_server_endpoint=endpoint,
        )
        try:
            assert second.recover(
                RuntimeRecoveryRequest(
                    execution_id="execution-history-recovery",
                    runtime_job_id=waiting.runtime_job_id,
                    thread_id=waiting.thread_id or "",
                    turn_id=waiting.turn_id or "",
                    workspace_id=waiting.workspace_id or "",
                    workspace_path=str(workspace),
                )
            )
            completion = second.wait_for_completion(
                "execution-history-recovery"
            )
            assert completion.result.summary == "Recovered Turn completed."
            assert completion.result.turn_id == waiting.turn_id
        finally:
            second.close()


def test_codex_runtime_adapter_submits_without_blocking_graph_node(tmp_path) -> None:
    managed_root = tmp_path / "managed"
    codex_home = managed_root / "codex-home"
    workspace = managed_root / "workspaces" / "workspace-1"
    codex_home.mkdir(parents=True)
    workspace.mkdir(parents=True)
    manager = WorkspaceManager(managed_root, protected_roots=())
    adapter = CodexRuntimeAdapter(
        workspace_manager=manager,
        resolve_workspace=lambda execution_id: RuntimeWorkspaceBinding(
            workspace_id=f"workspace:{execution_id}",
            path=workspace,
        ),
        codex_home=codex_home,
        command=(sys.executable, str(FAKE_APP_SERVER)),
    )

    try:
        assert adapter.capacity().status == "available"
        waiting = adapter.execute(
            execution_id="execution-test-1",
            role_key="backend",
            instructions="Run the bounded assignment.",
        )
        assert waiting.status == "waiting"
        assert waiting.thread_id is not None
        assert waiting.thread_id.startswith("thread-test-")
        assert waiting.turn_id is not None
        assert waiting.turn_id.startswith("turn-test-")
        assert waiting.runtime_job_id == waiting.turn_id
        assert waiting.workspace_id == "workspace:execution-test-1"

        replayed = adapter.execute(
            execution_id="execution-test-1",
            role_key="backend",
            instructions="This replay must not start another Turn.",
        )
        assert replayed == waiting

        completion = adapter.wait_for_completion("execution-test-1")
        assert completion.runtime_event_id == (
            f"codex:{waiting.thread_id}:{waiting.turn_id}:completed"
        )
        assert completion.result.status == "completed"
        assert completion.result.summary == "Delivered the bounded assignment."
        assert completion.result.usage is not None
        assert completion.result.usage.total_tokens == 16
        assert adapter.wait_for_completion("execution-test-1") == completion
    finally:
        adapter.close()


def test_codex_capacity_normalizes_available_and_limited_snapshots() -> None:
    available = CodexRuntimeAdapter._normalize_capacity(
        {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 42, "resetsAt": 2_000_000_000},
            }
        }
    )
    limited = CodexRuntimeAdapter._normalize_capacity(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "rateLimitReachedType": "primary",
                    "primary": {
                        "usedPercent": 100,
                        "resetsAt": 2_000_000_000,
                    },
                }
            }
        }
    )

    assert available.status == "available"
    assert available.reason is None
    assert limited.status == "limited"
    assert limited.reason == "primary"
    assert limited.resets_at == 2_000_000_000


def test_codex_capacity_is_unknown_when_custom_provider_omits_api(
    tmp_path,
    monkeypatch,
) -> None:
    class UnsupportedRateLimitsSession:
        def __init__(self, **kwargs) -> None:
            pass

        def connect(self) -> None:
            raise CodexAppServerError("method not found")

        def close(self) -> None:
            pass

    managed_root = tmp_path / "managed"
    codex_home = managed_root / "codex-home"
    workspace = managed_root / "workspaces" / "workspace-1"
    codex_home.mkdir(parents=True)
    workspace.mkdir(parents=True)
    manager = WorkspaceManager(managed_root, protected_roots=())
    monkeypatch.setattr(
        "mutiai.runtime.codex.CodexAppServerSession",
        UnsupportedRateLimitsSession,
    )
    adapter = CodexRuntimeAdapter(
        workspace_manager=manager,
        resolve_workspace=lambda execution_id: RuntimeWorkspaceBinding(
            workspace_id=f"workspace:{execution_id}",
            path=workspace,
        ),
        codex_home=codex_home,
        command=(sys.executable, str(FAKE_APP_SERVER)),
    )

    capacity = adapter.capacity()

    assert capacity.status == "unknown"
    assert capacity.reason == "provider_capacity_unavailable"


def test_codex_runtime_adapter_exposes_terminal_turn_failure(tmp_path) -> None:
    managed_root = tmp_path / "managed"
    codex_home = managed_root / "codex-home"
    workspace = managed_root / "workspaces" / "workspace-1"
    codex_home.mkdir(parents=True)
    workspace.mkdir(parents=True)
    manager = WorkspaceManager(managed_root, protected_roots=())
    adapter = CodexRuntimeAdapter(
        workspace_manager=manager,
        resolve_workspace=lambda execution_id: RuntimeWorkspaceBinding(
            workspace_id=f"workspace:{execution_id}",
            path=workspace,
        ),
        codex_home=codex_home,
        command=(sys.executable, str(FAKE_APP_SERVER)),
    )
    execution_id = "execution-terminal-failure"

    try:
        waiting = adapter.execute(
            execution_id=execution_id,
            role_key="backend",
            instructions="Implement backend behavior: fail-runtime-once",
        )

        with pytest.raises(CodexTurnFailedError) as captured:
            adapter.wait_for_completion(execution_id)

        failure = captured.value
        assert failure.thread_id == waiting.thread_id
        assert failure.turn_id == waiting.turn_id
        assert failure.status == "failed"
        assert failure.runtime_event_id.endswith(":failed")
        assert "simulated terminal Turn failure" in str(failure)
        assert "test_failure" in str(failure)
    finally:
        adapter.close()


@pytest.mark.parametrize(
    ("marker", "decision", "expected_kind"),
    [
        ("request-command-approval", "accept", "command_execution"),
        ("request-file-approval", "decline", "file_change"),
    ],
)
def test_codex_runtime_adapter_routes_approval_response_to_active_turn(
    tmp_path,
    marker: str,
    decision: str,
    expected_kind: str,
) -> None:
    managed_root = tmp_path / "managed"
    codex_home = managed_root / "codex-home"
    workspace = managed_root / "workspaces" / "workspace-1"
    codex_home.mkdir(parents=True)
    workspace.mkdir(parents=True)
    manager = WorkspaceManager(managed_root, protected_roots=())
    captured: list[CodexApprovalRequest] = []

    def decide(request: CodexApprovalRequest) -> dict[str, str]:
        captured.append(request)
        return {"decision": decision}

    adapter = CodexRuntimeAdapter(
        workspace_manager=manager,
        resolve_workspace=lambda execution_id: RuntimeWorkspaceBinding(
            workspace_id=f"workspace:{execution_id}",
            path=workspace,
        ),
        codex_home=codex_home,
        command=(sys.executable, str(FAKE_APP_SERVER)),
        approval_handler=decide,
    )
    execution_id = f"execution-{expected_kind}"

    try:
        waiting = adapter.execute(
            execution_id=execution_id,
            role_key="backend",
            instructions=f"Implement backend behavior: {marker}",
        )
        completion = adapter.wait_for_completion(execution_id)

        assert completion.result.summary == f"Approval decision '{decision}' was applied."
        assert len(captured) == 1
        approval = captured[0]
        assert approval.execution_id == execution_id
        assert approval.kind == expected_kind
        assert approval.thread_id == waiting.thread_id
        assert approval.turn_id == waiting.turn_id
        assert approval.item_id
        if expected_kind == "command_execution":
            assert approval.command == "python -m pytest"
            assert approval.cwd == str(workspace.resolve())
            assert approval.details["command_actions"][0]["type"] == "unknown"
        else:
            assert approval.command is None
            assert approval.details["grant_root"] == str(workspace.resolve())
    finally:
        adapter.close()
