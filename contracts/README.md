# Authoritative contracts

This directory will contain the product's source contracts after the architecture review freezes their first versions.

Planned categories:

- `openapi/` for HTTP APIs.
- `schemas/` for product documents such as `OrganizationSpec`.
- `events/` for versioned streaming and progress events.
- `fixtures/` for contract-valid examples used by tests and the frontend.

Do not add placeholder payloads that look authoritative. Contract examples must come from approved models and must be validated against their schemas.

The first generated snapshot is `schemas/organization-spec.v1.json`. Its source model is `src/mutiai/domain/organization.py`. Regenerate it with:

```powershell
uv run python scripts\export_contracts.py
```

The schema snapshot is checked by the domain test suite so that model and frontend contracts cannot silently drift.
