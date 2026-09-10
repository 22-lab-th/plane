# Adoption Report — Plane SSO Authentication (Gate A1: Approved)

> Produced by `bmad-auto:adopt`. The original plan is untouched; these artifacts only index it and fill the UX-reference gap.

## TL;DR

- Plan found in `docs/sso-auth-implementation-plan.md`; overall readiness is **ready with one reconstructed UX item**.
- Sixteen functional and eight non-functional requirements were indexed for story traceability.
- Three static HTML mockups were reconstructed from the plan and accepted through the user's delegated approval authority.
- No plan-vs-code conflict blocks development. Existing social OAuth is a reusable integration point, not an OIDC implementation.
- This indexed plan is the development contract. `bmad-orchestrator` owns evidence-based approvals through delivery under D-002.

## Entry-criteria mapping

| #   | Criterion        | Found in                          | Quality | Action taken                                                                   |
| --- | ---------------- | --------------------------------- | ------- | ------------------------------------------------------------------------------ |
| E1  | Requirements     | Original §§1, 4–8                 | OK      | Indexed as FR-001–FR-016 and NFR-001–NFR-008 in `docs/auto/plan.md`            |
| E2  | Scope boundaries | Original §§2, 6, 11               | OK      | Indexed with `apps/space` boundary made explicit                               |
| E3  | Tech decisions   | Original §§3–6                    | OK      | Extracted into `docs/auto/architecture.md`                                     |
| E4  | Quality targets  | Original §§5, 7–8 and `AGENTS.md` | OK      | Security validation, test layers, auth coverage, and repository checks indexed |
| E5  | UX reference     | Behavioral requirements only      | Missing | Reconstructed three HTML mockups                                               |
| E6  | Success criteria | Original §8                       | OK      | Mapped to requirement-level delivery criteria                                  |

Normalized index files: `docs/auto/plan.md`, `docs/auto/architecture.md`

## Reconstructed items

| #   | Item                                                                                  | Source of reconstruction                                                      | Where it now lives                   |
| --- | ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------ |
| R1  | Sign-in, provider configuration, and enforcement/recovery UX reference                | Original behavior, existing Plane auth/admin patterns, and WCAG 2.1 AA intent | `docs/auto/ux/mockups/*.html`, D-001 |
| R2  | New controls explicitly target keyboard navigation, visible focus, and labeled fields | BMAD quality default and repository component conventions                     | NFR-007 and mockups                  |

## Plan-vs-code conflicts

None requiring a product decision. The following implementation gaps are expected and already covered by the plan:

- `Account` does not model issuer-scoped OIDC identities; the plan adds dedicated models.
- Existing OAuth validates `state` but is not a complete OIDC ID Token flow; the plan adds a protocol adapter.
- `apps/web` has an empty extended OAuth hook and Admin exposes only core auth methods; the plan fills these extension points.
- Existing Admin configuration serialization reveals decrypted secrets; the SSO API will use a masked, write-only contract.

## Risks carried into development

| Risk                                    | Why                                          | Planned mitigation                                                                              |
| --------------------------------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Account takeover through unsafe linking | Existing user email can match an IdP claim   | Resolve by provider+subject first; require verified email and explicit policy for linking       |
| Admin lockout                           | Enforced SSO depends on an external IdP      | Test-before-enforce, backend break-glass path, audit and rollout rehearsal                      |
| SSRF through issuer/discovery URLs      | Admin-supplied URLs trigger backend requests | HTTPS and issuer validation, bounded timeouts, IP pinning and redirect checks                   |
| Protocol-validation defects             | OIDC validation is security critical         | Maintained library, pinned safe release, negative contract tests and 90% critical-path coverage |
| External compatibility variance         | Entra ID, Okta and Keycloak claims differ    | Configurable safe claim mapping and provider integration matrix                                 |
| Scope growth into provisioning          | SSO often expands into SCIM and role mapping | Preserve explicit deferred-work boundary                                                        |

## Revision ideas

1. Keep one active OIDC provider in the first Admin UI while preserving multi-provider schema support.
2. Keep `apps/space` outside the first delivery unless an enforcement-boundary test proves private Space requires it.
3. Deliver SAML and SCIM as separate follow-up projects after OIDC production evidence.

## Already-built audit

| Requirement area                            | Status      | Evidence                                                      |
| ------------------------------------------- | ----------- | ------------------------------------------------------------- |
| Django session creation and device metadata | Implemented | `plane/authentication/utils/login.py`                         |
| Post-auth invitation workflow               | Implemented | `plane/authentication/utils/user_auth_workflow.py`            |
| Safe callback redirect                      | Implemented | Current social OAuth callback views                           |
| Encrypted instance secrets                  | Partial     | Existing `InstanceConfiguration`; response masking is missing |
| Generic OIDC protocol                       | Absent      | No generic OIDC provider/routes/models                        |
| SSO identity and policy                     | Absent      | No issuer+subject identity model or enforcement service       |
| Admin SSO configuration                     | Absent      | Core auth methods only                                        |
| Web SSO entry point                         | Absent      | Extended OAuth hook returns no options                        |

Proposed development breakdown: 7–10 thin vertical stories after the build-state audit.

## Gate A1 decision

- **Decision:** Approved
- **Authority:** User-delegated approval to `bmad-orchestrator` (D-002)
- **Evidence:** E1–E6 mapping is complete; the only reconstructed items are documented; there are no blocking plan-vs-code conflicts; security, testing, recovery, and rollback criteria are explicit.
- **Result:** P1 is waived-adopted, P2 passes, and development is unlocked.

## Changelog

| Date       | Change                                                                                 | Source                               |
| ---------- | -------------------------------------------------------------------------------------- | ------------------------------------ |
| 2026-09-10 | Initial adoption mapping, requirement index, architecture index, and UX reconstruction | Existing SSO plan and codebase audit |
| 2026-09-10 | Gate A1 approved and future gate authority delegated to `bmad-orchestrator`            | Explicit user instruction and D-002  |
