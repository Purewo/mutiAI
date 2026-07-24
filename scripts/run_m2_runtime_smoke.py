"""Run one isolated product Task through the local Codex Runtime boundary."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr

from mutiai.config import Settings, get_settings
from mutiai.main import create_app
from mutiai.runtime import WorkspaceManager


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one bounded product Task through an isolated local Codex Runtime."
        )
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=180.0,
        help="Maximum time to wait for the product Task (default: 180).",
    )
    parser.add_argument(
        "--request",
        default=(
            "Complete this bounded Runtime smoke assignment. Do not modify files, "
            "run commands, or request approvals. Return a concise completion summary."
        ),
        help="Bounded user request sent to the organization lead.",
    )
    return parser.parse_args()


def build_settings() -> tuple[Settings, Path, str]:
    base = get_settings()
    manager = WorkspaceManager(base.runtime_workspace_root)
    run_dir = manager.provision(
        Path("system") / "m2-acceptance" / uuid4().hex[:12]
    )
    password = token_urlsafe(24)
    settings = Settings(
        app_env="test",
        app_host=base.app_host,
        app_port=base.app_port,
        database_url=f"sqlite+pysqlite:///{run_dir / 'product.db'}",
        database_auto_migrate=True,
        langgraph_checkpoint_path=run_dir / "checkpoints.db",
        runtime_provider="codex",
        runtime_max_concurrent_executions=base.runtime_max_concurrent_executions,
        runtime_token_budget_limit=base.runtime_token_budget_limit,
        runtime_token_reservation_per_execution=(
            base.runtime_token_reservation_per_execution
        ),
        runtime_provider_capacity_cache_seconds=(
            base.runtime_provider_capacity_cache_seconds
        ),
        runtime_workspace_root=base.runtime_workspace_root,
        codex_app_server_endpoint=base.codex_app_server_endpoint,
        codex_app_server_ready_timeout_seconds=(
            base.codex_app_server_ready_timeout_seconds
        ),
        codex_model=base.codex_model,
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password=SecretStr(password),
        session_cookie_name=base.session_cookie_name,
        session_ttl_seconds=base.session_ttl_seconds,
    )
    return settings, run_dir, password


def organization_spec() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "M2 Runtime Acceptance",
        "description": "A bounded organization for the local Runtime smoke.",
        "roles": [
            {
                "role_key": "lead",
                "name": "Organization Lead",
                "responsibility": "Review the bounded delivery.",
                "is_lead": True,
                "reports_to": None,
                "runtime_binding_key": "codex-local-default",
            },
            {
                "role_key": "backend",
                "name": "Backend Specialist",
                "responsibility": "Complete the bounded smoke assignment.",
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": "codex-local-default",
            },
        ],
    }


def wait_for_terminal(
    client: TestClient,
    task_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] | None = None
    terminal = {"completed", "needs_revision", "failed", "cancelled"}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}")
        response.raise_for_status()
        last_payload = response.json()
        if last_payload["status"] in terminal:
            return last_payload
        time.sleep(0.2)
    raise TimeoutError(
        f"Task did not reach a terminal state before timeout: {last_payload}"
    )


def run() -> dict[str, Any]:
    args = parse_args()
    settings, run_dir, password = build_settings()
    app = create_app(settings)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": password},
        )
        login.raise_for_status()

        proposal = client.post(
            "/api/v1/organizations/proposals",
            json={"spec": organization_spec()},
        )
        proposal.raise_for_status()
        proposal_payload = proposal.json()
        version_url = (
            f"/api/v1/organizations/{proposal_payload['organization_id']}/versions/"
            f"{proposal_payload['spec_version_id']}"
        )
        client.post(version_url + "/confirm").raise_for_status()
        client.post(version_url + "/publish").raise_for_status()

        submitted = client.post(
            f"/api/v1/organizations/{proposal_payload['organization_id']}/tasks",
            headers={"Idempotency-Key": f"m2-smoke-{uuid4().hex}"},
            json={"request": args.request},
        )
        submitted.raise_for_status()
        task_payload = wait_for_terminal(
            client,
            submitted.json()["task_id"],
            timeout_seconds=args.timeout_seconds,
        )
        controls = client.get("/api/v1/runtime/controls")
        controls.raise_for_status()
        events = client.get(
            f"/api/v1/tasks/{task_payload['task_id']}/events"
        )
        events.raise_for_status()
        event_types = [
            line.removeprefix("event: ")
            for line in events.text.splitlines()
            if line.startswith("event: ")
        ]

    return {
        "task_id": task_payload["task_id"],
        "organization_id": proposal_payload["organization_id"],
        "task_status": task_payload["status"],
        "result_summary_present": bool(task_payload.get("result_summary")),
        "assignments": [
            {
                "role": item["agent_role_key"],
                "status": item["status"],
                "runtime_status": item["runtime_execution"]["status"],
                "usage_status": item["runtime_execution"]["usage_status"],
                "total_tokens": item["runtime_execution"]["total_tokens"],
            }
            for item in task_payload["assignments"]
        ],
        "provider_capacity_status": controls.json()["provider_capacity_status"],
        "provider_capacity_reason": controls.json()["provider_capacity_reason"],
        "tokens_consumed": controls.json()["tokens_consumed"],
        "event_types": event_types,
        "isolated_control_dir": str(run_dir),
    }


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["task_status"] != "completed":
        return 1
    if result["provider_capacity_status"] not in {
        "available",
        "limited",
        "unknown",
    }:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
