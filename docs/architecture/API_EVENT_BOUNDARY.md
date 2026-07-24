# M0 API and event boundary

Status: Accepted boundary for the first vertical slice. Route names and payload fields remain versioned contracts.

## Contract rules

- Public API resources represent product entities, not LangGraph nodes or checkpoint objects.
- All resource reads are scoped to the authenticated user's ownership.
- Mutating requests that can create external work accept an idempotency key.
- Error responses use one stable envelope with a machine-readable code, human-readable message, request ID, and optional details.
- Long-running work returns a product task or execution identity. HTTP requests do not remain open until Codex finishes.
- SSE events are resumable by an event ID or cursor and may be delivered more than once.

## Initial HTTP surface

These routes are the smallest proposed surface for the first vertical slice:

### Account

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/logout`
- `GET /api/v1/auth/me`

V1 uses a browser-friendly HttpOnly session. The exact session store and CSRF policy are part of implementation review.

### Organization design

- `POST /api/v1/organizations/proposals`: ask the platform assistant to produce a structured proposal.
- `GET /api/v1/organizations`: list organizations owned by the current user.
- `GET /api/v1/organizations/{organization_id}`: return the organization and current published definition.
- `GET /api/v1/organizations/{organization_id}/versions`: list proposal and publication versions.
- `POST /api/v1/organizations/{organization_id}/versions/{version_id}/confirm`: confirm a proposal without publishing it.
- `POST /api/v1/organizations/{organization_id}/versions/{version_id}/publish`: publish a confirmed version.

Publishing is a product state change. It does not start a Codex Runtime.

During the M1 walking skeleton, the proposal route accepts an already structured and validated `OrganizationSpec`. The future platform-assistant design flow must emit the same contract rather than introduce a second organization format.

### Tasks and progress

- `POST /api/v1/organizations/{organization_id}/tasks`: create a task for a published organization.
- `GET /api/v1/tasks/{task_id}`: return current task, assignment, Runtime summary, and result summary.
- `POST /api/v1/tasks/{task_id}/retry`: explicitly retry failed Assignments without replaying completed siblings.
- `POST /api/v1/tasks/{task_id}/cancel`: request cancellation.
- `GET /api/v1/tasks/{task_id}/approvals`: list product-owned Runtime approval requests for the Task.
- `POST /api/v1/tasks/{task_id}/approvals/{approval_id}/decision`: submit one one-time `accept`, `decline`, or `cancel` decision.
- `GET /api/v1/tasks/{task_id}/events`: stream normalized task events through SSE.

Task creation must accept an idempotency key. The same key must return the original task identity rather than create a second external execution.

The HTTP header name is `Idempotency-Key`. Reusing a key with a different request payload returns an idempotency conflict.

An approval is a product database entity, not a LangGraph interrupt or a copy of Codex internal state. The Runtime worker waits outside the graph while LangGraph remains checkpointed. V1 decisions apply only to one App Server request; session-wide acceptance and policy amendments are not public API options.

Repeating the same approval decision is idempotent. Submitting a different decision after resolution returns a conflict. If the Runtime no longer owns and waits on the request, the API returns a conflict instead of claiming that Codex received the decision.

### Organization-lead conversation

- `POST /api/v1/organizations/{organization_id}/lead/messages`: submit a user message to the organization lead flow.
- `GET /api/v1/organizations/{organization_id}/lead/events`: stream the lead's product-safe response and task events.

The lead conversation remains an outer product interaction. Codex internal tool calls are not exposed as a public API transcript by default.

## Event envelope

Every persisted or streamed product event uses one envelope shape:

```text
event_id
event_type
schema_version
aggregate_type
aggregate_id
task_id (optional)
assignment_id (optional)
runtime_execution_id (optional)
sequence
occurred_at
source
correlation_id
payload
```

The event envelope is a product contract. `payload` is versioned by `schema_version`; consumers must ignore unknown fields and handle unknown event types without corrupting the current task view.

## Initial event catalog

The first implementation may need these event types:

- `organization.version.created`
- `organization.version.published`
- `task.created`
- `task.status_changed`
- `assignment.created`
- `assignment.status_changed`
- `runtime.execution_submitted`
- `runtime.execution_started`
- `runtime.progress`
- `runtime.execution_waiting`
- `runtime.execution_completed`
- `runtime.execution_failed`
- `runtime.execution_retry_requested`
- `runtime.approval_requested`
- `runtime.approval_resolved`
- `task.retry_requested`
- `artifact.created`
- `task.completed`
- `task.failed`

The adapter may receive many Codex-specific events but should normalize only stable product-relevant facts into this catalog.

`runtime.execution_failed` uses a product-level `reason`. The current Runtime boundary distinguishes a terminal Turn failure (`runtime_terminal_failure`) from loss of the process that owned an in-flight Turn (`runtime_owner_lost`). The browser must not infer recovery policy from raw Codex error text.

`runtime.approval_requested` identifies the product approval record, approval kind, and pending status. `runtime.approval_resolved` records the one-time decision, resulting status, and resolution reason. Command details remain available through the owned approval resource rather than being copied into LangGraph State.

## SSE behavior

- Each event has an SSE `id` derived from the product event identity or monotonic cursor.
- The client sends `Last-Event-ID` when reconnecting.
- The server replays events after the requested cursor before following new events.
- Duplicate delivery is expected; the frontend and backend consumers must deduplicate by event identity or sequence.
- A terminal task event closes the logical stream, but the task resource remains queryable.

The current M1 endpoint replays persisted events and then closes. Following newly appended events remains pending until task execution moves out of the request process.

## Error envelope

The initial error contract is:

```text
code
message
request_id
details (optional)
```

The API must distinguish invalid organization definitions, ownership violations, stale versions, idempotency conflicts, Runtime unavailability, and task terminal-state conflicts. It must not expose raw LangGraph or Codex stack traces to the browser.

## LangGraph adapter boundary

The compiler or workflow service receives product identifiers and task input, then returns or emits product-level updates. It must not make public APIs depend on:

- Graph node names.
- Channel names or reducers.
- Checkpoint IDs.
- Pregel superstep numbers.
- Raw interrupt payload formats.

Those details remain replaceable orchestration implementation details.
