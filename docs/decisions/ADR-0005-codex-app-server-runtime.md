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
- Use `workspaceWrite` for the managed workspace with network access disabled by default.
- Use `on-request` approval behavior by default. Reject unhandled App Server requests rather than silently approving them.
- Require an isolated product Codex home under the managed Runtime root. Do not write product App Server sessions into the user's interactive Codex home.
- Resolve the Windows command shim through `PATH` before starting the process. Python cannot directly launch the PowerShell `codex` shim on this host, while the resolved `codex.cmd` works.

## Verified behavior

- The Python client completed a real initialization handshake with local `codex-cli 0.145.0`.
- A real `thread/start` returned a UUID Thread ID and the isolated canonical working directory.
- The fake App Server integration verifies initialization, Thread start, Thread resume request shape, Turn start, notifications arriving before responses, terminal result extraction, and idempotent in-process submission.
- A Thread with no Turn did not survive App Server restart as a resumable rollout. Local `thread/resume` returned `no rollout found` for that empty Thread. Cross-process resume therefore requires a submitted Turn and remains an M2 acceptance item.

## Current limitations

- The adapter is not the application default until isolated authentication, event-worker supervision, and approval routing are implemented.
- Durable role Workspace records and first-use directory provisioning now exist. The application still defaults to FakeRuntime until the remaining Runtime controls are ready.
- Product approval routing, cancellation, reconnect supervision, and Runtime event workers remain pending.
- Isolated Codex authentication must be configured without adopting or polluting existing interactive sessions.
- One local App Server process is currently owned per active execution. Process pooling is a later optimization, not an M2 correctness requirement.

## References

- [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [App Server lifecycle](https://learn.chatgpt.com/docs/app-server#lifecycle-overview)
- [App Server approvals](https://learn.chatgpt.com/docs/app-server#approvals)
