# Steward

**Problems raised in a community group chat get buried in the scroll and quietly die. Steward catches them, remembers how the same problem was solved last time, and drives it to resolution.**

Built with the [Strands Agents SDK](https://strandsagents.com/) for the AWS Agents for Humans Hackathon, Good Neighbor Agents track.

> Status: deployed on AWS with persistent workflows, Cognito sign-in, live Strands /
> Bedrock inference, AgentCore, and verified Telegram group intake and output. The hosted Northgate
> demo uses synthetic community data and simulated vendor email and orders. Controlled SES
> RFQ, vendor reply, order and appointment confirmation passed a real two-address
> mail test with isolated application state. No real supplier or repair was involved.
> See [current readiness](docs/LIVE_READINESS.md) and
> [implementation evidence](IMPLEMENTATION.md).

[Hosted application](https://d35nbywkoth58f.cloudfront.net/) ·
[Architecture](docs/ARCHITECTURE.md) / [diagram PDF](docs/steward-architecture.pdf) · [Verification](docs/FINAL_REVIEW.md)

The hosted application requires a provisioned community account. It does not expose
the owner's management credentials. Use the reproducible local setup below to inspect
the code and product; reviewer access is provisioned separately before sharing the demo.

## Run the product

Use Python 3.12 and Node 22. From the repository root in PowerShell:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -c requirements.lock -e '.[dev]'
npm --prefix web ci
npm --prefix web run build
$env:STEWARD_EXECUTION_MODE = 'dry_run'
$env:STEWARD_SIMULATION = 'true'
$env:STEWARD_SIMULATION_CLOCK_START = '2026-09-09T10:00:00+00:00'
$env:STEWARD_API_TOKENS = '{"local-review-only":{"actor_id":"Simon O.","role":"manager"}}'
.venv/Scripts/steward-bootstrap.exe
.venv/Scripts/steward-server.exe
```

Open `http://127.0.0.1:8000` and sign in with `local-review-only`. This is an
explicit local test identity, unsuitable for deployment. The console uses actual
Bedrock inference; configure an authorized AWS profile and region first.
Use **Simulation studio** to submit resident messages, vendor replies and clock
advances through the same runtime. Its worker action processes pending work.
For continuous operation, run `steward-worker` in another shell with the same
environment. The API itself does not start a worker.

The `steward-demo`, `steward-smoke` and `steward-quote-smoke` commands remain
regression fixtures. The API, worker and web console are the product path.

---

## The problem

Anywhere people share a space, they also share a message thread. A resident writes "A Block elevator is broken again" at 9pm. Three people reply. The next morning the thread has moved on to parking, and the elevator issue is gone. Nobody filed a ticket, because nobody files tickets in a group chat.

Worse, the knowledge is gone too. The same elevator failed eight months ago. Somebody called a service company, negotiated a price, waited four days, and got it fixed. None of that is written down anywhere a person can find it. So the next time, the community starts from zero.

## What Steward does

Steward reads the group chat, and for every real problem it finds:

1. **Opens a case** and deduplicates it against problems already open, so three residents complaining about one elevator produce one case, not three.
2. **Consults category-scoped institutional memory.** What happened to this asset before? Which vendors were used? Who answered fastest? Who was cheapest? Who had to come back twice?
3. **Checks what it is allowed to do.** Management pre-authorizes autonomy per category. Some categories Steward handles end to end. Others it prepares completely and hands to a human.
4. **Carries the work forward.** Within its authority it prepares and dispatches RFQs, records replies, compares quotes against vendor history, and commits to a service call. Transport defaults to dry run; controlled SES delivery requires configuration and live verification.
5. **Refuses to forget.** Open cases stay on Steward's desk. If a vendor said "Tuesday" and it is Thursday, Steward chases. If chasing fails, it escalates.
6. **Writes the outcome back to memory.** Cost, timings, vendor performance, resolution notes. The next similar problem starts from experience instead of from zero.

```
Problem -> Action -> Follow-up -> Resolution -> Memory
                                       |
                                       +--> reused by the next similar problem
```

## Design decisions worth calling out

**The policy engine is not an LLM.** Autonomy limits, spend caps, and vendor allowlists are enforced in deterministic code that cannot be argued with, because the agent's input is a chat message any resident can write. See [`src/steward/policy/engine.py`](src/steward/policy/engine.py).

**Vendor scores are computed, not generated.** "Which vendor responds fastest" is an aggregate query over structured records, not a language model's impression of some text.

**Workflow time is injectable.** The most important behaviour is what happens when
nothing happens for two days. The demo advances persisted workflow time through the
same services. Provider timestamps, account-link expiry and delivery rate limits use
wall time. Scenario messages and delivery receipts remain explicitly simulated.

**Every claim is traceable.** When Steward says a vendor charged a certain amount in March, the statement carries the record it came from.

## Repository layout

```
src/steward/
  domain/      entities, enums, and the Clock abstraction
  policy/      deterministic autonomy and spend policy engine
  mail/        MailTransport interface, address tokens, SES adapter
  agents/      Strands agents (triage, memory, execution, decision, closure)
  store/       persistence
infra/         AWS CDK
web/           management console
tests/
```

## Data and privacy

All bundled Northgate data and evaluation inputs are synthetic: 63 historical
records, 14 assets and 11 vendors. Vendor addresses are scenario data, not proof
of functioning inboxes. Real Telegram messages have reached the deployed worker
and merged into one case. A [controlled SES roundtrip](docs/CONTROLLED_MAIL_TEST.md)
delivered five real emails through the RFQ/order/appointment flow and survived a
runtime restart. It used isolated local state; hosted RFQs/orders remain simulated.
Evaluation results and simulated costs are not a resident field study or measured
time savings. Raw live messages, account mappings and credentials are excluded from
the public source package.

## Reproduce the checks

```powershell
.venv/Scripts/python.exe tools/hackathon_acceptance.py
.venv/Scripts/python.exe -m pytest
npm --prefix web test
npm --prefix web run build
```

The offline acceptance command uses isolated synthetic stores and deterministic model
ports. Historical live model evaluations are documented separately; passing the offline
suite does not claim new model accuracy or real provider delivery.

For publishing source, run `python tools/prepare_source.py` and inspect the generated
manifest. This creates a local source package, never a public repository or submission.

## License

MIT. See [LICENSE](LICENSE).
