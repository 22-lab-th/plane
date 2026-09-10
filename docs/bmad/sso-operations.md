# Plane OIDC SSO Operations Runbook

This runbook covers initial configuration, staged rollout, recovery, secret rotation, rollback, and audit checks for the instance-wide OIDC integration.

## Deployment prerequisites

1. Set `SSO_BREAK_GLASS_ADMIN_EMAILS` to a comma-separated allowlist containing at least one active, verified instance administrator. Keep the account password in the deployment's approved secret manager.
2. Optionally set `SSO_BREAK_GLASS_SESSION_AGE`; the default recovery session lifetime is 900 seconds.
3. If the IdP is intentionally hosted on a private network, set `OIDC_ALLOWED_IPS` to the smallest trusted IP/CIDR allowlist. For a private certificate authority, set `OIDC_CA_BUNDLE` to its PEM CA bundle path. Plane still verifies the certificate and hostname; never disable TLS verification. Leave both empty for public IdPs.
4. Deploy the API and apply migrations with the repository's normal migration command. Migrations `license.0007_ssoprovider_ssoidentity` and `license.0008_ssoauditevent` are additive and leave existing authentication behavior enabled.
5. Deploy Admin and Web from the same revision as the API.
6. Register the exact callback URI at the IdP: `<APP_BASE_URL>/auth/sso/callback/`.

Never place an OIDC client secret in source control, screenshots, tickets, or logs.

## Local end-to-end verification

Install dependencies and the Playwright Chromium browser once, then run the isolated SSO scenario:

```bash
pnpm install
pnpm exec playwright install chromium
pnpm test:e2e:sso
```

The harness starts temporary PostgreSQL and Valkey containers with Podman, applies migrations, seeds a recovery administrator and OIDC provider, and starts a local HTTPS mock IdP plus the API, Web, and Admin applications. It verifies authorization code + PKCE login, local-first RP logout, God Mode enforcement, legacy-login blocking, and continued SSO access. Processes and containers are removed automatically when the command exits. Local artifacts are written under `.e2e-sso/`, `test-results/`, and `playwright-report/`.

## Configure and test

1. Sign in to God Mode and open **Authentication → SSO / OpenID Connect**.
2. Enter a display name, stable slug, HTTPS issuer, client ID, client secret, scopes, and any claim mapping or domain/group policy.
3. Save with **Enable provider** and **Enforce SSO** turned off.
4. Select **Test connection**. Plane validates exact issuer matching, required endpoints, authorization-code support, endpoint destinations, and a non-empty JWKS document.
5. Correct categorized errors before continuing. Changing issuer, client ID, scopes, protocol, or secret clears the prior test result.

The initial release supports one provider in the Admin UI. The schema keeps provider and identity records scoped so a later multi-provider UI does not require an identity-model replacement.

## Staged rollout

1. Turn on **Enable provider** and save. The main Plane sign-in page now offers the provider while password, magic-link, and social OAuth remain available.
2. Test with a pre-linked account. If enabled by policy, also test verified-email linking or JIT provisioning with allowed and denied domain/group cases.
3. Confirm invitations and onboarding complete as expected and review recent `sso_audit_events` records.
4. Confirm the break-glass account can sign in through the God Mode administrator form.
5. Turn on **Enforce SSO** only after the above checks. The API rejects direct legacy-auth requests while enforcement is active.

## Recovery and rollback

If the IdP is unavailable, use the normal God Mode sign-in page with an email in `SSO_BREAK_GLASS_ADMIN_EMAILS`. This path is rate-limited, audited, limited to active instance administrators, and receives a short session.

To roll back enforcement:

1. Sign in with the break-glass administrator.
2. Open **Authentication → SSO / OpenID Connect**.
3. Turn off **Enforce SSO** and save. Legacy authentication routes become available immediately.
4. If needed, turn off **Enable provider** after enforcement is off. Existing provider and identity rows remain in place for investigation or later re-enable.
5. Roll back application binaries while leaving migrations 0007 and 0008 applied. The additive tables are harmless to an older application revision and retaining them avoids identity/audit data loss.

Do not delete the provider as a rollback step. Plane blocks deletion while the provider is enabled, enforced, or has linked identities.

## Rotate a client secret

1. Create or stage the replacement secret at the IdP.
2. Enter the new value in God Mode and save. Plane replaces the encrypted value and never returns either secret through the API.
3. Run **Test connection** again, then enable the provider if the configuration was disabled during rotation.
4. Revoke the old secret at the IdP after the test succeeds.

## Logout behavior

Plane destroys its local session first. When discovery advertises a validated `end_session_endpoint`, Plane then sends the browser there with `client_id` and `post_logout_redirect_uri`. If discovery or the IdP is unavailable, local logout still succeeds and returns the browser to Plane. Plane does not retain an ID token solely for logout.

## Audit and incident checks

SSO events are stored in `sso_audit_events`. Review provider creation/update/deletion attempts, configuration tests, enforcement changes, identity links, login outcomes, logout, and break-glass attempts. Records contain bounded actor, provider, result, IP, user agent, and allow-listed metadata; they exclude codes, tokens, raw assertions, and secrets.

During an incident:

1. Disable enforcement through break-glass access if member access is blocked.
2. Inspect recent failed event categories and the provider's last successful configuration test.
3. Verify the issuer and JWKS at the IdP without copying secrets into logs.
4. Re-test, enable optional mode, validate a member login, and only then restore enforcement.

## Current limitation

Compatibility is covered with representative Entra ID, Okta, and Keycloak discovery/JWKS fixtures, plus a full local browser flow against the HTTPS mock IdP. A live tenant login must still be exercised in the deployment environment because no real IdP credentials are stored in this repository. Authlib 1.8.0 emits a deprecation notice for its `authlib.jose` compatibility API; migrate to the supported `joserfc` API before Authlib 2.0.
