"""Platform-assistant conversation service and Codex tool bridge."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, RLock
from typing import Any, get_args

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from mutiai.api.errors import ApiError
from mutiai.api.schemas.assistant import AssistantActionResponse
from mutiai.api.schemas.assistant_content import (
    AttachmentContentBlock,
    ContentPresentationRequest,
    DiagramContentBlock,
    DiagramPresentationRequest,
    MarkdownContentBlock,
    OrganizationDiagramSource,
    ResourceRefContentBlock,
    ResourceRefPresentationRequest,
    ResourceType,
    TaskPlanDiagramSource,
)
from mutiai.api.schemas.feasibility import FeasibilityCheckResponse
from mutiai.api.schemas.organizations import (
    OrganizationSummaryResponse,
    OrganizationVersionResponse,
)
from mutiai.api.schemas.tasks import (
    TaskCreateRequest,
    TaskResponse,
    TaskTokenUsageResponse,
)
from mutiai.config import Settings
from mutiai.domain import OrganizationSpec, WorkloadRequirements
from mutiai.models import (
    Artifact,
    Assignment,
    AssistantAction,
    AssistantActionStatus,
    AssistantAttachment,
    AssistantConversation,
    AssistantConversationStatus,
    AssistantEvent,
    AssistantMessage,
    AssistantMessageRole,
    AssistantMessageStatus,
    AssistantTurn,
    AssistantTurnStatus,
    FeasibilityCheck,
    Organization,
    OrganizationSpecVersion,
    RuntimeBinding,
    RuntimeExecution,
    Task,
    TaskExecutionPlan,
)
from mutiai.models.approval import ApprovalDecision
from mutiai.models.base import new_id, utc_now
from mutiai.models.task import TaskOrchestrationMode, TaskStatus
from mutiai.orchestration import TaskOrchestrator
from mutiai.runtime import (
    AgentRuntimeAdapter,
    RuntimeExecutionConfig,
    RuntimeResult,
    RuntimeTokenUsage,
    WorkspaceManager,
)
from mutiai.services.approvals import RuntimeApprovalCoordinator
from mutiai.services.artifacts import ArtifactError, ArtifactManager
from mutiai.services.assistant_attachments import (
    AssistantAttachmentError,
    AssistantAttachmentManager,
)
from mutiai.services.organizations import create_proposal, get_owned_organization
from mutiai.services.tasks import create_task, get_owned_task

SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "platform-assistant"
    / "references"
    / "system-prompt.md"
)
SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
SYSTEM_PROMPT_VERSION = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
ASSISTANT_ARTIFACT_CONTENT_MAX_BYTES = 64 * 1024
logger = logging.getLogger(__name__)
CONTENT_PRESENTATION_ADAPTER = TypeAdapter(ContentPresentationRequest)

# Codex strict response formats reject ``oneOf``. Keep the Runtime wire shape
# flat and nullable, then rebuild the product-owned discriminated union before
# validating or resolving any resource.
CONTENT_PRESENTATION_OUTPUT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["resource_ref", "diagram"]},
        "resource_type": {
            "type": ["string", "null"],
            "enum": [*get_args(ResourceType), None],
        },
        "resource_id": {"type": ["string", "null"]},
        "label": {"type": ["string", "null"]},
        "template": {
            "type": ["string", "null"],
            "enum": ["organization_chart", "execution_plan", None],
        },
        "source_kind": {
            "type": ["string", "null"],
            "enum": ["organization_spec_version", "task_plan", None],
        },
        "organization_id": {"type": ["string", "null"]},
        "spec_version_id": {"type": ["string", "null"]},
        "task_id": {"type": ["string", "null"]},
        "plan_id": {"type": ["string", "null"]},
        "text": {"type": ["string", "null"]},
    },
}

ASSISTANT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string"},
        "action": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "action_type": {
                    "type": "string",
                    "enum": [
                        "organization.confirm",
                        "organization.publish",
                        "task.submit",
                        "task.retry",
                        "task.cancel",
                        "approval.decide",
                    ],
                },
                "target_type": {"type": ["string", "null"]},
                "target_id": {"type": ["string", "null"]},
                "payload_json": {"type": "string"},
            },
            "required": [
                "action_type",
                "target_type",
                "target_id",
                "payload_json",
            ],
        },
        "presentation_requests": {
            "type": "array",
            "maxItems": 20,
            "items": CONTENT_PRESENTATION_OUTPUT_ITEM_SCHEMA,
        },
    },
    "required": ["reply", "action", "presentation_requests"],
}

ASSISTANT_THREAD_CONFIG: dict[str, Any] = {
    "features": {
        "apps": False,
        "multi_agent": False,
        "shell_tool": False,
        "unified_exec": False,
        "remote_plugin": False,
        "web_search": False,
    },
    "agents": {"enabled": False},
    "history": {"persistence": "save-all"},
}

ASSISTANT_ACTION_TYPES = frozenset(
    {
        "organization.confirm",
        "organization.publish",
        "task.submit",
        "task.retry",
        "task.cancel",
        "approval.decide",
    }
)


def _tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "inputSchema": schema,
    }


ASSISTANT_DYNAMIC_TOOLS: list[dict[str, Any]] = [
    _tool(
        "mutiai_list_organizations",
        "Read the user's organizations and their published version identities.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    _tool(
        "mutiai_get_organization",
        "Read one owner-scoped organization, including its published OrganizationSpec.",
        {
            "type": "object",
            "properties": {"organization_id": {"type": "string"}},
            "required": ["organization_id"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "mutiai_propose_organization",
        "Create or revise a preview OrganizationSpec. This never confirms or publishes it.",
        {
            "type": "object",
            "properties": {
                "organization_id": {"type": ["string", "null"]},
                "source_request": {"type": "string"},
                "spec": OrganizationSpec.model_json_schema(),
            },
            "required": ["organization_id", "source_request", "spec"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "mutiai_propose_action",
        "Create a pending product action. The user must confirm it through the product API.",
        {
            "type": "object",
            "properties": {
                "action_type": {
                    "type": "string",
                    "enum": [
                        "organization.confirm",
                        "organization.publish",
                        "task.submit",
                        "task.retry",
                        "task.cancel",
                        "approval.decide",
                    ],
                },
                "target_type": {"type": ["string", "null"]},
                "target_id": {"type": ["string", "null"]},
                "payload": {"type": "object"},
            },
            "required": ["action_type", "target_type", "target_id", "payload"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "mutiai_list_actions",
        "Read recent product actions from the current assistant conversation.",
        {
            "type": "object",
            "properties": {
                "status": {
                    "type": ["string", "null"],
                    "enum": [
                        "proposed",
                        "confirmed",
                        "executing",
                        "completed",
                        "failed",
                        "declined",
                        "cancelled",
                        "expired",
                        "superseded",
                        None,
                    ],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["status", "limit"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "mutiai_get_action",
        "Read one product action from the current assistant conversation.",
        {
            "type": "object",
            "properties": {"action_id": {"type": "string"}},
            "required": ["action_id"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "mutiai_check_task_feasibility",
        "Run and persist a Task feasibility preview without creating a Task or action.",
        {
            "type": "object",
            "properties": {
                "organization_id": {"type": "string"},
                "request": {"type": "string", "minLength": 1, "maxLength": 10000},
                "capability_requirements": WorkloadRequirements.model_json_schema(),
            },
            "required": [
                "organization_id",
                "request",
                "capability_requirements",
            ],
            "additionalProperties": False,
        },
    ),
    _tool(
        "mutiai_list_tasks",
        "Read the user's recent product Tasks and their persisted statuses.",
        {
            "type": "object",
            "properties": {"organization_id": {"type": ["string", "null"]}},
            "required": ["organization_id"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "mutiai_get_task",
        "Read one owner-scoped Task, including assignments, plan, and Artifact metadata.",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "mutiai_get_artifact_content",
        "Read one small released UTF-8 JSON or text Artifact through product access controls.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "artifact_id": {"type": "string"},
            },
            "required": ["task_id", "artifact_id"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "mutiai_get_attachment_content",
        "Read one owner-scoped UTF-8 JSON or text attachment already attached to a user message.",
        {
            "type": "object",
            "properties": {"attachment_id": {"type": "string"}},
            "required": ["attachment_id"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "mutiai_get_task_usage",
        "Read persisted token usage for one owner-scoped Task.",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "mutiai_get_feasibility_check",
        "Read one owner-scoped Runtime feasibility check and its findings.",
        {
            "type": "object",
            "properties": {"feasibility_check_id": {"type": "string"}},
            "required": ["feasibility_check_id"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "mutiai_list_version_feasibility_checks",
        "Read Runtime feasibility checks for one owner-scoped OrganizationSpec version.",
        {
            "type": "object",
            "properties": {
                "organization_id": {"type": "string"},
                "spec_version_id": {"type": "string"},
            },
            "required": ["organization_id", "spec_version_id"],
            "additionalProperties": False,
        },
    ),
]


class PlatformAssistantService:
    """Own assistant persistence while delegating reasoning to one Runtime Thread."""

    def __init__(
        self,
        database,
        settings: Settings,
        runtime_adapter: AgentRuntimeAdapter,
        workspace_manager: WorkspaceManager,
        orchestrator: TaskOrchestrator,
        approval_coordinator: RuntimeApprovalCoordinator | None = None,
        *,
        mutation_lock: RLock | None = None,
        attachment_manager: AssistantAttachmentManager | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.runtime_adapter = runtime_adapter
        self.workspace_manager = workspace_manager
        self.orchestrator = orchestrator
        self.approval_coordinator = approval_coordinator
        self.attachment_manager = attachment_manager
        self._lock = mutation_lock or RLock()
        self._workers = ThreadPoolExecutor(
            max_workers=max(1, settings.runtime_max_concurrent_executions),
            thread_name_prefix="mutiai-platform-assistant",
        )
        self._closing = Event()

    def close(self) -> None:
        """Stop workers without turning an in-flight external Turn into a failure.

        Codex continues the durable Turn in its own App Server. Closing the local
        connection unblocks waiters, while the ``closing`` flag keeps those
        waiters from overwriting the product record. A later process can recover
        the submitted/running/waiting Turn from its persisted identities.
        """

        if self._closing.is_set():
            return
        self._closing.set()
        try:
            with self.database.session() as session:
                execution_ids = session.scalars(
                    select(AssistantTurn.execution_id).where(
                        AssistantTurn.status.in_(
                            [
                                AssistantTurnStatus.SUBMITTED,
                                AssistantTurnStatus.RUNNING,
                                AssistantTurnStatus.WAITING,
                            ]
                        )
                    )
                ).all()
        except SQLAlchemyError:
            # Startup can fail before migrations create the assistant tables.
            execution_ids = []
        for execution_id in execution_ids:
            self._close_runtime_execution(execution_id)
        self._workers.shutdown(wait=True, cancel_futures=True)

    def create_conversation(
        self, session: Session, *, owner_user_id: str
    ) -> AssistantConversation:
        with self._lock:
            conversation = AssistantConversation(
                owner_user_id=owner_user_id,
                runtime_provider=self.runtime_adapter.provider,
                system_prompt_version=SYSTEM_PROMPT_VERSION,
                tool_contract_version=self.settings.assistant_tool_contract_version,
            )
            session.add(conversation)
            session.flush()
            self._append_event(
                session,
                conversation,
                event_type="assistant.conversation.created",
                aggregate_type="assistant_conversation",
                aggregate_id=conversation.conversation_id,
                payload={"conversation_id": conversation.conversation_id},
                correlation_id=conversation.conversation_id,
            )
            session.commit()
            session.refresh(conversation)
            return conversation

    def archive_conversation(
        self, session: Session, *, conversation_id: str, owner_user_id: str
    ) -> AssistantConversation:
        conversation = self.get_conversation(
            session, conversation_id=conversation_id, owner_user_id=owner_user_id
        )
        if conversation.status == AssistantConversationStatus.ARCHIVED:
            return conversation
        conversation.status = AssistantConversationStatus.ARCHIVED
        conversation.archived_at = utc_now()
        self._append_event(
            session,
            conversation,
            event_type="assistant.conversation.archived",
            aggregate_type="assistant_conversation",
            aggregate_id=conversation.conversation_id,
            payload={"conversation_id": conversation.conversation_id},
            correlation_id=conversation.conversation_id,
        )
        session.commit()
        session.refresh(conversation)
        return conversation

    def get_conversation(
        self, session: Session, *, conversation_id: str, owner_user_id: str
    ) -> AssistantConversation:
        conversation = session.scalar(
            select(AssistantConversation).where(
                AssistantConversation.conversation_id == conversation_id,
                AssistantConversation.owner_user_id == owner_user_id,
            )
        )
        if conversation is None:
            raise ApiError(
                404, "ASSISTANT_CONVERSATION_NOT_FOUND", "Conversation not found."
            )
        return conversation

    def list_messages(
        self,
        session: Session,
        *,
        conversation_id: str,
        owner_user_id: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[AssistantMessage], str | None]:
        conversation = self.get_conversation(
            session, conversation_id=conversation_id, owner_user_id=owner_user_id
        )
        query = select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation.conversation_id
        )
        if cursor is not None:
            anchor = session.scalar(
                select(AssistantMessage).where(
                    AssistantMessage.message_id == cursor,
                    AssistantMessage.conversation_id == conversation.conversation_id,
                )
            )
            if anchor is None:
                raise ApiError(
                    409,
                    "ASSISTANT_MESSAGE_CURSOR_INVALID",
                    "Message cursor is not available.",
                )
            query = query.where(AssistantMessage.sequence > anchor.sequence)
        rows = session.scalars(
            query.order_by(AssistantMessage.sequence).limit(limit + 1)
        ).all()
        next_cursor = rows[limit - 1].message_id if len(rows) > limit else None
        return rows[:limit], next_cursor

    def list_events(
        self,
        session: Session,
        *,
        conversation_id: str,
        owner_user_id: str,
        last_event_id: str | None,
    ) -> list[AssistantEvent]:
        conversation = self.get_conversation(
            session, conversation_id=conversation_id, owner_user_id=owner_user_id
        )
        after = 0
        if last_event_id is not None:
            cursor = session.scalar(
                select(AssistantEvent).where(
                    AssistantEvent.event_id == last_event_id,
                    AssistantEvent.conversation_id == conversation.conversation_id,
                )
            )
            if cursor is None:
                raise ApiError(
                    409,
                    "ASSISTANT_EVENT_CURSOR_INVALID",
                    "The assistant event cursor is not available.",
                )
            after = cursor.sequence
        return session.scalars(
            select(AssistantEvent)
            .where(
                AssistantEvent.conversation_id == conversation.conversation_id,
                AssistantEvent.sequence > after,
            )
            .order_by(AssistantEvent.sequence)
        ).all()

    def upload_attachment(
        self,
        session: Session,
        *,
        conversation_id: str,
        owner_user_id: str,
        file_name: str | None,
        media_type: str | None,
        source: Any,
    ) -> AssistantAttachment:
        self.get_conversation(
            session,
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
        )
        if self.attachment_manager is None:
            raise ApiError(
                500,
                "ASSISTANT_ATTACHMENT_STORAGE_FAILED",
                "The assistant attachment service is unavailable.",
            )
        try:
            attachment = self.attachment_manager.create(
                session,
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                file_name=file_name,
                media_type=media_type,
                source=source,
            )
        except AssistantAttachmentError as exc:
            raise ApiError(exc.status_code, exc.code, str(exc)) from exc
        session.commit()
        session.refresh(attachment)
        return attachment

    def revoke_attachment(
        self,
        session: Session,
        *,
        conversation_id: str,
        owner_user_id: str,
        attachment_id: str,
    ) -> AssistantAttachment:
        self.get_conversation(
            session,
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
        )
        if self.attachment_manager is None:
            raise ApiError(
                500,
                "ASSISTANT_ATTACHMENT_STORAGE_FAILED",
                "The assistant attachment service is unavailable.",
            )
        try:
            attachment = self.attachment_manager.revoke(
                session,
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                attachment_id=attachment_id,
            )
        except AssistantAttachmentError as exc:
            raise ApiError(exc.status_code, exc.code, str(exc)) from exc
        session.commit()
        session.refresh(attachment)
        return attachment

    def get_attachment(
        self,
        session: Session,
        *,
        conversation_id: str,
        owner_user_id: str,
        attachment_id: str,
    ) -> AssistantAttachment:
        self.get_conversation(
            session,
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
        )
        attachment = session.scalar(
            select(AssistantAttachment).where(
                AssistantAttachment.attachment_id == attachment_id,
                AssistantAttachment.conversation_id == conversation_id,
                AssistantAttachment.owner_user_id == owner_user_id,
            )
        )
        if attachment is None:
            raise ApiError(404, "ASSISTANT_ATTACHMENT_NOT_FOUND", "Assistant attachment not found.")
        return attachment

    def attachment_path(
        self,
        session: Session,
        *,
        conversation_id: str,
        owner_user_id: str,
        attachment_id: str,
    ) -> tuple[AssistantAttachment, Path]:
        attachment = self.get_attachment(
            session,
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            attachment_id=attachment_id,
        )
        if attachment.status == "revoked" or self.attachment_manager is None:
            raise ApiError(
                409,
                "ASSISTANT_ATTACHMENT_NOT_AVAILABLE",
                "The assistant attachment is no longer available.",
            )
        path = self.attachment_manager.path_for(attachment)
        if not path.is_file():
            raise ApiError(
                409,
                "ASSISTANT_ATTACHMENT_STORAGE_FAILED",
                "The assistant attachment file is unavailable.",
            )
        return attachment, path

    def submit_message(
        self,
        session: Session,
        *,
        conversation_id: str,
        owner_user_id: str,
        text: str,
        attachment_refs: list[dict],
        idempotency_key: str,
    ) -> tuple[AssistantMessage, AssistantTurn, bool]:
        with self._lock:
            conversation = self.get_conversation(
                session, conversation_id=conversation_id, owner_user_id=owner_user_id
            )
            existing = session.scalar(
                select(AssistantTurn).where(
                    AssistantTurn.conversation_id == conversation_id,
                    AssistantTurn.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                message = session.get(AssistantMessage, existing.source_message_id)
                if message is None:
                    raise ApiError(
                        409,
                        "ASSISTANT_IDEMPOTENCY_RECORD_INVALID",
                        "The assistant idempotency record is incomplete.",
                    )
                return message, existing, False
            if conversation.status != AssistantConversationStatus.ACTIVE:
                raise ApiError(
                    409,
                    "ASSISTANT_CONVERSATION_ARCHIVED",
                    "The assistant conversation is archived.",
                )
            in_flight = session.scalar(
                select(AssistantTurn).where(
                    AssistantTurn.conversation_id == conversation_id,
                    AssistantTurn.status.in_(
                        [
                            AssistantTurnStatus.QUEUED,
                            AssistantTurnStatus.SUBMITTED,
                            AssistantTurnStatus.RUNNING,
                            AssistantTurnStatus.WAITING,
                        ]
                    ),
                )
            )
            if in_flight is not None:
                raise ApiError(
                    409,
                    "ASSISTANT_TURN_IN_PROGRESS",
                    "The assistant is still processing the previous message.",
                )
            attachments: list[AssistantAttachment] = []
            if attachment_refs:
                if self.attachment_manager is None:
                    raise ApiError(
                        500,
                        "ASSISTANT_ATTACHMENT_STORAGE_FAILED",
                        "The assistant attachment service is unavailable.",
                    )
                try:
                    attachments = self.attachment_manager.validate_refs(
                        session,
                        conversation_id=conversation_id,
                        owner_user_id=owner_user_id,
                        refs=attachment_refs,
                    )
                except AssistantAttachmentError as exc:
                    raise ApiError(exc.status_code, exc.code, str(exc)) from exc
            normalized_refs = [
                {
                    "attachment_id": attachment.attachment_id,
                    "file_name": attachment.file_name,
                    "media_type": attachment.media_type,
                    "byte_size": attachment.byte_size,
                    "sha256": attachment.sha256,
                }
                for attachment in attachments
            ]
            content_blocks = [{"type": "text", "text": text}]
            content_blocks.extend(
                AttachmentContentBlock(
                    attachment_id=attachment.attachment_id,
                    file_name=attachment.file_name,
                    media_type=attachment.media_type,
                    byte_size=attachment.byte_size,
                    sha256=attachment.sha256,
                    text=f"Attachment: {attachment.file_name}",
                ).model_dump(mode="json")
                for attachment in attachments
            )
            message = self._new_message(
                session,
                conversation,
                owner_user_id=owner_user_id,
                role=AssistantMessageRole.USER,
                status=AssistantMessageStatus.ACCEPTED,
                text=text,
                content_blocks=content_blocks,
                attachment_refs=normalized_refs,
                idempotency_key=idempotency_key,
            )
            if self.attachment_manager is not None:
                self.attachment_manager.attach_to_message(
                    session,
                    message_id=message.message_id,
                    attachments=attachments,
                )
            turn = AssistantTurn(
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                source_message_id=message.message_id,
                execution_id=new_id(),
                idempotency_key=idempotency_key,
                runtime_provider=self.runtime_adapter.provider,
                runtime_thread_generation=conversation.runtime_thread_generation,
                status=AssistantTurnStatus.QUEUED,
                requested_model=self.settings.assistant_model
                or self.settings.codex_model,
                reasoning_effort=self.settings.assistant_reasoning_effort
                or self.settings.codex_reasoning_effort,
            )
            session.add(turn)
            session.flush()
            self._append_event(
                session,
                conversation,
                event_type="assistant.message.accepted",
                aggregate_type="assistant_message",
                aggregate_id=message.message_id,
                payload={
                    "message_id": message.message_id,
                    "sequence": message.sequence,
                },
                correlation_id=turn.execution_id,
            )
            self._append_event(
                session,
                conversation,
                event_type="assistant.turn.queued",
                aggregate_type="assistant_turn",
                aggregate_id=turn.turn_id,
                payload={"turn_id": turn.turn_id, "message_id": message.message_id},
                correlation_id=turn.execution_id,
            )
            session.commit()
            session.refresh(message)
            session.refresh(turn)
            self._workers.submit(
                self._run_turn,
                turn.turn_id,
                conversation_id,
                owner_user_id,
                text,
            )
            return message, turn, True

    def get_turn(
        self, session: Session, *, turn_id: str, owner_user_id: str
    ) -> AssistantTurn:
        turn = session.scalar(
            select(AssistantTurn).where(
                AssistantTurn.turn_id == turn_id,
                AssistantTurn.owner_user_id == owner_user_id,
            )
        )
        if turn is None:
            raise ApiError(404, "ASSISTANT_TURN_NOT_FOUND", "Assistant Turn not found.")
        return turn

    def list_actions(
        self, session: Session, *, conversation_id: str, owner_user_id: str
    ) -> list[AssistantAction]:
        self.get_conversation(
            session, conversation_id=conversation_id, owner_user_id=owner_user_id
        )
        return session.scalars(
            select(AssistantAction)
            .where(
                AssistantAction.conversation_id == conversation_id,
                AssistantAction.owner_user_id == owner_user_id,
            )
            .order_by(AssistantAction.proposed_at, AssistantAction.action_id)
        ).all()

    def decide_action(
        self,
        session: Session,
        *,
        action_id: str,
        owner_user_id: str,
        decision: str,
    ) -> AssistantAction:
        with self._lock:
            action = session.scalar(
                select(AssistantAction).where(
                    AssistantAction.action_id == action_id,
                    AssistantAction.owner_user_id == owner_user_id,
                )
            )
            if action is None:
                raise ApiError(
                    404, "ASSISTANT_ACTION_NOT_FOUND", "Assistant action not found."
                )
            if action.status in {
                AssistantActionStatus.COMPLETED,
                AssistantActionStatus.DECLINED,
                AssistantActionStatus.FAILED,
                AssistantActionStatus.CANCELLED,
                AssistantActionStatus.SUPERSEDED,
            }:
                return action
            if decision == "decline":
                if action.status != AssistantActionStatus.PROPOSED:
                    raise ApiError(
                        409,
                        "ASSISTANT_ACTION_STATE_CONFLICT",
                        "Only a proposed action can be declined.",
                    )
                action.status = AssistantActionStatus.DECLINED
                action.confirmed_at = utc_now()
                self._append_event(
                    session,
                    action.conversation,
                    event_type="assistant.action.declined",
                    aggregate_type="assistant_action",
                    aggregate_id=action.action_id,
                    payload={"action_id": action.action_id},
                    correlation_id=action.action_id,
                )
                session.commit()
                session.refresh(action)
                return action
            if action.status not in {
                AssistantActionStatus.PROPOSED,
                AssistantActionStatus.CONFIRMED,
                AssistantActionStatus.EXECUTING,
            }:
                raise ApiError(
                    409,
                    "ASSISTANT_ACTION_STATE_CONFLICT",
                    "Only a pending assistant action can be confirmed.",
                )
            if action.status == AssistantActionStatus.PROPOSED:
                action.status = AssistantActionStatus.CONFIRMED
                action.confirmed_at = utc_now()
                self._append_event(
                    session,
                    action.conversation,
                    event_type="assistant.action.confirmed",
                    aggregate_type="assistant_action",
                    aggregate_id=action.action_id,
                    payload={"action_id": action.action_id},
                    correlation_id=action.action_id,
                )
                session.commit()
            self._workers.submit(
                self._execute_action_worker,
                action.action_id,
                action.owner_user_id,
            )
            session.refresh(action)
            return action

    def recover_incomplete_actions(self) -> None:
        """Resume confirmed/executing actions after an API process restart."""

        with self.database.session() as session:
            action_ids = session.execute(
                select(AssistantAction.action_id, AssistantAction.owner_user_id).where(
                    AssistantAction.status.in_(
                        [
                            AssistantActionStatus.CONFIRMED,
                            AssistantActionStatus.EXECUTING,
                        ]
                    )
                )
            ).all()
        for action_id, owner_user_id in action_ids:
            self._workers.submit(
                self._execute_action_worker,
                action_id,
                owner_user_id,
            )

    def _execute_action_worker(self, action_id: str, owner_user_id: str) -> None:
        with self._lock, self.database.session() as session:
            action = session.scalar(
                select(AssistantAction).where(
                    AssistantAction.action_id == action_id,
                    AssistantAction.owner_user_id == owner_user_id,
                )
            )
            if action is None or action.status in {
                AssistantActionStatus.COMPLETED,
                AssistantActionStatus.DECLINED,
                AssistantActionStatus.FAILED,
                AssistantActionStatus.CANCELLED,
                AssistantActionStatus.SUPERSEDED,
            }:
                return
            conversation = session.get(AssistantConversation, action.conversation_id)
            if conversation is None:
                return
            try:
                action.status = AssistantActionStatus.EXECUTING
                session.commit()
                result = self._execute_action(session, action)
                action.result = result
                action.status = AssistantActionStatus.COMPLETED
                action.executed_at = utc_now()
                self._append_event(
                    session,
                    conversation,
                    event_type="assistant.action.completed",
                    aggregate_type="assistant_action",
                    aggregate_id=action.action_id,
                    payload={"action_id": action.action_id, "result": result},
                    correlation_id=action.action_id,
                )
            except ApiError as exc:
                action.status = AssistantActionStatus.FAILED
                action.error_code = exc.code
                action.error_status_code = exc.status_code
                action.error_details = exc.details
                action.error_message = exc.message
                self._append_event(
                    session,
                    conversation,
                    event_type="assistant.action.failed",
                    aggregate_type="assistant_action",
                    aggregate_id=action.action_id,
                    payload={"action_id": action.action_id, "code": exc.code},
                    correlation_id=action.action_id,
                )
            except SQLAlchemyError:
                # A failed flush/commit leaves the Session unusable until it is
                # rolled back. Preserve the action failure record instead of
                # masking the original error with PendingRollbackError.
                session.rollback()
                action = session.get(AssistantAction, action_id)
                conversation = session.get(
                    AssistantConversation,
                    action.conversation_id if action is not None else "",
                )
                if action is None or conversation is None:
                    return
                action.status = AssistantActionStatus.FAILED
                action.error_code = "ASSISTANT_ACTION_DATABASE_FAILED"
                action.error_status_code = 500
                action.error_details = None
                action.error_message = "The assistant action could not be persisted."
                self._append_event(
                    session,
                    conversation,
                    event_type="assistant.action.failed",
                    aggregate_type="assistant_action",
                    aggregate_id=action.action_id,
                    payload={
                        "action_id": action.action_id,
                        "code": action.error_code,
                    },
                    correlation_id=action.action_id,
                )
            except Exception as exc:
                logger.exception(
                    "Unhandled platform-assistant Action failure",
                    exc_info=exc,
                )
                action.status = AssistantActionStatus.FAILED
                action.error_code = "ASSISTANT_ACTION_EXECUTION_FAILED"
                action.error_status_code = 500
                action.error_details = None
                action.error_message = "The assistant action could not be completed."
                self._append_event(
                    session,
                    conversation,
                    event_type="assistant.action.failed",
                    aggregate_type="assistant_action",
                    aggregate_id=action.action_id,
                    payload={
                        "action_id": action.action_id,
                        "code": action.error_code,
                    },
                    correlation_id=action.action_id,
                )
            session.commit()

    def cancel_turn(
        self, session: Session, *, turn_id: str, owner_user_id: str
    ) -> AssistantTurn:
        with self._lock:
            turn = self.get_turn(session, turn_id=turn_id, owner_user_id=owner_user_id)
            if turn.status in {
                AssistantTurnStatus.COMPLETED,
                AssistantTurnStatus.FAILED,
                AssistantTurnStatus.CANCELLED,
            }:
                raise ApiError(
                    409,
                    "ASSISTANT_TURN_NOT_CANCELLABLE",
                    "Only an active assistant Turn can be cancelled.",
                )
            accepted = self.runtime_adapter.cancel(turn.execution_id)
            if not accepted:
                raise ApiError(
                    409,
                    "ASSISTANT_TURN_RUNTIME_NOT_ACTIVE",
                    "The Runtime no longer owns this assistant Turn.",
                )
            turn.status = AssistantTurnStatus.CANCELLED
            turn.completed_at = utc_now()
            self._append_event(
                session,
                turn.conversation,
                event_type="assistant.turn.cancelled",
                aggregate_type="assistant_turn",
                aggregate_id=turn.turn_id,
                payload={"turn_id": turn.turn_id},
                correlation_id=turn.execution_id,
            )
            session.commit()
            session.refresh(turn)
            return turn

    def recover_incomplete_turns(self) -> None:
        """Reattach product-owned Codex Turns after an API process restart."""

        with self.database.session() as session:
            turns = session.scalars(
                select(AssistantTurn).where(
                    AssistantTurn.status.in_(
                        [
                            AssistantTurnStatus.QUEUED,
                            AssistantTurnStatus.SUBMITTED,
                            AssistantTurnStatus.RUNNING,
                            AssistantTurnStatus.WAITING,
                        ]
                    )
                )
            ).all()
            for turn in turns:
                conversation = session.get(AssistantConversation, turn.conversation_id)
                if conversation is None:
                    continue
                if turn.status == AssistantTurnStatus.QUEUED:
                    message = session.get(AssistantMessage, turn.source_message_id)
                    if message is not None:
                        self._workers.submit(
                            self._run_turn,
                            turn.turn_id,
                            conversation.conversation_id,
                            conversation.owner_user_id,
                            message.text_content,
                        )
                    continue
                if not conversation.runtime_workspace_path:
                    continue
                recovery = getattr(self.runtime_adapter, "recover", None)
                if (
                    recovery is None
                    or not turn.runtime_thread_id
                    or not turn.runtime_turn_id
                ):
                    continue
                try:
                    recovered = recovery(self._recovery_request(turn, conversation))
                except Exception:  # noqa: BLE001 - recovery is best effort
                    recovered = False
                if recovered:
                    self._workers.submit(
                        self._await_turn,
                        turn.turn_id,
                        turn.execution_id,
                        conversation.conversation_id,
                        conversation.owner_user_id,
                    )

    def _run_turn(
        self, turn_id: str, conversation_id: str, owner_user_id: str, text: str
    ) -> None:
        with self.database.session() as session:
            turn = session.get(AssistantTurn, turn_id)
            conversation = session.get(AssistantConversation, conversation_id)
            if turn is None or conversation is None:
                return
            source_message = session.get(AssistantMessage, turn.source_message_id)
            attachment_refs = (
                list(source_message.attachment_refs or [])
                if source_message is not None
                else []
            )
            try:
                workspace = self._ensure_assistant_workspace(conversation)
                conversation.runtime_workspace_id = conversation.conversation_id
                conversation.runtime_workspace_path = str(workspace)
                self._rotate_thread_if_needed(session, conversation)
                turn.status = AssistantTurnStatus.SUBMITTED
                turn.started_at = utc_now()
                session.commit()
                runtime_config = RuntimeExecutionConfig(
                    binding_key="platform-assistant",
                    model=turn.requested_model,
                    reasoning_effort=turn.reasoning_effort,
                    security_mode="workspace_restricted",
                    approval_policy="never",
                    sandbox_mode="read-only",
                    network_access=False,
                )
                result = self.runtime_adapter.execute(
                    execution_id=turn.execution_id,
                    role_key="platform-assistant",
                    instructions=self._build_turn_prompt(text, attachment_refs),
                    workspace_id=conversation.runtime_workspace_id,
                    workspace_path=conversation.runtime_workspace_path,
                    thread_id=conversation.runtime_thread_id,
                    output_schema=ASSISTANT_OUTPUT_SCHEMA,
                    runtime_config=runtime_config,
                    developer_instructions=SYSTEM_PROMPT,
                    dynamic_tools=ASSISTANT_DYNAMIC_TOOLS,
                    thread_config=ASSISTANT_THREAD_CONFIG,
                    server_request_handler=self._dynamic_tool_handler(
                        conversation_id, owner_user_id, turn.turn_id
                    ),
                )
                self._record_submitted_runtime(session, conversation, turn, result)
                if result.status == "completed":
                    self._finish_turn(session, conversation, turn, result)
                    self._close_runtime_execution(turn.execution_id)
                else:
                    turn.status = AssistantTurnStatus.WAITING
                    self._append_event(
                        session,
                        conversation,
                        event_type="assistant.turn.waiting",
                        aggregate_type="assistant_turn",
                        aggregate_id=turn.turn_id,
                        payload={"turn_id": turn.turn_id},
                        correlation_id=turn.execution_id,
                    )
                    session.commit()
                    self._workers.submit(
                        self._await_turn,
                        turn.turn_id,
                        turn.execution_id,
                        conversation.conversation_id,
                        conversation.owner_user_id,
                    )
            except Exception as exc:  # noqa: BLE001 - normalize Runtime boundary
                session.rollback()
                if not self._closing.is_set():
                    self._fail_turn(session, conversation, turn, exc)
                self._close_runtime_execution(turn.execution_id)

    def _await_turn(
        self, turn_id: str, execution_id: str, conversation_id: str, owner_user_id: str
    ) -> None:
        try:
            completion = self.runtime_adapter.wait_for_completion(  # type: ignore[attr-defined]
                execution_id,
                timeout=None,
            )
        except Exception as exc:  # noqa: BLE001 - normalize Runtime boundary
            with self.database.session() as session:
                turn = session.get(AssistantTurn, turn_id)
                conversation = session.get(AssistantConversation, conversation_id)
                if (
                    not self._closing.is_set()
                    and turn is not None
                    and conversation is not None
                ):
                    self._fail_turn(session, conversation, turn, exc)
            self._close_runtime_execution(execution_id)
            return
        with self.database.session() as session:
            turn = session.get(AssistantTurn, turn_id)
            conversation = session.get(AssistantConversation, conversation_id)
            if turn is None or conversation is None:
                return
            try:
                self._finish_turn(session, conversation, turn, completion.result)
            except Exception as exc:  # noqa: BLE001 - normalize result boundary
                session.rollback()
                if not self._closing.is_set():
                    self._fail_turn(session, conversation, turn, exc)
        self._close_runtime_execution(execution_id)

    def _finish_turn(
        self,
        session: Session,
        conversation: AssistantConversation,
        turn: AssistantTurn,
        result: RuntimeResult,
    ) -> None:
        if turn.status == AssistantTurnStatus.CANCELLED:
            return
        reply, action_data, presentation_requests = self._parse_runtime_summary(
            result.summary
        )
        content_blocks = self._normalize_assistant_content(
            session,
            reply=reply,
            owner_user_id=turn.owner_user_id,
            presentation_requests=presentation_requests,
        )
        message = self._new_message(
            session,
            conversation,
            owner_user_id=turn.owner_user_id,
            role=AssistantMessageRole.ASSISTANT,
            status=AssistantMessageStatus.COMPLETED,
            text=reply,
            content_blocks=content_blocks,
            reply_to_message_id=turn.source_message_id,
        )
        turn.status = AssistantTurnStatus.COMPLETED
        turn.final_message_id = message.message_id
        turn.completed_at = utc_now()
        self._record_usage(turn, result.usage)
        turn.context_compactions = max(
            turn.context_compactions, result.context_compactions
        )
        conversation.observed_compactions += result.context_compactions
        self._append_event(
            session,
            conversation,
            event_type="assistant.message.completed",
            aggregate_type="assistant_message",
            aggregate_id=message.message_id,
            payload={"message_id": message.message_id, "turn_id": turn.turn_id},
            correlation_id=turn.execution_id,
        )
        if action_data is not None:
            self._create_action(
                session,
                conversation,
                source_turn_id=turn.turn_id,
                owner_user_id=turn.owner_user_id,
                action_data=action_data,
            )
        self._append_event(
            session,
            conversation,
            event_type="assistant.turn.completed",
            aggregate_type="assistant_turn",
            aggregate_id=turn.turn_id,
            payload={"turn_id": turn.turn_id, "message_id": message.message_id},
            correlation_id=turn.execution_id,
        )
        session.commit()

    def _fail_turn(
        self,
        session: Session,
        conversation: AssistantConversation,
        turn: AssistantTurn,
        exc: Exception,
    ) -> None:
        if turn.status == AssistantTurnStatus.CANCELLED:
            return
        turn.status = AssistantTurnStatus.FAILED
        turn.failure_code = "ASSISTANT_RUNTIME_FAILED"
        turn.failure_message = str(exc)[:4_000]
        turn.completed_at = utc_now()
        message = self._new_message(
            session,
            conversation,
            owner_user_id=turn.owner_user_id,
            role=AssistantMessageRole.ASSISTANT,
            status=AssistantMessageStatus.FAILED,
            text="小助理暂时无法完成这次回复，请稍后重试。",
            content_blocks=[
                {
                    "type": "error",
                    "code": turn.failure_code,
                    "text": "The platform assistant Runtime failed.",
                }
            ],
            reply_to_message_id=turn.source_message_id,
        )
        self._append_event(
            session,
            conversation,
            event_type="assistant.turn.failed",
            aggregate_type="assistant_turn",
            aggregate_id=turn.turn_id,
            payload={
                "turn_id": turn.turn_id,
                "message_id": message.message_id,
                "code": turn.failure_code,
            },
            correlation_id=turn.execution_id,
        )
        session.commit()

    def _dynamic_tool_handler(
        self, conversation_id: str, owner_user_id: str, turn_id: str
    ):
        def handle(request: Mapping[str, Any]) -> Mapping[str, Any]:
            params = request.get("params")
            if not isinstance(params, Mapping):
                return self._tool_error("dynamic tool request has no params")
            tool = params.get("tool")
            arguments = params.get("arguments")
            if not isinstance(tool, str) or not isinstance(arguments, Mapping):
                return self._tool_error("dynamic tool request is malformed")
            try:
                with self.database.session() as session:
                    result = self._call_product_tool(
                        session,
                        tool=tool,
                        arguments=dict(arguments),
                        conversation_id=conversation_id,
                        owner_user_id=owner_user_id,
                        turn_id=turn_id,
                    )
                return self._tool_result(result)
            except ApiError as exc:
                return self._tool_error(exc.message)
            except Exception as exc:  # noqa: BLE001 - tool boundary
                return self._tool_error(str(exc))

        return handle

    def _call_product_tool(
        self,
        session: Session,
        *,
        tool: str,
        arguments: dict[str, Any],
        conversation_id: str,
        owner_user_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        if tool == "mutiai_list_organizations":
            rows = session.scalars(
                select(Organization)
                .where(Organization.owner_user_id == owner_user_id)
                .order_by(Organization.created_at, Organization.organization_id)
            ).all()
            return {
                "organizations": [
                    OrganizationSummaryResponse.from_record(row).model_dump(mode="json")
                    for row in rows
                ]
            }
        if tool == "mutiai_get_organization":
            organization = get_owned_organization(
                session,
                organization_id=str(arguments.get("organization_id", "")),
                owner_user_id=owner_user_id,
            )
            published = None
            if organization.current_published_version_id:
                published_version = session.get(
                    OrganizationSpecVersion,
                    organization.current_published_version_id,
                )
                if published_version is not None:
                    published = OrganizationVersionResponse.from_record(
                        published_version
                    ).model_dump(mode="json")
            return {
                "organization": OrganizationSummaryResponse.from_record(
                    organization
                ).model_dump(mode="json"),
                "published_version": published,
            }
        if tool == "mutiai_propose_organization":
            spec = OrganizationSpec.model_validate(arguments.get("spec"))
            version = create_proposal(
                session,
                owner_user_id=owner_user_id,
                spec=spec,
                organization_id=arguments.get("organization_id"),
                source_request=str(arguments.get("source_request", "")),
            )
            check = self.orchestrator.feasibility.evaluate_organization_spec(
                session,
                owner_user_id=owner_user_id,
                spec=spec,
                target_id=version.spec_version_id,
                phase="proposal",
            )
            session.commit()
            return {
                "version": OrganizationVersionResponse.from_record(version).model_dump(
                    mode="json"
                ),
                "feasibility_check_id": check.feasibility_check_id,
                "feasibility_outcome": check.outcome,
            }
        if tool == "mutiai_propose_action":
            action = self._create_action(
                session,
                session.get(AssistantConversation, conversation_id),
                source_turn_id=turn_id,
                owner_user_id=owner_user_id,
                action_data=arguments,
            )
            session.commit()
            return {"action": {"action_id": action.action_id, "status": action.status}}
        if tool == "mutiai_list_actions":
            limit = arguments.get("limit", 20)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
                raise ApiError(
                    422,
                    "ASSISTANT_TOOL_ARGUMENT_INVALID",
                    "The action list limit must be between 1 and 100.",
                )
            query = select(AssistantAction).where(
                AssistantAction.owner_user_id == owner_user_id,
                AssistantAction.conversation_id == conversation_id,
            )
            status = arguments.get("status")
            if status is not None:
                try:
                    normalized_status = AssistantActionStatus(str(status))
                except ValueError as exc:
                    raise ApiError(
                        422,
                        "ASSISTANT_TOOL_ARGUMENT_INVALID",
                        "The requested action status is invalid.",
                    ) from exc
                query = query.where(AssistantAction.status == normalized_status)
            rows = session.scalars(
                query.order_by(
                    AssistantAction.proposed_at.desc(),
                    AssistantAction.action_id.desc(),
                ).limit(limit)
            ).all()
            return {
                "actions": [
                    AssistantActionResponse.from_record(row).model_dump(mode="json")
                    for row in rows
                ]
            }
        if tool == "mutiai_get_action":
            action = session.scalar(
                select(AssistantAction).where(
                    AssistantAction.action_id == str(arguments.get("action_id", "")),
                    AssistantAction.owner_user_id == owner_user_id,
                    AssistantAction.conversation_id == conversation_id,
                )
            )
            if action is None:
                raise ApiError(
                    404,
                    "ASSISTANT_ACTION_NOT_FOUND",
                    "Assistant action not found.",
                )
            return {
                "action": AssistantActionResponse.from_record(action).model_dump(
                    mode="json"
                )
            }
        if tool == "mutiai_check_task_feasibility":
            organization = get_owned_organization(
                session,
                organization_id=str(arguments.get("organization_id", "")),
                owner_user_id=owner_user_id,
            )
            if organization.current_published_version_id is None:
                raise ApiError(
                    409,
                    "ORGANIZATION_NOT_PUBLISHED",
                    "Publish an organization version before checking Task feasibility.",
                )
            version = session.get(
                OrganizationSpecVersion,
                organization.current_published_version_id,
            )
            if version is None:
                raise ApiError(
                    409,
                    "ORGANIZATION_VERSION_MISSING",
                    "The organization version is unavailable.",
                )
            request_text = arguments.get("request")
            if not isinstance(request_text, str) or not request_text.strip():
                raise ApiError(
                    422,
                    "ASSISTANT_TOOL_ARGUMENT_INVALID",
                    "The Task request is missing.",
                )
            try:
                requirements = WorkloadRequirements.model_validate(
                    arguments.get("capability_requirements") or {}
                )
            except ValueError as exc:
                raise ApiError(
                    422,
                    "ASSISTANT_TOOL_ARGUMENT_INVALID",
                    "The Task capability requirements are invalid.",
                ) from exc
            check = self.orchestrator.feasibility.evaluate_task_request(
                session,
                owner_user_id=owner_user_id,
                spec=OrganizationSpec.model_validate(version.spec_payload),
                request_text=request_text,
                explicit_requirements=requirements,
                target_id=new_id(),
                phase="assistant_task_preview",
            )
            session.commit()
            return {
                "feasibility_check": FeasibilityCheckResponse.from_record(
                    check, locale="zh-CN"
                ).model_dump(mode="json")
            }
        if tool == "mutiai_list_tasks":
            query = select(Task).where(Task.owner_user_id == owner_user_id)
            organization_id = arguments.get("organization_id")
            if organization_id:
                query = query.where(Task.organization_id == organization_id)
            rows = session.scalars(
                query.order_by(Task.created_at.desc()).limit(50)
            ).all()
            return {
                "tasks": [
                    TaskResponse.from_record(row).model_dump(mode="json")
                    for row in rows
                ]
            }
        if tool == "mutiai_get_task":
            task = get_owned_task(
                session,
                task_id=str(arguments.get("task_id", "")),
                owner_user_id=owner_user_id,
            )
            return {"task": TaskResponse.from_record(task).model_dump(mode="json")}
        if tool == "mutiai_get_artifact_content":
            task_id = str(arguments.get("task_id", ""))
            artifact_id = str(arguments.get("artifact_id", ""))
            get_owned_task(session, task_id=task_id, owner_user_id=owner_user_id)
            artifact = session.scalar(
                select(Artifact).where(
                    Artifact.artifact_id == artifact_id,
                    Artifact.task_id == task_id,
                )
            )
            if artifact is None:
                raise ApiError(404, "ARTIFACT_NOT_FOUND", "Artifact not found.")
            media_type = artifact.media_type.partition(";")[0].strip().casefold()
            if media_type != "application/json" and not media_type.startswith("text/"):
                raise ApiError(
                    415,
                    "ASSISTANT_ARTIFACT_MEDIA_UNSUPPORTED",
                    "The platform assistant can read only released UTF-8 JSON or text Artifacts.",
                )
            if artifact.byte_size > ASSISTANT_ARTIFACT_CONTENT_MAX_BYTES:
                raise ApiError(
                    413,
                    "ASSISTANT_ARTIFACT_CONTENT_TOO_LARGE",
                    "The Artifact is too large for the platform assistant content reader.",
                    details={
                        "artifact_id": artifact.artifact_id,
                        "byte_size": artifact.byte_size,
                        "max_bytes": ASSISTANT_ARTIFACT_CONTENT_MAX_BYTES,
                    },
                )
            try:
                path = ArtifactManager(self.workspace_manager).resolve_stored_file(
                    artifact
                )
                content_text = path.read_text(encoding="utf-8")
                content: Any = (
                    json.loads(content_text)
                    if media_type == "application/json"
                    else content_text
                )
            except ArtifactError as exc:
                raise ApiError(409, exc.code, str(exc)) from exc
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ApiError(
                    409,
                    "ASSISTANT_ARTIFACT_CONTENT_INVALID",
                    "The released Artifact content could not be read safely.",
                ) from exc
            return {
                "artifact": {
                    "artifact_id": artifact.artifact_id,
                    "task_id": artifact.task_id,
                    "contract_key": artifact.contract_key,
                    "schema_version": artifact.schema_version,
                    "artifact_version": artifact.artifact_version,
                    "media_type": artifact.media_type,
                    "file_name": artifact.file_name,
                    "sha256": artifact.sha256,
                    "byte_size": artifact.byte_size,
                    "status": artifact.status,
                },
                "content_format": (
                    "json" if media_type == "application/json" else "text"
                ),
                "content": content,
                "complete": True,
            }
        if tool == "mutiai_get_attachment_content":
            if self.attachment_manager is None:
                raise ApiError(
                    500,
                    "ASSISTANT_ATTACHMENT_STORAGE_FAILED",
                    "The assistant attachment service is unavailable.",
                )
            try:
                attachment, content = self.attachment_manager.read_text(
                    session,
                    conversation_id=conversation_id,
                    owner_user_id=owner_user_id,
                    attachment_id=str(arguments.get("attachment_id", "")),
                )
            except AssistantAttachmentError as exc:
                raise ApiError(exc.status_code, exc.code, str(exc)) from exc
            media_type = attachment.media_type.partition(";")[0].strip().casefold()
            return {
                "attachment": self.attachment_manager.metadata(attachment),
                "content_format": (
                    "json" if media_type == "application/json" else "text"
                ),
                "content": content,
                "complete": True,
            }
        if tool == "mutiai_get_task_usage":
            task_id = str(arguments.get("task_id", ""))
            get_owned_task(session, task_id=task_id, owner_user_id=owner_user_id)
            rows = session.execute(
                select(Assignment, RuntimeExecution)
                .join(
                    RuntimeExecution,
                    RuntimeExecution.assignment_id == Assignment.assignment_id,
                )
                .where(Assignment.task_id == task_id)
                .order_by(Assignment.created_at, Assignment.assignment_id)
            ).all()
            return {
                "usage": TaskTokenUsageResponse.from_records(
                    task_id, [(row[0], row[1]) for row in rows]
                ).model_dump(mode="json")
            }
        if tool == "mutiai_get_feasibility_check":
            check = session.scalar(
                select(FeasibilityCheck).where(
                    FeasibilityCheck.feasibility_check_id
                    == str(arguments.get("feasibility_check_id", "")),
                    FeasibilityCheck.owner_user_id == owner_user_id,
                )
            )
            if check is None:
                raise ApiError(
                    404,
                    "FEASIBILITY_CHECK_NOT_FOUND",
                    "Feasibility check not found.",
                )
            return {
                "feasibility_check": FeasibilityCheckResponse.from_record(
                    check, locale="zh-CN"
                ).model_dump(mode="json")
            }
        if tool == "mutiai_list_version_feasibility_checks":
            organization = get_owned_organization(
                session,
                organization_id=str(arguments.get("organization_id", "")),
                owner_user_id=owner_user_id,
            )
            version_id = str(arguments.get("spec_version_id", ""))
            version = session.scalar(
                select(OrganizationSpecVersion).where(
                    OrganizationSpecVersion.spec_version_id == version_id,
                    OrganizationSpecVersion.organization_id
                    == organization.organization_id,
                    OrganizationSpecVersion.owner_user_id == owner_user_id,
                )
            )
            if version is None:
                raise ApiError(
                    404,
                    "ORGANIZATION_VERSION_NOT_FOUND",
                    "Organization version not found.",
                )
            checks = session.scalars(
                select(FeasibilityCheck)
                .where(
                    FeasibilityCheck.owner_user_id == owner_user_id,
                    FeasibilityCheck.target_type == "organization_version",
                    FeasibilityCheck.target_id == version.spec_version_id,
                )
                .order_by(
                    FeasibilityCheck.created_at,
                    FeasibilityCheck.feasibility_check_id,
                )
            ).all()
            return {
                "feasibility_checks": [
                    FeasibilityCheckResponse.from_record(
                        check, locale="zh-CN"
                    ).model_dump(mode="json")
                    for check in checks
                ]
            }
        raise ApiError(
            422, "ASSISTANT_TOOL_NOT_FOUND", f"Unknown product tool '{tool}'."
        )

    def _execute_action(
        self, session: Session, action: AssistantAction
    ) -> dict[str, Any]:
        if action.action_type not in ASSISTANT_ACTION_TYPES:
            raise ApiError(
                422, "ASSISTANT_ACTION_INVALID", "The assistant action type is invalid."
            )
        payload = dict(action.payload or {})
        if action.action_type in {
            "organization.confirm",
            "organization.publish",
        }:
            version_id = str(payload.get("spec_version_id") or action.target_id or "")
            version = session.get(OrganizationSpecVersion, version_id)
            if version is None or version.owner_user_id != action.owner_user_id:
                raise ApiError(
                    404,
                    "ORGANIZATION_VERSION_NOT_FOUND",
                    "Organization version not found.",
                )
            phase = (
                "confirmation"
                if action.action_type == "organization.confirm"
                else "publication"
            )
            check = self.orchestrator.feasibility.evaluate_organization_spec(
                session,
                owner_user_id=action.owner_user_id,
                spec=OrganizationSpec.model_validate(version.spec_payload),
                target_id=version.spec_version_id,
                phase=phase,
            )
            try:
                self.orchestrator.feasibility.require_feasible(check)
            except Exception as exc:
                raise ApiError(
                    409,
                    f"FEASIBILITY_{check.outcome.upper()}",
                    "The Runtime feasibility gate blocked this assistant action.",
                    details={
                        "feasibility_check_id": check.feasibility_check_id,
                        "outcome": check.outcome,
                    },
                ) from exc
            organization_id = str(
                payload.get("organization_id") or version.organization_id
            )
        if action.action_type == "organization.confirm":
            from mutiai.services.organizations import confirm_version

            version = confirm_version(
                session,
                organization_id=organization_id,
                spec_version_id=version_id,
                owner_user_id=action.owner_user_id,
            )
            return {
                "spec_version_id": version.spec_version_id,
                "status": version.status,
            }
        if action.action_type == "organization.publish":
            from mutiai.services.organizations import publish_version

            version = publish_version(
                session,
                organization_id=organization_id,
                spec_version_id=version_id,
                owner_user_id=action.owner_user_id,
            )
            return {
                "spec_version_id": version.spec_version_id,
                "status": version.status,
            }
        if action.action_type == "task.submit":
            organization_id = str(
                payload.get("organization_id") or action.target_id or ""
            )
            organization = get_owned_organization(
                session,
                organization_id=organization_id,
                owner_user_id=action.owner_user_id,
            )
            if organization.current_published_version_id is None:
                raise ApiError(
                    409,
                    "ORGANIZATION_NOT_PUBLISHED",
                    "Publish an organization version before creating a task.",
                )
            version = session.get(
                OrganizationSpecVersion,
                organization.current_published_version_id,
            )
            if version is None:
                raise ApiError(
                    409,
                    "ORGANIZATION_VERSION_MISSING",
                    "The organization version is unavailable.",
                )
            request_text = payload.get("request")
            if not isinstance(request_text, str) or not request_text.strip():
                raise ApiError(
                    422,
                    "ASSISTANT_ACTION_PAYLOAD_INVALID",
                    "The Task request is missing.",
                )
            try:
                mode = TaskOrchestrationMode(
                    payload.get("orchestration_mode", TaskOrchestrationMode.LEGACY)
                )
                requirements = WorkloadRequirements.model_validate(
                    payload.get("capability_requirements") or {}
                )
            except ValueError as exc:
                raise ApiError(
                    422,
                    "ASSISTANT_ACTION_PAYLOAD_INVALID",
                    "The Task action payload is invalid.",
                ) from exc
            task_id = new_id()
            check = self.orchestrator.feasibility.evaluate_task_request(
                session,
                owner_user_id=action.owner_user_id,
                spec=OrganizationSpec.model_validate(version.spec_payload),
                request_text=request_text,
                explicit_requirements=requirements,
                target_id=task_id,
                phase="task_submission",
            )
            try:
                self.orchestrator.feasibility.require_feasible(check)
            except Exception as exc:
                raise ApiError(
                    409,
                    f"FEASIBILITY_{check.outcome.upper()}",
                    "The Runtime feasibility gate blocked this assistant action.",
                    details={
                        "feasibility_check_id": check.feasibility_check_id,
                        "outcome": check.outcome,
                    },
                ) from exc
            task, created = create_task(
                session,
                owner_user_id=action.owner_user_id,
                organization_id=organization_id,
                request_text=request_text,
                idempotency_key=f"assistant-action:{action.action_id}",
                orchestration_mode=mode,
                capability_requirements=requirements,
                task_id=task_id,
            )
            if created or task.status in {
                TaskStatus.CREATED,
                TaskStatus.PLANNING,
            }:
                if mode == TaskOrchestrationMode.PLANNED:
                    self.orchestrator.plan(task.task_id)
                else:
                    self.orchestrator.run(task.task_id)
            session.expire_all()
            task = session.get(Task, task.task_id)
            if task is None:
                raise ApiError(409, "TASK_NOT_FOUND", "Task not found.")
            return TaskResponse.from_record(task).model_dump(mode="json")
        if action.action_type == "task.retry":
            task_id = str(payload.get("task_id") or action.target_id or "")
            task = get_owned_task(
                session,
                task_id=task_id,
                owner_user_id=action.owner_user_id,
            )
            if task.status != TaskStatus.FAILED:
                raise ApiError(
                    409,
                    "TASK_NOT_RETRYABLE",
                    "Only a failed task can be retried.",
                )
            self.orchestrator.retry(task.task_id)
            session.expire_all()
            refreshed = session.get(Task, task.task_id)
            if refreshed is None:
                raise ApiError(409, "TASK_NOT_FOUND", "Task not found.")
            return TaskResponse.from_record(refreshed).model_dump(mode="json")
        if action.action_type == "task.cancel":
            task_id = str(payload.get("task_id") or action.target_id or "")
            task = get_owned_task(
                session,
                task_id=task_id,
                owner_user_id=action.owner_user_id,
            )
            try:
                self.orchestrator.cancel(task.task_id)
            except ValueError as exc:
                raise ApiError(
                    409,
                    "TASK_NOT_CANCELLABLE",
                    "Only a non-terminal task can be cancelled.",
                ) from exc
            session.expire_all()
            refreshed = session.get(Task, task.task_id)
            if refreshed is None:
                raise ApiError(409, "TASK_NOT_FOUND", "Task not found.")
            return TaskResponse.from_record(refreshed).model_dump(mode="json")
        if action.action_type == "approval.decide":
            if self.approval_coordinator is None:
                raise ApiError(
                    409,
                    "ASSISTANT_APPROVAL_UNAVAILABLE",
                    "The Runtime approval coordinator is unavailable.",
                )
            try:
                decision = ApprovalDecision(str(payload.get("decision", "")))
                approval = self.approval_coordinator.decide(
                    approval_id=str(
                        payload.get("approval_id") or action.target_id or ""
                    ),
                    task_id=str(payload.get("task_id") or ""),
                    owner_user_id=action.owner_user_id,
                    decision=decision,
                )
            except (LookupError, ValueError, RuntimeError) as exc:
                raise ApiError(
                    409,
                    "ASSISTANT_APPROVAL_DECISION_FAILED",
                    "The Runtime approval decision could not be applied.",
                ) from exc
            return {
                "approval_id": approval.approval_id,
                "status": approval.status,
                "decision": approval.decision,
            }
        raise ApiError(
            422, "ASSISTANT_ACTION_INVALID", "The assistant action type is invalid."
        )

    def _create_action(
        self,
        session: Session,
        conversation: AssistantConversation | None,
        *,
        source_turn_id: str | None,
        owner_user_id: str,
        action_data: Mapping[str, Any],
    ) -> AssistantAction:
        with self._lock:
            return self._create_action_locked(
                session,
                conversation,
                source_turn_id=source_turn_id,
                owner_user_id=owner_user_id,
                action_data=action_data,
            )

    def _create_action_locked(
        self,
        session: Session,
        conversation: AssistantConversation | None,
        *,
        source_turn_id: str | None,
        owner_user_id: str,
        action_data: Mapping[str, Any],
    ) -> AssistantAction:
        if conversation is None:
            raise ApiError(
                409, "ASSISTANT_CONVERSATION_NOT_FOUND", "Conversation not found."
            )
        if source_turn_id is not None:
            existing_turn_action = session.scalar(
                select(AssistantAction)
                .where(
                    AssistantAction.conversation_id == conversation.conversation_id,
                    AssistantAction.owner_user_id == owner_user_id,
                    AssistantAction.source_turn_id == source_turn_id,
                )
                .order_by(AssistantAction.proposed_at, AssistantAction.action_id)
            )
            if existing_turn_action is not None:
                logger.warning(
                    "Ignored an additional assistant Action from Turn %s; "
                    "the Turn already owns Action %s.",
                    source_turn_id,
                    existing_turn_action.action_id,
                )
                return existing_turn_action
        payload = action_data.get("payload")
        if payload is None and isinstance(action_data.get("payload_json"), str):
            try:
                payload = json.loads(action_data["payload_json"])
            except json.JSONDecodeError as exc:
                raise ApiError(
                    422,
                    "ASSISTANT_ACTION_PAYLOAD_INVALID",
                    "The assistant action payload is invalid.",
                ) from exc
        if not isinstance(payload, dict):
            payload = {}
        action_type = action_data.get("action_type")
        if not isinstance(action_type, str):
            raise ApiError(
                422, "ASSISTANT_ACTION_INVALID", "The assistant action type is missing."
            )
        if action_type not in ASSISTANT_ACTION_TYPES:
            raise ApiError(
                422,
                "ASSISTANT_ACTION_INVALID",
                "The assistant action type is invalid.",
            )
        payload = self._validate_action_payload(
            session,
            action_type=action_type,
            target_type=action_data.get("target_type"),
            target_id=action_data.get("target_id"),
            owner_user_id=owner_user_id,
            payload=payload,
        )
        canonical = json.dumps(
            {
                "action_type": action_type,
                "target_type": action_data.get("target_type"),
                "target_id": action_data.get("target_id"),
                "payload": payload,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        existing = session.scalar(
            select(AssistantAction).where(
                AssistantAction.conversation_id == conversation.conversation_id,
                AssistantAction.payload_hash == payload_hash,
                AssistantAction.status.in_(
                    [
                        AssistantActionStatus.PROPOSED,
                        AssistantActionStatus.CONFIRMED,
                        AssistantActionStatus.EXECUTING,
                    ]
                ),
            )
        )
        if existing is not None:
            return existing
        action_id = new_id()
        action = AssistantAction(
            action_id=action_id,
            conversation_id=conversation.conversation_id,
            owner_user_id=owner_user_id,
            source_turn_id=source_turn_id,
            action_type=action_type,
            target_type=action_data.get("target_type"),
            target_id=action_data.get("target_id"),
            payload=payload,
            payload_hash=payload_hash,
            idempotency_key=f"assistant-action:{action_id}:{payload_hash}",
            status=AssistantActionStatus.PROPOSED,
        )
        session.add(action)
        session.flush()
        self._append_event(
            session,
            conversation,
            event_type="assistant.action.proposed",
            aggregate_type="assistant_action",
            aggregate_id=action.action_id,
            payload={"action_id": action.action_id, "action_type": action.action_type},
            correlation_id=source_turn_id or action.action_id,
        )
        return action

    @staticmethod
    def _validate_action_payload(
        session: Session,
        *,
        action_type: str,
        target_type: Any,
        target_id: Any,
        owner_user_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if action_type != "task.submit":
            return payload

        organization_id = payload.get("organization_id")
        request_text = payload.get("request")
        feasibility_check_id = payload.get("feasibility_check_id")
        if (
            target_type != "organization"
            or not isinstance(target_id, str)
            or not target_id
            or not isinstance(organization_id, str)
            or organization_id != target_id
            or not isinstance(request_text, str)
            or not request_text.strip()
            or "capability_requirements" not in payload
            or not isinstance(feasibility_check_id, str)
            or not feasibility_check_id
        ):
            raise ApiError(
                422,
                "ASSISTANT_ACTION_PAYLOAD_INVALID",
                "The Task action requires a matching organization target, request, "
                "capability requirements, and feasibility check.",
            )

        try:
            task_request = TaskCreateRequest.model_validate(
                {
                    "request": request_text.strip(),
                    "orchestration_mode": payload.get(
                        "orchestration_mode", TaskOrchestrationMode.LEGACY
                    ),
                    "capability_requirements": payload["capability_requirements"],
                }
            )
        except ValueError as exc:
            raise ApiError(
                422,
                "ASSISTANT_ACTION_PAYLOAD_INVALID",
                "The Task action payload is invalid.",
            ) from exc

        feasibility_check = session.scalar(
            select(FeasibilityCheck).where(
                FeasibilityCheck.feasibility_check_id == feasibility_check_id,
                FeasibilityCheck.owner_user_id == owner_user_id,
            )
        )
        if (
            feasibility_check is None
            or feasibility_check.target_type != "task_request"
            or feasibility_check.phase != "assistant_task_preview"
            or feasibility_check.outcome != "feasible"
        ):
            raise ApiError(
                422,
                "ASSISTANT_ACTION_PAYLOAD_INVALID",
                "The Task action must reference a feasible Task preview owned by the user.",
            )

        required_inputs = payload.get("required_task_inputs", [])
        if not isinstance(required_inputs, list) or any(
            not isinstance(item, dict)
            or any(
                not isinstance(item.get(field), str) or not item[field].strip()
                for field in ("contract_key", "file_name", "media_type")
            )
            for item in required_inputs
        ):
            raise ApiError(
                422,
                "ASSISTANT_ACTION_PAYLOAD_INVALID",
                "The Task action input declarations are invalid.",
            )

        normalized = dict(payload)
        normalized["organization_id"] = organization_id
        normalized["request"] = task_request.request
        normalized["orchestration_mode"] = task_request.orchestration_mode.value
        normalized["capability_requirements"] = (
            task_request.capability_requirements.model_dump(mode="json")
        )
        normalized["feasibility_check_id"] = feasibility_check_id
        normalized["required_task_inputs"] = required_inputs
        return normalized

    def _ensure_assistant_workspace(self, conversation: AssistantConversation) -> Path:
        path = self.workspace_manager.provision(
            Path("users")
            / conversation.owner_user_id
            / "platform-assistant"
            / conversation.conversation_id
        )
        return path

    def _rotate_thread_if_needed(
        self,
        session: Session,
        conversation: AssistantConversation,
    ) -> None:
        if conversation.runtime_thread_id is None:
            conversation.system_prompt_version = SYSTEM_PROMPT_VERSION
            conversation.tool_contract_version = (
                self.settings.assistant_tool_contract_version
            )
            return

        reason = None
        if conversation.system_prompt_version != SYSTEM_PROMPT_VERSION:
            reason = "system_prompt_changed"
        elif conversation.tool_contract_version != (
            self.settings.assistant_tool_contract_version
        ):
            reason = "tool_contract_changed"
        elif self.settings.assistant_thread_max_compactions is not None:
            generation_compactions = session.scalar(
                select(
                    func.coalesce(func.sum(AssistantTurn.context_compactions), 0)
                ).where(
                    AssistantTurn.conversation_id == conversation.conversation_id,
                    AssistantTurn.runtime_thread_generation
                    == conversation.runtime_thread_generation,
                )
            )
            if generation_compactions >= (
                self.settings.assistant_thread_max_compactions
            ):
                reason = "compaction_limit"

        if reason is None:
            return
        previous_thread_id = conversation.runtime_thread_id
        previous_generation = conversation.runtime_thread_generation
        conversation.runtime_thread_id = None
        conversation.system_prompt_version = SYSTEM_PROMPT_VERSION
        conversation.tool_contract_version = (
            self.settings.assistant_tool_contract_version
        )
        self._append_event(
            session,
            conversation,
            event_type="assistant.runtime_thread.rotation_requested",
            aggregate_type="assistant_conversation",
            aggregate_id=conversation.conversation_id,
            payload={
                "reason": reason,
                "previous_thread_id": previous_thread_id,
                "previous_generation": previous_generation,
            },
            correlation_id=conversation.conversation_id,
        )

    def _close_runtime_execution(self, execution_id: str) -> None:
        close_execution = getattr(self.runtime_adapter, "close_execution", None)
        if close_execution is not None:
            close_execution(execution_id)

    @staticmethod
    def _build_turn_prompt(text: str, attachment_refs: list[dict] | None = None) -> str:
        attachment_context = ""
        if attachment_refs:
            safe_refs = [
                {
                    "attachment_id": ref.get("attachment_id"),
                    "file_name": ref.get("file_name"),
                    "media_type": ref.get("media_type"),
                    "byte_size": ref.get("byte_size"),
                    "sha256": ref.get("sha256"),
                }
                for ref in attachment_refs
                if isinstance(ref, dict)
            ]
            attachment_context = (
                "\nUser attachments are product-owned. Use "
                "mutiai_get_attachment_content with an attachment_id when you need "
                "to inspect a supported text or JSON file. Do not infer contents "
                "from metadata.\n"
                f"Attachment metadata:\n{json.dumps(safe_refs, ensure_ascii=False)}\n"
            )
        return (
            "Use the product-owned tools when current product state or a state change is needed. "
            "Do not claim an action completed until its product record says so. "
            "Set presentation_requests to an empty array unless a validated product resource "
            "reference or product-backed diagram materially helps the user. "
            "Return only JSON matching the requested output schema.\n\n"
            f"User message:\n{text}{attachment_context}"
        )

    def _normalize_assistant_content(
        self,
        session: Session,
        *,
        reply: str,
        owner_user_id: str,
        presentation_requests: list[Any],
    ) -> list[dict[str, Any]]:
        """Build validated product content from prose and resource hints."""

        normalized_reply = re.sub(r"</?[A-Za-z][^>]*>", "", reply.strip())
        if not normalized_reply:
            normalized_reply = "The platform assistant returned no displayable content."
        truncated = len(normalized_reply) > 20_000
        normalized_reply = normalized_reply[:20_000]
        blocks: list[dict[str, Any]] = [
            MarkdownContentBlock(
                text=normalized_reply,
                truncated=truncated,
            ).model_dump(mode="json")
        ]
        if not isinstance(presentation_requests, list):
            return blocks
        for raw_request in presentation_requests[:20]:
            try:
                canonical_request = self._canonical_presentation_request(raw_request)
                request = CONTENT_PRESENTATION_ADAPTER.validate_python(
                    canonical_request
                )
            except ValidationError:
                logger.warning("Ignored invalid assistant content presentation hint")
                continue
            if isinstance(request, ResourceRefPresentationRequest):
                block = self._resource_ref_block(
                    session,
                    owner_user_id=owner_user_id,
                    request=request,
                )
            elif isinstance(request, DiagramPresentationRequest):
                block = self._diagram_block(
                    session,
                    owner_user_id=owner_user_id,
                    request=request,
                )
            else:  # pragma: no cover - the discriminated adapter is exhaustive
                block = None
            if block is not None:
                blocks.append(block)
        return blocks

    @staticmethod
    def _canonical_presentation_request(raw_request: Any) -> Any:
        """Convert the flat Codex response shape into the product union."""

        if not isinstance(raw_request, dict) or "source" in raw_request:
            return raw_request
        kind = raw_request.get("kind")
        if kind == "resource_ref":
            return {
                "kind": kind,
                "resource_type": raw_request.get("resource_type"),
                "resource_id": raw_request.get("resource_id"),
                "label": raw_request.get("label"),
            }
        if kind != "diagram":
            return raw_request
        source_kind = raw_request.get("source_kind")
        if source_kind == "organization_spec_version":
            source = {
                "kind": source_kind,
                "organization_id": raw_request.get("organization_id"),
                "spec_version_id": raw_request.get("spec_version_id"),
            }
        elif source_kind == "task_plan":
            source = {
                "kind": source_kind,
                "task_id": raw_request.get("task_id"),
                "plan_id": raw_request.get("plan_id"),
            }
        else:
            source = {"kind": source_kind}
        return {
            "kind": kind,
            "template": raw_request.get("template"),
            "source": source,
            "text": raw_request.get("text"),
        }

    def _resource_ref_block(
        self,
        session: Session,
        *,
        owner_user_id: str,
        request: ResourceRefPresentationRequest,
    ) -> dict[str, Any] | None:
        resource_type = request.resource_type
        resource_id = request.resource_id
        label = request.label
        exists = False
        if resource_type == "organization":
            resource = session.scalar(
                select(Organization).where(
                    Organization.organization_id == resource_id,
                    Organization.owner_user_id == owner_user_id,
                )
            )
            exists = resource is not None
            label = label or (resource.name if resource is not None else None)
        elif resource_type == "organization_spec_version":
            resource = session.scalar(
                select(OrganizationSpecVersion).where(
                    OrganizationSpecVersion.spec_version_id == resource_id,
                    OrganizationSpecVersion.owner_user_id == owner_user_id,
                )
            )
            exists = resource is not None
            label = label or (
                f"OrganizationSpec v{resource.version_number}"
                if resource is not None
                else None
            )
        elif resource_type == "task":
            resource = session.scalar(
                select(Task).where(
                    Task.task_id == resource_id,
                    Task.owner_user_id == owner_user_id,
                )
            )
            exists = resource is not None
            label = label or (resource.request_text[:80] if resource else None)
        elif resource_type == "plan":
            resource = session.scalar(
                select(TaskExecutionPlan)
                .join(Task, Task.task_id == TaskExecutionPlan.task_id)
                .where(
                    TaskExecutionPlan.plan_id == resource_id,
                    Task.owner_user_id == owner_user_id,
                )
            )
            exists = resource is not None
            label = label or (resource.summary[:80] if resource else None)
        elif resource_type == "artifact":
            resource = session.scalar(
                select(Artifact)
                .join(Task, Task.task_id == Artifact.task_id)
                .where(
                    Artifact.artifact_id == resource_id,
                    Task.owner_user_id == owner_user_id,
                )
            )
            exists = resource is not None
            label = label or (resource.file_name if resource else None)
        elif resource_type == "feasibility_check":
            resource = session.scalar(
                select(FeasibilityCheck).where(
                    FeasibilityCheck.feasibility_check_id == resource_id,
                    FeasibilityCheck.owner_user_id == owner_user_id,
                )
            )
            exists = resource is not None
            label = label or "Runtime feasibility check"
        elif resource_type == "runtime_binding":
            resource = session.scalar(
                select(RuntimeBinding).where(
                    RuntimeBinding.runtime_binding_id == resource_id,
                    RuntimeBinding.owner_user_id == owner_user_id,
                )
            )
            exists = resource is not None
            label = label or (resource.binding_key if resource else None)
        if not exists:
            logger.warning(
                "Ignored assistant content reference to unavailable %s %s",
                resource_type,
                resource_id,
            )
            return None
        label = (label or f"{resource_type} {resource_id[:8]}").strip()[:120]
        return ResourceRefContentBlock(
            resource_type=resource_type,
            resource_id=resource_id,
            label=label,
            text=label,
        ).model_dump(mode="json")

    def _diagram_block(
        self,
        session: Session,
        *,
        owner_user_id: str,
        request: DiagramPresentationRequest,
    ) -> dict[str, Any] | None:
        if request.template == "organization_chart":
            if not isinstance(request.source, OrganizationDiagramSource):
                return None
            version = session.scalar(
                select(OrganizationSpecVersion).where(
                    OrganizationSpecVersion.spec_version_id
                    == request.source.spec_version_id,
                    OrganizationSpecVersion.organization_id
                    == request.source.organization_id,
                    OrganizationSpecVersion.owner_user_id == owner_user_id,
                )
            )
            if version is None:
                logger.warning("Ignored assistant diagram for unavailable organization")
                return None
            text = request.text or (
                f"Organization chart for version {version.version_number}."
            )
        else:
            if not isinstance(request.source, TaskPlanDiagramSource):
                return None
            plan = session.scalar(
                select(TaskExecutionPlan)
                .join(Task, Task.task_id == TaskExecutionPlan.task_id)
                .where(
                    TaskExecutionPlan.plan_id == request.source.plan_id,
                    TaskExecutionPlan.task_id == request.source.task_id,
                    Task.owner_user_id == owner_user_id,
                )
            )
            if plan is None:
                logger.warning("Ignored assistant diagram for unavailable Task plan")
                return None
            text = request.text or f"Execution plan {plan.plan_version}."
        return DiagramContentBlock(
            template=request.template,
            source=request.source,
            text=text,
        ).model_dump(mode="json")

    @staticmethod
    def _parse_runtime_summary(
        summary: str | None,
    ) -> tuple[str, dict[str, Any] | None, list[Any]]:
        if not summary:
            return "小助理没有返回可展示的内容。", None, []
        try:
            value = json.loads(summary)
        except (TypeError, json.JSONDecodeError):
            return summary, None, []
        if not isinstance(value, dict):
            return summary, None, []
        reply = value.get("reply")
        if not isinstance(reply, str) or not reply.strip():
            return summary, None, []
        action = value.get("action")
        requests = value.get("presentation_requests")
        return (
            reply,
            action if isinstance(action, dict) else None,
            requests if isinstance(requests, list) else [],
        )

    def _new_message(
        self,
        session: Session,
        conversation: AssistantConversation,
        *,
        owner_user_id: str,
        role: AssistantMessageRole,
        status: AssistantMessageStatus,
        text: str,
        content_blocks: list[dict] | None = None,
        attachment_refs: list[dict] | None = None,
        idempotency_key: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> AssistantMessage:
        conversation.last_message_sequence += 1
        message = AssistantMessage(
            conversation_id=conversation.conversation_id,
            owner_user_id=owner_user_id,
            sequence=conversation.last_message_sequence,
            role=role,
            status=status,
            text_content=text,
            content_blocks=content_blocks or [{"type": "text", "text": text}],
            attachment_refs=attachment_refs or [],
            idempotency_key=idempotency_key,
            reply_to_message_id=reply_to_message_id,
            completed_at=utc_now()
            if status != AssistantMessageStatus.ACCEPTED
            else None,
        )
        session.add(message)
        session.flush()
        return message

    def _append_event(
        self,
        session: Session,
        conversation: AssistantConversation,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict,
        correlation_id: str,
    ) -> AssistantEvent:
        conversation.last_event_sequence += 1
        event = AssistantEvent(
            conversation_id=conversation.conversation_id,
            owner_user_id=conversation.owner_user_id,
            event_type=event_type,
            schema_version="1.0",
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            sequence=conversation.last_event_sequence,
            occurred_at=utc_now(),
            source="platform_assistant",
            correlation_id=correlation_id,
            payload=payload,
        )
        session.add(event)
        session.flush()
        return event

    def _record_submitted_runtime(
        self,
        session: Session,
        conversation: AssistantConversation,
        turn: AssistantTurn,
        result: RuntimeResult,
    ) -> None:
        turn.runtime_job_id = result.runtime_job_id
        turn.runtime_thread_id = result.thread_id
        turn.runtime_turn_id = result.turn_id
        turn.actual_model = result.actual_model
        if result.thread_id and conversation.runtime_thread_id is None:
            conversation.runtime_thread_id = result.thread_id
            conversation.runtime_thread_generation = max(
                conversation.runtime_thread_generation,
                turn.runtime_thread_generation + 1,
            )
            turn.runtime_thread_generation = conversation.runtime_thread_generation
            thread_event_type = "assistant.runtime_thread.created"
        else:
            turn.runtime_thread_generation = conversation.runtime_thread_generation
            thread_event_type = "assistant.runtime_thread.resumed"
        turn.status = AssistantTurnStatus.RUNNING
        self._append_event(
            session,
            conversation,
            event_type=thread_event_type,
            aggregate_type="assistant_conversation",
            aggregate_id=conversation.conversation_id,
            payload={
                "turn_id": turn.turn_id,
                "thread_generation": turn.runtime_thread_generation,
            },
            correlation_id=turn.execution_id,
        )
        self._append_event(
            session,
            conversation,
            event_type="assistant.turn.started",
            aggregate_type="assistant_turn",
            aggregate_id=turn.turn_id,
            payload={"turn_id": turn.turn_id},
            correlation_id=turn.execution_id,
        )
        session.flush()

    @staticmethod
    def _record_usage(turn: AssistantTurn, usage: RuntimeTokenUsage | None) -> None:
        if usage is None:
            return
        turn.input_tokens = usage.input_tokens
        turn.cached_input_tokens = usage.cached_input_tokens
        turn.output_tokens = usage.output_tokens
        turn.reasoning_output_tokens = usage.reasoning_output_tokens
        turn.total_tokens = usage.total_tokens

    @staticmethod
    def _tool_result(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "contentItems": [
                {
                    "type": "inputText",
                    "text": json.dumps(
                        value, ensure_ascii=False, separators=(",", ":")
                    ),
                }
            ],
        }

    @staticmethod
    def _tool_error(message: str) -> dict[str, Any]:
        return {
            "success": False,
            "contentItems": [{"type": "inputText", "text": message[:4_000]}],
        }

    def _recovery_request(
        self, turn: AssistantTurn, conversation: AssistantConversation
    ):
        from mutiai.runtime import RuntimeRecoveryRequest

        return RuntimeRecoveryRequest(
            execution_id=turn.execution_id,
            runtime_job_id=turn.runtime_job_id,
            thread_id=turn.runtime_thread_id or "",
            turn_id=turn.runtime_turn_id or "",
            workspace_id=conversation.runtime_workspace_id
            or conversation.conversation_id,
            workspace_path=conversation.runtime_workspace_path or "",
            runtime_config=RuntimeExecutionConfig(
                binding_key="platform-assistant",
                model=turn.requested_model,
                reasoning_effort=turn.reasoning_effort,
                security_mode="workspace_restricted",
                approval_policy="never",
                sandbox_mode="read-only",
                network_access=False,
            ),
            developer_instructions=SYSTEM_PROMPT,
            dynamic_tools=ASSISTANT_DYNAMIC_TOOLS,
            thread_config=ASSISTANT_THREAD_CONFIG,
            server_request_handler=self._dynamic_tool_handler(
                conversation.conversation_id,
                conversation.owner_user_id,
                turn.turn_id,
            ),
        )
