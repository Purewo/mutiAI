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
    RuntimeExecution,
    Task,
    User,
    Workspace,
)
from mutiai.models.organization import OrganizationVersionStatus
from mutiai.models.task import (
    AssignmentKind,
    AssignmentStatus,
    RuntimeExecutionStatus,
    TaskOrchestrationMode,
    TaskStatus,
)
from mutiai.models.task_plan import ArtifactStatus, PlanStepStatus
from mutiai.runtime import RuntimeCapacity, RuntimeResult
from mutiai.services.task_plans import create_task_execution_plan


class StructuredParallelRuntime:
    provider = "fake"

    def __init__(self, *, wait_for_specialists: bool = False) -> None:
        self.wait_for_specialists = wait_for_specialists
        self.calls: list[tuple[str, str]] = []

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
        self.calls.append((role_key, instructions))
        if role_key in {"worker_a", "worker_b"} and self.wait_for_specialists:
            return RuntimeResult(
                status="waiting",
                runtime_job_id=f"fake:{execution_id}",
                thread_id=f"thread:{execution_id}",
                turn_id=f"turn:{execution_id}",
                workspace_id=workspace_id,
            )
        if role_key in {"worker_a", "worker_b"}:
            return self._complete_specialist(
                role_key=role_key,
                execution_id=execution_id,
                workspace_id=workspace_id,
                workspace_path=Path(workspace_path),
            )

        workspace = Path(workspace_path)
        inputs = sorted(path.name for path in workspace.rglob("*.json"))
        assert inputs == ["a.json", "b.json"]
        assert "worker.a.v1" in instructions
        assert "worker.b.v1" in instructions
        packet = json.loads(instructions.split("Assignment packet:\n", 1)[1])
        evidence = packet["execution_evidence"]
        assert evidence["source"] == "product_database"
        assert evidence["checks"] == {
            "planning_assignment_completed": True,
            "predecessor_steps_completed": True,
            "dependency_order_satisfied": True,
            "input_bindings_match_declared_contracts": True,
            "outputs_match_declared_contracts": True,
            "artifacts_released_and_validated": True,
        }
        assert {step["role_key"] for step in evidence["specialist_steps"]} == {
            "worker_a",
            "worker_b",
        }
        assert "storage_relative_path" not in instructions
        assert "canonical_path" not in instructions
        return RuntimeResult(
            status="completed",
            runtime_job_id=f"fake:{execution_id}",
            workspace_id=workspace_id,
            summary=json.dumps(
                {
                    "decision": "accepted",
                    "final_summary": "The lead accepted both parallel Artifacts.",
                    "issues": [],
                }
            ),
        )

    @staticmethod
    def _complete_specialist(
        *,
        role_key: str,
        execution_id: str,
        workspace_id: str | None,
        workspace_path: Path,
    ) -> RuntimeResult:
        suffix = role_key.removeprefix("worker_")
        output = workspace_path / "outputs" / f"{suffix}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"worker": suffix}), encoding="utf-8")
        return RuntimeResult(
            status="completed",
            runtime_job_id=f"fake:{execution_id}",
            workspace_id=workspace_id,
            summary=delivery(suffix),
        )

    def recover(self, request) -> bool:
        del request
        return False

    def cancel(self, execution_id: str) -> bool:
        del execution_id
        return False


class InvalidFirstParallelRuntime(StructuredParallelRuntime):
    """Return one invalid delivery while keeping the sibling valid."""

    def __init__(self) -> None:
        super().__init__()
        self._invalid_emitted = False

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
        if role_key == "worker_a" and not self._invalid_emitted:
            self._invalid_emitted = True
            del instructions, thread_id, output_schema, runtime_config
            assert workspace_path is not None
            self.calls.append((role_key, "invalid delivery"))
            return RuntimeResult(
                status="completed",
                runtime_job_id=f"fake:{execution_id}",
                workspace_id=workspace_id,
                summary="worker_a completed without a structured delivery.",
            )
        return super().execute(
            execution_id=execution_id,
            role_key=role_key,
            instructions=instructions,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            thread_id=thread_id,
            output_schema=output_schema,
            runtime_config=runtime_config,
        )


def delivery(suffix: str) -> str:
    return json.dumps(
        {
            "status": "completed",
            "summary": f"Worker {suffix} published its bounded Artifact.",
            "artifacts": [
                {
                    "contract_key": f"worker.{suffix}.v1",
                    "relative_path": f"outputs/{suffix}.json",
                    "media_type": "application/json",
                }
            ],
        }
    )


def parallel_environment(tmp_path) -> tuple[Database, Settings, str, str]:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'parallel.db'}",
        runtime_workspace_root=tmp_path / "managed",
        langgraph_checkpoint_path=tmp_path / "parallel-checkpoints.db",
        runtime_provider="fake",
        bootstrap_admin_enabled=False,
    )
    upgrade_database(settings.database_url)
    database = Database(settings)
    with database.session() as session:
        user = User(
            username="parallel-owner",
            password_hash="test-only",
            display_name="Parallel Owner",
        )
        session.add(user)
        session.flush()
        organization = Organization(
            owner_user_id=user.user_id,
            name="Parallel Organization",
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
                "name": "Parallel Organization",
                "description": "",
                "roles": [
                    {
                        "role_key": "lead",
                        "name": "Lead",
                        "responsibility": "Review both released Artifacts.",
                        "is_lead": True,
                        "reports_to": None,
                        "runtime_binding_key": "codex-local-default",
                    },
                    {
                        "role_key": "worker_a",
                        "name": "Worker A",
                        "responsibility": "Produce Artifact A only.",
                        "is_lead": False,
                        "reports_to": "lead",
                        "runtime_binding_key": "codex-local-default",
                    },
                    {
                        "role_key": "worker_b",
                        "name": "Worker B",
                        "responsibility": "Produce Artifact B only.",
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
            request_text="Produce two independent Artifacts and review both.",
            request_hash="d" * 64,
            idempotency_key="parallel-test",
            orchestration_mode=TaskOrchestrationMode.PLANNED,
            status=TaskStatus.CREATED,
        )
        session.add(task)
        session.commit()
        plan, _ = create_task_execution_plan(
            session,
            task=task,
            plan_spec=TaskExecutionPlanSpec(
                summary="Two parallel workers followed by lead review.",
                steps=(
                    PlanStepSpec(
                        step_key="produce_a",
                        role_key="worker_a",
                        objective="Produce Artifact A.",
                        acceptance_criteria="Return valid JSON A.",
                        output_contracts=(
                            ArtifactContractSpec(
                                contract_key="worker.a.v1",
                                media_type="application/json",
                                file_name="a.json",
                            ),
                        ),
                    ),
                    PlanStepSpec(
                        step_key="produce_b",
                        role_key="worker_b",
                        objective="Produce Artifact B.",
                        acceptance_criteria="Return valid JSON B.",
                        output_contracts=(
                            ArtifactContractSpec(
                                contract_key="worker.b.v1",
                                media_type="application/json",
                                file_name="b.json",
                            ),
                        ),
                    ),
                    PlanStepSpec(
                        step_key="review",
                        role_key="lead",
                        step_kind="lead_review",
                        objective="Review both released Artifacts.",
                        acceptance_criteria="Accept only both valid Artifacts.",
                        depends_on=("produce_a", "produce_b"),
                        input_contracts=("worker.a.v1", "worker.b.v1"),
                    ),
                ),
            ),
            source="test",
        )
        planning_assignment = Assignment(
            assignment_id=f"planning-{task.task_id}",
            task_id=task.task_id,
            assignment_key="lead.plan",
            assignment_kind=AssignmentKind.LEAD_PLAN,
            agent_role_key="lead",
            instructions="Test planning assignment.",
            acceptance_criteria="Return the persisted test plan.",
            execution_id=f"planning-execution-{task.task_id}",
            status=AssignmentStatus.COMPLETED,
            result_summary="Persisted test plan.",
        )
        planning_execution = RuntimeExecution(
            runtime_execution_id=f"planning-runtime-{task.task_id}",
            execution_id=planning_assignment.execution_id,
            assignment_id=planning_assignment.assignment_id,
            provider="fake",
            status=RuntimeExecutionStatus.COMPLETED,
            result_summary="Persisted test plan.",
        )
        session.add_all([planning_assignment, planning_execution])
        session.commit()
        return database, settings, task.task_id, plan.plan_id


def test_parallel_graph_publishes_both_artifacts_before_lead_review(tmp_path) -> None:
    database, settings, task_id, plan_id = parallel_environment(tmp_path)
    runtime = StructuredParallelRuntime()
    app = create_app(settings, runtime_adapter=runtime)
    try:
        result = app.state.task_orchestrator.run(task_id)
        assert result.status == TaskStatus.COMPLETED
        assert [role for role, _ in runtime.calls] == [
            "worker_a",
            "worker_b",
            "lead",
        ]
        with app.state.database.session() as session:
            steps = session.scalars(
                select(PlanStep)
                .where(PlanStep.plan_id == plan_id)
                .order_by(PlanStep.sequence)
            ).all()
            assert all(step.status == PlanStepStatus.COMPLETED for step in steps)
            artifacts = session.scalars(
                select(Artifact)
                .where(Artifact.task_id == task_id)
                .order_by(Artifact.contract_key)
            ).all()
            assert [artifact.contract_key for artifact in artifacts] == [
                "worker.a.v1",
                "worker.b.v1",
            ]
            assert all(
                artifact.status == ArtifactStatus.RELEASED for artifact in artifacts
            )
            bindings = session.scalars(
                select(ArtifactInputBinding).where(
                    ArtifactInputBinding.plan_step_id == steps[-1].plan_step_id
                )
            ).all()
            assert {binding.artifact_id for binding in bindings} == {
                artifact.artifact_id for artifact in artifacts
            }
            lead_assignment = session.scalar(
                select(Assignment).where(
                    Assignment.plan_step_id == steps[-1].plan_step_id
                )
            )
            assert lead_assignment is not None
            lead_workspace = session.get(
                Workspace,
                lead_assignment.runtime_execution.workspace_id,
            )
            assert lead_workspace is not None
            assert sorted(
                path.name for path in Path(lead_workspace.canonical_path).rglob("*.json")
            ) == ["a.json", "b.json"]
    finally:
        app.state.database.dispose()
        database.dispose()


def test_parallel_graph_converges_sibling_states_after_invalid_delivery(tmp_path) -> None:
    database, settings, task_id, plan_id = parallel_environment(tmp_path)
    runtime = InvalidFirstParallelRuntime()
    app = create_app(settings, runtime_adapter=runtime)
    try:
        result = app.state.task_orchestrator.run(task_id)
        assert result.status == TaskStatus.FAILED

        with app.state.database.session() as session:
            steps = session.scalars(
                select(PlanStep)
                .where(PlanStep.plan_id == plan_id)
                .order_by(PlanStep.sequence)
            ).all()
            assert [step.status for step in steps] == [
                PlanStepStatus.FAILED,
                PlanStepStatus.COMPLETED,
                PlanStepStatus.CANCELLED,
            ]
            artifacts = session.scalars(
                select(Artifact)
                .where(Artifact.task_id == task_id)
            ).all()
            assert [artifact.contract_key for artifact in artifacts] == [
                "worker.b.v1"
            ]
            assignments = session.scalars(
                select(Assignment)
                .where(Assignment.task_id == task_id)
                .where(Assignment.plan_step_id.is_not(None))
                .order_by(Assignment.agent_role_key)
            ).all()
            by_role = {assignment.agent_role_key: assignment for assignment in assignments}
            assert by_role["worker_a"].status == "failed"
            assert "Product delivery validation failed" in (
                by_role["worker_a"].result_summary or ""
            )
            assert by_role["worker_a"].runtime_execution.status == "completed"
            assert "Product delivery validation failed" in (
                by_role["worker_a"].runtime_execution.result_summary or ""
            )
            assert "lead" not in by_role
            event_types = session.scalars(
                select(ProductEvent.event_type)
                .where(ProductEvent.task_id == task_id)
                .order_by(ProductEvent.sequence)
            ).all()
            assert "plan.step_cancelled" in event_types

        recovered = app.state.task_orchestrator.retry(task_id)
        assert recovered.status == TaskStatus.COMPLETED
        with app.state.database.session() as session:
            steps = session.scalars(
                select(PlanStep)
                .where(PlanStep.plan_id == plan_id)
                .order_by(PlanStep.sequence)
            ).all()
            assert all(step.status == PlanStepStatus.COMPLETED for step in steps)
            assert session.scalar(
                select(Artifact).where(
                    Artifact.task_id == task_id,
                    Artifact.contract_key == "worker.a.v1",
                )
            ) is not None
    finally:
        app.state.database.dispose()
        database.dispose()


def test_parallel_graph_waits_for_every_artifact_before_lead_review(tmp_path) -> None:
    database, settings, task_id, _ = parallel_environment(tmp_path)
    runtime = StructuredParallelRuntime(wait_for_specialists=True)
    app = create_app(settings, runtime_adapter=runtime)
    try:
        waiting = app.state.task_orchestrator.run(task_id)
        assert waiting.status == TaskStatus.WAITING
        assert [role for role, _ in runtime.calls] == ["worker_a", "worker_b"]

        with app.state.database.session() as session:
            assignments = session.scalars(
                select(Assignment)
                .where(
                    Assignment.task_id == task_id,
                    Assignment.agent_role_key.in_(("worker_a", "worker_b")),
                )
                .order_by(Assignment.agent_role_key)
            ).all()
            execution_by_role = {
                assignment.agent_role_key: assignment.runtime_execution.execution_id
                for assignment in assignments
            }
            workspace_by_role = {
                assignment.agent_role_key: session.get(
                    Workspace,
                    assignment.runtime_execution.workspace_id,
                ).canonical_path
                for assignment in assignments
            }

        for role_key in ("worker_a", "worker_b"):
            suffix = role_key.removeprefix("worker_")
            output = Path(workspace_by_role[role_key]) / "outputs" / f"{suffix}.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({"worker": suffix}), encoding="utf-8")

        still_waiting = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=execution_by_role["worker_a"],
            runtime_event_id="parallel:event:a",
            summary=delivery("a"),
        )
        assert still_waiting.status == TaskStatus.WAITING
        with app.state.database.session() as session:
            assert session.scalar(
                    select(Assignment).where(
                        Assignment.task_id == task_id,
                        Assignment.agent_role_key == "lead",
                        Assignment.plan_step_id.is_not(None),
                    )
            ) is None
            assert session.scalar(
                select(Artifact).where(Artifact.task_id == task_id)
            ) is None

        completed = app.state.task_orchestrator.complete_runtime_execution(
            execution_id=execution_by_role["worker_b"],
            runtime_event_id="parallel:event:b",
            summary=delivery("b"),
        )
        assert completed.status == TaskStatus.COMPLETED
        assert [role for role, _ in runtime.calls] == [
            "worker_a",
            "worker_b",
            "lead",
        ]
        with app.state.database.session() as session:
            event_types = session.scalars(
                select(ProductEvent.event_type)
                .where(ProductEvent.task_id == task_id)
                .order_by(ProductEvent.sequence)
            ).all()
            lead_created_index = max(
                index
                for index, event_type in enumerate(event_types)
                if event_type == "assignment.created"
            )
            artifact_indices = [
                index
                for index, event_type in enumerate(event_types)
                if event_type == "artifact.released"
            ]
            assert len(artifact_indices) == 2
            assert max(artifact_indices) < lead_created_index
    finally:
        app.state.database.dispose()
        database.dispose()
