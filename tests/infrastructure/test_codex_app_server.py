import sys
from pathlib import Path

from mutiai.runtime import (
    CodexAppServerSession,
    CodexRuntimeAdapter,
    RuntimeWorkspaceBinding,
    WorkspaceManager,
)

FAKE_APP_SERVER = (
    Path(__file__).resolve().parents[1] / "support" / "fake_codex_app_server.py"
)


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
