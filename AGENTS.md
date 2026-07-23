# mutiAI development rules

## Product intent

mutiAI is a visual AI R&D organization system. A user talks to a platform assistant, designs organizations conversationally, publishes a confirmed organization definition, and sends work to an organization lead. Formal organization roles are persistent product entities backed by complete Codex Runtime sessions, not one-shot LLM function nodes.

## Required architecture boundaries

- The product database is the authoritative source for users, organizations, roles, tasks, permissions, runtime bindings, workspaces, artifacts, costs, and execution records.
- LangGraph is a replaceable workflow kernel for task-level routing, fan-out, waiting, approval, retry, recovery, and aggregation.
- Codex is an external execution Runtime. LangGraph must not mirror Codex conversation history or internal tool state.
- LangGraph may store stable identifiers and orchestration summaries, including product task ID, execution ID, runtime job ID, Codex thread ID, turn ID, workspace ID, status, and delivery summary.
- LangGraph owns outer organization-level coordination. Codex owns execution inside one assigned task. Do not let both layers decompose or retry the same responsibility.
- Long Runtime work must use submit, checkpoint, wait, event, and resume. Do not keep a graph node blocked for hours.
- External side effects require stable idempotency keys. Reusing a Codex thread does not provide exactly-once execution.

## Runtime workspace isolation

- Product source repositories under `G:\AI\AI_private\Codex_projects` are control-plane development directories. Never use them as the `cwd` of a managed Codex Runtime thread or turn.
- Local managed Runtime workspaces must be descendants of `G:\AI\AI_private\mutiAI-runtime-workspaces`.
- Canonicalize and validate every Runtime `cwd` against the configured root before starting, resuming, deleting, or cleaning a workspace.
- Store the exact `workspace_id`, canonical workspace path, Codex `thread_id`, and Runtime ownership record in the product database.
- Resume only Thread IDs created and owned by mutiAI. Never discover an unrelated interactive Thread by directory and adopt it.
- Keep managed App Server Threads separate from interactive CLI or IDE Threads. Filter and display product Threads by their recorded IDs, managed workspace paths, and App Server source.
- A Codex Thread may be reused only with its recorded workspace binding. Changing its workspace requires an explicit migration operation.
- Cleanup must be limited to verified descendants of the managed Runtime root. Never delete or move user source repositories or unrelated Codex workspaces.

## V1 product rules

- Use the term `organization`, not `department`.
- Every organization must have exactly one organization lead.
- The platform assistant is above organizations and is not an organization member.
- Users must confirm an organization proposal before it is published.
- Publishing an `OrganizationSpec` must not eagerly create workspaces or Runtime threads.
- V1 supports local Codex only. Keep Runtime interfaces portable to Linux.
- V1 does not include organization membership, invitations, human collaboration nodes, autonomous role creation, or a drag-and-drop editor.
- Existing roles may receive dynamically generated assignments. Agents must not invent and persist new formal roles at runtime.

## Contract ownership

- Authoritative OpenAPI, JSON Schema, and event schemas live in this repository under `contracts/`.
- The frontend repository consumes generated clients or versioned snapshots. It must not redefine product types by hand.
- Do not expose LangGraph state objects as public API resources.
- Event contracts must define event identity, aggregate identity, ordering position, timestamp, payload version, and payload.

## Cross-platform rules

- Windows is the initial development host. Linux is the production target.
- Keep shell commands, process signals, paths, permissions, and sandbox details inside Runtime or infrastructure adapters.
- Use configuration and cross-platform path APIs in the core.
- Validate the first complete Runtime vertical slice on Linux before production hardening.

## Security and quality

- Development credentials belong in ignored local environment files, never committed source.
- A simple bootstrap password is permitted only while the service is bound to localhost.
- Read existing contracts and decisions before changing a shared interface.
- Test code examples and application changes before delivery.
- Verify frontend changes against the real backend in a browser before calling them complete.
- Preserve user changes and avoid unrelated rewrites.
