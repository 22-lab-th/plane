# STORY-005: Link and provision SSO identities safely

- **Epic:** Identity policy · **Points:** 8 · **Status:** done
- **Depends on:** STORY-004
- **Requirements:** FR-005, FR-006, FR-016, NFR-002, NFR-004, NFR-005

## Value

As an approved organization member, I want my verified IdP identity linked or provisioned safely so that first-time SSO login works without account takeover risk.

## Acceptance criteria

1. **Given** an existing provider-subject link **when** verified claims contain a changed email **then** Plane returns the linked user without switching accounts.
2. **Given** no identity but a matching Plane email **when** verified-email auto-link is enabled **then** Plane atomically links that user.
3. **Given** no user or identity **when** JIT and domain/group policies permit the claims **then** Plane atomically creates the user, profile, and identity and runs invitation processing.
4. **Given** unverified/missing email, disabled JIT, disallowed domain/group, deactivated user, bot, or conflicting identity **when** resolved **then** Plane fails closed with no partial records.
5. **Given** concurrent first callbacks **when** they target the same subject or email **then** exactly one valid user/identity outcome is committed.

## Technical notes

- Put resolution and policy in a transaction-safe service independent of protocol parsing.
- Store only allow-listed safe claim metadata.
- Preserve signup/invitation semantics from the existing adapter.

## Test plan

- Unit: each policy branch and safe metadata filtering.
- Database/contract: concurrency/uniqueness, atomic rollback and first login.

## Completion evidence

- Commits: working-tree delivery; subject-first resolver and atomic JIT/linking policy
- Tests and coverage: identity resolver branch coverage 93%; policy tests passed
- Review: approved by `bmad-orchestrator` at D1
