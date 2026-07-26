# Organization and Task workflow

## Design an organization

1. Read the user's existing organizations when the request may modify or duplicate one.
2. Produce a structured OrganizationSpec proposal with exactly one organization lead.
3. Keep every formal role explicit. Do not invent roles during later Task execution.
4. Return the proposal as a product version and let the UI render it from structured data.
5. Revise through a new proposal version. Do not mutate a published version.
6. Require explicit confirmation before confirm and publish transitions.
7. Do not create a workspace or Runtime Thread merely because a version was published.

## Send work to an organization

1. Read the current published OrganizationSpec and identify the requested organization.
2. Create a proposed Task action that preserves the user's request and selected organization.
3. Require confirmation before submitting work that can consume Runtime capacity or tokens.
4. Submit through the Task service with its stable idempotency identity.
5. Let the organization lead plan, delegate to existing roles, review, and summarize.
6. Query Task, Assignment, approval, Artifact, and usage resources for progress and results.

## Change an organization during active work

A new OrganizationSpec version affects future planning only after confirmation and publication. Do not rewrite the immutable specification snapshot already attached to an existing Task.

## Handle organization-lead interaction

The organization lead owns task coordination inside its organization. The platform assistant may send a new Task or query stored progress. It does not require the lead to maintain a separate reporting conversation.

Ordinary user status questions must read product records. Do not interrupt or steer an active lead Turn unless the user confirms a dedicated steering action supported by the product contract.
