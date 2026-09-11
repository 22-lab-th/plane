# STORY-015: Deliver reversible Admin UX and component tests

- **Epic:** Optional SSO lifecycle and production hardening
- **Points:** 5
- **Status:** done
- **Depends on:** STORY-011, STORY-013, STORY-014
- **Requirements:** FR-019, FR-020, FR-021, FR-022, NFR-007, NFR-008

## Value

As an instance administrator, I want clear SSO modes and safe transition controls so that I understand which login methods remain available before changing access policy.

## Acceptance criteria

1. God Mode displays Disabled, Optional, or Enforced with metadata-test, interactive-login-test, and recovery readiness separately.
2. Disabled is the default and explains that normal Plane login remains active.
3. Disable confirmation lists remaining normal methods and states that provider and identity data are preserved.
4. Enforce confirmation requires current OIDC and recovery readiness.
5. Deployment-gate Off renders a read-only explanation rather than usable mutation controls.
6. Field-level API errors, stale readiness, failed transitions, and optimistic UI rollback are accessible and tested.

## Test plan

- Component tests for every mode, readiness state, failure, and transition.
- Storybook coverage for new reusable controls.
- Keyboard/focus/label review against updated mockups.
