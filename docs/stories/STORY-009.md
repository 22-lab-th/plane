# STORY-009: Complete logout, audit, and operational visibility

- **Epic:** Operations · **Points:** 5 · **Status:** done
- **Depends on:** STORY-008
- **Requirements:** FR-011, FR-012, NFR-004, NFR-008

## Value

As an instance operator, I want reliable logout and safe SSO diagnostics so that incidents can be investigated without exposing credentials.

## Acceptance criteria

1. **Given** any SSO session **when** local sign-out runs **then** Plane destroys its session even if IdP logout is absent or fails.
2. **Given** an IdP with an `end_session_endpoint` **when** sign-out runs **then** Plane can initiate RP logout using validated safe metadata.
3. **Given** provider administration, tests, enforcement, recovery, identity links, and SSO login outcomes **when** they occur **then** categorized audit records contain actor/provider/result/correlation data without secrets or raw tokens.
4. **Given** expected OIDC failures **when** observed **then** operators can distinguish discovery, exchange, validation, identity-policy, and provider-availability categories.

## Technical notes

- Reuse the repository audit/event pattern where one exists; otherwise use structured security logging with explicit allow-listed fields.
- Treat RP logout as best effort after local session invalidation.

## Test plan

- Unit/contract: local logout independence, safe RP redirect and audit redaction.

## Completion evidence

- Commits: working-tree delivery; audit events and local-first RP logout
- Tests and coverage: local logout, IdP-unavailable fallback, RP redirect and audit tests passed
- Review: approved by `bmad-orchestrator` at D1
