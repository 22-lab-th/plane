# STORY-016: Complete audit, compatibility, and release evidence

- **Epic:** Optional SSO lifecycle and production hardening
- **Points:** 8
- **Status:** backlog
- **Depends on:** STORY-011, STORY-013, STORY-014, STORY-015
- **Requirements:** FR-012, FR-014, FR-016, FR-020, NFR-002, NFR-008, NFR-010, NFR-011

## Value

As an operator delivering Plane to 22lab and customers, I want objective mode, rollback, and compatibility evidence so that the same build is safe with or without OIDC.

## Acceptance criteria

1. Audit events include correlation IDs and distinct enable, disable, enforce, unenforce, test, recovery, and denied-transition outcomes without secrets or tokens.
2. The complete Disabled/Optional/Enforced test matrix passes in Docker and Playwright.
3. 22lab passes normal-login smoke testing with the deployment gate Off and no IdP dependency.
4. Customer staging passes a real-tenant OIDC smoke test before enforcement.
5. Reverse-proxy HTTPS, callback URI, forwarded headers, cookie domain, secret rotation, IdP outage, disable, rollback, and recovery are exercised.
6. No critical/high review finding remains and a new delivery report replaces the prior D1 decision.

## Test plan

- Full relevant API, frontend, E2E, migration, lint, type, and build checks.
- Independent correctness/security review.
- Signed 22lab and customer-staging rollout checklist without storing credentials.
