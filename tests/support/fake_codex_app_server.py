"""Deterministic-enough JSONL App Server used by integration tests."""

import json
import os
import sys
import uuid


run_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
thread = {"id": f"thread-test-{run_id}", "status": {"type": "idle"}}
turn = {"id": f"turn-test-{run_id}", "status": "inProgress", "items": []}


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
        send({"method": "item/started", "params": {"turnId": turn["id"]}})
        send({"id": message["id"], "result": {"turn": turn}})
        send(
            {
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
            }
        )
