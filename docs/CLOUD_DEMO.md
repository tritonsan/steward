# Steward AWS demo

The demo uses the `StewardNorthgateLive` CloudFormation stack in `us-east-1`:
CloudFront/S3, Cognito, ECS Fargate API and worker, private encrypted RDS PostgreSQL,
and an ARM64 AgentCore runtime. The AWS-provided CloudFront hostname supplies HTTPS;
no domain registration is required. The expected small-demo budget is approximately
USD 120–155/month before credits, with inference and traffic dependent on usage.
This is an estimate, not a spending cap. Other account workloads are separate.

## Deployment

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
npm exec --yes --package aws-cdk -- cdk synth --app '.venv\Scripts\python.exe infra\app.py' --output infra/cdk.live.out --context mailEnabled=true --context telegramDeliveryMode=live --context "demoClock=$demoClock" --context "applicationImageUri=$($buildState.applicationImageUri)" --context "inferenceImageUri=$($buildState.inferenceImageUri)" --quiet
npm exec --yes --package aws-cdk -- cdk deploy StewardNorthgateLive --app infra/cdk.live.out --rollback --require-approval never --parameters MailDomain=steward.narrativenode-labs.cloud --outputs-file .scratch/steward-cloud-outputs.local.json
```

The deployment command creates billable AWS resources and is an explicit operator
action. Keep synthesis and deployment sequential because both use the same assembly.
The command above updates the existing stack. ECS task-definition replacement uses
rollback-enabled updates. For a fresh initial deployment, `--no-rollback` can preserve
resources for repair. `mailEnabled=true` creates the owned Steward subdomain identity
and receipt rules; DNS verification and controlled vendor addresses are still required.
See `STEWARD_MAIL_DNS.md` and `LIVE_READINESS.md` for outstanding provider checks.

## Initialize and verify

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
