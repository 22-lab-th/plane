# STORY-002: Manage structured SSO providers and protected secrets

- **Epic:** Authentication foundation · **Points:** 8 · **Status:** done
- **Depends on:** none
- **Requirements:** FR-001, FR-005, FR-013, NFR-004–NFR-006

## Value

As an instance administrator, I want a structured OIDC provider configuration API so that Plane can manage SSO without exposing secrets or overloading key-value configuration.

## Acceptance criteria

1. **Given** valid instance-admin input **when** a provider is created or updated **then** structured policy fields persist and its secret is encrypted.
2. **Given** any provider API response **when** a stored secret exists **then** the plaintext and ciphertext are absent and only a configured indicator is returned.
3. **Given** concurrent identity creation **when** the same provider subject is used **then** the database permits exactly one `SSOIdentity` link.
4. **Given** a non-admin or anonymous request **when** it accesses provider administration **then** access is denied.
5. **Given** an enabled or identity-linked provider **when** destructive removal is requested **then** safe lifecycle rules prevent orphaned or bypass-prone state.

## Technical notes

- Add `SSOProvider` and `SSOIdentity` models and migrations under the existing Django apps.
- Use existing encryption utilities behind a write-only serializer field.
- Add list/create/detail/update/delete endpoints under instance administration.
- Store no OIDC access, refresh, or ID token.

## Test plan

- Unit: field validation, encryption/masking, uniqueness and model constraints.
- Contract: CRUD permissions, masked responses, partial secret rotation.

## Completion evidence

- Commits: working-tree delivery; provider/identity models, migrations, serializer and API
- Tests and coverage: model, encryption, masking, authorization and deletion tests passed
- Review: approved by `bmad-orchestrator` at D1
