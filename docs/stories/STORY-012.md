# STORY-012: Centralize provider lifecycle and enforce one active provider

- **Epic:** Optional SSO lifecycle and production hardening
- **Points:** 5
- **Status:** backlog
- **Depends on:** none
- **Requirements:** FR-019, FR-020, FR-021, NFR-011

## Value

As an instance administrator, I want deterministic and reversible SSO modes so that provider changes cannot create ambiguous authentication behavior or lock out members.

## Acceptance criteria

1. Effective mode is exactly Disabled, Optional, or Enforced and all changes use one transactional lifecycle service.
2. The initial release permits exactly one enabled or enforced provider, including concurrent API updates.
3. Enforced to Optional to Disabled transitions preserve provider configuration and identity records and invalidate public configuration caches immediately.
4. Disabling is rejected with actionable guidance when no usable normal member authentication method remains.
5. Claim mapping keys and values are restricted to supported non-empty strings; malformed input returns a field error rather than causing login failure.
6. Relevant configuration changes invalidate prior readiness evidence.

## Test plan

- Model/service concurrency tests and API transition matrix.
- Cache invalidation and data-preservation tests.
- Serializer malformed-input regression tests.
