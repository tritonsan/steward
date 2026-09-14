"""Tests for the policy engine.

These are the tests that matter most in the repository. Everything else decides
how well Steward works; this decides whether it can be talked into spending a
community's money.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from steward.domain.enums import ApprovalMode, AutonomyLevel, Category
from steward.domain.models import GlobalSettings
from steward.policy import PolicyEngine, Rule
from tests.conftest import make_policy

CONFIDENT = 0.95


def engine_with(policy=None, settings: GlobalSettings | None = None) -> PolicyEngine:
    policy = policy if policy is not None else make_policy()
    return PolicyEngine({policy.category: policy}, settings)


# ----------------------------------------------------------------------
# Intake
# ----------------------------------------------------------------------


def test_happy_path_grants_autonomy_bounded_by_the_per_incident_cap():
    decision = engine_with().evaluate_intake(
        category=Category.ELEVATOR, triage_confidence=CONFIDENT
    )

    assert decision.level is AutonomyLevel.AUTONOMOUS
    assert decision.rule_id == Rule.INTAKE_WITHIN_POLICY
    assert decision.spend_cap == Decimal("1500.00")
    assert decision.allowed_vendor_ids == ("acme-elevator",)
    assert decision.may_contact_third_parties
    assert decision.may_commit_spend


def test_kill_switch_overrides_an_otherwise_fully_authorized_category():
    engine = engine_with(settings=GlobalSettings(kill_switch=True))

    decision = engine.evaluate_intake(category=Category.ELEVATOR, triage_confidence=CONFIDENT)

    assert decision.level is AutonomyLevel.ESCALATE
    assert decision.rule_id == Rule.KILL_SWITCH
    assert not decision.may_contact_third_parties


def test_unconfigured_category_escalates_rather_than_defaulting_to_action():
    engine = engine_with()

    decision = engine.evaluate_intake(category=Category.PLUMBING, triage_confidence=CONFIDENT)

    assert decision.level is AutonomyLevel.ESCALATE
    assert decision.rule_id == Rule.POLICY_MISSING


def test_escalate_only_category_may_not_contact_anyone():
    engine = engine_with(make_policy(mode=ApprovalMode.ESCALATE_ONLY))

    decision = engine.evaluate_intake(category=Category.ELEVATOR, triage_confidence=CONFIDENT)

    assert decision.level is AutonomyLevel.ESCALATE
    assert decision.rule_id == Rule.MODE_ESCALATE_ONLY
    assert not decision.may_contact_third_parties


def test_prepare_only_category_may_ask_for_quotes_but_not_commit():
    engine = engine_with(make_policy(mode=ApprovalMode.PREPARE_ONLY))

    decision = engine.evaluate_intake(category=Category.ELEVATOR, triage_confidence=CONFIDENT)

    assert decision.level is AutonomyLevel.PREPARE_ONLY
    assert decision.rule_id == Rule.MODE_PREPARE_ONLY
    assert decision.may_contact_third_parties, "requesting a quote creates no obligation"
    assert not decision.may_commit_spend, "accepting one does"


def test_low_triage_confidence_withholds_the_categorys_permissions():
    engine = engine_with(settings=GlobalSettings(min_triage_confidence=0.7))

    decision = engine.evaluate_intake(category=Category.ELEVATOR, triage_confidence=0.42)

    assert decision.level is AutonomyLevel.ESCALATE
    assert decision.rule_id == Rule.LOW_CONFIDENCE


def test_empty_vendor_allowlist_blocks_autonomy_even_when_always_approve():
    engine = engine_with(make_policy(allowed_vendor_ids=[]))

    decision = engine.evaluate_intake(category=Category.ELEVATOR, triage_confidence=CONFIDENT)

    assert decision.level is AutonomyLevel.PREPARE_ONLY
    assert decision.rule_id == Rule.NO_ALLOWED_VENDORS


def test_missing_per_incident_cap_is_no_authority_not_unlimited_authority():
    engine = engine_with(make_policy(per_incident_cap=None))

    decision = engine.evaluate_intake(category=Category.ELEVATOR, triage_confidence=CONFIDENT)

    assert decision.level is AutonomyLevel.PREPARE_ONLY
    assert decision.rule_id == Rule.CAP_UNCONFIGURED


def test_exhausted_monthly_budget_stops_further_autonomous_commitments():
    engine = engine_with()

    decision = engine.evaluate_intake(
        category=Category.ELEVATOR,
        triage_confidence=CONFIDENT,
        month_to_date_spend=Decimal("4000.00"),
    )

    assert decision.level is AutonomyLevel.PREPARE_ONLY
    assert decision.rule_id == Rule.MONTHLY_EXHAUSTED


def test_remaining_monthly_budget_narrows_the_effective_incident_cap():
    engine = engine_with()

    decision = engine.evaluate_intake(
        category=Category.ELEVATOR,
        triage_confidence=CONFIDENT,
        month_to_date_spend=Decimal("3200.00"),
    )

    assert decision.level is AutonomyLevel.AUTONOMOUS
    assert decision.spend_cap == Decimal("800.00"), "4000 monthly cap minus 3200 spent"
    assert decision.constraints, "the narrowing is explained, not silent"


def test_missing_monthly_cap_is_allowed_and_leaves_the_incident_cap_intact():
    engine = engine_with(make_policy(monthly_cap=None))

    decision = engine.evaluate_intake(
        category=Category.ELEVATOR,
        triage_confidence=CONFIDENT,
        month_to_date_spend=Decimal("99999.00"),
    )

    assert decision.level is AutonomyLevel.AUTONOMOUS
    assert decision.spend_cap == Decimal("1500.00")


# ----------------------------------------------------------------------
# Commitment
# ----------------------------------------------------------------------


def commit(engine: PolicyEngine, **overrides):
    kwargs = {
        "category": Category.ELEVATOR,
        "triage_confidence": CONFIDENT,
        "vendor_id": "acme-elevator",
        "vendor_allowlisted": True,
        "amount": Decimal("900.00"),
        "currency": "USD",
        "month_to_date_spend": Decimal("0"),
    }
    kwargs.update(overrides)
    return engine.authorize_commitment(**kwargs)


def test_commitment_within_all_caps_is_authorized():
    decision = commit(engine_with())

    assert decision.level is AutonomyLevel.AUTONOMOUS
    assert decision.rule_id == Rule.COMMIT_WITHIN_POLICY
    assert decision.may_commit_spend


def test_commitment_above_the_incident_cap_falls_back_to_human_approval():
    decision = commit(engine_with(), amount=Decimal("1500.01"))

    assert decision.level is AutonomyLevel.PREPARE_ONLY
    assert decision.rule_id == Rule.OVER_PER_INCIDENT_CAP


def test_commitment_that_would_breach_the_monthly_cap_falls_back():
    decision = commit(
        engine_with(), amount=Decimal("1000.00"), month_to_date_spend=Decimal("3500.00")
    )

    assert decision.level is AutonomyLevel.PREPARE_ONLY
    assert decision.rule_id == Rule.OVER_MONTHLY_CAP


def test_vendor_missing_the_global_allowlist_flag_cannot_be_committed_to():
    decision = commit(engine_with(), vendor_allowlisted=False)

    assert decision.level is AutonomyLevel.PREPARE_ONLY
    assert decision.rule_id == Rule.VENDOR_NOT_ALLOWLISTED


def test_vendor_allowlisted_globally_but_not_for_this_category_cannot_be_committed_to():
    decision = commit(engine_with(), vendor_id="green-thumb-landscaping")

    assert decision.level is AutonomyLevel.PREPARE_ONLY
    assert decision.rule_id == Rule.VENDOR_NOT_IN_CATEGORY_LIST


def test_currency_mismatch_escalates_instead_of_being_converted():
    decision = commit(engine_with(), currency="TRY", amount=Decimal("1200.00"))

    assert decision.level is AutonomyLevel.ESCALATE
    assert decision.rule_id == Rule.CURRENCY_MISMATCH


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-50.00")])
def test_nonpositive_amount_escalates_as_a_parsing_failure(amount):
    decision = commit(engine_with(), amount=amount)

    assert decision.level is AutonomyLevel.ESCALATE
    assert decision.rule_id == Rule.AMOUNT_INVALID


def test_commitment_rederives_intake_and_cannot_be_handed_a_forged_verdict():
    """The commitment gate does not accept a caller-supplied autonomy level.

    If it did, every rule in the engine would be bypassable by a caller that
    simply asserted it was already authorized.
    """
    signature = inspect.signature(PolicyEngine.authorize_commitment)
    assert "autonomy_level" not in signature.parameters
    assert "intake" not in signature.parameters
    assert "decision" not in signature.parameters

    engine = engine_with(settings=GlobalSettings(kill_switch=True))
    decision = commit(engine)

    assert decision.level is AutonomyLevel.ESCALATE
    assert decision.rule_id == Rule.KILL_SWITCH


# ----------------------------------------------------------------------
# Structural guarantees
# ----------------------------------------------------------------------


def test_urgency_is_structurally_incapable_of_granting_authority():
    """Urgency is model-inferred from resident text, so it is not a permission.

    Accepting it here would let anyone escalate Steward's privileges by writing
    the word URGENT. The guarantee is enforced by absence: the engine has no
    parameter to pass it through.
    """
    for method in (PolicyEngine.evaluate_intake, PolicyEngine.authorize_commitment):
        params = set(inspect.signature(method).parameters)
        assert "urgency" not in params, f"{method.__name__} must not read urgency"


def test_every_decision_carries_a_rule_id_and_a_reason():
    """The audit trail is only reviewable if no decision is anonymous."""
    engine = engine_with()
    decisions = [
        engine.evaluate_intake(category=Category.ELEVATOR, triage_confidence=CONFIDENT),
        engine.evaluate_intake(category=Category.PLUMBING, triage_confidence=CONFIDENT),
        engine.evaluate_intake(category=Category.ELEVATOR, triage_confidence=0.1),
        commit(engine),
        commit(engine, amount=Decimal("99999.00")),
        commit(engine, vendor_allowlisted=False),
    ]

    for decision in decisions:
        assert decision.rule_id, "a decision without a rule id cannot be audited"
        assert decision.reason.strip(), "a decision without a reason cannot be explained"


def test_the_engine_is_pure_and_repeatable():
    """Same inputs, same verdict. The audit trail depends on this."""
    engine = engine_with()
    first = commit(engine)
    second = commit(engine)

    assert first == second


def test_policy_decision_is_immutable():
    """A verdict must not be editable after the fact by the code it constrains."""
    decision = commit(engine_with(), amount=Decimal("99999.00"))
    assert decision.level is AutonomyLevel.PREPARE_ONLY

    with pytest.raises(FrozenInstanceError):
        decision.level = AutonomyLevel.AUTONOMOUS  # type: ignore[misc]

    assert decision.level is AutonomyLevel.PREPARE_ONLY
