# Meeting preparation before scheduling — 2026-09-11

## Root cause

The community worker invoked the agenda planner only after a candidate time met
quorum. Offering times therefore created an availability round without preparing
the subject, agenda or options. The immutable meeting brief also omitted the
historical text already available to the resolution planner. The management UI
rendered agenda headings only after the final meeting packet existed; it did not
render open questions or separate solution/quote-planning sections.

These were implementation gaps. Availability and external channel configuration
should not prevent internal meeting research.

## Corrected behavior

- A durable `community.draft` job prepares a discussion draft as soon as a
  resolution plan chooses a meeting. A bounded, idempotent reconciliation pass
  queues missing work for existing open meeting plans.
- Inputs are frozen before inference and include messages, historical records,
  management notes and permitted directory options. Each draft belongs to one
  immutable resolution-plan revision. Older drafts remain available.
- The structured agenda includes topic, agenda items, concrete discussion
  options/tradeoffs, open questions and conditional quote needs. Source and
  vendor IDs are validated. Quote needs are planning scopes, not actual prices,
  received supplier offers or authorization to send an RFQ.
- Model calls remain outside database transactions. The worker uses its existing
  renewable lease and bounded retries. Draft/evidence/timeline persistence is
  atomic and checks that the source plan and messages still match.
- The manager can read preparation before offering a date, inspect original
  source messages, and then offer times using the existing English/Istanbul
  controls. Resident invitations retain their membership and privacy checks.
- Quorum still controls scheduling. Once met, the final meeting packet reuses
  the prepared agenda. Draft creation cannot approve spending, submit personal
  availability, confirm a meeting or send invitations.

## Verification

- Full Python regression passed before the last source-consistency guard;
  16 PostgreSQL integration tests were skipped because no test DSN was set.
- Seven targeted preparation tests passed on the final code: API/worker delivery
  before scheduling, resident isolation, quorum reuse without another model call,
  invalid source/vendor rejection, restart recovery, existing-plan backfill and
  preservation across plan revisions.
- Frontend: 30 tests, TypeScript/Vite production build and source formatting passed.
  Python lint passed.
- A generic synthetic parking message was evaluated with real Nova Pro through
  the Strands agenda adapter. It produced a source-linked agenda, two discussion
  options and open questions without inventing a supplier quote. The prompt was
  then tightened to require concrete options and preserve explicitly competing
  rules. After explicit user permission, the hosted worker also evaluated the
  original Telegram message with this release and retained its 24/72-hour options.
- The desktop browser displayed the new draft before any scheduling command;
  opening a source showed the original synthetic resident message while keeping
  case context. Screenshots are local review evidence in the ignored
  `.scratch/meeting-preparation-review` directory. The hosted section was also
  verified at 390 x 844 with no horizontal overflow; the viewport was restored.

## Release status

API and inference images `163eb13d0f1c473d` built successfully on CodeBuild.
**Deployed and verified at 2026-09-11 14:16 UTC.** CloudFormation reported
`UPDATE_COMPLETE`; API and worker each had one running task, no pending task,
and completed rollouts on this release. Only application/inference images and
the static console changed; existing infrastructure and parameters were preserved.

Automatic approval review initially blocked the original-message validation
pending explicit transfer permission. The user then approved processing that
Telegram message in their AWS Bedrock account and deploying the correction.
The hosted worker prepared the existing case automatically; no replacement
message or manual case-state edit was needed.

The case advanced from version 4 to 5 with one preparation event. Its draft
contains three agenda items, the 24-hour and 72-hour alternatives with tradeoffs,
two open questions and one linked historical record. It identified no external
quote requirement for the rule-only scope. The management UI displayed the draft
and opened the original Telegram message as supporting evidence.

The offered schedule, participant configuration, own availability response and
final meeting-packet state matched the pre-deployment snapshot. The meeting still
waits for participant availability. Anonymous case access returned 401 and
authenticated access succeeded; the expected JavaScript and CSS assets were served.

The live offered time, participant list and responses are unchanged. This
release does not change the frozen demo clock or dry-run outbound channel mode.
