import json
from pathlib import Path

from sqlalchemy import select

from mutiai.config import Settings
from mutiai.db import Database
from mutiai.domain import ArtifactContractSpec, PlanStepSpec, TaskExecutionPlanSpec
from mutiai.main import create_app
from mutiai.migrations import upgrade_database
from mutiai.models import (
    Artifact,
    ArtifactInputBinding,
    Assignment,
    Organization,
    OrganizationSpecVersion,
    PlanStep,
    ProductEvent,
    Task,
    User,
    Workspace,
)
from mutiai.models.organization import OrganizationVersionStatus
from mutiai.models.task import TaskStatus
from mutiai.models.task_plan import ArtifactStatus, PlanStepStatus
from mutiai.runtime import RuntimeCapacity, RuntimeResult
from mutiai.services.task_plans import create_task_execution_plan


class StructuredLinearRuntime:
    provider = "fake"

    def __init__(self, *, wait_for_worker: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.wait_for_worker = wait_for_worker

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
        del thread_id, output_schema, runtime_config
        assert workspace_path is not None
        self.calls.append((role_key, execution_id, instructions))
        workspace = Path(workspace_path)
        if role_key == "worker":
            if self.wait_for_worker:
                return RuntimeResult(
                    status="waiting",
                    runtime_job_id=f"fake:{execution_id}",
                    thread_id=f"thread:{execution_id}",
                    turn_id=f"turn:{execution_id}",
                    workspace_id=workspace_id,
                )
            output = workspace / "outputs" / "result.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps({"value": "bounded worker output"}),
                encoding="utf-8",
            )
            summary = json.dumps(
                {
                    "status": "completed",
                    "summary": "Worker published one bounded JSON Artifact.",
                    "artifacts": [
                        {
                            "contract_key": "worker.result.v1",
                            "relative_path": "outputs/result.json",
                            "media_type": "application/json",
                        }
                    ],
                }
            )
        else:
            assert (workspace / "inputs").exists()
            summary = json.dumps(
                {
                    "decision": "accepted",
                    "final_summary": "The lead accepted the worker Artifact.",
                    "issues": [],
                }
            )
        return RuntimeResult(
            status="completed",
            runtime_job_id=f"fake:{execution_id}",
            summary=summary,
            workspace_id=workspace_id,
        )

    def recover(self, request) -> bool:
        del request
        return False

    def cancel(self, execution_id: str) -> bool:
        del execution_id
        return False


def linear_environment(tmp_path) -> tuple[Database, Settings, str, str]:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'linear.db'}",
        runtime_workspace_root=tmp_path / "managed",
        langgraph_checkpoint_path=tmp_path / "linear-checkpoints.db",
        runtime_provider="fake",
        bootstrap_admin_enabled=False,
    )
    upgrade_database(settings.database_url)
    database = Database(settings)
    with database.session() as session:
        user = User(
            username="linear-owner",
            password_hash="test-only",
            display_name="Linear Owner",
        )
        session.add(user)
        session.flush()
        organization = Organization(
            owner_user_id=user.user_id,
            name="Linear Organization",
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
                "name": "Linear Organization",
                "description": "",
                "roles": [
                    {
                        "role_key": "lead",
                        "name": "Lead",
                        "responsibility": "Review the worker delivery.",
                        "is_lead": True,
                        "reports_to": None,
                        "runtime_binding_key": "codex-local-default",
                    },
                    {
                        "role_key": "worker",
                        "name": "Worker",
                        "responsibility": "Produce the bounded JSON output.",
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
        task = Task(
            owner_user_id=user.user_id,
            organization_id=organization.organization_id,
            organization_spec_version_id=version.spec_version_id,
            request_text="Create a bounded worker output.",
            request_hash="c" * 64,
            idempotency_key="linear-test",
            status=TaskStatus.CREATED,
        )
        session.add(task)
        session.commit()
        plan, _ = create_task_execution_plan(
            session,
            task=task,
            plan_spec=TaskExecutionPlanSpec(
                summary="Worker then lead review.",
                steps=(
                    PlanStepSpec(
                        step_key="produce",
                        role_key="worker",
                        objective="Produce the JSON Artifact.",
                        acceptance_criteria="Return a valid JSON Artifact.",
                        output_contracts=(
                            ArtifactContractSpec(
                                contract_key="worker.result.v1",
                                media_type="application/json",
                                file_name="result.json",
                            ),
                        ),
                    ),
                    PlanStepSpec(
                        step_key="review",
                        role_key="lead",
                        step_kind="lead_review",
                        objective="Review the JSON Artifact.",
                        acceptance_criteria="Accept only the released Artifact.",
                        depends_on=("produce",),
                        input_contracts=("worker.result.v1",),
                    ),
                ),
            ),
            source="test",
        )
        return database, settings, task.task_id, plan.plan_id


def test_linear_graph_releases_artifact_before_lead_review(tmp_path) -> None:
    database, settings, task_id, plan_id = linear_environment(tmp_path)
    runtime = StructuredLinearRuntime()
    app = create_app(settings, runtime_adapter=runtime)
    try:
        result = app.state.task_orchestrator.run(task_id)
        assert result.status == TaskStatus.COMPLETED
        assert [call[0] for call in runtime.calls] == ["worker", "lead"]
        assert "Create a bounded worker output." not in runtime.calls[0][2]
        assert "Create a bounded worker output." in runtime.calls[1][2]

        with app.state.database.session() as session:
            task = session.get(Task, task_id)
            assert task is not None and task.status == TaskStatus.COMPLETED
            steps = session.scalars(
                select(PlanStep)
                .where(PlanStep.plan_id == plan_id)
                .order_by(PlanStep.sequence)
            ).all()
            assert [step.status for step in steps] == [
                PlanStepStatus.COMPLETED,
                PlanStepStatus.COMPLETED,
            ]
            artifact = session.scalar(
                select(Artifact).where(Artifact.contract_key == "worker.result.v1")
            )
            assert artifact is not None and artifact.status == ArtifactStatus.RELEASED
            binding = session.scalar(select(ArtifactInputBinding))
            assert binding is not None and binding.artifact_id == artifact.artifact_id
            assignments = session.scalars(
                select(Assignment).where(Assignment.task_id == task_id)
            ).all()
            assert [assignment.agent_role_key for assignment in assignments] == [
                "worker",
                "lead",
            ]
            event_types = session.scalars(
                select(ProductEvent.event_type)
                .where(ProductEvent.task_id == task_id)
                .order_by(ProductEvent.sequence)
            ).all()
            assert "artifact.released" in event_types
            assert "artifact.input_materialized" in event_types
            assert event_types.index("artifact.released") < event_types.index(
                "assignment.created",
                event_types.index("artifact.released") + 1,
            )
    finally:
        app.state.database.dispose()
        database.dispose()


def test_linear_graph_checkpoints_wait_and_resumes_from_external_completion(
    tmp_path,
) -> None:
    database, settings, task_id, _ = linear_environment(tmp_path)
    runtime = StructuredLinearRuntime(wait_for_worker=True)
    app = create_app(settings, runtime_adapter=runtime)
    try:
        waiting = app.state.task_orchestrator.run(task_id)
        assert waiting.status == TaskStatus.WAITING
        assert [call[0] for call in runtime.calls] == ["worker"]

        with app.state.database.session() as session:
            assignment = session.scalar(
                select(Assignment).where(
                    Assignment.task_id == task_id,
                    Assignment.agent_role_key == "worker",
                )
            )
            assert assignment is not None
            execution = assignment.runtime_execution
            assert execution is not None and execution.workspace_id is not None
            workspace = session.get(Workspace, execution.workspace_id)
            assert workspace is not None
            output = Path(workspace.canonical_path) / "outputs" / "result.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"value": "external result"}), encoding="utf-8")
            execution_id = execution.execution_id

        delivery = json.dumps(
            {
                "status": "completed",
                "summary": "Worker completed outside the graph process.",
                "artifacts": [
                    {
                        "contract_key": "worker.result.v1",
                        "relative_path": "outputs/result.json",
                        "media_type": "application/json",
                    }
                ],
            }
        )
        completed = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=execution_id,
            runtime_event_id="fake:event:worker-completed",
            summary=delivery,
        )
        assert completed.status == TaskStatus.COMPLETED
        assert [call[0] for call in runtime.calls] == ["worker", "lead"]

        with app.state.database.session() as session:
            artifact = session.scalar(
                select(Artifact).where(Artifact.contract_key == "worker.result.v1")
            )
            assert artifact is not None
            assert artifact.source_delivery_id == "fake:event:worker-completed"
            assert session.scalar(
                select(ProductEvent).where(
                    ProductEvent.task_id == task_id,
                    ProductEvent.event_type == "runtime.execution_waiting",
                )
            ) is not None
    finally:
        app.state.database.dispose()
        database.dispose()
