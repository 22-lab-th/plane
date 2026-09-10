# SSO Architecture Compliance Guardrails

The implementation follows [the adopted architecture](../auto/architecture.md) and
[the detailed SSO plan](../sso-auth-implementation-plan.md).

- Persist provider configuration and stable `(issuer, subject)` identity links in the database.
- Encrypt client secrets at rest and expose only a boolean configured indicator through APIs.
- Never persist OIDC access, refresh, or ID tokens.
- Validate OIDC issuer, signature, audience, expiry, nonce, and authorization response state before login.
- Link by provider subject first. Email linking requires a verified claim and an explicit administrator policy.
- Apply allowlists and JIT policy before creating or linking an account.
- Keep one documented emergency administrator path when SSO enforcement is active.
- Fail closed when discovery, token validation, policy evaluation, or identity linking is ambiguous.
- Record security-relevant configuration and authentication outcomes without secrets or tokens.
- End a Plane SSO session locally on logout. Do not persist an ID token solely to support optional IdP logout.
