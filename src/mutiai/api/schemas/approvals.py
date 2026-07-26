"""Public Runtime approval request and decision contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from mutiai.api.schemas.organizations import as_utc
from mutiai.models import ApprovalRequest
from mutiai.models.approval import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalStatus,
)


class ApprovalDecisionRequest(BaseModel):
    decision: ApprovalDecision


class ApprovalResponse(BaseModel):
    approval_id: str
    task_id: str
    assignment_id: str
    runtime_execution_id: str
    provider: str
    kind: ApprovalKind
    status: ApprovalStatus
    thread_id: str
    turn_id: str
    item_id: str
    reason: str | None
    command: str | None
    details: dict
    decision: ApprovalDecision | None
    decided_by_user_id: str | None
    created_at: datetime
    decided_at: datetime | None

    @classmethod
    def from_record(cls, approval: ApprovalRequest) -> ApprovalResponse:
        return cls(
            approval_id=approval.approval_id,
            task_id=approval.task_id,
            assignment_id=approval.assignment_id,
            runtime_execution_id=approval.runtime_execution_id,
            provider=approval.provider,
            kind=ApprovalKind(approval.kind),
            status=ApprovalStatus(approval.status),
            thread_id=approval.thread_id,
            turn_id=approval.turn_id,
            item_id=approval.item_id,
            reason=approval.reason,
            command=approval.command,
            # Host paths and raw App Server detail objects remain internal audit
            # facts. Add explicit safe public fields when a future approval kind
            # requires more context instead of forwarding opaque Runtime data.
            details={},
            decision=(
                ApprovalDecision(approval.decision)
                if approval.decision is not None
                else None
            ),
            decided_by_user_id=approval.decided_by_user_id,
            created_at=as_utc(approval.created_at),
            decided_at=as_utc(approval.decided_at),
        )
