# Task execution plans and Artifact handoff

Status: Accepted for M2.2 implementation.

## Decision

`OrganizationSpec` defines persistent roles and reporting relationships. It does not define the execution order of every Task.

Each Task owns an immutable, versioned `TaskExecutionPlan`. The plan is a directed acyclic graph of bounded steps assigned to existing roles. M2.2 first implements a strict linear plan, then generalizes the same persisted representation to parallel DAG branches.

Roles exchange product-owned Artifacts. They do not exchange Codex transcripts, adopt another role's Thread, share one Runtime Workspace, or read files directly from another role's Workspace.

## Required boundaries

- The product database owns plans, steps, dependencies, Artifact metadata, validation state, and input bindings.
- LangGraph checkpoints own only orchestration progress and stable product identifiers.
- Codex Threads own role execution context. A Thread remains bound to one role Workspace.
- The managed filesystem or a future object store owns Artifact bytes. The database stores immutable references, hashes, and validation facts.
- An organization change creates a new `OrganizationSpec` version. It does not mutate the plan snapshot of a running Task.
- A plan may reference only roles in the Task's published `OrganizationSpec` version.
- Runtime Agents cannot create persistent formal roles.

## TaskExecutionPlan

A plan records:

- `plan_id` and `task_id`.
- `organization_spec_version_id`.
- `plan_version`, canonical definition hash, and lifecycle status.
- Planning source and validation summary.
- Ordered steps and explicit dependencies.

Each `PlanStep` records:

- Stable `step_key`.
- Existing `role_key`.
- Step kind, instructions, and acceptance criteria.
- Dependency step keys.
- Required input Artifact contracts.
- Required output Artifact contracts.
- Current dependency and execution state.

The compiler rejects unknown roles, duplicate step keys, cycles, missing producers, and output contracts that cannot satisfy downstream input contracts.

## Assignment lifecycle

The scheduler creates or submits an Assignment only after all dependencies have released accepted Artifacts.

```text
pending_dependency
  -> ready
  -> submitted
  -> running
  -> waiting_runtime
  -> validating_output
  -> completed
```

Failure or rejection keeps dependent steps blocked. A dependent Assignment must not run early and compensate for a missing handoff.

The Runtime instructions contain only:

- The role responsibility boundary.
- The bounded step objective.
- Materialized input Artifact manifests.
- The output Artifact contract.
- Acceptance criteria.

The original user request is available to the organization lead for planning and review. It is not copied verbatim into every specialist Assignment when doing so exposes inputs outside that step's contract.

## Artifact model

An Artifact record contains:

- `artifact_id`, `task_id`, and producer Assignment identity.
- Origin metadata that distinguishes user-provided Task inputs from Assignment outputs.
- Contract key and schema version.
- Media type, byte size, and SHA-256 hash.
- Producer Workspace and safe relative source path.
- Product-owned storage reference.
- Validation status and summary.
- Creation, release, and supersession metadata.

Artifacts are immutable after release. A retry creates a new Artifact version instead of overwriting a released object.

Initial Task inputs are released Artifacts with `origin=task_input` and no producer Assignment or producer Workspace. They satisfy only contracts listed in `TaskExecutionPlan.initial_input_contracts`. Assignment outputs use `origin=assignment` and always record their producer Assignment, plan step, and Workspace.

## Filesystem handoff

Role Workspaces remain isolated under the managed Runtime root. A producer writes declared outputs inside its own Workspace. `ArtifactManager` then:

1. Canonicalizes the source path and rejects traversal outside the producer Workspace.
2. Validates the declared Artifact contract.
3. Computes the hash and records metadata.
4. Copies the immutable object into the Task Artifact store.
5. Emits Artifact publication events.
6. Materializes a downstream input copy under that role's Workspace.
7. Records an explicit `ArtifactInputBinding` before the dependent Assignment starts.

The initial local layout is:

```text
{runtime_root}/users/{user_id}/organizations/{organization_id}/
  workspaces/{workspace_id}/
  tasks/{task_id}/artifacts/{artifact_id}/
```

Linux production can replace local copying with object storage without changing product contracts.

## Runtime output envelope

Specialist Runtime Turns return a structured delivery envelope:

```json
{
  "status": "completed",
  "summary": "Bounded delivery summary",
  "artifacts": [
    {
      "contract_key": "invoice-extraction-v1",
      "relative_path": "outputs/invoice-extraction.json",
      "media_type": "application/json"
    }
  ]
}
```

`blocked` is a valid delivery status only when a required input is unavailable. It is not treated as successful work.

The product validates this envelope and the declared files. A textual claim that a file exists is not an Artifact.

## LangGraph execution model

The generic graph is dependency-driven:

```text
load_plan
  -> find_ready_steps
  -> bind_input_artifacts
  -> submit_runtime
  -> checkpoint_and_wait
  -> consume_runtime_event
  -> validate_and_publish_artifacts
  -> release_dependents
  -> next_ready_step | lead_review | END
```

Runtime nodes submit and return. Long Codex Turns run outside LangGraph and wake the persisted graph through product events.

A strict linear chain is represented by one dependency per step. Future independent steps can use `Send` only after their dependencies are satisfied.

## Invoice acceptance example

```text
lead.plan
  -> extract_invoice
  -> build_cny_workbook
  -> translate_usd
  -> lead.review
```

Artifact contracts are:

- `InvoiceImageV1` -> `InvoiceExtractionV1`.
- `InvoiceExtractionV1` -> `InvoiceWorkbookCNYV1`.
- `InvoiceWorkbookCNYV1` -> `InvoiceWorkbookUSDV1`.

The Excel role receives only the accepted extraction Artifact. The translator receives only the accepted CNY workbook. The lead reviews the final Artifact manifest and validation results.

## Security modes

`demo_full_access` remains useful for the localhost demonstration, but it is not a hard role-isolation boundary. In that mode, Assignment scoping, Artifact validation, and audit detect overreach but cannot prevent every filesystem access.

Production uses restricted Workspaces and materializes only approved input Artifacts. Full Access never authorizes direct cross-role Workspace adoption.

## Idempotency and recovery

- Plan creation is idempotent per Task and plan version.
- Step identity is stable for `(task_id, step_key)`.
- An Assignment input snapshot records exact Artifact IDs.
- Publishing is idempotent for one delivery and declared relative path.
- Runtime retry reuses the frozen input snapshot unless the product explicitly creates a revised plan or rebinding decision.
- A downstream step never silently switches to a newer upstream Artifact.
- Checkpoint recovery reads authoritative step and Artifact state from the product database.

## Visualization contract

The frontend renders the product plan and event stream, not LangGraph internals. It can display:

- Step and dependency topology.
- Role ownership.
- Waiting dependency, ready, Runtime, validation, and terminal states.
- Input and output Artifact IDs and validation status.
- Runtime binding, Thread, Turn, Workspace, usage, and recovery facts.

This contract remains usable if LangGraph is replaced by another orchestration engine.

## M2.2 implementation order

1. Persist plans, steps, dependencies, Artifacts, and input bindings.
2. Add Artifact path validation, immutable publication, and input materialization.
3. Add structured Runtime delivery envelopes.
4. Implement a generic strict-linear scheduler.
5. Expose plans and Artifacts through Task APIs and product events.
6. Run FakeRuntime recovery and idempotency tests.
7. Re-run the invoice workflow with real `gpt-5.5`, `medium`, and the localhost Full Access policy.
