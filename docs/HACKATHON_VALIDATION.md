# Reproduce the product acceptance checks

Install the development dependencies from the repository root, then run:

```powershell
.venv/Scripts/python.exe -m pip install -c requirements.lock -e '.[dev]'
.venv/Scripts/python.exe -X utf8 tools/hackathon_acceptance.py
```

This command needs no AWS credentials. It removes inherited deployment settings
from its child process, uses isolated SQLite databases, and submits synthetic
resident messages, vendor replies, time advances and authorized human commands
through the public API and worker. Extraction and planning use deterministic test
ports. It does not send email, Telegram messages or service orders externally.

The report is `artifacts/validation/hackathon-acceptance.json`. Each invocation
creates a separate evidence directory under `artifacts/validation/hackathon-runs/`.
The command exits with a nonzero status if a test fails, is skipped, any of the
20 scenarios lacks one of its three repetitions, or Python source changes during
the run. Old scenario files cannot satisfy a new run.

The scenarios include maintenance verification and rejected work, retries and
restarts, incomplete quotes, authorization and budget boundaries, community
decisions, and a confirmed community action leading to verified maintenance.
An appropriate human review is an expected outcome for an unsafe or incomplete
case; successful automation does not mean every case closes automatically.

## What this evidence supports

This is a reproducible functional measurement of the implemented product path.
It is separate from live language-model quality, PostgreSQL verification, browser
review, and actual channel delivery. It does not measure money or time saved in
a real community.

The existing live-model reports measure synthetic inputs:

- `triage-reserve-v2.json`: a reserved 200-message routing, category, asset and
  duplicate-case evaluation, with per-category results and corpus/prompt hashes.
- `quote-portfolios-regression.json`: 90 quote extractions in 30 sets. This
  measures price, currency, timing and source extraction, not complete vendor
  decision quality.
- `memory-counterfactuals.json`: nine controlled decisions comparing no history
  with reversed supplier recurrence histories. This tests sensitivity to the
  supplied history, not real supplier performance.
- `order-reply-live-v2.json` and `clarification-live-v1.json`: development probes
  for newer message types; these are not an independent held-out benchmark.

To deliberately rerun the first three measurements with an authorized AWS profile:

```powershell
.venv/Scripts/python.exe -m steward.evaluation --corpus data/evaluation/triage-reserve-v2.jsonl
.venv/Scripts/python.exe -m steward.quote_evaluation
.venv/Scripts/python.exe -m steward.memory_evaluation
```

These commands make actual paid Bedrock calls. A failed measured gate returns a
nonzero exit status. Historical reports describe the model, prompt and corpus
at their recorded run; a later code change does not retroactively update them.

Use `docs/LIVE_READINESS.md` for deployed-service and channel evidence. A simulated
RFQ or order must remain labeled as simulated in a demo or submission.
