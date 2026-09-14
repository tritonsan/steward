"""Validate and summarize the deterministic Northgate demo world."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from steward.seed import load_seed, validate_demo_world

__all__ = ["demo_world_summary", "main"]


def demo_world_summary(seed_dir: str | Path | None = None) -> dict[str, Any]:
    bundle = load_seed(seed_dir)
    validate_demo_world(bundle)
    opened = [case.opened_at for case in bundle.history]
    categories = Counter(case.category.value for case in bundle.history)
    selected_vendors = Counter(
        case.selected_vendor_id for case in bundle.history if case.selected_vendor_id is not None
    )
    return {
        "valid": True,
        "property_id": bundle.property_profile.property_id,
        "reference_date": str(bundle.reference_date),
        "history_records": len(bundle.history),
        "history_start": min(opened).isoformat(),
        "history_end": max(opened).isoformat(),
        "history_span_days": (max(opened) - min(opened)).days,
        "threads": len(bundle.threads),
        "quotes": sum(len(case.quotes) for case in bundle.history),
        "verified_records": sum(case.outcome_verified for case in bundle.history),
        "source_traced_records": sum(
            bool(case.resolution_source_ids and case.verification_source_ids)
            for case in bundle.history
        ),
        "simulated_records": sum(case.is_simulated for case in bundle.history),
        "categories": dict(sorted(categories.items())),
        "selected_vendors": dict(sorted(selected_vendors.items())),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-dir", help="override data/seed")
    parser.add_argument("--json", action="store_true", help="emit one JSON object")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = demo_world_summary(args.seed_dir)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through CLI tests
    raise SystemExit(main())
