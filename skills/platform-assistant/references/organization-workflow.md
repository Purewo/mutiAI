# Organization and Task workflow

## Design an organization

1. Read the user's existing organizations when the request may modify or duplicate one.
2. Produce a structured OrganizationSpec proposal with exactly one organization lead.
3. Keep every formal role explicit. Do not invent roles during later Task execution.
4. Normalize each role's workload requirements and run the product feasibility validator against its selected Runtime binding and capability profile.
5. Return the proposal and feasibility findings as product records and let the UI render them from structured data. Conditional, blocked, and capability-unknown proposals remain preview-only.
6. Revise through a new proposal version. Do not mutate a published version.
7. Require a current feasible check and explicit confirmation before confirm and publish transitions. Confirmation cannot override a hard feasibility failure.
8. Do not create a workspace or Runtime Thread merely because a version was published.

## Send work to an organization

1. Read the current published OrganizationSpec and identify the requested organization.
2. Normalize the Task's workload requirements and run the product feasibility validator against every affected role and current capability profile.
3. For a blocked or unknown result, explain the mismatch and alternatives without creating a Runtime-consuming action.
4. Create a proposed Task action only for a feasible request. Preserve the user's request, selected organization, requirement summary, and feasibility check identity.
5. Require confirmation before submitting work that can consume Runtime capacity or tokens.
6. Submit through the Task service with its stable idempotency identity. The Task service revalidates immediately before Runtime start.
7. Let the organization lead plan, delegate to existing roles, review, and summarize.
8. Query Task, Assignment, approval, Artifact, and usage resources for progress and results.

## Change an organization during active work

A new OrganizationSpec version affects future planning only after confirmation and publication. Do not rewrite the immutable specification snapshot already attached to an existing Task.

## Handle organization-lead interaction

The organization lead owns task coordination inside its organization. The platform assistant may send a new Task or query stored progress. It does not require the lead to maintain a separate reporting conversation.

Ordinary user status questions must read product records. Do not interrupt or steer an active lead Turn unless the user confirms a dedicated steering action supported by the product contract.
