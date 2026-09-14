# Public preview and jury review

The public preview and the working jury workspace are separate experiences.
The jury workspace uses the application services and the durable worker with a
separate operational store. It must never use a copy of the owner's live
database, Telegram identity bindings, or mail inbox.

## Entry points and sign-in

- **Public preview:** <https://d35nbywkoth58f.cloudfront.net/?mode=preview>.
  Anyone can explore three fictional cases, switch between sample manager and
  resident views, inspect sample sources, and try local approval/verification
  actions. The page explicitly identifies these as browser-only interactions;
  there is no live inference or external delivery. Reset affects only this sample.
- **Jury workspace:** <https://d35nbywkoth58f.cloudfront.net/?mode=judge>.
  Enter the dedicated code supplied in the appropriate Devpost testing field.
  No account creation, email verification or operator approval is needed. The
  review banner provides manager/resident switching; the application retains its
  sign-out action. Review sessions use a distinct API and do not grant access to
  the owner community.
- **Existing members:** <https://d35nbywkoth58f.cloudfront.net/?mode=member>.
  This retains the owner's Cognito membership and operational configuration. It
  is not the jury login route, and its credentials must not be shared as review
  credentials.

The jury code and role-specific session are retained in browser `sessionStorage`
for the review tab. Use **Sign out** when finished on a shared computer. Do not
put the code in a URL, source repository, public screenshot or public preview.
The listed URLs describe the intended entry points; deployment and live access
verification must be recorded separately rather than inferred from this guide.

## Hosted operator sequence

Use the existing `StewardNorthgateLive` stack in `us-east-1`:

```powershell
.venv/Scripts/python.exe tools/cloud_review.py configure
# Build and deploy with --context reviewEnabled=true (see CLOUD_DEMO.md).
.venv/Scripts/python.exe tools/cloud_review.py bootstrap
.venv/Scripts/python.exe tools/cloud_review.py status
```

Run `configure` before the enabling deployment, while CloudFormation is stable.
It preserves the member registry, creates or reuses a dedicated high-entropy
review code in its existing Secrets Manager secret, and saves the private access
details in `.scratch/steward-review-access.local.json`. It does not create an
owner/manager Cognito account or change the existing owner's password. The code
is not printed. Keep the private file and registry backup out of archives and
logs; `tools/cloud_operations.py user` is for owner access, not this workflow.

After the review-enabled deployment completes and services are healthy,
`bootstrap` runs preparation as a one-off task using the deployed worker image.
`status` reports that task's state and container exit code. Wait for `STOPPED`
with exit code `0`, then test the public route, code sign-in, both roles and a
worker-driven action. Status alone does not establish end-to-end readiness.
These operator commands reject an in-progress or failed stack; finish or repair
the deployment first.

Preserve `reviewEnabled=true` on later deployments, along with the existing mail
and owner Telegram contexts documented in [CLOUD_DEMO.md](CLOUD_DEMO.md). The
review runtime disables email and Telegram independently of those owner settings.
It uses the existing compute and database infrastructure with a separate schema;
new model calls still use the configured AWS inference services. Do not copy the
owner's live database or restore it into the review schema.

## Prepare the jury workspace

Run this explicit command in the same configuration as the hosted application:

```console
python -m steward.demo.review_seed
```

The equivalent source-checkout wrapper is `python tools/prepare_jury_demo.py`.
The command calls `steward.review.build_review_runtime()`. PostgreSQL uses the
`steward_review` schema; local SQLite uses `steward-review.db` beside the normal
database. Both email and Telegram delivery are disabled independently. There is
no `--reset`, live database argument, or data deletion option. The runtime still
enforces ordinary case versions, budget policy and human authorization.

Preparation imports the repository's 63 synthetic Northgate history records and
uses the ordinary simulation input, vendor-reply, settings and worker APIs to
prepare two cases:

1. **Visitor parking: choose a 24-hour or 72-hour limit.** Inspect the original
   resident message, source-linked discussion agenda, competing options, open
   question and conditional need for signage quotes. The prepared manager has
   offered two evening times in Europe/Istanbul with quorum two and confirmed
   their own availability. Daniel K. still needs to respond using the resident
   view. No meeting time or community decision has been confirmed.
2. **A Block elevator: recurring shudder near floor four.** Compare the USD 705
   complete scope with the USD 540 offer that excludes rail alignment. Inspect
   the recommendation, competing quote and evidence that would change the
   decision. The case is waiting for manager approval under a `prepare_only`
   policy. No service order or appointment has been committed by preparation.

These prepared classification, extraction and recommendation outputs are
**deterministic fixtures**, not evidence of fresh model inference. Source events,
the preparation marker and this guide identify the simulation. The preparation
ports are injected into this command only. Starting the review API/worker with
its normal configuration restores the real model adapters for newly entered
messages and subsequent model-dependent actions. Real external suppliers and
Telegram groups are not contacted from this review workspace.

Running the command again after success returns the original scenario IDs and
preserves reviewer changes. A partial preparation can resume only while its
input remains unchanged; it refuses to consume unrelated pending input or to
prepare an already-used workspace. Deploy the review-enabled API/worker, then
run this preparation command. The worker leaves review jobs untouched and jury
sign-in returns a preparation message until the complete marker exists. The
command writes that marker only after checking both actionable cases. No
application startup automatically reloads seed data.

## Suggested review route

Start with the parking case and inspect its evidence and agenda. In the resident
view, Daniel K. can answer the prepared invitation; other operational details
are not exposed there. Return to the manager view and compare
the elevator quotes before approving the current decision. The resulting order
is a recorded simulation; its delivery alone does not confirm an appointment.
Use the simulation tools to supply a vendor response and advance virtual time
when following later steps. New free-text input invokes the configured live
models, so a short processing delay is expected.

Each reviewer action changes this shared jury workspace. It does not change the
owner's live community. Repeated review must use current case versions and
should inspect completed timeline events if another reviewer already performed
an action. Do not expose the owner's account credentials as jury credentials.

## Devpost access requirement

The [official rules](https://agentsforhumans.devpost.com/rules) allow a private
working project when its testing instructions provide login credentials. The
project must remain free and available to the sponsor, administrator and judges
through the judging period, which ends October 8, 2026 at 5:00 PM Pacific
(October 9 at 03:00 in Istanbul). Public anonymous access is an additional
preview, not a replacement for functional review access.

Record the final public URL, jury URL, dedicated credentials and the testing
route in the appropriate submission fields only after verifying their access
and visibility. Do not include cloud credentials, local owner tokens, bot
tokens, or live supplier addresses in public testing notes.

## Hosted verification — 14 September 2026

The public preview and jury routes are deployed at the URLs above. The existing
CloudFormation stack reached `UPDATE_COMPLETE`; the API and worker both reached
their steady state. The explicit isolated preparation task exited with code zero.
The first image exposed an installed-package seed-path error; the fixed image was
verified with an installed-wheel bootstrap and deployed after automatic rollback.

The access smoke test passed: 63 synthetic historical records, two prepared cases,
manager and resident sessions, owner rejection of review tokens, resident denial of
management settings, and disabled real Telegram linking. Prepared deliveries have
recording-provider IDs. A third, labeled plumbing message was accepted through the
normal input API and the worker opened its case without a manual tick. The prior
verified booster-pump repair produced a warranty-review task before a new paid job.

The existing owner Cognito login still reads its separate four open cases and 63
historical records. The public preview and jury manager/resident pages were also
checked in the live browser. Access codes and raw credentials are excluded from the
public repository. This check does not establish a new real external-channel run.

Reproduce access checks with `python tools/cloud_review_smoke.py`; `--inject` adds
one fixed idempotent synthetic message, and `--status` observes it. The tool reads
only the ignored review access file and saves a sanitized aggregate report in
`artifacts/validation/cloud-jury-access.json`.
