import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mutiai.domain import AgentRoleSpec, OrganizationSpec

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def role(
    role_key: str,
    *,
    is_lead: bool = False,
    reports_to: str | None = None,
) -> AgentRoleSpec:
    return AgentRoleSpec(
        role_key=role_key,
        name=role_key.replace("-", " ").title(),
        responsibility=f"Own {role_key} work",
        is_lead=is_lead,
        reports_to=reports_to,
        runtime_binding_key="codex-local-default",
    )


def test_accepts_nested_tree_with_exactly_one_lead() -> None:
    spec = OrganizationSpec(
        name="Product Engineering",
        description="Build and validate product changes",
        roles=(
            role("lead", is_lead=True),
            role("backend-lead", reports_to="lead"),
            role("backend-dev", reports_to="backend-lead"),
            role("test", reports_to="lead"),
        ),
    )

    assert spec.schema_version == "1.0"
    assert spec.roles[0].role_key == "lead"


@pytest.mark.parametrize(
    ("roles", "message"),
    [
        ((role("developer"),), "exactly one lead"),
        (
            (
                role("lead-a", is_lead=True),
                role("lead-b", is_lead=True),
            ),
            "exactly one lead",
        ),
        (
            (
                role("lead", is_lead=True),
                role("developer"),
            ),
            "must declare reports_to",
        ),
        (
            (
                role("lead", is_lead=True),
                role("developer", reports_to="missing"),
            ),
            "reports to unknown role",
        ),
        (
            (
                role("lead", is_lead=True),
                role("developer", reports_to="developer"),
            ),
            "cannot report to itself",
        ),
        (
            (
                role("lead", is_lead=True),
                role("worker", reports_to="lead"),
                role("worker", reports_to="lead"),
            ),
            "duplicate role_key",
        ),
        (
            (
                role("lead", is_lead=True),
                role("a", reports_to="b"),
                role("b", reports_to="a"),
            ),
            "contains a cycle",
        ),
    ],
)
def test_rejects_invalid_role_hierarchy(
    roles: tuple[AgentRoleSpec, ...],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        OrganizationSpec(name="Invalid organization", roles=roles)


def test_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OrganizationSpec.model_validate(
            {
                "name": "Unexpected field",
                "roles": [
                    {
                        "role_key": "lead",
                        "name": "Lead",
                        "responsibility": "Coordinate the organization",
                        "is_lead": True,
                        "runtime_binding_key": "codex-local-default",
                    }
                ],
                "langgraph_node": "must-not-leak",
            }
        )


def test_committed_json_schema_matches_model() -> None:
    path = PROJECT_ROOT / "contracts" / "schemas" / "organization-spec.v1.json"
    committed = json.loads(path.read_text(encoding="utf-8"))

    assert committed == OrganizationSpec.model_json_schema()

