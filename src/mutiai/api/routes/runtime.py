"""Runtime control and Provider capacity routes."""

from fastapi import APIRouter

from mutiai.api.dependencies import CurrentUser, DbSession, TaskRunner
from mutiai.api.schemas.runtime_controls import RuntimeControlResponse

router = APIRouter(tags=["runtime"])


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
