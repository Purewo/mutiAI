"""Portable Runtime capability and workload requirement contracts."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OperatingSystemFamily = Literal["linux", "windows", "macos", "unknown"]
CapacityClass = Literal["light", "standard", "heavy", "unknown"]


class WorkloadRequirements(BaseModel):
    """Capabilities required by one role or one submitted workload."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        title="WorkloadRequirementsV1",
    )

    schema_version: Literal["1.0"] = "1.0"
    os_families: tuple[Literal["linux", "windows", "macos"], ...] = ()
    requires_gui: bool = False
    requires_gpu: bool = False
    min_gpu_memory_mb: int | None = Field(default=None, ge=1)
    min_cpu_capacity: Literal["light", "standard", "heavy"] = "light"
    min_memory_mb: int | None = Field(default=None, ge=1)
    required_tools: tuple[str, ...] = ()
    requires_network: bool = False
    required_external_services: tuple[str, ...] = ()
    required_hardware: tuple[str, ...] = ()
    required_proprietary_software: tuple[str, ...] = ()
    input_media_types: tuple[str, ...] = ()
    output_media_types: tuple[str, ...] = ()
    estimated_duration_seconds: int | None = Field(default=None, ge=1)
    estimated_input_size_bytes: int | None = Field(default=None, ge=1)
    resource_intensive: bool = False

    @field_validator(
        "os_families",
        "required_tools",
        "required_external_services",
        "required_hardware",
        "required_proprietary_software",
        "input_media_types",
        "output_media_types",
    )
    @classmethod
    def reject_duplicate_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = [item.casefold() for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("capability requirement items must be unique")
        return value

    @model_validator(mode="after")
    def require_gpu_for_gpu_memory(self) -> Self:
        if self.min_gpu_memory_mb is not None and not self.requires_gpu:
            raise ValueError("min_gpu_memory_mb requires requires_gpu=true")
        return self


class RuntimeCapabilityProfileSpec(BaseModel):
    """Versioned declaration of an effective Runtime environment."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        title="RuntimeCapabilityProfileV1",
    )

    schema_version: Literal["1.0"] = "1.0"
    os_family: OperatingSystemFamily = "unknown"
    os_version: str | None = Field(default=None, max_length=100)
    architecture: str | None = Field(default=None, max_length=100)
    headless: bool | None = None
    cpu_capacity_class: CapacityClass = "unknown"
    memory_mb: int | None = Field(default=None, ge=1)
    gpu_available: bool | None = None
    gpu_kind: str | None = Field(default=None, max_length=100)
    gpu_memory_mb: int | None = Field(default=None, ge=1)
    installed_tools: tuple[str, ...] = ()
    tool_inventory_complete: bool = False
    network_access: bool | None = None
    external_services: tuple[str, ...] = ()
    external_service_inventory_complete: bool = False
    attached_hardware: tuple[str, ...] = ()
    hardware_inventory_complete: bool = False
    proprietary_software: tuple[str, ...] = ()
    proprietary_software_inventory_complete: bool = False
    supported_input_media_types: tuple[str, ...] = ()
    supported_output_media_types: tuple[str, ...] = ()
    media_inventory_complete: bool = False
    max_duration_seconds: int | None = Field(default=None, ge=1)
    max_input_size_bytes: int | None = Field(default=None, ge=1)

    @field_validator(
        "installed_tools",
        "external_services",
        "attached_hardware",
        "proprietary_software",
        "supported_input_media_types",
        "supported_output_media_types",
    )
    @classmethod
    def reject_duplicate_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = [item.casefold() for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("capability profile items must be unique")
        return value

    @model_validator(mode="after")
    def validate_gpu_declaration(self) -> Self:
        if self.gpu_available is False and (
            self.gpu_kind is not None or self.gpu_memory_mb is not None
        ):
            raise ValueError("GPU details require an available or unknown GPU")
        return self


class FeasibilityFinding(BaseModel):
    """One deterministic mismatch or missing capability declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason_code: str = Field(min_length=1, max_length=100)
    role_key: str = Field(min_length=1, max_length=64)
    binding_key: str = Field(min_length=1, max_length=64)
    capability: str = Field(min_length=1, max_length=100)
    required: Any | None = None
    actual: Any | None = None
    alternative_codes: tuple[str, ...] = ()
