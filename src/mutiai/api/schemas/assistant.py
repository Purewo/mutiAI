"""Public platform-assistant conversation contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from mutiai.api.errors import DEFAULT_LOCALE, localize_error_message
from mutiai.api.schemas.organizations import as_utc
from mutiai.models import (
    AssistantAction,
    AssistantActionStatus,
    AssistantConversation,
    AssistantConversationStatus,
    AssistantEvent,
    AssistantMessage,
    AssistantMessageRole,
    AssistantMessageStatus,
    AssistantTurn,
    AssistantTurnStatus,
)


class AssistantConversationResponse(BaseModel):
    conversation_id: str
    status: AssistantConversationStatus
    runtime_provider: str
    runtime_thread_generation: int
    observed_compactions: int
    tool_contract_version: str
    system_prompt_version: str
    last_message_sequence: int
    last_event_sequence: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None

    @classmethod
    def from_record(
        cls, conversation: AssistantConversation
    ) -> AssistantConversationResponse:
        return cls(
            conversation_id=conversation.conversation_id,
            status=AssistantConversationStatus(conversation.status),
            runtime_provider=conversation.runtime_provider,
            runtime_thread_generation=conversation.runtime_thread_generation,
            observed_compactions=conversation.observed_compactions,
            tool_contract_version=conversation.tool_contract_version,
            system_prompt_version=conversation.system_prompt_version,
            last_message_sequence=conversation.last_message_sequence,
            last_event_sequence=conversation.last_event_sequence,
            created_at=as_utc(conversation.created_at),
            updated_at=as_utc(conversation.updated_at),
            archived_at=as_utc(conversation.archived_at),
        )


class AssistantMessageResponse(BaseModel):
    message_id: str
    conversation_id: str
    sequence: int
    role: AssistantMessageRole
    status: AssistantMessageStatus
    text: str
    content_blocks: list[dict]
    attachment_refs: list[dict]
    reply_to_message_id: str | None
    related_resource_type: str | None
    related_resource_id: str | None
    created_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_record(cls, message: AssistantMessage) -> AssistantMessageResponse:
        return cls(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            sequence=message.sequence,
            role=AssistantMessageRole(message.role),
            status=AssistantMessageStatus(message.status),
            text=message.text_content,
            content_blocks=list(message.content_blocks or []),
            attachment_refs=list(message.attachment_refs or []),
            reply_to_message_id=message.reply_to_message_id,
            related_resource_type=message.related_resource_type,
            related_resource_id=message.related_resource_id,
            created_at=as_utc(message.created_at),
            completed_at=as_utc(message.completed_at),
        )


class AssistantMessagePage(BaseModel):
    items: list[AssistantMessageResponse]
    next_cursor: str | None


class AssistantTurnResponse(BaseModel):
    turn_id: str
    conversation_id: str
    source_message_id: str
    status: AssistantTurnStatus
    runtime_provider: str
    runtime_thread_generation: int
    runtime_thread_id: str | None
    runtime_turn_id: str | None
    runtime_job_id: str | None
    requested_model: str | None
    actual_model: str | None
    reasoning_effort: str | None
    final_message_id: str | None
    failure_code: str | None
    failure_message: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None
    context_compactions: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_record(cls, turn: AssistantTurn) -> AssistantTurnResponse:
        return cls(
            turn_id=turn.turn_id,
            conversation_id=turn.conversation_id,
            source_message_id=turn.source_message_id,
            status=AssistantTurnStatus(turn.status),
            runtime_provider=turn.runtime_provider,
            runtime_thread_generation=turn.runtime_thread_generation,
            runtime_thread_id=turn.runtime_thread_id,
            runtime_turn_id=turn.runtime_turn_id,
            runtime_job_id=turn.runtime_job_id,
            requested_model=turn.requested_model,
            actual_model=turn.actual_model,
            reasoning_effort=turn.reasoning_effort,
            final_message_id=turn.final_message_id,
            failure_code=turn.failure_code,
            failure_message=turn.failure_message,
            input_tokens=turn.input_tokens,
            cached_input_tokens=turn.cached_input_tokens,
            output_tokens=turn.output_tokens,
            reasoning_output_tokens=turn.reasoning_output_tokens,
            total_tokens=turn.total_tokens,
            context_compactions=turn.context_compactions,
            created_at=as_utc(turn.created_at),
            started_at=as_utc(turn.started_at),
            completed_at=as_utc(turn.completed_at),
        )


class AssistantActionResponse(BaseModel):
    action_id: str
    conversation_id: str
    source_turn_id: str | None
    action_type: str
    target_type: str | None
    target_id: str | None
    payload: dict
    status: AssistantActionStatus
    result: dict | None
    error_code: str | None
    error_status_code: int | None
    error_details: Any | None
    error_message: str | None
    proposed_at: datetime
    confirmed_at: datetime | None
    executed_at: datetime | None

    @classmethod
    def from_record(
        cls,
        action: AssistantAction,
        *,
        locale: str = DEFAULT_LOCALE,
    ) -> AssistantActionResponse:
        error_message = action.error_message
        if action.error_code is not None:
            error_message = localize_error_message(
                code=action.error_code,
                fallback=(
                    action.error_message
                    or "The assistant action could not be completed."
                ),
                locale=locale,
                status_code=action.error_status_code or 409,
            )
        return cls(
            action_id=action.action_id,
            conversation_id=action.conversation_id,
            source_turn_id=action.source_turn_id,
            action_type=action.action_type,
            target_type=action.target_type,
            target_id=action.target_id,
            payload=dict(action.payload or {}),
            status=AssistantActionStatus(action.status),
            result=dict(action.result) if action.result is not None else None,
            error_code=action.error_code,
            error_status_code=action.error_status_code,
            error_details=action.error_details,
            error_message=error_message,
            proposed_at=as_utc(action.proposed_at),
            confirmed_at=as_utc(action.confirmed_at),
            executed_at=as_utc(action.executed_at),
        )


class AssistantEventResponse(BaseModel):
    event_id: str
    event_type: str
    schema_version: str
    aggregate_type: str
    aggregate_id: str
    conversation_id: str
    sequence: int
    occurred_at: datetime
    source: str
    correlation_id: str
    payload: dict

    @classmethod
    def from_record(cls, event: AssistantEvent) -> AssistantEventResponse:
        return cls(
            event_id=event.event_id,
            event_type=event.event_type,
            schema_version=event.schema_version,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            conversation_id=event.conversation_id,
            sequence=event.sequence,
            occurred_at=as_utc(event.occurred_at),
            source=event.source,
            correlation_id=event.correlation_id,
            payload=dict(event.payload or {}),
        )


class AssistantUserMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    attachment_refs: list[dict] = Field(default_factory=list, max_length=20)


class AssistantSubmissionResponse(BaseModel):
    message: AssistantMessageResponse
    turn: AssistantTurnResponse


class AssistantActionDecisionRequest(BaseModel):
    decision: Literal["confirm", "decline"]


class AssistantActionProposal(BaseModel):
    action_type: Literal[
        "organization.confirm",
        "organization.publish",
        "task.submit",
        "task.retry",
        "task.cancel",
        "approval.decide",
    ]
    target_type: str | None = Field(default=None, max_length=50)
    target_id: str | None = Field(default=None, max_length=100)
    payload: dict = Field(default_factory=dict)


class AssistantRuntimeEnvelope(BaseModel):
    reply: str = Field(min_length=1, max_length=20_000)
    action: AssistantActionProposal | None = None
