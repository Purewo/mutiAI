# V1 product definition

Status: Initial agreed boundary, before application implementation.

## Product goal

mutiAI enables an individual user to design and operate persistent AI organizations through conversation. The platform assistant manages the user's organizations. Each organization has a mandatory organization lead that plans, delegates, checks, and summarizes work performed by existing specialist roles.

The long-term product may become a low-code infinite-canvas organization editor. V1 validates conversational organization design and real task execution before building that editor.

## V1 experience

The first-party interface uses a left navigation area and a main content area. The initial navigation contains:

- Assistant.
- Organization management.
- Channel connections.
- Knowledge base placeholder.
- Personal center.

Personal WeChat is the first external messaging channel. The first-party application itself is web-only.

## Platform assistant

The platform assistant is the user's system-level manager. In V1 it may:

- Discuss organization needs with the user.
- Produce one or more structured organization proposals.
- Revise a proposal through conversation.
- Request explicit confirmation before publishing.
- Create and manage organizations owned by the user.
- Query stored task and progress information for any owned organization.
- Send a task to an organization lead on the user's behalf.

The platform assistant does not need organization leads to send conversational status reports upward. Product services store progress, and the assistant queries that data when needed.

## Organization model

- Use `organization` consistently. Do not use `department` as a synonym.
- Every organization has exactly one organization lead.
- The organization lead coordinates the organization and does not represent the platform assistant.
- Specialist roles are persistent product entities selected through a confirmed organization definition.
- V1 may assign dynamic work to existing roles but does not allow agents to create persistent formal roles autonomously.

## Organization lifecycle

The agreed V1 lifecycle is conceptual rather than a frozen state-machine contract:

1. The user describes an organization to the platform assistant.
2. The assistant produces one or more structured proposals.
3. The web application renders each proposal programmatically as an organization diagram. It must not use image generation.
4. The user revises or confirms a proposal.
5. Confirmation publishes an `OrganizationSpec` version.
6. Publishing alone does not initialize workspaces, Codex threads, or turns.
7. Runtime resources are created lazily when the user starts real work.

The exact version, patch, and publication data model remains an architecture task.

## Task experience

After publication, the organization detail page includes its organization diagram, role status, task progress, and a conversation entry for the organization lead. When work starts, the user must be able to see which role is executing which assignment and whether the overall task is still active, waiting, failed, or complete. Exact status names remain part of the task-state design and are not frozen by this document.

## Account boundary

- Product resources are isolated by an owning user.
- Each organization has one owner in V1.
- V1 has no organization membership, invitations, shared workspaces, or collaborative human nodes.
- The local development environment starts with an `admin` account.
- A simple development password is allowed only on localhost and must not be committed or reused in production.

## Runtime boundary

- V1 integrates local Codex only.
- Formal roles use persistent Codex Runtime context rather than one-shot model calls.
- Runtime resources are initialized after real work starts, not when an organization proposal is published.
- Windows is the first development host.
- Linux is the production target and receives a compatibility validation after the first Windows vertical slice.

## V1 acceptance direction

- A user can log in and talk to the platform assistant.
- The assistant can create multiple programmatically rendered organization proposals.
- The product enforces the mandatory organization lead rule.
- A confirmed proposal appears in organization management.
- The user can open an organization, inspect its structure, and talk to its lead.
- Task execution exposes role-level progress instead of leaving the user waiting without feedback.
- Personal WeChat can connect by QR code and continue the user's platform-assistant conversation.
- The personal center supports a minimal name and password update flow.

## Explicitly outside V1

- Drag-and-drop organization editing.
- Infinite canvas editing.
- Multi-user organization collaboration.
- Membership and invitations.
- Autonomous creation of persistent roles by agents.
- Multiple Agent Runtime providers.
- Production-scale sandboxing and distributed Runtime infrastructure.
- A complete first-party knowledge-base implementation.
