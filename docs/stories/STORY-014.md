# STORY-014: Harden authorization and break-glass recovery

- **Epic:** Optional SSO lifecycle and production hardening
- **Points:** 8
- **Status:** backlog
- **Depends on:** STORY-012
- **Requirements:** FR-006, FR-010, FR-022, NFR-002, NFR-004

## Value

As an operator, I want login authorization and recovery revalidated continuously so that group removal revokes access and enforced SSO always has a working recovery path.

## Acceptance criteria

1. Subject resolution remains stable, then current domain/group authorization policy is evaluated on every SSO login.
2. A linked user removed from an allowed group is denied on the next login without changing or deleting the identity link.
3. Domain-policy behavior for changed verified email claims is explicit, tested, and never switches identity ownership.
4. A viable break-glass account is allowlisted, active, non-bot, a verified InstanceAdmin, and has a usable password.
5. Enforcement requires a recent successful recovery re-auth test; no password or credential is retained.
6. Recovery attempts remain rate-limited, short-lived, audited, and non-enumerating.

## Test plan

- Identity resolution versus authorization-policy unit tests.
- Recovery viability and re-auth contract tests.
- IdP outage E2E proving recovery can end enforcement.
