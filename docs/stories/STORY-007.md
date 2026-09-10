# STORY-007: Configure and test OIDC from God Mode

- **Epic:** Product integration · **Points:** 8 · **Status:** done
- **Depends on:** STORY-002, STORY-003
- **Requirements:** FR-001, FR-002, FR-013, NFR-004, NFR-007, NFR-008

## Value

As an instance administrator, I want to configure, rotate, and test OIDC in God Mode so that SSO can be introduced without editing deployment files.

## Acceptance criteria

1. **Given** God Mode authentication settings **when** opened **then** the administrator can create or edit provider name, issuer, client ID, scopes, claim/policy fields, and an optional replacement secret.
2. **Given** a stored secret **when** the form reloads **then** its value is never returned or rendered and the UI shows only that a secret is configured.
3. **Given** valid form data **when** Test connection runs **then** categorized progress/result feedback appears and the provider stays disabled until explicitly enabled.
4. **Given** an untested or invalid provider **when** enable/enforce is attempted **then** the UI and backend reject the transition.
5. **Given** keyboard navigation and validation errors **when** the form is used **then** focus and accessible labels follow the provider mockup and existing design system.

## Technical notes

- Add shared types/services before Admin screens.
- Keep one active-provider UX while retaining a multi-provider API/schema.

## Test plan

- Component: validation, masking, save/rotation and test states.
- Types/lint/build for Admin and shared packages.

## Completion evidence

- Commits: working-tree delivery; God Mode provider configuration and service contract
- Tests and coverage: frontend lint, formatting, types and Admin production build passed
- Review: approved by `bmad-orchestrator` at D1
