"""Product-owned Runtime binding API contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from mutiai.api.schemas.organizations import as_utc
from mutiai.domain import RuntimeCapabilityProfileSpec
from mutiai.models import (
    RuntimeBinding,
    RuntimeCapabilityProfile,
    RuntimeSecurityMode,
)


class RuntimeBindingUpsertRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    provider: str = Field(min_length=1, max_length=32)
    model: str | None = Field(default=None, min_length=1, max_length=100)
    reasoning_effort: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
    )
    security_mode: RuntimeSecurityMode
    capability_profile: RuntimeCapabilityProfileSpec | None = None


class RuntimeCapabilityProfileResponse(BaseModel):
    capability_profile_id: str
    revision: int
    profile: RuntimeCapabilityProfileSpec
    source: str
    trusted: bool
    observed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_record(
        cls,
        profile: RuntimeCapabilityProfile,
    ) -> RuntimeCapabilityProfileResponse:
        return cls(
            capability_profile_id=profile.capability_profile_id,
            revision=profile.revision,
            profile=RuntimeCapabilityProfileSpec.model_validate(
                profile.profile_payload
            ),
            source=profile.source,
            trusted=profile.trusted,
            observed_at=as_utc(profile.observed_at),
            created_at=as_utc(profile.created_at),
        )


class RuntimeBindingResponse(BaseModel):
    runtime_binding_id: str
    binding_key: str
    provider: str
    model: str | None
    reasoning_effort: str | None
    security_mode: RuntimeSecurityMode
    capability_profile: RuntimeCapabilityProfileResponse
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(
        cls,
        binding: RuntimeBinding,
        profile: RuntimeCapabilityProfile,
    ) -> RuntimeBindingResponse:
        return cls(
            runtime_binding_id=binding.runtime_binding_id,
            binding_key=binding.binding_key,
            provider=binding.provider,
            model=binding.model,
            reasoning_effort=binding.reasoning_effort,
            security_mode=RuntimeSecurityMode(binding.security_mode),
            capability_profile=RuntimeCapabilityProfileResponse.from_record(profile),
            is_active=binding.is_active,
            created_at=as_utc(binding.created_at),
            updated_at=as_utc(binding.updated_at),
        )
