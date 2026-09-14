"""Tests over the authored archive.

Two jobs here. The first is ordinary: the JSON loads, references resolve, and
timestamps run forwards. The second matters more. The archive was written to
support a specific argument, that this community kept buying the cheapest
elevator quote and kept paying twice, and these tests assert that the argument
actually survives contact with the arithmetic. If someone edits a price or a
date and the story stops holding, that should fail here rather than surface as a
confident and wrong claim in the demo video.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from steward.domain.enums import ApprovalMode, AutonomyLevel, Category
from steward.mail import RecipientGuard
from steward.memory import build_scorecards, scorecard_table
from steward.policy import PolicyEngine, Rule
from steward.seed import load_seed

COMPUTED_AT = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
CONFIDENT = 0.95


@pytest.fixture(scope="module")
def seed():
    return load_seed()


@pytest.fixture(scope="module")
def scorecards(seed):
    return build_scorecards(
        seed.history,
        computed_at=COMPUTED_AT,
        recurrence_window_days=seed.settings.recurrence_window_days,
    )


@pytest.fixture(scope="module")
def engine(seed):
    return PolicyEngine(seed.policies, seed.settings)


def card(scorecards, vendor_id: str, category: Category = Category.ELEVATOR):
    return scorecards[(vendor_id, category)]


# ----------------------------------------------------------------------
# The archive loads and hangs together
# ----------------------------------------------------------------------


def test_seed_loads(seed):
    assert seed.property_profile.name == "Northgate Residence"
    assert len(seed.blocks) == 4
    assert len(seed.assets) == 14
    assert len(seed.residents) == 14
    assert len(seed.vendors) == 11
    # 33 authored cases plus 30 deterministic non-elevator background cases from
    # the demo-world manifest. The 33 authored records are unchanged; see
    # test_demo_world.py for the enrichment and scorecard-preservation proofs.
    assert len(seed.history) == 63
    assert len(seed.demo_messages) == 10


def test_every_case_references_a_real_asset(seed):
    known = {asset.asset_id for asset in seed.assets}

    for case in seed.history:
        if case.asset_id is not None:
            assert case.asset_id in known, f"{case.case_id} points at unknown asset"


def test_every_quote_and_selection_references_a_real_vendor(seed):
    known = {vendor.vendor_id for vendor in seed.vendors}

    for case in seed.history:
        for quote in case.quotes:
            assert quote.vendor_id in known, f"{case.case_id} quotes unknown vendor"
        if case.selected_vendor_id is not None:
            assert case.selected_vendor_id in known
        for vendor_id in case.vendors_contacted:
            assert vendor_id in known


def test_recurrence_links_are_symmetric_and_resolve(seed):
    by_id = {case.case_id: case for case in seed.history}

    for case in seed.history:
        if case.recurred_as_case_id:
            successor = by_id.get(case.recurred_as_case_id)
            assert successor is not None, f"{case.case_id} points at a missing successor"
            assert successor.recurrence_of_case_id == case.case_id, "link must point back"
            assert successor.opened_at > case.resolved_at, "a recurrence follows the repair"


def test_case_timelines_run_forwards(seed):
    for case in seed.history:
        stages = [
            ("opened", case.opened_at),
            ("rfq", case.rfq_sent_at),
            ("onsite", case.onsite_at),
            ("resolved", case.resolved_at),
            ("closed", case.closed_at),
        ]
        seen = [(name, at) for name, at in stages if at is not None]
        for (earlier_name, earlier), (later_name, later) in zip(seen, seen[1:], strict=False):
            assert earlier <= later, (
                f"{case.case_id}: {later_name} precedes {earlier_name}"
            )


def test_selected_vendor_actually_quoted(seed):
    for case in seed.history:
        if case.selected_vendor_id is None:
            continue
        assert case.quote_from(case.selected_vendor_id) is not None, (
            f"{case.case_id} awarded work to a vendor with no quote on file"
        )


def test_cases_resolved_without_a_vendor_cost_nothing(seed):
    """Administrative resolutions exist and must not look like purchases."""
    admin_cases = [c for c in seed.history if not c.had_vendor]

    assert {c.case_id for c in admin_cases} == {"hist-2025-010", "hist-2026-008"}
    for case in admin_cases:
        assert case.cost == Decimal("0.00")
        assert case.quotes == []


def test_the_vendor_character_sketches_survive_loading(seed):
    """They are the reason the archive reads as a story rather than a table."""
    coastline = seed.vendor("coastline-elevator")

    assert coastline is not None
    assert "cheapest quote" in coastline.notes


# ----------------------------------------------------------------------
# The argument the archive was written to support
# ----------------------------------------------------------------------


def test_coastline_invoices_less_on_average_than_meridian(scorecards):
    """The trap, stated in numbers. On paper Coastline is the cheaper company."""
    assert card(scorecards, "coastline-elevator").avg_cost == Decimal("393.33")
    assert card(scorecards, "meridian-lift").avg_cost == Decimal("533.33")


def test_coastline_repairs_come_back_and_meridian_repairs_do_not(scorecards):
    """The reason the cheaper invoice is not the cheaper choice."""
    coastline = card(scorecards, "coastline-elevator")
    meridian = card(scorecards, "meridian-lift")

    assert coastline.jobs_completed == 3
    assert coastline.repeat_failure_rate == pytest.approx(2 / 3)

    assert meridian.jobs_completed == 6
    assert meridian.repeat_failure_rate == 0.0


def test_response_times_separate_the_two_companies_by_an_order_of_magnitude(scorecards):
    meridian = card(scorecards, "meridian-lift")
    coastline = card(scorecards, "coastline-elevator")

    assert meridian.avg_first_response_hours == pytest.approx(3.822, abs=0.01)
    assert coastline.avg_first_response_hours == pytest.approx(30.0)


def test_response_time_is_learned_from_lost_bids_too(seed, scorecards):
    """Coastline answered nine elevator requests and won three of them.

    A committee comparing two quotes has no way to see this. It is the clearest
    thing the archive knows that no individual decision could have known.
    """
    coastline_quotes = [
        case.case_id
        for case in seed.history
        if case.counts_toward_response_record and case.quote_from("coastline-elevator")
    ]

    assert len(coastline_quotes) == 8
    assert card(scorecards, "coastline-elevator").jobs_completed == 3


def test_pinnacle_has_one_clean_job_and_that_is_not_a_track_record(scorecards):
    pinnacle = card(scorecards, "pinnacle-vertical")

    assert pinnacle.jobs_completed == 1
    assert pinnacle.repeat_failure_rate == 0.0
    assert pinnacle.avg_cost == Decimal("1240.00")
    assert pinnacle.avg_cost > card(scorecards, "meridian-lift").avg_cost


def test_planned_inspections_are_excluded_from_the_track_record(seed, scorecards):
    """Two annual inspections went to Meridian and neither counts as a job."""
    planned = [c for c in seed.history if c.is_planned_maintenance]

    # The four authored elevator/fire inspections are planned and remain so; the
    # deterministic background history may add further planned inspections, so
    # this asserts the authored set is present rather than exhaustive.
    assert {
        "hist-2025-005",
        "hist-2025-009",
        "hist-2026-011",
        "hist-2026-013",
    } <= {c.case_id for c in planned}
    meridian = card(scorecards, "meridian-lift")
    assert "hist-2025-005" not in meridian.source_case_ids
    assert "hist-2026-013" not in meridian.source_case_ids


def test_the_free_recall_does_not_flatter_coastlines_average(seed, scorecards):
    """A company returning to its own failure must not gain a cheap extra job."""
    recall = seed.case("hist-2026-001")

    assert recall is not None
    assert recall.is_recall
    assert recall.cost == Decimal("0.00")
    assert "hist-2026-001" not in card(scorecards, "coastline-elevator").source_case_ids


def test_every_figure_can_be_traced_back_to_cases(scorecards):
    for key, sc in scorecards.items():
        if sc.jobs_completed:
            assert len(sc.source_case_ids) == sc.jobs_completed, key


def test_display_order_puts_the_reliable_company_first(scorecards):
    ordered = [c.vendor_id for c in scorecard_table(scorecards, Category.ELEVATOR)]

    assert ordered.index("coastline-elevator") == len(ordered) - 1
    assert ordered[0] in {"meridian-lift", "pinnacle-vertical"}


# ----------------------------------------------------------------------
# The demo beats, run against the real seeded policies
# ----------------------------------------------------------------------


def test_the_elevator_case_is_pre_authorized(engine):
    decision = engine.evaluate_intake(category=Category.ELEVATOR, triage_confidence=CONFIDENT)

    assert decision.level is AutonomyLevel.AUTONOMOUS
    assert decision.spend_cap == Decimal("1500.00")


def test_both_live_elevator_quotes_are_inside_the_cap(engine, seed):
    """The demo's decision is a judgement, not a budget constraint.

    Worth asserting explicitly: if the expensive quote breached the cap, Steward
    choosing it would be arithmetic rather than reasoning, and the interesting
    part of the demonstration would disappear.
    """
    quotes = {r.vendor_id: r.amount for r in seed.vendor_replies if r.amount is not None}

    for vendor_id in ("meridian-lift", "coastline-elevator"):
        decision = engine.authorize_commitment(
            category=Category.ELEVATOR,
            triage_confidence=CONFIDENT,
            vendor_id=vendor_id,
            vendor_allowlisted=True,
            amount=quotes[vendor_id],
            currency="USD",
        )
        assert decision.level is AutonomyLevel.AUTONOMOUS, vendor_id

    assert quotes["meridian-lift"] > quotes["coastline-elevator"]


def test_the_unconfigured_hvac_category_escalates(engine, seed):
    assert "hvac" in seed.unconfigured_categories
    assert Category.HVAC not in seed.policies

    decision = engine.evaluate_intake(category=Category.HVAC, triage_confidence=CONFIDENT)

    assert decision.level is AutonomyLevel.ESCALATE
    assert decision.rule_id == Rule.POLICY_MISSING


def test_landscaping_prepares_and_waits(engine, seed):
    decision = engine.evaluate_intake(category=Category.LANDSCAPING, triage_confidence=CONFIDENT)

    assert decision.level is AutonomyLevel.PREPARE_ONLY
    assert decision.may_contact_third_parties, "it may still ask Greenline for a price"
    assert not decision.may_commit_spend

    greenline = next(r for r in seed.vendor_replies if r.vendor_id == "greenline-grounds")
    assert greenline.amount is not None
    assert greenline.amount < Decimal("1000.00"), "inside the cap and still not autonomous"


def test_the_pool_case_runs_without_troubling_anyone(engine, seed):
    bluewater = next(r for r in seed.vendor_replies if r.vendor_id == "bluewater-pool")

    decision = engine.authorize_commitment(
        category=Category.POOL,
        triage_confidence=CONFIDENT,
        vendor_id="bluewater-pool",
        vendor_allowlisted=True,
        amount=bluewater.amount,
        currency="USD",
    )

    assert decision.level is AutonomyLevel.AUTONOMOUS


def test_fire_safety_is_never_autonomous_however_routine(engine, seed):
    assert seed.policies[Category.FIRE_SAFETY].mode is ApprovalMode.PREPARE_ONLY

    decision = engine.evaluate_intake(category=Category.FIRE_SAFETY, triage_confidence=CONFIDENT)

    assert decision.level is AutonomyLevel.PREPARE_ONLY


def test_resident_disputes_reach_nobody_outside_the_building(engine):
    decision = engine.evaluate_intake(
        category=Category.MEETING_ADMIN, triage_confidence=CONFIDENT
    )

    assert decision.level is AutonomyLevel.ESCALATE
    assert decision.rule_id == Rule.MODE_ESCALATE_ONLY
    assert not decision.may_contact_third_parties


def test_waste_has_nobody_to_contact_and_says_so(engine, seed):
    assert seed.policies[Category.WASTE].allowed_vendor_ids == []

    decision = engine.evaluate_intake(category=Category.WASTE, triage_confidence=CONFIDENT)

    assert decision.level is AutonomyLevel.PREPARE_ONLY


def test_the_delisted_vendor_cannot_be_committed_to(engine, seed):
    """Thornfield's billing dispute is enforced, not merely remembered."""
    thornfield = seed.vendor("thornfield-landscaping")
    assert thornfield is not None and not thornfield.allowlisted

    decision = engine.authorize_commitment(
        category=Category.ELEVATOR,
        triage_confidence=CONFIDENT,
        vendor_id="thornfield-landscaping",
        vendor_allowlisted=thornfield.allowlisted,
        amount=Decimal("400.00"),
        currency="USD",
    )

    assert decision.level is AutonomyLevel.PREPARE_ONLY
    assert decision.rule_id == Rule.VENDOR_NOT_ALLOWLISTED


def test_an_allowlisted_vendor_from_another_trade_is_still_refused(engine, seed):
    greenline = seed.vendor("greenline-grounds")
    assert greenline is not None and greenline.allowlisted

    decision = engine.authorize_commitment(
        category=Category.ELEVATOR,
        triage_confidence=CONFIDENT,
        vendor_id="greenline-grounds",
        vendor_allowlisted=True,
        amount=Decimal("400.00"),
        currency="USD",
    )

    assert decision.rule_id == Rule.VENDOR_NOT_IN_CATEGORY_LIST


# ----------------------------------------------------------------------
# The injection attempt
# ----------------------------------------------------------------------


def test_the_seeded_injection_attempt_is_present_and_hostile(seed):
    injection = next(m for m in seed.demo_messages if m.demo_beat == "injection_defence")

    assert "ignore all previous instructions" in injection.message.text.lower()
    assert "50000" in injection.message.text


def test_the_injections_amount_is_refused_by_the_cap(engine):
    decision = engine.authorize_commitment(
        category=Category.ELEVATOR,
        triage_confidence=CONFIDENT,
        vendor_id="meridian-lift",
        vendor_allowlisted=True,
        amount=Decimal("50000.00"),
        currency="USD",
    )

    assert decision.level is not AutonomyLevel.AUTONOMOUS
    assert decision.rule_id == Rule.OVER_PER_INCIDENT_CAP


def test_the_injections_recipient_is_refused_by_the_transport(seed):
    """The floor that holds even if every layer above it were convinced."""
    guard = RecipientGuard.of(
        "vendors.narrativenode-labs.cloud", "site.narrativenode-labs.cloud"
    )

    assert not guard.is_allowed("billing@quickfix-facilities-now.example")
    for vendor in seed.vendors:
        assert guard.is_allowed(vendor.email), vendor.vendor_id


def test_no_seeded_message_can_reach_the_policy_engine_as_authority():
    """Stated as a structural fact rather than a behavioural hope.

    The engine's inputs are a category, a confidence number, a vendor id, an
    amount, and a currency. There is no parameter through which message text
    could travel, so no wording can widen authority.
    """
    import inspect

    for method in (PolicyEngine.evaluate_intake, PolicyEngine.authorize_commitment):
        params = set(inspect.signature(method).parameters) - {"self"}
        assert params <= {
            "category",
            "triage_confidence",
            "month_to_date_spend",
            "vendor_id",
            "vendor_allowlisted",
            "amount",
            "currency",
        }, f"{method.__name__} grew an input that could carry free text"
