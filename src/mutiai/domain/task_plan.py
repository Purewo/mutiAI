"""Portable Task plan and Runtime delivery contracts."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

KEY_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


class ArtifactContractSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    contract_key: str = Field(pattern=KEY_PATTERN)
    schema_version: str = Field(default="1.0", min_length=1, max_length=20)
    media_type: str = Field(min_length=1, max_length=255)
    file_name: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_file_name(self) -> Self:
        if "/" in self.file_name or "\\" in self.file_name:
            raise ValueError("Artifact contract file_name must be a base name")
        if self.file_name in {".", ".."}:
            raise ValueError("Artifact contract file_name is invalid")
        return self


class PlanStepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    step_key: str = Field(pattern=KEY_PATTERN)
    role_key: str = Field(min_length=1, max_length=64)
    step_kind: Literal["specialist", "lead_review"] = "specialist"
    objective: str = Field(min_length=1, max_length=10_000)
    acceptance_criteria: str = Field(min_length=1, max_length=10_000)
    depends_on: tuple[str, ...] = Field(default=(), max_length=100)
    input_contracts: tuple[str, ...] = Field(default=(), max_length=100)
    output_contracts: tuple[ArtifactContractSpec, ...] = Field(
        default=(), max_length=20
    )


class TaskExecutionPlanSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    summary: str = Field(default="", max_length=20_000)
    initial_input_contracts: tuple[str, ...] = Field(default=(), max_length=100)
    steps: tuple[PlanStepSpec, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_graph_and_contract_flow(self) -> Self:
        step_by_key: dict[str, PlanStepSpec] = {}
        output_producer: dict[str, str] = {}
        for step in self.steps:
            if step.step_key in step_by_key:
                raise ValueError(f"duplicate step_key '{step.step_key}'")
            step_by_key[step.step_key] = step
            if len(set(step.depends_on)) != len(step.depends_on):
                raise ValueError(f"step '{step.step_key}' has duplicate dependencies")
            for contract in step.output_contracts:
                if contract.contract_key in output_producer:
                    raise ValueError(
                        f"contract '{contract.contract_key}' has multiple producers"
                    )
                output_producer[contract.contract_key] = step.step_key

        for step in self.steps:
            for dependency in step.depends_on:
                if dependency not in step_by_key:
                    raise ValueError(
                        f"step '{step.step_key}' depends on unknown step "
                        f"'{dependency}'"
                    )
                if dependency == step.step_key:
                    raise ValueError(f"step '{step.step_key}' depends on itself")

        ancestors = self._assert_acyclic(step_by_key)
        initial = set(self.initial_input_contracts)
        for step in self.steps:
            for contract_key in step.input_contracts:
                producer = output_producer.get(contract_key)
                if contract_key in initial:
                    continue
                if producer is None or producer not in ancestors[step.step_key]:
                    raise ValueError(
                        f"step '{step.step_key}' input contract '{contract_key}' "
                        "has no dependency ancestor producer"
                    )
        return self

    @staticmethod
    def _assert_acyclic(
        step_by_key: dict[str, PlanStepSpec],
    ) -> dict[str, set[str]]:
        children: dict[str, set[str]] = defaultdict(set)
        indegree = {key: 0 for key in step_by_key}
        for step in step_by_key.values():
            for dependency in step.depends_on:
                children[dependency].add(step.step_key)
                indegree[step.step_key] += 1

        queue = deque(key for key, degree in indegree.items() if degree == 0)
        ordered: list[str] = []
        while queue:
            key = queue.popleft()
            ordered.append(key)
            for child in children[key]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(ordered) != len(step_by_key):
            raise ValueError("Task execution plan contains a dependency cycle")

        ancestors: dict[str, set[str]] = {key: set() for key in step_by_key}
        for key in ordered:
            for child in children[key]:
                ancestors[child].add(key)
                ancestors[child].update(ancestors[key])
        return ancestors


class ArtifactDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    contract_key: str = Field(pattern=KEY_PATTERN)
    relative_path: str = Field(min_length=1, max_length=1_024)
    media_type: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_relative_path(self) -> Self:
        path = self.relative_path
        if path.startswith(("/", "\\")) or WINDOWS_DRIVE_PATTERN.match(path):
            raise ValueError("Artifact relative_path must be relative")
        parts = [part for part in re.split(r"[\\/]", path) if part not in {"", "."}]
        if not parts or ".." in parts:
            raise ValueError("Artifact relative_path contains traversal")
        return self


class AssignmentDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    status: Literal["completed", "blocked"]
    summary: str = Field(min_length=1, max_length=20_000)
    artifacts: tuple[ArtifactDeclaration, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def blocked_delivery_has_no_artifacts(self) -> Self:
        if self.status == "blocked" and self.artifacts:
            raise ValueError("A blocked delivery cannot declare Artifacts")
        contract_keys = [artifact.contract_key for artifact in self.artifacts]
        if len(contract_keys) != len(set(contract_keys)):
            raise ValueError("A delivery cannot declare one contract more than once")
        return self
