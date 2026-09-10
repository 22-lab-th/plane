# STORY-003: Discover and test an OIDC provider safely

- **Epic:** OIDC protocol · **Points:** 5 · **Status:** done
- **Depends on:** STORY-002
- **Requirements:** FR-002, FR-015, NFR-001, NFR-003, NFR-004

## Value

As an instance administrator, I want Plane to test OIDC discovery safely so that I know the IdP is compatible before users depend on it.

## Acceptance criteria

1. **Given** an HTTPS issuer **when** discovery succeeds **then** Plane requires an exact metadata issuer match and required code-flow/JWKS endpoints.
2. **Given** a private, loopback, link-local, non-HTTPS, redirecting-to-private, oversized, malformed, or timed-out response **when** tested **then** Plane fails closed with a safe error.
3. **Given** a compatible configuration **when** the admin runs the test **then** `configuration_tested_at` is updated without enabling the provider.
4. **Given** discovery/JWKS rotation **when** cached data is stale or a key ID is unknown **then** Plane refreshes within bounded request limits.

## Technical notes

- Pin a currently safe Authlib version after checking official advisories.
- Reuse the repository SSRF-safe HTTP primitives where compatible.
- Return categorized diagnostics without endpoints' response bodies or secrets.

## Test plan

- Unit: issuer normalization and metadata capability checks.
- Contract: admin test endpoint success and SSRF/error matrix with mocked HTTP.

## Completion evidence

- Commits: working-tree delivery; Authlib OIDC client and safe discovery/JWKS fetch
- Tests and coverage: discovery, SSRF, bounds, cache, client auth, rotation and JWT-negative tests passed
- Review: approved by `bmad-orchestrator` at D1
