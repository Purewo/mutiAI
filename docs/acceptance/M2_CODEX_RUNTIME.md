# M2 local Codex Runtime acceptance

Status: In progress. The protocol, product Workspace, isolated provider configuration, background completion-worker seams, organization-lead review boundary, explicit terminal-failure retry, conservative owner-loss recovery, product-owned approval routing, and cross-process Turn reattachment are implemented.

## Verified locally

- `codex-cli 0.145.0` completes an `initialize`/`initialized` handshake through the Python JSONL client.
- Python resolves the Windows `codex.cmd` executable instead of attempting to launch the PowerShell shim directly.
- `thread/start` receives a canonical managed `cwd` and returns a Thread ID.
- `turn/start` returns immediately with a Turn ID and does not block the LangGraph node.
- App Server notifications can arrive before a matching JSON-RPC response without corrupting request handling.
- Fake App Server tests cover Thread start, Thread resume request shape, terminal Turn extraction, and duplicate completion reads.
- A Codex task flow provisions two stable role Workspaces, persists Thread/Turn/Workspace IDs, waits, resumes both Runtime branches, and aggregates the final result.
- A second task for the same organization reuses each role Workspace and Thread, then starts a new Turn.
- A background supervisor starts only after LangGraph has checkpointed the waiting state, delivers each terminal event through the idempotent product completion boundary, and serializes checkpoint resume for parallel branches.
- Duplicate watch requests do not start duplicate workers. A completed worker closes its owned App Server session, and worker errors remain queryable in memory and as persisted product events when the database is available.
- A custom model provider using the local relay configuration and API-key auth completes a real App Server Turn from the isolated managed Codex home on Windows.
- The real Turn creates files only inside the managed Runtime workspace and does not use the user's interactive Codex session directory.
- Live `item/completed` notifications are collected before `turn/completed`; the terminal Turn may contain an empty `items` list, so final Agent summaries are extracted from completed item notifications inside the Runtime worker.
- A real API-submitted product Task fans out through LangGraph to two Codex specialist Threads, checkpoints both waits, receives two supervisor completions, persists two Runtime completion events, resumes both branches, and finishes with two ready role Workspaces and no supervisor errors.
- A real three-role API Task fans out to backend and test Codex specialists, then creates a separate organization-lead Codex review Assignment only after both specialist deliveries complete.
- The lead Runtime receives only the original request and structured specialist summaries, not Codex conversation history or tool events. Its JSON Schema requires `decision`, `final_summary`, and `issues`, and Pydantic validation owns the product contract after Runtime completion.
- A real three-role smoke completed with `accepted`, three independent Workspaces, three Thread IDs, three Turn IDs, `lead.review_requested`, `lead.review_completed`, and `task.completed`, with no supervisor errors.
- A separate real smoke returned `needs_revision` because the lead detected inconsistent specialist claims about malformed upstream status handling. The product persisted the lead decision and did not mark the Task completed.
- A terminal `failed` Turn becomes one deterministic `runtime.execution_failed` event and marks its RuntimeExecution, Assignment, and Task as failed. Ordinary task replay does not restart it.
- `POST /api/v1/tasks/{task_id}/retry` resets only failed Assignments. A Codex retry reuses the recorded Workspace and Thread, starts a new Turn, preserves successful sibling results, and prevents late parallel completions from reviving a failed Task through an older checkpoint.
- An unexpected App Server exit becomes a `runtime_owner_lost` failure instead of leaving the Assignment in `waiting`; the same explicit retry path can reuse its recorded Thread and Workspace.
- On application startup, waiting Codex executions that have no active owner in the new process become `runtime_owner_lost` failures through `runtime.supervisor`. The reconciliation is idempotent and does not implicitly start a new Turn.
- Command and file-change App Server approval requests become durable product records associated with the Task, Assignment, RuntimeExecution, Thread, Turn, item, and original JSON-RPC request ID.
- `GET /api/v1/tasks/{task_id}/approvals` lists approval records owned by the authenticated Task owner. `POST /api/v1/tasks/{task_id}/approvals/{approval_id}/decision` accepts one-time `accept`, `decline`, or `cancel` decisions.
- The Runtime worker, not the LangGraph graph, waits for a user decision. The graph remains checkpointed while the Runtime Turn is waiting.
- Approval decisions are idempotent when the same decision is repeated. A different second decision returns a conflict, and a decision without an active Runtime waiter is rejected.
- Approval lifecycle changes persist as `runtime.approval_requested` and `runtime.approval_resolved` product events. Approval state is not copied into LangGraph State.
- Backend shutdown cancels active approval requests before stopping Runtime supervisors. Startup recovery cancels pending approval records whose Runtime owner was lost; the failed execution must use the explicit retry path.
- A long-lived external App Server can keep a Turn running after the API process disconnects. A new adapter reconnects to the same loopback WebSocket or Unix socket, calls `thread/resume`, validates Thread and Workspace identity, and watches the original Turn without calling `turn/start`.
- If the Turn completed while the API process was disconnected, recovery reads the persisted Thread history and delivers its terminal result through the same idempotent supervisor boundary.
- Recovery emits `runtime.execution_reconnected` before the supervisor consumes the terminal Turn. Repeated startup reconciliation does not create a second worker for an active execution.
- The per-execution stdio App Server remains intentionally conservative: when its owner process exits there is no rejoinable owner, so the product records `runtime_owner_lost` and requires explicit retry.
- Windows development uses a loopback WebSocket endpoint for a long-lived App Server. Linux production should prefer a Unix socket. Remote unauthenticated WebSocket endpoints are rejected by the adapter.
- `RUNTIME_PROVIDER=codex` assembles the Codex Adapter from application settings, requires the configured App Server `/readyz` check during startup, and keeps the sidecar outside the FastAPI lifespan. The default provider remains `fake` until the local sidecar and production supervision are operationalized.
- V1 intentionally excludes `acceptForSession`, exec-policy amendments, and network-policy amendments. It never broadens approval policy beyond the current request.
- `bootstrap_codex_home.py` copies only `config.toml` and `auth.json`. It never copies `sessions`, history, state databases, or existing Threads.

## Not yet accepted

- Reattach an approval-waiting Turn across owner loss. V1 requires explicit retry until the App Server guarantees redelivery of an unanswered server request to the new client connection.
- Handle task cancellation, provider rate limits, external App Server process supervision, and recovery after the App Server itself exits.
- Make the Codex adapter the application default after sidecar supervision, cancellation, and rate-limit controls are complete.

The web frontend does not need changes for this milestone. Its existing task and SSE contracts remain product-level and do not expose App Server protocol details.
