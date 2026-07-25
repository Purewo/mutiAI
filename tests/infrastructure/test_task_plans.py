import pytest
from sqlalchemy import func, select

from mutiai.api.errors import ApiError
from mutiai.config import Settings
from mutiai.db import Database
from mutiai.domain import (
    ArtifactContractSpec,
    PlanStepSpec,
    TaskExecutionPlanSpec,
)
from mutiai.migrations import upgrade_database
from mutiai.models import (
    Organization,
    OrganizationSpecVersion,
    PlanStep,
    PlanStepDependency,
    ProductEvent,
    Task,
    User,
)
from mutiai.models.organization import OrganizationVersionStatus
from mutiai.models.task import TaskStatus
from mutiai.models.task_plan import PlanStepStatus
from mutiai.services.task_plans import create_task_execution_plan


def seeded_task_database(tmp_path) -> tuple[Database, str]:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'task-plans.db'}",
        runtime_workspace_root=tmp_path / "managed",
        bootstrap_admin_enabled=False,
    )
    upgrade_database(settings.database_url)
    database = Database(settings)
    with database.session() as session:
        user = User(
            username="plan-owner",
            password_hash="test-only",
            display_name="Plan Owner",
        )
        session.add(user)
        session.flush()
        organization = Organization(
            owner_user_id=user.user_id,
            name="Invoice Organization",
            description="",
        )
        session.add(organization)
        session.flush()
        organization_payload = {
            "schema_version": "1.0",
            "name": "Invoice Organization",
            "description": "",
            "roles": [
                {
                    "role_key": "lead",
                    "name": "Lead",
                    "responsibility": "Plan and review work.",
                    "is_lead": True,
                    "reports_to": None,
                    "runtime_binding_key": "codex-lead",
                },
                {
                    "role_key": "extractor",
                    "name": "Extractor",
                    "responsibility": "Extract invoice data.",
                    "is_lead": False,
                    "reports_to": "lead",
                    "runtime_binding_key": "codex-extractor",
                },
                {
                    "role_key": "excel",
                    "name": "Excel",
                    "responsibility": "Build the CNY workbook.",
                    "is_lead": False,
                    "reports_to": "lead",
                    "runtime_binding_key": "codex-excel",
                },
                {
                    "role_key": "translator",
                    "name": "Translator",
                    "responsibility": "Add USD values.",
                    "is_lead": False,
                    "reports_to": "lead",
                    "runtime_binding_key": "codex-translator",
                },
            ],
        }
        version = OrganizationSpecVersion(
            organization_id=organization.organization_id,
            owner_user_id=user.user_id,
            version_number=1,
            status=OrganizationVersionStatus.PUBLISHED,
            spec_payload=organization_payload,
        )
        session.add(version)
        session.flush()
        organization.current_published_version_id = version.spec_version_id
        task = Task(
            owner_user_id=user.user_id,
            organization_id=organization.organization_id,
            organization_spec_version_id=version.spec_version_id,
            request_text="Extract the invoice and convert CNY to USD.",
            request_hash="a" * 64,
            idempotency_key="task-plan-test",
            status=TaskStatus.CREATED,
        )
        session.add(task)
        session.commit()
        return database, task.task_id


def contract(key: str, file_name: str, media_type: str) -> ArtifactContractSpec:
    return ArtifactContractSpec(
        contract_key=key,
        file_name=file_name,
        media_type=media_type,
    )


def invoice_plan() -> TaskExecutionPlanSpec:
    json_media = "application/json"
    xlsx_media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return TaskExecutionPlanSpec(
        summary="Linear invoice handoff.",
        initial_input_contracts=("invoice.image.v1",),
        steps=(
            PlanStepSpec(
                step_key="extract",
                role_key="extractor",
                objective="Extract only invoice fields.",
                acceptance_criteria="Return valid structured invoice data.",
                input_contracts=("invoice.image.v1",),
                output_contracts=(
                    contract("invoice.extracted.v1", "invoice.json", json_media),
                ),
            ),
            PlanStepSpec(
                step_key="excel",
                role_key="excel",
                objective="Build the CNY workbook.",
                acceptance_criteria="Preserve every extracted CNY value.",
                depends_on=("extract",),
                input_contracts=("invoice.extracted.v1",),
                output_contracts=(
                    contract("invoice.workbook.cny.v1", "invoice-cny.xlsx", xlsx_media),
                ),
            ),
            PlanStepSpec(
                step_key="translate",
                role_key="translator",
                objective="Add USD values at the fixed exchange rate.",
                acceptance_criteria="Preserve CNY and round USD to two decimals.",
                depends_on=("excel",),
                input_contracts=("invoice.workbook.cny.v1",),
                output_contracts=(
                    contract("invoice.workbook.usd.v1", "invoice-usd.xlsx", xlsx_media),
                ),
            ),
            PlanStepSpec(
                step_key="review",
                role_key="lead",
                step_kind="lead_review",
                objective="Review the final workbook without repairing it.",
                acceptance_criteria="Accept only a contract-complete workbook.",
                depends_on=("translate",),
                input_contracts=("invoice.workbook.usd.v1",),
            ),
        ),
    )


def parallel_plan() -> TaskExecutionPlanSpec:
    return TaskExecutionPlanSpec(
        summary="Extract three independent invoice views and review all Artifacts.",
        initial_input_contracts=("invoice.image.v1",),
        steps=(
            PlanStepSpec(
                step_key="extract",
                role_key="extractor",
                objective="Extract invoice fields.",
                acceptance_criteria="Return structured invoice data.",
                input_contracts=("invoice.image.v1",),
                output_contracts=(
                    contract(
                        "invoice.extracted.v1",
                        "invoice.json",
                        "application/json",
                    ),
                ),
            ),
            PlanStepSpec(
                step_key="excel",
                role_key="excel",
                objective="Produce an independent workbook manifest.",
                acceptance_criteria="Return the workbook manifest.",
                input_contracts=("invoice.image.v1",),
                output_contracts=(
                    contract(
                        "invoice.excel-manifest.v1",
                        "excel-manifest.json",
                        "application/json",
                    ),
                ),
            ),
            PlanStepSpec(
                step_key="translate",
                role_key="translator",
                objective="Produce an independent currency manifest.",
                acceptance_criteria="Return the currency manifest.",
                input_contracts=("invoice.image.v1",),
                output_contracts=(
                    contract(
                        "invoice.currency-manifest.v1",
                        "currency-manifest.json",
                        "application/json",
                    ),
                ),
            ),
            PlanStepSpec(
                step_key="review",
                role_key="lead",
                step_kind="lead_review",
                objective="Review all released specialist Artifacts.",
                acceptance_criteria="Accept only a complete parallel delivery.",
                depends_on=("extract", "excel", "translate"),
                input_contracts=(
                    "invoice.extracted.v1",
                    "invoice.excel-manifest.v1",
                    "invoice.currency-manifest.v1",
                ),
            ),
        ),
    )


def test_create_task_execution_plan_is_durable_and_idempotent(tmp_path) -> None:
    database, task_id = seeded_task_database(tmp_path)
    try:
        with database.session() as session:
            task = session.get(Task, task_id)
            assert task is not None
            plan, created = create_task_execution_plan(
                session,
                task=task,
                plan_spec=invoice_plan(),
                source="lead_runtime",
            )
            plan_id = plan.plan_id
            assert created is True

        with database.session() as session:
            task = session.get(Task, task_id)
            assert task is not None
            replayed, created = create_task_execution_plan(
                session,
                task=task,
                plan_spec=invoice_plan(),
                source="lead_runtime",
            )
            assert replayed.plan_id == plan_id
            assert created is False
            steps = session.scalars(
                select(PlanStep)
                .where(PlanStep.plan_id == plan_id)
                .order_by(PlanStep.sequence)
            ).all()
            assert [step.step_key for step in steps] == [
                "extract",
                "excel",
                "translate",
                "review",
            ]
            assert steps[0].status == PlanStepStatus.READY
            assert all(
                step.status == PlanStepStatus.PENDING_DEPENDENCY
                for step in steps[1:]
            )
            assert session.scalar(
                select(func.count()).select_from(PlanStepDependency)
            ) == 3
            assert session.scalar(
                select(func.count())
                .select_from(ProductEvent)
                .where(ProductEvent.event_type == "task.execution_plan_created")
            ) == 1
    finally:
        database.dispose()


def test_task_plan_version_replay_rejects_a_different_definition(tmp_path) -> None:
    database, task_id = seeded_task_database(tmp_path)
    try:
        with database.session() as session:
            task = session.get(Task, task_id)
            assert task is not None
            create_task_execution_plan(
                session,
                task=task,
                plan_spec=invoice_plan(),
                source="lead_runtime",
            )

        changed = invoice_plan().model_copy(update={"summary": "Changed plan."})
        with database.session() as session, pytest.raises(ApiError) as exc_info:
            task = session.get(Task, task_id)
            assert task is not None
            create_task_execution_plan(
                session,
                task=task,
                plan_spec=changed,
                source="lead_runtime",
            )
        assert exc_info.value.code == "TASK_PLAN_VERSION_CONFLICT"
    finally:
        database.dispose()


def test_task_plan_persists_every_parallel_root_as_ready(tmp_path) -> None:
    database, task_id = seeded_task_database(tmp_path)
    try:
        with database.session() as session:
            task = session.get(Task, task_id)
            assert task is not None
            plan, created = create_task_execution_plan(
                session,
                task=task,
                plan_spec=parallel_plan(),
                source="lead_runtime",
            )
            assert created is True
            assert plan.validation_summary == "Validated as a pure parallel M2.3 plan."
            steps = session.scalars(
                select(PlanStep)
                .where(PlanStep.plan_id == plan.plan_id)
                .order_by(PlanStep.sequence)
            ).all()
            assert [step.status for step in steps] == [
                PlanStepStatus.READY,
                PlanStepStatus.READY,
                PlanStepStatus.READY,
                PlanStepStatus.PENDING_DEPENDENCY,
            ]
            assert session.scalar(
                select(func.count())
                .select_from(ProductEvent)
                .where(ProductEvent.event_type == "plan.step_ready")
            ) == 3
    finally:
        database.dispose()


def test_task_plan_rejects_a_mixed_serial_parallel_shape(tmp_path) -> None:
    database, task_id = seeded_task_database(tmp_path)
    base = invoice_plan()
    branched_steps = list(base.steps)
    branched_steps[2] = branched_steps[2].model_copy(
        update={"depends_on": ("extract",)}
    )
    branched = base.model_copy(update={"steps": tuple(branched_steps)})
    try:
        with database.session() as session, pytest.raises(ApiError) as exc_info:
            task = session.get(Task, task_id)
            assert task is not None
            create_task_execution_plan(
                session,
                task=task,
                plan_spec=branched,
                source="lead_runtime",
            )
        assert exc_info.value.code == "TASK_PLAN_SUPPORTED_SHAPE_REQUIRED"
    finally:
        database.dispose()


def test_parallel_task_plan_requires_every_output_at_lead_review(tmp_path) -> None:
    database, task_id = seeded_task_database(tmp_path)
    base = parallel_plan()
    changed_steps = list(base.steps)
    changed_steps[-1] = changed_steps[-1].model_copy(
        update={"input_contracts": ("invoice.extracted.v1",)}
    )
    changed = base.model_copy(update={"steps": tuple(changed_steps)})
    try:
        with database.session() as session, pytest.raises(ApiError) as exc_info:
            task = session.get(Task, task_id)
            assert task is not None
            create_task_execution_plan(
                session,
                task=task,
                plan_spec=changed,
                source="lead_runtime",
            )
        assert exc_info.value.code == "TASK_PLAN_PARALLEL_REVIEW_INPUTS_INCOMPLETE"
    finally:
        database.dispose()


def test_task_plan_rejects_an_unknown_organization_role(tmp_path) -> None:
    database, task_id = seeded_task_database(tmp_path)
    base = invoice_plan()
    changed_steps = list(base.steps)
    changed_steps[0] = changed_steps[0].model_copy(
        update={"role_key": "unknown-role"}
    )
    changed = base.model_copy(update={"steps": tuple(changed_steps)})
    try:
        with database.session() as session, pytest.raises(ApiError) as exc_info:
            task = session.get(Task, task_id)
            assert task is not None
            create_task_execution_plan(
                session,
                task=task,
                plan_spec=changed,
                source="lead_runtime",
            )
        assert exc_info.value.code == "TASK_PLAN_UNKNOWN_ROLE"
    finally:
        database.dispose()
