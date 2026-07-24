# mutiAI core

`mutiAI` is the source repository for the product model, backend services, orchestration, runtime adapters, and authoritative contracts of the mutiAI project.

The M1 backend walking skeleton is complete. M2 is implementing the local Codex App Server Runtime boundary.

## Repository responsibilities

- Own the product database model and business rules.
- Own `OrganizationSpec` and other product-level contracts.
- Use LangGraph as a replaceable orchestration kernel.
- Integrate Codex through an `AgentRuntimeAdapter` boundary.
- Publish HTTP and event contracts consumed by the frontend repository.
- Keep organization, task, permission, runtime, and artifact facts outside LangGraph state.

The companion frontend repository is [Purewo/mutiAI-aistdio-gemini](https://github.com/Purewo/mutiAI-aistdio-gemini).

## Current product boundary

- The first-party user interface is a web application.
- Personal WeChat is the first external messaging channel.
- Each organization has exactly one mandatory organization lead.
- The platform assistant manages all organizations owned by one user.
- Organization design is conversational and preview-first. The first release has no drag-and-drop editor.
- Publishing an `OrganizationSpec` does not create a workspace or Codex thread.
- Runtime resources are initialized lazily when real work begins.
- The first Runtime implementation uses local Codex on Windows.
- Linux is the production target and must be validated after the first local vertical slice.
- Accounts are isolated by owner. The first release has no organization membership, invitations, or collaboration.
- Managed Codex Runtime sessions use `G:\AI\AI_private\mutiAI-runtime-workspaces`; product source repositories under `Codex_projects` must never be used as Runtime working directories.

## Documentation

- [V1 implementation roadmap](docs/ROADMAP.md)
- [V1 product definition](docs/product/V1_PRODUCT_DEFINITION.md)
- [M0 domain model](docs/product/M0_DOMAIN_MODEL.md)
- [System boundaries](docs/architecture/SYSTEM_BOUNDARIES.md)
- [M0 API and event boundary](docs/architecture/API_EVENT_BOUNDARY.md)
- [Frontend contract workflow](docs/collaboration/FRONTEND_CONTRACT_WORKFLOW.md)
- [Two-repository decision](docs/decisions/ADR-0001-two-repository-boundary.md)
- [V1 technology decision](docs/decisions/ADR-0002-v1-technology-stack.md)
- [Browser session decision](docs/decisions/ADR-0003-browser-session-authentication.md)
- [M1 task orchestration decision](docs/decisions/ADR-0004-m1-task-orchestration.md)
- [Codex App Server Runtime decision](docs/decisions/ADR-0005-codex-app-server-runtime.md)
- [M2 Codex Runtime acceptance](docs/acceptance/M2_CODEX_RUNTIME.md)
- [M1 vertical-slice acceptance](docs/acceptance/M1_VERTICAL_SLICE.md)
- [Contract directory](contracts/README.md)

## Python environment

The bootstrap environment uses Python 3.12 and LangGraph 1.2.9, matching the verified local learning environment.

```powershell
uv sync
.\.venv\Scripts\python.exe --version
uv run alembic upgrade head
```

The first architecture review selected FastAPI, SQLAlchemy, and Alembic for the initial backend skeleton. Keep the HTTP boundary and persistence adapters replaceable as the product evolves.

Run the local backend during M1 with:

```powershell
uv run uvicorn mutiai.main:app --reload
```

The initial health endpoint is `GET http://127.0.0.1:8000/api/v1/health`.

In development and tests, application startup runs pending migrations when `DATABASE_AUTO_MIGRATE=true`. Production deployments must run migrations as an explicit release step.

### Isolated local Codex configuration

The local Codex adapter uses a dedicated `CODEX_HOME` below the managed Runtime root. To reuse a local custom-provider relay without adopting existing interactive sessions, copy only the provider configuration and API-key auth file:

```powershell
uv run python scripts/bootstrap_codex_home.py
```

The bootstrap copies `config.toml` and `auth.json` only. It does not copy `sessions`, `history.jsonl`, SQLite state, or existing Threads. Use `--replace` only when the source home is the intended provider configuration source. Official ChatGPT device-code login is optional and is not required for the current custom-provider setup.

The current Windows test root is `G:\AI\AI_private\mutiAI-runtime-workspaces`. Linux deployment should inject provider credentials through a dedicated secret mechanism instead of copying a developer Codex home.

## Local development account

The first local environment may seed an `admin` account with a simple development-only password. Keep the real value in the ignored `.env` file. Never expose that account on a network or reuse it in production.

The browser login uses an opaque HttpOnly session cookie. The database stores only its SHA-256 hash, expiry, and revocation state. See [the browser session decision](docs/decisions/ADR-0003-browser-session-authentication.md).
