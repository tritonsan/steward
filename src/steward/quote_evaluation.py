"""Measure live quote extraction across 30 synthetic, three-vendor portfolios."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from steward.agents.quote import QUOTE_EXTRACTION_SYSTEM_PROMPT, StrandsQuoteExtractor
from steward.config import StewardSettings


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/validation/quote-portfolios.json")
    args = parser.parse_args(argv)
    settings = StewardSettings()
    extractor = StrandsQuoteExtractor.bedrock(
        model_id=settings.bedrock_model_id,
        profile_name=settings.aws_profile,
        region_name=settings.aws_region,
        max_tokens=1024,
    )
    at = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)
    scopes = [
        "Replace lift guide shoes and test all stops.",
        "Inspect the pump seals and replace the damaged gasket.",
        "Replace the faulty controller and perform a safety test.",
        "Clear the drain and confirm water flows freely.",
        "Replace the damaged light fittings and test the circuit.",
    ]
    probes = []
    for group in range(30):
        for vendor in range(3):
            amount = Decimal(250 + group * 73 + vendor * 137) + Decimal("0.50")
            currency = ["SGD", "USD", "EUR"][group % 3]
            price = f"{amount:,.2f}" if group % 2 else str(amount)
            scope = scopes[group % len(scopes)]
            body = (
                f"Quotation from contractor {vendor + 1}: total {currency} {price}. "
                f"{scope} Parts and labour are included. Additional work is excluded. "
                "We can attend on 2026-09-11 at 09:00 UTC. "
                "This quotation is valid until 2026-09-20 at 17:00 UTC."
            )
            probes.append(
                {
                    "id": f"portfolio-{group + 1:02d}-vendor-{vendor + 1}",
                    "portfolio": group + 1,
                    "body": body,
                    "amount": str(amount),
                    "currency": currency,
                }
            )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def run(probe):
        try:
            result = extractor.extract(
                body_text=probe["body"],
                requested_currency="SGD",
                rfq_sent_at=at,
                received_at=at,
                source_message_id=probe["id"],
            )
            checks = {
                "has_quote": result.has_quote,
                "amount": result.amount == Decimal(probe["amount"]),
                "currency": result.currency == probe["currency"],
                "amount_source": bool(result.amount_evidence)
                and result.amount_evidence in probe["body"],
                "scope_source": bool(result.scope_evidence)
                and result.scope_evidence in probe["body"],
                "onsite": result.earliest_onsite_at
                == datetime(2026, 9, 11, 9, tzinfo=timezone.utc),
                "validity": result.valid_until == datetime(2026, 9, 20, 17, tzinfo=timezone.utc),
            }
            return {
                **probe,
                "checks": checks,
                "passed": all(checks.values()),
                "actual": result.model_dump(mode="json"),
            }
        except Exception as exc:
            return {**probe, "passed": False, "error": type(exc).__name__}

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = [f.result() for f in as_completed([pool.submit(run, p) for p in probes])]
    complete = sum(all(r["passed"] for r in rows if r["portfolio"] == n) for n in range(1, 31))
    report = {
        "live_model_calls": True,
        "simulated_inputs": True,
        "model": settings.bedrock_model_id,
        "prompt_sha256": sha256(QUOTE_EXTRACTION_SYSTEM_PROMPT.encode()).hexdigest(),
        "portfolio_count": 30,
        "quote_count": 90,
        "passing_portfolios": complete,
        "passing_quotes": sum(r["passed"] for r in rows),
        "passed": complete == 30,
        "limitations": [
            "Tests extraction, not decision quality or real supplier performance.",
            "Synthetic bodies share templates; no unreadable attachments included.",
        ],
        "results": sorted(rows, key=lambda r: r["id"]),
    }
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
