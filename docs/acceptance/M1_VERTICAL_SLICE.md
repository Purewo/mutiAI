# M1 vertical-slice acceptance checklist

Status: Initial test contract.

## Preconditions

- Backend runs on the Windows development host.
- Development-only `admin` account exists.
- Product Runtime root is `G:\AI\AI_private\mutiAI-runtime-workspaces`.
- A valid OrganizationSpec contains one organization lead and at least two existing specialist roles.

## Acceptance path

1. Log in as `admin`.
2. Create or load one structured OrganizationSpec proposal.
3. Reject a proposal that has no organization lead.
4. Publish a confirmed OrganizationSpec version.
5. Verify that publication creates no Runtime workspace or Codex Thread.
6. Submit one task to the published organization.
7. Verify the product creates one Task and bounded Assignments.
8. Run the LangGraph workflow through a FakeRuntimeAdapter.
9. Verify task and assignment events are appended and queryable.
10. Re-submit the same request with the same idempotency key and verify no duplicate Task or external execution is created.
11. Simulate one Runtime branch waiting and another completing.
12. Resume the waiting branch and verify the organization lead receives both results.
13. Restart the backend process and resume from persisted state.
14. Verify the final task summary is stable and the Runtime workspace path remains inside the managed root.

## Failure cases that must be covered

- Unauthenticated access to another user's organization.
- Invalid OrganizationSpec without a mandatory lead.
- Publishing a stale or superseded version.
- Duplicate task idempotency key with different request content.
- Runtime submission failure after the product execution record exists.
- Duplicate Runtime completion event.
- Event stream reconnect from a prior cursor.
- Graph replay after a partially completed parallel step.
- Attempt to use a source repository as a Runtime workspace.

## Exit condition

The M1 path is complete when the product can demonstrate durable product state, idempotent task submission, recoverable orchestration, and a stable event stream without a real Codex dependency.

