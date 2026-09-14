from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from steward.domain.clock import FrozenClock
from steward.domain.enums import (
    ActorType,
    AutonomyLevel,
    CaseStatus,
    Category,
    Urgency,
    group_of,
)
from steward.domain.models import AuditEntry, Case, Quote
from steward.mail import AddressScheme, OutboundMessage
from steward.operations import (
    CompletionClaim,
    CompletionVerificationService,
    VerificationIntegrityError,
    VerificationOutcome,
    VerificationResponse,
    VerifiedClosureCoordinator,
)
from steward.policy import PolicyEngine, Rule
from steward.procurement import (
    CommitmentDisposition,
    DecisionReason,
    DecisionReasonCode,
    QuoteDecisionPackage,
    RfqBatch,
    RfqDispatch,
)
from steward.seed import load_seed
from steward.store import ConcurrencyConflict, SqliteOperationalStore

UTC = timezone.utc
SCHEME = AddressScheme(
    management_domain="site.narrativenode-labs.cloud",
    vendor_domain="vendors.narrativenode-labs.cloud",
)


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return f"{prefix}-verification-{self.value:03d}"


def verification_case():
    opened = datetime(2026, 9, 10, 9, tzinfo=UTC)
    case = Case(
        case_id="case-verification-001",
        reply_token="abcdef123456",
        title="A Block elevator shudders",
        category=Category.ELEVATOR,
        group=group_of(Category.ELEVATOR),
        urgency=Urgency.HIGH,
        asset_id="elevator-a",
        opened_at=opened,
        updated_at=opened,
        accepted_quote_id="quote-verification-001",
        scheduled_for=opened + timedelta(hours=20),
        status=CaseStatus.SCHEDULED,
        autonomy_level=AutonomyLevel.AUTONOMOUS,
    )
    claim = CompletionClaim(
        claim_id="claim-verification-001",
        case_id=case.case_id,
        vendor_id="meridian-lift",
        quote_id=case.accepted_quote_id,
        source_id="vendor-completion-email-001",
        onsite_at=opened + timedelta(hours=22),
        resolved_at=opened + timedelta(hours=24),
        claimed_at=opened + timedelta(hours=25),
        actual_cost=Decimal("705.00"),
        currency="USD",
        work_performed="Corrected rail bracket alignment and replaced guide shoes.",
        notes="Vendor reports normal travel restored.",
        simulated=True,
    )
    return case, claim


def procurement_context(case: Case, claim: CompletionClaim):
    bundle = load_seed()
    policy = PolicyEngine(bundle.policies, bundle.settings)
    quote = Quote(
        quote_id=claim.quote_id,
        case_id=case.case_id,
        vendor_id=claim.vendor_id,
        amount=claim.actual_cost,
        currency=claim.currency,
        scope="correct rail bracket alignment and replace guide shoes",
        earliest_onsite_at=case.scheduled_for,
        received_at=case.opened_at + timedelta(hours=3),
        source_email_message_id="<quote@vendors.narrativenode-labs.cloud>",
    )
    decision_at = case.opened_at + timedelta(hours=5)
    commitment = policy.authorize_commitment(
        category=case.category,
        triage_confidence=0.95,
        vendor_id=quote.vendor_id,
        vendor_allowlisted=True,
        amount=quote.amount,
        currency=quote.currency,
    )
    decision = QuoteDecisionPackage(
        case_id=case.case_id,
        recommended_quote=quote,
        comparisons=(),
        nonresponding_vendor_ids=(),
        price_premium=Decimal("0"),
        reasons=(
            DecisionReason(
                code=DecisionReasonCode.POLICY_AUTHORIZATION,
                summary="Exact quote is within policy.",
                source_ids=(quote.quote_id, Rule.COMMIT_WITHIN_POLICY),
            ),
        ),
        commitment_decision=commitment,
        commitment_disposition=CommitmentDisposition.AUTHORIZED_NOT_SENT,
        commitment_audit=AuditEntry(
            audit_id="audit-verification-commit",
            case_id=case.case_id,
            at=decision_at,
            action="commit_quote_dry_run",
            autonomy_level=AutonomyLevel.AUTONOMOUS,
            policy_rule_id=Rule.COMMIT_WITHIN_POLICY,
            reason=commitment.reason,
            amount=quote.amount,
            vendor_id=quote.vendor_id,
        ),
        created_at=decision_at,
    )
    message = OutboundMessage(
        to=("quotes@meridian-lift.vendors.narrativenode-labs.cloud",),
        subject="RFQ",
        body_text="Request for quote only.",
        from_address=SCHEME.management_address,
    )
    batch = RfqBatch(
        case_id=case.case_id,
        sent_at=case.opened_at + timedelta(hours=1),
        policy_decision=policy.evaluate_intake(
            category=case.category,
            triage_confidence=0.95,
        ),
        dispatches=(
            RfqDispatch(
                vendor_id=quote.vendor_id,
                provider_message_id="recorded-rfq-001",
                audit_id="audit-rfq-001",
                message=message,
            ),
        ),
        exclusions=(),
    )
    return quote, decision, batch


def test_vendor_claim_waits_for_durable_human_confirmation_before_closure(tmp_path):
    path = tmp_path / "verified.db"
    case, claim = verification_case()
    request_clock = FrozenClock(claim.claimed_at)
    ids = Ids()
    service = CompletionVerificationService(clock=request_clock, id_factory=ids)

    requested = service.request(case=case, claim=claim)

    assert requested.case.status is CaseStatus.AWAITING_VERIFICATION
    assert requested.case.closed_at is None
    assert requested.request.source_ids == (
        claim.claim_id,
        claim.source_id,
        claim.quote_id,
    )
    assert requested.outbox_items[0].payload["outbound_channel_selected"] is False

    store = SqliteOperationalStore(path)
    saved = store.save_transition(
        case=requested.case,
        expected_version=0,
        new_case=True,
        idempotency_key="verification:request:001",
        timeline_events=requested.timeline_events,
        outbox_items=requested.outbox_items,
        artifacts=requested.artifacts,
    )
    assert saved.version == 1
    store.close()

    restarted = SqliteOperationalStore(path)
    persisted = restarted.get_case(case.case_id)
    assert persisted == requested.case
    assert restarted.artifact("verification_request", requested.request.request_id) is not None
    assert len(restarted.pending_outbox()) == 1

    response_at = claim.claimed_at + timedelta(hours=2)
    request_clock.set(response_at)
    response = VerificationResponse(
        response_id="verification-response-001",
        request_id=requested.request.request_id,
        case_id=case.case_id,
        outcome=VerificationOutcome.CONFIRMED,
        actor=ActorType.RESIDENT,
        actor_label="Resident A.",
        source_id="telegram-confirmation-001",
        responded_at=response_at,
        notes="The lift is running smoothly now.",
    )
    confirmed = service.respond(
        case=persisted,
        claim=claim,
        request=requested.request,
        response=response,
    )

    assert confirmed.case.status is CaseStatus.RESOLVED
    assert confirmed.case.closed_at is None
    assert confirmed.resolution_evidence is not None
    assert confirmed.resolution_evidence.verification_source_ids == (
        "telegram-confirmation-001",
    )
    second = restarted.save_transition(
        case=confirmed.case,
        expected_version=1,
        idempotency_key="verification:response:001",
        timeline_events=confirmed.timeline_events,
        artifacts=confirmed.artifacts,
    )
    assert second.version == 2

    quote, decision, batch = procurement_context(confirmed.case, claim)
    coordinator = VerifiedClosureCoordinator(
        store=restarted,
        clock=FrozenClock(response_at),
        id_factory=ids,
    )
    with pytest.raises(ConcurrencyConflict):
        coordinator.close(
            case=confirmed.case,
            expected_version=1,
            idempotency_key="verification:close:stale",
            decision=decision,
            rfq_batch=batch,
            quotes=(quote,),
            evidence=confirmed.resolution_evidence,
            raised_by="Resident A.",
            raised_as="The lift is shuddering.",
        )
    assert restarted.get(case.case_id) is None
    assert restarted.get_case(case.case_id) == confirmed.case

    atomic = coordinator.close(
        case=confirmed.case,
        expected_version=2,
        idempotency_key="verification:close:001",
        decision=decision,
        rfq_batch=batch,
        quotes=(quote,),
        evidence=confirmed.resolution_evidence,
        raised_by="Resident A.",
        raised_as="The lift is shuddering.",
    )
    closed = atomic.resolution
    assert closed.record.outcome_verified is True
    assert closed.record.verification_source_ids == ["telegram-confirmation-001"]
    assert atomic.transition.version == 3
    restarted.close()

    final_restart = SqliteOperationalStore(path)
    assert final_restart.get_case(case.case_id) == closed.case
    assert final_restart.get(case.case_id) == closed.record
    final_restart.close()


def test_partial_or_rejected_verification_reopens_as_warranty_without_success_memory(tmp_path):
    case, claim = verification_case()
    clock = FrozenClock(claim.claimed_at)
    service = CompletionVerificationService(clock=clock, id_factory=Ids())
    requested = service.request(case=case, claim=claim)
    clock.advance(timedelta(hours=1))
    response = VerificationResponse(
        response_id="verification-response-partial",
        request_id=requested.request.request_id,
        case_id=case.case_id,
        outcome=VerificationOutcome.PARTIAL,
        actor=ActorType.HUMAN,
        actor_label="Building Manager",
        source_id="manager-review-001",
        responded_at=clock.now(),
        notes="The grinding stopped but the car still shudders near floor four.",
    )

    result = service.respond(
        case=requested.case,
        claim=claim,
        request=requested.request,
        response=response,
    )

    assert result.case.status is CaseStatus.WARRANTY_REVIEW
    assert result.case.resolved_at is None
    assert result.case.next_action_due_at == clock.now()
    assert result.resolution_evidence is None
    assert result.warranty_claim is not None
    assert result.warranty_claim.vendor_id == claim.vendor_id
    assert result.outbox_items[0].kind == "warranty_review_required"
    assert result.outbox_items[0].payload["vendor_contact_allowed"] is False

    store = SqliteOperationalStore(tmp_path / "warranty.db")
    store.save_transition(
        case=result.case,
        expected_version=0,
        new_case=True,
        idempotency_key="verification:partial:001",
        timeline_events=(*requested.timeline_events, *result.timeline_events),
        outbox_items=result.outbox_items,
        artifacts=(*requested.artifacts, *result.artifacts),
    )
    assert store.records() == ()
    assert store.due_cases(now=clock.now()) == (result.case,)
    assert store.artifact("warranty_claim", result.warranty_claim.warranty_id) is not None
    store.close()


def test_verification_rejects_wrong_case_request_actor_and_unverified_resolution():
    case, claim = verification_case()
    clock = FrozenClock(claim.claimed_at)
    service = CompletionVerificationService(clock=clock, id_factory=Ids())
    requested = service.request(case=case, claim=claim)
    clock.advance(timedelta(hours=1))

    with pytest.raises(ValueError, match="resident or manager"):
        VerificationResponse(
            response_id="bad-actor",
            request_id=requested.request.request_id,
            case_id=case.case_id,
            outcome=VerificationOutcome.CONFIRMED,
            actor=ActorType.VENDOR,
            actor_label="Vendor",
            source_id="vendor-self-confirmation",
            responded_at=clock.now(),
        )
    wrong = VerificationResponse(
        response_id="wrong-request",
        request_id="another-request",
        case_id=case.case_id,
        outcome=VerificationOutcome.CONFIRMED,
        actor=ActorType.RESIDENT,
        actor_label="Resident A.",
        source_id="resident-source",
        responded_at=clock.now(),
    )
    with pytest.raises(VerificationIntegrityError, match="does not match"):
        service.respond(
            case=requested.case,
            claim=claim,
            request=requested.request,
            response=wrong,
        )
