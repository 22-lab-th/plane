# Plane SSO Development Contract Index

This is a thin adoption index. The source of truth remains
[`docs/sso-auth-implementation-plan.md`](../sso-auth-implementation-plan.md).
The identifiers below normalize that plan for story traceability without
rewriting it.

## Functional requirements

| ID     | Priority | Requirement                                                                                                                                                                                                     | Source                               |
| ------ | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| FR-001 | Must     | An instance administrator can create, read, update, disable, and delete an OIDC provider without receiving its stored client secret in plaintext.                                                               | Original §§1, 4.3, 6 Phase 3         |
| FR-002 | Must     | An instance administrator can test discovery and provider configuration before enabling or enforcing it.                                                                                                        | Original §§4.3, 6 Phase 2–3, 8       |
| FR-003 | Must     | Plane starts an OIDC Authorization Code transaction using PKCE S256, state, nonce, an exact callback URI, and a short-lived session-bound transaction.                                                          | Original §5.1                        |
| FR-004 | Must     | Plane validates discovery metadata, the token response, ID Token signature, algorithm, issuer, audience, authorized party, time claims, nonce, subject, email, and verified-email status before authentication. | Original §§5.1, 8                    |
| FR-005 | Must     | Plane resolves an existing SSO user by provider and subject and updates safe identity metadata without changing the linked Plane account when the IdP email changes.                                            | Original §§4.1–4.2, 8                |
| FR-006 | Must     | Plane applies verified-email auto-link, JIT provisioning, allowed-domain, and allowed-group policies atomically and creates no partial user data when policy fails.                                             | Original §§4.2, 6 Phase 2, 8         |
| FR-007 | Must     | A successful SSO callback runs the existing post-auth workflow, creates a rotated Django session, consumes transaction data once, and redirects only to a safe Plane path.                                      | Original §§3.2, 5.4, 6 Phase 2       |
| FR-008 | Must     | The public instance response and main web sign-in UI expose enabled SSO providers using non-sensitive data and allow the user to start SSO.                                                                     | Original §6 Phase 4                  |
| FR-009 | Must     | Enforced SSO is applied by the backend to password, magic-code, and social OAuth entry points and cannot be bypassed by direct requests.                                                                        | Original §§5.3, 6 Phase 4, 8         |
| FR-010 | Must     | At least one restricted, rate-limited, audited break-glass path remains available to instance administrators when SSO is enforced or the IdP is unavailable.                                                    | Original §§5.3, 8                    |
| FR-011 | Should   | Local sign-out always destroys the Plane session and uses RP-initiated logout when supported without making IdP logout a dependency of local logout.                                                            | Original §§5.4, 6 Phase 5            |
| FR-012 | Must     | Plane records provider administration, configuration tests, enforcement changes, identity links, and categorized SSO login outcomes without logging credentials or tokens.                                      | Original §5.5                        |
| FR-013 | Must     | SSO client secrets are encrypted at rest, write-only through APIs, masked in the Admin UI, and replaceable without exposing the previous value.                                                                 | Original §§3.4, 5.5, 6 Phase 1 and 3 |
| FR-014 | Must     | The generic OIDC implementation interoperates with Microsoft Entra ID, Okta, and Keycloak.                                                                                                                      | Original §§1, 7, 8                   |
| FR-015 | Should   | OIDC discovery and JWKS data are cached safely and refreshed on signing-key rotation.                                                                                                                           | Original §§6 Phase 2, 7              |
| FR-016 | Must     | Existing password, magic-code, social OAuth, deactivated-user, bot-user, invitation, and onboarding behavior remains unchanged while SSO is optional.                                                           | Original §§3.2, 6 Phase 1, 7         |
| FR-017 | Must     | `bmad-orchestrator` routes, evaluates, and records all remaining workflow and delivery gate decisions under the user's delegated authority until completion.                                                    | Original §2 Delivery governance      |
| FR-018 | Must     | OIDC is protected by a deployment capability gate that defaults to disabled; while disabled, existing authentication remains independent of OIDC and no IdP request is made.                                    | Approved change proposal 2026-09-11  |
| FR-019 | Must     | The effective instance authentication mode is exactly one of Disabled, Optional, or Enforced, with guarded atomic transitions and one active OIDC provider in the initial release.                              | Approved change proposal 2026-09-11  |
| FR-020 | Must     | An administrator can transition Enforced to Optional to Disabled without deleting provider configuration or identity records, and normal authentication returns when enforcement ends.                          | Approved change proposal 2026-09-11  |
| FR-021 | Must     | The system rejects a transition that would leave members with no usable authentication method and reports which normal method must be enabled first.                                                            | Approved change proposal 2026-09-11  |
| FR-022 | Must     | Enforced mode requires a current successful interactive OIDC login test and a successful recovery test by a viable break-glass administrator.                                                                   | Approved change proposal 2026-09-11  |

## Non-functional requirements

| ID      | Priority | Requirement                                                                                                                                                                 | Source                                        |
| ------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| NFR-001 | Must     | OIDC behavior follows OpenID Connect Core, OpenID Connect Discovery, and OAuth Security BCP RFC 9700.                                                                       | Original §5.1                                 |
| NFR-002 | Must     | Authentication-critical code reaches at least 90% meaningful branch coverage and includes unit, contract, integration, and frontend tests listed in the original test plan. | Original §§7–8 and repository `AGENTS.md`     |
| NFR-003 | Must     | All outbound discovery, JWKS, token, and UserInfo requests have bounded timeouts and SSRF-safe destination and redirect validation.                                         | Original §5.1                                 |
| NFR-004 | Must     | Authorization codes, tokens, raw assertions, and client secrets never appear in application logs or public/admin API responses.                                             | Original §§5.5, 8                             |
| NFR-005 | Must     | Database constraints and transactions prevent duplicate identities and partial JIT provisioning under concurrent callbacks.                                                 | Original §4.2                                 |
| NFR-006 | Must     | Schema rollout is backward-compatible while SSO remains disabled and can be rolled back without deleting providers or identities.                                           | Original §9                                   |
| NFR-007 | Should   | New interactive Admin and sign-in controls follow the existing Plane design system, keyboard navigation, visible focus, form labels, and WCAG 2.1 AA intent.                | Reconstructed UX quality default; see mockups |
| NFR-008 | Must     | Frontend checks and the relevant Docker-backed Django authentication suite pass before delivery.                                                                            | Original §8 and repository `AGENTS.md`        |
| NFR-009 | Must     | Automated gate approval is evidence-based and cannot waive security controls, required tests, critical/high findings, traceability, or mandatory human escalations.         | Original §2 Delivery governance               |
| NFR-010 | Must     | Disabled mode makes normal-auth behavior, latency, and availability independent of OIDC configuration and IdP health.                                                       | Approved change proposal 2026-09-11           |
| NFR-011 | Must     | Mode changes are atomic, audited, cache-invalidated, reversible, and preserve provider and identity data.                                                                   | Approved change proposal 2026-09-11           |

## Scope boundaries

In scope for the development contract:

- OIDC disabled by default, with Disabled, Optional, and Enforced operating modes
- Existing 22lab password/magic-code/social authentication with no OIDC dependency
- Reversible disable without deleting provider or identity data
- Generic OIDC at instance scope
- Exactly one enabled provider in the initial release while retaining a future-compatible schema
- Provider administration, connection testing, optional SSO, enforcement, and recovery
- Main Plane web sign-in flow
- Local logout and optional RP-initiated logout
- Compatibility testing for Entra ID, Okta, and Keycloak

Deferred according to the original plan:

- SAML 2.0 implementation
- SCIM user and group provisioning
- Group-to-workspace or project-role provisioning
- Per-workspace SSO policy
- Domain-routed selection among multiple active IdPs
- Self-service account linking and unlinking
- IdP-initiated and back-channel logout
- SAML metadata and certificate rollover automation

`apps/space` SSO remains deferred unless a private-Space acceptance test proves it is required for the instance-wide enforcement boundary.

## Success criteria

Delivery is accepted when all Must requirements are implemented, all security-negative tests pass, Entra ID/Okta/Keycloak compatibility is evidenced, recovery is exercised, the full Disabled/Optional/Enforced matrix passes, 22lab normal authentication is proven independent of OIDC, disable and rollback preserve data, the validation commands in FR-016/NFR-008 are green, and a new evidence-based delivery decision closes the reopened gate.
