# Steward AWS demo

The demo uses the `StewardNorthgateLive` CloudFormation stack in `us-east-1`:
CloudFront/S3, Cognito, ECS Fargate API and worker, private encrypted RDS PostgreSQL,
and an ARM64 AgentCore runtime. The AWS-provided CloudFront hostname supplies HTTPS;
no domain registration is required. The expected small-demo budget is approximately
USD 120–155/month before credits, with inference and traffic dependent on usage.
This is an estimate, not a spending cap. Other account workloads are separate.

## Public, jury and community access

The existing CloudFront distribution serves three entry points:

| Entry point | Purpose | Access and effects |
| --- | --- | --- |
| [`/?mode=preview`](https://d35nbywkoth58f.cloudfront.net/?mode=preview) | Public interactive introduction | No registration. Fictional records and actions remain in the browser session; no live model or channel calls. |
| [`/?mode=judge`](https://d35nbywkoth58f.cloudfront.net/?mode=judge) | Working evaluation workspace | Dedicated review code, manager/resident views, separate synthetic data and simulated email/Telegram delivery. |
| [`/?mode=member`](https://d35nbywkoth58f.cloudfront.net/?mode=member) | Existing community application | Existing Cognito membership and the owner's configured operational channels. |

The jury runtime shares the existing API/worker infrastructure and RDS instance,
but uses the separate `steward_review` operational schema. It does not copy owner
cases, Telegram identities, live mailbox input or channel credentials. Both outbound
channels are independently disabled in its runtime composition; the owner's
`telegramDeliveryMode=live` setting does not enable delivery from the jury workspace.
New model-dependent jury input uses the configured model adapters and can incur
inference usage. Prepared examples are labelled deterministic fixtures.

The routes and operator procedure below describe the implementation. A successful
deployment and separate access checks are required before reporting live validation.
See [JURY_DEMO.md](JURY_DEMO.md) for the dataset, test route and disclosure boundaries.

## Deployment

Before first enabling jury access, configure its dedicated code while the existing
stack is in `CREATE_COMPLETE` or `UPDATE_COMPLETE`:

```powershell
.venv/Scripts/python.exe tools/cloud_review.py configure
```

This preserves the existing member registry and adds a reserved review-code field
to that Secrets Manager secret. It reuses a valid existing code on later runs and
refuses automatic rotation of an invalid one. It does not send email, create a
Cognito account, restart services or create infrastructure. The private access URL
and code are saved only in `.scratch/steward-review-access.local.json`; a first-change
registry backup is also private under `.scratch`. Never print these files into logs,
include them in source archives, or use the owner's manager credentials for judges.

The existing Dockerfiles are built in CodeBuild when local Docker is unavailable:

Base images use Docker Official Images on ECR Public. This avoids the Docker Hub
anonymous pull limit encountered on shared CodeBuild egress during the Telegram fix.

```powershell
.venv/Scripts/python.exe tools/cloud_images.py build
.venv/Scripts/python.exe tools/cloud_images.py status
```

The source archive contains only allowlisted source/build files and synthetic seed
data. It excludes `.scratch`, local databases, environment files, credentials, and
test artifacts. `steward-build-*`, `steward-api`, and `steward-inference` are dedicated
build resources. Uploaded build sources expire after 14 days. Images use release
tags in repositories configured to reject tag overwrites.

After both builds succeed:

```powershell
npm --prefix web run build
$buildState = Get-Content '.scratch/cloud-build.local.json' -Raw | ConvertFrom-Json
$demoClock = .venv/Scripts/python.exe -c "import json; print(json.load(open('.scratch/cloud-build.local.json'))['demoClock'])"
npm exec --yes --package aws-cdk -- cdk synth --app '.venv\Scripts\python.exe infra\app.py' --output infra/cdk.live.out --context mailEnabled=true --context telegramDeliveryMode=live --context reviewEnabled=true --context "demoClock=$demoClock" --context "applicationImageUri=$($buildState.applicationImageUri)" --context "inferenceImageUri=$($buildState.inferenceImageUri)" --quiet
npm exec --yes --package aws-cdk -- cdk deploy StewardNorthgateLive --app infra/cdk.live.out --rollback --require-approval never --parameters MailDomain=steward.narrativenode-labs.cloud --outputs-file .scratch/steward-cloud-outputs.local.json
```

The deployment command creates billable AWS resources and is an explicit operator
action. Keep synthesis and deployment sequential because both use the same assembly.
The command above updates the existing stack. ECS task-definition replacement uses
rollback-enabled updates. For a fresh initial deployment, `--no-rollback` can preserve
resources for repair. `mailEnabled=true` creates the owned Steward subdomain identity
and receipt rules; DNS verification and controlled vendor addresses are still required.
See `STEWARD_MAIL_DNS.md` and `LIVE_READINESS.md` for outstanding provider checks.

Preserve **all three existing contexts** on subsequent deployments:
`mailEnabled=true`, `telegramDeliveryMode=live` and `reviewEnabled=true`. Review
enablement supplies the dedicated code to the API/worker and adds the CloudFront
`review/api/*` behavior. Omitting it can remove jury access even if the public
frontend continues to load. Do not switch the owner's live channels to simulated
delivery to isolate jury activity; the review runtime already enforces its own
channel boundary.

## Prepare and check the jury workspace

After the review-enabled stack reaches `UPDATE_COMPLETE` and its services are
healthy, explicitly prepare the isolated examples:

```powershell
.venv/Scripts/python.exe tools/cloud_review.py bootstrap
.venv/Scripts/python.exe tools/cloud_review.py status
```

`bootstrap` starts a one-off Fargate task using the deployed worker definition,
network and packaged `steward.demo.review_seed` command. It refuses a worker that
does not have review enabled. Wait for this task to reach `STOPPED` with container
exit code `0` before attempting dependent checks. `status` reports the last
bootstrap task started from this checkout; it is not a full application health or
access check. All three cloud-review commands require a stable completed stack,
so wait for any deployment in progress before running them.

Preparation does not reset existing jury changes. A completed preparation is
idempotent; incomplete or already-used data is checked before it can resume. The
worker and sign-in route wait for the completed preparation marker. Normal
application startup never reloads these examples.

Before supplying the jury link and dedicated code in the appropriate Devpost
testing field, verify the following separately:

- Anonymous public preview works and its actions make no operational API calls.
- Correct review-code sign-in opens the prepared cases; incorrect codes fail.
- Manager and resident views expose their respective tasks and permissions.
- A synthetic input is processed by the deployed worker in the review store, with
  email and Telegram delivery recorded as simulation.
- Review credentials cannot read the owner API or alter its cases or channel
  bindings; the existing member sign-in still works.

Keep the code out of public source, screenshots, URLs/query strings and the public
preview bundle. Check the submission field's visibility before entering it. The
review workspace is shared by judges, so later reviewers may see actions already
completed; their timelines remain available. Do not rerun owner bootstrap or rotate
review access as a routine reset. Maintain functional access through the judging
period described in [JURY_DEMO.md](JURY_DEMO.md).

## Initialize and verify the owner community

```powershell
.venv/Scripts/python.exe tools/cloud_operations.py status
.venv/Scripts/python.exe tools/cloud_operations.py bootstrap
.venv/Scripts/python.exe tools/cloud_operations.py index
.venv/Scripts/python.exe tools/cloud_operations.py user --email YOUR_EMAIL
```

Wait for each one-off task to exit successfully before starting a dependent action.
Bootstrap imports the 63 synthetic Northgate history records without resetting cases.
Indexing invokes Titan embeddings. Task references are written to ignored local files.
Normal API/worker startup runs migrations but does not bootstrap data.

User provisioning does not send email. It generates a password and records it only
in `.scratch/steward-cloud-login.local.json`, links the Cognito subject to Northgate
manager Simon O., and refreshes both services to reload the member registry.
Rerunning the user command rotates that user's password. Never publish that file,
put it in the source archive, or use the local development access tokens in cloud.

The persisted demo clock is initialized from `demoClock` and can be advanced from the
manager simulator. This is simulation time, not live appointment timing. Omit that
context and use a fresh database for a real-time channel validation run.

The web simulator is enabled only for authenticated managers, and supplier/mail execution
remains `dry_run`. Telegram output has its own `STEWARD_TELEGRAM_DELIVERY_MODE` and
is live on the hosted demo as of 2026-09-13. Preserve `--context telegramDeliveryMode=live`
when synthesizing an update for this existing deployment. New deployments default to
simulated Telegram delivery. Bedrock inference is real. The dedicated `Steward_Demo_Bot` is now
configured in Secrets Manager with its HTTPS webhook registered to the live API.
Manager account/group linking is performed in Settings. The operator commands are
`tools/cloud_telegram.py configure`, an API/worker refresh, then
`tools/cloud_telegram.py register`; `status` is read-only. Stop local polling before
registration. Tokens are never printed and pending provider updates are preserved.
Personal account-link expiry uses wall time, independently of the demo clock.
Telegram message future-date validation also uses wall time. Workflow ingestion and
scheduled jobs retain the configured simulation clock. Authenticated terminal input
rejections are persisted before HTTP 200 acknowledgement so an unlinked group's
message cannot block its subsequent linking command. Authentication, concurrency,
transport and storage failures remain non-2xx and are not acknowledged as delivered.

SES resources now accept `steward.narrativenode-labs.cloud` once DNS resolves.
The account already has an active receipt ruleset for another project. Run
`tools/cloud_mail_route.py` to add only the Steward recipient rule to that existing
ruleset; never replace the active ruleset. This shared-rule copy is tracked in
`cloud-mail-routing.json` and must be reviewed during infrastructure updates/teardown.
SES remains sandboxed. Hosted outbound vendor mail remains simulated; controlled real
mail was verified separately. Real Telegram group delivery is enabled independently
and verified through the API/outbox/worker with Telegram's provider message receipt.
Settings distinguishes real, simulated and paused delivery. Its connection test is
idempotent per group connection and does not create a case or service order. Historical
simulated notices are not replayed when enabling the channel. See
[TELEGRAM_DELIVERY_REVIEW](TELEGRAM_DELIVERY_REVIEW.md).

## Recovery and eventual shutdown

The isolated public/jury access release was verified on September 14. See
[JURY_DEMO](JURY_DEMO.md#hosted-verification--14-september-2026) for preparation,
access-boundary checks and a worker-processed synthetic message. The existing
owner/member login remains available at `?mode=member`.

RDS is private, encrypted, has seven-day backup retention, and deletion protection.
API and worker deployment circuit breakers detect unhealthy revisions. The initial
stack deployment uses `--no-rollback` to preserve successfully created infrastructure
for repair if a service cannot start. AgentCore
creation explicitly depends on its ECR access policy. PostgreSQL 16.15 was checked
against the live RDS engine catalog at deployment preparation.
The application image makes the RDS CA bundle readable by its non-root user while
keeping TLS verification enabled. A CloudFormation custom resource discovers the
CloudFront-managed VPC-origin security group; a separate ingress resource allows
that group to reach the private ALB. Do not replace this with a VPC-CIDR-only rule.

Before shutdown, export any wanted results and decide whether to retain the database.
Stopping ECS tasks alone does not stop RDS, NAT, load-balancer, or storage charges.
Stack removal retains configured data/log resources and snapshots; review these and
the separate build resources as part of teardown. No automatic teardown is scheduled.
Do not disable database deletion protection or discard retained data without reviewing
the exact affected resources.

Deployment preparation and a passing local test suite are not proof of a healthy
cloud deployment. Confirm stack completion, running services, Cognito login, history
counts, and a worker-processed synthetic message before sharing the URL with judges.

## Live deployment evidence — 2026-09-10

The deployed URL is https://d35nbywkoth58f.cloudfront.net. CloudFormation reached
`UPDATE_COMPLETE`; API and worker started successfully against RDS. The frontend
returns 200, anonymous API access returns 401, and Cognito manager authentication
returns the Simon O. manager principal. Bootstrap imported 63 synthetic historical
records and Titan embeddings indexed all 63.

`tools/cloud_smoke.py --inject` submitted one labelled, idempotent resident message
through the public API. The worker processed it without a manual tick: one elevator
case, historical evidence, and three simulated RFQ deliveries. No real vendor email
was sent. Evidence is in `artifacts/validation/cloud-smoke.json`,
`cloud-bootstrap.json`, `cloud-index.json`, and `cloud-runtime.json`.
`tools/cloud_verify.py` refreshes read-only case/worker evidence. This deployment
check does not establish a complete live Telegram/SES maintenance lifecycle or all
AgentCore specialist paths.

The worker was then replaced through an ECS rolling deployment. A different task
resumed processing; the complete case snapshot (including timeline and three outbox
records) remained identical, with no repeated RFQ. `cloud-restart.json` and
`cloud-before-restart.json` record this check. Cognito's hosted authorization page
also returned a working password form (`cloud-hosted-login.json`).
