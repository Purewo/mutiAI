# M0 domain model

Status: Accepted for the first vertical slice. Future fields require a versioned contract change.

## Modeling principles

- Product entities are the durable source of truth.
- LangGraph is an execution view of a product task, not the product data model.
- Codex Thread history remains inside Codex. The product stores IDs and selected summaries.
- Every resource owned by a user is isolated by `owner_user_id`.
- Every external side effect has a stable idempotency identity.
- Organization definitions are versioned and immutable after publication.

## Core entities

### User

Represents an individual account. V1 allows one owner per organization and does not expose membership or invitations.

Important facts:

- `user_id`.
- Login identity and password hash.
- Display name.
- Account status.

The local environment seeds one development `admin` account. The development password is not a production policy.

### Organization

Represents one persistent AI team owned by one user.

Important facts:

- `organization_id`.
- `owner_user_id`.
- Display name and description.
- Current published specification version, if any.
- Lifecycle and audit timestamps.

The organization must always resolve to exactly one organization-lead role in its published specification.

### OrganizationSpecVersion

Represents one structured organization proposal or published definition.

Important facts:

- `organization_spec_version_id`.
- `organization_id` and `owner_user_id`.
- Version number.
- Structured organization definition.
- Source conversation or request reference, if available.
- Version lifecycle.
- Confirmation and publication metadata.

The structured definition contains persistent role identities and runtime bindings, but it does not contain LangGraph node objects, checkpoints, Codex transcripts, or operating-system paths.

Proposed lifecycle:

```text
proposal → confirmed → published → superseded
                         └──────→ archived
```

Only a confirmed version can become the current published version. Publishing a version does not provision a workspace or start a Runtime Thread.

### AgentRole

Represents a persistent position in an organization.

Important facts:

- `agent_role_id`.
- `organization_id`.
- Stable role key and display name.
- Role description and responsibility boundary.
- Whether it is the organization lead.
- Runtime binding configuration reference.

V1 allows task-level dynamic assignment to existing roles. It does not allow a running Agent to create a new persistent formal role.

### Task

Represents a user-requested unit of organization work.

Important facts:

- `task_id`.
- `owner_user_id` and `organization_id`.
- User request and accepted task scope.
- OrganizationSpec version used for planning.
- Current task status.
- Idempotency key for creation.
- Result summary and terminal timestamps.

Draft task lifecycle:

```text
created → planning → running → completed
                    ├──────→ waiting
                    ├──────→ failed
                    └──────→ cancelled
```

`waiting` covers external Runtime completion, approval, or another explicitly modeled wait. A failed task can later be retried or resumed according to the product policy; retry history is not erased.

### Assignment

Represents one bounded piece of a Task assigned to one existing AgentRole.

Important facts:

- `assignment_id`.
- `task_id` and `agent_role_id`.
- Assignment instructions and acceptance criteria.
- Stable `execution_id`.
- Assignment status and result summary.
- Runtime execution reference.

Draft assignment lifecycle:

```text
pending → submitted → running → completed
                    ├──────→ waiting
                    ├──────→ failed
                    └──────→ cancelled
```

The same assignment may be replayed after a graph restart, but the same `execution_id` must not create an untracked duplicate external job.

### RuntimeExecution

Represents the product's binding to one external Runtime execution.

Important facts:

- `runtime_execution_id`.
- `execution_id` as the idempotency identity.
- `assignment_id`.
- Runtime provider, initially Codex.
- Codex `thread_id` and current `turn_id`, when available.
- `workspace_id`.
- Runtime status and last event position.
- Start, completion, failure, cancellation, and reconnect metadata.

This entity is not a copy of Codex history. It is the product's operational index into the external Runtime.

### Workspace

Represents a product-owned working directory binding.

Important facts:

- `workspace_id`.
- `owner_user_id`, `organization_id`, and optional project identity.
- Canonical path under the managed Runtime root.
- Git repository and revision metadata.
- Workspace lifecycle and cleanup policy.

The Runtime Manager must reject paths outside the configured root and must never use product source repositories as managed Runtime workspaces.

### Artifact

Represents a deliverable or verification output produced by a task.

Important facts:

- `artifact_id` and `task_id`.
- Type and metadata.
- Workspace or Git reference.
- Test and validation summary.
- Storage location or immutable content reference.

### Event

Represents an append-only product event or normalized Runtime event summary.

Important facts:

- Event identity.
- Aggregate type and aggregate ID.
- Monotonic sequence or cursor position.
- Event type and schema version.
- Occurrence timestamp.
- Sanitized payload.
- Source and correlation IDs.

Raw Codex event streams remain an adapter concern. Product events must be stable enough for the web UI, audit, retries, and future orchestration engines.

## Ownership and state boundaries

| Concern | Product database | LangGraph checkpoint | Codex Thread | Git/workspace |
| --- | --- | --- | --- | --- |
| User and organization ownership | Authoritative | Reference IDs only | Not owned | Not owned |
| OrganizationSpec versions | Authoritative | Selected version ID | Prompt context only | Not owned |
| Task and Assignment | Authoritative | Current route and summaries | Bounded task instructions | Not owned |
| Runtime mapping | Authoritative | Current wait and IDs | Owns execution context | Workspace reference |
| Internal conversation history | Summary/reference only | Never copy full history | Authoritative | Not owned |
| Code and test artifacts | Metadata/reference | Never copy files | May operate on files | Authoritative |
| Idempotency and audit | Authoritative | Not sufficient alone | Not sufficient alone | Commit/history facts |

## M0 open decisions

The following do not block writing the boundary, but must be resolved before the corresponding implementation:

- Exact ID format and generation library.
- Exact password hashing and session implementation.
- Final database migration and connection strategy.
- Whether a task may have multiple active assignments for the same role.
- Artifact storage location and retention.
