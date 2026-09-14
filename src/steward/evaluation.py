"""Versioned English evaluation. Expected labels are never passed to the classifier."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from steward.agents import StrandsTriageClassifier
from steward.agents.triage import TRIAGE_SYSTEM_PROMPT
from steward.config import StewardSettings
from steward.domain.enums import Category, Urgency, group_of
from steward.domain.models import Case, ResidentMessage
from steward.seed import load_seed


def evaluate(corpus, output, *, workers=4):
    settings = StewardSettings()
    bundle = load_seed()
    corpus_bytes = Path(corpus).read_bytes()
    probes = [json.loads(line) for line in corpus_bytes.decode().splitlines() if line.strip()]
    if not probes:
        raise ValueError("evaluation corpus must contain at least one probe")
    if workers < 1:
        raise ValueError("workers must be a positive integer")
    if len({p["id"] for p in probes}) != len(probes):
        raise ValueError("duplicate evaluation probe id")
    classifier = StrandsTriageClassifier.bedrock(
        settings.bedrock_model_id,
        region_name=settings.aws_region,
        profile_name=settings.aws_profile,
    )
    now = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)

    def run(probe):
        started = perf_counter()
        # Only these public scenario facts cross the model boundary.
        message = ResidentMessage(
            message_id="eval:" + probe["id"],
            source="evaluation",
            chat_id="northgate",
            sender_display="James D.",
            text=probe["text"],
            sent_at=now,
            ingested_at=now,
        )
        existing = []
        for context in probe.get("open_cases", []):
            category = Category(context["category"])
            existing.append(
                Case(
                    case_id=context["case_id"],
                    reply_token="eval-reply",
                    title=context["title"],
                    category=category,
                    group=group_of(category),
                    urgency=Urgency.NORMAL,
                    asset_id=context.get("asset_id"),
                    opened_at=now,
                    updated_at=now,
                )
            )
        try:
            result = classifier.classify(
                message=message, assets=bundle.assets, open_cases=tuple(existing)
            )
            expected = probe["expected"]
            checks = {
                "routing": result.action.value == expected["action"],
                "category": result.category.value in expected["categories"],
                "asset": expected.get("asset_id") == result.asset_id
                if expected["action"] != "ignore"
                else True,
                "merge": expected.get("duplicate_case_id") == result.duplicate_case_id,
            }
            return {
                "id": probe["id"],
                "group": probe["group"],
                "expected": expected,
                "actual": result.model_dump(mode="json"),
                "checks": checks,
                "passed": all(checks.values()),
                "latency_ms": round((perf_counter() - started) * 1000),
            }
        except Exception as exc:
            return {
                "id": probe["id"],
                "group": probe["group"],
                "passed": False,
                "error": type(exc).__name__,
            }

    results = []
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (
        path.with_suffix(".jsonl").open("w", encoding="utf-8") as stream,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        for future in as_completed([pool.submit(run, p) for p in probes]):
            result = future.result()
            results.append(result)
            stream.write(json.dumps(result) + "\n")
            stream.flush()
            if len(results) % 20 == 0:
                print(f"Evaluated {len(results)}/{len(probes)}", flush=True)
    groups = defaultdict(list)
    for row in results:
        groups[row["group"]].append(row)
    summary = {
        "corpus_sha256": sha256(corpus_bytes).hexdigest(),
        "prompt_sha256": sha256(TRIAGE_SYSTEM_PROMPT.encode()).hexdigest(),
        "model": settings.bedrock_model_id,
        "live_model_calls": True,
        "simulated_inputs": True,
        "sample_count": len(results),
        "accuracy": sum(r["passed"] for r in results) / len(results),
        "per_category": {
            k: {"count": len(v), "accuracy": sum(r["passed"] for r in v) / len(v)}
            for k, v in groups.items()
        },
        "acceptance_target": 0.95,
        "passed": len(results) >= 200 and sum(r["passed"] for r in results) / len(results) >= 0.95,
        "limitations": [
            "Synthetic English inputs; linguistic templates are shared within groups.",
            "Not a field study or independent human usability assessment.",
        ],
        "failures": [r for r in results if not r["passed"]],
    }
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="data/evaluation/triage-v1.jsonl")
    parser.add_argument("--output", default="artifacts/validation/triage-v1.json")
    args = parser.parse_args(argv)
    result = evaluate(args.corpus, args.output)
    print(json.dumps({k: result[k] for k in ("sample_count", "accuracy", "passed")}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
