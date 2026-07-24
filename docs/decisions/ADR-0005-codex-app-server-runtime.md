# ADR-0005: Codex App Server Runtime boundary

Status: Accepted for the M2 local integration spike.

## Context

The first real Runtime must preserve Codex Thread and Turn capabilities without blocking a LangGraph node or using an existing interactive Codex working directory. The product must capture stable Runtime identities while leaving detailed commands, file changes, tool calls, and conversation history inside Codex.

The local implementation was checked against `codex-cli 0.145.0`, its generated App Server JSON Schema, and the current official App Server documentation.

## Decision

- Run an owned local `codex app-server --listen stdio://` process and communicate through newline-delimited JSON-RPC messages.
- Complete `initialize` and `initialized` once per connection before sending other requests.
- Start a new execution with `thread/start`, then immediately submit its work with `turn/start`.
- Resume only a product-recorded Thread ID with `thread/resume`. Always send the recorded canonical workspace path as the `cwd` consistency check.
- For transparent cross-process recovery, connect to a long-lived external App Server over a loopback WebSocket on Windows or a Unix socket on Linux. Rejoin with `thread/resume`, validate the returned Thread and `cwd`, and continue observing the recorded Turn. Recovery never calls `turn/start`.
- Run that App Server as an independent sidecar rather than as a child owned by the FastAPI lifespan. The API performs the documented `/readyz` health check, closes only its client connections during shutdown, and reconnects on the next startup.
- If `thread/resume` reports a terminal Turn, read the persisted Thread with `thread/read(includeTurns=true)` and deliver that terminal result through the normal supervisor boundary.
- Treat a pending command/file approval as a recovery boundary. The public App Server lifecycle documents the approval request/response exchange but does not guarantee replay of an unanswered server request after client loss. Startup reconciliation therefore marks such executions `runtime_owner_lost` and requires explicit retry.
- Set `clientUserMessageId` to the product `execution_id` when starting a Turn.
- Return `waiting` with the App Server Thread ID, Turn ID, Runtime job ID, and product Workspace ID as soon as submission succeeds. Do not wait for `turn/completed` inside LangGraph.
- Consume streamed notifications in a Runtime worker. Convert a terminal Turn into one deterministic product Runtime event, persist it, then resume LangGraph through the existing idempotent completion boundary.
- Treat a `turn/completed` notification with status `failed` or `interrupted` as a terminal Runtime failure. Persist the RuntimeExecution, Assignment, and Task failure without resuming the graph automatically.
- Retry only through an explicit product command. Reset failed Assignments, reuse their recorded Workspace and Thread, start a new Turn, and keep completed sibling Assignments unchanged.
- Treat loss of the owned App Server connection as `runtime_owner_lost`. During intentional supervisor shutdown, leave the durable wait for startup reconciliation; when a live supervisor detects the loss, persist the failure without replaying the Turn.
- On startup, reconcile waiting Codex executions that have no active owner in the new process into the same explicit-retry state. Do not claim that a new process has safely resumed an in-flight Turn.
- Use `workspaceWrite` for the managed workspace with network access disabled by default.
- Use `on-request` approval behavior by default. Convert command-execution and file-change approval requests into product-owned database records, then let the Runtime worker wait outside LangGraph for a one-time user decision.
- Expose only `accept`, `decline`, and `cancel` in V1. Do not expose `acceptForSession`, exec-policy amendments, or network-policy amendments.
- Preserve the App Server JSON-RPC request ID with the RuntimeExecution, Thread, Turn, and item identities. Reply through the same owned App Server connection only after the product decision is committed.
- Persist `runtime.approval_requested` and `runtime.approval_resolved` events through the product event sequence. Keep approval state out of LangGraph State.
- Cancel active approvals during controlled backend shutdown. After owner loss or restart, cancel stale pending approval records and require explicit Runtime retry instead of claiming that the old App Server request can be resumed.
- Reject other unhandled App Server requests rather than silently approving them.
- Accept external WebSocket endpoints only on loopback hosts unless a future authenticated remote transport is added. Unix socket paths must be absolute. The adapter does not connect to an unauthenticated remote endpoint.
- Require an isolated product Codex home under the managed Runtime root. Do not write product App Server sessions into the user's interactive Codex home.
- For the current local relay setup, bootstrap that home by copying only `config.toml` and `auth.json`. Do not copy `sessions`, history, SQLite state, or other interactive-home data. Official ChatGPT device-code login remains an optional alternative, not a prerequisite for custom-provider API-key authentication.
- Resolve the Windows command shim through `PATH` before starting the process. Python cannot directly launch the PowerShell `codex` shim on this host, while the resolved `codex.cmd` works.

## Verified behavior

- The Python client completed a real initialization handshake with local `codex-cli 0.145.0`.
- A real `thread/start` returned a UUID Thread ID and the isolated canonical working directory.
- The fake App Server integration verifies initialization, Thread start, Thread resume request shape, Turn start, notifications arriving before responses, terminal result extraction, and idempotent in-process submission.
- The background Runtime supervisor waits outside LangGraph, delivers terminal results through the product completion boundary, serializes parallel checkpoint resumes, deduplicates workers, and closes each execution's App Server session.
- A real relay-backed Turn completed from the isolated home and produced a file inside the managed workspace.
- A real two-specialist product Task completed through the API, LangGraph wait/resume path, Runtime supervisor, product database, and role-specific managed Workspaces.
- A real three-role product Task completed through specialist fan-out and a separate organization-lead review Turn. The lead returned a schema-constrained `accepted` decision, and a separate run returned `needs_revision` when specialist claims conflicted.
- A fake terminal Turn failure is normalized into a deterministic product event and exposed through an explicit retry API. The retry reuses the failed role's Thread and Workspace, creates a new Turn, and does not replay a successful parallel role.
- An App Server process exit is normalized as `runtime_owner_lost`; the worker persists the failure and the retry API completes the assignment on a new Turn.
- A persistent external App Server preserves an in-flight Turn across a client/backend disconnect. A second client rejoined the same Thread, observed the original Turn completion, and the product retained the original Thread and Turn IDs.
- `RUNTIME_PROVIDER=codex` assembles the real Adapter from settings and refuses to enter its lifespan when the configured sidecar is not ready. The default `RUNTIME_PROVIDER=fake` remains unchanged.
- A Turn completed while the client was disconnected is recovered from Thread history without starting a replacement Turn. Startup emits `runtime.execution_reconnected` before the supervisor consumes the terminal state.
- A backend restart with one completed specialist and one orphaned waiting specialist marks only the orphaned branch failed at startup. Explicit retry starts a new Turn for that branch while preserving the completed sibling's Runtime result and Turn identity.
- Fake App Server integration covers command and file-change approval requests plus `accept`, `decline`, and `cancel` responses. API integration verifies authenticated ownership, durable request and resolution events, same-decision idempotency, conflicting-decision rejection, and continued or interrupted Turn outcomes.
- The local relay rejected an optional lead schema when its default-valued `issues` property was not listed as required. The product contract now requires every lead response property, matching the relay's `response_format` validation rules.
- A Thread with no Turn did not survive App Server restart as a resumable rollout. Local `thread/resume` returned `no rollout found` for that empty Thread, so recovery was validated with a real in-flight Turn and a long-lived external App Server instead of treating an empty Thread as evidence.

## Current limitations

- The adapter is not the application default until sidecar supervision, cancellation, and rate-limit controls are implemented.
- Durable role Workspace records and first-use directory provisioning now exist. The application still defaults to FakeRuntime until the remaining Runtime controls are ready.
- Task cancellation and reconnect supervision remain pending beyond the verified App Server reattachment path. Approval routing is implemented for command and file-change requests with one-time decisions only; approval-waiting executions remain conservative across owner restart.
- Explicit retry handles terminal Turn failure and owner loss. Per-execution stdio remains conservative because its owner process cannot be rejoined after a backend restart. External App Server reattachment is available only when that server remains alive, identities and Workspace binding validate, and no approval request is pending. External side effects must remain idempotent.
- Custom-provider API-key authentication is verified through the isolated home. Production must move the credential source to a dedicated secret store or environment injection rather than copying a personal home.
- One local App Server process is currently owned per active execution. Process pooling is a later optimization, not an M2 correctness requirement.

## References

- [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [App Server lifecycle](https://learn.chatgpt.com/docs/app-server#lifecycle-overview)
- [App Server approvals](https://learn.chatgpt.com/docs/app-server#approvals)
- [Custom model providers](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers)
