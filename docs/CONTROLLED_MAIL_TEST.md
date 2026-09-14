# Controlled real mail verification

On 2026-09-11, run `20260911T092642Z-b701d1` passed with **five real emails**.
The two controlled identities were `management@steward.narrativenode-labs.cloud`
and `test-vendor@steward.narrativenode-labs.cloud`. Vendor replies used Steward's
normal tokenized case Reply-To address under the same owned domain.

| Real message | Verified application result |
|---|---|
| Management → test vendor: RFQ | Original MIME arrived in the private SES/S3 vendor mailbox |
| Test vendor → Steward: 705 USD quotation | SES receipt passed DMARC/spam/virus checks; S3 → SNS → SQS → durable inbox/job → actual model extraction stored the quote |
| Management → test vendor: approved service order | Original MIME arrived; case remained `awaiting_appointment`, with one logical order and one budget reservation |
| Test vendor → Steward: dated appointment reply | After a runtime restart, the genuine SES order thread matched the same case; real model extraction and configured access/hours rules accepted the proposal |
| Management → test vendor: appointment confirmation | Confirmation arrived before the case was asserted `scheduled` |

Evidence: [redacted machine-readable report](../artifacts/validation/controlled-mail-roundtrip.json).
Raw MIME, provider IDs, reply tokens and the isolated database remain in ignored
local working state and the private AWS archive.

## Scope and reproducibility

The test used the existing application services with isolated SQLite persistence,
wall-clock time, actual Bedrock model calls and actual AWS SES/S3/SNS/SQS services.
A labelled synthetic resident event entered the normal durable inbox. The worker
created and advanced the case. The public simulation API remained disabled for
live outbound mode. No direct case-status or quote-evidence edits were made.

An operator requested quote comparison immediately and approved the resulting
decision through the authenticated command API. This verifies mail delivery and
the approval/scheduling continuation; it does not measure the automatic 24-hour
comparison delay. A controlled reply supplied explicit working time and unchanged
terms. No actual supplier, physical visit, repair, invoice or payment was involved.

The hosted demo remains in `dry_run` for outgoing messages. This test establishes
that the existing implementation can send, receive and process real mail; it does
not claim the complete Telegram → clarification → hosted ECS mail → repair
verification journey ran in one session. PDF delivery and broad new-message
accuracy were not measured by this text-email test.

```powershell
.venv/Scripts/python.exe -X utf8 tools/cloud_mail_roundtrip.py --run
```

This explicitly sends test emails and requires AWS access to the existing verified
domain, archive, configuration set, shared SES receipt rules and temporary SNS/SQS
resources. The test refuses application sends to any address other than the exact
controlled vendor and labels every outgoing order as a software test requesting no
real service. Its isolated vendor seed does not change hosted vendor mappings.

Each run creates a temporary exact-case receipt route so the hosted worker cannot
consume its replies. Cleanup removes only that route and the new queue/topic, then
asserts the original rules and ordering are restored. Existing Steward and Factor
rules were preserved. Stored mail remains available as private evidence.

## Findings while preparing the run

- The simulation API correctly rejected live-mode event injection. The harness now
  supplies a labelled external event through the normal durable inbox.
- SES creates a setup notification when a receipt destination is configured. Two
  preliminary runs mistakenly treated this probe as the vendor response. The probe
  lacked DMARC evidence and was correctly quarantined. The harness now distinguishes
  its fixed setup ID while leaving authentication checks unchanged. SES documents
  its receipt fields in the [notification reference](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-notifications-contents.html).
- In one preliminary run the real model omitted the inclusions excerpt from an
  otherwise priced quote. The completeness gate blocked the order. The final
  controlled message states inclusions explicitly on their own line. This successful
  run is a narrow integration check, not an extraction reliability benchmark.

No product runtime code or deployed permissions needed changing. Five targeted
test-harness boundary checks and the existing mail-threading/source-release checks
passed together (22 tests).
