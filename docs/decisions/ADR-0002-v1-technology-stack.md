# ADR-0002: V1 technology stack and implementation order

Status: Accepted for the first implementation pass.

## Context

The product needs a Python backend, explicit OpenAPI contracts for the Gemini frontend, durable product records, LangGraph orchestration, and a local Codex Runtime adapter. The first development host is Windows, while production targets Linux.

## Decision

Use the following initial stack unless an implementation spike provides contrary evidence:

- Python 3.12.
- FastAPI for the HTTP boundary and OpenAPI generation.
- Pydantic v2 for request, response, event, and OrganizationSpec validation.
- SQLAlchemy 2 with Alembic for persistence and migrations.
- SQLite for the first local backend skeleton.
- PostgreSQL as the target for concurrent Runtime execution and production.
- LangGraph 1.2.9 as the initial orchestration kernel.
- SSE for server-to-browser progress and chat events in V1.
- HttpOnly browser sessions for the first web login flow.
- Codex App Server as the first Runtime integration.

## Rationale

FastAPI makes the HTTP contract visible to the frontend repository. Pydantic keeps the product schemas explicit. SQLAlchemy and Alembic keep the local SQLite path portable to PostgreSQL. SSE is sufficient for server-to-browser streams while user commands remain ordinary HTTP requests. App Server provides the Thread, Turn, `cwd`, and event lifecycle needed by the Runtime Manager.

SQLite is intentionally a local bootstrap choice, not a claim that production Runtime concurrency should use an uncoordinated file database. The switch to PostgreSQL must happen before production-scale parallel Runtime execution.

## Implementation order

1. Write and review product schemas and state transitions.
2. Build the FastAPI health, session, OrganizationSpec, and task skeleton.
3. Use a FakeRuntimeAdapter to test idempotency, events, and recovery.
4. Replace the fake adapter with the local Windows Codex App Server adapter.
5. Integrate the Gemini frontend against the versioned contracts.
6. Validate the complete Runtime path in Linux.

## Consequences

The first pass remains small enough to test locally. It also creates explicit seams for replacing SQLite, LangGraph, Windows process handling, and the initial Runtime provider without changing product entities or frontend contracts.

## Not decided by this ADR

- Final frontend component library.
- Final organization diagram library.
- Production deployment topology.
- WeChat adapter details.
- Multi-user collaboration and permissions beyond owner isolation.
- Complete knowledge-base technology.
