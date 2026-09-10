# STORY-008: Enforce SSO with protected administrator recovery

- **Epic:** Access policy · **Points:** 8 · **Status:** done
- **Depends on:** STORY-006, STORY-007
- **Requirements:** FR-009, FR-010, FR-016, NFR-002, NFR-004, NFR-008, NFR-009

## Value

As an instance administrator, I want to enforce SSO with tested recovery so that members cannot bypass organizational authentication and admins retain emergency access.

## Acceptance criteria

1. **Given** a tested enabled provider and configured recovery administrator **when** enforcement is enabled **then** the transition succeeds and is audited.
2. **Given** enforced SSO **when** a non-recovery user directly calls password, magic-code, or social OAuth entry points **then** the backend denies authentication consistently.
3. **Given** enforced SSO **when** sign-in loads **then** it prioritizes/directs to SSO without creating redirect loops after IdP failure.
4. **Given** an IdP outage **when** an authorized break-glass administrator uses the recovery route **then** rate limiting, short admin-session lifetime, and auditing apply.
5. **Given** no tested provider or viable recovery account **when** enforcement is requested **then** Plane rejects it.

## Technical notes

- Centralize enforcement policy so every credential/social endpoint uses the same decision.
- Keep recovery configuration deployment-controlled and do not expose whether a supplied email is privileged.

## Test plan

- Unit: enforcement/recovery policy matrix.
- Contract: direct endpoint bypass attempts, enable preconditions, recovery rate limit and audit.
- UI: compare with `docs/auto/ux/mockups/enforcement.html`.

## Completion evidence

- Commits: working-tree delivery; backend enforcement and restricted break-glass recovery
- Tests and coverage: direct-route enforcement, recovery authorization/rate/session tests passed
- Review: approved by `bmad-orchestrator` at D1
