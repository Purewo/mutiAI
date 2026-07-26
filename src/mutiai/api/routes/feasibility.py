"""Owner-scoped Runtime feasibility evidence routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from mutiai.api.dependencies import CurrentUser, DbSession
from mutiai.api.errors import ApiError, ErrorEnvelope, resolve_locale
from mutiai.api.schemas.feasibility import FeasibilityCheckResponse
from mutiai.models import FeasibilityCheck
from mutiai.services.organizations import get_owned_version
from mutiai.services.tasks import get_owned_task

router = APIRouter(tags=["feasibility"])
ERROR_RESPONSES = {
    401: {"model": ErrorEnvelope},
    404: {"model": ErrorEnvelope},
}


def _response(check: FeasibilityCheck, request: Request) -> FeasibilityCheckResponse:
    return FeasibilityCheckResponse.from_record(
        check,
        locale=resolve_locale(request.headers.get("Accept-Language")),
    )


@router.get(
    "/feasibility-checks/{feasibility_check_id}",
    response_model=FeasibilityCheckResponse,
    responses=ERROR_RESPONSES,
)
def get_feasibility_check(
    feasibility_check_id: str,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> FeasibilityCheckResponse:
    check = session.scalar(
        select(FeasibilityCheck).where(
            FeasibilityCheck.feasibility_check_id == feasibility_check_id,
            FeasibilityCheck.owner_user_id == user.user_id,
        )
    )
    if check is None:
        raise ApiError(404, "FEASIBILITY_CHECK_NOT_FOUND", "Check not found.")
    return _response(check, request)


@router.get(
    "/organizations/{organization_id}/versions/{spec_version_id}/feasibility-checks",
    response_model=list[FeasibilityCheckResponse],
    responses=ERROR_RESPONSES,
)
def list_organization_feasibility_checks(
    organization_id: str,
    spec_version_id: str,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> list[FeasibilityCheckResponse]:
    get_owned_version(
        session,
        organization_id=organization_id,
        spec_version_id=spec_version_id,
        owner_user_id=user.user_id,
    )
    checks = session.scalars(
        select(FeasibilityCheck)
        .where(
            FeasibilityCheck.owner_user_id == user.user_id,
            FeasibilityCheck.target_type == "organization_version",
            FeasibilityCheck.target_id == spec_version_id,
        )
        .order_by(FeasibilityCheck.created_at, FeasibilityCheck.feasibility_check_id)
    ).all()
    return [_response(check, request) for check in checks]


@router.get(
    "/tasks/{task_id}/feasibility-checks",
    response_model=list[FeasibilityCheckResponse],
    responses=ERROR_RESPONSES,
)
def list_task_feasibility_checks(
    task_id: str,
    request: Request,
    user: CurrentUser,
    session: DbSession,
) -> list[FeasibilityCheckResponse]:
    get_owned_task(session, task_id=task_id, owner_user_id=user.user_id)
    checks = session.scalars(
        select(FeasibilityCheck)
        .where(
            FeasibilityCheck.owner_user_id == user.user_id,
            FeasibilityCheck.target_type == "task",
            FeasibilityCheck.target_id == task_id,
        )
        .order_by(FeasibilityCheck.created_at, FeasibilityCheck.feasibility_check_id)
    ).all()
    return [_response(check, request) for check in checks]
