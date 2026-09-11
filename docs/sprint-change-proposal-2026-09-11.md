# Sprint Change Proposal: Optional and Reversible OIDC SSO

- **Date:** 2026-09-11
- **Project:** Plane SSO Authentication
- **Change type:** Moderate course correction
- **Recommended path:** Direct adjustment with a new hardening sprint
- **Status:** Approved by the user on 2026-09-11; ready for implementation handoff

## 1. Issue Summary

The current delivery contains a functional OIDC foundation, but production readiness cannot be accepted yet. The review found unresolved high-risk behavior in provider lifecycle, configuration testing, identity authorization, and administrator recovery.

The stakeholder requirement is now explicit:

1. The same Plane build must continue to support the existing 22lab deployment using normal authentication with no OIDC dependency.
2. OIDC is an optional customer capability, not a mandatory system dependency.
3. Administrators must be able to disable OIDC and return to normal authentication without deleting provider or identity data.
4. Enforced SSO must never be enabled unless OIDC login and administrator recovery have both been exercised successfully.

This is a clarification and strengthening of FR-016, plus a production-readiness correction triggered by the code review. It does not invalidate the existing OIDC protocol implementation.

### Evidence

- Disabled providers are currently omitted from the public sign-in response, but the disabled behavior is not treated as a complete, tested operating mode.
- The database permits more than one enabled or enforced provider while runtime code selects the first provider.
- The current configuration test validates discovery and JWKS only; it does not prove that client credentials can complete an authorization-code exchange.
- Recovery viability does not require a verified administrator with a usable password.
- Existing linked identities do not re-evaluate group/domain authorization policy.
- Admin state transitions and normal-login regression behavior lack focused frontend tests.

## 2. Impact Analysis

### Epic impact

Existing epics E1-E4 remain historically valid but their delivery status is no longer sufficient for a production release. Add a new epic:

- **E5 — Optional SSO lifecycle and production hardening**
  - Makes disabled/no-OIDC operation a first-class supported mode.
  - Repairs lifecycle, recovery, authorization, audit, and validation findings.
  - Reopens the final release gate after real deployment smoke testing.

No completed story needs to be rolled back. STORY-002, STORY-003, STORY-005, STORY-007, STORY-008, STORY-009, and STORY-010 require superseding acceptance criteria through new stories.

### Requirements impact

The MVP remains achievable. Add the following Must requirements:

- **FR-018 — Deployment capability gate:** `ENABLE_OIDC_SSO` defaults to disabled. When disabled, OIDC Admin controls, public provider metadata, initiation/callback behavior, and enforcement are inactive while existing authentication works unchanged.
- **FR-019 — Explicit operating modes:** The effective instance mode is exactly one of `disabled`, `optional`, or `enforced`, with guarded atomic transitions.
- **FR-020 — Reversible disable:** An administrator can transition `enforced → optional → disabled` without deleting provider configuration or identities. Normal authentication is restored immediately when enforcement ends.
- **FR-021 — Authentication availability:** The system prevents a transition that would leave members with no usable authentication method and identifies which normal method must be enabled first.
- **FR-022 — Verified readiness:** Enforced mode requires a successful interactive OIDC login test for the current issuer/client/secret configuration and a successful recovery test by a viable break-glass administrator.
- **NFR-010 — Mode isolation:** With OIDC disabled, no request to the IdP is made and normal-auth behavior, latency, and availability do not depend on OIDC configuration or IdP health.
- **NFR-011 — Reversible rollout:** Mode changes are atomic, audited, cache-invalidated, and reversible without destructive data changes.

### Architecture impact

Replace loosely coupled booleans as the behavioral authority with a single lifecycle service. The database may retain `is_enabled` and `is_enforced` for migration compatibility, but all reads and writes must pass through an explicit state mapping and transition API.

| Deployment gate | Provider state        | Effective mode | Member sign-in behavior                                      |
| --------------- | --------------------- | -------------- | ------------------------------------------------------------ |
| Off             | Any stored state      | Disabled       | Existing password, magic-code, and enabled social OAuth only |
| On              | Disabled              | Disabled       | Existing authentication only; no SSO button                  |
| On              | Enabled, not enforced | Optional       | SSO plus existing enabled methods                            |
| On              | Enabled and enforced  | Enforced       | SSO only for members; tested break-glass path for God Mode   |

Required transition rules:

- `disabled → optional`: one provider only, valid typed configuration, current interactive login test passed.
- `optional → enforced`: current interactive login test passed, viable recovery admin verified, recovery test passed, confirmation completed.
- `enforced → optional`: always available to an authorized/recovery administrator and applied immediately.
- `optional → disabled`: allowed without deleting configuration/identities after confirming at least one normal member authentication method is usable.
- Deployment gate Off overrides stored provider state to effective Disabled and emits an operator-visible configuration warning if stored enforcement remains true.

The provider lifecycle service must also enforce a single enabled/enforced provider in the initial release using a database constraint where possible and transaction-level locking for portable correctness.

### UI/UX impact

- God Mode must show a prominent mode selector/status: **Disabled**, **Optional**, **Enforced**.
- Disabled must be the default and must clearly say that existing Plane login remains active.
- “Test metadata” and “Test OIDC login” must be separate actions. Only the interactive login test grants readiness to enable/enforce.
- Disabling must show which normal authentication methods will remain available and must not imply that provider data will be deleted.
- Enforcing must use the existing confirmation design and additionally show recovery test status.
- If the deployment gate is Off, God Mode shows a read-only explanation and the environment variable needed by customer deployments; the normal sign-in UI remains unchanged.
- Validation errors must render field-level API details rather than a generic save failure.

### Operational and artifact impact

Update the development contract, architecture index, stories, sprint status, runbook, environment examples, public instance contract, test harness, audit event list, delivery report, and gate decision. Existing migrations remain additive; provider and identity data must not be removed during rollback.

## 3. Recommended Approach

Use **Direct Adjustment**: preserve the protocol implementation and add E5 as a hardening sprint.

- **Why:** The core authorization-code, PKCE, state, nonce, JWKS, token-validation, secret-storage, and session work is reusable. The remaining work is concentrated around lifecycle and production safety.
- **Estimated engineering effort:** 10-15 working days for one engineer, plus 2-5 days of customer/IdP coordination that can overlap.
- **Risk:** Medium-high because authentication transitions can lock out users; mitigated by disabled-by-default rollout, atomic transitions, recovery verification, and mode-matrix tests.
- **Rollback option:** Not recommended. Removing the OIDC implementation would discard sound protocol work and would not improve the customer delivery path.
- **MVP reduction option:** Keep multiple active providers, SAML, SCIM, and per-workspace policies deferred. Support exactly one active OIDC provider in this release.

## 4. Detailed Change Proposals

### PRD / development contract

**Section: Scope and assumptions**

OLD:

> SSO policy applies at instance scope, and the first release enables one IdP.

NEW:

> OIDC SSO is an optional, disabled-by-default instance capability. The product supports Disabled, Optional, and Enforced modes. The same build must operate normally without OIDC. The first release permits exactly one enabled provider, and disabling OIDC preserves configuration and identity records.

**Section: Acceptance criteria**

OLD:

> Existing optional-auth behavior has no regression.

NEW:

> With the deployment gate or provider mode disabled, public configuration exposes no SSO provider, no IdP request occurs, password/magic-code/social OAuth retain their configured behavior, and an administrator can later enable or disable OIDC without data deletion. Every transition is covered by backend and frontend mode-matrix tests.

### Architecture

**Section: Component boundaries**

OLD:

> SSO policy service handles enabled/enforced/JIT/domain/group/recovery decisions.

NEW:

> An SSO lifecycle service is the sole authority for the effective Disabled/Optional/Enforced mode and guarded transitions. Identity resolution establishes the stable user link; a separate authorization-policy evaluation runs on every login. Recovery readiness and configuration readiness are explicit, time-stamped states invalidated by relevant changes.

### Existing story clarifications

- **STORY-003:** Rename the current operation to metadata validation. A successful full configuration test requires an interactive authorization-code callback using the current client configuration.
- **STORY-005:** Separate identity resolution from login authorization. Existing subject links remain stable, but configured group/domain access policy is re-evaluated on every login.
- **STORY-007:** Replace independent enable/enforce toggles with explicit mode transitions and a reversible Disable action.
- **STORY-008:** A viable recovery account must be an active, non-bot, verified instance admin with a usable password and a recent successful recovery test.
- **STORY-009:** Add correlation IDs and distinct provider-enabled, provider-disabled, enforcement-enabled, enforcement-disabled, test, and transition-denied events.
- **STORY-010:** Reopen delivery readiness until the new E5 tests and a real customer IdP smoke test pass.

### New implementation stories

#### STORY-011 — Make no-OIDC mode a supported deployment profile (5 points)

- Add `ENABLE_OIDC_SSO`, default Off in environment examples and 22lab deployment configuration.
- When Off, suppress Admin mutation controls and public provider metadata; reject OIDC initiation/callback safely; bypass all SSO enforcement logic.
- Prove existing password, magic-code, Google, GitHub, GitLab, and Gitea behavior is unchanged.
- Add startup/config diagnostics for stored enforced state while the deployment gate is Off.

#### STORY-012 — Centralize provider lifecycle and enforce one active provider (5 points)

- Implement Disabled/Optional/Enforced state mapping and atomic transition service.
- Add single-active-provider invariant and database/transaction protection.
- Validate `claim_mappings` keys and values as supported non-empty strings.
- Invalidate readiness when issuer, client ID, secret, protocol, scopes, redirect base, or required claim mappings change.

#### STORY-013 — Prove client configuration with an interactive OIDC test (8 points)

- Retain metadata/JWKS validation as a fast preflight.
- Add an Admin-initiated test transaction that completes authorization code exchange, validates the ID token, and returns test status without creating/linking a Plane user or persistent member session.
- Store test timestamp plus a configuration fingerprint; never store codes or tokens.
- Reject enable/enforce when the fingerprint no longer matches.
- Cover wrong secret, unsupported token auth method, callback mismatch, cancellation, timeout, and key rotation.

#### STORY-014 — Harden authorization and break-glass recovery (8 points)

- Re-evaluate group/domain authorization policy after subject resolution on every SSO login.
- Define domain policy semantics explicitly for changed verified emails without changing identity ownership.
- Require active, non-bot, verified InstanceAdmin, usable password, deployment allowlist membership, and recent recovery verification.
- Add a recovery self-test/re-auth flow and record `recovery_tested_at` without storing credentials.
- Add regression tests proving an IdP group removal revokes the next login and an unusable recovery account cannot enable enforcement.

#### STORY-015 — Deliver reversible Admin UX and component tests (5 points)

- Implement mode status/selector, readiness indicators, normal-auth availability summary, disable confirmation, and enforcement confirmation.
- Preserve provider/identity data on disable.
- Render field-level API errors and safe test diagnostics.
- Add component tests for disabled defaults, failed saves/tests, stale readiness, all transitions, and rollback of optimistic UI state.
- Move reusable form/status controls to the shared design system with Storybook coverage where appropriate.

#### STORY-016 — Complete audit, compatibility, and release evidence (8 points)

- Add request correlation IDs and distinct lifecycle audit events.
- Run the full Disabled/Optional/Enforced matrix in Docker and Playwright.
- Test 22lab with the deployment gate Off and existing normal login methods.
- Run a real-tenant smoke test for the target customer's IdP before customer enforcement.
- Validate reverse proxy HTTPS, callback URI, forwarded headers, cookie domain, secret rotation, IdP outage, disable, rollback, and break-glass recovery.
- Replace the prior D1 PASS with a new evidence-based gate decision.

### Dependency order

```text
STORY-011 ───────────────┐
STORY-012 → STORY-013 ───┼→ STORY-015 → STORY-016
       └──→ STORY-014 ───┘
```

## 5. Test and Acceptance Matrix

| Scenario                                                | Expected result                                                                     |
| ------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Fresh 22lab deployment, gate Off, no provider           | Existing sign-in UI/API unchanged; zero IdP calls                                   |
| Stored provider, gate Off                               | Effective Disabled; normal login works; Admin sees configuration warning            |
| Gate On, provider Disabled                              | No SSO button; normal methods work                                                  |
| Optional                                                | SSO and all independently enabled normal methods work                               |
| Enforced                                                | Member legacy endpoints are blocked; SSO works; verified break-glass God Mode works |
| Enforced → Optional                                     | Legacy methods return immediately; identities/config remain                         |
| Optional → Disabled                                     | SSO disappears; normal methods remain; identities/config remain                     |
| Disable with no normal method available                 | Transition rejected with actionable guidance                                        |
| Wrong/rotated client secret                             | Interactive test fails; enable/enforce rejected                                     |
| Linked user removed from allowed group                  | Next SSO login denied and audited                                                   |
| Break-glass user has unusable password or is unverified | Enforcement rejected                                                                |
| IdP outage in Optional mode                             | Normal login remains usable                                                         |
| IdP outage in Enforced mode                             | Member login fails safely; tested recovery admin can disable enforcement            |

Release acceptance requires all matrix rows, targeted security tests, frontend component tests, Docker-backed API tests, Playwright E2E, lint/types/build, migration drift, and customer IdP smoke testing to pass.

## 6. Rollout Plan

1. Merge schema/service changes with `ENABLE_OIDC_SSO=0` everywhere.
2. Deploy to 22lab with the gate Off and run normal-login regression smoke tests.
3. Enable the gate only in a customer staging environment.
4. Configure one provider; run metadata validation and interactive login test.
5. Enable Optional mode and verify both SSO and normal login.
6. Exercise recovery and disable/rollback paths.
7. Enable Enforced mode only in a maintenance window after customer approval.
8. Monitor login success/failure, transition, recovery, and provider-health audit events.

Rollback is always `Enforced → Optional → Disabled`; keep provider and identity records. Application rollback must leave additive migrations applied.

## 7. Implementation Handoff

- **Scope classification:** Moderate; backlog reorganization and implementation are required, but no fundamental rearchitecture is needed.
- **Product owner:** Accept FR-018 through FR-022 and the exact semantics of continuous domain/group authorization.
- **Architect/security reviewer:** Approve lifecycle transitions, single-provider invariant, interactive test flow, recovery proof, and audit model.
- **Developer:** Implement STORY-011 through STORY-016 with tests and update affected documents.
- **QA/operator:** Run the mode matrix in 22lab/staging, verify rollback, and execute the real customer IdP smoke test.

### Definition of done

- OIDC is Off by default and 22lab normal login passes unchanged.
- An administrator can disable OIDC without deleting configuration or identity data.
- Exactly one effective mode and one active provider exist.
- Enable/enforce requires current interactive OIDC readiness evidence.
- Enforce requires a verified, usable, tested recovery administrator.
- Access policy is enforced on every SSO login.
- All high-severity review findings are closed and independently re-reviewed.
- A new delivery report and gate decision replace the previous production-readiness claim.

## 8. Checklist Status

- [x] Trigger, category, and evidence documented.
- [x] Epic and story impacts assessed.
- [x] PRD, architecture, UX, operations, testing, and delivery artifacts assessed.
- [x] Direct adjustment selected over rollback or MVP expansion.
- [x] Detailed requirement/story changes, sequencing, estimates, and risks defined.
- [x] Handoff roles and success criteria defined.
- [x] User approval received on 2026-09-11.
