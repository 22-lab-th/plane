# Runbook: personal-data breach notification

Scope: a personal-data breach involving project files — the `file_objects` /
`file_versions` / `file_links` rows and their objects in the bucket — and the
personal-data columns of the audit trail that describe access to them
(`file_access_logs`). Requirement R-NFR-12; acceptance AC-33. Legal basis:
PDPA s. 37(4), and the PDPC Notification on Rules and Methods for Notification of
the Personal Data Breach B.E. 2565 (2022) — Royal Gazette 15 December 2022, in
force from publication (Clause 2).

Owner: the **22lab owner** is the accountable role (the same role recorded in the
decision log and named in the erasure runbook). Executing roles: the **incident
lead** (the 22lab owner, or the person the owner names in writing before an
incident), the **platform engineer** who reads the file surfaces and preserves
evidence, and the owner for the processor and the Office filing. The individual
names, the on-call rota and the monitoring window are **not recorded anywhere in
this repository** — they are owner actions (§7).

This runbook makes **no legal claim**. No external counsel was engaged for this
system (owner decision 2026-09-20, R-LEG-4), so the reportability analysis, the
notification text and the processor assessment are owner decisions with the
consequences recorded in R-LEG-4. What this runbook fixes is the clock, the
trigger, the path and the evidence — the parts that have to be mechanical while
an incident is running.

## 1. The clock, and when it starts

- **72 hours** to notify the **PDPC Office**, from the moment 22lab becomes
  aware of the breach (PDPA s. 37(4): notify "without delay and, where feasible,
  within 72 hours after having become aware of it"; the PDPC breach
  notification, Clause 5(3): within 72 hours of becoming aware "to the extent
  possible"). The duty does not apply where the breach is unlikely to result in a
  risk to the rights and freedoms of natural persons.
- **Awareness** for this system is fixed as: the moment a person on the team
  reads a report, alert or message that describes the event — not the moment an
  unattended alert fired, and not the moment the investigation concluded. Write
  that timestamp down first; every later deadline is computed from it. This is
  the runbook's **working reading of an unsettled statutory term**, not a
  verified construction of "becoming aware" (no counsel has reviewed it, R-LEG-4)
  — it exists so an incident has one timestamp everyone uses, and the owner
  should confirm it. Consequence: if nobody reads the file-related signals, the
  clock is not governed by this runbook at all. A monitoring window (who reads
  what, how often) is an owner action (§7).
- **The clock does not pause for the processor leg** (§5). Start the Office
  filing on 22lab's own awareness and 22lab's own evidence.
- If the deadline cannot be met, the PDPC notification's Clause 7 provides a
  request-for-exemption path (the retrieved source records a 15-day window for
  that request) — an **owner** action, and one that has had no legal review.

## 2. Triage: is it a personal-data breach, and which surface answers

A breach is a breach of security leading to accidental or unlawful destruction,
loss, alteration or unauthorised disclosure of, or access to, personal data —
the working definition the PDPA breach texts use; RSCH-002 does not quote it
verbatim, so the owner should confirm it against the Thai text while doing the
review this runbook does not do. Ask in this order: (a) does the affected data
relate to a natural person — a project file can be a contract, an ID document,
payroll or HR material, and the audit columns `ip_address`, `user_agent`,
`actor_display` and `file_name_snapshot` carry personal data (whether the audit
trail and the provider's access logs must be _treated_ as personal data held by
22lab is an open legal question — RSCH-002 §7 item 7 — but the design masks them
on schedule regardless, R-NFR-13, after `AUDIT_PII_RETENTION_DAYS`, default 90);
(b) was it unauthorised; (c) does it risk rights and freedoms; (d) is the risk
high (§4).

| Event class                                                             | Where the answer is                                                                                                                                                                                  | What to capture                                                                                                                                       |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| A signed URL leaked or was delivered to the wrong party                 | `file_access_logs` rows for the file (`action` = `downloaded` / `previewed`, actor id, timestamps) + the endpoint that issued it                                                                     | File id, version number, key, issuing timestamp, the URL's expiry (`SIGNED_URL_EXPIRATION`, default 3600 s, hard cap 604800 s)                        |
| Unauthorised access through the API or a permission gap                 | `file_access_logs` and the project/workspace membership rows; `plane.files` records for presign/finalize/delete                                                                                      | Actor ids, workspaces, projects, files, actions, timestamps. A `permission_denied` row is a refused attempt worth reading next to the successful ones |
| Bucket exposure (public access, `r2.dev`, anonymous listing)            | The bucket configuration itself (R-PERM-2 requires no public access and no `r2.dev` in production) and R2's own logs (§6)                                                                            | Bucket name, the exposure window, the objects reachable in it                                                                                         |
| Credential or signing-key compromise (R2 API token, `SECRET_KEY`)       | The deployment configuration. The settings module warns in its own comments that a known or placeholder `SECRET_KEY` allows password-reset token forging, which is why it refuses the flagged values | Which credential, issued when, where it could read from                                                                                               |
| Loss or destruction (accidental purge, storage loss, a botched restore) | The DR runbook's procedure and the erasure runbook's completion records                                                                                                                              | What was destroyed, whether a backup covers it, the recovery point                                                                                    |
| Processor-side incident                                                 | Cloudflare's notice (§5)                                                                                                                                                                             | Provider reference, its timeframe, the affected keys                                                                                                  |

The audit trail is the system's evidence surface, and it is designed for this:
`file_access_logs.file_id` is a plain id, so the rows survive the file they
describe (R-NFR-9, AC-27), and a download or preview row is written without the
presigned URL (AC-34). The `plane.files` records (`file.presign`,
`file.finalize`, `file.delete` with workspace, project, file and version
identifiers plus an outcome — see `docs/observability/project-file-lifecycle.md`)
answer "what the system did"; `file_access_logs` answers "who read what".

The exact `action` values are `FileAccessLog.Action` in
`apps/api/plane/db/models/file.py`. The ones that matter in an incident:
`downloaded`, `previewed`, `permission_denied`, `upload_initiated`,
`upload_completed`, `upload_failed`, `version_created`, `version_activated`,
`renamed`, `moved`, `copied`, `linked`, `unlinked`, `trashed`, `restored`,
`purged`, `quota_rejected`, the four `folder_*` values, and `pii_masked` — the
last one is written by the masking run itself and tells you the personal-data
columns in that window have already been cleared.

## 3. Notify the Office

- **Who files:** the 22lab owner. **Where:** the PDPC Office's notification
  channel. **By when:** 72 hours from awareness (§1).
- **This presumes 22lab owes a controller's filing duty.** Whether 22lab is
  controller or processor for customer-uploaded project files is an explicitly
  open question (`RSCH-002` §7 item 1: whether those files contain personal data
  at all, and which role 22lab holds, is unsettled and unreviewed). The duties in
  this runbook are the controller's; a processor's duty is to notify the
  controller, and if that analysis lands differently this section changes with
  it. The markers are the same kind as the definition in §2 and the Clause 6
  list below: what is verified here is the mechanism, not the role. Owner action,
  listed in §7.
- **What the notification carries.** The PDPC notification's Clause 6 makes the
  content mandatory; the _exact enumeration in the Thai text has not been
  verified in this tree_ (the retrieved source records the Clause's existence,
  not its item list — RSCH-002 S37). The list below is therefore what an operator
  must collect regardless, and the legal check of it against Clause 6 is an owner
  item:

| Item                                                                   | Source                                                                                        |
| ---------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Description of the breach and the personal data affected               | §2 triage                                                                                     |
| Cause and the period during which the breach occurred                  | The timestamps in §2's evidence                                                               |
| The data subjects affected, or the categories, and the number          | `file_access_logs` actor ids, `FileObject.created_by` / `uploaded_by`, `WorkspaceMember` rows |
| The measures taken or proposed to deal with the breach and reduce harm | The containment log (§6)                                                                      |
| The contact person for follow-up                                       | The incident lead (§7 owner action)                                                           |

- **Reportability is an owner decision, not this runbook's.** Record the decision
  and the reasoning either way; a decision _not_ to notify is also a record.

## 4. Data-subject notification trigger

If the breach "is likely to result in a high risk to the rights and freedoms of
the natural person", **notify the affected data subjects without delay**, in
parallel with the Office — that is the trigger sentence (PDPA s. 37(4) second
limb; PDPC breach notification Clause 5(4) adds the remediation step). The
trigger is the likelihood of high risk, not a choice of tone: once it is met, the
runbook's instruction is to notify, alongside whatever the Office filing says.
The decision on whether the trigger is met is the incident lead's, recorded with
its reasoning, and the content list above is the minimum the notice should carry
in plain language. Containment comes first (§2's surfaces, checklist item 9 in
§6): if the exposure is still live, ending it is part of reducing the harm the
notice describes. Note the design's one limitation here — a signed URL cannot be
revoked individually; its exposure is bounded by `SIGNED_URL_EXPIRATION`
(default 3600 s). Removing the access key that signed it is the lever an incident
lead may choose, but this repository does not document that key removal
invalidates an outstanding URL, so verify it before relying on it.

## 5. The processor path: Cloudflare

Cloudflare holds the objects (R2) as processor. The Cloudflare DPA v6.4
(effective 2026-04-03) promises breach notice to the customer **"without undue
delay"** — no fixed hours — and its "Applicable Data Protection Laws" definition
covers European and United States laws only, so the Thai PDPA is not named in the
agreement. R-LEG-4 records the owner's decision on this (internal company use,
no external counsel engaged, DPA assessment not performed, residual risk
accepted with revisit triggers).

Operational consequence, and why this section exists as a separate path:

1. **Do not wait for Cloudflare to start 22lab's clock.** 22lab's 72 hours run
   from 22lab's own awareness; the processor's notice has no clock in the
   agreement 22lab holds. Both tracks run in parallel.
2. **Raise the request through the Cloudflare account channel, citing the DPA's
   breach-notice obligation, and ask for, in writing:** the incident reference,
   the timeframe, the affected bucket and object keys, whether the data was
   accessed or exfiltrated, and what actions Cloudflare took. The account id and
   the contact route are deployment configuration and are **not recorded here**
   (§7).
3. **File the written response** with the incident record (§6). The erasure
   runbook relies on the same clause family for delete-or-return, so a response
   that names keys is useful to both.
4. R2 Data Access Logs are the provider-side record of per-object operations
   (key, request URI, IP, user agent, access-key id — RSCH-002 S15). The same
   source describes their delivery as "asynchronous and best effort … may be
   delayed or omitted", and says they are unavailable for jurisdictional buckets
   — 22lab's bucket uses default placement (owner decision 2026-09-20), so the
   logs are available **if enabled**. Whether they are enabled is an owner action
   (§7); **do not assume completeness** if they are used as evidence.

## 6. Evidence checklist (filled in during the incident)

Fill this in as the incident runs; the row that is empty at the end is a gap in
the notice, not a detail to reconstruct later.

| #   | Item                                                                                | Where it comes from                                                | Filled by         |
| --- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ----------------- |
| 1   | Awareness timestamp (who read what, when)                                           | §1                                                                 | incident lead     |
| 2   | What happened, in one paragraph                                                     | §2                                                                 | incident lead     |
| 3   | Event class from §2's table                                                         | §2                                                                 | incident lead     |
| 4   | Affected workspace, project, file ids, version numbers and object keys              | `file_objects`, `file_versions`                                    | platform engineer |
| 5   | Affected data subjects or categories, and the count                                 | `file_access_logs` actor ids, uploader ids, workspace members      | platform engineer |
| 6   | Audit rows: actor, action, timestamp, ip_address, user_agent (never the signed URL) | `file_access_logs` for the affected files and window               | platform engineer |
| 7   | System records: presign / finalize / delete events and outcomes in the window       | `plane.files` JSON records (`file_record` payloads)                | platform engineer |
| 8   | Whether any signed URL is still live, and when it expires                           | `SIGNED_URL_EXPIRATION` and the issuing timestamp                  | platform engineer |
| 9   | Containment actions with timestamps and the resulting state                         | §2 surfaces + the deployment log                                   | whoever acts      |
| 10  | R2 Data Access Logs request and response (or the reason they cannot be used)        | Cloudflare console/API                                             | owner             |
| 11  | Cloudflare written notice (reference, timeframe, keys)                              | §5                                                                 | owner             |
| 12  | Office notification: filed at (UTC), reference, and the text sent                   | §3                                                                 | owner             |
| 13  | Data-subject notice: trigger decision, the text, and the time sent                  | §4                                                                 | owner             |
| 14  | Reportability decision and reasoning, including "not reportable"                    | §3                                                                 | owner             |
| 15  | Data-subject erasure requests that arrive as a follow-on                            | `docs/runbooks/project-file-erasure.md`                            | platform engineer |
| 16  | The contact person for follow-up (name, role, channel) that the notice names        | §3's content list; an owner appointment, not recorded in this tree | owner             |

Two properties of the design worth knowing while filling this in: the audit rows
survive the file they describe (so a purge does not erase the evidence), and once
`AUDIT_PII_RETENTION_DAYS` (default 90) passes, the masking task clears
`ip_address`, `user_agent`, `actor_display` and `file_name_snapshot` from those
rows. If an incident is still open near that boundary, export the rows before the
task runs; after masking they cannot be recovered.

## 7. What this runbook does not cover — owner actions

| Item                                                                                                                                          | Why it matters                                                                                                                                                                                         | Status                                                                |
| --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------- |
| Whether 22lab is controller or processor for project-file personal data, and whether those files hold personal data at all (§3's presumption) | The filing duty this runbook organises is the controller's; RSCH-002 §7 item 1 records the role analysis as an open question needing a qualified Thai review                                           | Open legal question, owner decision with no counsel engaged           |
| The awareness definition §1 fixes ("a person reads the report")                                                                               | It is a working reading of an unsettled statutory term with no counsel review (R-LEG-4); the owner should confirm it, because every deadline in §1 is computed from it                                 | Not verified — owner action                                           |
| The individual incident lead and on-call rota, and the monitoring window that reads the file-related signals                                  | §1's awareness definition assumes a person reads them                                                                                                                                                  | Not recorded anywhere in this tree — owner action                     |
| Reportability analysis and the notification wording                                                                                           | §3, §4                                                                                                                                                                                                 | Owner action; no counsel engaged (R-LEG-4, 2026-09-20)                |
| The Cloudflare account id, contact route and whether R2 Data Access Logs are enabled                                                          | §5, checklist 10                                                                                                                                                                                       | Not recorded in this tree — owner action                              |
| A tabletop walkthrough of this runbook                                                                                                        | R-NFR-12 asks for the runbook to be reviewed by the owner **and** a tabletop walkthrough recorded as evidence. Neither has happened: the runbook is written and registered, no walkthrough is recorded | Owner action                                                          |
| Breaches outside project files (SSO/OIDC, billing, the legacy `FileAsset` surface)                                                            | This runbook scopes to project-file personal data                                                                                                                                                      | Separate surfaces — the OIDC runbook is `docs/bmad/sso-operations.md` |
| Whether the R2 Data Access Logs are themselves personal data that needs notice, retention and erasure                                         | The logs carry IP and user agent (RSCH-002 §7 item 7)                                                                                                                                                  | Open legal question, owner decision with no counsel engaged           |
