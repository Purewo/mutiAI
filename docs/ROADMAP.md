# mutiAI V1 implementation roadmap

Status: Active implementation plan. The milestones below are the working order for the first product vertical slice.

## Guiding rule

Build the smallest complete path through the system before expanding individual modules:

```text
admin login
  → OrganizationSpec proposal and publication
  → task submission
  → LangGraph orchestration
  → Runtime Adapter
  → persisted events and progress
  → organization-lead review and summary
```

The first release proves the product's core coordination value. It does not attempt to complete the long-term low-code editor, multi-user collaboration, or distributed Runtime platform.

## M0. Freeze the minimum contracts

Status: Completed for the initial vertical-slice boundary.

Deliverables:

- Domain entities and ownership rules.
- OrganizationSpec version lifecycle.
- Task, Assignment, RuntimeExecution, Workspace, and Artifact boundaries.
- Product state versus LangGraph state versus Codex Thread state.
- Task and Assignment state transitions.
- First HTTP API surface.
- First event envelope and SSE rules.
- Idempotency and recovery rules.
- Technology decision record.
- First vertical-slice acceptance checklist.

Exit condition: the backend can be scaffolded without inventing resource ownership, status semantics, or event shapes.

Reference documents:

- [M0 domain model](product/M0_DOMAIN_MODEL.md)
- [M0 API and event boundary](architecture/API_EVENT_BOUNDARY.md)
- [V1 technology decision](decisions/ADR-0002-v1-technology-stack.md)
- [M1 acceptance checklist](acceptance/M1_VERTICAL_SLICE.md)

## M1. Build the backend walking skeleton

Status: Completed. The walking skeleton includes authentication, OrganizationSpec publication, idempotent task submission, LangGraph `Send` fan-out, FakeRuntime execution, persisted events, SSE replay, failed-branch recovery, idempotent event-triggered resume for one or more waiting Runtime branches, and canonical managed-workspace enforcement without eager provisioning.

Implement only the minimum product path:

- Seed a development-only `admin` account.
- Login and current-user endpoint.
- Create, validate, preview, and publish an OrganizationSpec version.
- Persist an organization with one mandatory organization lead.
- Create a task for a published organization.
- Run a LangGraph workflow using a FakeRuntimeAdapter.
- Persist task and assignment events.
- Expose task state and an SSE event stream.
- Verify restart and resume behavior without duplicate task submission.

The FakeRuntimeAdapter is a test seam. It proves the product database, orchestration, and frontend event contract before external Codex behavior is introduced.

## M2. Replace the fake Runtime with local Codex

Status: Completed for the V1 local Runtime boundary. The local `codex-cli 0.145.0` protocol client, non-blocking Runtime submission, background completion supervision, durable role Workspaces, Thread reuse, parallel branch recovery, organization-lead review, explicit retry, conservative owner-loss handling, product-owned approvals, cross-process Turn reattachment, external sidecar supervision, task cancellation, Provider capacity normalization, database-backed concurrency, and product token accounting are covered. The remaining approval-recovery and Provider-policy constraints are explicit deployment gates, not hidden incomplete behavior.

Implement the local Windows Codex adapter through the App Server boundary:

- App Server connection initialization.
- Thread start and resume.
- Turn start and streamed notifications.
- Stable `execution_id` and Runtime job idempotency.
- Dedicated Runtime workspace allocation.
- Thread-to-workspace binding.
- Completion, failure, interruption, reconnect, and cancellation handling.
- Cross-process reconnect through a long-lived App Server endpoint. Rejoin the recorded Thread with `thread/resume`, observe the original Turn, and never issue a replacement `turn/start` during recovery.
- Run the App Server as an independent sidecar. The API checks `/readyz` and reconnects to it during startup; API shutdown closes only product client connections.
- Supervise the local sidecar outside FastAPI with a readiness gate, bounded exponential restart backoff, and a finite restart budget. A sidecar restart restores availability; an execution disconnected by the process exit remains explicit-retry work and is never replayed automatically.
- Product-owned command and file-change approval requests, one-time user decisions, and normalized approval events.
- Provider capacity checks before `turn/start`. Treat an explicit limit as unavailable and preserve unsupported custom-relay responses as `unknown` under a documented fail-open policy.
- Product-owned concurrency admission. Checkpoint branches that exceed the configured limit and resume the oldest eligible waiter when capacity is released.
- Product-owned token budgets. Reserve before submission, settle terminal executions once, and conservatively charge the reservation when Runtime usage is unavailable.
- Normalized `thread/tokenUsage/updated` accounting for one Turn. Keep account-level usage summaries outside the product attribution ledger.
- `GET /api/v1/runtime/controls` for the current owner/provider policy, active count, token totals, and last observed Provider capacity signal.
- One organization lead delegating to two existing specialist roles.
- Organization-lead review with a required structured decision, final summary, and issue list. The lead can complete a Task as `completed` or return it as `needs_revision` for user-directed follow-up.
- Product-safe event summaries and organization-lead delivery summaries.

Do not use `mutiAI` or `mutiAI-aistdio-gemini` as Runtime working directories. Use only the managed Runtime root documented in [system boundaries](architecture/SYSTEM_BOUNDARIES.md).

## M2.1. Add role Runtime policy and Thread lifecycle

Status: Completed for the first-week local demo boundary.

- Product-owned Runtime bindings select a role's model, reasoning effort, and named security mode.
- RuntimeExecution freezes the resolved policy and App Server-reported model for audit and retry stability.
- The localhost demo defaults to Full Access without approval prompts. Production and non-loopback bindings reject that mode.
- Restricted mode preserves product-owned command and file approval requests.
- Context compactions are counted without copying Thread history.
- An optional explicit threshold rotates the next Thread while preserving its role Workspace and last delivery summary.
- Runtime binding and Task response fields are exported through the authoritative OpenAPI snapshot.

Reference: [M2.1 Runtime policy acceptance](acceptance/M2_1_RUNTIME_POLICY.md).

## M2.2. Add dependency plans and Artifact handoff

Status: Completed for the first-week local Codex boundary (2026-07-25). The product persists immutable plans and Artifacts, runs a recoverable organization-lead planning boundary, accepts controlled initial input Artifacts, runs the strict-linear dependency scheduler, materializes exact input bindings, and exposes the resulting state. The real invoice acceptance run completed with the user-provided image, structured delivery envelopes, isolated role Workspaces, and a validated final XLSX Artifact.

- Persist an immutable `TaskExecutionPlan` with validated role references and step dependencies.
- Implement a generic strict-linear scheduler before extending the same representation to parallel DAG branches.
- Create dependent Assignments only after required upstream Artifacts are released.
- Add product-owned Artifact metadata, validation, immutable publication, and downstream input bindings.
- Keep role Workspaces isolated and move deliverables through a Task Artifact store.
- Replace unstructured specialist file claims with structured Runtime delivery envelopes.
- Scope each specialist Assignment to declared inputs instead of copying the full original request to every role.
- Expose plan and Artifact state through stable product API and event contracts.
- Keep `legacy` fan-out compatibility while `planned` Tasks use `lead.plan`, explicit input upload, and `/start`.
- Support separate `{task_id}:planning` and `{task_id}:linear` LangGraph checkpoint threads with external Runtime resume.
- Run the invoice workflow with `gpt-5.5`, `medium`, and the localhost Full Access demonstration policy. The run completed as `lead.plan -> content_extractor -> excel_builder -> currency_translator -> lead.review`; all five Assignments and four plan steps reached `completed`, and the final workbook contained the fixed rate and validated USD values.

References:

- [Task execution plans and Artifact handoff](architecture/TASK_PLAN_ARTIFACT_HANDOFF.md)
- [M2.2 linear Artifact handoff acceptance](acceptance/M2_2_LINEAR_ARTIFACT_HANDOFF.md)

## M2.3. Add parallel Artifact delivery and product result access

Status: Completed on 2026-07-25. Ready for frontend integration.

- Preserve strict-linear planned execution and add a pure parallel specialist fan-out followed by organization-lead review.
- Validate that every parallel specialist publishes declared Artifacts and that lead review depends on every specialist and receives every released output.
- Reject mixed serial-parallel plan shapes until a later milestone.
- Preserve exact Artifact publication, materialization, recovery, retry, and event semantics across parallel branches.
- Add an authenticated Artifact content endpoint for inline reads and downloads without exposing host filesystem paths.
- Add Task-level token usage totals and per-Assignment breakdowns from the existing product-owned Runtime ledger.
- Export the updated OpenAPI contract and pass mixed-DAG, access-control, integrity, usage, and regression gates.

Reference: [M2.3 parallel Artifact, result access, and Task usage plan](architecture/M2_3_PARALLEL_ARTIFACT_ACCESS_USAGE.md).

## M3. Integrate the web frontend

Status: Ready for frontend integration. Frontend task visualization must consume plan, Artifact, and usage resources instead of the temporary unstructured fan-out shape.

Gemini implements bounded frontend tasks against versioned contracts. The integration gate is:

- Generated or reviewed TypeScript client.
- Real backend integration.
- Loading, empty, error, reconnect, and terminal states.
- Browser console and network verification.
- Organization preview and task progress verification.

The active frontend repository is `Purewo/mutiAI-aistdio-gemini`.

The first bounded Gemini task is defined in [M3 frontend task packet](collaboration/M3_FRONTEND_TASK_PACKET.md) and targets backend contract commit `356ae35`.

## M4. Add personal WeChat

Status: Pending stable web task flow.

Add the first external channel through an adapter that routes messages to the user's platform assistant. Do not duplicate organization or task logic inside the channel integration.

## Deliberately deferred

- Drag-and-drop organization editing.
- Infinite canvas editing.
- Organization members, invitations, and collaborative human nodes.
- Autonomous creation of persistent formal roles.
- Multiple Runtime providers.
- Production distributed scheduling and sandbox infrastructure.
- A complete knowledge-base product.
- Large-scale parallel Git merge automation.
- Long-term Artifact retention and Git-reference policies beyond the M2.2 local Task Artifact store.
