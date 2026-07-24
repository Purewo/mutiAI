"""Organization API contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import overload

from pydantic import BaseModel, Field

from mutiai.domain import OrganizationSpec
from mutiai.models import Organization, OrganizationSpecVersion
from mutiai.models.organization import OrganizationVersionStatus


@overload
def as_utc(value: datetime) -> datetime: ...


@overload
def as_utc(value: None) -> None: ...


def as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class OrganizationProposalRequest(BaseModel):
    organization_id: str | None = Field(default=None, max_length=36)
    source_request: str | None = Field(default=None, max_length=4_000)
    spec: OrganizationSpec


class OrganizationVersionResponse(BaseModel):
    spec_version_id: str
    organization_id: str
    version_number: int
    status: OrganizationVersionStatus
    spec: OrganizationSpec
    source_request: str | None
    created_at: datetime
    confirmed_at: datetime | None
    published_at: datetime | None

    @classmethod
    def from_record(
        cls, version: OrganizationSpecVersion
    ) -> OrganizationVersionResponse:
        return cls(
            spec_version_id=version.spec_version_id,
            organization_id=version.organization_id,
            version_number=version.version_number,
            status=OrganizationVersionStatus(version.status),
            spec=OrganizationSpec.model_validate(version.spec_payload),
            source_request=version.source_request,
            created_at=as_utc(version.created_at),
            confirmed_at=as_utc(version.confirmed_at),
            published_at=as_utc(version.published_at),
        )


class OrganizationSummaryResponse(BaseModel):
    organization_id: str
    name: str
    description: str
    current_published_version_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, organization: Organization) -> OrganizationSummaryResponse:
        return cls(
            organization_id=organization.organization_id,
            name=organization.name,
            description=organization.description,
            current_published_version_id=(organization.current_published_version_id),
            created_at=as_utc(organization.created_at),
            updated_at=as_utc(organization.updated_at),
        )


class OrganizationDetailResponse(OrganizationSummaryResponse):
    current_published_spec: OrganizationSpec | None
