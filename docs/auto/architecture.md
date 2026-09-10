# Plane SSO Architecture Index

This document extracts implementation decisions from
[`docs/sso-auth-implementation-plan.md`](../sso-auth-implementation-plan.md).
The original plan remains authoritative.

## Selected design

- Keep Django server-side sessions as Plane's internal authenticated state.
- Add `SSOProvider` and `SSOIdentity` models rather than expanding the generic
  `InstanceConfiguration` key-value table.
- Identify a principal with `(sso_provider_id, subject)`.
- Implement OIDC as a protocol adapter with a shared identity/policy service;
  do not treat the existing social OAuth adapter as an ID Token validator.
- Use Authorization Code Flow, PKCE S256, state, nonce, OIDC discovery, and
  JWKS validation.
- Retain no IdP token after the callback for authentication-only SSO.
- Reuse `post_user_auth_workflow()`, `user_login()`, and the existing safe
  redirect utility after identity and policy validation succeeds.
- Enforce SSO in backend entry points while retaining a restricted break-glass
  route for instance administrators.
- Add SAML later behind the same provider, identity, policy, audit, and session
  boundaries.

## Component boundaries

| Component                            | Responsibility                                                            |
| ------------------------------------ | ------------------------------------------------------------------------- |
| `plane.authentication.provider.oidc` | Discovery, authorization request, token exchange, and protocol validation |
| SSO identity service                 | Resolve/link/provision a Plane user atomically from validated claims      |
| SSO policy service                   | Enabled/enforced/JIT/domain/group/recovery decisions                      |
| License/instance API                 | Admin CRUD, provider test operation, and public safe provider summary     |
| `apps/admin`                         | Configure, test, enable, enforce, and rotate provider credentials         |
| `apps/web`                           | Display enabled SSO and initiate login                                    |
| Django session layer                 | Rotate and persist the authenticated Plane session                        |

## Data decisions

`SSOProvider` stores structured protocol and policy configuration. Its client
secret uses the repository's existing encryption utilities but is write-only
at the API boundary. `SSOIdentity` stores the stable subject link and safe
metadata only. Database uniqueness and atomic transactions are the final
authority for concurrent identity creation.

## Cost sanity note

The selected design adds no external infrastructure: it uses the existing
Django service, PostgreSQL, Redis/session stack, and Admin/Web applications.
An established OIDC client library is cheaper and safer to maintain than a
custom JOSE implementation. A provider table costs slightly more development
time than key-value configuration but prevents a costly migration when a
second provider or SAML is introduced. SAML and SCIM stay deferred to avoid
adding XML-signature and provisioning operations before demand exists.

## Dependency and security constraint

The original plan recommends Authlib and requires a release unaffected by
GHSA-m344-f55w-2m6j. Dependency selection must be rechecked against current
official advisories immediately before it is pinned.
