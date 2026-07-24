# M2 local Codex Runtime acceptance

Status: In progress. The protocol, product Workspace, isolated provider configuration, background completion-worker seams, organization-lead review boundary, and explicit terminal-failure retry are implemented. Approval routing and cross-process Turn recovery remain.

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
- `bootstrap_codex_home.py` copies only `config.toml` and `auth.json`. It never copies `sessions`, history, state databases, or existing Threads.

## Not yet accepted

- Route command/file approvals and persist approval decisions.
- Recover a real Turn after App Server process or backend restart.
- Handle cancellation, reconnect, provider rate limits, and process supervision.
- Make the Codex adapter the application default after the approval and recovery controls are complete.

The web frontend does not need changes for this milestone. Its existing task and SSE contracts remain product-level and do not expose App Server protocol details.
