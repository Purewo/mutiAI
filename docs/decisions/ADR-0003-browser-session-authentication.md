# ADR-0003: Browser session authentication

Status: Accepted for V1.

## Context

The first product interface is a same-origin web application for one account owner. Sessions must be revocable, must not expose password material, and must not make the frontend responsible for bearer-token storage. The initial backend remains bound to localhost during development.

## Decision

- Hash account passwords with Argon2 through `pwdlib`.
- Generate a cryptographically random opaque token after a successful login.
- Return the raw token only in an HttpOnly, SameSite=Lax cookie.
- Set the cookie Secure flag in production.
- Store only the SHA-256 token hash, user binding, expiry, and revocation timestamp in the product database.
- Revoke the server-side session during logout before deleting the browser cookie.
- Use the same error message for unknown users and invalid passwords.
- Seed the configured development admin only outside production and never reset an existing account password during startup.
- Do not use JWT for the V1 browser session.

## Rationale

The product already requires a durable database and does not need stateless authentication. Server-side opaque sessions provide immediate revocation, straightforward audit records, and a smaller browser security surface. JWT would add key rotation and revocation complexity without improving the first deployment topology.

## Security boundary

The M1 service remains bound to localhost. SameSite=Lax is defense in depth, not a complete authorization model. Before a network-accessible frontend is deployed, enforce the approved frontend origin and complete the CSRF and session-cleanup policy. Never expose the bootstrap password or raw session token in logs, events, OpenAPI examples, or frontend storage.

## Consequences

- Each authenticated request performs one indexed session lookup.
- Session records can be revoked and audited independently.
- Expired and revoked session cleanup requires a later maintenance job.
- Multiple browser sessions per user remain possible.
