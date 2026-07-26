# Nexwork current project status

Status date: 2026-07-27

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

M3 implementation is complete for the current frontend slice, and its reference completed Task has passed the main browser flow. M3 acceptance remains active until the three deterministic Runtime scenarios below pass through the real frontend. Do not redirect work to WeChat, a drag-and-drop editor, multi-user collaboration, mixed plan topology, additional Runtime providers, or production distributed scheduling before this web loop passes acceptance.

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

- Backend repository baseline: `205a845` (`fix: localize persisted assistant action failures`).
- Frontend repository baseline: `b898175` (`fix: make graphs and long identifiers usable on narrow screens`).
- M0, M1, M2, M2.1, M2.2, and M2.3 backend boundaries are complete for the local V1 slice.
- Persistent platform-assistant Conversation, Message, Turn, Action, event replay, managed Codex Thread, and feasibility-gate APIs are implemented.
- The fake planned path now creates deterministic plans, returns valid structured `AssignmentDelivery` envelopes, publishes declared Artifacts, and converges parallel branches to terminal state.
- A completed fake Task exposes released Artifact content and downloads through owner-scoped URLs, reports Task and per-Assignment usage, and ends its event sequence with `task.completed`.
- The frontend has implemented the real assistant conversation transport and the Task observation, Artifact, and usage slices on `feat/m3-frontend-foundation`. The reference Task `249abdb4` passed the main completed parallel-flow browser acceptance.
- The backend now provides isolated loopback harnesses in `docs/acceptance/M3_RUNTIME_SCENARIOS.md` for `wait-cancel`, `needs-revision`, and `approval` states. The harnesses do not add public test endpoints or production switches.
- Planned Task cancellation now converges the Task, current execution plan, and every unfinished plan step to `cancelled`; completed specialist Assignments remain completed when their result was already observed.
- Public approval responses retain the user-visible `command` but no longer expose `cwd` or opaque Runtime detail objects. Internal audit records retain those values.
- Account self-service now exposes `PATCH /api/v1/auth/me` and `POST /api/v1/auth/password`. Password changes preserve the current browser session and revoke other active sessions.
- Runtime responses now expose persisted timing facts and derived queue, run, wall, dependency-wait, and active durations for bottleneck analysis.
- `lead.review` now receives product-owned execution evidence for plan order, Assignment ownership, Artifact bindings, validation, and Runtime timing without undeclared upstream file contents.
- The platform assistant now has a persisted Task-feasibility preview tool, owner-scoped Action list/read tools, and a controlled released JSON/text Artifact content reader capped at 64 KiB. It never receives arbitrary URLs, storage paths, Workspace IDs, or binary content through that reader.
- Failed or terminal Assistant Actions can be proposed again with a new Action and idempotency identity; active duplicate proposals remain deduplicated.
- Assistant Action failures persist stable codes, original status codes, and structured details. Action list, detail, and decision responses localize `error_message` for each reader's `Accept-Language`; existing English fallback records no longer force an English UI.
- The first real browser invoice run (`d608a67b-0542-4004-bd4f-588b4d4b7f50`) validated the complete four-Artifact linear chain and workbook values but correctly returned `needs_revision` because the previous review packet lacked execution evidence. That historical Task remains unchanged as a regression record.
- The full backend suite passes with `172 passed`, and Ruff passes for `src`, `tests`, and `scripts` after the review-evidence, timing, platform-assistant read-path, and persisted Action localization fixes.

Repository commits move after this handoff. Before relying on the two baseline hashes, compare them with each repository's current `HEAD` and read newer commit messages. The product boundaries and active M3 gate remain authoritative until this file is deliberately updated.

## Immediate next gate

Fable5 first refreshes the frontend snapshot from the review-evidence/timing backend baseline and displays the returned per-role timing fields. Then run a new real Codex invoice Task and confirm the same four-Artifact chain reaches `completed`, with the lead using product execution evidence rather than undeclared upstream files. After that, complete the remaining M3 browser acceptance with the loopback `wait-cancel`, `needs-revision`, and `approval` scenarios. The verification must cover authentication, platform-assistant conversation and actions, organization preview, Runtime binding display, planned Task submission, initial input upload, strict-linear and pure-parallel progress, SSE reconnect and deduplication, Artifact preview and download, usage totals and timing, failure states, approvals, cancellation, console errors, network responses, interactions, and responsive layout.

When verification exposes a defect, isolate the failing layer. Fable5 corrects frontend implementation and page behavior. The backend agent corrects backend behavior, contracts, fixtures, or persistence defects. Do not call M3 complete from fixture-only, typecheck-only, or backend-test-only evidence.

The next assistant-specific acceptance must use the real platform-assistant conversation: preflight a Task through the feasibility tool before proposing `task.submit`, query a failed Action before creating a replacement, and read a small released JSON Artifact through the content tool. Compare every reported value with the product database and confirm that unsupported or oversized content is refused without guessing.

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
