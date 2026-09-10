# STORY-004: Sign in a pre-linked user through validated OIDC

- **Epic:** OIDC protocol · **Points:** 8 · **Status:** done
- **Depends on:** STORY-002, STORY-003
- **Requirements:** FR-003–FR-005, FR-007, NFR-001–NFR-005

## Value

As a member with a linked SSO identity, I want to authenticate through my IdP so that I can enter Plane without another password.

## Acceptance criteria

1. **Given** an enabled provider **when** login starts **then** Plane records a short-lived session-bound state, nonce, and PKCE verifier and sends an exact authorization request.
2. **Given** a valid callback for a pre-linked identity **when** token and ID Token validation succeeds **then** Plane consumes the transaction, runs the post-auth workflow, rotates the Django session, and safely redirects.
3. **Given** invalid state, nonce, issuer, audience, authorized party, signature, algorithm, time claim, subject, or code exchange **when** callback runs **then** login fails without creating a session or leaking sensitive data.
4. **Given** a consumed or expired transaction **when** replayed **then** Plane rejects it.
5. **Given** a disabled provider, deactivated user, or bot user **when** authentication is attempted **then** access is denied.

## Technical notes

- Add generic OIDC initiation and callback routes.
- Permit an identity already created by fixtures/admin data; JIT is STORY-005.
- Clear transaction material on success and terminal failure.

## Test plan

- Unit: authorization parameters and claims validation boundaries.
- Contract: success plus negative/replay matrix with signed test JWT/JWKS fixtures.

## Completion evidence

- Commits: working-tree delivery; session-bound PKCE/state/nonce flow and callback
- Tests and coverage: successful login, replay rejection and invalid callback paths passed
- Review: approved by `bmad-orchestrator` at D1
