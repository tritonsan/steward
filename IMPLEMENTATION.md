# Steward implementation and acceptance ledger

Updated 2026-09-10. **The full production acceptance plan is not yet complete.** This ledger
separates implemented behavior, recorded evidence and unfulfilled acceptance
criteria. It does not assign a hackathon score.

The final engineering review, current test results and demo limitations are in
[FINAL_REVIEW](docs/FINAL_REVIEW.md). AWS deployment, Cognito, AgentCore and real
Telegram group intake are now verified. The hosted demo simulates outgoing
deliveries. Older measurements below retain their original scope; they are not
a fresh model benchmark for every subsequent change.

## Implemented product

| Area | Implemented behavior | Evidence |
|---|---|---|
| Durable execution | Atomic intake/case/job persistence; versioned transitions; leased jobs with heartbeat, fencing, retry limits and human escalation; SQLite migration 6 and PostgreSQL adapter | Workflow, boundary and PostgreSQL tests |
| Spending | Exact decimal reservations, concurrent category budget checks, pending commitments counted, delivery-time authority/quote checks, no automatic retry of an ambiguous order | Product boundary and PostgreSQL tests |
| Quote revisions | Source-preserving portfolios; explicit corrections; new quote or policy inputs trigger a new decision. An unsent order can be withdrawn atomically; a dispatched order is preserved for review | Quote decision and product boundary tests |
| Maintenance | Intake → RFQ → timed comparison → authorization → service order → follow-up → authorized verification → memory. Invalid vendor prices produce targeted clarification; rejected repairs enter rework/warranty follow-up | Product lifecycle tests; 60 API scenario reports |
| Community | Source-bound planning, quorum-based availability, agenda, written minutes, authorized confirmation, owned/due actions, evidence and outcome verification | Community product tests |
| Community procurement | Confirmed action creates an ordinary inbound maintenance request under existing spending rules. Parent action cannot complete before maintenance is verified | Community product and PostgreSQL tests |
| Plan revisions | Management review creates a linked immutable revision with new notes and a policy snapshot. Changed case versions reject stale inference results | Product boundary tests |
| Strands | Read-only evidence tools, coordinator and specialist agents, shared call limits, structured recommendations, prompt/model/source/usage/latency traces | Coordinator tests and live report |
| Memory | Deterministic vendor statistics, lexical retrieval, verified memory, recent-repair warranty gate, proactive proposals with accept/dismiss, optional Titan/pgvector retrieval | Semantic index and counterfactual reports |
| UI | React/TypeScript/Vite; Today, Cases, Decisions, memory and settings; source pane, quote reasoning, approval, verification/rejection, meetings and linked maintenance; separate simulation studio and resident view | Production build, desktop/mobile inspection |
| API and identity | FastAPI, optimistic versions/idempotency, SSE, server-derived actors, member checks, resident projections, Cognito JWT validation and PKCE | Python boundary tests; three PKCE tests |
| Channels | Telegram webhook and durable task notices; SES sender verdict/token/vendor/thread checks; S3/SQS ingestion with durable acknowledgement; text/PDF extraction and quarantine | Real Telegram intake verified; controlled RFQ/quote/order/appointment mail passed separately with five real SES messages and isolated local state. Hosted outgoing delivery remains simulated |
| Infrastructure | CDK CloudFront/S3, private API origin, ECS API/worker, RDS, Secrets Manager, Cognito, SES/S3/SQS, logs, AgentCore | Deployed; sign-in, real inference, persisted cases and worker replacement verified |

## Four-stage continuity extension

The current additions and their explicit limits are tracked in
[STAGES_1_4.md](docs/STAGES_1_4.md). Earlier measurements below are historical
evidence; they are not automatically evidence for newly added message types.

## Historical model and implementation measurements

UI scope refinement (2026-09-09): the supplied square Steward logo replaces the
leaf/sprout branding. Residents have a read-only community case summary and their
own meeting invitations, with a small availability response. They do not receive
quotes, spend data, raw messages, draft minutes, participant lists or workflow tasks.
Management retains the operational view; history is expandable and next steps remain
visible on mobile. Resident membership and meeting eligibility are enforced by the API.

The current functional gate is [hackathon-acceptance.json](artifacts/validation/hackathon-acceptance.json).
Counts immediately below describe the earlier 2026-09-09 validation batch.

- Python: **378 passed, 8 skipped** in the main run. The skipped tests need a
  PostgreSQL DSN and are executed separately.
- PostgreSQL 16: **8 integration tests passed** using real independent connections,
  including concurrent reservations and lease fencing.
- Lifecycle: **20 scenarios × 3 runs = 60 successful runs**, through authenticated
  API events and the worker. Model ports are deterministic in this suite. Each run
  finishes at a verified result or its expected human task.
- Live Nova triage: the original 200-input development corpus scored **90%**;
  fixes reached **99.5%** on that corpus. A separate reserved 200-input corpus,
  not used to adjust the prompt, scored **100%**. Inputs are synthetic and share
  linguistic templates; this is not an independent population estimate.
- Live quote extraction: **90/90 quotes across 30 controlled portfolios** passed
  after currency and validity corrections. The retained baseline was 78/90.
  This measures extraction, not all aspects of procurement decision quality.
- Live memory counterfactual: **9 decisions**, three repeats per history condition.
  Without adverse history the cheaper vendor won; changing which vendor had
  recurring failures changed the selected quote. Source IDs were checked.
- Live coordinator: recorded `case_evidence` and `meeting_specialist`, six model
  calls, usage and latency. The earlier failed trace is retained alongside the
  successful prompt-v3 trace. External sends were dry run.
- Semantic retrieval: **63 historical records** embedded with live Titan V2 and
  searched through actual PostgreSQL/pgvector.
- Restore: SQLite integrity and logical hashes match after backup/restore.
  PostgreSQL `pg_dump`/`pg_restore` into a fresh local database preserved 20 logical tables.
- Web: three PKCE tests passed, TypeScript/Vite production build passed and npm
  audit reported zero vulnerabilities. Desktop/mobile source navigation, focus
  return, form labels and overflow were inspected locally.

Reports: [Python](artifacts/validation/python-tests.xml),
[PostgreSQL](artifacts/validation/postgres-tests.xml),
[scenarios](artifacts/validation/scenario-runs-v1),
[reserved triage](artifacts/validation/triage-reserve-v2.json),
[quotes](artifacts/validation/quote-portfolios-regression.json),
[counterfactuals](artifacts/validation/memory-counterfactuals.json),
[coordinator](artifacts/validation/live-coordinator.json),
[semantic index](artifacts/validation/semantic-index.json),
[SQLite restore](artifacts/validation/sqlite-restore.json),
[PostgreSQL restore](artifacts/validation/postgres-restore.json).

## Remaining requirements

These are open requirements, not completed items.

1. **Complete hosted channel acceptance.** The controlled RFQ → quote → order →
   appointment run passed on 2026-09-11 using five real SES emails, actual model
   extraction and a local runtime restart. See [mail verification](docs/CONTROLLED_MAIL_TEST.md).
   A combined Telegram → clarification → hosted mail → repair verification session
   remains open. Hosted outgoing delivery is simulated; no commercial order is claimed.
2. **New-model acceptance.** Five live development examples cover the new post-order
   reply extractor. A reserved corpus for the new mail/clarification types, with
   category-level metrics, remains open. Earlier evaluation reports are historical.
3. **Broader continuity acceptance.** Targeted API/worker and PostgreSQL tests cover
   the new paths, including two fault boundaries. They do not establish exhaustive
   crash coverage or all adversarial combinations in the requested acceptance matrix.
   Recorded cost is the selected quote, not a reconciled invoice.
4. **Simulation and impact.** The API simulator uses the product runtime, and
   functional runs record human commands. A complete isolated actor world,
   persistent scenario event/replay console, and matched manual / memoryless /
   full-Steward comparison remain open. **No 50% saving or field impact is claimed.**
5. **Design acceptance.** Final desktop/mobile browser checks cover source review,
   approval, rejected repair, meeting action completion, permission edits and resident
   invitations/cards. No independent user study or full screen-reader certification
   is claimed. See the current review for exact scope.
6. **Cloud operations.** Docker/ARM64 builds, Cognito, AgentCore, Telegram and isolated
   SES mailbox delivery are verified. RDS PITR and previous-image rollback have not
   been rehearsed; the successful worker restart is a different check.
7. **Fault injection.** Concurrency, restart, ambiguity and reservations pass.
   Exhaustive crash injection at every persistent boundary with two workers and
   actual providers is not yet demonstrated.

## Deployment inputs and local effects

The Northgate stack is deployed in `us-east-1`. Live Bedrock and Titan embeddings,
Telegram group ingress and controlled SES mailbox delivery used real AWS/provider
services. Hosted outgoing messages and orders remain in dry-run mode; the isolated
controlled mail run explicitly used real delivery to the two owned test identities.

PostgreSQL 16 and pgvector were installed in the existing WSL Ubuntu distribution.
Isolated test and restore databases remain; generated credentials and scratch dumps
are ignored local files. No existing application database was replaced. PostgreSQL
deliberately serializes writes for this single-community scope.

Submission/video, payments, multiple properties, WhatsApp and audio minutes remain
outside the accepted scope. See [operations](docs/OPERATIONS.md) for launch and recovery.

## Telegram group onboarding (2026-09-10)

Management can choose one Telegram group via an account-bound startgroup link,
with live provider checks for bot and manager permissions before committing the
connection. Dynamic group authorization also gates pending group notifications.
The Settings panel shows receipt and processing separately. The real Northgate Demo
group is connected; two resident messages reached the worker and merged into one
B Block case. Historical onboarding tests and the final regression suite verify
authorization, durable rejection and separate provider/workflow clocks.
[Setup](docs/TELEGRAM_SETUP.md) · [Evidence](artifacts/validation/telegram-onboarding.json).
