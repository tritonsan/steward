# Independent Telegram delivery

2026-09-13. The user authorized enabling Telegram output and verifying delivery to
the existing connected test group. Email and service-order execution stay `dry_run`.

## Implementation

- `STEWARD_TELEGRAM_DELIVERY_MODE` explicitly selects `dry_run` (default) or `live`.
  Neither `live_rfq` nor `live_commitment` implicitly enables Telegram. Live Telegram
  requires the configured bot, while permitting the existing frozen demo clock.
- A notice captures its delivery mode when queued. Old simulated/pending records
  cannot become real sends merely because the operator enables Telegram later.
- Settings shows real/simulated/paused sending and offers one clearly labeled
  connection test per group connection. The command accepts the expected connection
  version, verifies manager membership and queues a fixed message in the existing
  outbox. Repeated requests resolve to the same intent. No artificial case is created.
- The ordinary worker sends the test, persisting Telegram's returned `message_id`.
  Real delivery receipts use wall time. The UI polls status instead of treating
  successful queueing as delivery.
- Dispatch checks the emergency stop, current group/version and requesting manager.
  Existing private-task recipient checks remain in place. Unknown provider outcomes
  are not retried automatically. Disconnecting suppresses pending group sends.

The supported operational notices remain important community status/outcome events,
personal assigned tasks/invitations and the daily digest. This change does not add a
general conversational bot. The digest and workflow reminders still follow the demo
clock; enabling Telegram does not make that clock advance automatically.

## Local verification

- 11 new tests cover independent authority, live API-to-worker delivery, idempotency,
  manager/version controls, dry-run history, timeout ambiguity, disconnect, emergency
  stop, protected configuration hydration, wall-time receipts and runtime restart.
- Relevant Python subset: 58 passed.
- Full regression: 476 passed, 16 PostgreSQL-specific tests skipped in this local run.
- Frontend: 30 tests passed; TypeScript/Vite build and changed-file lint passed.

Evidence: `artifacts/validation/telegram-delivery-tests.xml` and
`artifacts/validation/telegram-delivery-regression.xml`.

## Deployment and provider verification

Release `47f9e2ed64dc586a` has successful API and inference CodeBuild images.
The reviewed CloudFormation update changes only API/worker/inference images, static
console assets and the separate Telegram delivery environment variable for API and
worker. Database, mail authority, policy, demo clock and other resources are preserved.

Live delivery verified at 2026-09-13 12:54:52 UTC (15:54 Istanbul): the ordinary
API queued the test, the hosted worker sent it to the connected Northgate Demo group,
and Telegram returned message ID `7`. The PostgreSQL outbox receipt is `delivered`
with no error. Both services are stable on the new release, and mail remains
`dry_run`. The manager UI displayed the real receipt and wall-time delivery date.
Desktop and 390px mobile layouts were checked; there was no horizontal overflow.
Temporary viewport changes were reset.

Aggregate evidence: `artifacts/validation/cloud-telegram-delivery.json`. Screenshots
remain private under `.scratch/telegram-delivery-review/` because the panel also
contains real incoming group messages.

The explicit operator
command `python tools/cloud_telegram_delivery.py test` authenticates as the existing
manager and invokes the ordinary API. `status` is read-only and writes only aggregate
service/channel status and the provider receipt, never bot credentials or chat text.

To disable Telegram output, deploy with `telegramDeliveryMode=dry_run`; the general
emergency stop also pauses it immediately at dispatch. Previously simulated or
delivery-uncertain records are never automatically replayed.
