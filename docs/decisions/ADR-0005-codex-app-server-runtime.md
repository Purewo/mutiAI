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
- Set `clientUserMessageId` to the product `execution_id` when starting a Turn.
- Return `waiting` with the App Server Thread ID, Turn ID, Runtime job ID, and product Workspace ID as soon as submission succeeds. Do not wait for `turn/completed` inside LangGraph.
- Consume streamed notifications in a Runtime worker. Convert a terminal Turn into one deterministic product Runtime event, persist it, then resume LangGraph through the existing idempotent completion boundary.
- Treat a `turn/completed` notification with status `failed` or `interrupted` as a terminal Runtime failure. Persist the RuntimeExecution, Assignment, and Task failure without resuming the graph automatically.
- Retry only through an explicit product command. Reset failed Assignments, reuse their recorded Workspace and Thread, start a new Turn, and keep completed sibling Assignments unchanged.
- Use `workspaceWrite` for the managed workspace with network access disabled by default.
- Use `on-request` approval behavior by default. Reject unhandled App Server requests rather than silently approving them.
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
- The local relay rejected an optional lead schema when its default-valued `issues` property was not listed as required. The product contract now requires every lead response property, matching the relay's `response_format` validation rules.
- A Thread with no Turn did not survive App Server restart as a resumable rollout. Local `thread/resume` returned `no rollout found` for that empty Thread. Cross-process resume therefore remains an M2 acceptance item.

## Current limitations

- The adapter is not the application default until approval routing and recovery controls are implemented.
- Durable role Workspace records and first-use directory provisioning now exist. The application still defaults to FakeRuntime until the remaining Runtime controls are ready.
- Product approval routing, cancellation, and reconnect supervision remain pending.
- Explicit retry handles terminal Turn failure in one live backend process. Recovery after App Server or backend restart remains pending.
- Custom-provider API-key authentication is verified through the isolated home. Production must move the credential source to a dedicated secret store or environment injection rather than copying a personal home.
- One local App Server process is currently owned per active execution. Process pooling is a later optimization, not an M2 correctness requirement.

## References

- [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [App Server lifecycle](https://learn.chatgpt.com/docs/app-server#lifecycle-overview)
- [App Server approvals](https://learn.chatgpt.com/docs/app-server#approvals)
- [Custom model providers](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers)
