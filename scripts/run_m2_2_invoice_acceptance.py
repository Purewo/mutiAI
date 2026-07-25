"""Run the real planned invoice workflow through the local Codex Runtime."""

from __future__ import annotations

import argparse
import base64
import json
import time
import zipfile
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree

from fastapi.testclient import TestClient
from pydantic import SecretStr

from mutiai.config import Settings, get_settings
from mutiai.main import create_app
from mutiai.runtime import WorkspaceManager

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TERMINAL_TASK_STATUSES = {"completed", "needs_revision", "failed", "cancelled"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the real M2.2 invoice planning and Artifact acceptance flow."
    )
    parser.add_argument("image", type=Path, help="Source invoice JPEG or PNG file.")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--exchange-rate", default="7.20")
    parser.add_argument("--timeout-seconds", type=float, default=1200.0)
    return parser.parse_args()


def build_settings(
    *,
    model: str,
    reasoning_effort: str,
) -> tuple[Settings, Path, str]:
    base = get_settings()
    manager = WorkspaceManager(base.runtime_workspace_root)
    run_dir = manager.provision(
        Path("system") / "m2-2-invoice-acceptance" / uuid4().hex[:12]
    )
    password = token_urlsafe(24)
    settings = Settings(
        app_env="test",
        app_host="127.0.0.1",
        database_url=f"sqlite+pysqlite:///{run_dir / 'product.db'}",
        database_auto_migrate=True,
        langgraph_checkpoint_path=run_dir / "checkpoints.db",
        runtime_provider="codex",
        runtime_max_concurrent_executions=1,
        runtime_provider_capacity_cache_seconds=0,
        runtime_default_binding_key="codex-local-default",
        runtime_security_mode="demo_full_access",
        runtime_workspace_root=base.runtime_workspace_root,
        codex_app_server_endpoint=base.codex_app_server_endpoint,
        codex_app_server_ready_timeout_seconds=(
            base.codex_app_server_ready_timeout_seconds
        ),
        codex_model=model,
        codex_reasoning_effort=reasoning_effort,
        bootstrap_admin_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password=SecretStr(password),
        session_cookie_name=base.session_cookie_name,
        session_ttl_seconds=base.session_ttl_seconds,
    )
    return settings, run_dir, password


def organization_spec() -> dict[str, Any]:
    binding = "codex-local-default"
    return {
        "schema_version": "1.0",
        "name": "Invoice Processing Organization",
        "description": "A strict linear organization for invoice acceptance.",
        "roles": [
            {
                "role_key": "lead",
                "name": "Organization Lead",
                "responsibility": (
                    "Create a strict linear plan using every specialist exactly once, "
                    "then review the final released workbook. Do not perform specialist "
                    "work or repair specialist files."
                ),
                "is_lead": True,
                "reports_to": None,
                "runtime_binding_key": binding,
            },
            {
                "role_key": "content_extractor",
                "name": "Invoice Content Extractor",
                "responsibility": (
                    "Read only the materialized source invoice image and publish a "
                    "structured JSON Artifact containing all invoice fields and CNY "
                    "monetary values. Do not create or edit Excel files."
                ),
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": binding,
            },
            {
                "role_key": "excel_builder",
                "name": "Excel Builder",
                "responsibility": (
                    "Read only the released extraction JSON and create a valid XLSX "
                    "workbook preserving the extracted CNY values. Do not inspect the "
                    "source invoice image or calculate USD values."
                ),
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": binding,
            },
            {
                "role_key": "currency_translator",
                "name": "Currency Translator",
                "responsibility": (
                    "Read only the released CNY workbook and publish a final XLSX that "
                    "preserves each original CNY value, records the supplied CNY per USD "
                    "rate, and adds correctly rounded two-decimal USD values. Do not read "
                    "the source image or reconstruct extraction data."
                ),
                "is_lead": False,
                "reports_to": "lead",
                "runtime_binding_key": binding,
            },
        ],
    }


def task_request(exchange_rate: str) -> str:
    return (
        "Process the uploaded Chinese electronic invoice through this exact linear "
        "responsibility chain: content_extractor -> excel_builder -> "
        "currency_translator -> lead review. The content extractor must publish JSON, "
        "the Excel builder must publish a CNY XLSX workbook from that JSON, and the "
        "currency translator must publish the final USD XLSX workbook from the CNY "
        f"workbook. Use the fixed conversion rate 1 USD = {exchange_rate} CNY. Preserve "
        "the original CNY values and add USD values rounded to two decimals. The final "
        "lead reviews released Artifacts only and must not repair files."
    )


def wait_for_plan(
    client: TestClient,
    task_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}")
        response.raise_for_status()
        last_payload = response.json()
        if last_payload["status"] in TERMINAL_TASK_STATUSES:
            raise RuntimeError(
                "Task reached a terminal state before planning completed: "
                f"{last_payload['status']}"
            )
        if (
            last_payload.get("execution_plan") is not None
            and last_payload["status"] == "created"
        ):
            return last_payload
        time.sleep(0.5)
    raise TimeoutError(f"Planning did not complete before timeout: {last_payload}")


def wait_for_terminal(
    client: TestClient,
    task_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}")
        response.raise_for_status()
        last_payload = response.json()
        if last_payload["status"] in TERMINAL_TASK_STATUSES:
            return last_payload
        time.sleep(0.5)
    raise TimeoutError(f"Task did not reach a terminal state: {last_payload}")


def xlsx_cell_text(path: Path) -> str:
    namespace = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values: list[str] = []
    with zipfile.ZipFile(path) as workbook:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            for item in root.findall("a:si", namespace):
                shared.append(
                    "".join(node.text or "" for node in item.findall(".//a:t", namespace))
                )
        for name in sorted(workbook.namelist()):
            if not name.startswith("xl/worksheets/sheet") or not name.endswith(".xml"):
                continue
            root = ElementTree.fromstring(workbook.read(name))
            for cell in root.findall(".//a:c", namespace):
                cell_type = cell.attrib.get("t")
                value_node = cell.find("a:v", namespace)
                inline_nodes = cell.findall(".//a:is/a:t", namespace)
                if inline_nodes:
                    values.append("".join(node.text or "" for node in inline_nodes))
                elif value_node is not None and value_node.text is not None:
                    if cell_type == "s":
                        values.append(shared[int(value_node.text)])
                    else:
                        values.append(value_node.text)
    return "\n".join(values)


def safe_error_details(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:1000]
    return json.dumps(payload, ensure_ascii=False)[:2000]


def run() -> dict[str, Any]:
    args = parse_args()
    image = args.image.expanduser().resolve(strict=True)
    if not image.is_file():
        raise ValueError("Invoice image must be a file")
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(image.suffix.lower())
    if media_type is None:
        raise ValueError("Invoice input must be JPEG or PNG")

    settings, run_dir, password = build_settings(
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": password},
        )
        login.raise_for_status()

        binding = client.put(
            "/api/v1/runtime/bindings/codex-local-default",
            json={
                "provider": "codex",
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "security_mode": "demo_full_access",
            },
        )
        binding.raise_for_status()

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
            headers={"Idempotency-Key": f"invoice-{uuid4().hex}"},
            json={
                "request": task_request(args.exchange_rate),
                "orchestration_mode": "planned",
            },
        )
        if submitted.status_code >= 400:
            raise RuntimeError(
                f"Task submission failed: {safe_error_details(submitted)}"
            )
        task_id = submitted.json()["task_id"]
        planned = wait_for_plan(
            client,
            task_id,
            timeout_seconds=args.timeout_seconds,
        )
        initial_contracts = planned["execution_plan"]["initial_input_contracts"]
        if len(initial_contracts) != 1:
            raise RuntimeError(
                "Invoice plan must declare exactly one initial input contract: "
                f"{initial_contracts}"
            )

        uploaded = client.post(
            f"/api/v1/tasks/{task_id}/inputs",
            json={
                "contract_key": initial_contracts[0],
                "schema_version": "1.0",
                "media_type": media_type,
                "file_name": image.name,
                "content_base64": base64.b64encode(image.read_bytes()).decode("ascii"),
                "source_delivery_id": f"invoice-source:{uuid4().hex}",
            },
        )
        if uploaded.status_code >= 400:
            raise RuntimeError(
                f"Invoice upload failed: {safe_error_details(uploaded)}"
            )

        started = client.post(f"/api/v1/tasks/{task_id}/start")
        if started.status_code >= 400:
            raise RuntimeError(f"Task start failed: {safe_error_details(started)}")
        task_payload = wait_for_terminal(
            client,
            task_id,
            timeout_seconds=args.timeout_seconds,
        )
        controls = client.get("/api/v1/runtime/controls")
        controls.raise_for_status()
        events = client.get(f"/api/v1/tasks/{task_id}/events")
        events.raise_for_status()

    plan_steps = task_payload["execution_plan"]["steps"]
    final_specialist_step = plan_steps[-2]
    final_candidates = [
        artifact
        for artifact in task_payload["artifacts"]
        if artifact["producer_plan_step_id"] == final_specialist_step["plan_step_id"]
        and artifact["media_type"] == XLSX_MEDIA_TYPE
        and artifact["status"] == "released"
    ]
    final_artifact = final_candidates[-1] if final_candidates else None
    workbook_checks: dict[str, bool] = {}
    final_path: str | None = None
    if final_artifact is not None:
        manager = WorkspaceManager(settings.runtime_workspace_root)
        path = manager.canonicalize(
            final_artifact["storage_relative_path"],
            must_exist=True,
        )
        final_path = str(path)
        cell_text = xlsx_cell_text(path)
        workbook_checks = {
            "exchange_rate_7_20_present": any(
                marker in cell_text for marker in ("7.20", "7.2")
            ),
            "net_usd_12_38_present": "12.38" in cell_text,
            "tax_usd_0_37_present": "0.37" in cell_text,
            "total_usd_12_76_present": "12.76" in cell_text,
        }

    event_types = [
        line.removeprefix("event: ")
        for line in events.text.splitlines()
        if line.startswith("event: ")
    ]
    assignments = [
        {
            "assignment_key": item["assignment_key"],
            "assignment_kind": item["assignment_kind"],
            "role": item["agent_role_key"],
            "status": item["status"],
            "requested_model": item["runtime_execution"]["requested_model"],
            "actual_model": item["runtime_execution"]["actual_model"],
            "reasoning_effort": item["runtime_execution"]["reasoning_effort"],
            "security_mode": item["runtime_execution"]["security_mode"],
            "workspace_id": item["runtime_execution"]["workspace_id"],
            "thread_id_present": bool(item["runtime_execution"]["thread_id"]),
            "turn_id_present": bool(item["runtime_execution"]["turn_id"]),
            "total_tokens": item["runtime_execution"]["total_tokens"],
        }
        for item in task_payload["assignments"]
    ]
    return {
        "task_id": task_payload["task_id"],
        "organization_id": proposal_payload["organization_id"],
        "task_status": task_payload["status"],
        "result_summary": task_payload["result_summary"],
        "plan_roles": [step["role_key"] for step in plan_steps],
        "plan_step_statuses": [step["status"] for step in plan_steps],
        "input_binding_counts": [
            len(step["input_bindings"]) for step in plan_steps
        ],
        "assignments": assignments,
        "artifact_contracts": [
            {
                "contract_key": item["contract_key"],
                "media_type": item["media_type"],
                "status": item["status"],
                "sha256": item["sha256"],
                "byte_size": item["byte_size"],
            }
            for item in task_payload["artifacts"]
        ],
        "final_artifact_path": final_path,
        "workbook_checks": workbook_checks,
        "provider_capacity_status": controls.json()["provider_capacity_status"],
        "tokens_consumed": controls.json()["tokens_consumed"],
        "event_types": event_types,
        "isolated_control_dir": str(run_dir),
    }


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["task_status"] != "completed":
        return 1
    if not result["final_artifact_path"]:
        return 1
    if not all(result["workbook_checks"].values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
