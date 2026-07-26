# Product boundaries

## Ownership

| Concern | Authority |
| --- | --- |
| User-visible conversations and messages | Product database |
| Organizations, versions, roles, tasks, permissions, costs, and Artifacts | Product database |
| Declared Runtime capabilities and feasibility check records | Product database |
| Durable workflow position, routing, and waits | Replaceable orchestration layer |
| Runtime reasoning, private context, commands, and tool activity | Codex Thread |
| Code, files, commits, and tests | Managed workspace and Git |

The platform assistant operates above organizations. It is not an organization role and must not perform specialist assignments.

## Conversation memory

The product stores user-visible messages, Runtime identifiers, selected summaries, action records, and event positions. It does not copy the complete Codex transcript or hidden tool activity.

Query product state before reporting a mutable fact. Thread context can help interpret the user, but it cannot prove that an organization was published, a Task started, or an approval was resolved.

Runtime capability claims follow the same rule. Read the current product-owned capability profile and feasibility check. Do not infer host resources from a provider, model, workspace path, or earlier successful Task.

## Runtime isolation

Platform-assistant Threads belong only to mutiAI and run through the product-managed Codex Home. Never adopt an interactive user's Thread. Never use a product source repository as a managed Runtime working directory.

The platform assistant does not require a development workspace. Its authority comes from explicitly exposed product tools, not from filesystem access.

## Replaceability

Do not encode Codex-specific Thread, Turn, tool, or compaction objects into public resource payloads beyond stable optional Runtime references. Another AssistantRuntimeAdapter must be able to replace Codex without migrating product conversations or organizations.
