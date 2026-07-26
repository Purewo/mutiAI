# Platform assistant conversation architecture

Status: Accepted design baseline. Implementation and versioned public contracts remain pending.

## Purpose

The platform assistant is the user's system-level entry point for designing organizations, publishing confirmed OrganizationSpec versions, submitting work, and reading product progress. It uses Codex first, but product conversations and commands remain Runtime-neutral.

The product owns the conversation system. Codex supplies reasoning and tool use through an AssistantRuntimeAdapter.

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

The product stores public conversation history because the web application and future channels must display the same messages. It does not copy Codex's complete internal transcript.

## Domain resources

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
- Confirmation, execution, result, failure, and audit metadata.

Organization confirmation/publication, Task submission, Task retry/cancellation, and Runtime approval decisions require a confirmed AssistantAction. Read-only queries and proposal drafts do not.

### AssistantEvent

Represents append-only product events for a conversation.

The envelope follows the existing event rules: event identity, schema version, aggregate identity, sequence, timestamp, source, correlation identity, and versioned payload. Conversation event ordering is independent from Task event ordering.

## Initial HTTP surface

The implementation should expose resource-oriented routes under `/api/v1/assistant`:

- Create and list conversations.
- Read one conversation.
- List its messages with cursor pagination.
- Submit a user message with `Idempotency-Key`.
- Read one AssistantTurn.
- Stream resumable conversation events with `Last-Event-ID`.
- Read pending actions.
- Submit an accept or decline decision for one action.
- Cancel a non-terminal AssistantTurn.

The exact payloads enter OpenAPI only when the persistence and service invariants are implemented and tested. The frontend must not infer action completion from streamed text alone.

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

The product creates a new Thread generation when the previous Thread is unavailable, exceeds the configured compaction policy, or becomes incompatible with the current prompt or tool-contract version. Rotation carries forward only product-owned messages, selected summary, and current resource references.

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

UI mocks must remain visibly separate from captured backend Fixtures. Real Fixtures are generated only after the implemented API returns validated responses.

## Non-goals

- Reimplementing an LLM or Agent Runtime.
- Making Codex Thread history the product's conversation database.
- Giving the platform assistant unrestricted development tools.
- Using one Thread for the platform assistant and organization roles.
- Routing every chat message through LangGraph.
- Allowing conversational text alone to bypass explicit product confirmation.
