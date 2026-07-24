import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest
from websockets.sync.server import ServerConnection, serve

from mutiai.runtime import (
    CodexApprovalRequest,
    CodexAppServerError,
    CodexAppServerSession,
    CodexRuntimeAdapter,
    CodexTurnFailedError,
    RuntimeRecoveryRequest,
    RuntimeWorkspaceBinding,
    WorkspaceManager,
)

FAKE_APP_SERVER = (
    Path(__file__).resolve().parents[1] / "support" / "fake_codex_app_server.py"
)


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

    server = serve(handler, "127.0.0.1", 0)
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
        assert adapter.wait_for_completion("execution-test-1") == completion
    finally:
        adapter.close()


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
