# Authoritative contracts

This directory will contain the product's source contracts after the architecture review freezes their first versions.

Planned categories:

- `openapi/` for HTTP APIs.
- `schemas/` for product documents such as `OrganizationSpec`.
- `events/` for versioned streaming and progress events.
- `fixtures/` for contract-valid examples used by tests and the frontend.

Do not add placeholder payloads that look authoritative. Contract examples must come from approved models and must be validated against their schemas.

Generated snapshots currently include:

- `schemas/organization-spec.v1.json` from `src/mutiai/domain/organization.py`.
- `openapi/openapi.v1.json` from the FastAPI application routes and models.
- `events/task-event.v1.json` from the persisted and streamed task event envelope.

The OpenAPI snapshot includes the authenticated Runtime binding resources and
the immutable RuntimeExecution policy fields consumed by the frontend. Runtime
binding values are product configuration; raw LangGraph checkpoints and Codex
Thread history are not public contract resources.

Runtime binding resources include a versioned `capability_profile`. Organization
roles and Task requests can declare structured `capability_requirements`. The
owner-scoped feasibility endpoints expose persisted validator results with
outcomes `feasible`, `conditional`, `blocked`, and `capability_unknown` before
the frontend renders or submits a state-changing action.

Captured, model-validated feasibility responses live under
`fixtures/feasibility/`. Regenerate them from an isolated temporary database
with:

```powershell
uv run python scripts\export_feasibility_fixtures.py
```

Regenerate both snapshots with:

```powershell
uv run python scripts\export_contracts.py
```

The snapshots are checked by the test suite so that backend and frontend contracts cannot silently drift.
