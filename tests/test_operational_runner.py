"""Stage 6 operational hardening: leases, persisted suggestions, and the runner.

These tests exercise the restart-safe boundaries introduced in Stage 6 without
touching the seed loader, demo bootstrap, or any network transport. Every
assertion is about durable, deterministic behaviour: two workers cannot hold the
same lease, an expired lease is reclaimable, a stale token fails closed, proactive
suggestions persist and decide idempotently while never granting vendor contact,
the runner quiesces on a second tick and never enables outbound, and a confirmed
completion is ordered before its resolved event on the causal timeline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from steward.agents import IntakeService, TriageAction, TriageResult
from steward.channels import TelegramShadowAdapter
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
    OperationalRunner,
    VerificationOutcome,
    VerificationResponse,
    VerifiedClosureCoordinator,
)
from steward.playbooks import default_playbooks
from steward.policy import PolicyEngine, Rule
from steward.proactive import (
    MaintenanceSuggestion,
    ProactiveMaintenanceEngine,
    SuggestionStatus,
)
from steward.procurement import (
    CommitmentDisposition,
    DecisionReason,
    DecisionReasonCode,
    QuoteDecisionPackage,
    RfqBatch,
    RfqDispatch,
)
from steward.seed import load_seed
from steward.store import (
    InboxItem,
    InboxStatus,
    OutboxItem,
    SqliteOperationalStore,
    StaleLeaseToken,
)

UTC = timezone.utc
SECRET = "operational-runner-secret-2026"
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


def telegram_update(sent_at: datetime, *, update_id: int = 9901, message_id: int = 177):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": int(sent_at.timestamp()),
            "chat": {"id": int(CHAT_ID), "type": "supergroup"},
            "from": {"id": 99887766, "is_bot": False, "first_name": "Daniel"},
            "text": "A Block lift is shuddering again near the fourth floor.",
        },
    }


def build_runner(store, clock, *, proactive=None):
    bundle = load_seed()
    policy = PolicyEngine(bundle.policies, bundle.settings)
    intake = IntakeService(
        classifier=ElevatorClassifier(),
        store=store,
        policy=policy,
        assets=bundle.assets,
        clock=clock,
        id_factory=Ids("intake"),
        reply_token_factory=lambda: "abcdef123456",
    )
    return OperationalRunner(
        store=store,
        intake=intake,
        clock=clock,
        proactive=proactive,
    )


def seed_inbound(store, adapter, sent_at):
    accepted = adapter.handle_update(telegram_update(sent_at), secret_header=SECRET)
    assert accepted.message is not None
    return accepted


# -- Lease exclusion / reclaim / stale token -----------------------------


def _inbox_item(at: datetime) -> InboxItem:
    return InboxItem(
        source="telegram_shadow",
        external_id="5001",
        payload_hash="a" * 64,
        received_at=at,
        status=InboxStatus.PENDING,
        message=None,
    )


def test_two_stores_cannot_both_claim_the_same_inbound_lease(tmp_path):
    path = tmp_path / "lease-exclusion.db"
    at = datetime(2026, 4, 1, 9, tzinfo=UTC)
    store_a = SqliteOperationalStore(path)
    assert store_a.record_inbound(_inbox_item(at))

    store_b = SqliteOperationalStore(path)
    claimed_a = store_a.claim_inbound(token="worker-a", now=at, lease_seconds=300)
    claimed_b = store_b.claim_inbound(token="worker-b", now=at, lease_seconds=300)

    assert [item.external_id for item in claimed_a] == ["5001"]
    assert claimed_b == ()  # the live lease excludes the second worker
    store_a.close()
    store_b.close()


def test_expired_inbound_lease_is_reclaimed_with_incremented_attempts(tmp_path):
    path = tmp_path / "lease-reclaim.db"
    at = datetime(2026, 4, 1, 9, tzinfo=UTC)
    store = SqliteOperationalStore(path)
    assert store.record_inbound(_inbox_item(at))

    first = store.claim_inbound(token="worker-a", now=at, lease_seconds=60)
    assert [item.external_id for item in first] == ["5001"]
    # Still leased before expiry: nobody else can take it.
    assert store.claim_inbound(token="worker-b", now=at + timedelta(seconds=30)) == ()
    # After expiry a different worker reclaims it.
    reclaimed = store.claim_inbound(
        token="worker-b",
        now=at + timedelta(seconds=61),
        lease_seconds=60,
    )
    assert [item.external_id for item in reclaimed] == ["5001"]

    # The original worker's token is now stale and fails closed on completion.
    with pytest.raises(StaleLeaseToken):
        store.complete_inbound_claim(
            source="telegram_shadow",
            external_id="5001",
            token="worker-a",
            now=at + timedelta(seconds=62),
        )
    # The current holder completes normally.
    assert store.complete_inbound_claim(
        source="telegram_shadow",
        external_id="5001",
        token="worker-b",
        now=at + timedelta(seconds=63),
    )
    assert store.pending_inbound() == ()
    store.close()


def test_stale_outbox_token_fails_closed(tmp_path):
    path = tmp_path / "outbox-lease.db"
    at = datetime(2026, 4, 1, 9, tzinfo=UTC)
    store = SqliteOperationalStore(path)
    _open_case_with_outbox(store, at)

    claimed = store.claim_outbox(token="w1", now=at, lease_seconds=60)
    assert len(claimed) == 1
    outbox_id = claimed[0].outbox_id
    # Expire and reclaim with a new token.
    store.claim_outbox(token="w2", now=at + timedelta(seconds=61), lease_seconds=60)

    with pytest.raises(StaleLeaseToken):
        store.complete_outbox_claim(
            outbox_id=outbox_id,
            token="w1",
            delivered_at=at + timedelta(seconds=62),
        )
    assert store.complete_outbox_claim(
        outbox_id=outbox_id,
        token="w2",
        delivered_at=at + timedelta(seconds=63),
    )
    assert store.pending_outbox() == ()
    store.close()


def _open_case_with_outbox(store, at):
    from steward.agents import TriageAssessment
    from steward.domain.enums import group_of
    from steward.domain.models import Case, ResidentMessage

    message = ResidentMessage(
        message_id="tg:test:1",
        source="telegram_shadow",
        chat_id="test",
        sender_display="Resident A.",
        text="A Block elevator is shuddering.",
        sent_at=at,
        ingested_at=at,
        case_id="case-outbox-001",
    )
    triage = TriageResult(
        action=TriageAction.OPEN_CASE,
        category=Category.ELEVATOR,
        urgency=Urgency.HIGH,
        confidence=0.95,
        title="A Block elevator shudders",
        asset_id="elevator-a",
        rationale="Shuddering elevator report.",
    )
    assessment = TriageAssessment(message_id=message.message_id, assessed_at=at, result=triage)
    case = Case(
        case_id="case-outbox-001",
        reply_token="abcdef123456",
        title=triage.title,
        category=triage.category,
        group=group_of(triage.category),
        urgency=triage.urgency,
        asset_id=triage.asset_id,
        opened_at=at,
        updated_at=at,
        source_message_ids=[message.message_id],
        autonomy_level=AutonomyLevel.AUTONOMOUS,
        spend_cap=Decimal("1500.00"),
    )
    store.record_intake(message=message, assessment=assessment, case=case, new_case=True)
    updated = case.model_copy(update={"updated_at": at + timedelta(minutes=1)}, deep=True)
    outbox = OutboxItem(
        outbox_id="outbox-lease-001",
        case_id=case.case_id,
        dedup_key=f"{case.case_id}:notify:1",
        kind="notification",
        payload={"case_id": case.case_id},
        created_at=updated.updated_at,
    )
    store.save_transition(
        case=updated,
        expected_version=1,
        idempotency_key="outbox:seed:001",
        outbox_items=(outbox,),
    )


# -- Persisted suggestion lifecycle / restart / no-contact ----------------


def _verified_record(case_id: str, opened: datetime) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        title="A Block elevator shudders near floor four",
        category=Category.ELEVATOR,
        asset_id="elevator-a",
        opened_at=opened,
        resolved_at=opened,
        closed_at=opened,
        outcome_verified=True,
        problem="Recurring shudder and grinding near floor four.",
        work_performed="Corrected rail bracket alignment.",
        verification_source_ids=[f"verification-{case_id}"],
    )


def _elevator_engine():
    bundle = load_seed()
    return ProactiveMaintenanceEngine(
        policy=PolicyEngine(bundle.policies, bundle.settings),
        rules=default_playbooks().proactive_rules,
    )


def test_persisted_suggestions_survive_restart_decide_idempotently_and_never_contact(
    tmp_path,
):
    path = tmp_path / "suggestions.db"
    store = SqliteOperationalStore(path)
    for record in (
        _verified_record("verified-001", datetime(2026, 1, 1, tzinfo=UTC)),
        _verified_record("verified-002", datetime(2026, 4, 1, tzinfo=UTC)),
    ):
        store.write(record)

    engine = _elevator_engine()
    suggestions = engine.suggest(store.records(), as_of=datetime(2026, 6, 15, tzinfo=UTC))
    elevator = next(s for s in suggestions if s.rule_id == "elevator.recurrence.review")

    assert store.upsert_suggestion(elevator) is True
    # Idempotent upsert of identical facts is a no-op.
    assert store.upsert_suggestion(elevator) is False
    assert elevator.vendor_contact_allowed is False
    later_tick = elevator.model_copy(
        update={"suggested_at": elevator.suggested_at + timedelta(hours=1)}
    )
    assert store.upsert_suggestion(later_tick) is False
    assert store.suggestion(elevator.suggestion_id) == elevator

    assert elevator.requires_human_review is True

    store.close()
    reopened = SqliteOperationalStore(path)
    persisted = reopened.suggestion(elevator.suggestion_id)
    assert persisted == elevator
    assert reopened.list_suggestions(status=SuggestionStatus.SUGGESTED) == (elevator,)

    decided = reopened.decide_suggestion(
        elevator.suggestion_id,
        status=SuggestionStatus.ACCEPTED,
        decided_by="manager@site.example",
        decided_at=datetime(2026, 6, 16, tzinfo=UTC),
    )
    assert decided.status is SuggestionStatus.ACCEPTED
    assert decided.decided_by == "manager@site.example"
    assert decided.decided_at == datetime(2026, 6, 16, tzinfo=UTC)
    assert decided.vendor_contact_allowed is False

    # Deciding again with the same target is idempotent.
    again = reopened.decide_suggestion(
        elevator.suggestion_id,
        status=SuggestionStatus.ACCEPTED,
        decided_by="manager@site.example",
        decided_at=datetime(2026, 6, 17, tzinfo=UTC),
    )
    assert again == decided

    # A recompute does not overwrite a decided suggestion.
    assert reopened.upsert_suggestion(elevator) is False
    assert reopened.suggestion(elevator.suggestion_id).status is SuggestionStatus.ACCEPTED
    reopened.close()


def test_decision_requires_source_and_time_and_status_is_immutable_after_decide(tmp_path):
    store = SqliteOperationalStore(tmp_path / "decide-guard.db")
    store.write(_verified_record("verified-001", datetime(2026, 1, 1, tzinfo=UTC)))
    store.write(_verified_record("verified-002", datetime(2026, 4, 1, tzinfo=UTC)))
    engine = _elevator_engine()
    elevator = next(
        s
        for s in engine.suggest(store.records(), as_of=datetime(2026, 6, 15, tzinfo=UTC))
        if s.rule_id == "elevator.recurrence.review"
    )
    store.upsert_suggestion(elevator)

    # The schema itself refuses a decided status with no source or time.
    with pytest.raises(ValueError):
        MaintenanceSuggestion.model_validate(
            {**elevator.model_dump(), "status": SuggestionStatus.ACCEPTED.value}
        )

    store.decide_suggestion(
        elevator.suggestion_id,
        status=SuggestionStatus.DISMISSED,
        decided_by="manager",
        decided_at=datetime(2026, 6, 16, tzinfo=UTC),
    )
    from steward.store import StoreInvariantError

    with pytest.raises(StoreInvariantError):
        store.decide_suggestion(
            elevator.suggestion_id,
            status=SuggestionStatus.ACCEPTED,
            decided_by="other",
            decided_at=datetime(2026, 6, 17, tzinfo=UTC),
        )
    store.close()


def test_expire_suggestions_moves_past_due_suggested_rows_only(tmp_path):
    store = SqliteOperationalStore(tmp_path / "expire.db")
    store.write(_verified_record("verified-001", datetime(2026, 1, 1, tzinfo=UTC)))
    store.write(_verified_record("verified-002", datetime(2026, 4, 1, tzinfo=UTC)))
    engine = _elevator_engine()
    elevator = next(
        s
        for s in engine.suggest(store.records(), as_of=datetime(2026, 6, 15, tzinfo=UTC))
        if s.rule_id == "elevator.recurrence.review"
    )
    store.upsert_suggestion(elevator)

    # Before the due date nothing expires.
    assert store.expire_suggestions(now=elevator.due_at - timedelta(days=1)) == ()
    expired = store.expire_suggestions(now=elevator.due_at + timedelta(days=1))
    assert expired == (elevator.suggestion_id,)
    assert store.suggestion(elevator.suggestion_id).status is SuggestionStatus.EXPIRED
    # Expiry is idempotent.
    assert store.expire_suggestions(now=elevator.due_at + timedelta(days=2)) == ()
    store.close()


# -- Runner: quiescence, no outbound, restart safety ----------------------


def test_runner_second_tick_is_quiescent_and_never_enables_outbound(tmp_path):
    path = tmp_path / "runner-tick.db"
    opened_at = datetime(2026, 4, 1, 9, tzinfo=UTC)
    clock = FrozenClock(opened_at + timedelta(minutes=1))
    store = SqliteOperationalStore(path)
    store.write(_verified_record("historical-001", datetime(2026, 1, 1, tzinfo=UTC)))
    store.write(_verified_record("historical-002", datetime(2026, 4, 1, tzinfo=UTC)))

    adapter = TelegramShadowAdapter(
        secret_token=SECRET,
        allowed_chat_ids={CHAT_ID},
        inbox=store,
        clock=clock,
    )
    seed_inbound(store, adapter, opened_at)

    runner = build_runner(store, clock, proactive=_elevator_engine())
    assert runner.outbound_enabled is False

    first = runner.tick()
    assert first.outbound_enabled is False
    assert first.processed_external_ids == ("9901",)
    assert len(first.intake_outcomes) == 1
    assert first.intake_outcomes[0].case is not None
    assert store.pending_inbound() == ()
    # The runner claims and completes but never dispatches: outbox untouched.
    assert store.pending_outbox() == ()
    assert len(first.persisted_suggestion_ids) >= 1

    # Second tick has nothing left to do: fully quiescent.
    second = runner.tick()
    assert second.processed_external_ids == ()
    assert second.intake_outcomes == ()
    assert second.persisted_suggestion_ids == ()
    assert second.outbound_enabled is False
    assert second.pending_inbound_remaining == 0

    # Restart and confirm the intake case is durable and still needs no outbound.
    store.close()
    reopened = SqliteOperationalStore(path)
    open_cases = reopened.list_open_cases()
    assert len(open_cases) == 1
    assert open_cases[0].category is Category.ELEVATOR
    assert reopened.pending_outbox() == ()
    runner2 = build_runner(reopened, clock, proactive=_elevator_engine())
    third = runner2.tick()
    assert third.processed_external_ids == ()
    assert third.outbound_enabled is False
    reopened.close()


# -- Causal timeline: confirmed before resolved --------------------------


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
        source_email_message_id="<runner-quote@vendors.test>",
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
            audit_id="audit-runner-commit",
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
                provider_message_id="recorded-rfq-runner",
                audit_id="audit-rfq-runner",
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


def test_verification_confirmed_is_ordered_before_resolved_on_the_timeline(tmp_path):
    path = tmp_path / "causal-timeline.db"
    opened_at = datetime(2026, 4, 1, 9, tzinfo=UTC)
    bundle = load_seed()
    policy = PolicyEngine(bundle.policies, bundle.settings)
    playbooks = default_playbooks()
    clock = FrozenClock(opened_at + timedelta(minutes=1))
    store = SqliteOperationalStore(path)

    adapter = TelegramShadowAdapter(
        secret_token=SECRET,
        allowed_chat_ids={CHAT_ID},
        inbox=store,
        clock=clock,
    )
    accepted = seed_inbound(store, adapter, opened_at)
    runner = build_runner(store, clock)
    report = runner.tick()
    case = report.intake_outcomes[0].case
    assert case is not None

    scheduled_at = opened_at + timedelta(hours=5)
    clock.set(scheduled_at)
    scheduled = case.model_copy(
        update={
            "accepted_quote_id": "quote-runner-001",
            "scheduled_for": opened_at + timedelta(days=1),
            "status": CaseStatus.SCHEDULED,
            "updated_at": scheduled_at,
        },
        deep=True,
    )
    assert store.save_transition(
        case=scheduled,
        expected_version=1,
        idempotency_key="runner:schedule:001",
    ).version == 2

    # The vendor resolves the work, but the resident confirms it only later.
    resolved_moment = opened_at + timedelta(days=1, hours=2)
    reported_moment = opened_at + timedelta(days=2, hours=4)
    claim = CompletionClaim(
        claim_id="claim-runner-001",
        case_id=case.case_id,
        vendor_id="meridian-lift",
        quote_id=scheduled.accepted_quote_id,
        source_id="vendor-completion-runner-001",
        onsite_at=opened_at + timedelta(days=1),
        resolved_at=resolved_moment,
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
    assert store.save_transition(
        case=requested.case,
        expected_version=2,
        idempotency_key="runner:verification-request:001",
        timeline_events=requested.timeline_events,
        outbox_items=requested.outbox_items,
        artifacts=requested.artifacts,
    ).version == 3

    # The confirmation lands at the reported moment (after resolution occurred).
    clock.set(reported_moment)
    response = VerificationResponse(
        response_id="resident-confirmation-runner-001",
        request_id=requested.request.request_id,
        case_id=case.case_id,
        outcome=VerificationOutcome.CONFIRMED,
        actor=ActorType.RESIDENT,
        actor_label="Daniel K.",
        source_id="telegram-resident-confirmation-runner-001",
        responded_at=reported_moment,
        notes="The elevator now travels smoothly past the fourth floor.",
    )
    confirmed = CompletionVerificationService(
        clock=clock,
        playbooks=playbooks,
        id_factory=Ids("response"),
    ).respond(
        case=store.get_case(case.case_id),
        claim=claim,
        request=requested.request,
        response=response,
    )
    assert confirmed.resolution_evidence is not None
    assert store.save_transition(
        case=confirmed.case,
        expected_version=3,
        idempotency_key="runner:verification-response:001",
        timeline_events=confirmed.timeline_events,
        artifacts=confirmed.artifacts,
    ).version == 4

    quote, decision, batch = procurement_context(confirmed.case, claim, policy)
    VerifiedClosureCoordinator(
        store=store,
        clock=clock,
        id_factory=Ids("closure"),
    ).close(
        case=confirmed.case,
        expected_version=4,
        idempotency_key="runner:verified-close:001",
        decision=decision,
        rfq_batch=batch,
        quotes=(quote,),
        evidence=confirmed.resolution_evidence,
        raised_by=accepted.message.sender_display,
        raised_as=accepted.message.text,
    )

    timeline = store.timeline_for(case.case_id)
    kinds = [event.kind for event in timeline]
    confirmed_idx = kinds.index(EventKind.VERIFICATION_CONFIRMED)
    resolved_idx = kinds.index(EventKind.RESOLVED)
    # The causal fix: the confirmation that authorized closure is ordered before
    # the resolved event, because RESOLVED is now stamped at reported_at.
    assert confirmed_idx < resolved_idx
    resolved_event = timeline[resolved_idx]
    # The actual resolved_at survives in the payload even though the timeline
    # position uses reported_at.
    assert resolved_event.at == reported_moment
    assert resolved_event.payload["resolved_at"] == resolved_moment.isoformat()
    store.close()
