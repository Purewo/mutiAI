"""Runtime control and Provider capacity routes."""

from typing import Annotated

from fastapi import APIRouter, Path

from mutiai.api.dependencies import CurrentUser, DbSession, TaskRunner
from mutiai.api.errors import ApiError, ErrorEnvelope
from mutiai.api.schemas.runtime_bindings import (
    RuntimeBindingResponse,
    RuntimeBindingUpsertRequest,
)
from mutiai.api.schemas.runtime_controls import RuntimeControlResponse
from mutiai.services.runtime_bindings import (
    RuntimeBindingInput,
    RuntimeBindingResolutionError,
)

router = APIRouter(tags=["runtime"])
ERROR_RESPONSES = {
    401: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope},
    422: {"model": ErrorEnvelope},
}


@router.get(
    "/runtime/bindings",
    response_model=list[RuntimeBindingResponse],
    responses=ERROR_RESPONSES,
)
def list_runtime_bindings(
    user: CurrentUser,
    session: DbSession,
    orchestrator: TaskRunner,
) -> list[RuntimeBindingResponse]:
    service = orchestrator.runtime_bindings
    service.ensure_default(session, owner_user_id=user.user_id)
    session.commit()
    responses = []
    for binding in service.list_for_owner(
        session,
        owner_user_id=user.user_id,
    ):
        profile = service.ensure_profile(
            session,
            binding=binding,
            owner_user_id=user.user_id,
        )
        responses.append(RuntimeBindingResponse.from_record(binding, profile))
    session.commit()
    return responses


@router.put(
    "/runtime/bindings/{binding_key}",
    response_model=RuntimeBindingResponse,
    responses=ERROR_RESPONSES,
)
def put_runtime_binding(
    binding_key: Annotated[str, Path(min_length=1, max_length=64)],
    payload: RuntimeBindingUpsertRequest,
    user: CurrentUser,
    session: DbSession,
    orchestrator: TaskRunner,
) -> RuntimeBindingResponse:
    active_provider = orchestrator.runtime_adapter.provider
    if payload.provider != active_provider:
        raise ApiError(
            409,
            "RUNTIME_PROVIDER_MISMATCH",
            f"The active Runtime Provider is '{active_provider}'.",
        )
    try:
        binding = orchestrator.runtime_bindings.upsert(
            session,
            owner_user_id=user.user_id,
            data=RuntimeBindingInput(
                binding_key=binding_key,
                provider=payload.provider,
                model=payload.model,
                reasoning_effort=payload.reasoning_effort,
                security_mode=payload.security_mode,
                capability_profile=payload.capability_profile,
            ),
        )
    except RuntimeBindingResolutionError as exc:
        raise ApiError(409, "RUNTIME_SECURITY_MODE_INVALID", str(exc)) from exc
    profile = orchestrator.runtime_bindings.ensure_profile(
        session,
        binding=binding,
        owner_user_id=user.user_id,
    )
    session.commit()
    return RuntimeBindingResponse.from_record(binding, profile)


@router.get("/runtime/controls", response_model=RuntimeControlResponse)
def get_runtime_controls(
    user: CurrentUser,
    session: DbSession,
    orchestrator: TaskRunner,
) -> RuntimeControlResponse:
    snapshot = orchestrator.runtime_controls.snapshot(
        session,
        owner_user_id=user.user_id,
        provider=orchestrator.runtime_adapter.provider,
    )
    return RuntimeControlResponse.from_snapshot(snapshot)
