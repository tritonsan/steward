# Final engineering review — 10 September 2026

Steward has a reproducible, tested product path suitable for preparing the hackathon
submission and video. This conclusion covers the explicitly labeled synthetic demo,
real model execution and the verified channel checks below. It is not a claim that
the entire production acceptance plan or a field impact study is complete.

## Defects corrected

| Finding | Result |
|---|---|
| Replies to an order/appointment were matched only against the original RFQ Message-ID | Delivered same-case/same-vendor thread IDs and SES provider Message-IDs now route correctly; sender, token and original-RFQ evidence remain mandatory |
| A scanned attachment without body text was classified as a case-routing failure | Authenticated unreadable attachments enter the correct review and replacement-document path |
| Local Telegram polling could advance its cursor after a retryable store conflict | Retryable failures remain pending; only durable terminal rejection advances the cursor |
| Manager availability omitted the current meeting round | Responses carry the current schedule ID and restore the actor's own saved choice |
| Open case details became stale during worker progress | SSE updates refresh the active case while preserving source context |
| Settings refresh erased unsaved permission edits | Draft values survive refresh and remain explicitly unsaved until submitted |
| Community outcome review also showed maintenance verification buttons | Only the appropriate task-specific outcome actions are shown |
| Proactive maintenance duplicated a calendar review after evidence bootstrap | One review identity per rule/asset/date; older copies remain as inactive history, and accepted/dismissed reviews do not reopen |
| Evaluation processes returned success despite failed metrics | Failed gates and empty corpora return nonzero; each acceptance run uses fresh evidence and rejects source changes during validation |
| Public release risked carrying local/cloud working state | An allowlisted source package, SHA-256 manifest and redacted secret-pattern gate exclude raw messages, reply tokens, local credentials and databases |

Cognito loading/cancellation/expiry messages, appointment history, action reassignment
display, English dates, focus handling and empty/error states were also tightened.
The container images now include the MIT license.

## Current verification

| Check | Recorded result |
|---|---|
| Complete Python regression suite | 448 passed; 16 PostgreSQL-gated skips, run separately below |
| PostgreSQL | 16 lifecycle + 20 Telegram + 3 proactive checks passed; isolated schemas and real connections |
| Reproducible offline acceptance command | 180 passed; 20 scenarios × 3 = 60 successful API/worker runs, including 33 verified outcomes and 27 expected human reviews |
| Frontend | 5 tests, TypeScript/Vite build and formatting passed |
| Dependencies | npm audit: 0 known advisories across 147 dependencies; OSV: 0 affected packages among 75 Python pins; pip check clean |
| Browser | Desktop and 390-pixel mobile view; source/back-focus, approval, repair rejection, community action and outcome, permission edits, resident invitations and personal reply |
| Source reproducibility | Clean extracted source imports and seed loading; Python compilation and wheel build passed; no public repository was created |
| Controlled real mail follow-up, 2026-09-11 | Five real SES emails through RFQ, quote, order and confirmed appointment; actual model extraction, isolated local state, restart, one order/reservation; 22 targeted mail/harness/source checks passed |
| UI and workflow follow-up, 2026-09-11 | 24 frontend tests, build/format, 5 new backend checks and 36 existing targeted regressions passed; isolated desktop and 390 × 844 mobile journeys verified approval continuity, repair rejection, retained meeting outcomes and resident-only controls. See [UI polish review](UI_POLISH_REVIEW.md) for the findings and hosted verification. |
| English date/time follow-up, 2026-09-11 | 30 frontend tests passed under America/Los_Angeles; English calendar, Istanbul display/UTC persistence, keyboard and mobile checks passed. Static-only live update verified and the existing meeting proposal preserved. See [Date/time review](DATE_TIME_REVIEW.md). |
| Meeting preparation follow-up, 2026-09-11 | Preparation separated from quorum, historical evidence included, and management draft UI added. Full Python regression and seven targeted tests passed; 30 frontend tests and build passed. Following explicit user permission, release 163eb13d0f1c473d was deployed: the real Telegram message produced a 24/72-hour comparison while preserving scheduling/availability. Desktop, mobile, source inspection and service health verified. See [Meeting preparation review](MEETING_PREPARATION_REVIEW.md). |

The offline suite uses deterministic model ports and synthetic external events.
Historical real model reports remain separately labeled: reserved synthetic triage
200/200, quote extraction 90/90 across 30 sets, and 9 controlled memory-counterfactual
decisions. The newer reply/clarification probes are development examples, not a
reserved accuracy benchmark. No percentage time saving or field-study impact is claimed.

Machine-readable summaries: [functional gate](../artifacts/validation/hackathon-acceptance.json),
[final review](../artifacts/validation/final-review.json),
[UI review](../artifacts/validation/final-ui-review.json),
[dependencies](../artifacts/validation/dependency-audit.json).
See [HACKATHON_VALIDATION](HACKATHON_VALIDATION.md) for rerunning the checks.

## Claims available for the submission

The [official event page](https://agentsforhumans.devpost.com/) has five judging
criteria, including Presentation. Steward fits the Good Neighbor Agents track.

| Criterion | Demonstrable evidence | Limit to state |
|---|---|---|
| Technical implementation | Strands extraction/planning, AgentCore specialist tools, durable continuations, deterministic spending gates, actual cloud deployment | Full provider lifecycle and every possible crash boundary are not certified |
| Design | Separate resident/manager workflows, source-backed decisions, contextual personal responses, tested mobile actions | No independent usability study or screen-reader certification |
| Potential impact | A defined community workflow, completed scenarios and explicit tracking of human interventions | Synthetic evaluation; no measured resident time or financial savings |
| Creativity | Recurrence/warranty checks, history-sensitive vendor decisions and community decisions tracked through implementation | Seed history and controlled comparisons are synthetic |
| Presentation | A working product and reproducible scenarios ready to record | Video itself has not been prepared or judged |

## Handoff to submission preparation

1. Publish the reviewed source package to a public repository with MIT license,
   README, setup instructions and [architecture diagram](ARCHITECTURE.md).
2. Prepare the submission description and a working demo video of at most five
   minutes, explaining the audience/problem and labeling simulated vendor events.
3. Supply appropriate reviewer access for the optional hosted demo. Do not publish
   the owner's manager password, Telegram identifiers or vendor reply tokens.
4. Complete required Devpost fields and Builder ID during submission preparation.
   Nothing has been submitted or published by this audit.

The controlled real vendor-mail portion was completed on 2026-09-11; see
[CONTROLLED_MAIL_TEST](CONTROLLED_MAIL_TEST.md). It used two owned identities and
isolated local application state, with no actual supplier or repair. The public
demo still uses dry-run supplier mail. Telegram output is independently enabled and
verified on 2026-09-13 with a real group delivery receipt; see
[TELEGRAM_DELIVERY_REVIEW](TELEGRAM_DELIVERY_REVIEW.md). This follow-up passed 476
Python tests (16 PostgreSQL-specific skips), 30 frontend tests and the live panel
check. Remaining production follow-ups include
the complete hosted Telegram-to-repair journey, RDS restore/rollback rehearsal,
independent user research and comparative impact study in
[REMAINING_WORK](REMAINING_WORK.md).
