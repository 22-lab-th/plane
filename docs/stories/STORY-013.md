# STORY-013: Prove client configuration with an interactive OIDC test

- **Epic:** Optional SSO lifecycle and production hardening
- **Points:** 8
- **Status:** done
- **Depends on:** STORY-012
- **Requirements:** FR-002, FR-022, NFR-001, NFR-003, NFR-004

## Value

As an instance administrator, I want to test the real authorization-code flow before enabling SSO so that invalid client credentials cannot be marked production-ready.

## Acceptance criteria

1. Metadata/JWKS validation remains a separate, clearly labelled preflight operation.
2. An Admin-initiated interactive test completes authorization, code exchange, and ID-token validation with the current configuration without creating/linking a member or retaining tokens.
3. Readiness stores a timestamp and non-secret configuration fingerprint and is invalidated when any material configuration changes.
4. Enable and Enforce reject stale or missing interactive readiness.
5. Wrong secrets, unsupported client authentication, callback mismatch, denial, timeout, invalid tokens, and signing-key rotation fail safely with categorized diagnostics.

## Test plan

- Unit/contract tests for fingerprinting, invalidation, and all failure categories.
- Playwright success/cancel/failure flows against the HTTPS mock IdP.
- Customer staging smoke test against the real IdP.
