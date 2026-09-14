# Steward live readiness

This is the deployment checklist for https://d35nbywkoth58f.cloudfront.net.
Do not equate a configured integration with a completed provider round trip.

Final source audit and submission-preparation handoff:
[FINAL_REVIEW](FINAL_REVIEW.md). The current demo release keeps real inference and
Telegram intake enabled, and application outbound delivery explicitly simulated.

Final release `ecef682d8482ee11` reached `UPDATE_COMPLETE` on 2026-09-10.
Both services run the current image; ten authenticated endpoints returned 200,
anonymous access returned 401, Telegram membership/intake persisted, and four
active suggestions remain without duplicates. Earlier copies are preserved as
inactive history. The redacted result is `artifacts/validation/final-review.json`.

| Check | State | Evidence / next action |
|---|---|---|
| CloudFront HTTPS, API, worker, RDS | Verified; channel update reached UPDATE_COMPLETE | API and worker both running 1, deployment completed; `cloud-smoke.json`, `cloud-restart.json` |
| Cognito manager sign-in and anonymous rejection | Verified | `cloud-smoke.json`, `cloud-hosted-login.json` |
| Cases, tasks, memory, settings, rules, review API | Verified | `cloud-api-audit.json` |
| 63 Northgate history records and embeddings | Verified | `cloud-bootstrap.json`, `cloud-index.json` |
| Real Strands coordinator and procurement specialist on AgentCore | Verified with read-only synthetic evidence | `cloud-agentcore-live.json` |
| Persistent input → case → simulated RFQs; worker restart | Verified | `cloud-runtime.json`, `cloud-restart.json` |
| Telegram bot configuration and HTTPS webhook | Configured and provider URL verified | `cloud-telegram.json` |
| Personal link creation and webhook authentication | Verified | `cloud-telegram-api.json`; 200 / 403 |
| Telegram link expiry in frozen demo time | Fixed and regression tested | Expiry uses wall clock, independently of simulation |
| Personal Telegram identity | Verified through real provider webhook; rechecked after user confirmation | Live API reports `personal_linked=true`; no need to repeat this step |
| Group admin check | Verified | Northgate Demo connected at version 1; provider confirms bot administrator and linked manager creator |
| Real group message processing | Verified 2026-09-10 | Two genuine B Block elevator messages processed and linked to one case (`case-928e66ed63eb4df6a520e763e6cd939a`); 3 simulated RFQs, worker failures=0, provider pending updates=0. `cloud-telegram-current.json` |
| SES identity and durable receipt resources | Prepared | `steward.narrativenode-labs.cloud`; S3 → SNS → SQS |
| Active SES rule without interrupting another project | Prepared | `cloud-mail-routing.json`; only Steward recipient added to existing active ruleset |
| SES domain DNS | Verified 2026-09-10 | 3 DKIM CNAMEs and 1 MX added through Netlify; SES identity and DKIM both SUCCESS; `cloud-mail-dns.json` |
| Controlled vendor test mailbox | Created; real self-addressed delivery verified | `test-vendor@steward.narrativenode-labs.cloud`, private SES/S3 mailbox; `cloud-test-mailbox.json`, `TEST_MAILBOX.md` |
| Controlled real RFQ → quote → order → appointment mail flow | Verified 2026-09-11 | Five real SES emails, two authenticated replies, real models, one order/reservation, runtime restart; isolated local state. `controlled-mail-roundtrip.json`, `CONTROLLED_MAIL_TEST.md` |
| Real PDF attachment delivery and complete hosted channel journey | Not established by the text-email run | Existing local attachment checks are separate; SES remains sandboxed |
| Real outgoing Telegram notifications and vendor emails | Disabled by demo execution mode | Do not silently enable paid orders or outbound sends when configuring ingress |
| Offline maintenance + meeting acceptance | Rechecked 2026-09-10 | 180 checks, 20 scenarios × 3 runs; 33 verified outcomes and 27 expected human reviews. Deterministic model ports; `hackathon-acceptance.json` |
| Current SQLite / PostgreSQL regressions | Rechecked 2026-09-10 | 448 Python passes; 16 lifecycle, 20 Telegram and 3 proactive PostgreSQL checks pass separately |
| Desktop/mobile product actions | Checked locally against current product build | Manager approvals, sources, rejected repair, meeting tasks/outcome, permissions; resident private cards and invitations. `final-ui-review.json` |
| Complete real vendor lifecycle, RDS restore drill, judge accounts | Not established by deployment smoke tests | These remain separate checks; reviewer access is prepared during submission handoff |

Credentials remain in local ignored files and AWS Secrets Manager. Do not include
them in evidence files or submission materials. Current stack uses simulation time
and dry-run outbound delivery. A real-time channel lifecycle needs an explicitly
scoped run; advancing the demo clock is not provider-time verification.

The active SES ruleset is shared with another application. Never switch it to the
Steward-only ruleset: `tools/cloud_mail_route.py` preserves all previous rules and
adds only `steward-northgate-intake`. This shared-rule copy must be reviewed on mail
infrastructure updates and removed explicitly during Steward teardown.
