# M2.3 parallel Artifact, result access, and Task usage plan

Status: Completed on 2026-07-25. Ready for frontend integration.

M2.3 removes three backend gaps before frontend integration: parallel work has no complete Artifact handoff, clients cannot safely read released files, and Task token totals require client-side arithmetic. The implementation is complete and the acceptance gates below passed.

## Scope

M2.3 delivers three product-owned capabilities:

1. Planned Tasks support a pure parallel fan-out followed by organization-lead review with complete Artifact handoff.
2. Authenticated clients read or download released Artifacts through a controlled HTTP endpoint.
3. Authenticated clients query Task-level token totals and per-Assignment usage facts.

Strict-linear planned execution remains supported. Mixed structures that combine multiple serial and parallel stages are explicitly deferred.

## Supported planned shapes

The backend accepts exactly two planned shapes.

### Strict linear

```text
specialist A
  -> specialist B
  -> specialist C
  -> lead review
```

Each step receives only the released Artifacts declared by its contract.

### Pure parallel fan-out and review join

```text
specialist A --\
specialist B ----> lead review
specialist C --/
```

Every specialist is an independent root step. The final and only `lead_review` step depends on every specialist, receives their released output Artifacts, and returns a structured review decision without creating a replacement Artifact.

The following mixed shape is not accepted in M2.3:

```text
A and B in parallel -> C joins -> D and E in parallel -> lead review
```

### Plan validation

A valid plan must satisfy all of the following rules:

- Every role exists in the frozen published organization version.
- Every specialist step belongs to a non-lead role.
- One formal role appears at most once in a Task plan.
- The final and only `lead_review` belongs to the organization lead and declares no output Artifact.
- A linear plan depends only on the immediately preceding step.
- A parallel plan has dependency-free specialist roots, and lead review depends on every specialist.
- Every parallel specialist declares at least one output Artifact, and lead review declares every specialist output contract as an input.
- Every input contract is produced by an allowed ancestor or declared as an initial Task input.
- Contract producers are unique and dependencies are acyclic.

## Parallel execution and recovery

The parallel specialist Assignments use one LangGraph `Send` fan-out superstep:

```text
prepare every specialist
  -> materialize exact initial inputs
  -> dispatch all specialists
  -> checkpoint and wait for Runtime events
  -> validate and publish every specialist delivery
  -> materialize all released outputs for lead review
  -> lead review
```

- Runtime submission keeps submit, checkpoint, wait, external event, and resume semantics.
- Parallel completion events are serialized through the existing graph-resume lock.
- Successful sibling deliveries remain durable when another branch fails.
- Explicit retry resets only failed Assignments and does not replay completed siblings.
- Stable `(task_id, step_key)` identities and Runtime event IDs preserve idempotency.

## Controlled Artifact content access

Add this owner-scoped route:

```text
GET /api/v1/tasks/{task_id}/artifacts/{artifact_id}/content?download=false
```

The endpoint authenticates the user, verifies Task and Artifact ownership, accepts only released or superseded immutable versions, canonicalizes the storage reference inside the managed Runtime root, and revalidates byte size and SHA-256 before serving content.

The response preserves the declared media type and safe file name. It uses inline disposition by default and attachment disposition when `download=true`. It also returns an immutable ETag and SHA-256 header.

`ArtifactResponse` exposes `content_url` and `download_url`. Clients must not construct filesystem paths from `storage_relative_path`.

## Task token usage

Runtime executions already persist normalized Turn usage and conservative budget settlement. M2.3 adds an owner-scoped aggregate route without copying usage into LangGraph State:

```text
GET /api/v1/tasks/{task_id}/usage
```

The response contains execution, reported, unavailable, and pending counts; reserved and charged ledger totals; observed input, cached input, output, reasoning output, and total tokens; and a per-Assignment breakdown with role, model, status, and persisted counters.

Observed totals include only Provider-reported usage. `charged_tokens` remains the conservative cost-control number when a Provider omits usage.

## Acceptance gates

Frontend integration starts only after all gates pass:

- A pure parallel plan starts every specialist before lead review.
- Each specialist publishes a validated Artifact through its structured delivery envelope.
- Lead review is not created until every specialist completes and every required Artifact is released.
- Lead review receives all specialist Artifacts and no specialist Workspace.
- Existing strict-linear invoice behavior remains green.
- Mixed serial-parallel plans are rejected with an explicit product error.
- Cross-owner and cross-Task Artifact reads return not found without leaking metadata.
- Corrupt or non-released Artifact content is not served.
- Inline and attachment responses preserve media type, file name, ETag, and SHA-256.
- Task usage totals equal persisted RuntimeExecution records, and duplicate terminal events do not double-count.
- The authoritative OpenAPI snapshot, backend tests, and contract tests pass.

## Delivered contract

The implementation provides the following frontend-facing contract:

- `TaskResponse.artifacts[*].content_url` reads verified content inline.
- `TaskResponse.artifacts[*].download_url` requests attachment disposition.
- `GET /api/v1/tasks/{task_id}/artifacts/{artifact_id}/content` serves only an owner-visible released or superseded Artifact after managed-root, byte-size, and SHA-256 verification. Cross-owner and cross-Task lookups return the existing not-found envelope.
- `GET /api/v1/tasks/{task_id}/usage` returns Task totals and one usage row per persisted `RuntimeExecution`. `observed_total_tokens` and the component observed counters include only Provider-reported usage; `charged_tokens` remains the product budget ledger value.
- The OpenAPI snapshot in `contracts/openapi/openapi.v1.json` is generated from the application and identifies Artifact content as a binary response.

Verification completed with `138 passed`, `uv tool run ruff check src tests scripts`, `git diff --check`, and the OpenAPI contract equality test.
