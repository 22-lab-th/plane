# STORY-006: Offer OIDC SSO on the Plane sign-in screen

- **Epic:** Product integration · **Points:** 5 · **Status:** done
- **Depends on:** STORY-004, STORY-005
- **Requirements:** FR-008, FR-014, FR-016, NFR-004, NFR-007, NFR-008

## Value

As a member, I want to see and start my organization's SSO from Plane's sign-in screen so that the secure login path is clear.

## Acceptance criteria

1. **Given** an enabled provider **when** public instance configuration loads **then** it returns only provider slug/name and safe enabled metadata.
2. **Given** optional SSO **when** sign-in renders **then** the enabled SSO action appears alongside existing enabled methods and starts the correct API route with safe next-path handling.
3. **Given** no enabled provider **when** sign-in renders **then** existing authentication UI remains unchanged.
4. **Given** keyboard or assistive-technology use **when** navigating the new control and errors **then** focus, label, status, and contrast behavior follows existing accessible components and the sign-in mockup.

## Technical notes

- Extend shared instance types and the existing authentication option hook.
- Use reusable `@plane/ui` components and add a Storybook story when a new shared component is necessary.

## Test plan

- Unit/component: public payload mapping, visibility and initiation URL.
- UI: compare behavior with `docs/auto/ux/mockups/sign-in.html`.

## Completion evidence

- Commits: working-tree delivery; safe instance metadata and Web SSO option
- Tests and coverage: frontend lint, formatting, types and production build passed
- Review: approved by `bmad-orchestrator` at D1
