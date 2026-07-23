# System boundaries

Status: Architecture baseline for detailed design.

## Component ownership

### Web application

Owns presentation, user interaction, organization preview, progress visualization, and channel setup flows. It consumes product contracts and does not interpret LangGraph checkpoints directly.

### Product control layer

Owns accounts, organization definitions and versions, roles, tasks, permissions, runtime bindings, workspaces, artifacts, costs, and audit records. This layer is the product's source of truth.

### LangGraph orchestration layer

Owns the current task workflow: decomposition routing, fan-out to existing roles, waiting, approval, retries, recovery, and aggregation. LangGraph is replaceable and must not own the product organization model.

### Runtime manager

Owns Runtime process lifecycle, task submission, event collection, reconnection, cancellation, and OS-specific operations. It maps stable product execution IDs to Codex thread and turn IDs.

### Codex Runtime

Owns execution inside one bounded assignment: reading a repository, planning, running commands, modifying files, running tests, and maintaining its own thread context.

### Workspace and Git

Own code and file truth. Parallel development eventually requires isolated workspaces or Git worktrees, explicit merge policy, validation, and conflict handling.

## Long-running task pattern

The intended pattern is:

1. Create a stable product execution record.
2. Submit or reuse a Runtime job through an idempotent adapter.
3. Store Runtime identifiers in the product database.
4. Checkpoint the graph and enter a waiting state.
5. Let Codex continue outside the graph process.
6. Persist Runtime events in the product event log.
7. Wake the correct graph thread when a terminal, approval, or actionable event arrives.
8. Resume validation, routing, retry, or aggregation.

Graph replay can re-enter all unfinished nodes. Every external side effect therefore needs an idempotency boundary. A persistent Codex thread preserves context but does not guarantee exactly-once execution.

## State ownership

### Product database

Must retain durable identities, ownership, organization versions, task records, assignment records, Runtime mappings, workspace mappings, event positions, approvals, costs, artifacts, and audit information.

### LangGraph checkpoint

May retain the workflow position, stable product identifiers, current routing information, accumulated delivery summaries, and pending waits. It must not duplicate the full product database or Codex internal history.

### Codex thread

Retains Runtime conversation and execution context. The product stores the thread ID and selected summaries rather than copying the entire internal session into graph state.

### Git and workspace

Retain code, file modifications, commits, test artifacts, and merge outcomes.

## Control-plane separation

The platform assistant operates above organizations. It can publish organization definitions, query stored progress, and issue product commands. Organization leads coordinate work inside their own organizations. They do not need to maintain a second reporting conversation with the platform assistant.

## Replaceability boundary

Product APIs and persisted product entities must not expose LangGraph-specific channels, reducers, checkpoint objects, or node names as their durable representation. A compiler or orchestration adapter will translate product definitions and task plans into LangGraph structures. A later orchestration engine can replace LangGraph without rewriting the account, organization, Runtime, or artifact layers.

## Windows-to-Linux plan

Windows is the first development environment because it provides the strongest local performance. The migration risk is concentrated in process management, shell execution, paths, permissions, signals, filesystem behavior, and sandboxing. Keep these concerns behind infrastructure and Runtime adapters, then validate the first complete vertical slice in Linux before production hardening.
