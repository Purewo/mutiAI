# M3 Runtime browser acceptance scenarios

Status: Implemented as loopback-only development harnesses.

These scenarios create deterministic backend state for the three M3 browser paths that a millisecond-scale default FakeRuntime cannot keep observable: SSE reconnect plus cancellation, organization-lead `needs_revision`, and a product-owned Runtime approval.

The harness does not add public test endpoints. Each scenario uses an isolated database below `var/m3-acceptance/` and managed Runtime Workspaces below the configured Runtime root. Non-default FakeRuntime scenarios are rejected in production configuration.

## Run a scenario

Stop the normal backend on port 8000. Start one scenario from the backend repository:

```powershell
uv run python scripts/run_m3_acceptance_backend.py --scenario wait-cancel
```

In another terminal, seed its organization and Task:

```powershell
uv run python scripts/seed_m3_acceptance_task.py --scenario wait-cancel
```

The seed command prints the organization ID, Task ID, and persisted status. Point the existing frontend development proxy at this loopback backend and log in with the configured development account. Run only one scenario backend on a port at a time.

Available scenarios:

- `wait-cancel`: A planned pure-parallel Task leaves exactly one specialist Runtime execution waiting while the sibling completes. Use this stable window to interrupt the browser connection, reconnect with `Last-Event-ID`, verify event deduplication, and cancel the Task through the public control API.
- `needs-revision`: A planned Task completes specialist Artifact delivery, then the organization lead returns a deterministic `needs_revision` decision and issue list.
- `approval`: The backend uses the protocol-level test App Server through the normal `CodexRuntimeAdapter`. A legacy Task creates a command approval for `python -m pytest`. Seed a new Task for each `accept`, `decline`, or `cancel` decision.

Start or seed the other scenarios by replacing the scenario argument:

```powershell
uv run python scripts/run_m3_acceptance_backend.py --scenario needs-revision
uv run python scripts/seed_m3_acceptance_task.py --scenario needs-revision

uv run python scripts/run_m3_acceptance_backend.py --scenario approval
uv run python scripts/seed_m3_acceptance_task.py --scenario approval
```

## Verify wait, reconnect, and cancellation

1. Open the printed Task in the frontend and confirm its persisted status is `waiting`.
2. Record the last rendered event ID.
3. Use browser network controls to disconnect for at least one frontend reconnect interval, then restore connectivity.
4. Confirm the next request includes `Last-Event-ID` and previously rendered event IDs do not produce duplicate rows.
5. Cancel the Task from the frontend.
6. Confirm the Task, waiting Assignment, Runtime execution, plan, and unfinished step converge to their contracted cancelled states. The completed sibling remains completed.
7. Confirm `task.cancellation_requested` and one terminal `task.cancelled` event remain queryable after the event response ends.

## Verify needs revision

1. Open the printed Task and refresh it from the persisted resource.
2. Confirm the Task and execution plan show `needs_revision`, not `failed` or `completed`.
3. Confirm the lead summary is `The delivery needs a user-directed revision.` and the issue list contains `The test evidence is incomplete.`.
4. Confirm released specialist Artifacts and their completed Assignments remain visible.
5. Confirm the terminal event is `task.needs_revision` and remains queryable after reconnect.

## Verify approval decisions

1. Open the printed Task and its pending approval.
2. Confirm the command is visible so the user can make an informed decision.
3. Confirm the product UI does not display a host `cwd` or construct a host path.
4. Submit one decision and confirm the approval resource and Task events converge without duplicate resolution events.
5. Seed a fresh Task before testing another decision because approval decisions are immutable and idempotent.

The approval harness exercises the actual product approval coordinator and `CodexRuntimeAdapter` server-request boundary. It does not claim to validate a real model's decision quality or operating-system sandbox enforcement.

## Restore normal development

Stop the scenario backend and restart the normal backend configuration. Scenario databases and managed Workspaces remain isolated from the normal development database. Remove them only through an explicit, separately verified cleanup operation.
