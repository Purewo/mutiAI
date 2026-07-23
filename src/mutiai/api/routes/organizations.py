"""Organization proposal, confirmation, and publication routes."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from mutiai.api.dependencies import CurrentUser, DbSession
from mutiai.api.errors import ErrorEnvelope
from mutiai.api.schemas.organizations import (
    OrganizationDetailResponse,
    OrganizationProposalRequest,
    OrganizationSummaryResponse,
    OrganizationVersionResponse,
)
from mutiai.domain import OrganizationSpec
from mutiai.models import Organization, OrganizationSpecVersion
from mutiai.services.organizations import (
    confirm_version,
    create_proposal,
    get_owned_organization,
    publish_version,
)


router = APIRouter(prefix="/organizations", tags=["organizations"])
ERROR_RESPONSES = {
    401: {"model": ErrorEnvelope},
    404: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope},
    422: {"model": ErrorEnvelope},
}


@router.post(
    "/proposals",
    response_model=OrganizationVersionResponse,
    status_code=201,
    responses=ERROR_RESPONSES,
)
def propose_organization(
    payload: OrganizationProposalRequest,
    user: CurrentUser,
    session: DbSession,
) -> OrganizationVersionResponse:
    version = create_proposal(
        session,
        owner_user_id=user.user_id,
        spec=payload.spec,
        organization_id=payload.organization_id,
        source_request=payload.source_request,
    )
    return OrganizationVersionResponse.from_record(version)


@router.get(
    "",
    response_model=list[OrganizationSummaryResponse],
    responses={401: {"model": ErrorEnvelope}},
)
def list_organizations(
    user: CurrentUser,
    session: DbSession,
) -> list[OrganizationSummaryResponse]:
    organizations = session.scalars(
        select(Organization)
        .where(Organization.owner_user_id == user.user_id)
        .order_by(Organization.created_at, Organization.organization_id)
    ).all()
    return [
        OrganizationSummaryResponse.from_record(organization)
        for organization in organizations
    ]


@router.get(
    "/{organization_id}",
    response_model=OrganizationDetailResponse,
    responses=ERROR_RESPONSES,
)
def get_organization(
    organization_id: str,
    user: CurrentUser,
    session: DbSession,
) -> OrganizationDetailResponse:
    organization = get_owned_organization(
        session,
        organization_id=organization_id,
        owner_user_id=user.user_id,
    )
    current_spec = None
    if organization.current_published_version_id:
        current_version = session.get(
            OrganizationSpecVersion,
            organization.current_published_version_id,
        )
        if current_version is not None:
            current_spec = OrganizationSpec.model_validate(current_version.spec_payload)

    summary = OrganizationSummaryResponse.from_record(organization)
    return OrganizationDetailResponse(
        **summary.model_dump(),
        current_published_spec=current_spec,
    )


@router.get(
    "/{organization_id}/versions",
    response_model=list[OrganizationVersionResponse],
    responses=ERROR_RESPONSES,
)
def list_organization_versions(
    organization_id: str,
    user: CurrentUser,
    session: DbSession,
) -> list[OrganizationVersionResponse]:
    get_owned_organization(
        session,
        organization_id=organization_id,
        owner_user_id=user.user_id,
    )
    versions = session.scalars(
        select(OrganizationSpecVersion)
        .where(
            OrganizationSpecVersion.organization_id == organization_id,
            OrganizationSpecVersion.owner_user_id == user.user_id,
        )
        .order_by(OrganizationSpecVersion.version_number)
    ).all()
    return [OrganizationVersionResponse.from_record(version) for version in versions]


@router.post(
    "/{organization_id}/versions/{spec_version_id}/confirm",
    response_model=OrganizationVersionResponse,
    responses=ERROR_RESPONSES,
)
def confirm_organization_version(
    organization_id: str,
    spec_version_id: str,
    user: CurrentUser,
    session: DbSession,
) -> OrganizationVersionResponse:
    version = confirm_version(
        session,
        organization_id=organization_id,
        spec_version_id=spec_version_id,
        owner_user_id=user.user_id,
    )
    return OrganizationVersionResponse.from_record(version)


@router.post(
    "/{organization_id}/versions/{spec_version_id}/publish",
    response_model=OrganizationVersionResponse,
    responses=ERROR_RESPONSES,
)
def publish_organization_version(
    organization_id: str,
    spec_version_id: str,
    user: CurrentUser,
    session: DbSession,
) -> OrganizationVersionResponse:
    version = publish_version(
        session,
        organization_id=organization_id,
        spec_version_id=spec_version_id,
        owner_user_id=user.user_id,
    )
    return OrganizationVersionResponse.from_record(version)
