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
    EventKind,
    Urgency,
    group_of,
)
from steward.domain.models import AuditEntry, Case, Quote
from steward.mail import (
    AddressScheme,
    OutboundMessage,
    RecipientGuard,
    RecipientNotAllowed,
    RecordingMailTransport,
)
from steward.memory import InMemoryMemoryArchive, MemoryConflictError
from steward.operations import (
    ExecutionMode,
    FollowUpCoordinator,
    FollowUpDisposition,
    LiveExecutionDisabled,
    ResolutionDisposition,
    ResolutionEvidence,
    ResolutionIntegrityError,
    ResolutionRecorder,
)
from steward.policy import PolicyEngine, Rule
from steward.procurement import (
    CommitmentDisposition,
    DecisionReason,
    DecisionReasonCode,
    QuoteDecisionPackage,
    RecordingAuditSink,
    RfqBatch,
    RfqDispatch,
)
from steward.seed import load_seed

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
        return f"{prefix}-operations-{self.value:03d}"


class OrderedRecordingTransport(RecordingMailTransport):
    def __init__(self, *, guard, audit):
        super().__init__(guard=guard)
        self.audit = audit
        self.calls = 0

    def send(self, message):
        assert len(self.audit.entries) == self.calls + 1, "audit must precede transport"
        self.calls += 1
        return super().send(message)


def scenario():
    bundle = load_seed()
    policy = PolicyEngine(bundle.policies, bundle.settings)
    opened_at = datetime(2026, 9, 10, 9, tzinfo=UTC)
    rfq_at = opened_at + timedelta(hours=1)
    received_at = rfq_at + timedelta(hours=3)
    scheduled_for = datetime(2026, 9, 11, 15, tzinfo=UTC)
    case = Case(
        case_id="case-operations-001",
        reply_token="abcdef123456",
        title="A Block elevator shudders near floor four",
        category=Category.ELEVATOR,
        group=group_of(Category.ELEVATOR),
        urgency=Urgency.HIGH,
        asset_id="elevator-a",
        opened_at=opened_at,
        updated_at=opened_at,
        source_message_ids=["resident-message-001"],
        currency="USD",
    )
    quote = Quote(
        quote_id="quote-meridian-001",
        case_id=case.case_id,
        vendor_id="meridian-lift",
        amount=Decimal("705.00"),
        currency="USD",
        scope="check and correct the fourth-to-fifth-floor rail bracket alignment",
        earliest_onsite_at=scheduled_for,
        received_at=received_at,
        source_email_message_id="<meridian-quote-001@vendors.narrativenode-labs.cloud>",
    )
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
        nonresponding_vendor_ids=("pinnacle-vertical",),
        price_premium=Decimal("165.00"),
        reasons=(
            DecisionReason(
                code=DecisionReasonCode.KNOWN_CAUSE_COVERED,
                summary="Meridian covers the known rail alignment cause.",
                source_ids=("hist-2026-001", quote.quote_id),
            ),
        ),
        commitment_decision=commitment,
        commitment_disposition=CommitmentDisposition.AUTHORIZED_NOT_SENT,
        commitment_audit=AuditEntry(
            audit_id="audit-commit-001",
            case_id=case.case_id,
            at=received_at + timedelta(hours=1),
            action="commit_quote_dry_run",
            autonomy_level=AutonomyLevel.AUTONOMOUS,
            policy_rule_id=Rule.COMMIT_WITHIN_POLICY,
            reason=commitment.reason,
            amount=quote.amount,
            vendor_id=quote.vendor_id,
            actor=ActorType.AGENT,
        ),
        created_at=received_at + timedelta(hours=1),
    )
    message = OutboundMessage(
        to=("quotes@meridian-lift.vendors.narrativenode-labs.cloud",),
        subject="Quote request",
        body_text="Request for quote only.",
        from_address=SCHEME.management_address,
    )
    batch = RfqBatch(
        case_id=case.case_id,
        sent_at=rfq_at,
        policy_decision=policy.evaluate_intake(
            category=case.category,
            triage_confidence=0.95,
        ),
        dispatches=(
            RfqDispatch(
                vendor_id="meridian-lift",
                provider_message_id="recorded-rfq-001",
                audit_id="audit-rfq-001",
                message=message,
            ),
        ),
        exclusions=(),
    )
    vendor = bundle.vendor("meridian-lift")
    assert vendor is not None
    return bundle, policy, case, quote, decision, batch, vendor


def test_follow_up_records_two_guarded_reminders_once_then_escalates():
    bundle, policy, case, _quote, decision, _batch, vendor = scenario()
    clock = FrozenClock(decision.created_at)
    guard = RecipientGuard.of(SCHEME.vendor_domain)
    audit = RecordingAuditSink()
    transport = OrderedRecordingTransport(guard=guard, audit=audit)
    coordinator = FollowUpCoordinator(
        policy=policy,
        transport=transport,
        recipient_guard=guard,
        address_scheme=SCHEME,
        audit_sink=audit,
        clock=clock,
        execution_mode=ExecutionMode.DRY_RUN,
        id_factory=Ids(),
    )
    with pytest.raises(ValueError, match="needs a source id"):
        coordinator.start_tracking(
            case=case,
            decision=decision,
            vendor=vendor,
            scheduled_for=decision.created_at + timedelta(days=2),
        )

    started = coordinator.start_tracking(case=case, decision=decision, vendor=vendor)
    assert started.disposition is FollowUpDisposition.TRACKING_STARTED
    assert started.case.status is CaseStatus.SCHEDULED
    expected_due = decision.recommended_quote.earliest_onsite_at + timedelta(
        hours=bundle.settings.follow_up_grace_hours
    )
    assert started.case.next_action_due_at == expected_due
    assert transport.sent == []
    repeated_start = coordinator.start_tracking(
        case=started.case,
        decision=decision,
        vendor=vendor,
    )
    assert repeated_start.disposition is FollowUpDisposition.ALREADY_TRACKING
    assert repeated_start.timeline_events == ()

    before_due = coordinator.sweep(
        case=started.case,
        decision=decision,
        vendor=vendor,
        triage_confidence=0.95,
    )
    assert before_due.disposition is FollowUpDisposition.NOT_DUE

    clock.set(started.case.next_action_due_at)
    first = coordinator.sweep(
        case=started.case,
        decision=decision,
        vendor=vendor,
        triage_confidence=0.95,
    )
    assert first.disposition is FollowUpDisposition.FOLLOW_UP_RECORDED
    assert first.case.follow_up_count == 1
    assert first.timeline_events[0].kind is EventKind.FOLLOW_UP_SENT
    assert len(audit.entries) == len(transport.sent) == 1
    assert "not delivered" in transport.sent[0].body_text
    assert "does not authorize work" in transport.sent[0].body_text

    repeated = coordinator.sweep(
        case=first.case,
        decision=decision,
        vendor=vendor,
        triage_confidence=0.95,
    )
    assert repeated.disposition is FollowUpDisposition.NOT_DUE
    assert len(transport.sent) == 1

    clock.set(first.case.next_action_due_at)
    second = coordinator.sweep(
        case=first.case,
        decision=decision,
        vendor=vendor,
        triage_confidence=0.95,
    )
    assert second.case.follow_up_count == 2
    assert len(audit.entries) == len(transport.sent) == 2

    clock.set(second.case.next_action_due_at)
    escalated = coordinator.sweep(
        case=second.case,
        decision=decision,
        vendor=vendor,
        triage_confidence=0.95,
    )
    assert escalated.disposition is FollowUpDisposition.ESCALATED
    assert escalated.case.status is CaseStatus.AWAITING_APPROVAL
    assert escalated.case.escalated_at == clock.now()
    assert escalated.case.next_action_due_at is None
    assert escalated.timeline_events[0].kind is EventKind.ESCALATED
    assert len(transport.sent) == 2

    inactive = coordinator.sweep(
        case=escalated.case,
        decision=decision,
        vendor=vendor,
        triage_confidence=0.95,
    )
    assert inactive.disposition is FollowUpDisposition.INACTIVE
    assert len(transport.sent) == 2


def test_follow_up_live_mode_and_poisoned_recipient_fail_closed_before_audit():
    _bundle, policy, case, _quote, decision, _batch, vendor = scenario()
    guard = RecipientGuard.of(SCHEME.vendor_domain)
    audit = RecordingAuditSink()
    transport = RecordingMailTransport(guard=guard)

    with pytest.raises(LiveExecutionDisabled):
        FollowUpCoordinator(
            policy=policy,
            transport=transport,
            recipient_guard=guard,
            address_scheme=SCHEME,
            audit_sink=audit,
            clock=FrozenClock(decision.created_at),
            execution_mode=ExecutionMode.LIVE,
        )

    clock = FrozenClock(decision.created_at)
    coordinator = FollowUpCoordinator(
        policy=policy,
        transport=transport,
        recipient_guard=guard,
        address_scheme=SCHEME,
        audit_sink=audit,
        clock=clock,
        id_factory=Ids(),
    )
    started = coordinator.start_tracking(case=case, decision=decision, vendor=vendor)
    clock.set(started.case.next_action_due_at)
    poisoned = vendor.model_copy(update={"email": "attacker@outside.example"})

    with pytest.raises(RecipientNotAllowed):
        coordinator.sweep(
            case=started.case,
            decision=decision,
            vendor=poisoned,
            triage_confidence=0.95,
        )

    assert audit.entries == []
    assert transport.sent == []


def test_policy_kill_switch_escalates_follow_up_without_recording_mail():
    bundle, original_policy, case, _quote, decision, _batch, vendor = scenario()
    guard = RecipientGuard.of(SCHEME.vendor_domain)
    initial = FollowUpCoordinator(
        policy=original_policy,
        transport=RecordingMailTransport(guard=guard),
        recipient_guard=guard,
        address_scheme=SCHEME,
        audit_sink=RecordingAuditSink(),
        clock=FrozenClock(decision.created_at),
        id_factory=Ids(),
    ).start_tracking(case=case, decision=decision, vendor=vendor)

    stopped_policy = PolicyEngine(
        bundle.policies,
        bundle.settings.model_copy(update={"kill_switch": True}),
    )
    clock = FrozenClock(initial.case.next_action_due_at)
    audit = RecordingAuditSink()
    transport = RecordingMailTransport(guard=guard)
    stopped = FollowUpCoordinator(
        policy=stopped_policy,
        transport=transport,
        recipient_guard=guard,
        address_scheme=SCHEME,
        audit_sink=audit,
        clock=clock,
        id_factory=Ids(),
    ).sweep(
        case=initial.case,
        decision=decision,
        vendor=vendor,
        triage_confidence=0.95,
    )

    assert stopped.disposition is FollowUpDisposition.ESCALATED
    assert stopped.case.escalated_at == clock.now()
    assert audit.entries == []
    assert transport.sent == []


def test_resolution_writes_source_traced_memory_learns_and_is_idempotent():
    bundle, _policy, case, quote, decision, batch, _vendor = scenario()
    archive = InMemoryMemoryArchive(
        list(bundle.history),
        recurrence_window_days=bundle.settings.recurrence_window_days,
    )
    before_count = len(archive.records())
    before = archive.recall(
        category=case.category,
        asset_id=case.asset_id,
        query_text=case.title,
        as_of=decision.created_at,
    )
    before_jobs = next(
        item.scorecard.jobs_completed
        for item in before.vendor_scorecards
        if item.scorecard.vendor_id == "meridian-lift"
    )
    evidence = ResolutionEvidence(
        source_id="completion-report-001",
        vendor_id=quote.vendor_id,
        quote_id=quote.quote_id,
        onsite_at=quote.earliest_onsite_at + timedelta(days=2),
        resolved_at=quote.earliest_onsite_at + timedelta(days=2, hours=2),
        reported_at=quote.earliest_onsite_at + timedelta(days=2, hours=2),
        actual_cost=quote.amount,
        currency=quote.currency,
        work_performed="Corrected rail bracket alignment and replaced worn guide shoes.",
        notes="Synthetic completion.",
        verification_source_ids=("resident-confirmation-001",),
        simulated=True,
    )
    tracked_case = case.model_copy(
        update={
            "accepted_quote_id": quote.quote_id,
            "status": CaseStatus.AWAITING_APPROVAL,
        },
        deep=True,
    )
    recorder = ResolutionRecorder(
        archive=archive,
        clock=FrozenClock(evidence.reported_at),
        id_factory=Ids(),
    )

    result = recorder.record(
        case=tracked_case,
        decision=decision,
        rfq_batch=batch,
        quotes=(quote,),
        evidence=evidence,
        raised_by="Synthetic Resident",
        raised_as="The elevator is shuddering again.",
    )

    assert result.disposition is ResolutionDisposition.RECORDED
    assert result.case.status is CaseStatus.CLOSED
    assert result.case.total_cost == Decimal("705.00")
    assert result.record.is_simulated is True
    assert result.record.selection_source_ids == ["hist-2026-001", quote.quote_id]
    assert result.record.resolution_source_ids == ["completion-report-001"]
    assert result.record.quotes[0].quote_id == quote.quote_id
    assert result.record.quotes[0].source_email_message_id == quote.source_email_message_id
    assert [event.kind for event in result.timeline_events] == [
        EventKind.RESOLVED,
        EventKind.MEMORY_WRITTEN,
        EventKind.CLOSED,
    ]
    assert len(archive.records()) == before_count + 1

    after = archive.recall(
        category=case.category,
        asset_id=case.asset_id,
        query_text=case.title,
        as_of=result.case.closed_at + timedelta(seconds=1),
    )
    after_jobs = next(
        item.scorecard.jobs_completed
        for item in after.vendor_scorecards
        if item.scorecard.vendor_id == "meridian-lift"
    )
    assert case.case_id in after.related_case_ids
    assert after_jobs == before_jobs + 1

    duplicate = recorder.record(
        case=tracked_case,
        decision=decision,
        rfq_batch=batch,
        quotes=(quote,),
        evidence=evidence,
        raised_by="Synthetic Resident",
        raised_as="The elevator is shuddering again.",
    )
    assert duplicate.disposition is ResolutionDisposition.ALREADY_RECORDED
    assert duplicate.timeline_events == ()
    assert len(archive.records()) == before_count + 1

    defensive = archive.get(case.case_id)
    assert defensive is not None
    defensive.work_performed = "tampered outside the archive"
    assert archive.get(case.case_id).work_performed == result.record.work_performed


def test_resolution_rejects_unlabelled_or_changed_cost_and_archive_conflicts():
    bundle, _policy, case, quote, decision, batch, _vendor = scenario()
    archive = InMemoryMemoryArchive(list(bundle.history))
    recorder = ResolutionRecorder(
        archive=archive,
        clock=FrozenClock(datetime(2026, 9, 15, 18, tzinfo=UTC)),
        id_factory=Ids(),
    )
    base = ResolutionEvidence(
        source_id="completion-report-002",
        vendor_id=quote.vendor_id,
        quote_id=quote.quote_id,
        onsite_at=datetime(2026, 9, 15, 15, tzinfo=UTC),
        resolved_at=datetime(2026, 9, 15, 17, tzinfo=UTC),
        reported_at=datetime(2026, 9, 15, 17, tzinfo=UTC),
        actual_cost=quote.amount,
        currency=quote.currency,
        work_performed="Corrected bracket alignment.",
        verification_source_ids=("manager-confirmation-002",),
        simulated=True,
    )

    with pytest.raises(ResolutionIntegrityError, match="resident or manager verification"):
        recorder.record(
            case=case,
            decision=decision,
            rfq_batch=batch,
            quotes=(quote,),
            evidence=base.model_copy(update={"verification_source_ids": ()}),
            raised_by="Synthetic Resident",
            raised_as="Elevator issue.",
        )
    with pytest.raises(ResolutionIntegrityError, match="change approval"):
        recorder.record(
            case=case,
            decision=decision,
            rfq_batch=batch,
            quotes=(quote,),
            evidence=base.model_copy(update={"actual_cost": Decimal("706.00")}),
            raised_by="Synthetic Resident",
            raised_as="Elevator issue.",
        )
    with pytest.raises(ResolutionIntegrityError, match="explicitly simulated"):
        recorder.record(
            case=case,
            decision=decision,
            rfq_batch=batch,
            quotes=(quote,),
            evidence=base.model_copy(update={"simulated": False}),
            raised_by="Synthetic Resident",
            raised_as="Elevator issue.",
        )
    with pytest.raises(ResolutionIntegrityError, match="precede authorization"):
        recorder.record(
            case=case,
            decision=decision,
            rfq_batch=batch,
            quotes=(quote,),
            evidence=base.model_copy(
                update={"onsite_at": decision.created_at - timedelta(minutes=1)}
            ),
            raised_by="Synthetic Resident",
            raised_as="Elevator issue.",
        )
    uncontacted = quote.model_copy(
        update={
            "quote_id": "quote-uncontacted-001",
            "vendor_id": "coastline-elevator",
            "source_email_message_id": "<uncontacted@example.test>",
        }
    )
    with pytest.raises(ResolutionIntegrityError, match="uncontacted vendor"):
        recorder.record(
            case=case,
            decision=decision,
            rfq_batch=batch,
            quotes=(quote, uncontacted),
            evidence=base,
            raised_by="Synthetic Resident",
            raised_as="Elevator issue.",
        )

    original = bundle.history[0]
    assert archive.write(original) is False
    conflicting = original.model_copy(update={"resolution_notes": "conflicting facts"})
    with pytest.raises(MemoryConflictError):
        archive.write(conflicting)
