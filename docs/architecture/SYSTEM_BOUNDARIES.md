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

The local Codex App Server runs as an independent sidecar. FastAPI owns client connections and recovery coordination, but it does not own the sidecar lifetime. This separation lets Codex continue an active Turn while the API process restarts.

The local sidecar supervisor owns process readiness and bounded restart. A process restart does not change product task state or replay a Turn. The API records the disconnected execution as owner-lost, and only an explicit product retry may create the replacement Turn.

### Codex Runtime

Owns execution inside one bounded assignment: reading a repository, planning, running commands, modifying files, running tests, and maintaining its own thread context.

### Workspace and Git

Own code and file truth. Parallel development eventually requires isolated workspaces or Git worktrees, explicit merge policy, validation, and conflict handling.

## Managed Runtime workspace isolation

The local V1 Runtime root is:

```text
G:\AI\AI_private\mutiAI-runtime-workspaces
```

The product source repositories under `G:\AI\AI_private\Codex_projects` are control-plane development directories. They must never become the working directory of a managed Codex Thread or Turn.

The planned managed hierarchy is:

```text
mutiAI-runtime-workspaces/
└── users/{user_id}/
    └── organizations/{organization_id}/
        └── projects/{project_id}/
            └── workspaces/{workspace_id}/
```

The exact hierarchy may evolve, but these invariants do not. The current M2 local provisioner uses `users/{user_id}/organizations/{organization_id}/workspaces/{workspace_id}` and keeps the stable `agent_role_key` binding in the product Workspace record:

```text
mutiAI-runtime-workspaces/
└── users/{user_id}/organizations/{organization_id}/workspaces/{workspace_id}/
```

- Every managed workspace has a durable product `workspace_id`.
- Every Runtime `cwd` resolves to a canonical descendant of the configured managed root.
- Every Codex Thread belongs to an explicit Runtime binding and workspace record.
- The Runtime Manager resumes only Thread IDs previously created by mutiAI.
- A Thread is never adopted from a user's existing interactive history merely because its `cwd` matches.
- A Thread-to-workspace change is an explicit migration, not an incidental Turn override.
- Cleanup verifies both root containment and a mutiAI ownership record before touching a directory.

The core exposes a WorkspaceManager that canonicalizes an existing or planned Runtime path and rejects any path that is not a strict descendant of the configured root. On the local Windows host it also rejects configurations that overlap `G:\AI\AI_private\Codex_projects`. WorkspaceProvisioner performs explicit first-use provisioning and records the canonical path before marking the product Workspace ready. The manager performs no cleanup by itself.

App Server Threads are additionally distinguishable from normal interactive CLI and IDE Threads by their source. Product history views should use recorded Thread IDs, managed workspace paths, and App Server source filters rather than mixing all local Codex history.

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

M1 stores checkpoints in the separate SQLite file configured by `LANGGRAPH_CHECKPOINT_PATH`; product tables remain in `DATABASE_URL`. Sharing a host or database technology does not merge their ownership boundaries.

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

## Codex references

- [App Server lifecycle](https://learn.chatgpt.com/docs/app-server#lifecycle-overview): `turn/start` can explicitly set `cwd`, while Thread start and resume remain separate lifecycle operations.
- [Thread list filters](https://learn.chatgpt.com/docs/app-server#list-threads-with-pagination--filters): Thread history can be filtered by exact session `cwd` and source kind.
