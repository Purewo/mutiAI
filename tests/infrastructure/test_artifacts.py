import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import func, select

from mutiai.config import Settings
from mutiai.db import Database
from mutiai.domain import (
    ArtifactContractSpec,
    ArtifactDeclaration,
    AssignmentDelivery,
    PlanStepSpec,
    TaskExecutionPlanSpec,
)
from mutiai.migrations import upgrade_database
from mutiai.models import (
    Artifact,
    Assignment,
    Organization,
    OrganizationSpecVersion,
    PlanStep,
    ProductEvent,
    RuntimeExecution,
    Task,
    TaskExecutionPlan,
    User,
    Workspace,
)
from mutiai.models.organization import OrganizationVersionStatus
from mutiai.models.task import (
    AssignmentKind,
    AssignmentStatus,
    RuntimeExecutionStatus,
    TaskStatus,
)
from mutiai.models.task_plan import ArtifactStatus
from mutiai.runtime import WorkspaceManager
from mutiai.services.artifacts import XLSX_MEDIA_TYPE, ArtifactError, ArtifactManager
from mutiai.services.task_plans import create_task_execution_plan
from mutiai.services.workspaces import WorkspaceProvisioner


@dataclass(frozen=True, slots=True)
class ArtifactEnvironment:
    database: Database
    manager: WorkspaceManager
    task_id: str
    plan_id: str
    step_ids: dict[str, str]
    assignment_ids: dict[str, str]
    workspace_ids: dict[str, str]
    workspace_paths: dict[str, Path]


def artifact_environment(tmp_path) -> ArtifactEnvironment:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'artifacts.db'}",
        runtime_workspace_root=tmp_path / "managed",
        bootstrap_admin_enabled=False,
    )
    upgrade_database(settings.database_url)
    database = Database(settings)
    manager = WorkspaceManager(settings.runtime_workspace_root, protected_roots=())
    provisioner = WorkspaceProvisioner(manager)

    with database.session() as session:
        user = User(
            username="artifact-owner",
            password_hash="test-only",
            display_name="Artifact Owner",
        )
        session.add(user)
        session.flush()
        organization = Organization(
            owner_user_id=user.user_id,
            name="Artifact Organization",
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
                "name": "Artifact Organization",
                "description": "",
                "roles": [
                    {
                        "role_key": "lead",
                        "name": "Lead",
                        "responsibility": "Review the final Artifact.",
                        "is_lead": True,
                        "reports_to": None,
                        "runtime_binding_key": "codex-lead",
                    },
                    {
                        "role_key": "extractor",
                        "name": "Extractor",
                        "responsibility": "Extract structured invoice data.",
                        "is_lead": False,
                        "reports_to": "lead",
                        "runtime_binding_key": "codex-extractor",
                    },
                    {
                        "role_key": "excel",
                        "name": "Excel",
                        "responsibility": "Create the workbook.",
                        "is_lead": False,
                        "reports_to": "lead",
                        "runtime_binding_key": "codex-excel",
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
            request_text="Create a validated invoice workbook.",
            request_hash="b" * 64,
            idempotency_key="artifact-test",
            status=TaskStatus.CREATED,
        )
        session.add(task)
        session.commit()

        json_contract = ArtifactContractSpec(
            contract_key="invoice.extracted.v1",
            media_type="application/json",
            file_name="invoice.json",
        )
        workbook_contract = ArtifactContractSpec(
            contract_key="invoice.workbook.v1",
            media_type=XLSX_MEDIA_TYPE,
            file_name="invoice.xlsx",
        )
        plan_spec = TaskExecutionPlanSpec(
            initial_input_contracts=("invoice.image.v1",),
            steps=(
                PlanStepSpec(
                    step_key="extract",
                    role_key="extractor",
                    objective="Extract the invoice.",
                    acceptance_criteria="Return valid JSON.",
                    input_contracts=("invoice.image.v1",),
                    output_contracts=(json_contract,),
                ),
                PlanStepSpec(
                    step_key="excel",
                    role_key="excel",
                    objective="Create the workbook.",
                    acceptance_criteria="Return a valid XLSX file.",
                    depends_on=("extract",),
                    input_contracts=("invoice.extracted.v1",),
                    output_contracts=(workbook_contract,),
                ),
                PlanStepSpec(
                    step_key="review",
                    role_key="lead",
                    step_kind="lead_review",
                    objective="Review the workbook.",
                    acceptance_criteria="Do not repair specialist output.",
                    depends_on=("excel",),
                    input_contracts=("invoice.workbook.v1",),
                ),
            ),
        )
        plan, _ = create_task_execution_plan(
            session,
            task=task,
            plan_spec=plan_spec,
            source="test",
        )
        steps = session.scalars(
            select(PlanStep)
            .where(PlanStep.plan_id == plan.plan_id)
            .order_by(PlanStep.sequence)
        ).all()
        step_ids = {step.step_key: step.plan_step_id for step in steps}

        workspace_ids: dict[str, str] = {}
        workspace_paths: dict[str, Path] = {}
        for role_key in ("extractor", "excel", "lead"):
            workspace = provisioner.ensure_role_workspace(
                session,
                owner_user_id=user.user_id,
                organization_id=organization.organization_id,
                agent_role_key=role_key,
            )
            workspace_ids[role_key] = workspace.workspace_id
            workspace_paths[role_key] = Path(workspace.canonical_path)

        assignment_ids: dict[str, str] = {}
        for role_key, step_key in (("extractor", "extract"), ("excel", "excel")):
            assignment = Assignment(
                task_id=task.task_id,
                assignment_key=f"plan_step:{step_key}",
                assignment_kind=AssignmentKind.PLAN_STEP,
                agent_role_key=role_key,
                instructions=f"Complete {step_key}.",
                acceptance_criteria="Satisfy the output contract.",
                execution_id=f"execution-{role_key}",
                plan_step_id=step_ids[step_key],
                status=AssignmentStatus.RUNNING,
            )
            session.add(assignment)
            session.flush()
            execution = RuntimeExecution(
                execution_id=assignment.execution_id,
                assignment_id=assignment.assignment_id,
                provider="codex",
                workspace_id=workspace_ids[role_key],
                status=RuntimeExecutionStatus.RUNNING,
            )
            session.add(execution)
            assignment_ids[role_key] = assignment.assignment_id
        session.commit()
        return ArtifactEnvironment(
            database=database,
            manager=manager,
            task_id=task.task_id,
            plan_id=plan.plan_id,
            step_ids=step_ids,
            assignment_ids=assignment_ids,
            workspace_ids=workspace_ids,
            workspace_paths=workspace_paths,
        )


def publish_initial_image(
    environment: ArtifactEnvironment,
    source: Path,
) -> str:
    source.write_bytes(b"\x89PNG\r\n\x1a\ninvoice")
    artifact_manager = ArtifactManager(environment.manager)
    with environment.database.session() as session:
        task = session.get(Task, environment.task_id)
        plan = session.get(TaskExecutionPlan, environment.plan_id)
        assert task is not None and plan is not None
        artifact = artifact_manager.publish_task_input(
            session,
            task=task,
            plan=plan,
            contract=ArtifactContractSpec(
                contract_key="invoice.image.v1",
                media_type="image/png",
                file_name="invoice.png",
            ),
            source_path=source,
            source_delivery_id="upload-1",
        )
        return artifact.artifact_id


def extraction_delivery() -> AssignmentDelivery:
    return AssignmentDelivery(
        status="completed",
        summary="Extracted invoice fields.",
        artifacts=(
            ArtifactDeclaration(
                contract_key="invoice.extracted.v1",
                relative_path="outputs/invoice.json",
                media_type="application/json",
            ),
        ),
    )


def test_artifact_handoff_is_isolated_idempotent_and_version_frozen(tmp_path) -> None:
    environment = artifact_environment(tmp_path)
    artifact_manager = ArtifactManager(environment.manager)
    try:
        input_artifact_id = publish_initial_image(
            environment,
            tmp_path / "invoice.png",
        )
        with environment.database.session() as session:
            task = session.get(Task, environment.task_id)
            step = session.get(PlanStep, environment.step_ids["extract"])
            workspace = session.get(Workspace, environment.workspace_ids["extractor"])
            assert task is not None and step is not None and workspace is not None
            bindings = artifact_manager.materialize_step_inputs(
                session,
                task=task,
                plan_step=step,
                consumer_workspace=workspace,
            )
            assert bindings[0].artifact_id == input_artifact_id

        output = environment.workspace_paths["extractor"] / "outputs" / "invoice.json"
        output.parent.mkdir(parents=True)
        output.write_text(json.dumps({"total_cny": "720.00"}), encoding="utf-8")
        with environment.database.session() as session:
            task = session.get(Task, environment.task_id)
            assignment = session.get(
                Assignment,
                environment.assignment_ids["extractor"],
            )
            assert task is not None and assignment is not None
            first = artifact_manager.publish_assignment_delivery(
                session,
                task=task,
                assignment=assignment,
                delivery=extraction_delivery(),
                source_delivery_id="extract-turn-1",
            )[0]
            first_artifact_id = first.artifact_id
            replayed = artifact_manager.publish_assignment_delivery(
                session,
                task=task,
                assignment=assignment,
                delivery=extraction_delivery(),
                source_delivery_id="extract-turn-1",
            )[0]
            assert replayed.artifact_id == first_artifact_id

        with environment.database.session() as session:
            task = session.get(Task, environment.task_id)
            step = session.get(PlanStep, environment.step_ids["excel"])
            workspace = session.get(Workspace, environment.workspace_ids["excel"])
            assert task is not None and step is not None and workspace is not None
            first_binding = artifact_manager.materialize_step_inputs(
                session,
                task=task,
                plan_step=step,
                consumer_workspace=workspace,
            )[0]
            binding_id = first_binding.input_binding_id
            assert first_binding.artifact_id == first_artifact_id

        excel_workspace = environment.workspace_paths["excel"]
        assert list(excel_workspace.rglob("invoice.json"))
        assert not list(excel_workspace.rglob("invoice.png"))

        output.write_text(json.dumps({"total_cny": "1440.00"}), encoding="utf-8")
        with environment.database.session() as session:
            task = session.get(Task, environment.task_id)
            assignment = session.get(
                Assignment,
                environment.assignment_ids["extractor"],
            )
            assert task is not None and assignment is not None
            second = artifact_manager.publish_assignment_delivery(
                session,
                task=task,
                assignment=assignment,
                delivery=extraction_delivery(),
                source_delivery_id="extract-turn-2",
            )[0]
            assert second.artifact_version == 2
            first = session.get(Artifact, first_artifact_id)
            assert first is not None and first.status == ArtifactStatus.SUPERSEDED

        with environment.database.session() as session:
            task = session.get(Task, environment.task_id)
            step = session.get(PlanStep, environment.step_ids["excel"])
            workspace = session.get(Workspace, environment.workspace_ids["excel"])
            assert task is not None and step is not None and workspace is not None
            replayed_binding = artifact_manager.materialize_step_inputs(
                session,
                task=task,
                plan_step=step,
                consumer_workspace=workspace,
            )[0]
            assert replayed_binding.input_binding_id == binding_id
            assert replayed_binding.artifact_id == first_artifact_id
            assert session.scalar(
                select(func.count())
                .select_from(ProductEvent)
                .where(ProductEvent.event_type == "artifact.released")
            ) == 3
    finally:
        environment.database.dispose()


def test_artifact_manager_validates_xlsx_and_rejects_invalid_media(tmp_path) -> None:
    environment = artifact_environment(tmp_path)
    artifact_manager = ArtifactManager(environment.manager)
    workbook = environment.workspace_paths["excel"] / "outputs" / "invoice.xlsx"
    workbook.parent.mkdir(parents=True)
    delivery = AssignmentDelivery(
        status="completed",
        summary="Created workbook.",
        artifacts=(
            ArtifactDeclaration(
                contract_key="invoice.workbook.v1",
                relative_path="outputs/invoice.xlsx",
                media_type=XLSX_MEDIA_TYPE,
            ),
        ),
    )
    try:
        with zipfile.ZipFile(workbook, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types />")
            archive.writestr("xl/workbook.xml", "<workbook />")

        with environment.database.session() as session:
            task = session.get(Task, environment.task_id)
            assignment = session.get(Assignment, environment.assignment_ids["excel"])
            assert task is not None and assignment is not None
            artifact = artifact_manager.publish_assignment_delivery(
                session,
                task=task,
                assignment=assignment,
                delivery=delivery,
                source_delivery_id="excel-turn-1",
            )[0]
            assert artifact.validation_summary is not None
            assert "XLSX" in artifact.validation_summary

        workbook.write_bytes(b"not-an-xlsx")
        with environment.database.session() as session, pytest.raises(
            ArtifactError
        ) as exc_info:
            task = session.get(Task, environment.task_id)
            assignment = session.get(Assignment, environment.assignment_ids["excel"])
            assert task is not None and assignment is not None
            artifact_manager.publish_assignment_delivery(
                session,
                task=task,
                assignment=assignment,
                delivery=delivery,
                source_delivery_id="excel-turn-2",
            )
        assert exc_info.value.code == "ARTIFACT_MEDIA_VALIDATION_FAILED"
        with environment.database.session() as session:
            assert session.scalar(
                select(func.count())
                .select_from(Artifact)
                .where(Artifact.contract_key == "invoice.workbook.v1")
            ) == 1
    finally:
        environment.database.dispose()


def test_artifact_manager_rejects_missing_or_mismatched_outputs(tmp_path) -> None:
    environment = artifact_environment(tmp_path)
    artifact_manager = ArtifactManager(environment.manager)
    try:
        with environment.database.session() as session, pytest.raises(
            ArtifactError
        ) as exc_info:
            task = session.get(Task, environment.task_id)
            assignment = session.get(
                Assignment,
                environment.assignment_ids["extractor"],
            )
            assert task is not None and assignment is not None
            artifact_manager.publish_assignment_delivery(
                session,
                task=task,
                assignment=assignment,
                delivery=AssignmentDelivery(
                    status="completed",
                    summary="No Artifact was produced.",
                ),
                source_delivery_id="extract-missing-output",
            )
        assert exc_info.value.code == "ARTIFACT_OUTPUT_SET_MISMATCH"

        wrong_media = AssignmentDelivery(
            status="completed",
            summary="Declared the wrong media.",
            artifacts=(
                ArtifactDeclaration(
                    contract_key="invoice.extracted.v1",
                    relative_path="outputs/invoice.json",
                    media_type="text/plain",
                ),
            ),
        )
        with environment.database.session() as session, pytest.raises(
            ArtifactError
        ) as exc_info:
            task = session.get(Task, environment.task_id)
            assignment = session.get(
                Assignment,
                environment.assignment_ids["extractor"],
            )
            assert task is not None and assignment is not None
            artifact_manager.publish_assignment_delivery(
                session,
                task=task,
                assignment=assignment,
                delivery=wrong_media,
                source_delivery_id="extract-wrong-media",
            )
        assert exc_info.value.code == "ARTIFACT_MEDIA_TYPE_MISMATCH"
    finally:
        environment.database.dispose()
