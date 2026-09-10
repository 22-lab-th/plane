# STORY-010: Prove compatibility and release readiness

- **Epic:** Delivery · **Points:** 5 · **Status:** done
- **Depends on:** STORY-001–STORY-009
- **Requirements:** FR-014, FR-016, FR-017, NFR-001–NFR-009

## Value

As an instance operator, I want evidence and runbooks for the completed SSO feature so that rollout and rollback are controlled and supportable.

## Acceptance criteria

1. **Given** representative Entra ID, Okta, and Keycloak metadata/claim fixtures **when** compatibility tests run **then** all providers satisfy the generic OIDC contract or documented configuration mapping.
2. **Given** the completed change **when** repository checks run **then** formatting, lint, types, builds, relevant frontend tests, Docker-backed Django tests, and measured auth coverage pass.
3. **Given** an adversarial code review **when** security/correctness/test lenses complete **then** no unresolved critical/high issue remains.
4. **Given** an operator following documentation **when** configuring, testing, enabling, enforcing, recovering, rotating a secret, disabling, and rolling back SSO **then** every step has an exact command or UI path and no real credential is committed.
5. **Given** delivery evidence **when** `bmad-orchestrator` evaluates D1 **then** requirement traceability, deviations, limitations, test results, and rollback are recorded before approval.

## Technical notes

- Use mocked/local fixtures when real IdP credentials are unavailable; record the limitation accurately.
- Produce `docs/auto/delivery-report.md` and operator documentation.

## Test plan

- Full repository validation appropriate to changed packages.
- Security review and final requirement traceability audit.

## Completion evidence

- Commits: working-tree delivery documented in `docs/auto/delivery-report.md`
- Tests and coverage: provider compatibility, 91% auth-critical branch coverage, API/frontend regression checks, and Playwright SSO browser tests (3/3) passed
- Review: D1 PASS in `docs/bmad/gate-check.md`; no unresolved critical/high finding
