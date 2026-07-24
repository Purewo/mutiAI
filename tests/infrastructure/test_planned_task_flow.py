import json
from pathlib import Path

import pytest
from sqlalchemy import select

from mutiai.config import Settings
from mutiai.db import Database
from mutiai.main import create_app
from mutiai.migrations import upgrade_database
from mutiai.models import (
    Assignment,
    Organization,
    OrganizationSpecVersion,
    Task,
    TaskExecutionPlan,
    User,
)
from mutiai.models.organization import OrganizationVersionStatus
from mutiai.models.task import AssignmentKind, TaskOrchestrationMode, TaskStatus
from mutiai.runtime import RuntimeCapacity, RuntimeResult
from mutiai.services.tasks import create_task


class PlannedRuntime:
    provider = "fake"

    def __init__(
        self,
        *,
        wait_for_planning: bool = False,
        invalid_plan_once: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.wait_for_planning = wait_for_planning
        self.invalid_plan_once = invalid_plan_once

    def capacity(self) -> RuntimeCapacity:
        return RuntimeCapacity(status="available")

    def execute(
        self,
        *,
        execution_id: str,
        role_key: str,
        instructions: str,
        workspace_id: str | None = None,
        workspace_path: str | None = None,
        thread_id: str | None = None,
        output_schema: dict | None = None,
        runtime_config=None,
    ) -> RuntimeResult:
        del execution_id, thread_id, runtime_config
        self.calls.append(role_key)
        properties = output_schema.get("properties", {}) if output_schema else {}
        if "steps" in properties:
            if self.wait_for_planning and self.calls.count("lead") == 1:
                return RuntimeResult(
                    status="waiting",
                    runtime_job_id="planned:plan",
                    thread_id="thread:plan",
                    turn_id="turn:plan",
                    workspace_id=workspace_id,
                )
            if self.invalid_plan_once and self.calls.count("lead") == 1:
                return RuntimeResult(
                    status="completed",
                    runtime_job_id="planned:invalid-plan",
                    summary=json.dumps({"summary": "Missing plan steps."}),
                )
            return RuntimeResult(
                status="completed",
                runtime_job_id="planned:plan",
                summary=json.dumps(
                    {
                        "summary": "Extract the invoice and review the result.",
                        "initial_input_contracts": ["invoice.input.v1"],
                        "steps": [
                            {
                                "step_key": "extract",
                                "role_key": "extractor",
                                "step_kind": "specialist",
                                "objective": "Extract invoice fields.",
                                "acceptance_criteria": "Return valid extracted JSON.",
                                "input_contracts": ["invoice.input.v1"],
                                "output_contracts": [
                                    {
                                        "contract_key": "invoice.extracted.v1",
                                        "media_type": "application/json",
                                        "file_name": "invoice.json",
                                    }
                                ],
                            },
                            {
                                "step_key": "review",
                                "role_key": "lead",
                                "step_kind": "lead_review",
                                "objective": "Review the extracted invoice.",
                                "acceptance_criteria": "Accept the released extraction.",
                                "depends_on": ["extract"],
                                "input_contracts": ["invoice.extracted.v1"],
                            },
                        ],
                    }
                ),
            )
        if "decision" in properties:
            return RuntimeResult(
                status="completed",
                runtime_job_id="planned:review",
                summary=json.dumps(
                    {
                        "decision": "accepted",
                        "final_summary": "The invoice extraction was accepted.",
                        "issues": [],
                    }
                ),
                workspace_id=workspace_id,
            )
        assert workspace_path is not None
        output = Path(workspace_path) / "outputs" / "invoice.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"total": 100}), encoding="utf-8")
        return RuntimeResult(
            status="completed",
            runtime_job_id="planned:extract",
            summary=json.dumps(
                {
                    "status": "completed",
                    "summary": "Invoice fields extracted.",
                    "artifacts": [
                        {
                            "contract_key": "invoice.extracted.v1",
                            "relative_path": "outputs/invoice.json",
                            "media_type": "application/json",
                        }
                    ],
                }
            ),
            workspace_id=workspace_id,
        )

    def recover(self, request) -> bool:
        del request
        return False

    def cancel(self, execution_id: str) -> bool:
        del execution_id
        return False


def planned_environment(tmp_path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'planned.db'}",
        runtime_workspace_root=tmp_path / "managed",
        langgraph_checkpoint_path=tmp_path / "planned-checkpoints.db",
        runtime_provider="fake",
        bootstrap_admin_enabled=False,
    )
    upgrade_database(settings.database_url)
    database = Database(settings)
    with database.session() as session:
        user = User(username="planned-owner", password_hash="test-only", display_name="Planned Owner")
        session.add(user)
        session.flush()
        organization = Organization(
            owner_user_id=user.user_id,
            name="Invoice Organization",
            description="",
        )
        session.add(organization)
        session.flush()
        version = OrganizationSpecVersion(
            organization_id=organization.organization_id,
            owner_user_id=user.user_id,
            version_number=1,
            status=OrganizationVersionStatus.PUBLISHED,
            spec_payload={
                "schema_version": "1.0",
                "name": "Invoice Organization",
                "description": "",
                "roles": [
                    {
                        "role_key": "lead",
                        "name": "Lead",
                        "responsibility": "Plan and review invoice work.",
                        "is_lead": True,
                        "reports_to": None,
                        "runtime_binding_key": "codex-local-default",
                    },
                    {
                        "role_key": "extractor",
                        "name": "Extractor",
                        "responsibility": "Extract invoice fields.",
                        "is_lead": False,
                        "reports_to": "lead",
                        "runtime_binding_key": "codex-local-default",
                    },
                ],
            },
        )
        session.add(version)
        session.flush()
        organization.current_published_version_id = version.spec_version_id
        session.commit()
        task, _ = create_task(
            session,
            owner_user_id=user.user_id,
            organization_id=organization.organization_id,
            request_text="Extract the invoice and review it.",
            idempotency_key="planned-1",
            orchestration_mode=TaskOrchestrationMode.PLANNED,
        )
        return database, settings, task.task_id


def test_planned_task_plans_uploads_input_and_runs_linear_flow(tmp_path) -> None:
    database, settings, task_id = planned_environment(tmp_path)
    runtime = PlannedRuntime()
    app = create_app(settings, runtime_adapter=runtime)
    try:
        planned = app.state.task_orchestrator.plan(task_id)
        assert planned.status == TaskStatus.CREATED
        replayed_plan = app.state.task_orchestrator.plan(task_id)
        assert replayed_plan.status == TaskStatus.CREATED
        assert runtime.calls == ["lead"]
        with app.state.database.session() as session:
            assignments = session.scalars(
                select(Assignment).where(Assignment.task_id == task_id)
            ).all()
            assert len(assignments) == 1
            assert assignments[0].assignment_kind == AssignmentKind.LEAD_PLAN
        with pytest.raises(ValueError, match="invoice.input.v1"):
            app.state.task_orchestrator.start(task_id)

        artifact = app.state.task_orchestrator.publish_task_input(
            task_id=task_id,
            contract_key="invoice.input.v1",
            schema_version="1.0",
            media_type="application/json",
            file_name="invoice.json",
            content=b'{"total": 100}',
            source_delivery_id="input:invoice:1",
        )
        assert artifact.origin == "task_input"
        completed = app.state.task_orchestrator.start(task_id)
        assert completed.status == TaskStatus.COMPLETED
        assert runtime.calls == ["lead", "extractor", "lead"]
        with app.state.database.session() as session:
            lead_assignments = session.scalars(
                select(Assignment).where(
                    Assignment.task_id == task_id,
                    Assignment.agent_role_key == "lead",
                )
            ).all()
            assert {item.assignment_key for item in lead_assignments} == {
                "lead.plan",
                "plan_step:review",
            }
    finally:
        app.state.database.dispose()
        database.dispose()


def test_planned_lead_plan_waits_and_resumes_from_runtime_event(tmp_path) -> None:
    database, settings, task_id = planned_environment(tmp_path)
    runtime = PlannedRuntime(wait_for_planning=True)
    app = create_app(settings, runtime_adapter=runtime)
    try:
        waiting = app.state.task_orchestrator.plan(task_id)
        assert waiting.status == TaskStatus.WAITING
        with app.state.database.session() as session:
            assignment = session.scalar(
                select(Assignment).where(Assignment.task_id == task_id)
            )
            assert assignment is not None
            assert assignment.assignment_kind == AssignmentKind.LEAD_PLAN
            execution = assignment.runtime_execution
            assert execution is not None
            execution_id = execution.execution_id
        plan_payload = json.dumps(
            {
                "summary": "Extract the invoice and review the result.",
                "initial_input_contracts": ["invoice.input.v1"],
                "steps": [
                    {
                        "step_key": "extract",
                        "role_key": "extractor",
                        "step_kind": "specialist",
                        "objective": "Extract invoice fields.",
                        "acceptance_criteria": "Return valid extracted JSON.",
                        "input_contracts": ["invoice.input.v1"],
                        "output_contracts": [
                            {
                                "contract_key": "invoice.extracted.v1",
                                "media_type": "application/json",
                                "file_name": "invoice.json",
                            }
                        ],
                    },
                    {
                        "step_key": "review",
                        "role_key": "lead",
                        "step_kind": "lead_review",
                        "objective": "Review the extracted invoice.",
                        "acceptance_criteria": "Accept the released extraction.",
                        "depends_on": ["extract"],
                        "input_contracts": ["invoice.extracted.v1"],
                    },
                ],
            }
        )
        resumed = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=execution_id,
            runtime_event_id="planned:event:1",
            summary=plan_payload,
        )
        assert resumed.status == TaskStatus.CREATED
        replayed = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=execution_id,
            runtime_event_id="planned:event:1",
            summary=plan_payload,
        )
        assert replayed.status == TaskStatus.CREATED
        with app.state.database.session() as session:
            plan = session.scalar(
                select(TaskExecutionPlan).where(
                    TaskExecutionPlan.task_id == task_id
                )
            )
            assert plan is not None
    finally:
        app.state.database.dispose()
        database.dispose()


def test_invalid_lead_plan_is_retryable_without_reusing_old_graph_output(
    tmp_path,
) -> None:
    database, settings, task_id = planned_environment(tmp_path)
    runtime = PlannedRuntime(invalid_plan_once=True)
    app = create_app(settings, runtime_adapter=runtime)
    try:
        with pytest.raises(RuntimeError, match="invalid execution plan"):
            app.state.task_orchestrator.plan(task_id)
        with app.state.database.session() as session:
            task = session.get(Task, task_id)
            assignment = session.scalar(
                select(Assignment).where(Assignment.task_id == task_id)
            )
            assert task is not None and task.status == TaskStatus.FAILED
            assert assignment is not None and assignment.status == "failed"

        recovered = app.state.task_orchestrator.retry(task_id)
        assert recovered.status == TaskStatus.CREATED
        assert runtime.calls == ["lead", "lead"]
    finally:
        app.state.database.dispose()
        database.dispose()
