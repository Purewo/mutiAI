# M2.1 Runtime policy acceptance

Status: Completed for the first-week local demo boundary.

## Accepted behavior

- `RuntimeBinding` is a product-owned, per-user resource referenced by `OrganizationSpec.roles[].runtime_binding_key`.
- `GET /api/v1/runtime/bindings` returns the owner's bindings and lazily creates the configured default binding.
- `PUT /api/v1/runtime/bindings/{binding_key}` idempotently creates or updates a binding for the active Runtime provider.
- The first execution resolves the role binding and freezes its binding ID, binding key, requested model, reasoning effort, security mode, approval policy, sandbox mode, and network policy in `RuntimeExecution`.
- Explicit task retry reuses the frozen execution snapshot. Editing the mutable binding does not change the retry policy.
- Codex `thread/start`, `thread/resume`, and `turn/start` receive the resolved model and security policy. `turn/start` also receives the resolved reasoning effort.
- App Server's reported model is persisted separately from the requested model.
- `demo_full_access` compiles to `approvalPolicy=never`, Thread sandbox `danger-full-access`, and Turn sandbox policy `dangerFullAccess`.
- `workspace_restricted` compiles to `approvalPolicy=on-request`, Thread sandbox `workspace-write`, and a Turn policy limited to the canonical product Workspace with network access disabled.
- Configuration rejects demo Full Access in production and on non-loopback HTTP bindings.
- Full Access does not bypass product Workspace validation, isolated `CODEX_HOME`, recorded Thread ownership, or interactive-session separation.
- Completed `context_compaction` and compatible `compaction` Runtime items increment execution and Workspace counters.
- Thread rotation is disabled unless `RUNTIME_THREAD_MAX_COMPACTIONS` is explicitly configured.
- When the threshold is reached, the next Assignment starts a new Thread in the same Workspace, increments `thread_generation`, and adds the last delivery summary to the new bounded instructions.
- The product stores Thread lifecycle counts and delivery summaries, not Codex conversation history.
- Restricted-mode command and file-change approvals remain product-owned resources with authenticated list and one-time decision APIs. Demo Full Access does not require those approval interactions.

## Verification

Automated coverage verifies:

- Production and non-loopback Full Access rejection.
- Exact Full Access App Server wire parameters.
- Per-role model and reasoning-effort resolution.
- Immutable execution snapshots across binding edits and retry.
- Binding API idempotency and active-provider validation.
- Compaction item counting, explicit Thread rotation, Workspace reuse, generation tracking, and delivery-summary continuity.
- Alembic creation of Runtime binding, execution snapshot, and Workspace Thread lifecycle fields.

The M2.1 test pass does not run the real Provider smoke. Use the existing isolated smoke only when validating a provider or App Server change because it creates managed Threads and consumes tokens.
