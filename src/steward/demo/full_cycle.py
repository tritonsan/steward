"""One-command Steward demo: problem, action, follow-up, resolution, memory.

Every resident, vendor, message, and outcome is synthetic.  RFQs and follow-ups
are captured by ``RecordingMailTransport``; no commitment message is created.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from steward.agents import (
    IntakeDisposition,
    IntakeService,
    QuoteExtractor,
    StrandsQuoteExtractor,
    StrandsTriageClassifier,
    TriageClassifier,
)
from steward.demo.quote_decision import DEFAULT_MODEL_ID, DEFAULT_REGION, SCHEME
from steward.domain.clock import FrozenClock
from steward.domain.enums import ActorType, CaseStatus, EventKind
from steward.domain.models import TimelineEvent
from steward.mail import InboundMessage, RecipientGuard, RecordingMailTransport
from steward.memory import InMemoryMemoryArchive, MemoryRecall
from steward.operations import (
    CompletionClaim,
    CompletionVerificationService,
    ExecutionMode,
    FollowUpCoordinator,
    ResolutionRecorder,
    VerificationOutcome,
    VerificationResponse,
)
from steward.policy import PolicyEngine, Rule
from steward.procurement import (
    CommitmentDisposition,
    QuoteDecisionBuilder,
    QuoteIngestion,
    QuoteIngestor,
    RecordingAuditSink,
    RfqBatch,
    RfqDispatcher,
)
from steward.seed import SeedBundle, load_seed
from steward.store import InMemoryCaseStore

OPENING_MESSAGE_IDS = (
    "tg:-1002481179934:88401",
    "tg:-1002481179934:88402",
    "tg:-1002481179934:88403",
    "tg:-1002481179934:88404",
)
INJECTION_MESSAGE_ID = "tg:-1002481179934:88408"


@dataclass(frozen=True, slots=True)
class DemoIntakeStep:
    message_id: str
    disposition: str
    case_id: str | None


@dataclass(frozen=True, slots=True)
class DemoQuote:
    quote_id: str
    vendor_id: str
    amount: str
    currency: str
    source_email_message_id: str | None


@dataclass(frozen=True, slots=True)
class DemoTimelineBeat:
    at: str
    kind: str
    summary: str
    refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FullCycleReport:
    intake_steps: tuple[DemoIntakeStep, ...]
    case_id: str
    historical_case_ids: tuple[str, ...]
    rfq_vendor_ids: tuple[str, ...]
    rfq_provider_message_ids: tuple[str, ...]
    quotes: tuple[DemoQuote, ...]
    nonresponding_vendor_ids: tuple[str, ...]
    recommended_vendor_id: str
    recommended_amount: str
    price_premium: str
    commitment_rule_id: str
    commitment_disposition: str
    commitment_sent: bool
    follow_up_count: int
    follow_up_provider_message_ids: tuple[str, ...]
    follow_up_audit_count: int
    escalated_at: str | None
    final_status: str
    resolution_cost: str
    resolution_source_ids: tuple[str, ...]
    memory_record_count_before: int
    memory_record_count_after: int
    memory_recall_case_ids: tuple[str, ...]
    meridian_jobs_before: int
    meridian_jobs_after: int
    timeline: tuple[DemoTimelineBeat, ...]
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "safe_mode": {
                "execution_mode": ExecutionMode.DRY_RUN.value,
                "rfq_transport": "RecordingMailTransport",
                "follow_up_transport": "RecordingMailTransport",
                "commitment_sent": self.commitment_sent,
                "synthetic_data": True,
            },
            "intake": [asdict(step) for step in self.intake_steps],
            "case": {
                "case_id": self.case_id,
                "historical_case_ids": list(self.historical_case_ids),
                "final_status": self.final_status,
            },
            "procurement": {
                "rfq_vendor_ids": list(self.rfq_vendor_ids),
                "rfq_provider_message_ids": list(self.rfq_provider_message_ids),
                "quotes": [asdict(quote) for quote in self.quotes],
                "nonresponding_vendor_ids": list(self.nonresponding_vendor_ids),
                "recommended_vendor_id": self.recommended_vendor_id,
                "recommended_amount": self.recommended_amount,
                "price_premium": self.price_premium,
                "commitment_rule_id": self.commitment_rule_id,
                "commitment_disposition": self.commitment_disposition,
                "commitment_sent": self.commitment_sent,
            },
            "follow_up": {
                "count": self.follow_up_count,
                "provider_message_ids": list(self.follow_up_provider_message_ids),
                "audit_count": self.follow_up_audit_count,
                "escalated_at": self.escalated_at,
                "delivered": False,
            },
            "resolution": {
                "cost": self.resolution_cost,
                "source_ids": list(self.resolution_source_ids),
                "simulated": True,
            },
            "memory": {
                "record_count_before": self.memory_record_count_before,
                "record_count_after": self.memory_record_count_after,
                "recall_case_ids": list(self.memory_recall_case_ids),
                "meridian_jobs_before": self.meridian_jobs_before,
                "meridian_jobs_after": self.meridian_jobs_after,
            },
            "timeline": [asdict(beat) for beat in self.timeline],
            "errors": list(self.errors),
        }


class _StableIds:
    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)

    def __call__(self, prefix: str) -> str:
        self._counts[prefix] += 1
        return f"{prefix}-full-demo-{self._counts[prefix]:03d}"


class _StableReplyTokens:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"{self._value:012x}"


def run_full_cycle_demo(
    triage_classifier: TriageClassifier,
    quote_extractor: QuoteExtractor,
    *,
    include_injection: bool = True,
    seed: SeedBundle | None = None,
) -> FullCycleReport:
    """Run the complete synthetic lifecycle; the two model ports are injected."""
    bundle = seed or load_seed()
    ids = _StableIds()
    archive = InMemoryMemoryArchive(
        list(bundle.history),
        recurrence_window_days=bundle.settings.recurrence_window_days,
    )
    memory_count_before = len(archive.records())
    store = InMemoryCaseStore()
    policy = PolicyEngine(bundle.policies, bundle.settings)
    by_message_id = {item.message.message_id: item for item in bundle.demo_messages}
    message_ids = (
        (*OPENING_MESSAGE_IDS, INJECTION_MESSAGE_ID) if include_injection else OPENING_MESSAGE_IDS
    )
    missing = [message_id for message_id in message_ids if message_id not in by_message_id]
    if missing:
        raise ValueError(f"seed is missing full-cycle messages: {', '.join(missing)}")

    intake_clock = FrozenClock(by_message_id[OPENING_MESSAGE_IDS[0]].message.ingested_at)
    intake = IntakeService(
        classifier=triage_classifier,
        store=store,
        policy=policy,
        assets=bundle.assets,
        clock=intake_clock,
        memory=archive,
        id_factory=ids,
        reply_token_factory=_StableReplyTokens(),
    )
    outcomes = []
    intake_steps: list[DemoIntakeStep] = []
    for message_id in message_ids:
        outcome = intake.process(by_message_id[message_id].message)
        outcomes.append(outcome)
        intake_steps.append(
            DemoIntakeStep(
                message_id=message_id,
                disposition=outcome.disposition.value,
                case_id=outcome.case.case_id if outcome.case else None,
            )
        )

    opening = outcomes[0]
    if opening.case is None or opening.memory_recall is None:
        raise RuntimeError("opening message did not produce a case with memory")
    current_case = store.get_case(opening.case.case_id)
    if current_case is None:
        raise RuntimeError("opened case disappeared from the store")
    memory = opening.memory_recall
    primary_message = by_message_id[OPENING_MESSAGE_IDS[0]].message
    rfq_at = current_case.opened_at + timedelta(minutes=18)

    guard = RecipientGuard.of(SCHEME.vendor_domain)
    rfq_transport = RecordingMailTransport(guard=guard)
    rfq_audit = RecordingAuditSink()
    batch = RfqDispatcher(
        policy=policy,
        transport=rfq_transport,
        recipient_guard=guard,
        address_scheme=SCHEME,
        audit_sink=rfq_audit,
        clock=FrozenClock(rfq_at),
        id_factory=ids,
    ).dispatch(
        case=current_case,
        triage_confidence=opening.triage.confidence,
        asset=_required_asset(bundle, current_case.asset_id),
        vendors=bundle.vendors,
    )

    ingestions, nonresponding = _ingest_demo_quotes(
        bundle=bundle,
        case=current_case,
        batch=batch,
        rfq_at=rfq_at,
        extractor=quote_extractor,
        id_factory=ids,
    )
    decision = QuoteDecisionBuilder(
        policy=policy,
        clock=FrozenClock(rfq_at + timedelta(hours=30)),
        id_factory=ids,
    ).build(
        case=current_case,
        triage_confidence=opening.triage.confidence,
        quotes=(item.quote for item in ingestions),
        vendors=bundle.vendors,
        memory=memory,
        nonresponding_vendor_ids=nonresponding,
    )

    winner = _required_vendor(bundle, decision.recommended_quote.vendor_id)
    follow_clock = FrozenClock(decision.created_at)
    follow_transport = RecordingMailTransport(guard=guard)
    follow_audit = RecordingAuditSink()
    coordinator = FollowUpCoordinator(
        policy=policy,
        transport=follow_transport,
        recipient_guard=guard,
        address_scheme=SCHEME,
        audit_sink=follow_audit,
        clock=follow_clock,
        execution_mode=ExecutionMode.DRY_RUN,
        id_factory=ids,
    )
    tracking = coordinator.start_tracking(
        case=current_case,
        decision=decision,
        vendor=winner,
        scheduled_for=decision.created_at + timedelta(hours=13),
        schedule_source_id="demo-schedule-confirmation-001",
    )
    current_case = tracking.case
    operational_events = list(tracking.timeline_events)
    follow_provider_ids: list[str] = []

    # Compress three days into three deterministic sweeps.  The first two
    # record reminders; the third escalates without sending anything.
    for _ in range(bundle.settings.max_follow_ups_before_escalation + 1):
        if current_case.next_action_due_at is None:
            break
        follow_clock.set(current_case.next_action_due_at)
        sweep = coordinator.sweep(
            case=current_case,
            decision=decision,
            vendor=winner,
            triage_confidence=opening.triage.confidence,
        )
        current_case = sweep.case
        operational_events.extend(sweep.timeline_events)
        follow_provider_ids.extend(sweep.provider_message_ids)

    if current_case.escalated_at is None:
        raise RuntimeError("full-cycle demo did not reach its escalation beat")
    completion_at = current_case.escalated_at + timedelta(hours=2)
    claim = CompletionClaim(
        claim_id="demo-completion-claim-001",
        case_id=current_case.case_id,
        source_id="demo-completion-report-001",
        vendor_id=winner.vendor_id,
        quote_id=decision.recommended_quote.quote_id,
        onsite_at=current_case.escalated_at + timedelta(hours=1),
        resolved_at=completion_at,
        claimed_at=completion_at,
        actual_cost=decision.recommended_quote.amount,
        currency=decision.recommended_quote.currency,
        work_performed=(
            "Corrected the fourth-to-fifth-floor rail bracket alignment and "
            "replaced the worn guide shoes."
        ),
        notes="Dry-run completion evidence for the synthetic hackathon scenario.",
        simulated=True,
    )
    verification_clock = FrozenClock(completion_at)
    verification_service = CompletionVerificationService(
        clock=verification_clock,
        id_factory=ids,
    )
    verification_request = verification_service.request(case=current_case, claim=claim)
    current_case = verification_request.case
    operational_events.extend(verification_request.timeline_events)
    verification_clock.advance(timedelta(hours=1))
    verification_response = VerificationResponse(
        response_id="demo-verification-response-001",
        request_id=verification_request.request.request_id,
        case_id=current_case.case_id,
        outcome=VerificationOutcome.CONFIRMED,
        actor=ActorType.RESIDENT,
        actor_label=primary_message.sender_display,
        source_id="demo-resident-confirmation-001",
        responded_at=verification_clock.now(),
        notes="The elevator is running smoothly now.",
    )
    verification_decision = verification_service.respond(
        case=current_case,
        claim=claim,
        request=verification_request.request,
        response=verification_response,
    )
    current_case = verification_decision.case
    operational_events.extend(verification_decision.timeline_events)
    evidence = verification_decision.resolution_evidence
    if evidence is None:
        raise RuntimeError("confirmed demo verification produced no resolution evidence")
    resolution = ResolutionRecorder(
        archive=archive,
        clock=FrozenClock(verification_clock.now()),
        id_factory=ids,
    ).record(
        case=current_case,
        decision=decision,
        rfq_batch=batch,
        quotes=(item.quote for item in ingestions),
        evidence=evidence,
        raised_by=primary_message.sender_display,
        raised_as=primary_message.text,
        problem=current_case.title,
    )
    current_case = resolution.case
    operational_events.extend(resolution.timeline_events)

    post_recall_at = current_case.closed_at + timedelta(seconds=1)
    post_recall = archive.recall(
        category=current_case.category,
        asset_id=current_case.asset_id,
        query_text="\n".join((primary_message.text, current_case.title)),
        as_of=post_recall_at,
    )
    reuse_event = TimelineEvent(
        event_id=ids("event"),
        case_id=current_case.case_id,
        at=post_recall_at,
        kind=EventKind.MEMORY_CONSULTED,
        actor=ActorType.SYSTEM,
        summary="A fresh recall immediately found the newly remembered outcome.",
        refs=[current_case.case_id],
        payload={"post_resolution_check": True},
    )
    operational_events.append(reuse_event)

    all_events = [
        *store.timeline_for(current_case.case_id),
        *_procurement_events(
            case_id=current_case.case_id,
            batch=batch,
            ingestions=ingestions,
            decision=decision,
            id_factory=ids,
        ),
        *operational_events,
    ]
    all_events.sort(key=lambda event: event.at)
    timeline = tuple(
        DemoTimelineBeat(
            at=event.at.isoformat(),
            kind=event.kind.value,
            summary=event.summary,
            refs=tuple(event.refs),
        )
        for event in all_events
    )
    before_jobs = _jobs_for(memory, "meridian-lift")
    after_jobs = _jobs_for(post_recall, "meridian-lift")
    errors = _acceptance_errors(
        include_injection=include_injection,
        intake_steps=tuple(intake_steps),
        case_count=len(store.list_cases()),
        batch=batch,
        ingestions=ingestions,
        nonresponding=nonresponding,
        decision=decision,
        follow_transport=follow_transport,
        follow_audit_count=len(follow_audit.entries),
        final_case=current_case,
        resolution=resolution,
        memory_count_before=memory_count_before,
        memory_count_after=len(archive.records()),
        post_recall=post_recall,
        before_jobs=before_jobs,
        after_jobs=after_jobs,
    )
    return FullCycleReport(
        intake_steps=tuple(intake_steps),
        case_id=current_case.case_id,
        historical_case_ids=memory.related_case_ids,
        rfq_vendor_ids=batch.contacted_vendor_ids,
        rfq_provider_message_ids=tuple(
            dispatch.provider_message_id for dispatch in batch.dispatches
        ),
        quotes=tuple(
            DemoQuote(
                quote_id=item.quote.quote_id,
                vendor_id=item.quote.vendor_id,
                amount=_money(item.quote.amount),
                currency=item.quote.currency,
                source_email_message_id=item.quote.source_email_message_id,
            )
            for item in ingestions
        ),
        nonresponding_vendor_ids=nonresponding,
        recommended_vendor_id=decision.recommended_quote.vendor_id,
        recommended_amount=_money(decision.recommended_quote.amount),
        price_premium=_money(decision.price_premium),
        commitment_rule_id=decision.commitment_decision.rule_id,
        commitment_disposition=decision.commitment_disposition.value,
        commitment_sent=decision.commitment_sent,
        follow_up_count=current_case.follow_up_count,
        follow_up_provider_message_ids=tuple(follow_provider_ids),
        follow_up_audit_count=len(follow_audit.entries),
        escalated_at=(current_case.escalated_at.isoformat() if current_case.escalated_at else None),
        final_status=current_case.status.value,
        resolution_cost=_money(resolution.record.cost),
        resolution_source_ids=tuple(resolution.record.resolution_source_ids),
        memory_record_count_before=memory_count_before,
        memory_record_count_after=len(archive.records()),
        memory_recall_case_ids=post_recall.related_case_ids,
        meridian_jobs_before=before_jobs,
        meridian_jobs_after=after_jobs,
        timeline=timeline,
        errors=tuple(errors),
    )


def _ingest_demo_quotes(
    *,
    bundle: SeedBundle,
    case,
    batch: RfqBatch,
    rfq_at: datetime,
    extractor: QuoteExtractor,
    id_factory,
) -> tuple[tuple[QuoteIngestion, ...], tuple[str, ...]]:
    vendors = {vendor.vendor_id: vendor for vendor in bundle.vendors}
    replies = {reply.vendor_id: reply for reply in bundle.vendor_replies}
    ingestor = QuoteIngestor(
        extractor=extractor,
        address_scheme=SCHEME,
        id_factory=id_factory,
    )
    ingestions: list[QuoteIngestion] = []
    nonresponding: list[str] = []
    for vendor_id in batch.contacted_vendor_ids:
        script = replies[vendor_id]
        if script.stays_silent:
            nonresponding.append(vendor_id)
            continue
        if script.delay_hours_after_rfq is None or script.body is None:
            raise ValueError(f"reply script is incomplete for {vendor_id}")
        vendor = vendors[vendor_id]
        inbound = InboundMessage(
            message_id=f"<full-demo-{vendor_id}@{SCHEME.vendor_domain}>",
            from_address=vendor.email,
            from_display_name=vendor.name,
            to_addresses=(SCHEME.case_reply_address(case.reply_token),),
            cc_addresses=(),
            delivered_to=SCHEME.case_reply_address(case.reply_token),
            subject=f"Re: Quote request: {case.title}",
            body_text=script.body,
            body_full_text=script.body,
            received_at=rfq_at + timedelta(hours=script.delay_hours_after_rfq),
            in_reply_to=next(
                dispatch.provider_message_id
                for dispatch in batch.dispatches
                if dispatch.vendor_id == vendor_id
            ),
        )
        ingestions.append(
            ingestor.ingest(
                case=case,
                vendor=vendor,
                inbound=inbound,
                rfq_sent_at=rfq_at,
            )
        )
    return tuple(ingestions), tuple(nonresponding)


def _procurement_events(
    *,
    case_id: str,
    batch: RfqBatch,
    ingestions: tuple[QuoteIngestion, ...],
    decision,
    id_factory,
) -> tuple[TimelineEvent, ...]:
    events = [
        TimelineEvent(
            event_id=id_factory("event"),
            case_id=case_id,
            at=batch.sent_at,
            kind=EventKind.RFQ_SENT,
            actor=ActorType.AGENT,
            summary=(
                f"Recorded RFQs for {len(batch.dispatches)} allowed vendors; "
                "no real email was delivered."
            ),
            refs=[dispatch.audit_id for dispatch in batch.dispatches],
            payload={
                "vendor_ids": list(batch.contacted_vendor_ids),
                "transport": "RecordingMailTransport",
                "delivered": False,
            },
        )
    ]
    for item in ingestions:
        quote = item.quote
        source_id = quote.source_email_message_id or quote.quote_id
        events.extend(
            (
                TimelineEvent(
                    event_id=id_factory("event"),
                    case_id=case_id,
                    at=quote.received_at,
                    kind=EventKind.VENDOR_REPLIED,
                    actor=ActorType.VENDOR,
                    actor_label=quote.vendor_id,
                    summary=f"Received a synthetic reply from {quote.vendor_id}.",
                    refs=[source_id],
                ),
                TimelineEvent(
                    event_id=id_factory("event"),
                    case_id=case_id,
                    at=quote.received_at,
                    kind=EventKind.QUOTE_RECORDED,
                    actor=ActorType.AGENT,
                    summary=(
                        f"Recorded source-backed quote of {_money(quote.amount)} "
                        f"{quote.currency} from {quote.vendor_id}."
                    ),
                    refs=[quote.quote_id, source_id],
                ),
            )
        )
    events.append(
        TimelineEvent(
            event_id=id_factory("event"),
            case_id=case_id,
            at=decision.created_at,
            kind=EventKind.COMMITMENT_AUTHORIZED,
            actor=ActorType.SYSTEM,
            summary=(
                f"Recommended {decision.recommended_quote.vendor_id} at "
                f"{_money(decision.recommended_quote.amount)} USD; authorized but not sent."
            ),
            refs=list(decision.source_ids),
            payload={
                "rule_id": decision.commitment_decision.rule_id,
                "disposition": decision.commitment_disposition.value,
                "commitment_sent": False,
            },
        )
    )
    return tuple(events)


def _acceptance_errors(
    *,
    include_injection,
    intake_steps,
    case_count,
    batch,
    ingestions,
    nonresponding,
    decision,
    follow_transport,
    follow_audit_count,
    final_case,
    resolution,
    memory_count_before,
    memory_count_after,
    post_recall,
    before_jobs,
    after_jobs,
) -> list[str]:
    errors: list[str] = []
    expected = [
        IntakeDisposition.OPENED.value,
        IntakeDisposition.LINKED.value,
        IntakeDisposition.LINKED.value,
        IntakeDisposition.IGNORED.value,
    ]
    if include_injection:
        expected.append(IntakeDisposition.IGNORED.value)
    actual = [step.disposition for step in intake_steps]
    if actual != expected:
        errors.append(f"intake sequence differed: expected {expected}, got {actual}")
    if case_count != 1:
        errors.append(f"full-cycle intake opened {case_count} cases instead of one")
    if batch.contacted_vendor_ids != (
        "meridian-lift",
        "coastline-elevator",
        "pinnacle-vertical",
    ):
        errors.append("RFQ batch did not contain the three approved elevator vendors")
    amounts = {item.quote.vendor_id: item.quote.amount for item in ingestions}
    if amounts != {
        "meridian-lift": Decimal("705.00"),
        "coastline-elevator": Decimal("540.00"),
    }:
        errors.append(f"quote amounts differed: {amounts}")
    if nonresponding != ("pinnacle-vertical",):
        errors.append(f"expected Pinnacle silence, got {nonresponding}")
    if decision.recommended_quote.vendor_id != "meridian-lift":
        errors.append("decision did not recommend Meridian")
    if decision.commitment_disposition is not CommitmentDisposition.AUTHORIZED_NOT_SENT:
        errors.append("decision was not AUTHORIZED_NOT_SENT")
    if decision.commitment_decision.rule_id != Rule.COMMIT_WITHIN_POLICY:
        errors.append("winner did not pass the exact commitment policy gate")
    if decision.commitment_sent:
        errors.append("a commitment was unexpectedly sent")
    expected_followups = 2
    if len(follow_transport.sent) != expected_followups:
        errors.append("follow-up loop did not record exactly two reminders")
    if follow_audit_count != expected_followups:
        errors.append("each recorded follow-up must have one pre-send audit")
    if any(
        "not delivered" not in message.body_text
        or "does not authorize work" not in message.body_text
        for message in follow_transport.sent
    ):
        errors.append("a follow-up lacked explicit dry-run/no-authorization language")
    if final_case.escalated_at is None:
        errors.append("unanswered follow-ups did not escalate")
    if final_case.status is not CaseStatus.CLOSED:
        errors.append(f"final case status was {final_case.status.value}, not closed")
    if resolution.record.is_simulated is not True:
        errors.append("demo outcome was not visibly marked as simulated")
    if resolution.record.resolution_source_ids != ["demo-completion-report-001"]:
        errors.append("resolution was not linked to its exact completion source")
    if resolution.record.verification_source_ids != ["demo-resident-confirmation-001"]:
        errors.append("resolution was not linked to resident verification")
    if memory_count_after != memory_count_before + 1:
        errors.append("resolution did not append exactly one memory record")
    if final_case.case_id not in post_recall.related_case_ids:
        errors.append("fresh memory recall did not find the newly closed case")
    if after_jobs != before_jobs + 1:
        errors.append(f"Meridian scorecard did not learn one job: {before_jobs} -> {after_jobs}")
    return errors


def _jobs_for(recall: MemoryRecall, vendor_id: str) -> int:
    for evidence in recall.vendor_scorecards:
        if evidence.scorecard.vendor_id == vendor_id:
            return evidence.scorecard.jobs_completed
    return 0


def _required_asset(bundle: SeedBundle, asset_id: str | None):
    if asset_id is None:
        raise ValueError("full-cycle case has no asset")
    asset = bundle.asset(asset_id)
    if asset is None:
        raise ValueError(f"unknown full-cycle asset: {asset_id}")
    return asset


def _required_vendor(bundle: SeedBundle, vendor_id: str):
    vendor = bundle.vendor(vendor_id)
    if vendor is None:
        raise ValueError(f"unknown full-cycle vendor: {vendor_id}")
    return vendor


TriageFactory = Callable[..., TriageClassifier]
QuoteFactory = Callable[..., QuoteExtractor]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="steward-demo",
        description=(
            "Run Steward's synthetic problem-to-memory lifecycle without sending real mail."
        ),
    )
    parser.add_argument(
        "--model-id",
        default=os.environ.get("STEWARD_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
    )
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "default"))
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION)),
    )
    parser.add_argument("--triage-max-tokens", type=_positive_int, default=512)
    parser.add_argument("--quote-max-tokens", type=_positive_int, default=768)
    parser.add_argument(
        "--without-injection",
        action="store_true",
        help="Skip the synthetic prompt-injection probe.",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    triage_factory: TriageFactory | None = None,
    quote_factory: QuoteFactory | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    triage_builder = triage_factory or StrandsTriageClassifier.bedrock
    quote_builder = quote_factory or StrandsQuoteExtractor.bedrock
    try:
        triage = triage_builder(
            args.model_id,
            profile_name=args.profile,
            region_name=args.region,
            temperature=0.0,
            max_tokens=args.triage_max_tokens,
        )
        quote = quote_builder(
            args.model_id,
            profile_name=args.profile,
            region_name=args.region,
            temperature=0.0,
            max_tokens=args.quote_max_tokens,
        )
        report = run_full_cycle_demo(
            triage,
            quote,
            include_injection=not args.without_injection,
        )
    except Exception as exc:
        payload = {
            "passed": False,
            "safe_mode": {
                "execution_mode": ExecutionMode.DRY_RUN.value,
                "rfq_transport": "RecordingMailTransport",
                "follow_up_transport": "RecordingMailTransport",
                "commitment_sent": False,
                "synthetic_data": True,
            },
            "model_id": args.model_id,
            "region": args.region,
            "error": f"{type(exc).__name__}: {exc}",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"FULL DEMO ERROR: {payload['error']}", file=sys.stderr)
        return 2

    payload = {
        "model_id": args.model_id,
        "region": args.region,
        **report.to_dict(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print("PASS" if report.passed else "FAIL")
        print("SAFE MODE: synthetic data; RFQs/follow-ups recorded; commitment not sent")
        print(
            f"Case {report.case_id}: Meridian {report.recommended_amount} USD "
            f"({report.price_premium} USD over cheapest)"
        )
        print(
            f"Follow-up: {report.follow_up_count} reminder(s), "
            f"escalated={report.escalated_at is not None}"
        )
        print(
            f"Memory learned: Meridian jobs {report.meridian_jobs_before} -> "
            f"{report.meridian_jobs_after}"
        )
        print("\nTIMELINE")
        for beat in report.timeline:
            print(f"{beat.at}  [{beat.kind}]  {beat.summary}")
        for error in report.errors:
            print(f"ERROR: {error}")
    return 0 if report.passed else 1


def _money(value: Decimal) -> str:
    return format(value, ".2f")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
