# Steward operations guide

## Local product

Run from the repository root. Python 3.12 and Node 22 match the container definitions.
Follow [README](../README.md) to install dependencies, build the web console,
bootstrap Northgate history and start the API. Python uses `requirements.lock` as
a constraint file; npm uses its committed lockfile and `.npmrc`.
Configuration is read from process environment. `.env.example` is a reference,
**not an automatically loaded file**.

`STEWARD_EXECUTION_MODE=dry_run` simulates provider mail and Telegram sends.
Bedrock still makes real inference calls unless tests inject model ports.
Set `STEWARD_AWS_PROFILE` and `STEWARD_AWS_REGION` for an authorized account.
Nova Pro is the reference model; semantic embeddings use Titan V2.

Use identical database/clock settings for API and worker. Leave
`STEWARD_SIMULATION_CLOCK_START` unset for wall time. Virtual time is persisted
and allowed only in dry run. Use a separate database for live operation;
changing an environment variable does not remove stored simulation history.

`steward-bootstrap` explicitly imports synthetic history without resetting cases.
Normal API/worker startup does not import history. Seed files supply the directory
and initial policies. Automatic RFQ initiation initially covers `elevator`;
additional `STEWARD_AUTO_QUEUE_RFQ_CATEGORIES` require category policy permission too.

The local API serves `web/dist` on `127.0.0.1:8000`; it does not launch a worker.
Run this in a second shell with the same environment for continuous processing:

```powershell
.venv/Scripts/steward-worker.exe
```

Workers scan every ten wall-clock seconds. Simulation studio can request a tick.
Database locking and fenced leases support a second worker, although cloud defaults
to one. Provider/model calls run outside database transactions. Existing standalone
demo commands are regression fixtures; the API/worker/console is the product path.

## Identity and channels

Local tokens are for development only. Never expose the documented sample token
on a network-accessible host. Production uses Cognito plus `STEWARD_MEMBERS`, a
server-side JSON map from JWT subject to `{actor_id, role}`. Managers must also
belong to the Northgate management committee. JWT claims cannot grant membership.

Set `STEWARD_COGNITO_ISSUER`, `STEWARD_COGNITO_CLIENT_ID` and
`STEWARD_COGNITO_DOMAIN`. The browser obtains public configuration from the API
and uses authorization code + PKCE. CDK disables self-registration.

Telegram supports manager-selected group onboarding from Settings. See
[TELEGRAM_SETUP.md](TELEGRAM_SETUP.md) for the real one-person test. The bot token
and webhook secret are operator configuration; managers link accounts and select
a group without editing server configuration. Legacy static allowlists remain
supported until the first persistent connection. The channel secret schema is:

```json
{
  "telegram_enabled": true,
  "telegram_bot_token": "set-in-secrets-manager",
  "telegram_bot_username": "your_test_bot",
  "telegram_webhook_secret": "set-in-secrets-manager",
  "telegram_allowed_chat_ids": ["configured-private-chat-id"],
  "telegram_member_names": {"configured-user-id": "James D."}
}
```

Configure the provider webhook at `/api/channels/telegram` with the matching
secret. Notices use `STEWARD_PUBLIC_URL`. Set `STEWARD_TELEGRAM_BOT_USERNAME` to
show the account-link button. An authenticated member receives a single-use,
10-minute `/start` link; private questions never fall back to the group.

For local controlled tests only, set `STEWARD_TELEGRAM_TEST_POLLING=true` on the
worker and remove the webhook from that **test bot**. API/webhook and polling
share ingress; the cursor advances after durable acceptance or durable rejection.
Do not point this mode at a bot used by another deployment. Keep secrets out of logs.

SES needs owned management/controlled vendor domains, verified identities, a
configuration set, active receipt rules and correct DNS. The worker consumes
`STEWARD_SES_QUEUE_URL`; acknowledgement follows durable raw receipt and continuation/quarantine, before model inference.
Case token, authentication verdicts and registered vendor must agree. The API
exposes quarantined receipts at `/api/inbound-reviews`. Configure directory addresses
for the controlled inboxes; a CDK domain parameter does not rewrite seed vendors.

`live_rfq` enables real RFQs within policy. `live_commitment` also requires
`STEWARD_ALLOW_LIVE_COMMITMENTS=true`. Kill switch, budget, allowlist and quote
evidence are checked at dispatch. Ambiguous service orders remain reserved and
require manager/provider evidence using `reconcile_order_delivery`, never a new send.

## PostgreSQL and migrations

Set `STEWARD_DATABASE_URL` to a DSN using a dedicated role. Cloud ECS injects RDS
JSON as `STEWARD_DATABASE_SECRET`; the adapter uses verified TLS with the RDS CA.
Do not print either configuration in diagnostics. Provision the `vector` extension,
set `STEWARD_SEMANTIC_MEMORY_ENABLED=true` and index explicitly:

```powershell
.venv/Scripts/steward-bootstrap.exe
.venv/Scripts/steward-index-memory.exe
```

The index is derived data. Verified history remains the source of truth. Matching
content hashes skip indexing; verified closure enqueues an index continuation.

Migrations execute at store startup and preserve existing rows. Migration 5 adds
jobs, human tasks and reservations and seeds jobs for existing source-backed open
cases. PostgreSQL serializes writes with an advisory transaction lock; SQLite uses
`BEGIN IMMEDIATE`. Nested writes use savepoints. Back up before changing versions.
Seed bootstrap is not a migration; do not downgrade schemas in place.

## AWS preparation

```powershell
npm --prefix web run build
.venv/Scripts/python.exe -m pip install -c requirements.lock -r infra/requirements.txt
.venv/Scripts/python.exe infra/app.py
```

The assembly is `infra/cdk.out`. Deployment needs a healthy Docker builder for
API and ARM64 AgentCore images, CDK CLI, a bootstrapped account/region and explicit
`MailDomain` / `ControlledVendorDomain` parameters. Review the synthesized resources
and costs. Default execution is dry run; initial member/channel secrets are empty.
Run history bootstrap and indexing in a one-off ECS task once RDS is ready.

Outputs include console URL, member registry secret, receipt rule set, database
secret and inference ARN. Provision Cognito users and registry entries, update
channel secrets and restart tasks after secret changes. Check DNS, receipt
activation and exact vendor addresses before choosing a live mode.

For the AWS demo, prebuilt CodeBuild images and the mail-disabled deployment path
are documented in [CLOUD_DEMO](CLOUD_DEMO.md). Use its commands when local Docker is
unavailable. Live status must be verified with `tools/cloud_operations.py status`;
synthesis or image-build success alone does not establish application health.

## Verification

```powershell
.venv/Scripts/python.exe -m pytest --junitxml=artifacts/validation/python-tests.xml
.venv/Scripts/python.exe -m ruff check src/steward tests infra/app.py
npm --prefix web test
npm --prefix web run build
npm --prefix web audit
```

For real PostgreSQL tests, export a test-only `STEWARD_TEST_POSTGRES_DSN` and run
`tests/test_postgres_integration.py`. It creates/removes generated test schemas.
Do not use a production role.

Live evaluations consume provider inference and retain synthetic-input labels:

```powershell
.venv/Scripts/python.exe -m steward.evaluation --corpus data/evaluation/triage-reserve-v2.jsonl --output artifacts/validation/triage-reserve-v2.json
.venv/Scripts/python.exe -m steward.quote_evaluation --output artifacts/validation/quote-portfolios-regression.json
.venv/Scripts/python.exe -m steward.memory_evaluation
```

The reserved corpus has now been measured. Create a new held-out corpus if tuning
against these results. Do not call a repeatedly tuned benchmark unseen.

## Restore and rollback

SQLite uses its online backup API, exclusive destination creation, integrity/FK
checks and canonical logical hashes:

```powershell
.venv/Scripts/steward-backup-check.exe steward.db backup-new.db restore-new.db --report artifacts/validation/sqlite-restore-new.json
```

Existing destination files are never overwritten. Keep production backups outside
release directories. For PostgreSQL, take custom-format `pg_dump`, restore into
a **new database**, then compare schema, counts and canonical row hashes before
switching anything. The recorded local PostgreSQL rehearsal passed.

RDS PITR and previous-image rollback are not yet rehearsed. The intended cloud
sequence is: pause workers, retain current image digest and backup, restore into
a separate database, validate with external sends disabled, then change service
configuration. Never replay ambiguous external orders on restore.

CloudWatch and inference artifacts record errors, attempts, evidence IDs, model
usage and latency. Limit source-evidence and log access to the configured team.
