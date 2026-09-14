# Four-stage continuity extension

Implemented locally on 2026-09-09. Product language remains English; the single
Northgate property, supplied Steward logo and resident/management separation remain.
The complete combined SES/Telegram-to-verified-repair acceptance run is still open.
Code and local tests alone must not be presented as provider verification.

Update 2026-09-10: AWS deployment, real Telegram group intake and an authenticated
controlled SES mailbox delivery are now verified separately. Final defect fixes and
fresh regression evidence are in [FINAL_REVIEW](FINAL_REVIEW.md). The historical
counts below describe the original extension, not the current complete suite.

Update 2026-09-11: a controlled RFQ → quote → order → appointment run passed with
five real SES emails, actual model extraction, isolated local persistence and a
runtime restart. See [CONTROLLED_MAIL_TEST](CONTROLLED_MAIL_TEST.md) for its precise
scope. Hosted outgoing delivery is still simulated.

## Stage 1: clarification and comparable quotes

`operations/clarifications.py` stores one personal clarification per intake question,
the original assessment, a source-linked answer and the reassessment continuation.
The reporter or a manager can answer. A resident cannot read or answer another
resident's question. Web and verified private Telegram replies use the same service.
After 24 hours one reminder is recorded; after 48 hours management takes over.
RFQs remain blocked until a usable assessment replaces the incomplete one. Conflicting
asset/category evidence or a still-incomplete reply goes to management in the same case.

Quote evidence includes explicit currency, total, scope, inclusions, exclusions,
availability and validity. Missing information remains visible and triggers a targeted
vendor question. No currency conversion or inferred missing terms authorize an order.
Rejection applies to one quote revision. A later corrected quote can be considered.
The comparison clock starts at the first successful RFQ delivery, including when no
vendor has replied. Reservation and delivery gates recheck the current evidence.
Delivered or uncertain orders are protected from silent replacement.

## Stage 2: vendor-backed appointments

`awaiting_appointment` separates an order receipt from a confirmed visit. Post-order
mail is first stored with an `appointment.extract` job; the worker extracts acceptance,
rejection, appointment, postponement, completion, no-show or changed-quote evidence.
Model calls are outside the intake transaction. Exact source excerpts are validated.

Management configures working weekdays/hours, notice period and access instructions.
Automatic acceptance needs explicit start/end, unchanged price/scope, access agreement,
no conflicting visit and configured rules. A required access person's card and delivery
of the acceptance must finish before the case becomes `scheduled`. A management exception
requires a reason and explicit vendor evidence. An uncertain acceptance requires a
provider receipt; it is never blindly sent twice. A late receipt for an older revision
cannot replace a newer confirmed date.

The worker checks one hour after the visit ends. Silence prompts a status request,
not a no-show finding. Confirmed completion still requires the existing authorized
result verification; rejection follows the warranty/rework path. Proposal history
is retained and automatic rescheduling is bounded to two retries.

## Stage 3: meeting and decision continuity

Scheduling responses bind to a specific round. Management supplies initial slots,
participants, quorum and an approved future retry window. Expired rounds offer unused
future slots, with at most two automatic retries and no reduction in quorum. Old
responses cannot vote for a new round. Missing approved options open a management task.

A material revision creates a new linked community case. The original decision remains
effective until the replacement completes meeting, minutes and authorized confirmation.
Rescheduling a confirmed meeting similarly creates a linked replacement and invalidates
the former invitation. Neither action cancels an existing paid commitment or completes
an old task. Owner/date edits require a reason, preserve successive assignment revisions
and keep a durable follow-up at the new due date, including after an earlier escalation.

## Stage 4: audience boundaries and durable channel intake

Tasks, schedules, reminders and timeline events are durable notification intentions;
the notification collector materializes them through the existing fenced outbox.
Personal tasks and invitations go only to registered linked recipients. Group notices
contain sanitized shared status/results. New activity is summarized after 18:00 in the
property timezone. Membership, assignment, invitation validity and kill switch are
checked again at dispatch; changed deadlines invalidate old pending task notices.

`POST /api/telegram/link` issues a single-use ten-minute private-chat token. Only its
hash is stored. Unlinked residents use their web card; private details are never sent
to the group as a fallback. Test-only long polling uses the webhook normalization and
persistent ingress path. Its cursor advances after durable intake or a durable rejected
update record. Identity mappings are excluded from resident data/model evidence.

SES ingress retains verification verdicts and raw routed input, then creates a durable
continuation or review before acknowledging SQS. It does not hold SQS open for inference.
Management can dismiss a review, request a readable replacement from a verified sender,
or reprocess a trusted routed record. Authentication failures cannot be overridden by a
"trust" button. Unknown senders and unreadable attachments remain distinct review reasons.

## API and data compatibility

- Existing case command API adds `answer_clarification`, `reject_quote`, `clarify_quote`,
  `appointment_access`, `appointment_accept`, `reconcile_appointment_delivery`,
  `meeting_revise_decision`, `meeting_reschedule` and `meeting_reassign`.
- Appointment policy uses `GET/PUT /api/appointment-rules`; inbound review uses
  `GET /api/inbound-reviews` and `POST /api/inbound-reviews/{id}/commands`.
- Commands retain actor identity from the session, idempotency and expected versions.
  Resident views expose only relevant case/meeting information and assigned small cards.
- Shared migration 6 runs on SQLite and PostgreSQL without seed reload or record deletion.
  Legacy `scheduled` records without confirmed appointment evidence become
  `awaiting_appointment`, with a management review instead of a fabricated visit time.
  Incomplete historical quotes cannot authorize new orders; sent orders stay preserved.

## Evidence and remaining acceptance

The local suite passed **398 tests**, with **16 PostgreSQL scenarios** passing in a
separate real database run. TypeScript/Vite and three web tests passed.
Machine-readable results are in
[`four-stage-continuity.json`](../artifacts/validation/four-stage-continuity.json).
The new regression file is `tests/test_four_stage_continuity.py`; shared-store scenarios
also run against actual local PostgreSQL. They include atomically rolled-back clarification
answers and recovery after appointment acceptance delivery without a second send.

Five development examples used real Nova Pro calls for post-order extraction, with
exact evidence and time checks; three more checked missing information and clarification. These are smoke checks, not a blind accuracy benchmark.
The actual resident personal-card answer was exercised in a narrow browser viewport;
the manager settings and supplied logo were visually checked. The final local sign-in
also passed and the worker is running against the existing review database in dry-run mode. Full desktop/mobile and
screen-reader acceptance, independent usability, the reserved new-type model corpus,
and exhaustive fault injection remain open.

The live gate requires the test Telegram bot and private test group, registered member
mapping, verified SES domain, controlled sender/vendor addresses, AWS profile and
SES S3/SQS settings. With those inputs, follow [OPERATIONS.md](OPERATIONS.md) and record
resident message → clarification → RFQ → reply → order → appointment → result verification,
restarting the worker in the middle. No real provider messages were sent in this extension.

Full AWS application deployment, payment/invoice reconciliation, matched impact studies,
submission materials and independent user studies are outside this extension's scope.
