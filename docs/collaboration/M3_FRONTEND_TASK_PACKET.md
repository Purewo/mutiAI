# M3 frontend task packet

Status: Ready for Gemini implementation.

## Contract source

Use the backend repository commit `957feea` as the contract baseline. The authoritative files are:

- `contracts/openapi/openapi.v1.json`
- `contracts/schemas/organization-spec.v1.json`
- `contracts/events/task-event.v1.json`
- `docs/architecture/API_EVENT_BOUNDARY.md`
- `docs/acceptance/M2_1_RUNTIME_POLICY.md`

Do not redefine backend resource shapes in the frontend repository. If a screen needs a field that is not in the OpenAPI snapshot, stop and report the missing contract instead of inventing one.

## First frontend slice

Build the authenticated single-user web shell and the preview-first organization workflow:

1. Login with the development `admin` account.
2. List the user's organizations.
3. Show one organization's published `OrganizationSpec` as a read-only organization preview.
4. Show Runtime bindings and allow editing model, reasoning effort, and security mode through the binding API.
5. Submit a task for a published organization.
6. Show task status, per-role Assignment status, Runtime IDs, requested/actual model, security snapshot, delivery summaries, and context-compaction count.
7. Reconnect task progress using the task event endpoint and then refresh the task resource.

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
- `GET /api/v1/tasks/{task_id}`
- `GET /api/v1/tasks/{task_id}/events`
- `GET /api/v1/tasks/{task_id}/approvals`
- `POST /api/v1/tasks/{task_id}/approvals/{approval_id}/decision`
- `POST /api/v1/tasks/{task_id}/retry`
- `POST /api/v1/tasks/{task_id}/cancel`

Task submission must send a unique `Idempotency-Key`. Event replay uses `Last-Event-ID` and deduplicates by `event_id` or `sequence`.

## State mapping

Treat these as product states, not LangGraph states:

- Task: `created`, `planning`, `running`, `waiting`, `needs_revision`, `completed`, `failed`, `cancelled`.
- Assignment: `pending`, `submitted`, `running`, `waiting`, `completed`, `failed`, `cancelled`.
- Runtime execution: `submitted`, `running`, `waiting`, `completed`, `failed`, `cancelled`.
- Approval: `pending`, `accepted`, `declined`, `cancelled`.
- Organization version: `proposal`, `confirmed`, `published`, `superseded`, `archived`.

`waiting` is not an error. It can mean an external Runtime Turn, capacity queue, or approval boundary. A terminal task remains queryable after the SSE response ends.

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
- Provider-specific raw Codex history or an in-browser terminal.
- Production authentication hardening and deployment configuration.

## Acceptance checks

- Login, logout, and refresh preserve the authenticated session.
- Organization preview renders loading, empty, validation-error, and published states.
- Binding updates are idempotent and show backend conflict errors without inventing fallback values.
- Task submission never duplicates work when the request is retried with the same idempotency key.
- Task progress survives an SSE reconnect and does not duplicate event rows.
- Runtime approval decisions are shown only for an approval record returned by the backend.
- Completed, failed, cancelled, and needs-revision task states are visibly distinct.
- Browser console has no uncaught errors, and network requests match the OpenAPI contract.

After Gemini submits the frontend change, Codex will run the frontend against the real local backend and perform browser verification before calling M3 complete.
