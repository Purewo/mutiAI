"""Deterministic-enough JSONL App Server used by integration tests."""

import json
import os
import sys
import uuid
from pathlib import Path

run_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
thread = {"id": f"thread-test-{run_id}", "status": {"type": "idle"}}
turn = {"id": f"turn-test-{run_id}", "status": "inProgress", "items": []}
account = None


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"platformOs": "windows"}})
    elif method == "initialized":
        pass
    elif method == "account/read":
        send(
            {
                "id": message["id"],
                "result": {
                    "account": account,
                    "requiresOpenaiAuth": True,
                },
            }
        )
    elif method == "account/login/start":
        login_id = f"login-test-{run_id}"
        account = {"type": "chatgpt", "planType": "plus"}
        send(
            {
                "id": message["id"],
                "result": {
                    "type": "chatgptDeviceCode",
                    "loginId": login_id,
                    "verificationUrl": "https://auth.openai.com/codex/device",
                    "userCode": "TEST-CODE",
                },
            }
        )
        send(
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": login_id,
                    "success": True,
                    "error": None,
                },
            }
        )
        send(
            {
                "method": "account/updated",
                "params": {"authMode": "chatgpt", "planType": "plus"},
            }
        )
    elif method == "thread/start":
        send(
            {
                "id": message["id"],
                "result": {"thread": thread, "cwd": message["params"]["cwd"]},
            }
        )
        send({"method": "thread/started", "params": {"thread": thread}})
    elif method == "thread/resume":
        resumed_thread = dict(thread)
        resumed_thread["id"] = message["params"]["threadId"]
        send(
            {
                "id": message["id"],
                "result": {
                    "thread": resumed_thread,
                    "cwd": message["params"]["cwd"],
                },
            }
        )
    elif method == "turn/start":
        thread_id = message["params"]["threadId"]
        input_items = message["params"].get("input", [])
        instructions = "\n".join(
            item.get("text", "")
            for item in input_items
            if isinstance(item, dict) and item.get("type") == "text"
        )
        failure_marker = Path.cwd() / ".fake-codex-terminal-failure-seen"
        should_fail_once = (
            "fail-runtime-once" in instructions
            and "Implement backend behavior" in instructions
            and not failure_marker.exists()
        )
        if should_fail_once:
            failure_marker.write_text("failed", encoding="utf-8")
            send({"id": message["id"], "result": {"turn": turn}})
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {
                            "id": turn["id"],
                            "status": "failed",
                            "items": [],
                            "error": {
                                "message": "simulated terminal Turn failure",
                                "codexErrorInfo": "test_failure",
                            },
                        },
                    },
                }
            )
            continue
        hang_marker = Path.cwd() / ".fake-codex-hang-seen"
        should_hang_once = (
            "hang-runtime-once" in instructions
            and "Complete the task within this responsibility boundary" in instructions
            and "Implement backend behavior" in instructions
            and not hang_marker.exists()
        )
        if should_hang_once:
            hang_marker.write_text("waiting", encoding="utf-8")
            send({"id": message["id"], "result": {"turn": turn}})
            continue
        crash_marker = Path.cwd() / ".fake-codex-crash-seen"
        should_crash_once = (
            "crash-runtime-once" in instructions
            and "Implement backend behavior" in instructions
            and not crash_marker.exists()
        )
        if should_crash_once:
            crash_marker.write_text("crashed", encoding="utf-8")
            send({"id": message["id"], "result": {"turn": turn}})
            raise SystemExit(17)
        output_schema = message["params"].get("outputSchema")
        is_lead_review = (
            isinstance(output_schema, dict)
            and "decision" in output_schema.get("properties", {})
        )
        item_text = (
            json.dumps(
                {
                    "decision": "accepted",
                    "final_summary": (
                        "The organization lead accepted the specialist deliveries."
                    ),
                    "issues": [],
                }
            )
            if is_lead_review
            else "Delivered the bounded assignment."
        )
        item = {
            "id": "message-test-1",
            "type": "agentMessage",
            "text": item_text,
        }
        send(
            {
                "method": "item/started",
                "params": {"threadId": thread_id, "turnId": turn["id"]},
            }
        )
        send({"id": message["id"], "result": {"turn": turn}})
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn["id"],
                    "item": item,
                    "completedAtMs": 0,
                },
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {
                        "id": turn["id"],
                        "status": "completed",
                        "items": [],
                    },
                },
            }
        )
