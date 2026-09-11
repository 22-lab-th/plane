# Sprint Plan: Plane SSO Authentication

- **Created:** 2026-09-10
- **Delivery model:** Three sequential implementation sprints
- **Approval owner:** `bmad-orchestrator` under D-002
- **Total:** 16 stories, 102 points

## Goal

Deliver secure, instance-wide generic OIDC SSO with provider administration,
verified identity resolution, optional and enforced sign-in, recovery,
observability, tests, and operator documentation.

## Sprint 1 — Secure OIDC backend (32 points)

| Story     | Title                                                 | Points | Depends on           |
| --------- | ----------------------------------------------------- | -----: | -------------------- |
| STORY-001 | Stabilize the existing OAuth baseline                 |      3 | None                 |
| STORY-002 | Manage structured SSO providers and protected secrets |      8 | None                 |
| STORY-003 | Discover and test an OIDC provider safely             |      5 | STORY-002            |
| STORY-004 | Sign in a pre-linked user through validated OIDC      |      8 | STORY-002, STORY-003 |
| STORY-005 | Link and provision SSO identities safely              |      8 | STORY-004            |

Sprint 1 exits when the backend can configure and test an IdP, authenticate a
pre-linked user, and apply atomic verified-email/JIT identity policy with
security-negative contract tests.

## Sprint 2 — Product integration and hardening (31 points)

| Story     | Title                                              | Points | Depends on           |
| --------- | -------------------------------------------------- | -----: | -------------------- |
| STORY-006 | Offer OIDC SSO on the Plane sign-in screen         |      5 | STORY-004, STORY-005 |
| STORY-007 | Configure and test OIDC from God Mode              |      8 | STORY-002, STORY-003 |
| STORY-008 | Enforce SSO with protected administrator recovery  |      8 | STORY-006, STORY-007 |
| STORY-009 | Complete logout, audit, and operational visibility |      5 | STORY-008            |
| STORY-010 | Prove compatibility and release readiness          |      5 | STORY-001–STORY-009  |

Sprint 2 exits when an administrator can safely configure, test, enable, and
enforce SSO; members can sign in from Plane; recovery and logout work; and all
required validation evidence is recorded.

## Sprint 3 — Optional lifecycle and production hardening (39 points)

| Story     | Title                                                         | Points | Depends on                      |
| --------- | ------------------------------------------------------------- | -----: | ------------------------------- |
| STORY-011 | Make no-OIDC mode a supported deployment profile              |      5 | None                            |
| STORY-012 | Centralize provider lifecycle and enforce one active provider |      5 | None                            |
| STORY-013 | Prove client configuration with an interactive OIDC test      |      8 | STORY-012                       |
| STORY-014 | Harden authorization and break-glass recovery                 |      8 | STORY-012                       |
| STORY-015 | Deliver reversible Admin UX and component tests               |      5 | STORY-011, STORY-013, STORY-014 |
| STORY-016 | Complete audit, compatibility, and release evidence           |      8 | STORY-011, STORY-013–STORY-015  |

Sprint 3 exits only when OIDC is disabled by default, 22lab normal login has
no OIDC dependency, Disabled/Optional/Enforced transitions and rollback pass,
recovery is exercised, and a real customer IdP smoke test is recorded.

## Dependency order

```text
STORY-001 ───────────────────────────────────────────────┐
STORY-002 → STORY-003 → STORY-004 → STORY-005 → STORY-006│
     │          │                              │          ├→ STORY-010
     └──────────┴→ STORY-007 ─────────────────→ STORY-008 → STORY-009

STORY-011 ───────────────┐
STORY-012 → STORY-013 ───┼→ STORY-015 → STORY-016
       └──→ STORY-014 ───┘
```

## Capacity basis

There is no historical team velocity. Estimates use conservative relative
sizing for one sequential agent and include implementation, tests, review,
and documentation. Sprint boundaries are milestones rather than calendar
commitments; the orchestrator continues across both without a routine human
approval pause.

## Definition of done

- All Given/When/Then criteria are evidenced.
- Code and meaningful tests land together.
- Authentication-critical paths meet the 90% target where measurement is practical.
- Relevant targeted tests, type checks, lint, formatting, and regression suites pass.
- Reviewer finds no unresolved critical/high security or correctness issue.
- Disabled, Optional, Enforced, disable, and rollback mode-matrix tests pass.
- 22lab normal authentication and the customer staging OIDC flow are both evidenced.
- Story status, requirement traceability, decisions, and validation evidence are updated.
- No production deployment or real IdP credentials are required for story completion.
