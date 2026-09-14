"""Dry-run RFQ and source-traceable quote decision acceptance tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from steward.domain.clock import FrozenClock
from steward.domain.enums import Category, Urgency, group_of
from steward.domain.models import Case, Quote
from steward.mail import (
    AddressScheme,
    RecipientGuard,
    RecipientNotAllowed,
    RecordingMailTransport,
)
from steward.memory import StructuredMemoryRetriever
from steward.policy import PolicyEngine, Rule
from steward.procurement import (
    CommitmentDisposition,
    DecisionReasonCode,
    QuoteDecisionBuilder,
    RecordingAuditSink,
    RfqDispatcher,
    RfqNotAuthorized,
)
from steward.seed import load_seed

SCHEME = AddressScheme(
    management_domain="site.narrativenode-labs.cloud",
    vendor_domain="vendors.narrativenode-labs.cloud",
    management_display_name="Northgate Residence Management",
)


@pytest.fixture(scope="module")
def seed():
    return load_seed()


@pytest.fixture
def elevator_case(seed):
    now = seed.demo_messages[0].message.ingested_at
    return Case(
        case_id="case-elevator-live",
        reply_token="abcdef123456",
        title="A Block elevator shudders near the fourth floor",
        category=Category.ELEVATOR,
        group=group_of(Category.ELEVATOR),
        urgency=Urgency.HIGH,
        asset_id="elevator-a",
        opened_at=now,
        updated_at=now,
        related_case_ids=[
            "hist-2025-017",
            "hist-2026-001",
            "hist-2025-002",
            "hist-2025-008",
            "hist-2026-007",
        ],
        currency="USD",
    )


def memory_for(seed, case):
    query = "\n".join(
        (
            seed.demo_messages[0].message.text,
            case.title,
            "Recurring shuddering and grinding on ascent near floor four.",
        )
    )
    return StructuredMemoryRetriever(
        seed.history,
        recurrence_window_days=seed.settings.recurrence_window_days,
    ).recall(
        category=case.category,
        asset_id=case.asset_id,
        query_text=query,
        as_of=case.opened_at,
    )


class OrderedRecordingTransport:
    def __init__(self, audit, guard):
        self.audit = audit
        self.recording = RecordingMailTransport(guard=guard)
        self.calls = 0

    def send(self, message):
        assert len(self.audit.entries) == self.calls + 1, "audit must precede transport"
        self.calls += 1
        return self.recording.send(message)

    @property
    def sent(self):
        return self.recording.sent


def test_rfq_batch_is_preflighted_allowlisted_and_audited_before_recording(seed, elevator_case):
    guard = RecipientGuard.of(SCHEME.vendor_domain)
    audit = RecordingAuditSink()
    transport = OrderedRecordingTransport(audit, guard)
    dispatcher = RfqDispatcher(
        policy=PolicyEngine(seed.policies, seed.settings),
        transport=transport,
        recipient_guard=guard,
        address_scheme=SCHEME,
        audit_sink=audit,
        clock=FrozenClock(elevator_case.opened_at),
        id_factory=lambda prefix: f"{prefix}-{len(audit.entries) + 1}",
    )

    batch = dispatcher.dispatch(
        case=elevator_case,
        triage_confidence=0.9,
        asset=seed.asset("elevator-a"),
        vendors=seed.vendors,
    )

    assert batch.contacted_vendor_ids == (
        "meridian-lift",
        "coastline-elevator",
        "pinnacle-vertical",
    )
    assert len(transport.sent) == 3
    assert len(audit.entries) == 3
    assert all(entry.action == "send_rfq" for entry in audit.entries)
    assert all(entry.policy_rule_id == Rule.INTAKE_WITHIN_POLICY for entry in audit.entries)
    assert {message.to[0] for message in transport.sent} == {
        "meridian@vendors.narrativenode-labs.cloud",
        "coastline@vendors.narrativenode-labs.cloud",
        "pinnacle@vendors.narrativenode-labs.cloud",
    }
    assert all(
        message.reply_to == SCHEME.case_reply_address(elevator_case.reply_token)
        for message in transport.sent
    )
    assert all("No work is authorized" in message.body_text for message in transport.sent)
    assert all("Daniel" not in message.body_text for message in transport.sent)


def test_poisoned_recipient_blocks_entire_batch_before_audit_or_send(seed, elevator_case):
    guard = RecipientGuard.of(SCHEME.vendor_domain)
    audit = RecordingAuditSink()
    transport = RecordingMailTransport(guard=guard)
    vendors = list(seed.vendors)
    meridian_index = next(
        index for index, vendor in enumerate(vendors) if vendor.vendor_id == "meridian-lift"
    )
    vendors[meridian_index] = vendors[meridian_index].model_copy(
        update={"email": "billing@attacker.example"}
    )
    dispatcher = RfqDispatcher(
        policy=PolicyEngine(seed.policies, seed.settings),
        transport=transport,
        recipient_guard=guard,
        address_scheme=SCHEME,
        audit_sink=audit,
        clock=FrozenClock(elevator_case.opened_at),
    )

    with pytest.raises(RecipientNotAllowed):
        dispatcher.dispatch(
            case=elevator_case,
            triage_confidence=0.9,
            asset=seed.asset("elevator-a"),
            vendors=vendors,
        )

    assert transport.sent == []
    assert audit.entries == []


def test_policy_without_contact_authority_sends_nothing(seed, elevator_case):
    guard = RecipientGuard.of(SCHEME.vendor_domain)
    audit = RecordingAuditSink()
    transport = RecordingMailTransport(guard=guard)
    dispatcher = RfqDispatcher(
        policy=PolicyEngine({}, seed.settings),
        transport=transport,
        recipient_guard=guard,
        address_scheme=SCHEME,
        audit_sink=audit,
        clock=FrozenClock(elevator_case.opened_at),
    )

    with pytest.raises(RfqNotAuthorized, match="POLICY.MISSING"):
        dispatcher.dispatch(
            case=elevator_case,
            triage_confidence=0.9,
            asset=seed.asset("elevator-a"),
            vendors=seed.vendors,
        )

    assert transport.sent == []
    assert audit.entries == []


def quote(
    case,
    *,
    quote_id: str,
    vendor_id: str,
    amount: str,
    scope: str,
    hours_to_onsite: float,
):
    return Quote(
        quote_id=quote_id,
        case_id=case.case_id,
        vendor_id=vendor_id,
        amount=Decimal(amount),
        currency="USD",
        scope=scope,
        earliest_onsite_at=case.opened_at + timedelta(hours=hours_to_onsite),
        received_at=case.opened_at + timedelta(hours=1),
        source_email_message_id=f"<{quote_id}@vendors.example>",
    )


def test_decision_prefers_root_cause_reliability_over_cheapest_failed_remedy(
    seed, elevator_case
):
    meridian = quote(
        elevator_case,
        quote_id="quote-meridian",
        vendor_id="meridian-lift",
        amount="705.00",
        scope=(
            "Quote is 705 including labour, travel and shoes, and rail alignment "
            "correction if we find it."
        ),
        hours_to_onsite=20,
    )
    coastline = quote(
        elevator_case,
        quote_id="quote-coastline",
        vendor_id="coastline-elevator",
        amount="540.00",
        scope="We can replace the guide shoes for 540 all in.",
        hours_to_onsite=96,
    )
    builder = QuoteDecisionBuilder(
        policy=PolicyEngine(seed.policies, seed.settings),
        clock=FrozenClock(elevator_case.opened_at + timedelta(hours=30)),
        id_factory=lambda prefix: f"{prefix}-decision-1",
    )

    package = builder.build(
        case=elevator_case,
        triage_confidence=0.9,
        quotes=(meridian, coastline),
        vendors=seed.vendors,
        memory=memory_for(seed, elevator_case),
        nonresponding_vendor_ids=("pinnacle-vertical",),
    )

    assert package.recommended_quote.vendor_id == "meridian-lift"
    assert package.recommended_quote.amount == Decimal("705.00")
    assert package.price_premium == Decimal("165.00")
    assert package.nonresponding_vendor_ids == ("pinnacle-vertical",)
    assert package.commitment_disposition is CommitmentDisposition.AUTHORIZED_NOT_SENT
    assert package.commitment_decision.rule_id == Rule.COMMIT_WITHIN_POLICY
    assert package.commitment_sent is False
    assert package.commitment_audit.action == "commit_quote_dry_run"
    assert package.commitment_audit.vendor_id == "meridian-lift"
    assert package.commitment_audit.amount == Decimal("705.00")
    assert elevator_case.accepted_quote_id is None

    comparisons = {item.quote.vendor_id: item for item in package.comparisons}
    assert comparisons["meridian-lift"].addresses_known_cause
    assert not comparisons["meridian-lift"].repeats_failed_scope_only
    assert comparisons["coastline-elevator"].repeats_failed_scope_only
    assert comparisons["meridian-lift"].repeat_failure_rate == 0.0
    assert comparisons["coastline-elevator"].repeat_failure_rate == pytest.approx(2 / 3)

    reasons = {reason.code: reason for reason in package.reasons}
    assert set(reasons) == {
        DecisionReasonCode.KNOWN_CAUSE_COVERED,
        DecisionReasonCode.FAILED_REMEDY_AVOIDED,
        DecisionReasonCode.RELIABILITY_RECORD,
        DecisionReasonCode.RESPONSE_RECORD,
        DecisionReasonCode.PRICE_TRADEOFF,
        DecisionReasonCode.POLICY_AUTHORIZATION,
    }
    assert "6 completed" in reasons[DecisionReasonCode.RELIABILITY_RECORD].summary
    assert "66.7%" in reasons[DecisionReasonCode.RELIABILITY_RECORD].summary
    assert "3.82 hours" in reasons[DecisionReasonCode.RESPONSE_RECORD].summary
    assert "30.00 hours" in reasons[DecisionReasonCode.RESPONSE_RECORD].summary
    assert "165.00 USD" in reasons[DecisionReasonCode.PRICE_TRADEOFF].summary
    assert {"hist-2025-017", "hist-2026-001"} <= set(package.source_ids)
    assert {"quote-meridian", "quote-coastline", Rule.COMMIT_WITHIN_POLICY} <= set(
        package.source_ids
    )


def test_monthly_cap_can_withhold_commitment_without_changing_recommendation(
    seed, elevator_case
):
    meridian = quote(
        elevator_case,
        quote_id="quote-meridian",
        vendor_id="meridian-lift",
        amount="705.00",
        scope="rail bracket alignment correction and guide shoes",
        hours_to_onsite=20,
    )
    coastline = quote(
        elevator_case,
        quote_id="quote-coastline",
        vendor_id="coastline-elevator",
        amount="540.00",
        scope="replace guide shoes",
        hours_to_onsite=96,
    )

    package = QuoteDecisionBuilder(
        policy=PolicyEngine(seed.policies, seed.settings),
        clock=FrozenClock(elevator_case.opened_at),
    ).build(
        case=elevator_case,
        triage_confidence=0.9,
        quotes=(meridian, coastline),
        vendors=seed.vendors,
        memory=memory_for(seed, elevator_case),
        month_to_date_spend=Decimal("3500.00"),
    )

    assert package.recommended_quote.vendor_id == "meridian-lift"
    assert package.commitment_disposition is CommitmentDisposition.AWAITING_HUMAN_APPROVAL
    assert package.commitment_decision.rule_id == Rule.OVER_MONTHLY_CAP
    assert package.commitment_sent is False
