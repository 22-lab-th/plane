# Delivery Report — Plane OIDC SSO hardening

- **Date:** 2026-09-11
- **Gate:** D1 re-evaluation
- **Decision:** HOLD — implementation complete; deployment acceptance pending
- **Approval owner:** `bmad-orchestrator` under D-002

## Delivered behavior

The same build now supports three explicit effective modes: Disabled, Optional,
and Enforced. `ENABLE_OIDC_SSO` defaults Off, so 22lab's existing password,
magic-code, and configured social login paths do not depend on an IdP. Customer
deployments can enable OIDC without changing the default deployment profile.

Provider configuration and mode changes use a transactional lifecycle service,
with database constraints and an instance lock enforcing one active provider.
Interactive OIDC readiness and break-glass recovery readiness are separate,
time-stamped prerequisites. Configuration changes invalidate readiness, while
unenforcement remains available even if readiness has become stale. Disable
preserves provider configuration and linked identities and is rejected if it
would remove the final usable authentication method.

God Mode exposes deployment-gate, configuration, interactive-test, and recovery
states; gate-Off is read-only. Disable and Enforce require explicit confirmation,
and the Disable confirmation identifies the normal login methods that remain.
Lifecycle, test, recovery, denied-transition, and authentication events carry a
correlation ID without storing credentials, tokens, or raw claims.

## Current validation evidence

- Focused API OIDC, lifecycle, identity, readiness, discovery, and security suite:
  83 passed in the isolated Podman stack.
- Admin readiness and normal-auth helper unit tests: 4 passed.
- Admin format, lint, and type checks: passed (existing warning budget only).
- Admin production client and SSR build: passed.
- Django migration drift check: no changes detected.
- Python formatting and lint for all changed API files: passed.
- Prior baseline evidence remains green: full API unit/contract suite, HTTPS mock
  IdP Playwright flow, private-CA handling, Web/Admin builds, and representative
  Entra ID, Okta, and Keycloak metadata fixtures.

The current monorepo-wide `pnpm check` stops on formatting of the generated,
unchanged `packages/i18n/src/types/keys.generated.ts`; the OIDC/Admin targeted
checks and production build pass, and this run produced no tracked diff there.

## Remaining deployment acceptance

D1 stays on hold until the two environment-specific checks that cannot be proven
from repository fixtures are signed off:

1. Run the 22lab normal-login smoke checklist with `ENABLE_OIDC_SSO=0` in the
   target deployment.
2. Run the customer staging authorization-code flow against its real OIDC tenant,
   then test outage, secret rotation, reverse-proxy callback headers, disable,
   rollback, and break-glass recovery before enabling Enforced mode.

No live credentials, production configuration, or deployment action was used in
this implementation run. Follow `docs/bmad/sso-operations.md`; roll out Disabled
→ tested → Optional → Enforced, and return to Optional/Disabled for rollback.

## Known follow-up

Authlib 1.8 warns that `authlib.jose` will move to `joserfc`; migrate before
Authlib 2.0. SAML, SCIM, per-workspace SSO, IdP-initiated logout, and back-channel
logout remain outside this delivery.
