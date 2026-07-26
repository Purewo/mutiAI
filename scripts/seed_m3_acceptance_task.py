"""Create one deterministic Task for an M3 browser acceptance scenario."""

from __future__ import annotations

import argparse
import json
import time
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import (
    HTTPCookieProcessor,
    Request,
    build_opener,
)
from uuid import uuid4

SCENARIOS = ("wait-cancel", "needs-revision", "approval")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed a Task into a running M3 acceptance backend."
    )
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="123456")
    return parser.parse_args()


class ApiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        data = None
        request_headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = Request(
            self.base_url + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                body = response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"{method} {path} returned HTTP {exc.code}: {body}"
            ) from exc
        return json.loads(body) if body else {}


def organization_spec(scenario: str) -> dict:
    return {
        "schema_version": "1.0",
        "name": f"M3 {scenario} acceptance organization",
        "description": "Isolated deterministic browser acceptance state.",
        "roles": [
            {
                "role_key": "lead",
                "name": "Organization Lead",
                "responsibility": "Plan, review, and summarize",
                "is_lead": True,
                "reports_to": None,
                "runtime_binding_key": "codex-local-default",
            },
            {
                "role_key": "backend",
                "name": "Backend Developer",
                "responsibility": "Implement backend behavior",
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": "codex-local-default",
            },
            {
                "role_key": "test",
                "name": "Test Engineer",
                "responsibility": "Verify backend behavior",
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": "codex-local-default",
            },
        ],
    }


def publish_organization(client: ApiClient, scenario: str) -> str:
    proposal = client.request(
        "POST",
        "/organizations/proposals",
        payload={"spec": organization_spec(scenario)},
    )
    organization_id = proposal["organization_id"]
    version_id = proposal["spec_version_id"]
    version_path = f"/organizations/{organization_id}/versions/{version_id}"
    client.request("POST", version_path + "/confirm")
    client.request("POST", version_path + "/publish")
    return organization_id


def wait_for_pending_approval(client: ApiClient, task_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        approvals = client.request("GET", f"/tasks/{task_id}/approvals")
        pending = [item for item in approvals if item["status"] == "pending"]
        if pending:
            return pending[0]
        time.sleep(0.1)
    raise RuntimeError("The approval scenario did not create a pending approval")


def seed(client: ApiClient, scenario: str) -> dict:
    organization_id = publish_organization(client, scenario)
    task_path = f"/organizations/{organization_id}/tasks"
    idempotency_key = f"m3-{scenario}-{uuid4()}"
    if scenario == "approval":
        task = client.request(
            "POST",
            task_path,
            headers={"Idempotency-Key": idempotency_key},
            payload={
                "request": "request-command-approval for the backend assignment."
            },
        )
        approval = wait_for_pending_approval(client, task["task_id"])
        return {
            "scenario": scenario,
            "organization_id": organization_id,
            "task_id": task["task_id"],
            "status": "pending_approval",
            "approval_id": approval["approval_id"],
            "command": approval["command"],
        }

    task = client.request(
        "POST",
        task_path,
        headers={"Idempotency-Key": idempotency_key},
        payload={
            "request": f"Run the deterministic {scenario} browser scenario.",
            "orchestration_mode": "planned",
        },
    )
    started = client.request("POST", f"/tasks/{task['task_id']}/start")
    expected_status = "waiting" if scenario == "wait-cancel" else "needs_revision"
    if started["status"] != expected_status:
        raise RuntimeError(
            f"Expected Task status '{expected_status}', got '{started['status']}'"
        )
    return {
        "scenario": scenario,
        "organization_id": organization_id,
        "task_id": task["task_id"],
        "status": started["status"],
    }


def main() -> None:
    args = parse_args()
    client = ApiClient(f"http://127.0.0.1:{args.port}/api/v1")
    client.request(
        "POST",
        "/auth/login",
        payload={"username": args.username, "password": args.password},
    )
    result = seed(client, args.scenario)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
