"""Platform-assistant conversations, Turns, actions, and event stream."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from mutiai.api.dependencies import (
    CurrentUser,
    DbSession,
    PlatformAssistant,
)
from mutiai.api.errors import ApiError, ErrorEnvelope, resolve_locale
from mutiai.api.schemas.assistant import (
    AssistantActionDecisionRequest,
    AssistantActionResponse,
    AssistantConversationResponse,
    AssistantEventResponse,
    AssistantMessagePage,
    AssistantMessageResponse,
    AssistantSubmissionResponse,
    AssistantTurnResponse,
    AssistantUserMessageRequest,
)
from mutiai.models import AssistantAction, AssistantConversation

router = APIRouter(prefix="/assistant", tags=["assistant"])
ERROR_RESPONSES = {
    401: {"model": ErrorEnvelope},
    404: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope},
    422: {"model": ErrorEnvelope},
}


def _action_response(
    action: AssistantAction,
    *,
    request: Request,
    response: Response,
) -> AssistantActionResponse:
    locale = resolve_locale(request.headers.get("Accept-Language"))
    response.headers["Content-Language"] = locale
    response.headers["Vary"] = "Accept-Language"
    return AssistantActionResponse.from_record(action, locale=locale)


@router.post(
    "/conversations",
    response_model=AssistantConversationResponse,
    status_code=201,
    responses=ERROR_RESPONSES,
)
def create_conversation(
    user: CurrentUser,
    session: DbSession,
    assistant: PlatformAssistant,
) -> AssistantConversationResponse:
    conversation = assistant.create_conversation(session, owner_user_id=user.user_id)
    return AssistantConversationResponse.from_record(conversation)


@router.get(
    "/conversations",
    response_model=list[AssistantConversationResponse],
    responses={401: {"model": ErrorEnvelope}},
)
def list_conversations(
    user: CurrentUser,
    session: DbSession,
) -> list[AssistantConversationResponse]:
    conversations = session.scalars(
        select(AssistantConversation)
        .where(AssistantConversation.owner_user_id == user.user_id)
        .order_by(
            AssistantConversation.updated_at.desc(),
            AssistantConversation.conversation_id,
        )
    ).all()
    return [
        AssistantConversationResponse.from_record(conversation)
        for conversation in conversations
    ]


@router.get(
    "/conversations/{conversation_id}",
    response_model=AssistantConversationResponse,
    responses=ERROR_RESPONSES,
)
def get_conversation(
    conversation_id: str,
    user: CurrentUser,
    session: DbSession,
    assistant: PlatformAssistant,
) -> AssistantConversationResponse:
    conversation = assistant.get_conversation(
        session,
        conversation_id=conversation_id,
        owner_user_id=user.user_id,
    )
    return AssistantConversationResponse.from_record(conversation)


@router.post(
    "/conversations/{conversation_id}/archive",
    response_model=AssistantConversationResponse,
    responses=ERROR_RESPONSES,
)
def archive_conversation(
    conversation_id: str,
    user: CurrentUser,
    session: DbSession,
    assistant: PlatformAssistant,
) -> AssistantConversationResponse:
    conversation = assistant.archive_conversation(
        session,
        conversation_id=conversation_id,
        owner_user_id=user.user_id,
    )
    return AssistantConversationResponse.from_record(conversation)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=AssistantMessagePage,
    responses=ERROR_RESPONSES,
)
def list_messages(
    conversation_id: str,
    user: CurrentUser,
    session: DbSession,
    assistant: PlatformAssistant,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AssistantMessagePage:
    messages, next_cursor = assistant.list_messages(
        session,
        conversation_id=conversation_id,
        owner_user_id=user.user_id,
        cursor=cursor,
        limit=limit,
    )
    return AssistantMessagePage(
        items=[AssistantMessageResponse.from_record(message) for message in messages],
        next_cursor=next_cursor,
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=AssistantSubmissionResponse,
    status_code=202,
    responses=ERROR_RESPONSES,
)
def submit_message(
    conversation_id: str,
    payload: AssistantUserMessageRequest,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    assistant: PlatformAssistant,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ],
) -> AssistantSubmissionResponse:
    message, turn, created = assistant.submit_message(
        session,
        conversation_id=conversation_id,
        owner_user_id=user.user_id,
        text=payload.text,
        attachment_refs=payload.attachment_refs,
        idempotency_key=idempotency_key,
    )
    if not created:
        response.status_code = 200
    return AssistantSubmissionResponse(
        message=AssistantMessageResponse.from_record(message),
        turn=AssistantTurnResponse.from_record(turn),
    )


@router.get(
    "/turns/{turn_id}",
    response_model=AssistantTurnResponse,
    responses=ERROR_RESPONSES,
)
def get_turn(
    turn_id: str,
    user: CurrentUser,
    session: DbSession,
    assistant: PlatformAssistant,
) -> AssistantTurnResponse:
    return AssistantTurnResponse.from_record(
        assistant.get_turn(session, turn_id=turn_id, owner_user_id=user.user_id)
    )


@router.post(
    "/turns/{turn_id}/cancel",
    response_model=AssistantTurnResponse,
    responses=ERROR_RESPONSES,
)
def cancel_turn(
    turn_id: str,
    user: CurrentUser,
    session: DbSession,
    assistant: PlatformAssistant,
) -> AssistantTurnResponse:
    return AssistantTurnResponse.from_record(
        assistant.cancel_turn(session, turn_id=turn_id, owner_user_id=user.user_id)
    )


@router.get(
    "/conversations/{conversation_id}/actions",
    response_model=list[AssistantActionResponse],
    responses=ERROR_RESPONSES,
)
def list_actions(
    conversation_id: str,
    request: Request,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    assistant: PlatformAssistant,
) -> list[AssistantActionResponse]:
    actions = assistant.list_actions(
        session,
        conversation_id=conversation_id,
        owner_user_id=user.user_id,
    )
    locale = resolve_locale(request.headers.get("Accept-Language"))
    response.headers["Content-Language"] = locale
    response.headers["Vary"] = "Accept-Language"
    return [
        AssistantActionResponse.from_record(action, locale=locale)
        for action in actions
    ]


@router.get(
    "/actions/{action_id}",
    response_model=AssistantActionResponse,
    responses=ERROR_RESPONSES,
)
def get_action(
    action_id: str,
    request: Request,
    response: Response,
    user: CurrentUser,
    session: DbSession,
) -> AssistantActionResponse:
    action = session.scalar(
        select(AssistantAction).where(
            AssistantAction.action_id == action_id,
            AssistantAction.owner_user_id == user.user_id,
        )
    )
    if action is None:
        raise ApiError(404, "ASSISTANT_ACTION_NOT_FOUND", "Assistant action not found.")
    return _action_response(action, request=request, response=response)


@router.post(
    "/actions/{action_id}/decision",
    response_model=AssistantActionResponse,
    responses=ERROR_RESPONSES,
)
def decide_action(
    action_id: str,
    payload: AssistantActionDecisionRequest,
    request: Request,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    assistant: PlatformAssistant,
) -> AssistantActionResponse:
    action = assistant.decide_action(
        session,
        action_id=action_id,
        owner_user_id=user.user_id,
        decision=payload.decision,
    )
    return _action_response(action, request=request, response=response)


@router.get(
    "/conversations/{conversation_id}/events",
    responses=ERROR_RESPONSES,
)
def stream_events(
    conversation_id: str,
    user: CurrentUser,
    session: DbSession,
    assistant: PlatformAssistant,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    records = assistant.list_events(
        session,
        conversation_id=conversation_id,
        owner_user_id=user.user_id,
        last_event_id=last_event_id,
    )
    events = [AssistantEventResponse.from_record(record) for record in records]

    def generate() -> Iterator[str]:
        yield "retry: 3000\n\n"
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
