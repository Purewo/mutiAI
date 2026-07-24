"""Product-owned admission, concurrency, and token-budget accounting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from mutiai.config import Settings
from mutiai.models import (
    Assignment,
    RuntimeControlPolicy,
    RuntimeExecution,
    RuntimeProviderCapacityRecord,
    Task,
)
from mutiai.models.base import utc_now
from mutiai.models.task import RuntimeExecutionStatus
from mutiai.runtime import (
    AgentRuntimeAdapter,
    RuntimeCapacity,
    RuntimeTokenUsage,
)

CONCURRENCY_WAIT_REASON = "concurrency_limit"


class RuntimeProviderRateLimitedError(RuntimeError):
    """The Provider explicitly reported that new work cannot start."""

    def __init__(self, *, provider: str, reason: str, resets_at: int | None) -> None:
        self.provider = provider
        self.reason = reason
        self.resets_at = resets_at
        super().__init__(f"Runtime Provider '{provider}' is rate limited: {reason}")


class RuntimeBudgetExceededError(RuntimeError):
    """A product-owned token reservation would exceed the configured budget."""

    def __init__(
        self,
        *,
        provider: str,
        limit: int,
        consumed: int,
        reserved: int,
        requested: int,
    ) -> None:
        self.provider = provider
        self.limit = limit
        self.consumed = consumed
        self.reserved = reserved
        self.requested = requested
        super().__init__(
            f"Runtime token budget for '{provider}' cannot reserve {requested} tokens"
        )


@dataclass(frozen=True, slots=True)
class RuntimeAdmissionDecision:
    admitted: bool
    active_executions: int
    max_concurrent_executions: int
    reserved_tokens: int
    provider_capacity: RuntimeCapacity | None


@dataclass(frozen=True, slots=True)
class RuntimeControlSnapshot:
    provider: str
    max_concurrent_executions: int
    active_executions: int
    token_budget_limit: int | None
    token_reservation_per_execution: int | None
    tokens_reserved: int
    tokens_consumed: int
    provider_capacity_status: str
    provider_capacity_reason: str | None
    provider_capacity_resets_at: datetime | None
    provider_capacity_observed_at: datetime | None


class RuntimeControlService:
    """Apply product policy without placing account state in LangGraph."""

    def __init__(
        self,
        settings: Settings,
        runtime_adapter: AgentRuntimeAdapter,
    ) -> None:
        self.settings = settings
        self.runtime_adapter = runtime_adapter

    def admit(
        self,
        session: Session,
        *,
        task: Task,
        execution: RuntimeExecution,
    ) -> RuntimeAdmissionDecision:
        """Reserve one product slot and token budget, or defer without blocking."""

        policy = self.ensure_policy(
            session,
            owner_user_id=task.owner_user_id,
            provider=execution.provider,
        )
        active = self.active_execution_count(
            session,
            owner_user_id=task.owner_user_id,
            provider=execution.provider,
            excluding_execution_id=execution.execution_id,
        )
        if active >= policy.max_concurrent_executions:
            return RuntimeAdmissionDecision(
                admitted=False,
                active_executions=active,
                max_concurrent_executions=policy.max_concurrent_executions,
                reserved_tokens=0,
                provider_capacity=None,
            )

        capacity = self._read_capacity()
        self._record_provider_capacity(
            session,
            owner_user_id=task.owner_user_id,
            provider=execution.provider,
            capacity=capacity,
        )
        if capacity.status == "limited":
            raise RuntimeProviderRateLimitedError(
                provider=execution.provider,
                reason=capacity.reason or "provider_rate_limit_reached",
                resets_at=capacity.resets_at,
            )

        reservation = policy.token_reservation_per_execution or 0
        if (
            policy.token_budget_limit is not None
            and policy.tokens_consumed + policy.tokens_reserved + reservation
            > policy.token_budget_limit
        ):
            raise RuntimeBudgetExceededError(
                provider=execution.provider,
                limit=policy.token_budget_limit,
                consumed=policy.tokens_consumed,
                reserved=policy.tokens_reserved,
                requested=reservation,
            )

        policy.tokens_reserved += reservation
        policy.updated_at = utc_now()
        execution.reserved_tokens = reservation
        execution.charged_tokens = None
        execution.usage_status = "pending"
        execution.input_tokens = None
        execution.cached_input_tokens = None
        execution.output_tokens = None
        execution.reasoning_output_tokens = None
        execution.total_tokens = None
        execution.admitted_at = utc_now()
        execution.budget_settled_at = None
        execution.wait_reason = None
        session.flush()
        return RuntimeAdmissionDecision(
            admitted=True,
            active_executions=active,
            max_concurrent_executions=policy.max_concurrent_executions,
            reserved_tokens=reservation,
            provider_capacity=capacity,
        )

    def settle(
        self,
        session: Session,
        *,
        task: Task,
        execution: RuntimeExecution,
        usage: RuntimeTokenUsage | None,
    ) -> int:
        """Release one reservation and charge observed or conservative usage once."""

        if execution.budget_settled_at is not None:
            return execution.charged_tokens or 0

        policy = self.ensure_policy(
            session,
            owner_user_id=task.owner_user_id,
            provider=execution.provider,
        )
        reservation = execution.reserved_tokens
        charged = usage.total_tokens if usage is not None else reservation
        policy.tokens_reserved = max(0, policy.tokens_reserved - reservation)
        policy.tokens_consumed += charged
        policy.updated_at = utc_now()

        execution.reserved_tokens = 0
        execution.charged_tokens = charged
        execution.usage_status = "reported" if usage is not None else "unavailable"
        if usage is not None:
            execution.input_tokens = usage.input_tokens
            execution.cached_input_tokens = usage.cached_input_tokens
            execution.output_tokens = usage.output_tokens
            execution.reasoning_output_tokens = usage.reasoning_output_tokens
            execution.total_tokens = usage.total_tokens
        execution.budget_settled_at = utc_now()
        session.flush()
        return charged

    def snapshot(
        self,
        session: Session,
        *,
        owner_user_id: str,
        provider: str,
    ) -> RuntimeControlSnapshot:
        policy = self.ensure_policy(
            session,
            owner_user_id=owner_user_id,
            provider=provider,
        )
        capacity = session.scalar(
            select(RuntimeProviderCapacityRecord).where(
                RuntimeProviderCapacityRecord.owner_user_id == owner_user_id,
                RuntimeProviderCapacityRecord.provider == provider,
            )
        )
        return RuntimeControlSnapshot(
            provider=provider,
            max_concurrent_executions=policy.max_concurrent_executions,
            active_executions=self.active_execution_count(
                session,
                owner_user_id=owner_user_id,
                provider=provider,
            ),
            token_budget_limit=policy.token_budget_limit,
            token_reservation_per_execution=(
                policy.token_reservation_per_execution
            ),
            tokens_reserved=policy.tokens_reserved,
            tokens_consumed=policy.tokens_consumed,
            provider_capacity_status=capacity.status if capacity else "unknown",
            provider_capacity_reason=capacity.reason if capacity else None,
            provider_capacity_resets_at=capacity.resets_at if capacity else None,
            provider_capacity_observed_at=capacity.observed_at if capacity else None,
        )

    def ensure_policy(
        self,
        session: Session,
        *,
        owner_user_id: str,
        provider: str,
    ) -> RuntimeControlPolicy:
        policy = session.scalar(
            select(RuntimeControlPolicy).where(
                RuntimeControlPolicy.owner_user_id == owner_user_id,
                RuntimeControlPolicy.provider == provider,
            )
        )
        if policy is not None:
            return policy
        policy = RuntimeControlPolicy(
            owner_user_id=owner_user_id,
            provider=provider,
            max_concurrent_executions=(
                self.settings.runtime_max_concurrent_executions
            ),
            token_budget_limit=self.settings.runtime_token_budget_limit,
            token_reservation_per_execution=(
                self.settings.runtime_token_reservation_per_execution
            ),
            tokens_reserved=0,
            tokens_consumed=0,
        )
        session.add(policy)
        session.flush()
        return policy

    @staticmethod
    def active_execution_count(
        session: Session,
        *,
        owner_user_id: str,
        provider: str,
        excluding_execution_id: str | None = None,
    ) -> int:
        conditions = [
            Task.owner_user_id == owner_user_id,
            RuntimeExecution.provider == provider,
            or_(
                RuntimeExecution.status == RuntimeExecutionStatus.RUNNING,
                and_(
                    RuntimeExecution.status == RuntimeExecutionStatus.WAITING,
                    RuntimeExecution.wait_reason.is_(None),
                ),
            ),
        ]
        if excluding_execution_id is not None:
            conditions.append(RuntimeExecution.execution_id != excluding_execution_id)
        count = session.scalar(
            select(func.count())
            .select_from(RuntimeExecution)
            .join(
                Assignment,
                Assignment.assignment_id == RuntimeExecution.assignment_id,
            )
            .join(Task, Task.task_id == Assignment.task_id)
            .where(*conditions)
        )
        return int(count or 0)

    def _read_capacity(self) -> RuntimeCapacity:
        try:
            return self.runtime_adapter.capacity()
        except Exception:  # noqa: BLE001 - Provider signal boundary
            return RuntimeCapacity(
                status="unknown",
                reason="provider_capacity_unavailable",
            )

    @staticmethod
    def _record_provider_capacity(
        session: Session,
        *,
        owner_user_id: str,
        provider: str,
        capacity: RuntimeCapacity,
    ) -> None:
        record = session.scalar(
            select(RuntimeProviderCapacityRecord).where(
                RuntimeProviderCapacityRecord.owner_user_id == owner_user_id,
                RuntimeProviderCapacityRecord.provider == provider,
            )
        )
        resets_at = (
            datetime.fromtimestamp(capacity.resets_at, tz=UTC).replace(
                tzinfo=None
            )
            if capacity.resets_at is not None
            else None
        )
        if record is None:
            record = RuntimeProviderCapacityRecord(
                owner_user_id=owner_user_id,
                provider=provider,
                status=capacity.status,
                reason=capacity.reason,
                resets_at=resets_at,
                observed_at=utc_now(),
            )
            session.add(record)
        else:
            record.status = capacity.status
            record.reason = capacity.reason
            record.resets_at = resets_at
            record.observed_at = utc_now()
        session.flush()
