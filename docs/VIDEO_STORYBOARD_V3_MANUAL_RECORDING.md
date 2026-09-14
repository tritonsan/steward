# Steward — manual recording plan, v3

14 September 2026. Replaces the automated product-footage approach in v2. The user will record the product interactions. The approved 30-second Telegram opening remains first. Existing automated product clips and technical presentation cards are not the intended final footage.

Target: 4:40, 1920 × 1080, 30 fps, English interface and narration. Record at normal speed; pacing, cuts, labels and narration are added in editing. All timings are editorial targets, not response-time claims.

## Narrative

A community message becomes a case, evidence informs the next step, people make the required decisions, and Steward keeps the follow-up visible until the outcome is verified. Show changes caused by actions. Integrate the resident/manager distinction into the meeting interaction instead of interrupting the story with a separate dashboard tour.

| Time | Sequence | Visible interaction and result |
|---|---|---|
| 00:00–00:30 | Approved opening | Existing fictional Telegram conversation, Steward response and logo. Existing user voice remains. |
| 00:30–00:55 | Workflow preview | Short excerpts from later recordings: source → case; quote → approval; invitation → confirmation; outcome → memory. No separate overview recording is needed. |
| 00:55–01:40 | Parking preparation | Open the parking case, inspect its original resident source, return to topic, agenda, competing 24/72-hour options and the earlier community source. Show the manager's next step. |
| 01:40–02:20 | Parking follow-through | Resident submits availability; after normal processing, Refresh reveals Meeting confirmed. Cut explicitly to After the meeting. Submit demo written minutes, review and select a decision AND its action, confirm, show owner and due date. Brief completion/verification result if pacing permits. |
| 02:20–03:05 | Maintenance decision | Open elevator report and relevant history. Inspect quote evidence and differences in scope, incomplete fields, recommendation, alternative and conditions that would change the decision. |
| 03:05–03:35 | Maintenance follow-through | Enter approval reason and approve; show awaiting vendor proposal. After a labelled simulated reply, show confirmed appointment with vendor evidence. Record completion claim, then separately verify it and show the memory result. |
| 03:35–04:10 | Technical implementation | Brief real code/terminal recording: coordinator/tools, actual previously recorded model/tool result, and narrow restart verification. Intercut with the case source opened in the product. No fabricated trace-viewer UI. |
| 04:10–04:25 | Channels | Actual Telegram case source/delivery evidence and a separate controlled SES test receipt or mailbox recording. Label the different test contexts. |
| 04:25–04:40 | Closing | The resulting task/outcome and community memory; end with logo and demo URL. |

## English narration draft

These words describe the intended successful takes. Match them against the captured results before voice recording. Simulated lifecycle footage must carry a clear demo label. Do not describe the deterministic local filming runtime as a live model run.

### 01 — Workflow preview, 25 seconds

Steward turns everyday community messages into work that can be followed through. It connects a report to a case, checks earlier experience, and prepares the next step. A repair may need a vendor. A shared concern may need a meeting. Residents participate, managers make the necessary decisions, and the follow-up stays visible.

### 02 — Parking preparation, 45 seconds

Consider visitor parking. Some residents want a twenty-four-hour limit; others need seventy-two hours for weekend guests. Steward brings that concern into a community case. Here is the original message behind it. The meeting draft already includes the topic, agenda, competing options, and questions that still need an answer. Earlier community experience is available alongside the draft. These are proposals for discussion. The residents and the authorized decision-maker still decide what the community will adopt.

### 03 — Parking follow-through, 40 seconds

An invited resident only needs to choose a suitable time. Once the required participants have responded, the meeting can be confirmed. After the meeting, Steward extracts proposed decisions and actions from the written minutes. The manager reviews both before confirming them. Now the parking rule has a concrete next step: a named person must publish it by a specified date. Completion evidence and verification keep that promise connected to its outcome.

### 04 — Maintenance decision, 45 seconds

A recurring elevator problem takes a different path. Steward links the report to the asset and relevant repair history. The manager can inspect what happened before, then review the vendor quotes. These prices do not buy the same work: the cheaper proposal covers guide shoes, while the recommended proposal also includes alignment. Another quote is still missing information. The recommendation explains the evidence, the alternative, and what could change the decision, so the manager can review the trade-off before approving.

### 05 — Maintenance follow-through, 30 seconds

Approval authorizes the next step within the application's rules. It does not mean a visit has been booked. The case waits for the vendor's appointment proposal. Once a dated proposal is accepted, the confirmed visit appears with its evidence. Reported completion is another separate step. An authorized person verifies the result before the case closes and the outcome enters community memory.

### 06 — Technical implementation, 35 seconds

Strands coordinates source-linked planning on Amazon Bedrock AgentCore. Specialists and evidence tools support the recommendation; application services enforce permissions, budget checks, and state changes. This recorded model run shows the tools actually used. PostgreSQL stores cases and queued work. Our restart check preserved the case and delivery records when the worker was replaced. Verified outcomes provide context for the next decision.

### 07 — Channels, 15 seconds

Telegram input and group delivery were verified on the hosted application. Real email was tested separately between controlled addresses. Vendor replies in the lifecycle demonstration are simulated.

### 08 — Closing, 15 seconds

Steward connects what a community says to what happens next, and remembers the outcome. A little less to manage. A community that remembers.

## Individual raw takes

Record each take separately. Start with two seconds of context, perform the action normally, then hold the visible result for three seconds. Keep loading/waiting time in the raw recording; the edit can remove it without inventing a response time. No simultaneous narration is required.

1. `01-parking-source.mp4`: Original Telegram report or its product source → associated parking case. No newly created case should be implied if the take opens an existing case.
2. `02-parking-preparation.mp4`: Source panel → meeting topic/agenda → 24/72-hour options → earlier community evidence.
3. `03-resident-availability.mp4`: Resident My meetings → choose offered time → Share availability → response receipt. After processing, Refresh → Meeting confirmed. Expand What we'll discuss.
4. `04-minutes-and-actions.mp4`: Written demo minutes → extraction result → select decision AND action → confirmation note → Confirm selected decisions → owner/due date.
5. `05-community-outcome.mp4`: Action-completion evidence → Record completion → verification evidence → Verify outcome; show resulting verified outcome if available.
6. `06-elevator-history.mp4`: Elevator case → relevant historical incident → source evidence → back to current case.
7. `07-quote-review.mp4`: Vendor quotes → quote sources → incomplete evidence → recommendation/alternative/What could change this decision.
8. `08-order-approval.mp4`: Approval note → Approve this service order → changed next step; after normal dispatch processing, Await the vendor's appointment proposal.
9. `09-appointment.mp4`: Clearly labelled simulated vendor reply supplied between takes → Current confirmed appointment → Vendor evidence. Do not show a sent order as appointment confirmation.
10. `10-repair-verification.mp4`: Completion evidence → Record completion → awaiting verification → Your verification notes → Confirm resolved → Outcome verified and remembered; inspect memory.
11. `11-technical.mp4`: Real editor/terminal and operational source, prepared command text supplied separately. Display recorded evidence as recorded evidence. The existing UI has no tool-trace or job-queue viewer.
12. `12-channel-evidence.mp4`: Task-relevant Telegram message/receipt and separate controlled SES test evidence. Show only the intended demo/test content. Never present separate test runs as one uninterrupted live case.

The 25-second overview is edited from these takes. Today/dashboard and memory navigation can be captured as short contextual handles inside the relevant takes; they do not need lengthy standalone tours.

## Current implementation constraints for the director

- Parking options are discussion text, not clickable votes. Community decisions come from reviewed written minutes.
- ResidentDesk needs Refresh after processing. No numeric quorum dashboard exists; Meeting confirmed is the clearest result.
- Quote UI is a list with recommendation and evidence, not a comparison matrix or budget chart.
- Manager commands may scroll to the top and clear notes. Use separate takes and reread the result after each command.
- The isolated local filming scenarios are ready and separately rehearsed, but user login and the remaining interaction captures are pending. Exact staging instructions remain private in `.scratch/video-production/UI_SHOT_GUIDE.md`.
- Time jumps require After the meeting / Demo time advanced labels. Vendor-input cuts require Simulated vendor reply. Processing time must not be confused with elapsed days.
- The recorded technical trace is real, with synthetic input. The hosted restart artifact proves preserved case/outbox records and worker replacement; it does not prove a particular leased job resumed or RDS recovery.
- Opening conversation, seed history and local scenario outcomes are synthetic. Hosted Telegram verification and separate controlled SES testing have distinct evidence scopes. No real supplier, payment, repair or measured real-world savings is implied.
