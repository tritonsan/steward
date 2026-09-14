# Remaining product work

Updated 2026-09-13 after real Telegram group delivery verification. Current evidence and fixed
defects are in [FINAL_REVIEW](FINAL_REVIEW.md).

Management can now link its Telegram account and choose a group from Settings.
The API/worker path passes automated checks; the real bot/group intake is verified.
Real group output is also verified through the hosted API/outbox/worker, with a
Telegram provider receipt. See [TELEGRAM_DELIVERY_REVIEW](TELEGRAM_DELIVERY_REVIEW.md).
See [LIVE_READINESS](LIVE_READINESS.md).

The resident product is **community cases + invited meetings**. Meeting availability
is a small contextual action. Management owns the operational workflow. Do not
grow the resident screen into a dashboard of tasks, budgets or agent decisions.

| Priority | Work | Status / exit gate |
|---|---|---|
| Submission preparation | Publish reviewed source, supply reviewer access, architecture and a video up to five minutes | Source packaging and architecture are prepared locally. Public repository, submission text, video and reviewer credentials are the next handoff, not completed submissions. |
| Production acceptance | Complete hosted channel journey | Controlled RFQ/reply/order/appointment mail passed with five real SES emails, actual models and a local runtime restart. Hosted Telegram intake and group delivery are real and verified. Hosted supplier mail stays simulated. The combined Telegram/clarification/hosted ECS/repair-verification session remains separate. See `CONTROLLED_MAIL_TEST.md` and `TELEGRAM_DELIVERY_REVIEW.md`. |
| Broader acceptance | Independent usability and exhaustive fault testing | Browser workflows and API/worker boundary tests exist. Independent users, full screen-reader certification and every crash boundary are not established. |
| Cloud operations | Rehearse recovery | Deployment, Docker/ARM64, Cognito, AgentCore/ECS/RDS and worker replacement are verified. RDS PITR and previous-image rollback remain open. |
| 3 | Simulation and impact proof | Matched manual / memoryless / full-Steward comparisons and a complete isolated actor/replay world remain open. No measured 50% saving is claimed. |

The four-stage code extension is described in [STAGES_1_4.md](STAGES_1_4.md).
It adds personal clarification and reassessment, complete-quote gates and review
commands, vendor-backed appointments, bounded scheduling rounds, additive decision
revisions, audience-specific notices, private Telegram linking and deferred mail
processing. The controlled mail portion passed on 2026-09-11. Its complete combined
Telegram-to-verified-repair acceptance gate has not passed in one provider session.

The existing 60 scenario runs and live model evaluations are useful evidence, but
do not establish 50% savings, field impact, exhaustive fault tolerance or a 5/5 score.
The implementation and evidence inventory is [IMPLEMENTATION.md](../IMPLEMENTATION.md).

Submission text/video, real payments, multiple properties, WhatsApp and audio
meeting transcription remain outside this release. A separate operations UI for
residents is not remaining work.
