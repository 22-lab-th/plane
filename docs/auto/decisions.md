# SSO Adoption Decisions

## D-001 — UX references reconstructed as static HTML

- **Status:** Proposed at Gate A1
- **Date:** 2026-09-10
- **Context:** The original SSO plan defines UI behavior but contains no visual reference. Phase 2 needs an agreed interaction baseline.
- **Decision:** Use three self-contained HTML mockups for sign-in, provider configuration, and enforcement/recovery. They define content hierarchy, controls, states, and accessibility intent while implementation continues to use existing Plane UI components.
- **Consequences:** Visual styling may be adjusted to match current components, but the represented fields, safeguards, and user flow remain acceptance inputs.

## D-002 — Delegate workflow gate approval to BMAD Orchestrator

- **Status:** Accepted by user
- **Date:** 2026-09-10
- **Context:** The user requested uninterrupted development and delegated approval management through completion to `bmad-orchestrator`.
- **Decision:** `bmad-orchestrator` may pass planning, architecture, story, and delivery gates only from objective repository evidence and recorded quality thresholds. It routes specialist BMAD workflows, rejects or reopens incomplete work, and continues without routine human approval.
- **Non-delegated actions:** Production deployment, credential/payment handling, spending money, deleting user data, external communications, and material product-contract conflicts still require the user.
- **Consequences:** Gate A1/P2 can pass from the user's explicit delegation in this decision. D1 becomes an orchestrator evidence gate rather than a user response gate. Security requirements and tests cannot be waived by automated approval.

## D-003 — Local-first RP-initiated logout

- **Status:** Accepted by `bmad-orchestrator`
- **Date:** 2026-09-10
- **Context:** Plane must always end its own session while supporting providers that advertise RP-initiated logout. Retaining an ID token only for logout would increase credential exposure.
- **Decision:** Destroy the Plane session before any IdP interaction. If validated discovery metadata includes `end_session_endpoint`, redirect with `client_id` and the Plane return URI. Fall back to the local return URI on discovery or provider failure and do not persist the ID token.
- **Consequences:** Local logout is reliable and compatible providers receive an RP logout request. Providers that require `id_token_hint` may keep their IdP session, while the Plane session remains terminated.

## D-004 — Make OIDC optional, disabled by default, and reversible

- **Status:** Accepted by user
- **Date:** 2026-09-11
- **Context:** 22lab currently relies on normal Plane authentication, while OIDC is being prepared as an optional customer capability. The production-readiness review also found unresolved lifecycle, configuration-test, authorization, and recovery risks.
- **Decision:** Adopt the approved Sprint Change Proposal at `docs/sprint-change-proposal-2026-09-11.md`. Add a deployment capability gate that defaults Off, explicit Disabled/Optional/Enforced modes, reversible non-destructive disable, one active provider, interactive readiness testing, and tested break-glass recovery.
- **Consequences:** The previous D1 delivery decision is reopened. STORY-011 through STORY-016 must be completed and a new evidence-based D1 decision recorded before the feature is called production-ready. No production deployment is authorized by this decision.
