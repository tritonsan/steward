"""Source-traced resolution recording and institutional-memory write-back."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from steward.domain.clock import Clock
from steward.domain.enums import ActorType, CaseStatus, EventKind
from steward.domain.models import Case, Quote, TimelineEvent, UtcDatetime
from steward.memory.archive import MemoryArchive, MemoryConflictError
from steward.memory.records import CaseRecord, QuoteRecord
from steward.procurement import (
    CommitmentDisposition,
    QuoteDecisionPackage,
    RfqBatch,
)

__all__ = [
    "ResolutionDisposition",
    "ResolutionEvidence",
    "ResolutionIntegrityError",
    "ResolutionRecorder",
    "ResolutionResult",
]

IdFactory = Callable[[str], str]


class ResolutionIntegrityError(ValueError):
    """Outcome facts do not match the exact case, vendor, quote, or chronology."""


class ResolutionDisposition(str, Enum):
    RECORDED = "recorded"
    ALREADY_RECORDED = "already_recorded"


class ResolutionEvidence(BaseModel):
    """Facts supplied by a trusted completion adapter or a labelled demo."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1)
    vendor_id: str = Field(min_length=1)
    quote_id: str = Field(min_length=1)
    onsite_at: UtcDatetime
    resolved_at: UtcDatetime
    reported_at: UtcDatetime
    actual_cost: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    work_performed: str = Field(min_length=1, max_length=2000)
    notes: str = Field(default="", max_length=2000)
    verification_source_ids: tuple[str, ...] = ()
    simulated: bool = False

    @field_validator(
        "source_id",
        "vendor_id",
        "quote_id",
        "work_performed",
        "notes",
    )
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("verification_source_ids")
    @classmethod
    def _validate_verification_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned):
            raise ValueError("verification source ids must not be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("verification source ids must be unique")
        return cleaned

    @model_validator(mode="after")
    def _chronology(self) -> ResolutionEvidence:
        if self.resolved_at < self.onsite_at:
            raise ValueError("resolution cannot precede onsite attendance")
        if self.reported_at < self.resolved_at:
            raise ValueError("completion report cannot precede resolution")
        return self


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    case: Case
    record: CaseRecord
    disposition: ResolutionDisposition
    timeline_events: tuple[TimelineEvent, ...] = ()


class ResolutionRecorder:
    """Close an exact dry-run decision and append one structured memory record."""

    __slots__ = ("_archive", "_clock", "_id_factory")

    def __init__(
        self,
        *,
        archive: MemoryArchive,
        clock: Clock,
        id_factory: IdFactory = lambda prefix: f"{prefix}-{uuid4().hex}",
    ) -> None:
        self._archive = archive
        self._clock = clock
        self._id_factory = id_factory

    def record(
        self,
        *,
        case: Case,
        decision: QuoteDecisionPackage,
        rfq_batch: RfqBatch,
        quotes: Iterable[Quote],
        evidence: ResolutionEvidence,
        raised_by: str,
        raised_as: str,
        problem: str | None = None,
    ) -> ResolutionResult:
        """Validate, close, and write the case exactly once.

        This slice closes only explicitly simulated outcomes following an
        ``AUTHORIZED_NOT_SENT`` decision.  A future live completion adapter can
        share the archive contract without turning demo evidence into real work.
        """
        existing = self._archive.get(case.case_id)
        if existing is not None:
            return self._already_recorded(
                case=case,
                decision=decision,
                evidence=evidence,
                existing=existing,
            )

        quote_items = tuple(quote.model_copy(deep=True) for quote in quotes)
        self._validate(
            case=case,
            decision=decision,
            rfq_batch=rfq_batch,
            quotes=quote_items,
            evidence=evidence,
        )
        now = _aware_utc(self._clock.now())
        if evidence.reported_at > now:
            raise ResolutionIntegrityError("completion report cannot come from the future")

        selected = decision.recommended_quote
        rationale = " ".join(reason.summary for reason in decision.reasons)
        record = CaseRecord(
            case_id=case.case_id,
            title=case.title,
            category=case.category,
            asset_id=case.asset_id,
            urgency=case.urgency,
            is_simulated=evidence.simulated,
            outcome_verified=True,
            opened_at=case.opened_at,
            raised_by=raised_by,
            raised_as=raised_as,
            problem=problem or case.title,
            rfq_sent_at=rfq_batch.sent_at,
            vendors_contacted=list(rfq_batch.contacted_vendor_ids),
            quotes=[
                QuoteRecord(
                    quote_id=quote.quote_id,
                    source_email_message_id=quote.source_email_message_id,
                    vendor_id=quote.vendor_id,
                    amount=quote.amount,
                    first_response_at=quote.received_at,
                    earliest_onsite_at=quote.earliest_onsite_at,
                    scope=quote.scope,
                )
                for quote in quote_items
            ],
            selected_vendor_id=selected.vendor_id,
            selection_rationale=rationale,
            selection_source_ids=list(decision.source_ids),
            onsite_at=evidence.onsite_at,
            resolved_at=evidence.resolved_at,
            cost=evidence.actual_cost,
            currency=evidence.currency,
            work_performed=evidence.work_performed,
            resolution_notes=evidence.notes,
            resolution_source_ids=[evidence.source_id],
            verification_source_ids=list(evidence.verification_source_ids),
            closed_at=now,
        )
        try:
            written = self._archive.write(record)
        except MemoryConflictError as exc:
            raise ResolutionIntegrityError(str(exc)) from exc
        if not written:
            stored = self._archive.get(case.case_id)
            if stored is None:
                raise RuntimeError("memory write reported a duplicate without a stored record")
            return self._already_recorded(
                case=case,
                decision=decision,
                evidence=evidence,
                existing=stored,
            )

        closed = _closed_case(case, record, quote_id=selected.quote_id)
        quote_refs = [selected.quote_id]
        if selected.source_email_message_id:
            quote_refs.append(selected.source_email_message_id)
        resolved_event = TimelineEvent(
            event_id=self._id_factory("event"),
            case_id=case.case_id,
            at=evidence.reported_at,
            kind=EventKind.RESOLVED,
            actor=ActorType.VENDOR,
            actor_label=selected.vendor_id,
            summary=(f"Recorded completion by {selected.vendor_id}: {evidence.work_performed}"),
            refs=[evidence.source_id, *evidence.verification_source_ids, *quote_refs],
            payload={
                "simulated": evidence.simulated,
                "actual_cost": format(evidence.actual_cost, ".2f"),
                "currency": evidence.currency,
                "onsite_at": evidence.onsite_at.isoformat(),
                "resolved_at": evidence.resolved_at.isoformat(),
                "reported_at": evidence.reported_at.isoformat(),
            },
        )
        memory_event = TimelineEvent(
            event_id=self._id_factory("event"),
            case_id=case.case_id,
            at=now,
            kind=EventKind.MEMORY_WRITTEN,
            actor=ActorType.SYSTEM,
            summary="Wrote the source-traced outcome to institutional memory.",
            refs=[case.case_id, evidence.source_id, *evidence.verification_source_ids],
            payload={
                "selected_vendor_id": selected.vendor_id,
                "source_count": len(record.selection_source_ids)
                + len(record.resolution_source_ids)
                + len(record.verification_source_ids),
                "simulated": evidence.simulated,
            },
        )
        closed_event = TimelineEvent(
            event_id=self._id_factory("event"),
            case_id=case.case_id,
            at=now,
            kind=EventKind.CLOSED,
            actor=ActorType.SYSTEM,
            summary="Closed the case after its outcome was safely remembered.",
            refs=[case.case_id],
            payload={"memory_written": True},
        )
        return ResolutionResult(
            case=closed,
            record=record.model_copy(deep=True),
            disposition=ResolutionDisposition.RECORDED,
            timeline_events=(resolved_event, memory_event, closed_event),
        )

    @staticmethod
    def _validate(
        *,
        case: Case,
        decision: QuoteDecisionPackage,
        rfq_batch: RfqBatch,
        quotes: tuple[Quote, ...],
        evidence: ResolutionEvidence,
    ) -> None:
        selected = decision.recommended_quote
        if not evidence.verification_source_ids:
            raise ResolutionIntegrityError(
                "vendor completion claim requires resident or manager verification"
            )
        if not evidence.simulated:
            raise ResolutionIntegrityError(
                "AUTHORIZED_NOT_SENT can only produce an explicitly simulated outcome"
            )
        if decision.commitment_disposition is not CommitmentDisposition.AUTHORIZED_NOT_SENT:
            raise ResolutionIntegrityError("resolution requires an authorized dry-run decision")
        if decision.commitment_sent:
            raise ResolutionIntegrityError("dry-run decision unexpectedly reports a send")
        if decision.case_id != case.case_id or selected.case_id != case.case_id:
            raise ResolutionIntegrityError("case and quote decision do not match")
        if rfq_batch.case_id != case.case_id:
            raise ResolutionIntegrityError("RFQ batch belongs to a different case")
        if case.accepted_quote_id not in (None, selected.quote_id):
            raise ResolutionIntegrityError("case is bound to a different accepted quote")
        if evidence.vendor_id != selected.vendor_id or evidence.quote_id != selected.quote_id:
            raise ResolutionIntegrityError("completion evidence does not match the exact winner")
        if evidence.actual_cost != selected.amount or evidence.currency != selected.currency:
            raise ResolutionIntegrityError(
                "completion cost differs from the authorized quote; change approval is required"
            )
        if len({quote.quote_id for quote in quotes}) != len(quotes):
            raise ResolutionIntegrityError("resolution contains duplicate quote ids")
        if len({quote.vendor_id for quote in quotes}) != len(quotes):
            raise ResolutionIntegrityError("resolution contains duplicate vendor responses")
        contacted = set(rfq_batch.contacted_vendor_ids)
        if selected.vendor_id not in contacted:
            raise ResolutionIntegrityError("winning vendor was not contacted in the RFQ batch")
        if any(quote.vendor_id not in contacted for quote in quotes):
            raise ResolutionIntegrityError("resolution contains a quote from an uncontacted vendor")
        by_id = {quote.quote_id: quote for quote in quotes}
        if by_id.get(selected.quote_id) != selected:
            raise ResolutionIntegrityError("winning quote is missing or changed")
        if any(quote.case_id != case.case_id for quote in quotes):
            raise ResolutionIntegrityError("resolution contains a quote from another case")

        rfq_sent_at = _aware_utc(rfq_batch.sent_at)
        decision_at = _aware_utc(decision.created_at)
        if rfq_sent_at < case.opened_at:
            raise ResolutionIntegrityError("RFQ cannot precede the case")
        if decision_at < rfq_sent_at:
            raise ResolutionIntegrityError("decision cannot precede the RFQ")
        if any(quote.received_at < rfq_sent_at for quote in quotes):
            raise ResolutionIntegrityError("a quote cannot precede the RFQ")
        if any(quote.received_at > decision_at for quote in quotes):
            raise ResolutionIntegrityError("decision cannot precede a quote it considered")
        if evidence.onsite_at < decision_at:
            raise ResolutionIntegrityError("onsite attendance cannot precede authorization")

    def _already_recorded(
        self,
        *,
        case: Case,
        decision: QuoteDecisionPackage,
        evidence: ResolutionEvidence,
        existing: CaseRecord,
    ) -> ResolutionResult:
        selected = decision.recommended_quote
        exact_match = (
            existing.selected_vendor_id == selected.vendor_id
            and existing.quote_from(selected.vendor_id) is not None
            and existing.quote_from(selected.vendor_id).quote_id == selected.quote_id
            and existing.cost == evidence.actual_cost
            and existing.currency == evidence.currency
            and existing.work_performed == evidence.work_performed
            and evidence.source_id in existing.resolution_source_ids
            and set(evidence.verification_source_ids) == set(existing.verification_source_ids)
        )
        if not exact_match:
            raise ResolutionIntegrityError(
                f"memory already contains different facts for case {case.case_id}"
            )
        return ResolutionResult(
            case=_closed_case(case, existing, quote_id=selected.quote_id),
            record=existing.model_copy(deep=True),
            disposition=ResolutionDisposition.ALREADY_RECORDED,
        )


def _closed_case(case: Case, record: CaseRecord, *, quote_id: str) -> Case:
    closed = case.model_copy(deep=True)
    closed.status = CaseStatus.CLOSED
    closed.accepted_quote_id = quote_id
    closed.next_action_due_at = None
    closed.scheduled_for = record.onsite_at
    closed.resolved_at = record.resolved_at
    closed.closed_at = record.closed_at
    closed.total_cost = record.cost
    closed.currency = record.currency
    closed.resolution_notes = record.resolution_notes
    closed.updated_at = record.closed_at
    return closed


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ResolutionIntegrityError("resolution timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
