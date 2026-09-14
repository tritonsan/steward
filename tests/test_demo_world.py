"""Tests over the Stage 6 demo-world layer.

Two obligations sit on this layer. It has to make the loaded world large and
long enough to read as a real archive, with stable ids and correspondence. And
it has to do that without moving a single number in the authored elevator
argument, because that argument is the point of the demo and these tests are the
thing standing between an edit and a confident, wrong claim on screen.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from steward.domain.enums import Category
from steward.memory import build_scorecards
from steward.seed import (
    DemoWorldError,
    enrich_case,
    enrich_history,
    load_seed,
    validate_demo_world,
)
from steward.seed.loader import default_seed_dir
from steward.seed.preflight import demo_world_summary
from steward.seed.preflight import main as preflight_main
from steward.seed.world import (
    background_thread_docs,
    generate_background_cases,
    load_manifest,
    quote_id_for,
    source_message_id_for,
)

COMPUTED_AT = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)

# The exact authored elevator scorecard numbers, copied from the authored-only
# archive. These must not move once the demo world is assembled.
AUTHORED_ELEVATOR = {
    "coastline-elevator": {"jobs": 3, "avg_cost": Decimal("393.33"), "failure": 2 / 3},
    "meridian-lift": {"jobs": 6, "avg_cost": Decimal("533.33"), "failure": 0.0},
    "pinnacle-vertical": {"jobs": 1, "avg_cost": Decimal("1240.00"), "failure": 0.0},
}


@pytest.fixture(scope="module")
def full():
    return load_seed()


@pytest.fixture(scope="module")
def authored_only():
    return load_seed(include_demo_world=False, validate=False)


@pytest.fixture(scope="module")
def manifest():
    return load_manifest(default_seed_dir())


# ----------------------------------------------------------------------
# Stable counts and ids
# ----------------------------------------------------------------------


def test_authored_only_archive_still_has_exactly_33_cases(authored_only):
    assert len(authored_only.history) == 33


def test_full_world_has_at_least_60_cases_spanning_two_years(full):
    assert len(full.history) >= 60
    opened = [c.opened_at for c in full.history]
    assert (max(opened) - min(opened)).days >= 24 * 30


def test_full_world_has_at_least_15_threads(full):
    assert len(full.threads) >= 15


def test_background_case_ids_are_stable_and_prefixed(full):
    background = [c for c in full.history if c.case_id.startswith("hist-bg-")]
    assert background, "expected background cases"
    # Loading twice yields identical ids in identical order.
    again = [c.case_id for c in load_seed().history if c.case_id.startswith("hist-bg-")]
    assert [c.case_id for c in background] == again


def test_quote_ids_are_unique_across_the_whole_world(full):
    ids = [q.quote_id for c in full.history for q in c.quotes]
    assert all(ids)
    assert len(ids) == len(set(ids))


def test_every_history_record_is_visibly_synthetic(full):
    assert all(case.is_simulated for case in full.history)



def test_id_helpers_are_pure_functions():
    assert quote_id_for("hist-2025-002", "coastline-elevator") == (
        "q-hist-2025-002-coastline-elevator"
    )
    first = source_message_id_for("hist-2025-002", "coastline-elevator")
    second = source_message_id_for("hist-2025-002", "coastline-elevator")
    assert first == second
    assert first.endswith("@vendors.narrativenode-labs.cloud>")


# ----------------------------------------------------------------------
# Enrichment: completeness and idempotency
# ----------------------------------------------------------------------


def test_every_loaded_quote_has_an_id_and_a_message_id(full):
    for case in full.history:
        for quote in case.quotes:
            assert quote.quote_id
            assert quote.source_email_message_id
            assert quote.source_email_message_id.endswith(
                "@vendors.narrativenode-labs.cloud>"
            )


def test_selected_resolved_and_verified_cases_carry_sources(full):
    for case in full.history:
        if case.selected_vendor_id is not None:
            assert case.selection_source_ids
        if case.resolved_at is not None:
            assert case.resolution_source_ids
            assert case.outcome_verified is True
            assert case.verification_source_ids


def test_enrichment_is_idempotent(authored_only):
    once = enrich_history(authored_only.history)
    twice = enrich_history(once)
    assert [c.model_dump() for c in once] == [c.model_dump() for c in twice]


def test_enrichment_does_not_invent_new_quotes_or_change_costs(authored_only):
    for original in authored_only.history:
        enriched = enrich_case(original)
        assert len(enriched.quotes) == len(original.quotes)
        assert enriched.cost == original.cost
        assert enriched.selected_vendor_id == original.selected_vendor_id
        assert enriched.recurred_as_case_id == original.recurred_as_case_id
        assert enriched.resolved_at == original.resolved_at
        for before, after in zip(original.quotes, enriched.quotes, strict=True):
            assert after.amount == before.amount
            assert after.first_response_at == before.first_response_at


def test_enrichment_preserves_a_human_supplied_id(authored_only):
    case = authored_only.history[0]
    quote = case.quotes[0]
    kept = quote.model_copy(
        update={"quote_id": "human-id", "source_email_message_id": "<kept@x>"}
    )
    seeded = case.model_copy(update={"quotes": [kept]})
    enriched = enrich_case(seeded)
    assert enriched.quotes[0].quote_id == "human-id"
    assert enriched.quotes[0].source_email_message_id == "<kept@x>"


# ----------------------------------------------------------------------
# The elevator scorecards are exactly unchanged
# ----------------------------------------------------------------------


def _elevator_cards(history):
    cards = build_scorecards(
        history, computed_at=COMPUTED_AT, recurrence_window_days=90
    )
    return {vid: cards[(vid, Category.ELEVATOR)] for vid in AUTHORED_ELEVATOR}


def test_elevator_scorecards_are_identical_with_and_without_background(full, authored_only):
    before = _elevator_cards(authored_only.history)
    after = _elevator_cards(full.history)
    for vendor_id, expected in AUTHORED_ELEVATOR.items():
        assert after[vendor_id].jobs_completed == expected["jobs"], vendor_id
        assert after[vendor_id].avg_cost == expected["avg_cost"], vendor_id
        assert after[vendor_id].source_case_ids == before[vendor_id].source_case_ids
        assert after[vendor_id].repeat_failure_rate == pytest.approx(expected["failure"])


def test_background_cases_are_never_elevator(full):
    for case in full.history:
        if case.case_id.startswith("hist-bg-"):
            assert case.category is not Category.ELEVATOR


# ----------------------------------------------------------------------
# Curated threads preserved verbatim
# ----------------------------------------------------------------------


def test_the_three_curated_threads_survive_verbatim(full, authored_only):
    curated = {"hist-2025-017", "hist-2025-018", "hist-2026-001"}
    assert curated <= set(full.threads)
    for case_id in curated:
        # The curated thread is byte-identical to the authored-only load.
        assert full.threads[case_id].model_dump() == authored_only.threads[
            case_id
        ].model_dump()


def test_synthesized_threads_are_deterministic_and_chronological(manifest):
    docs = background_thread_docs(manifest)
    again = background_thread_docs(manifest)
    assert docs == again
    opened = [doc["messages"][0]["sent_at"] for doc in docs]
    assert opened == sorted(opened)


# ----------------------------------------------------------------------
# The validator, positively and negatively
# ----------------------------------------------------------------------


def test_preflight_reports_the_valid_source_traced_world(capsys):
    summary = demo_world_summary()
    assert summary["valid"] is True
    assert summary["history_records"] == 63
    assert summary["verified_records"] == 63
    assert summary["source_traced_records"] == 63
    assert summary["simulated_records"] == 63
    assert summary["threads"] == 17

    assert preflight_main(["--json"]) == 0
    assert '"valid": true' in capsys.readouterr().out



def test_validate_accepts_the_assembled_world(full):
    assert validate_demo_world(full) is full


def test_validator_rejects_a_missing_selected_quote(full):
    victim = next(c for c in full.history if c.selected_vendor_id is not None)
    broken_case = victim.model_copy(update={"quotes": []})
    broken = _replace_case(full, broken_case)
    with pytest.raises(DemoWorldError):
        validate_demo_world(broken)


def test_validator_rejects_out_of_order_chronology(full):
    victim = next(c for c in full.history if c.resolved_at and c.rfq_sent_at)
    broken_case = victim.model_copy(update={"closed_at": victim.opened_at})
    broken = _replace_case(full, broken_case)
    with pytest.raises(DemoWorldError):
        validate_demo_world(broken)


def test_validator_rejects_an_unsafe_email_domain(full):
    victim_id = next(iter(full.threads))
    thread = full.threads[victim_id]
    bad_message = thread.messages[0].model_copy(
        update={"to": ["attacker@evil.example"]}
    )
    bad_thread = thread.model_copy(update={"messages": [bad_message, *thread.messages[1:]]})
    broken_threads = dict(full.threads)
    broken_threads[victim_id] = bad_thread
    broken = _replace_bundle(full, threads=broken_threads)
    with pytest.raises(DemoWorldError):
        validate_demo_world(broken)


def test_validator_rejects_a_short_history(full):
    broken = _replace_bundle(full, history=full.history[:10])
    with pytest.raises(DemoWorldError):
        validate_demo_world(broken)


def test_validator_rejects_a_broken_recurrence_link(full):
    victim = next(c for c in full.history if c.recurred_as_case_id is not None)
    broken_case = victim.model_copy(update={"recurred_as_case_id": "hist-does-not-exist"})
    broken = _replace_case(full, broken_case)
    with pytest.raises(DemoWorldError):
        validate_demo_world(broken)


# ----------------------------------------------------------------------
# Generation determinism
# ----------------------------------------------------------------------


def test_generate_background_cases_is_deterministic(manifest):
    first = generate_background_cases(manifest)
    second = generate_background_cases(manifest)
    assert [c.model_dump() for c in first] == [c.model_dump() for c in second]


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _replace_case(bundle, new_case):
    history = tuple(new_case if c.case_id == new_case.case_id else c for c in bundle.history)
    return _replace_bundle(bundle, history=history)


def _replace_bundle(bundle, **overrides):
    import dataclasses

    return dataclasses.replace(bundle, **overrides)
