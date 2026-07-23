# Frontend contract workflow

## Repository roles

- `Purewo/mutiAI` is authoritative for product semantics, backend behavior, OpenAPI, JSON Schema, and event schemas.
- `Purewo/mutiAI-aistdio-gemini` is authoritative for the web frontend implementation and frontend-specific design documentation.

The two repositories do not independently define the same resource shapes.

## Contract flow

1. Define or update a product contract in `mutiAI/contracts`.
2. Review breaking changes before frontend work begins.
3. Generate a TypeScript client where practical.
4. Copy or publish a versioned contract snapshot to `mutiAI-aistdio-gemini/contracts`.
5. Record the source core commit in the snapshot metadata.
6. Give Gemini a bounded frontend task referencing that contract version.
7. Gemini submits a feature branch or pull request.
8. Codex checks out the frontend change locally, runs it against the real backend, and verifies the result in a browser.

## Contract categories

- OpenAPI for HTTP operations, authentication, errors, pagination, and idempotency headers.
- JSON Schema for stable product documents such as `OrganizationSpec` and structured patches.
- Event schemas for chat streaming, task progress, Agent status, Runtime events, artifacts, and reconnect positions.
- Valid fixtures for empty, loading, active, waiting, failed, completed, and permission-denied UI states once those states are formally defined.

OpenAPI does not replace event documentation. The event catalog must define identity, aggregate identity, ordering or cursor position, timestamp, schema version, event type, and payload.

## Gemini task packet

Every Gemini frontend task should include:

- User-facing goal.
- Routes and screens in scope.
- Allowed files or directories.
- Contract source commit.
- Generated client or contract snapshot.
- Valid fixtures.
- Interaction and responsive behavior.
- Loading, empty, error, and reconnect expectations.
- Acceptance checks.
- Explicit out-of-scope items.

Gemini must not invent backend fields, status names, permission behavior, or API endpoints. Unspecified behavior becomes a question or a visual placeholder, not a fabricated contract.

## Integration gate

A Gemini commit is a candidate implementation, not a completed delivery. Completion requires local dependency installation, type checking, automated tests, real-backend integration, browser console and network inspection, responsive checks, and core interaction verification.
