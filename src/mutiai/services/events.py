"""Append-only product event operations."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mutiai.models import ProductEvent, Task


def append_task_event(
    session: Session,
    *,
    task: Task,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    source: str,
    payload: dict,
    assignment_id: str | None = None,
    runtime_execution_id: str | None = None,
) -> ProductEvent:
    session.flush()
    latest_sequence = session.scalar(
        select(func.max(ProductEvent.sequence)).where(
            ProductEvent.task_id == task.task_id
        )
    )
    event = ProductEvent(
        event_type=event_type,
        schema_version="1.0",
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        task_id=task.task_id,
        assignment_id=assignment_id,
        runtime_execution_id=runtime_execution_id,
        sequence=(latest_sequence or 0) + 1,
        source=source,
        correlation_id=task.task_id,
        payload=payload,
    )
    session.add(event)
    session.flush()
    return event
