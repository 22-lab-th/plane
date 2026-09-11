# STORY-011: Make no-OIDC mode a supported deployment profile

- **Epic:** Optional SSO lifecycle and production hardening
- **Points:** 5
- **Status:** backlog
- **Depends on:** none
- **Requirements:** FR-016, FR-018, NFR-010

## Value

As a 22lab operator, I want OIDC disabled by default so that the existing Plane login remains available without any dependency on an IdP.

## Acceptance criteria

1. Given `ENABLE_OIDC_SSO` is absent or false, normal authentication retains its configured behavior and no OIDC request is made.
2. Given stored provider data while the gate is off, the public API exposes no SSO provider, SSO routes fail safely, and God Mode shows a deployment-controlled disabled state.
3. Given 22lab's current authentication configuration, password, magic-code, and enabled social OAuth regression tests pass unchanged.
4. Given stored enforcement while the gate is off, the effective mode is Disabled and an operator-visible warning is recorded without blocking normal login.

## Test plan

- Backend unit/contract tests for gate-off public, route, middleware, and normal-auth behavior.
- Frontend tests proving the sign-in UI is unchanged.
- 22lab smoke test with the gate off and no provider.
