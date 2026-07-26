# Nexwork current project status

Status date: 2026-07-26

This file is the primary handoff for a new AI agent or developer. Read it before selecting work. The architecture documents explain why the system is designed this way; this file identifies the active milestone, current ownership, verified starting point, next gate, and deferred scope.

## Product identity and goal

Nexwork is the product name. The repositories and Python package retain the historical working name `mutiAI` until a separate rename is planned.

Nexwork is a visual AI R&D organization system. A user works mainly through a platform assistant and organization leads. The platform assistant designs feasible organizations conversationally, presents a preview, and publishes a versioned `OrganizationSpec` only after explicit confirmation. Each formal role is a persistent product entity backed by a complete Codex Runtime context, not a one-shot LLM function node.

The first product must prove that an individual user can design an AI organization, start real work, observe responsibility and progress, and receive controlled deliverables. It does not need to prove every future editing, collaboration, provider, or deployment capability.

## Non-negotiable architecture boundaries

- The product database is the source of truth for users, organizations, roles, tasks, permissions, Runtime bindings, Workspaces, Artifacts, costs, and execution records.
- LangGraph is a replaceable organization-level workflow kernel for routing, fan-out, waiting, approval, retry, recovery, and aggregation. It does not own the product organization model.
- Codex is an external execution Runtime. It owns the detailed execution inside one bounded Assignment, including project exploration, tools, files, terminal work, code changes, and tests.
- LangGraph and Codex must not decompose, retry, or aggregate the same responsibility. Outer organization coordination belongs to the product and LangGraph; temporary execution tactics inside one Assignment belong to Codex.
- LangGraph state may store stable IDs, status, and delivery summaries. It must not copy complete Codex transcripts, hidden reasoning, or internal tool state.
- Long Runtime work follows submit, persist IDs, checkpoint, wait for events, and resume. A LangGraph node must not block for hours.
- Role Workspaces remain isolated. Cross-role delivery uses product-owned, immutable Artifacts and explicit input bindings, not another role's Thread, transcript, or Workspace.
- Managed Runtime workspaces must remain under `G:\AI\AI_private\mutiAI-runtime-workspaces`. Product source repositories under `Codex_projects` are never managed Runtime workspaces.
- Windows is the current high-performance local test host. Linux remains the production target, so OS-specific behavior stays behind adapters.
- Runtime feasibility is a deterministic product law. Prompt text or user confirmation cannot bypass missing OS, GUI, GPU, tool, network, media, hardware, or capacity requirements.

## V1 boundary

- The current first-party product is a single-user web application.
- The localhost development account is `admin` with password `123456`. This simple credential is not permitted for a non-loopback deployment.
- Every organization has exactly one organization lead. The platform assistant sits above organizations and is not an organization member.
- Organization design is preview-first. V1 uses conversational proposals and program-rendered organization previews, not a drag-and-drop editor.
- Publishing an `OrganizationSpec` does not create a Workspace, Codex Thread, or Turn. Runtime resources are created lazily when real work starts.
- V1 integrates local Codex only. Each role selects its model, reasoning effort, and security mode through a product-owned `RuntimeBinding`.
- `demo_full_access` is allowed only for the localhost demonstration. It is not a production isolation policy.
- Planned execution currently supports a strict-linear specialist chain or a pure-parallel specialist fan-out joined by organization-lead review. Mixed serial-parallel plans are deferred.
- Formal roles remain user-confirmed product entities. Runtime agents may receive dynamic Assignments but may not invent and persist new formal roles.

## Active milestone: M3 web product loop

M3 is the only active product milestone. Do not redirect work to WeChat, a drag-and-drop editor, multi-user collaboration, mixed plan topology, additional Runtime providers, or production distributed scheduling before this web loop passes acceptance.

The required browser flow is:

```text
Log in and preserve the authenticated session
-> talk to the persistent platform assistant
-> create, preview, confirm, and publish an OrganizationSpec
-> inspect the organization and role RuntimeBindings
-> submit a planned Task
-> let the organization lead produce and validate the execution plan
-> upload every declared initial input Artifact
-> start strict-linear or pure-parallel execution
-> observe Task, Assignment, PlanStep, and Runtime state through reconnectable SSE
-> preview or download released Artifacts through controlled backend URLs
-> inspect Task totals and per-Assignment Token usage
-> handle feasibility blocks, approvals, failures, retries, cancellation, and terminal results
```

SSE is a resumable change-notification channel, not the sole source of truth. The frontend reconnects with `Last-Event-ID`, deduplicates by event ID or sequence, and refreshes persisted resources after events.

## Ownership during M3

- Fable5 owns all frontend implementation, frontend repository checks, page acceptance, and real-backend browser verification, including console, network, interactions, responsive layout, SSE reconnect behavior, Artifact access, and usage presentation.
- The backend agent owns architecture, backend implementation, product rules, OpenAPI and JSON Schema contracts, fixtures, backend test coverage, API defect resolution, and cross-layer integration support.
- `Purewo/mutiAI` is authoritative for product and transport contracts. `Purewo/mutiAI-aistdio-gemini` consumes versioned snapshots and generated types; it must not redefine backend contracts by guesswork.
- A frontend problem is reported to Fable5 for correction. Backend code is changed only when the failing layer is the backend or published contract.

## Verified starting point

- Backend repository baseline: `1e2f969` (`fix: complete fake planned artifact delivery`).
- Frontend repository baseline: `9a5d07b` (`feat: add task execution observation, Artifact access, and usage`).
- M0, M1, M2, M2.1, M2.2, and M2.3 backend boundaries are complete for the local V1 slice.
- Persistent platform-assistant Conversation, Message, Turn, Action, event replay, managed Codex Thread, and feasibility-gate APIs are implemented.
- The fake planned path now creates deterministic plans, returns valid structured `AssignmentDelivery` envelopes, publishes declared Artifacts, and converges parallel branches to terminal state.
- A completed fake Task exposes released Artifact content and downloads through owner-scoped URLs, reports Task and per-Assignment usage, and ends its event sequence with `task.completed`.
- The frontend has implemented the real assistant conversation transport and the Task observation, Artifact, and usage slices. Completion still requires frontend-owned real-backend browser acceptance.

Repository commits move after this handoff. Before relying on the two baseline hashes, compare them with each repository's current `HEAD` and read newer commit messages. The product boundaries and active M3 gate remain authoritative until this file is deliberately updated.

## Immediate next gate

Fable5 runs the current frontend against the real local backend and completes M3 browser acceptance. The verification must cover authentication, platform-assistant conversation and actions, organization preview, Runtime binding display, planned Task submission, initial input upload, strict-linear and pure-parallel progress, SSE reconnect and deduplication, Artifact preview and download, usage totals, failure states, approvals, cancellation, console errors, network responses, interactions, and responsive layout.

When verification exposes a defect, isolate the failing layer. Fable5 corrects frontend implementation and page behavior. The backend agent corrects backend behavior, contracts, fixtures, or persistence defects. Do not call M3 complete from fixture-only, typecheck-only, or backend-test-only evidence.

## Next milestone and deferred scope

M4 adds personal WeChat as the first external channel only after the M3 web flow is stable. The channel routes messages to the existing platform assistant and must not duplicate organization or Task logic.

The following remain deliberately deferred:

- Drag-and-drop or infinite-canvas organization editing.
- Multi-user organization membership, invitations, and collaborative human nodes.
- Autonomous creation of persistent formal roles.
- Mixed serial-parallel execution plans.
- Multiple Agent Runtime providers.
- Production distributed scheduling, sandbox infrastructure, and large-scale Git merge automation.
- Raft-inspired Agent Inbox, Held Draft, and RuntimeHost work until the current M3 acceptance gate is complete and those concepts receive separate product decisions.

## Maintenance rule

Update this file when any of the following changes:

- Product name or active milestone.
- Frontend or backend ownership.
- Verified repository baselines.
- Immediate acceptance gate.
- Supported execution topology.
- Deferred scope promoted into active work.

Keep `AGENTS.md` and `CLAUDE.md` identical. Both files must continue to point new agents to this handoff.
