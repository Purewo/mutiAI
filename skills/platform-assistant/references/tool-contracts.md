# Product tool contracts

## Tool classes

The Runtime bridge exposes product capabilities. Exact transport names may change, but their product semantics remain stable.

### Read operations

- List and read organizations and OrganizationSpec versions.
- Read Tasks, Assignments, approvals, Artifacts, and usage.
- Read current Runtime controls and binding summaries.
- Read versioned Runtime capability profiles and current feasibility checks.

Read operations do not require a proposed-action confirmation. They remain owner-scoped.

### Draft operations

- Create an OrganizationSpec proposal.
- Create a revised proposal from an existing version.
- Create a proposed product action for a later state transition.

Draft operations may persist a proposal or action record, but they do not publish a version or start external Runtime work.

A feasibility operation normalizes a versioned requirement set, compares it with selected profile revisions through the product validator, and persists the result. It does not require user confirmation, but only a current `feasible` result can authorize a later publication, Task submission, or Runtime start.

### Confirmed operations

- Confirm or publish an OrganizationSpec version.
- Submit a Task.
- Retry or cancel a Task.
- Accept, decline, or cancel a Runtime approval.

Every confirmed operation receives a persisted action identity and an idempotency identity from the product. Execute the exact recorded action. Reject stale targets and payload mismatches instead of silently creating a replacement.

Organization confirmation/publication and Task submission must also reference the current feasibility check identity. The backend verifies its input hashes, profile revisions, and outcome. Do not manufacture a check identity, reuse a stale result, or substitute conversational approval for a feasible result.

## Required response handling

- Treat a returned product resource as the source of truth.
- Preserve product error codes and explain them without inventing fallback values.
- On a conflict, re-read current state before proposing another action.
- On timeout or disconnect, query by action or resource identity before retrying.
- Never broaden Runtime permissions in response to an error.
- If a capability profile or feasibility tool is unavailable, return the product failure and stop the state-changing operation. Do not infer support from Runtime memory or host assumptions.

## Prohibited tool use

Do not use shell, filesystem, Git, terminal, raw database, or unrestricted HTTP tools for platform-assistant operations. If a required product capability is not exposed, report the missing capability and stop rather than bypassing the product API.
