"""Live decision counterfactuals with controlled synthetic supplier histories."""

import argparse
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from steward.agents.decision import StrandsQuoteRecommender
from steward.config import StewardSettings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/validation/memory-counterfactuals.json")
    args = parser.parse_args(argv)
    settings = StewardSettings()
    model = StrandsQuoteRecommender.bedrock(
        settings.bedrock_model_id,
        profile_name=settings.aws_profile,
        region_name=settings.aws_region,
        max_tokens=1280,
    )
    base = {
        "case": {
            "title": "Recurring elevator vibration",
            "asset_id": "elevator-a",
            "category": "elevator",
        },
        "offered_quote_ids": ["quote-a", "quote-b"],
        "allowed_source_ids": ["quote-a", "quote-b"],
        "quotes": [],
        "history_cases": [],
        "vendor_history": [],
        "nonresponding_vendor_ids": [],
    }
    for identifier, name, amount in [
        ("a", "Harbor Maintenance", "500"),
        ("b", "Orchard Maintenance", "650"),
    ]:
        base["quotes"].append(
            {
                "quote_artifact_id": "quote-" + identifier,
                "vendor_name": name,
                "vendor_allowlisted": True,
                "quote": {
                    "quote_id": "quote-" + identifier,
                    "vendor_id": "vendor-" + identifier,
                    "amount": amount,
                    "currency": "USD",
                    "scope": "Replace guide shoes and correct rail alignment; "
                    "parts and labour included.",
                    "earliest_onsite_at": "2026-09-11T09:00:00Z",
                    "valid_until": "2026-09-20T17:00:00Z",
                },
            }
        )
    runs = []
    for mode in ("without_memory", "history_a_repeats", "history_b_repeats"):
        context = copy.deepcopy(base)
        if mode != "without_memory":
            for identifier in ("a", "b"):
                failures = 18 if mode == f"history_{identifier}_repeats" else 0
                sources = []
                for index in range(20):
                    source = f"history-{identifier}-{index + 1}"
                    sources.append(source)
                    context["allowed_source_ids"].append(source)
                    context["history_cases"].append(
                        {
                            "case_id": source,
                            "selected_vendor_id": "vendor-" + identifier,
                            "outcome_verified": True,
                            "work_performed": "Replaced guide shoes and corrected rail alignment.",
                            "resolution_notes": "Same fault returned within 30 days."
                            if index < failures
                            else "No recurrence in 180 days of follow-up.",
                            "recurred_as_case_id": f"repeat-{source}" if index < failures else None,
                            "source_ids": [source],
                        }
                    )
                context["vendor_history"].append(
                    {
                        "vendor_id": "vendor-" + identifier,
                        "jobs_completed": 20,
                        "repeat_failure_rate": failures / 20,
                        "avg_first_response_hours": 4,
                        "avg_hours_to_onsite": 24,
                        "avg_hours_to_resolution": 30,
                        "currency": "USD",
                        "source_case_ids": sources,
                    }
                )
        for repeat in range(1, 4):
            runs.append((mode, repeat, context))

    def run(item):
        mode, repeat, context = item
        try:
            result = model.recommend(portfolio_id=f"memory-probe:{mode}:{repeat}", context=context)
            valid = result.recommended_quote_id in context["offered_quote_ids"] and set(
                result.source_ids
            ) <= set(context["allowed_source_ids"])
            expected = {"history_a_repeats": "quote-b", "history_b_repeats": "quote-a"}.get(mode)
            return {
                "mode": mode,
                "repeat": repeat,
                "recommendation": result.model_dump(mode="json"),
                "sources_valid": valid,
                "history_sensitive": expected is None or result.recommended_quote_id == expected,
            }
        except Exception as exc:
            return {
                "mode": mode,
                "repeat": repeat,
                "error": type(exc).__name__,
                "sources_valid": False,
                "history_sensitive": False,
            }

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, runs))
    report = {
        "live_model_calls": True,
        "simulated_histories": True,
        "model": settings.bedrock_model_id,
        "passed": all(r["sources_valid"] and r["history_sensitive"] for r in results),
        "limitations": [
            "Controlled counterfactual; not a real vendor ranking or an impact pilot.",
            "Identical scope and availability isolate the effect of price and verified recurrence.",
        ],
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "runs": len(results),
                "selections": [
                    r.get("recommendation", {}).get("recommended_quote_id") for r in results
                ],
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
