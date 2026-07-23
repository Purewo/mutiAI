"""Organization proposal and publication state transitions."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mutiai.api.errors import ApiError
from mutiai.domain import OrganizationSpec
from mutiai.models import Organization, OrganizationSpecVersion
from mutiai.models.base import utc_now
from mutiai.models.organization import OrganizationVersionStatus


def get_owned_organization(
    session: Session,
    *,
    organization_id: str,
    owner_user_id: str,
) -> Organization:
    organization = session.scalar(
        select(Organization).where(
            Organization.organization_id == organization_id,
            Organization.owner_user_id == owner_user_id,
        )
    )
    if organization is None:
        raise ApiError(404, "ORGANIZATION_NOT_FOUND", "Organization not found.")
    return organization


def get_owned_version(
    session: Session,
    *,
    organization_id: str,
    spec_version_id: str,
    owner_user_id: str,
) -> OrganizationSpecVersion:
    version = session.scalar(
        select(OrganizationSpecVersion).where(
            OrganizationSpecVersion.spec_version_id == spec_version_id,
            OrganizationSpecVersion.organization_id == organization_id,
            OrganizationSpecVersion.owner_user_id == owner_user_id,
        )
    )
    if version is None:
        raise ApiError(
            404,
            "ORGANIZATION_VERSION_NOT_FOUND",
            "Organization version not found.",
        )
    return version


def create_proposal(
    session: Session,
    *,
    owner_user_id: str,
    spec: OrganizationSpec,
    organization_id: str | None,
    source_request: str | None,
) -> OrganizationSpecVersion:
    if organization_id is None:
        organization = Organization(
            owner_user_id=owner_user_id,
            name=spec.name,
            description=spec.description,
        )
        session.add(organization)
        session.flush()
        version_number = 1
    else:
        organization = get_owned_organization(
            session,
            organization_id=organization_id,
            owner_user_id=owner_user_id,
        )
        latest_version = session.scalar(
            select(func.max(OrganizationSpecVersion.version_number)).where(
                OrganizationSpecVersion.organization_id == organization.organization_id
            )
        )
        version_number = (latest_version or 0) + 1

    version = OrganizationSpecVersion(
        organization_id=organization.organization_id,
        owner_user_id=owner_user_id,
        version_number=version_number,
        status=OrganizationVersionStatus.PROPOSAL,
        spec_payload=spec.model_dump(mode="json"),
        source_request=source_request,
    )
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


def confirm_version(
    session: Session,
    *,
    organization_id: str,
    spec_version_id: str,
    owner_user_id: str,
) -> OrganizationSpecVersion:
    version = get_owned_version(
        session,
        organization_id=organization_id,
        spec_version_id=spec_version_id,
        owner_user_id=owner_user_id,
    )
    if version.status == OrganizationVersionStatus.CONFIRMED:
        return version
    if version.status != OrganizationVersionStatus.PROPOSAL:
        raise ApiError(
            409,
            "ORGANIZATION_VERSION_STATE_CONFLICT",
            "Only a proposal can be confirmed.",
        )

    version.status = OrganizationVersionStatus.CONFIRMED
    version.confirmed_at = utc_now()
    session.commit()
    session.refresh(version)
    return version


def publish_version(
    session: Session,
    *,
    organization_id: str,
    spec_version_id: str,
    owner_user_id: str,
) -> OrganizationSpecVersion:
    organization = get_owned_organization(
        session,
        organization_id=organization_id,
        owner_user_id=owner_user_id,
    )
    version = get_owned_version(
        session,
        organization_id=organization_id,
        spec_version_id=spec_version_id,
        owner_user_id=owner_user_id,
    )
    if (
        version.status == OrganizationVersionStatus.PUBLISHED
        and organization.current_published_version_id == version.spec_version_id
    ):
        return version
    if version.status != OrganizationVersionStatus.CONFIRMED:
        raise ApiError(
            409,
            "ORGANIZATION_VERSION_STATE_CONFLICT",
            "Only a confirmed version can be published.",
        )

    latest_version_number = session.scalar(
        select(func.max(OrganizationSpecVersion.version_number)).where(
            OrganizationSpecVersion.organization_id == organization_id
        )
    )
    if version.version_number != latest_version_number:
        raise ApiError(
            409,
            "ORGANIZATION_VERSION_STALE",
            "A newer organization version already exists.",
        )

    now = utc_now()
    if organization.current_published_version_id is not None:
        current = session.get(
            OrganizationSpecVersion,
            organization.current_published_version_id,
        )
        if (
            current is not None
            and current.status == OrganizationVersionStatus.PUBLISHED
        ):
            current.status = OrganizationVersionStatus.SUPERSEDED

    spec = OrganizationSpec.model_validate(version.spec_payload)
    version.status = OrganizationVersionStatus.PUBLISHED
    version.published_at = now
    organization.current_published_version_id = version.spec_version_id
    organization.name = spec.name
    organization.description = spec.description
    organization.updated_at = now
    session.commit()
    session.refresh(version)
    return version
