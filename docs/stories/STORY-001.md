# STORY-001: Stabilize the existing OAuth baseline

- **Epic:** Authentication foundation · **Points:** 3 · **Status:** done
- **Depends on:** none
- **Requirements:** FR-016, NFR-002, NFR-008

## Value

As an instance operator, I want existing OAuth behavior covered and expiry data correct so that SSO work does not build on a broken baseline.

## Acceptance criteria

1. **Given** an OAuth token response with `expires_in` **when** Plane stores its expiry **then** the timestamp equals the current time plus that duration.
2. **Given** existing Google, GitHub, GitLab, and Gitea authentication **when** SSO foundation changes land **then** their enabled and callback behavior has no regression.
3. **Given** relevant OAuth adapter tests **when** run in the Docker test stack **then** they pass and cover expiry and verified-email behavior.

## Technical notes

- Correct relative-expiry conversion across providers that use `expires_in`.
- Add focused tests without changing the public auth contract.
- Do not broaden this story into refactoring all social OAuth providers.

## Test plan

- Unit: relative expiry, absent expiry, verified/unverified email mapping.
- Contract: existing initiation/callback guard behavior where fixtures permit.

## Completion evidence

- Commits: working-tree delivery; Google/GitHub expiry fixes
- Tests and coverage: OAuth expiry regression tests passed
- Review: approved by `bmad-orchestrator` at D1
