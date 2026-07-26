# M3 frontend task packet

Status: Ready for Fable5 real-backend integration.

## Contract source

Use the current backend repository commit that contains this packet as the
contract baseline. The authoritative files are:

- `contracts/openapi/openapi.v1.json`
- `contracts/schemas/organization-spec.v1.json`
- `contracts/events/task-event.v1.json`
- `docs/architecture/API_EVENT_BOUNDARY.md`
- `docs/architecture/TASK_PLAN_ARTIFACT_HANDOFF.md`
- `docs/architecture/M2_3_PARALLEL_ARTIFACT_ACCESS_USAGE.md`
- `docs/acceptance/M2_1_RUNTIME_POLICY.md`
- `contracts/events/assistant-event.v1.json`
- `contracts/fixtures/assistant/`
- `docs/architecture/PLATFORM_ASSISTANT_CONVERSATION.md`

Do not redefine backend resource shapes in the frontend repository. If a screen needs a field that is not in the OpenAPI snapshot, stop and report the missing contract instead of inventing one.

## First frontend slice

Build the authenticated single-user web shell and the preview-first organization workflow:

1. Login with the development `admin` account.
2. List the user's organizations.
3. Show one organization's published `OrganizationSpec` as a read-only organization preview.
4. Show Runtime bindings and allow editing model, reasoning effort, and security mode through the binding API.
5. Submit a planned task for a published organization and show its generated execution plan before starting execution.
6. Upload declared initial Task inputs, start the validated plan, and show exact step dependencies as either a strict-linear chain or a pure-parallel specialist fan-out followed by lead review.
7. Show task status, per-role Assignment and plan-step status, Runtime IDs, requested/actual model, security snapshot, delivery summaries, and context-compaction count.
8. Show released Artifact results through `content_url` and `download_url`. Never construct or display host paths from `storage_relative_path`.
9. Show Task-level Token totals and the per-Assignment usage breakdown. Label Provider-observed counters separately from the conservative `charged_tokens` budget value.
10. Reconnect task progress using the task event endpoint and then refresh the Task and usage resources.
11. Replace the platform-assistant demo engine with the real conversation API. Keep the browser UI owner-scoped and preview-first; do not create a second client-side source of truth for messages or Actions.

The UI can borrow Dify's visual clarity, but the first slice does not include a drag-and-drop editor. Organization changes remain structured preview data and backend-confirmed publication actions.

## HTTP routes in scope

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/logout`
- `GET /api/v1/auth/me`
- `GET /api/v1/organizations`
- `GET /api/v1/organizations/{organization_id}`
- `GET /api/v1/organizations/{organization_id}/versions`
- `POST /api/v1/organizations/{organization_id}/versions/{spec_version_id}/confirm`
- `POST /api/v1/organizations/{organization_id}/versions/{spec_version_id}/publish`
- `GET /api/v1/runtime/bindings`
- `PUT /api/v1/runtime/bindings/{binding_key}`
- `GET /api/v1/runtime/controls`
- `POST /api/v1/organizations/{organization_id}/tasks`
- `POST /api/v1/tasks/{task_id}/plan`
- `POST /api/v1/tasks/{task_id}/inputs`
- `POST /api/v1/tasks/{task_id}/start`
- `GET /api/v1/tasks/{task_id}`
- `GET /api/v1/tasks/{task_id}/artifacts/{artifact_id}/content`
- `GET /api/v1/tasks/{task_id}/usage`
- `GET /api/v1/tasks/{task_id}/events`
- `GET /api/v1/tasks/{task_id}/approvals`
- `POST /api/v1/tasks/{task_id}/approvals/{approval_id}/decision`
- `POST /api/v1/tasks/{task_id}/retry`
- `POST /api/v1/tasks/{task_id}/cancel`

Platform-assistant conversation routes:

- `POST /api/v1/assistant/conversations`
- `GET /api/v1/assistant/conversations`
- `GET /api/v1/assistant/conversations/{conversation_id}`
- `POST /api/v1/assistant/conversations/{conversation_id}/archive`
- `GET /api/v1/assistant/conversations/{conversation_id}/messages`
- `POST /api/v1/assistant/conversations/{conversation_id}/messages`
- `GET /api/v1/assistant/turns/{turn_id}`
- `POST /api/v1/assistant/turns/{turn_id}/cancel`
- `GET /api/v1/assistant/conversations/{conversation_id}/actions`
- `GET /api/v1/assistant/actions/{action_id}`
- `POST /api/v1/assistant/actions/{action_id}/decision`
- `GET /api/v1/assistant/conversations/{conversation_id}/events`

Assistant message submission requires a unique `Idempotency-Key`. Action
confirmation is asynchronous: render `confirmed` and `executing` as pending
states, then refresh the Action and referenced resource after
`assistant.action.completed` or `assistant.action.failed`. Event replay uses
`Last-Event-ID`; the response is a finite ordered batch and the browser
`EventSource` reconnects using the server-provided `retry` value.

The assistant's product-tool results are not a second frontend API. They are
visible through persisted Organization, Task, Artifact, usage, and feasibility
resources. Do not render Codex private history or raw tool calls.

Task submission must send a unique `Idempotency-Key` and set `orchestration_mode` to `planned` for the plan-driven workflow. An initial Artifact `contract_key` must appear in `execution_plan.initial_input_contracts`; send the uploaded file's schema version, media type, file name, and bytes through `TaskInputArtifactRequest`, whose current transport uses `content_base64`. Event replay uses `Last-Event-ID` and deduplicates by `event_id` or `sequence`.

The Artifact content endpoint is owner-scoped and returns the declared media type. Use its default inline response for preview when the browser supports the media type, and add `download=true` for explicit download. Treat 409 integrity or release-state errors as unavailable results rather than attempting a direct filesystem fallback.

Send `Accept-Language: zh-CN` on API requests. For an `ErrorEnvelope`, keep the
stable `code` for control flow and display the backend-provided localized
`message`; do not replace business errors with frontend-authored translations.
The response `Content-Language` identifies the selected locale. Network and
timeout failures without an error envelope remain client-transport states.

Runtime binding responses now include the current versioned
`capability_profile`. Organization role definitions may declare
`capability_requirements`, and Task submission may declare the same structured
workload requirements. The backend validates these requirements before
confirmation, publication, Task submission, and Runtime start.

Use the owner-scoped feasibility resources when rendering the preview and
execution states:

- `GET /api/v1/feasibility-checks/{feasibility_check_id}`
- `GET /api/v1/organizations/{organization_id}/versions/{spec_version_id}/feasibility-checks`
- `GET /api/v1/tasks/{task_id}/feasibility-checks`

The current outcomes are `feasible`, `conditional`, `blocked`, and
`capability_unknown`. Findings contain stable `reason_code`, affected role and
binding, required and actual capability values, `alternative_codes`, and
backend-localized `message` and `alternatives`. Do not infer feasibility from
the model name or from a successful earlier Runtime execution.

## State mapping

Treat these as product states, not LangGraph states:

- Task: `created`, `planning`, `running`, `waiting`, `needs_revision`, `completed`, `failed`, `cancelled`.
- Assignment: `pending`, `submitted`, `running`, `waiting`, `completed`, `failed`, `cancelled`.
- Runtime execution: `submitted`, `running`, `waiting`, `completed`, `failed`, `cancelled`.
- Plan: `draft`, `validated`, `active`, `completed`, `needs_revision`, `failed`, `cancelled`.
- Plan step: `pending_dependency`, `ready`, `submitted`, `running`, `waiting`, `validating_output`, `completed`, `blocked`, `failed`, `cancelled`.
- Artifact: `draft`, `validated`, `released`, `rejected`, `superseded`.
- Approval: `pending`, `accepted`, `declined`, `cancelled`.
- Organization version: `proposal`, `confirmed`, `published`, `superseded`, `archived`.
- Assistant conversation: `active`, `archived`.
- Assistant Turn: `queued`, `submitted`, `running`, `waiting`, `completed`, `failed`, `cancelled`.
- Assistant Action: `proposed`, `confirmed`, `executing`, `completed`, `failed`, `declined`, `cancelled`, `expired`, `superseded`.

`waiting` is not an error. It can mean an external Runtime Turn, capacity queue, or approval boundary. A terminal task remains queryable after the SSE response ends.

Render plan topology from `execution_plan.steps[*].plan_step_id` and `dependency_step_ids`. M2.3 accepts only a strict-linear specialist chain ending in lead review or dependency-free parallel specialists joined by lead review. Do not implement or visually imply mixed serial-parallel stages in this slice.

## Runtime binding rules

- The binding key is the stable value referenced by `OrganizationSpec.roles[].runtime_binding_key`.
- `workspace_restricted` displays approval-capable restricted execution.
- `demo_full_access` is available only for the localhost development demo. Do not add a frontend control that bypasses backend validation.
- Show both `requested_model` and `actual_model`; either can be null when the Provider does not report a value.
- Do not display Codex transcripts, raw tool events, LangGraph checkpoints, or filesystem paths as product history.

## Explicitly out of scope

- Multi-user organization membership, invitations, and human collaboration nodes.
- Drag-and-drop organization editing or autonomous role creation.
- WeChat integration.
- Mixed serial-parallel plan editing or execution.
- Provider-specific raw Codex history or an in-browser terminal.
- Production authentication hardening and deployment configuration.

## Acceptance checks

- Login, logout, and refresh preserve the authenticated session.
- Organization preview renders loading, empty, validation-error, and published states.
- Binding updates are idempotent and show backend conflict errors without inventing fallback values.
- Task submission never duplicates work when the request is retried with the same idempotency key.
- Task progress survives an SSE reconnect and does not duplicate event rows.
- Linear and pure-parallel plans render from persisted step dependencies without inferring edges from array order.
- Parallel lead review appears only after every specialist result and required Artifact are released.
- Artifact previews and downloads use only the backend URLs and expose integrity or release errors.
- Task usage totals match the returned per-Assignment rows, while observed totals and charged budget totals remain visibly distinct.
- Runtime approval decisions are shown only for an approval record returned by the backend.
- Completed, failed, cancelled, and needs-revision task states are visibly distinct.
- Assistant Actions show explicit proposed, pending, completed, failed, and declined states and never treat a chat reply as mutation completion.
- Browser console has no uncaught errors, and network requests match the OpenAPI contract.

After Gemini submits the frontend change, Codex will run the frontend against the real local backend and perform browser verification before calling M3 complete.
