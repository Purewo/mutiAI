# ADR-0004: M1 task orchestration and recovery boundary

Status: Accepted for the M1 orchestration boundary.

## Context

M1 must prove task idempotency, dynamic fan-out to existing roles, durable events, LangGraph recovery, and Runtime result reuse before Codex App Server is introduced. The implementation must not make LangGraph the source of product facts or treat the synchronous fake adapter as the long-task production design.

## Decision

- Persist Task, Assignment, RuntimeExecution, and ProductEvent records in the product database.
- Store LangGraph checkpoints in a separate SQLite database configured by `LANGGRAPH_CHECKPOINT_PATH`.
- Use the product `task_id` as the LangGraph `thread_id`.
- Generate deterministic Assignment and execution IDs from the task ID and existing role key.
- Use LangGraph `Send` to fan out work only to non-lead roles already present in the published OrganizationSpec.
- Keep the organization-lead aggregation summary in graph state and persist the accepted summary on the Task.
- Check the product RuntimeExecution before invoking an adapter. Replayed completed executions return their persisted result without another adapter call.
- Persist normalized product events before exposing them through the SSE endpoint.
- Require an `Idempotency-Key` for task creation and reject reuse with a different request payload.
- Persist an external Runtime completion and its stable event ID in the product database before resuming LangGraph with `Command(resume=...)`.
- Use LangGraph interrupts only as orchestration wake-up points. Runtime execution status and completion results remain product-database facts.

## M1 execution limitation

The FakeRuntimeAdapter can either complete inside the request or return a waiting result so the checkpoint and resume boundary can be tested without an external process. It does not run a durable worker or deliver callbacks. The Codex adapter must submit external work, persist Thread and Turn IDs, checkpoint the graph, enter a wait state, and resume from a Runtime event. A LangGraph node must not block for the duration of a real Codex task.

## Recovery semantics

The product database remains authoritative when checkpoints and product rows temporarily disagree:

- A completed RuntimeExecution is not submitted again.
- A completed graph whose final Task write was interrupted can restore the graph summary and complete the product Task.
- A failed parallel branch can resume without replaying a successful sibling branch.
- A Runtime completion is committed before graph resume. Replaying the same event ID is safe, while completing the same execution with another event ID is rejected.
- Multiple waiting branches resume independently. The Task remains waiting until the final interrupt is resolved.
- Product event delivery is at least once. Consumers resume with `Last-Event-ID` and deduplicate by event ID or sequence.

## Consequences

- SQLite limits the M1 process to local, low-concurrency validation.
- Event sequence allocation is serialized inside the local orchestrator.
- A durable task queue and external wake-up mechanism remain required before real long-running Runtime work.
- LangGraph checkpoints can be replaced without migrating authoritative organization or task records.
