# mutiAI core

`mutiAI` is the source repository for the product model, backend services, orchestration, runtime adapters, and authoritative contracts of the mutiAI project.

The M1 backend walking skeleton is complete. M2 and M2.1 now include the local Codex App Server boundary, role-level Runtime bindings, execution-policy snapshots, Thread compaction lifecycle, product-owned Runtime concurrency, Provider capacity signals, and token-budget accounting.

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
- [M3 frontend task packet](docs/collaboration/M3_FRONTEND_TASK_PACKET.md)
- [Two-repository decision](docs/decisions/ADR-0001-two-repository-boundary.md)
- [V1 technology decision](docs/decisions/ADR-0002-v1-technology-stack.md)
- [Browser session decision](docs/decisions/ADR-0003-browser-session-authentication.md)
- [M1 task orchestration decision](docs/decisions/ADR-0004-m1-task-orchestration.md)
- [Codex App Server Runtime decision](docs/decisions/ADR-0005-codex-app-server-runtime.md)
- [M2 Codex Runtime acceptance](docs/acceptance/M2_CODEX_RUNTIME.md)
- [M2.1 Runtime policy acceptance](docs/acceptance/M2_1_RUNTIME_POLICY.md)
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

### Run the local Codex Runtime sidecar

The default `RUNTIME_PROVIDER=fake` keeps the backend self-contained for contract and orchestration tests. To run the real local Codex Runtime, start the App Server in a separate process so it survives an API restart:

```powershell
$env:CODEX_APP_SERVER_ENDPOINT="ws://127.0.0.1:4500"
uv run python scripts/run_codex_app_server.py
```

The wrapper requires `/readyz` for each process start and restarts unexpected exits with bounded exponential backoff. The defaults allow 10 restarts, starting at 0.5 seconds and capping the delay at 5 seconds. Run `uv run python scripts/run_codex_app_server.py --help` to change the local supervision policy.

In another terminal, select Codex and start the API:

```powershell
$env:RUNTIME_PROVIDER="codex"
$env:CODEX_APP_SERVER_ENDPOINT="ws://127.0.0.1:4500"
uv run uvicorn mutiai.main:app --reload
```

The API checks `/readyz` before recovering waiting Runtime executions. The sidecar uses the isolated managed `CODEX_HOME`; it does not use the interactive Codex session directory. A sidecar restart restores service availability but does not claim that an in-flight Turn survived the process exit. The disconnected execution becomes `runtime_owner_lost`; after the sidecar is ready, the existing task retry endpoint reuses the recorded Thread and Workspace and starts a new Turn. Plain WebSocket transport is restricted to loopback. Linux production should prefer a Unix socket and an external service manager.

### Configure Runtime controls

Runtime admission is owned by the product database, not LangGraph or the Provider account API:

- `RUNTIME_MAX_CONCURRENT_EXECUTIONS` limits active executions for one owner and Runtime provider. The default is `2`.
- `RUNTIME_PROVIDER_CAPACITY_CACHE_SECONDS` controls how long the Codex Adapter caches `account/rateLimits/read`. A custom relay that does not support this method is recorded as `unknown` and admitted under the current fail-open policy.
- `RUNTIME_TOKEN_BUDGET_LIMIT` and `RUNTIME_TOKEN_RESERVATION_PER_EXECUTION` enable a product token budget only when both are set. A reservation cannot exceed the total budget.

The backend reserves the configured token amount before Runtime submission and settles it once on completion, failure, or cancellation. When `thread/tokenUsage/updated` is available, settlement uses the observed Turn usage. When usage is unavailable, settlement conservatively charges the reservation. `account/usage/read` is an account activity summary and never replaces the product task ledger.

When the concurrency limit is full, the database records a `concurrency_limit` wait and LangGraph checkpoints the branch. Releasing capacity resumes the oldest eligible branch; graph nodes do not sleep or hold a worker while waiting. An explicit Provider limit returns HTTP `429` with `PROVIDER_RATE_LIMITED`, and an exhausted product budget returns HTTP `409` with `RUNTIME_BUDGET_EXCEEDED`, both before `turn/start`.

Authenticated clients can inspect the current product policy and last normalized Provider signal with:

```text
GET /api/v1/runtime/controls
```

The current admission lock is process-local around database transactions. It is correct for the single API process used in V1 local development. A multi-instance deployment must replace it with database row locking or a dedicated scheduler before sharing one budget and concurrency pool.

### Configure role Runtime bindings

Each `OrganizationSpec` role references a product-owned `runtime_binding_key`. Authenticated clients can list and update bindings through:

```text
GET /api/v1/runtime/bindings
PUT /api/v1/runtime/bindings/{binding_key}
```

A binding selects the active Runtime provider, model, reasoning effort, and named security mode. The first execution for an Assignment freezes these values into `RuntimeExecution`; an explicit retry reuses that snapshot even if the mutable binding changes later. The Runtime response exposes both the requested model and the model reported by App Server.

The first-week localhost demo defaults to `RUNTIME_SECURITY_MODE=demo_full_access`, which compiles to `approvalPolicy=never` and `danger-full-access`. Configuration rejects this mode in production or when `APP_HOST` is not loopback. `workspace_restricted` compiles to `approvalPolicy=on-request`, `workspace-write`, and no network access. Full Access does not relax product Workspace, isolated `CODEX_HOME`, Thread ownership, or interactive-session separation.

Codex context compactions are counted from completed Runtime items. No rotation limit is assumed by default. Set `RUNTIME_THREAD_MAX_COMPACTIONS` to rotate a role Thread before its next Assignment after the explicit threshold is reached. Rotation preserves the Workspace, increments its Thread generation, and carries forward only the last delivery summary, not the Codex transcript.

### Run the M2 real Runtime smoke

After the sidecar is ready, run the isolated M2 acceptance flow:

```powershell
uv run python scripts/run_m2_runtime_smoke.py
```

The script creates a temporary product database and checkpoint database under `system/m2-acceptance` inside the managed Runtime root. It publishes a two-role organization, submits one bounded task, waits for the specialist and organization-lead Turns, then prints product-safe task, usage, capacity, and event evidence. It never uses a product source repository as Runtime `cwd` and never reads an interactive Codex session directory.

This is a real Provider smoke, not a unit test. Each run creates managed Codex Threads and consumes real tokens. Use it for Runtime acceptance and Provider changes, not routine test loops.

## Local development account

The first local environment may seed an `admin` account with a simple development-only password. Keep the real value in the ignored `.env` file. Never expose that account on a network or reuse it in production.

The browser login uses an opaque HttpOnly session cookie. The database stores only its SHA-256 hash, expiry, and revocation state. See [the browser session decision](docs/decisions/ADR-0003-browser-session-authentication.md).
