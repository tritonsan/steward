# Controlled vendor test mailbox

Address: **test-vendor@steward.narrativenode-labs.cloud**

This is an AWS SES/S3 mailbox for the operator-controlled vendor simulator. It does
not have a webmail login, password, IMAP, or POP access. Incoming messages are stored
privately as original MIME emails in the existing encrypted Steward mail archive.
The verified parent SES identity permits sending from this address.

The `steward-test-vendor-mailbox` rule is before the broader Steward intake rule and
stops further rule processing for this exact recipient. Vendor inbox messages therefore
do not enter Steward's case/review queue. Other recipients and projects are preserved.

```powershell
.venv/Scripts/python.exe tools/cloud_test_mailbox.py list
.venv/Scripts/python.exe tools/cloud_test_mailbox.py read --key "mail/test-vendor/MESSAGE_KEY"
.venv/Scripts/python.exe tools/cloud_test_mailbox.py send --subject "Reply subject" --body-file .scratch/vendor-reply.txt
```

`send` is an explicit operator action from this address to
`management@steward.narrativenode-labs.cloud` only. It does not automatically associate
the sender with a seeded vendor or bypass quote authentication/case reply tokens.
Configure that mapping and use the genuine RFQ reply context before a quote lifecycle
test. The application itself remains in dry-run delivery mode.

The complete controlled mail test passed on 2026-09-11 using the management and
test-vendor identities, a tokenized case reply address, real SES delivery and the
normal application worker. RFQ, quote, order, appointment reply and confirmation
all arrived; the runtime restarted between order and appointment. See
[CONTROLLED_MAIL_TEST](CONTROLLED_MAIL_TEST.md) for evidence and rerun instructions.

`create` is idempotent for the exact existing configuration. `smoke` sends a new
verification email to this same mailbox. It is not retried automatically if the
delivery result is ambiguous. AWS access is required; no new standalone mailbox
subscription or account was created.

The mailbox rule is an operator-managed addition to the shared active SES ruleset.
During teardown remove only `steward-test-vendor-mailbox`; review retained messages
before deleting any data. Re-check rule order after updates to the shared intake rule.
