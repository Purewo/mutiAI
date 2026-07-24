# M2.2 linear Artifact handoff acceptance

Status: Core implementation complete; real local Codex acceptance pending.

M2.2 closes the gap found by the invoice role-boundary acceptance run: specialist roles respected missing-input boundaries, but the orchestration graph submitted all specialists before their upstream deliverables existed.

## Required acceptance behavior

- Persist one immutable `TaskExecutionPlan` for the Task.
- Validate that every plan role exists in the published `OrganizationSpec` snapshot.
- Represent the invoice workflow as extractor -> Excel -> translator -> lead review.
- Do not submit a dependent Assignment before every required input Artifact is released.
- Do not pass the original invoice image path to Excel or translator Assignments.
- Publish producer files only through `ArtifactManager`.
- Reject absolute paths, path traversal, missing files, invalid media, and contract mismatches.
- Record SHA-256, byte size, producer Assignment, source Workspace, storage reference, and validation status.
- Materialize downstream inputs inside the downstream role Workspace and persist the exact input binding.
- Require a structured Runtime delivery envelope. A textual file claim is not sufficient.
- Keep released Artifacts immutable. Retry creates a new version.
- Preserve submit, checkpoint, wait, external event, and resume semantics for every Runtime step.
- Expose plan steps, dependency state, Artifact metadata, and Runtime facts without exposing Codex transcripts or LangGraph checkpoints.

## Invoice exit condition

The real Runtime flow produces exactly one accepted final `InvoiceWorkbookUSDV1` Artifact. It preserves CNY values, records `1 USD = 7.20 CNY`, contains correct two-decimal USD values, and passes workbook validation.

The content extractor does not create Excel files. The Excel role does not inspect the source image or calculate USD. The translator does not inspect the source image or reconstruct extraction data. The organization lead does not repair specialist files.

## Regression gates

- Existing authentication, organization publication, Runtime binding, approval, capacity, retry, cancellation, and recovery tests remain green.
- Event sequences remain unique and resumable.
- Runtime Workspaces remain under the managed root.
- Product source repositories remain outside managed Runtime execution.
