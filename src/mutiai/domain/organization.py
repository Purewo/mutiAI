"""Organization definition contracts independent of LangGraph and Codex."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mutiai.domain.feasibility import WorkloadRequirements


class AgentRoleSpec(BaseModel):
    """One persistent formal role in an organization definition."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    role_key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    responsibility: str = Field(min_length=1, max_length=2_000)
    is_lead: bool = False
    reports_to: str | None = None
    runtime_binding_key: str = Field(min_length=1, max_length=64)
    capability_requirements: WorkloadRequirements = Field(
        default_factory=WorkloadRequirements
    )


class OrganizationSpec(BaseModel):
    """A versioned, portable organization definition for the V1 tree model."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        title="OrganizationSpecV1",
    )

    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2_000)
    roles: tuple[AgentRoleSpec, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_role_tree(self) -> Self:
        role_by_key = self._unique_roles(self.roles)
        lead_keys = [role.role_key for role in self.roles if role.is_lead]

        if len(lead_keys) != 1:
            raise ValueError("organization must contain exactly one lead role")

        lead_key = lead_keys[0]
        lead = role_by_key[lead_key]
        if lead.reports_to is not None:
            raise ValueError("organization lead must not report to another role")

        for role in self.roles:
            if role.role_key == lead_key:
                continue
            if role.reports_to is None:
                raise ValueError(
                    f"non-lead role '{role.role_key}' must declare reports_to"
                )
            if role.reports_to not in role_by_key:
                raise ValueError(
                    f"role '{role.role_key}' reports to unknown role "
                    f"'{role.reports_to}'"
                )
            if role.reports_to == role.role_key:
                raise ValueError(
                    f"role '{role.role_key}' cannot report to itself"
                )

        for role in self.roles:
            self._assert_path_reaches_lead(role, role_by_key, lead_key)

        return self

    @staticmethod
    def _unique_roles(
        roles: Iterable[AgentRoleSpec],
    ) -> dict[str, AgentRoleSpec]:
        role_by_key: dict[str, AgentRoleSpec] = {}
        for role in roles:
            if role.role_key in role_by_key:
                raise ValueError(f"duplicate role_key '{role.role_key}'")
            role_by_key[role.role_key] = role
        return role_by_key

    @staticmethod
    def _assert_path_reaches_lead(
        role: AgentRoleSpec,
        role_by_key: dict[str, AgentRoleSpec],
        lead_key: str,
    ) -> None:
        visited: set[str] = set()
        current = role

        while current.role_key != lead_key:
            if current.role_key in visited:
                raise ValueError(
                    f"role hierarchy contains a cycle at '{current.role_key}'"
                )
            visited.add(current.role_key)

            parent_key = current.reports_to
            if parent_key is None or parent_key not in role_by_key:
                raise ValueError(
                    f"role '{role.role_key}' is not connected to the lead"
                )
            current = role_by_key[parent_key]
