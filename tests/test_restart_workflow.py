from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from steward.agents import IntakeService, TriageAction, TriageResult
from steward.channels import TelegramShadowAdapter, TelegramShadowWorker
from steward.domain.clock import FrozenClock
from steward.domain.enums import (
    ActorType,
    AutonomyLevel,
    CaseStatus,
    Category,
    EventKind,
    Urgency,
)
from steward.domain.models import AuditEntry, Quote
from steward.mail import AddressScheme, OutboundMessage
from steward.memory import CaseRecord
from steward.operations import (
    CompletionClaim,
    CompletionVerificationService,
    VerificationOutcome,
    VerificationRequest,
    VerificationResponse,
    VerifiedClosureCoordinator,
)
from steward.playbooks import default_playbooks
from steward.policy import PolicyEngine, Rule
from steward.proactive import ProactiveMaintenanceEngine
from steward.procurement import (
    CommitmentDisposition,
    DecisionReason,
    DecisionReasonCode,
    QuoteDecisionPackage,
    RfqBatch,
    RfqDispatch,
)
from steward.seed import load_seed
from steward.store import SqliteOperationalStore

UTC = timezone.utc
SECRET = "integration-shadow-secret-2026"
CHAT_ID = "-1002481179934"
SCHEME = AddressScheme(
    management_domain="site.narrativenode-labs.cloud",
    vendor_domain="vendors.narrativenode-labs.cloud",
)


class Ids:
    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return f"{prefix}-{self.phase}-{self.value:03d}"


class ElevatorClassifier:
    def classify(self, *, message, assets, open_cases):
        del message, assets, open_cases
        return TriageResult(
            action=TriageAction.OPEN_CASE,
            category=Category.ELEVATOR,
            urgency=Urgency.HIGH,
            confidence=0.95,
            title="A Block elevator shudders near floor four",
            asset_id="elevator-a",
            rationale="Resident reports recurring shudder near floor four.",
        )


def telegram_update(sent_at: datetime) -> dict[str, object]:
    return {
        "update_id": 9901,
        "message": {
            "message_id": 177,
            "date": int(sent_at.timestamp()),
            "chat": {"id": int(CHAT_ID), "type": "supergroup"},
            "from": {
                "id": 99887766,
                "is_bot": False,
                "first_name": "Daniel",
                "last_name": "Kaya",
                "username": "must_not_persist",
            },
            "text": "A Block lift is shuddering again near the fourth floor.",
        },
    }


def procurement_context(case, claim, policy):
    quote = Quote(
        quote_id=claim.quote_id,
        case_id=case.case_id,
        vendor_id=claim.vendor_id,
        amount=claim.actual_cost,
        currency=claim.currency,
        scope="correct rail bracket alignment and replace guide shoes",
        earliest_onsite_at=claim.onsite_at,
        received_at=case.opened_at + timedelta(hours=3),
        source_email_message_id="<integration-quote@vendors.test>",
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
            audit_id="audit-integration-commit",
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
                provider_message_id="recorded-rfq-integration",
                audit_id="audit-rfq-integration",
                message=OutboundMessage(
                    to=("quotes@meridian-lift.vendors.test",),
                    subject="RFQ",
                    body_text="Recording-only request for quote.",
                    from_address=SCHEME.management_address,
                ),
            ),
        ),
        exclusions=(),
    )
    return quote, decision, batch


def test_shadow_intake_survives_restart_then_closes_verified_and_suggests_maintenance(
    tmp_path,
):
    path = tmp_path / "restart-workflow.db"
    opened_at = datetime(2026, 4, 1, 9, tzinfo=UTC)
    bundle = load_seed()
    policy = PolicyEngine(bundle.policies, bundle.settings)
    playbooks = default_playbooks()
    clock = FrozenClock(opened_at + timedelta(minutes=1))
    store = SqliteOperationalStore(path)
    assert store.write(
        CaseRecord(
            case_id="historical-elevator-001",
            title="Prior A Block elevator shudder",
            category=Category.ELEVATOR,
            asset_id="elevator-a",
            opened_at=datetime(2026, 1, 1, 9, tzinfo=UTC),
            resolved_at=datetime(2026, 1, 2, 9, tzinfo=UTC),
            closed_at=datetime(2026, 1, 2, 10, tzinfo=UTC),
            outcome_verified=True,
            verification_source_ids=["manager-history-confirmation-001"],
        )
    )

    shadow = TelegramShadowAdapter(
        secret_token=SECRET,
        allowed_chat_ids={CHAT_ID},
        inbox=store,
        clock=clock,
    )
    accepted = shadow.handle_update(
        telegram_update(opened_at),
        secret_header=SECRET,
    )
    intake = IntakeService(
        classifier=ElevatorClassifier(),
        store=store,
        policy=policy,
        assets=bundle.assets,
        clock=clock,
        id_factory=Ids("before-restart"),
        reply_token_factory=lambda: "abcdef123456",
    )
    drained = TelegramShadowWorker(inbox=store, intake=intake).drain()

    assert accepted.message is not None
    assert drained.processed_external_ids == ("9901",)
    assert len(drained.outcomes) == 1
    assert store.pending_inbound() == ()
    assert store.pending_outbox() == ()
    case = drained.outcomes[0].case
    assert case is not None
    assert case.category is Category.ELEVATOR
    assert case.asset_id == "elevator-a"

    scheduled_at = opened_at + timedelta(hours=5)
    clock.set(scheduled_at)
    scheduled = case.model_copy(
        update={
            "accepted_quote_id": "quote-restart-integration-001",
            "scheduled_for": opened_at + timedelta(days=1),
            "status": CaseStatus.SCHEDULED,
            "updated_at": scheduled_at,
        },
        deep=True,
    )
    scheduled_save = store.save_transition(
        case=scheduled,
        expected_version=1,
        idempotency_key="integration:schedule:001",
    )
    assert scheduled_save.version == 2

    claim = CompletionClaim(
        claim_id="claim-restart-integration-001",
        case_id=case.case_id,
        vendor_id="meridian-lift",
        quote_id=scheduled.accepted_quote_id,
        source_id="vendor-completion-integration-001",
        onsite_at=opened_at + timedelta(days=1),
        resolved_at=opened_at + timedelta(days=1, hours=2),
        claimed_at=opened_at + timedelta(days=1, hours=3),
        actual_cost=Decimal("705.00"),
        currency="USD",
        work_performed="Corrected rail bracket alignment and replaced guide shoes.",
        notes="Vendor reports smooth travel restored.",
        simulated=True,
    )
    clock.set(claim.claimed_at)
    requested = CompletionVerificationService(
        clock=clock,
        playbooks=playbooks,
        id_factory=Ids("request"),
    ).request(case=scheduled, claim=claim)
    request_save = store.save_transition(
        case=requested.case,
        expected_version=2,
        idempotency_key="integration:verification-request:001",
        timeline_events=requested.timeline_events,
        outbox_items=requested.outbox_items,
        artifacts=requested.artifacts,
    )
    assert request_save.version == 3
    store.close()

    restarted = SqliteOperationalStore(path)
    persisted_case = restarted.get_case(case.case_id)
    claim_artifact = restarted.artifact("completion_claim", claim.claim_id)
    request_artifact = restarted.artifact(
        "verification_request",
        requested.request.request_id,
    )
    assert persisted_case is not None
    assert persisted_case.status is CaseStatus.AWAITING_VERIFICATION
    assert claim_artifact is not None
    assert request_artifact is not None
    durable_claim = CompletionClaim.model_validate(claim_artifact.payload)
    durable_request = VerificationRequest.model_validate(request_artifact.payload)

    response_at = claim.claimed_at + timedelta(hours=1)
    clock.set(response_at)
    response = VerificationResponse(
        response_id="resident-confirmation-integration-001",
        request_id=durable_request.request_id,
        case_id=case.case_id,
        outcome=VerificationOutcome.CONFIRMED,
        actor=ActorType.RESIDENT,
        actor_label="Daniel K.",
        source_id="telegram-resident-confirmation-001",
        responded_at=response_at,
        notes="The elevator now travels smoothly past the fourth floor.",
    )
    confirmed = CompletionVerificationService(
        clock=clock,
        playbooks=playbooks,
        id_factory=Ids("response"),
    ).respond(
        case=persisted_case,
        claim=durable_claim,
        request=durable_request,
        response=response,
    )
    assert confirmed.resolution_evidence is not None
    confirmed_save = restarted.save_transition(
        case=confirmed.case,
        expected_version=3,
        idempotency_key="integration:verification-response:001",
        timeline_events=confirmed.timeline_events,
        artifacts=confirmed.artifacts,
    )
    assert confirmed_save.version == 4

    quote, decision, batch = procurement_context(confirmed.case, durable_claim, policy)
    atomic = VerifiedClosureCoordinator(
        store=restarted,
        clock=clock,
        id_factory=Ids("closure"),
    ).close(
        case=confirmed.case,
        expected_version=4,
        idempotency_key="integration:verified-close:001",
        decision=decision,
        rfq_batch=batch,
        quotes=(quote,),
        evidence=confirmed.resolution_evidence,
        raised_by=accepted.message.sender_display,
        raised_as=accepted.message.text,
    )
    assert atomic.transition.version == 5
    assert atomic.resolution.case.status is CaseStatus.CLOSED
    restarted.close()

    final_store = SqliteOperationalStore(path)
    final_case = final_store.get_case(case.case_id)
    learned = final_store.get(case.case_id)
    assert final_case is not None
    assert final_case.status is CaseStatus.CLOSED
    assert learned is not None
    assert learned.outcome_verified is True
    assert learned.verification_source_ids == ["telegram-resident-confirmation-001"]
    assert final_store.case_version(case.case_id) == 5
    timeline_kinds = {event.kind for event in final_store.timeline_for(case.case_id)}
    assert {
        EventKind.CASE_OPENED,
        EventKind.COMPLETION_CLAIMED,
        EventKind.VERIFICATION_REQUESTED,
        EventKind.VERIFICATION_CONFIRMED,
        EventKind.RESOLVED,
        EventKind.MEMORY_WRITTEN,
        EventKind.CLOSED,
    } <= timeline_kinds
    assert all(
        item.payload.get("outbound_channel_selected") is False
        for item in final_store.pending_outbox()
    )

    suggestions = ProactiveMaintenanceEngine(
        policy=policy,
        rules=playbooks.proactive_rules,
    ).suggest(
        final_store.records(),
        as_of=datetime(2026, 6, 15, 9, tzinfo=UTC),
    )
    elevator = next(
        item for item in suggestions if item.rule_id == "elevator.recurrence.review"
    )
    assert elevator.source_case_ids == (
        "historical-elevator-001",
        case.case_id,
    )
    assert elevator.vendor_contact_allowed is False
    assert elevator.requires_human_review is True
    assert elevator.due_at.date().isoformat() == "2026-06-30"
    final_store.close()
