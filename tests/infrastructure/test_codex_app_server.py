import sys

from mutiai.runtime import (
    CodexAppServerSession,
    CodexRuntimeAdapter,
    RuntimeWorkspaceBinding,
    WorkspaceManager,
)


FAKE_APP_SERVER = r"""
import json
import sys

thread = {"id": "thread-test-1", "status": {"type": "idle"}}
turn = {"id": "turn-test-1", "status": "inProgress", "items": []}

def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"platformOs": "windows"}})
    elif method == "initialized":
        pass
    elif method == "thread/start":
        send({
            "id": message["id"],
            "result": {"thread": thread, "cwd": message["params"]["cwd"]},
        })
        send({"method": "thread/started", "params": {"thread": thread}})
    elif method == "thread/resume":
        send({"id": message["id"], "result": {"thread": thread}})
    elif method == "turn/start":
        send({"method": "item/started", "params": {"turnId": turn["id"]}})
        send({"id": message["id"], "result": {"turn": turn}})
        send({
            "method": "turn/completed",
            "params": {
                "threadId": message["params"]["threadId"],
                "turn": {
                    "id": turn["id"],
                    "status": "completed",
                    "items": [
                        {
                            "id": "message-test-1",
                            "type": "agentMessage",
                            "text": "Delivered the bounded assignment.",
                        }
                    ],
                },
            },
        })
"""


def test_codex_app_server_session_handshake_thread_resume_and_turn(tmp_path) -> None:
    fake_server = tmp_path / "fake_app_server.py"
    fake_server.write_text(FAKE_APP_SERVER, encoding="utf-8")

    with CodexAppServerSession(
        cwd=tmp_path,
        command=(sys.executable, str(fake_server)),
    ) as session:
        thread = session.start_thread()
        assert thread["thread"]["id"] == "thread-test-1"
        assert session.next_event()["method"] == "thread/started"

        resumed = session.resume_thread("thread-test-1")
        assert resumed["thread"]["id"] == "thread-test-1"
        turn = session.start_turn(
            thread_id="thread-test-1",
            instructions="Run the bounded assignment.",
            execution_id="execution-test-1",
            output_schema={"type": "object"},
        )
        assert turn["turn"]["id"] == "turn-test-1"
        completed = session.wait_for_turn(
            thread_id="thread-test-1",
            turn_id="turn-test-1",
        )
        assert completed["status"] == "completed"


def test_codex_runtime_adapter_submits_without_blocking_graph_node(tmp_path) -> None:
    fake_server = tmp_path / "fake_app_server.py"
    fake_server.write_text(FAKE_APP_SERVER, encoding="utf-8")
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
        command=(sys.executable, str(fake_server)),
    )

    try:
        waiting = adapter.execute(
            execution_id="execution-test-1",
            role_key="backend",
            instructions="Run the bounded assignment.",
        )
        assert waiting.status == "waiting"
        assert waiting.thread_id == "thread-test-1"
        assert waiting.turn_id == "turn-test-1"
        assert waiting.runtime_job_id == "turn-test-1"
        assert waiting.workspace_id == "workspace:execution-test-1"

        replayed = adapter.execute(
            execution_id="execution-test-1",
            role_key="backend",
            instructions="This replay must not start another Turn.",
        )
        assert replayed == waiting

        completion = adapter.wait_for_completion("execution-test-1")
        assert completion.runtime_event_id == (
            "codex:thread-test-1:turn-test-1:completed"
        )
        assert completion.result.status == "completed"
        assert completion.result.summary == "Delivered the bounded assignment."
        assert adapter.wait_for_completion("execution-test-1") == completion
    finally:
        adapter.close()
