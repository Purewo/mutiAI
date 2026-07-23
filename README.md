# mutiAI core

`mutiAI` is the source repository for the product model, backend services, orchestration, runtime adapters, and authoritative contracts of the mutiAI project.

The repository is currently in the design and bootstrap phase. It intentionally contains no application implementation yet.

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

## Documentation

- [V1 product definition](docs/product/V1_PRODUCT_DEFINITION.md)
- [System boundaries](docs/architecture/SYSTEM_BOUNDARIES.md)
- [Frontend contract workflow](docs/collaboration/FRONTEND_CONTRACT_WORKFLOW.md)
- [Two-repository decision](docs/decisions/ADR-0001-two-repository-boundary.md)
- [Contract directory](contracts/README.md)

## Python environment

The bootstrap environment uses Python 3.12 and LangGraph 1.2.9, matching the verified local learning environment.

```powershell
uv sync
.\.venv\Scripts\python.exe --version
```

Do not add a backend web framework until the first architecture review selects it explicitly.

## Local development account

The first local environment may seed an `admin` account with a simple development-only password. Keep the real value in the ignored `.env` file. Never expose that account on a network or reuse it in production.
