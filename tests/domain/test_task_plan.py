import pytest
from pydantic import ValidationError

from mutiai.domain import (
    ArtifactContractSpec,
    ArtifactDeclaration,
    AssignmentDelivery,
    PlanStepSpec,
    TaskExecutionPlanSpec,
)


def artifact_contract(contract_key: str, file_name: str) -> ArtifactContractSpec:
    return ArtifactContractSpec(
        contract_key=contract_key,
        media_type="application/json",
        file_name=file_name,
    )


def plan_step(
    step_key: str,
    *,
    depends_on: tuple[str, ...] = (),
    input_contracts: tuple[str, ...] = (),
    output_contracts: tuple[ArtifactContractSpec, ...] = (),
) -> PlanStepSpec:
    return PlanStepSpec(
        step_key=step_key,
        role_key=f"role-{step_key}",
        objective=f"Complete {step_key}.",
        acceptance_criteria=f"Validate {step_key}.",
        depends_on=depends_on,
        input_contracts=input_contracts,
        output_contracts=output_contracts,
    )


def test_task_execution_plan_accepts_a_contract_complete_linear_flow() -> None:
    plan = TaskExecutionPlanSpec(
        summary="Extract the invoice, create a workbook, and convert the currency.",
        steps=(
            plan_step(
                "extract",
                output_contracts=(
                    artifact_contract("invoice.extracted.v1", "invoice.json"),
                ),
            ),
            plan_step(
                "workbook",
                depends_on=("extract",),
                input_contracts=("invoice.extracted.v1",),
                output_contracts=(
                    artifact_contract("invoice.workbook.cny.v1", "invoice-cny.json"),
                ),
            ),
            plan_step(
                "translate",
                depends_on=("workbook",),
                input_contracts=("invoice.workbook.cny.v1",),
                output_contracts=(
                    artifact_contract("invoice.workbook.usd.v1", "invoice-usd.json"),
                ),
            ),
        ),
    )

    assert tuple(step.step_key for step in plan.steps) == (
        "extract",
        "workbook",
        "translate",
    )


def test_task_execution_plan_rejects_an_unknown_dependency() -> None:
    with pytest.raises(ValidationError, match="depends on unknown step 'missing'"):
        TaskExecutionPlanSpec(
            steps=(plan_step("extract", depends_on=("missing",)),)
        )


def test_task_execution_plan_rejects_a_dependency_cycle() -> None:
    with pytest.raises(ValidationError, match="dependency cycle"):
        TaskExecutionPlanSpec(
            steps=(
                plan_step("extract", depends_on=("workbook",)),
                plan_step("workbook", depends_on=("extract",)),
            )
        )


def test_task_execution_plan_rejects_duplicate_step_keys() -> None:
    with pytest.raises(ValidationError, match="duplicate step_key 'extract'"):
        TaskExecutionPlanSpec(
            steps=(plan_step("extract"), plan_step("extract"))
        )


def test_task_execution_plan_rejects_multiple_contract_producers() -> None:
    contract = artifact_contract("invoice.extracted.v1", "invoice.json")

    with pytest.raises(ValidationError, match="has multiple producers"):
        TaskExecutionPlanSpec(
            steps=(
                plan_step("extract-a", output_contracts=(contract,)),
                plan_step("extract-b", output_contracts=(contract,)),
            )
        )


def test_task_execution_plan_requires_an_ancestor_contract_producer() -> None:
    with pytest.raises(ValidationError, match="no dependency ancestor producer"):
        TaskExecutionPlanSpec(
            steps=(
                plan_step(
                    "extract",
                    output_contracts=(
                        artifact_contract("invoice.extracted.v1", "invoice.json"),
                    ),
                ),
                plan_step(
                    "workbook",
                    input_contracts=("invoice.extracted.v1",),
                ),
            )
        )


@pytest.mark.parametrize(
    "relative_path",
    (
        "../invoice.json",
        "outputs/../../invoice.json",
        "/tmp/invoice.json",
        r"C:\tmp\invoice.json",
        r"C:tmp\invoice.json",
        r"\\server\share\invoice.json",
    ),
)
def test_artifact_declaration_rejects_unsafe_paths(relative_path: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactDeclaration(
            contract_key="invoice.extracted.v1",
            relative_path=relative_path,
            media_type="application/json",
        )


def test_blocked_delivery_cannot_declare_artifacts() -> None:
    declaration = ArtifactDeclaration(
        contract_key="invoice.extracted.v1",
        relative_path="outputs/invoice.json",
        media_type="application/json",
    )

    with pytest.raises(ValidationError, match="blocked delivery cannot declare"):
        AssignmentDelivery(
            status="blocked",
            summary="The source invoice is unavailable.",
            artifacts=(declaration,),
        )
