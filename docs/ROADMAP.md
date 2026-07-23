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

Status: In progress.

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

Status: Pending M0 review.

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

Status: Pending M1.

Implement the local Windows Codex adapter through the App Server boundary:

- App Server connection initialization.
- Thread start and resume.
- Turn start and streamed notifications.
- Stable `execution_id` and Runtime job idempotency.
- Dedicated Runtime workspace allocation.
- Thread-to-workspace binding.
- Completion, failure, interruption, reconnect, and cancellation handling.
- One organization lead delegating to two existing specialist roles.
- Event summaries, artifact records, and delivery summaries.

Do not use `mutiAI` or `mutiAI-aistdio-gemini` as Runtime working directories. Use only the managed Runtime root documented in [system boundaries](architecture/SYSTEM_BOUNDARIES.md).

## M3. Integrate the web frontend

Status: Pending M0 contract snapshots and M1 APIs.

Gemini implements bounded frontend tasks against versioned contracts. The integration gate is:

- Generated or reviewed TypeScript client.
- Real backend integration.
- Loading, empty, error, reconnect, and terminal states.
- Browser console and network verification.
- Organization preview and task progress verification.

The active frontend repository is `Purewo/mutiAI-aistdio-gemini`.

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

