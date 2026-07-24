"""Task submission, status, and resumable event routes."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import APIRouter, Header, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from mutiai.api.dependencies import (
    ApprovalManager,
    CurrentUser,
    DbSession,
    TaskRunner,
)
from mutiai.api.errors import ApiError, ErrorEnvelope
from mutiai.api.schemas.approvals import (
    ApprovalDecisionRequest,
    ApprovalResponse,
)
from mutiai.api.schemas.tasks import (
    ArtifactResponse,
    TaskCreateRequest,
    TaskEventResponse,
    TaskInputArtifactRequest,
    TaskResponse,
)
from mutiai.models import ApprovalRequest, ProductEvent, Task
from mutiai.models.task import TaskStatus
from mutiai.orchestration import TaskCancellationIncompleteError
from mutiai.services.artifacts import ArtifactError
from mutiai.services.runtime_bindings import RuntimeBindingResolutionError
from mutiai.services.runtime_controls import (
    RuntimeBudgetExceededError,
    RuntimeProviderRateLimitedError,
)
from mutiai.services.tasks import create_task, get_owned_task

router = APIRouter(tags=["tasks"])
ERROR_RESPONSES = {
    401: {"model": ErrorEnvelope},
    404: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope},
    429: {"model": ErrorEnvelope},
    422: {"model": ErrorEnvelope},
}


def load_task_response(session: DbSession, task_id: str) -> TaskResponse:
    session.expire_all()
    task = session.get(Task, task_id)
    if task is None:
        raise ApiError(404, "TASK_NOT_FOUND", "Task not found.")
    return TaskResponse.from_record(task)


def run_orchestration(operation: Callable[[], Task]) -> Task:
    try:
        return operation()
    except RuntimeProviderRateLimitedError as exc:
        raise ApiError(
            429,
            "PROVIDER_RATE_LIMITED",
            "The Runtime Provider is rate limited. Try again after it resets.",
            details={"provider": exc.provider, "resets_at": exc.resets_at},
        ) from exc
    except RuntimeBudgetExceededError as exc:
        raise ApiError(
            409,
            "RUNTIME_BUDGET_EXCEEDED",
            "The configured Runtime token budget cannot admit this execution.",
            details={
                "provider": exc.provider,
                "budget_limit": exc.limit,
                "tokens_consumed": exc.consumed,
                "tokens_reserved": exc.reserved,
                "requested_tokens": exc.requested,
            },
        ) from exc
    except RuntimeBindingResolutionError as exc:
        raise ApiError(409, "RUNTIME_BINDING_INVALID", str(exc)) from exc


@router.post(
    "/organizations/{organization_id}/tasks",
    response_model=TaskResponse,
    status_code=201,
    responses=ERROR_RESPONSES,
)
def submit_task(
    organization_id: str,
    payload: TaskCreateRequest,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    orchestrator: TaskRunner,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
        ),
    ],
) -> TaskResponse:
    task, created = create_task(
        session,
        owner_user_id=user.user_id,
        organization_id=organization_id,
        request_text=payload.request,
        idempotency_key=idempotency_key,
        orchestration_mode=payload.orchestration_mode,
    )
    if payload.orchestration_mode.value == "planned":
        run_orchestration(lambda: orchestrator.plan(task.task_id))
    else:
        run_orchestration(lambda: orchestrator.run(task.task_id))
    if not created:
        response.status_code = 200
    return load_task_response(session, task.task_id)


@router.post(
    "/tasks/{task_id}/plan",
    response_model=TaskResponse,
    responses=ERROR_RESPONSES,
)
def plan_task(
    task_id: str,
    user: CurrentUser,
    session: DbSession,
    orchestrator: TaskRunner,
) -> TaskResponse:
    get_owned_task(session, task_id=task_id, owner_user_id=user.user_id)
    try:
        run_orchestration(lambda: orchestrator.plan(task_id))
    except ValueError as exc:
        raise ApiError(409, "TASK_PLAN_NOT_ALLOWED", str(exc)) from exc
    return load_task_response(session, task_id)


@router.post(
    "/tasks/{task_id}/start",
    response_model=TaskResponse,
    responses=ERROR_RESPONSES,
)
def start_task(
    task_id: str,
    user: CurrentUser,
    session: DbSession,
    orchestrator: TaskRunner,
) -> TaskResponse:
    get_owned_task(session, task_id=task_id, owner_user_id=user.user_id)
    try:
        run_orchestration(lambda: orchestrator.start(task_id))
    except ValueError as exc:
        raise ApiError(409, "TASK_NOT_READY_TO_START", str(exc)) from exc
    return load_task_response(session, task_id)


@router.post(
    "/tasks/{task_id}/inputs",
    response_model=ArtifactResponse,
    status_code=201,
    responses=ERROR_RESPONSES,
)
def upload_task_input(
    task_id: str,
    payload: TaskInputArtifactRequest,
    user: CurrentUser,
    session: DbSession,
    orchestrator: TaskRunner,
) -> ArtifactResponse:
    get_owned_task(session, task_id=task_id, owner_user_id=user.user_id)
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ApiError(
            422,
            "TASK_INPUT_BASE64_INVALID",
            "The Task input content_base64 is invalid.",
        ) from exc
    try:
        artifact = orchestrator.publish_task_input(
            task_id=task_id,
            contract_key=payload.contract_key,
            schema_version=payload.schema_version,
            media_type=payload.media_type,
            file_name=payload.file_name,
            content=content,
            source_delivery_id=payload.source_delivery_id,
        )
    except ValueError as exc:
        raise ApiError(
            422,
            "TASK_INPUT_INVALID",
            str(exc),
        ) from exc
    except ArtifactError as exc:
        raise ApiError(409, exc.code, str(exc)) from exc
    return ArtifactResponse.from_record(artifact)


@router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    responses=ERROR_RESPONSES,
)
def get_task(
    task_id: str,
    user: CurrentUser,
    session: DbSession,
) -> TaskResponse:
    task = get_owned_task(
        session,
        task_id=task_id,
        owner_user_id=user.user_id,
    )
    return TaskResponse.from_record(task)


@router.post(
    "/tasks/{task_id}/retry",
    response_model=TaskResponse,
    responses=ERROR_RESPONSES,
)
def retry_failed_task(
    task_id: str,
    user: CurrentUser,
    session: DbSession,
    orchestrator: TaskRunner,
) -> TaskResponse:
    task = get_owned_task(
        session,
        task_id=task_id,
        owner_user_id=user.user_id,
    )
    if task.status != TaskStatus.FAILED:
        raise ApiError(
            409,
            "TASK_NOT_RETRYABLE",
            "Only a failed task can be retried.",
        )
    orchestrator.retry(task_id)
    return load_task_response(session, task_id)


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=TaskResponse,
    responses=ERROR_RESPONSES,
)
def cancel_task(
    task_id: str,
    user: CurrentUser,
    session: DbSession,
    orchestrator: TaskRunner,
) -> TaskResponse:
    get_owned_task(
        session,
        task_id=task_id,
        owner_user_id=user.user_id,
    )
    try:
        orchestrator.cancel(task_id)
    except ValueError as exc:
        raise ApiError(
            409,
            "TASK_NOT_CANCELLABLE",
            "Only a non-terminal task can be cancelled.",
        ) from exc
    except TaskCancellationIncompleteError as exc:
        raise ApiError(
            409,
            "TASK_CANCELLATION_INCOMPLETE",
            "The task workflow was cancelled, but one or more Runtime "
            "interruptions were not confirmed.",
            details={"failures": exc.failures},
        ) from exc
    return load_task_response(session, task_id)


@router.get(
    "/tasks/{task_id}/approvals",
    response_model=list[ApprovalResponse],
    responses=ERROR_RESPONSES,
)
def list_task_approvals(
    task_id: str,
    user: CurrentUser,
    session: DbSession,
) -> list[ApprovalResponse]:
    get_owned_task(
        session,
        task_id=task_id,
        owner_user_id=user.user_id,
    )
    approvals = session.scalars(
        select(ApprovalRequest)
        .where(ApprovalRequest.task_id == task_id)
        .order_by(ApprovalRequest.created_at, ApprovalRequest.approval_id)
    ).all()
    return [ApprovalResponse.from_record(approval) for approval in approvals]


@router.post(
    "/tasks/{task_id}/approvals/{approval_id}/decision",
    response_model=ApprovalResponse,
    responses=ERROR_RESPONSES,
)
def decide_task_approval(
    task_id: str,
    approval_id: str,
    payload: ApprovalDecisionRequest,
    user: CurrentUser,
    session: DbSession,
    approval_manager: ApprovalManager,
) -> ApprovalResponse:
    get_owned_task(
        session,
        task_id=task_id,
        owner_user_id=user.user_id,
    )
    try:
        approval = approval_manager.decide(
            approval_id=approval_id,
            task_id=task_id,
            owner_user_id=user.user_id,
            decision=payload.decision,
        )
    except LookupError as exc:
        raise ApiError(
            404,
            "APPROVAL_NOT_FOUND",
            "Approval request not found.",
        ) from exc
    except ValueError as exc:
        raise ApiError(
            409,
            "APPROVAL_ALREADY_RESOLVED",
            "The approval request was already resolved with another decision.",
        ) from exc
    except RuntimeError as exc:
        raise ApiError(
            409,
            "APPROVAL_NOT_ACTIVE",
            "The Runtime is no longer waiting for this approval.",
        ) from exc
    return ApprovalResponse.from_record(approval)


@router.get(
    "/tasks/{task_id}/events",
    responses=ERROR_RESPONSES,
)
def stream_task_events(
    task_id: str,
    user: CurrentUser,
    session: DbSession,
    last_event_id: Annotated[
        str | None,
        Header(alias="Last-Event-ID"),
    ] = None,
) -> StreamingResponse:
    get_owned_task(
        session,
        task_id=task_id,
        owner_user_id=user.user_id,
    )
    after_sequence = 0
    if last_event_id is not None:
        cursor = session.scalar(
            select(ProductEvent).where(
                ProductEvent.event_id == last_event_id,
                ProductEvent.task_id == task_id,
            )
        )
        if cursor is None:
            raise ApiError(
                409,
                "TASK_EVENT_CURSOR_INVALID",
                "The task event cursor is not available.",
            )
        after_sequence = cursor.sequence

    records = session.scalars(
        select(ProductEvent)
        .where(
            ProductEvent.task_id == task_id,
            ProductEvent.sequence > after_sequence,
        )
        .order_by(ProductEvent.sequence)
    ).all()
    events = [TaskEventResponse.from_record(record) for record in records]

    def generate() -> Iterator[str]:
        for event in events:
            data = json.dumps(
                event.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield (f"id: {event.event_id}\nevent: {event.event_type}\ndata: {data}\n\n")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
