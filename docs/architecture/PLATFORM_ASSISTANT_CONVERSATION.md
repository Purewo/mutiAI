# Platform assistant conversation architecture

Status: Implemented V1 backend baseline. Conversation persistence, resumable
Codex Threads, confirmed actions, SSE replay, and Runtime feasibility gates are
available through the public API.

## Purpose

The platform assistant is the user's system-level entry point for designing organizations, publishing confirmed OrganizationSpec versions, submitting work, and reading product progress. It uses Codex first, but product conversations and commands remain Runtime-neutral.

The product owns the conversation system. Codex supplies reasoning and tool use through an AssistantRuntimeAdapter.

Feasibility is a product law, not a prompt suggestion. The platform assistant must never publish an organization or start work that the selected Runtime cannot execute. The product therefore combines a canonical system prompt, versioned Runtime capability profiles, and a deterministic validator. The prompt explains the rule to the assistant; the validator enforces it for every caller.

## Component flow

```text
Web or future channel adapter
        |
        v
Conversation API and product database
        |
        v
PlatformAssistantService
context builder, action policy, product tool bridge
        |
        v
AssistantRuntimeAdapter
Codex first; other Runtimes remain replaceable
        |
        v
Product services
organizations, versions, tasks, approvals, Artifacts, and usage
```

LangGraph is not required for every chat turn. A simple query reads product state and returns a response. A message enters a durable workflow only when it creates a proposal, waits for confirmation, submits work, or manages another recoverable product operation.

## State ownership

| State | Owner |
| --- | --- |
| User-visible messages and attachments | Product database |
| Conversation and message ordering | Product database |
| Pending and completed product actions | Product database |
| Runtime provider, Thread, Turn, generation, and summary references | Product database |
| Hidden reasoning, internal Runtime messages, and tool activity | Runtime Thread |
| Durable action workflow and waits | Product service or replaceable orchestration layer |
| Organization, Task, approval, Artifact, and usage truth | Product database |

The product also owns Runtime capability profiles and feasibility checks. A profile describes the declared execution environment, while a check records the requirements evaluated against a particular profile revision. Codex Thread memory cannot establish or extend either record.

The product stores public conversation history because the web application and future channels must display the same messages. It does not copy Codex's complete internal transcript.

## Domain resources

### RuntimeCapabilityProfile and FeasibilityCheck

`RuntimeCapabilityProfile` is a versioned product record selected by a `RuntimeBinding`. It describes the capabilities that the binding is allowed to claim: operating-system family and architecture, headless or GUI availability, CPU and memory capacity class, GPU and accelerator support, installed tools and runtimes, network policy, external hardware or proprietary software, supported media, and workload limits. Unknown values remain unknown. The model must not assume that a local development host has the same capabilities as a production Linux Runtime.

`FeasibilityCheck` is an immutable product record containing the normalized workload requirements, affected role and binding identities, profile revision, validator version, input hashes, outcome, and findings. Its outcome is one of `feasible`, `conditional`, `blocked`, or `capability_unknown`. Only `feasible` permits the relevant state transition. Conditional, blocked, and capability-unknown results remain visible for preview and explanation but cannot be overridden by a user confirmation.

The initial capability dimensions are deliberately provider-neutral. They must be sufficient to reject Windows-only work on Linux, GUI work on headless hosts, GPU-dependent work without a declared GPU, heavy media/rendering/training without an explicit capacity profile, and work requiring undeclared tools, services, hardware, network, or media formats. The validator may use a policy catalog for known workload classes, but it must fail closed when required evidence is absent.

The same check is required at four boundaries:

1. Proposal creation, to show feasibility findings in preview.
2. Confirmation and publication, to prevent an infeasible OrganizationSpec from becoming active.
3. Task submission, against the current published spec and current binding profile.
4. Runtime start, to catch profile drift, revoked tools, or changed resource limits.

If a check is reused, its input hashes and profile revisions must still match. A successful earlier check is not proof after the binding changes.

### AssistantConversation

Represents a product-owned platform-assistant conversation.

Required facts:

- Conversation identity and owner.
- Status: active or archived.
- Runtime provider and optional Runtime Thread identity.
- Thread generation and observed compaction count.
- Tool-contract version and selected rolling summary.
- Last message and event positions.
- Creation, update, and archive timestamps.

V1 presents one active platform-assistant conversation for the single user, but the database model must not prevent later archived or additional conversations.

### AssistantMessage

Represents one user-visible message.

Required facts:

- Message identity, conversation identity, owner, and monotonic sequence.
- Role: user, assistant, or product event.
- Structured content blocks and attachment references.
- Delivery status and optional reply-to identity.
- Related organization, version, Task, approval, Artifact, or action identities.
- Creation and completion timestamps.

Runtime tool items, raw commands, hidden reasoning, and private model history are not AssistantMessage records.

### AssistantTurn

Represents one product submission to an AssistantRuntimeAdapter.

Required facts:

- Turn identity and source user message.
- Stable execution and idempotency identities.
- Runtime provider, Thread, Runtime Turn, and generation references.
- Status: queued, submitted, running, waiting, completed, failed, or cancelled.
- Requested and actual model policy snapshot, token usage, and compaction count when available.
- Final assistant message or normalized failure information.

The product may resume the recorded Thread, but Thread reuse does not prove exactly-once Turn submission.

### AssistantAction

Represents a product operation proposed through conversation.

Required facts:

- Action identity, conversation, source Turn, type, target, and versioned payload.
- Payload hash and stable idempotency identity.
- Status: proposed, confirmed, executing, completed, failed, declined, cancelled, expired, or superseded.
- Confirmation, execution, result, failure, and audit metadata. Failures persist
  a stable code, original HTTP status, structured details, and an untranslated
  fallback instead of a reader-specific translation.

Organization confirmation/publication, Task submission, Task retry/cancellation, and Runtime approval decisions require a confirmed AssistantAction. Read-only queries and proposal drafts do not.

Action list, detail, and decision responses localize `error_message` when the
record is read, using the current request's `Accept-Language`. The same persisted
failure can therefore be read in different languages without changing the
record. Responses declare `Content-Language` and vary on `Accept-Language`.

### AssistantEvent

Represents append-only product events for a conversation.

The envelope follows the existing event rules: event identity, schema version, aggregate identity, sequence, timestamp, source, correlation identity, and versioned payload. Conversation event ordering is independent from Task event ordering.

## HTTP surface

The implementation exposes resource-oriented routes under `/api/v1/assistant`:

- `POST /conversations` and `GET /conversations`.
- `GET /conversations/{conversation_id}` and `POST /conversations/{conversation_id}/archive`.
- `GET /conversations/{conversation_id}/messages` with cursor pagination.
- `POST /conversations/{conversation_id}/messages` with `Idempotency-Key`.
- `GET /turns/{turn_id}` and `POST /turns/{turn_id}/cancel`.
- `GET /conversations/{conversation_id}/actions` and `GET /actions/{action_id}`.
- `POST /actions/{action_id}/decision` with `confirm` or `decline`.
- `GET /conversations/{conversation_id}/events` with `Last-Event-ID`.

The event route returns an ordered replay batch and closes the response. Its
`retry` directive lets `EventSource` reconnect with `Last-Event-ID`, which keeps
V1 resource progress resumable without holding one API worker indefinitely.
Clients refresh messages, actions, and referenced product resources after each
event batch. The frontend must not infer action completion from streamed text.

Confirmed actions execute outside the decision request. The decision response
can therefore contain `confirmed`, `executing`, or a terminal status. The
product event stream and `GET /actions/{action_id}` provide the authoritative
completion state. Confirmed or executing actions are resumed after an API
process restart.

## Event catalog

The first implementation requires product-safe event types for:

- Conversation creation and archival.
- User-message acceptance.
- Assistant Turn submission, start, waiting, completion, failure, and cancellation.
- Assistant message creation and completion.
- Action proposal, confirmation, decline, execution, completion, and failure.
- Runtime Thread creation, resume, rotation, and owner loss.

SSE delivery may repeat. Consumers deduplicate by event identity or sequence and refresh the conversation, messages, actions, and referenced product resources after reconnecting.

## Runtime policy

One supervised Codex App Server may serve multiple product-owned Threads. V1 maps one active platform-assistant conversation to one resumable Codex Thread generation.

Platform-assistant Threads:

- Use the product-managed Codex Home.
- Never adopt an interactive user's Thread.
- Do not use a product source repository or organization development workspace as their working directory.
- Receive only product-owned tools needed by the platform assistant.
- Do not receive shell, filesystem, Git, terminal, or raw database authority.

The current product-tool bridge can list and read organizations, read a
published OrganizationSpec version, create proposal drafts, check Task
feasibility, propose confirmed actions, list and read Actions and Tasks, read
released JSON or text Artifact content up to 64 KiB, read Task token usage, and read
feasibility checks. Organization confirmation/publication, Task
submission/retry/cancel, and approval decisions remain explicit
`AssistantAction` mutations.

The Artifact content tool accepts only Task and Artifact identities. It reuses
the product's owner scope, released-state, managed-root, byte-size, and SHA-256
verification. It returns no storage path or Workspace identity, refuses binary
and oversized content without partial truncation, and leaves those files on the
controlled download path.

The canonical feasibility policy is sourced from `skills/platform-assistant/references/system-prompt.md` and the detailed rules in `skills/platform-assistant/references/feasibility-rules.md`. The Runtime adapter injects the policy for every Thread generation and records the policy version or hash on each AssistantTurn. Deployment must not rely on a compressed Thread summary to preserve this law.

The product creates a new Thread generation when the previous Thread is unavailable, exceeds the configured compaction policy, or becomes incompatible with the current prompt or tool-contract version. Rotation carries forward only product-owned messages, selected summary, and current resource references.

The current backend exposes owner-scoped feasibility records through
`/api/v1/feasibility-checks/{id}`, organization-version feasibility lists, and
Task feasibility lists. These routes expose the persisted profile revisions,
requirements, stable finding codes, and localized explanations without
exposing host paths or Runtime transcripts.

## Skill packaging

The source Skill lives at `skills/platform-assistant/`. It is versioned with the backend and validated as a normal Codex Skill.

Deployment copies or mounts the approved Skill version into the product-managed Codex Home. It never installs the Skill into a developer's personal Codex Home. The Skill defines behavior and tool policy; it does not own credentials, conversation records, or tool implementations.

## Frontend development states

Before real Runtime integration is complete, the frontend may use contract-shaped UI mocks for:

- Empty and populated conversations.
- User message queued or accepted.
- Assistant Turn running, waiting, failed, or completed.
- Streaming assistant text.
- Proposed action awaiting confirmation.
- Accepted, declined, stale, completed, and failed actions.
- SSE reconnect and duplicate-event handling.
- Feasibility preview marked feasible, conditional, blocked, or capability unknown, including the affected role, binding, reason, profile evidence, and suggested alternative.

UI mocks must remain visibly separate from captured backend Fixtures. Real Fixtures are generated only after the implemented API returns validated responses.

## Non-goals

- Reimplementing an LLM or Agent Runtime.
- Making Codex Thread history the product's conversation database.
- Giving the platform assistant unrestricted development tools.
- Using one Thread for the platform assistant and organization roles.
- Routing every chat message through LangGraph.
- Allowing conversational text alone to bypass explicit product confirmation.
- Treating a model provider or Codex Thread as evidence of host capabilities.
- Allowing a user confirmation to override a known or unknown hard feasibility failure.
