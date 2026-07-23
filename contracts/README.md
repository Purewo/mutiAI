# Authoritative contracts

This directory will contain the product's source contracts after the architecture review freezes their first versions.

Planned categories:

- `openapi/` for HTTP APIs.
- `schemas/` for product documents such as `OrganizationSpec`.
- `events/` for versioned streaming and progress events.
- `fixtures/` for contract-valid examples used by tests and the frontend.

Do not add placeholder payloads that look authoritative. Contract examples must come from approved models and must be validated against their schemas.
