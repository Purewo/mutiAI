# M2 local Codex Runtime acceptance

Status: In progress. The protocol, product Workspace, and background completion-worker seams are implemented. Isolated authentication, approval routing, real streamed-event validation, and real Turn recovery remain.

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

## Not yet accepted

- Configure isolated Codex authentication without adopting the user's interactive Codex home.
- Run a real model Turn in an isolated managed workspace.
- Validate the completion worker against real streamed App Server notifications.
- Route command/file approvals and persist approval decisions.
- Recover a real Turn after App Server process or backend restart.
- Handle cancellation, reconnect, provider rate limits, and process supervision.

The web frontend does not need changes for this milestone. Its existing task and SSE contracts remain product-level and do not expose App Server protocol details.
