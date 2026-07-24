"""Runtime control and Provider capacity API contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from mutiai.api.schemas.organizations import as_utc
from mutiai.services.runtime_controls import RuntimeControlSnapshot


class RuntimeControlResponse(BaseModel):
    provider: str
    max_concurrent_executions: int
    active_executions: int
    token_budget_limit: int | None
    token_reservation_per_execution: int | None
    tokens_reserved: int
    tokens_consumed: int
    tokens_remaining: int | None
    provider_capacity_status: str
    provider_capacity_reason: str | None
    provider_capacity_resets_at: datetime | None
    provider_capacity_observed_at: datetime | None

    @classmethod
    def from_snapshot(cls, snapshot: RuntimeControlSnapshot) -> RuntimeControlResponse:
        remaining = (
            max(
                0,
                snapshot.token_budget_limit
                - snapshot.tokens_consumed
                - snapshot.tokens_reserved,
            )
            if snapshot.token_budget_limit is not None
            else None
        )
        return cls(
            provider=snapshot.provider,
            max_concurrent_executions=snapshot.max_concurrent_executions,
            active_executions=snapshot.active_executions,
            token_budget_limit=snapshot.token_budget_limit,
            token_reservation_per_execution=snapshot.token_reservation_per_execution,
            tokens_reserved=snapshot.tokens_reserved,
            tokens_consumed=snapshot.tokens_consumed,
            tokens_remaining=remaining,
            provider_capacity_status=snapshot.provider_capacity_status,
            provider_capacity_reason=snapshot.provider_capacity_reason,
            provider_capacity_resets_at=(
                as_utc(snapshot.provider_capacity_resets_at)
                if snapshot.provider_capacity_resets_at is not None
                else None
            ),
            provider_capacity_observed_at=(
                as_utc(snapshot.provider_capacity_observed_at)
                if snapshot.provider_capacity_observed_at is not None
                else None
            ),
        )
