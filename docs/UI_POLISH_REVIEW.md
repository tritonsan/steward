# UI and workflow polish review — 11 September 2026

This review covers Steward’s logo treatment, management decision desk, case progress,
approval controls and retained community records. Desktop and mobile verification
passed against an isolated local fixture in `dry_run`. The hosted update completed
successfully. Its login, management desk, case reads, served assets and service
health were verified; state-changing journey checks used the isolated fixture.

## Journey reviewed

1. **Enter Steward.** Check the supplied square logo on the light sign-in surface and
   dark brand panel. Preserve its artwork while removing the visible white-paper box.
2. **Find the next decision.** Open Today and distinguish management approvals and
   exceptions from participant availability or a question assigned to another person.
3. **Review a case and its evidence.** Read the next action, responsible party and
   expected follow-up; open a source without losing the case context or keyboard focus.
4. **Approve and follow progress.** Record the approval reason, submit once and keep
   the case open with success feedback and the refreshed next step. Distinguish a sent
   service order from a confirmed appointment.
5. **Review alternatives and exceptions.** Inspect quote price, scope, validity and
   evidence together. Identify rejected or replaced versions; reconcile uncertain
   appointment delivery against an explicitly selected proposal.
6. **Verify the outcome.** Record completion or reject an unsuccessful repair. Show
   verification or warranty follow-up instead of describing a past visit as upcoming.
7. **Read community decisions after completion.** Retain agreed decisions, action
   owners, dates and completion evidence while removing inapplicable mutation controls.

## Initial findings and changes

| Finding | Change |
|---|---|
| The square logo’s white background stood out on the navy theme | The original artwork remains in use. Surface-specific blending and a dark-surface treatment integrate it with the existing navy–green palette; navigation branding has more appropriate scale and spacing. |
| Every open human task was counted as a management decision | Presentation now separates management action from waiting or personally assigned responses, with task-specific headings and links. Authorization remains server-side. |
| A successful approval closed the case and hid the resulting state | The dialog stays open, shows confirmation and refreshes its next step. Source inspection retains return focus and case context. |
| Outcome verification was buried below the recommendation and historical quotes | Verification and community action controls now appear immediately after the next-step summary, ahead of historical procurement details. |
| Quote facts and controls appeared in separate, repeated sections | One quote section groups amount, currency, scope, validity, inclusions, exclusions, source access and applicable actions. |
| Delivered orders and rejected quote versions still offered unusable changes | Delivery state locks quote mutations. Persisted rejection and supersession records label historical versions and suppress their controls; a corrected reply remains eligible. Multiple active versions require an explicit review rather than an inferred replacement. |
| Appointment acceptance appeared before sufficient evidence or during pending delivery | Controls distinguish an actionable exception, missing terms/access/rules, an access response and acceptance awaiting delivery. Reconciliation requires choosing the proposal covered by the provider receipt. |
| A delivered, confirmed appointment acceptance could hide the control for a newer proposal at the same simulation timestamp | Already-confirmed delivered outbox records are excluded from the pending-acceptance check. Pending and ambiguous delivery still prevent a duplicate acceptance. |
| Meeting completion could be offered before linked maintenance was ready | Pending procurement requests and unverified linked work show their waiting state and prevent premature completion or a repeat request. |
| Completed meetings lost their decision/action presentation | Confirmed statements, assignments and completion evidence remain readable. Open task types and terminal state determine which actions are displayed. |
| Repeated case refreshes accumulated duplicate panels in the DOM | Continuity and meeting siblings now have distinct React key prefixes. They no longer share the case ID as their sibling key. |
| Progress summaries blurred delivery, appointments and completed memory | The API read model distinguishes a delivered order awaiting an appointment, appointment follow-up, verification/warranty review and terminal memory processing. |

Frontend controls are a usability layer. Policy, identity, evidence, version checks,
budget checks and command authorization still run on the server. Calendar expiry is
also server-owned; browser wall-clock time does not invalidate a virtual-time scenario.

## Verification evidence

| Check | Recorded result |
|---|---|
| Local desktop, 1280 × 720 | Passed: decision review, source inspection, approval feedback, ongoing progress and repair rejection; local fixture only |
| Frontend regression tests | 24 passed |
| Frontend production build and formatting | Passed |
| Backend read-model tests | 5 new tests passed |
| Existing targeted backend regressions | 36 passed |
| Local mobile, 390 × 844 | Passed: no horizontal overflow; approval, verification and resident meeting response |
| AWS image builds | Application and inference builds both succeeded for candidate `5607f9f9642efbca` |
| Deployment change review | CDK diff contains only application/inference images and frontend assets |
| Hosted release | Passed: `UPDATE_COMPLETE`; API and worker each running 1, pending 0, rollout completed; backend/inference release `5607f9f9642efbca` |
| Hosted web and authentication | Passed: `index-CfoeOiLV.js` and `index-BnTzkBvE.css` served; Cognito login and existing case reads succeeded; anonymous case access returned 401 |

The final local journey checks confirmed:

- Approving a service order keeps the dialog open and displays **Send the approved
  service order**, with **Steward** responsible for the next action.
- The verification form precedes historical quote controls. Rejecting the repair
  returns the case to warranty review.
- Completing a meeting action leads to community outcome verification. Closing the
  verified case retains its confirmed decisions and action evidence, with the next
  step **Verified outcome saved to community memory**.
- Repeated state updates retain exactly one `.continuity-panel` in the DOM.
- The resident view shows community cases, a personal question and the resident’s own
  meeting invitation. Availability submission returns success, and no management
  action buttons are present.

The frontend checks include delivery boundaries, explicit quote rejection/supersession,
pending procurement, retained completion evidence and management-versus-waiting task
classification. The backend checks cover the additive state projections and their
regressions. These targeted checks do not represent a new run of the complete project
acceptance suite.

Screenshots are retained as private local evidence in
`.scratch/ui-polish-20260911/`: `01-login-before.png`, `02-today-before.png`,
`05-login-after.png`, `06-today-after.png`, `07-source-after.png`,
`10-mobile-today-after.png`, `11-mobile-approved-after.png`,
`12-mobile-verification-after.png`, `13-meeting-closed-after.png` (mobile),
`14-desktop-today-after.png` and `15-mobile-resident-meeting-after.png`.
Final desktop and hosted captures are `16-desktop-desk-final.png`,
`17-live-login-final.png`, `18-live-desk-final.png` and
`19-live-operations-final.png`.
Screenshots `08` and `09` are intermediate observations and are excluded from final
evidence. These private files are not part of the public source package.

## Scope and remaining verification

This pass used synthetic local cases and did not send real provider messages, request
physical work or make payments. Existing controlled-channel verification is recorded
separately in [CONTROLLED_MAIL_TEST](CONTROLLED_MAIL_TEST.md).

Hosted verification retained the two existing cases and checked the additive case
projection. Manager and resident state-changing flows were tested locally with
synthetic fixtures; this pass did not create real orders or provider notifications.
An independent usability study and a comprehensive accessibility audit remain
outside this focused review. Aggregate deployment evidence is retained privately
in `.scratch/ui-polish-20260911/live-verification.json`.

The CDK asset uploader stalled while publishing the reviewed template. The same
template was uploaded through S3 path-style addressing, read back and checked for
byte equality, then applied through CloudFormation with all three existing stack
parameters preserved. The previously reviewed change scope stayed limited to the
application/inference images and frontend assets.
The final appointment-control correction required a subsequent static-only update.
Before applying it, the synthesized template was compared to the deployed template:
only the console asset key could differ. Existing service images and parameters
were preserved. Final verification completed on 2026-09-11 at 10:19 UTC.
