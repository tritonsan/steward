"""Human verification boundary between a vendor claim and remembered success."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from steward.domain.clock import Clock
from steward.domain.enums import TERMINAL_STATUSES, ActorType, CaseStatus, EventKind
from steward.domain.models import Case, TimelineEvent, UtcDatetime
from steward.operations.resolution import ResolutionEvidence
from steward.playbooks import PlaybookCatalog
from steward.store.contracts import OutboxItem, WorkflowArtifact

__all__ = [
    "CompletionClaim",
    "CompletionVerificationService",
    "VerificationDecision",
    "VerificationIntegrityError",
    "VerificationOutcome",
    "VerificationRequest",
    "VerificationRequested",
    "VerificationResponse",
    "WarrantyClaim",
]

IdFactory = Callable[[str], str]


class VerificationIntegrityError(ValueError):
    """Verification facts do not belong to the same exact completion claim."""


class VerificationOutcome(str, Enum):
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    REJECTED = "rejected"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class CompletionClaim(_Record):
    claim_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    vendor_id: str = Field(min_length=1)
    quote_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    claimed_at: UtcDatetime
    onsite_at: UtcDatetime
    resolved_at: UtcDatetime
    actual_cost: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    work_performed: str = Field(min_length=1, max_length=2000)
    notes: str = Field(default="", max_length=2000)
    simulated: bool = False

    @field_validator(
        "claim_id",
        "case_id",
        "vendor_id",
        "quote_id",
        "source_id",
        "work_performed",
        "notes",
    )
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def _chronology(self) -> CompletionClaim:
        if self.resolved_at < self.onsite_at:
            raise ValueError("claimed resolution cannot precede onsite attendance")
        if self.claimed_at < self.resolved_at:
            raise ValueError("vendor claim cannot precede claimed resolution")
        return self


class VerificationRequest(_Record):
    request_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    requested_at: UtcDatetime
    due_at: UtcDatetime
    allowed_actors: tuple[ActorType, ...] = (
        ActorType.RESIDENT,
        ActorType.HUMAN,
    )
    source_ids: tuple[str, ...]

    @model_validator(mode="after")
    def _valid_request(self) -> VerificationRequest:
        if self.due_at <= self.requested_at:
            raise ValueError("verification deadline must follow its request")
        if not self.source_ids:
            raise ValueError("verification request needs source ids")
        if not self.allowed_actors:
            raise ValueError("verification request needs at least one allowed actor")
        return self


class VerificationResponse(_Record):
    response_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    outcome: VerificationOutcome
    actor: ActorType
    actor_label: str = Field(min_length=1, max_length=120)
    source_id: str = Field(min_length=1)
    responded_at: UtcDatetime
    notes: str = Field(default="", max_length=2000)

    @field_validator("actor_label", "source_id", "notes")
    @classmethod
    def _strip_response_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _human_actor(self) -> VerificationResponse:
        if self.actor not in (ActorType.RESIDENT, ActorType.HUMAN):
            raise ValueError("completion verification must come from a resident or manager")
        return self


class WarrantyClaim(_Record):
    warranty_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    completion_claim_id: str = Field(min_length=1)
    vendor_id: str = Field(min_length=1)
    quote_id: str = Field(min_length=1)
    opened_at: UtcDatetime
    outcome: VerificationOutcome
    reason: str = Field(min_length=1, max_length=2000)
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerificationRequested:
    case: Case
    claim: CompletionClaim
    request: VerificationRequest
    timeline_events: tuple[TimelineEvent, ...]
    outbox_items: tuple[OutboxItem, ...]
    artifacts: tuple[WorkflowArtifact, ...]


@dataclass(frozen=True, slots=True)
class VerificationDecision:
    case: Case
    response: VerificationResponse
    resolution_evidence: ResolutionEvidence | None
    warranty_claim: WarrantyClaim | None
    timeline_events: tuple[TimelineEvent, ...]
    outbox_items: tuple[OutboxItem, ...]
    artifacts: tuple[WorkflowArtifact, ...]


class CompletionVerificationService:
    """Require a resident or manager before a vendor claim can close a case."""

    __slots__ = ("_clock", "_id_factory", "_playbooks", "_timeout")

    def __init__(
        self,
        *,
        clock: Clock,
        verification_timeout_hours: int = 48,
        playbooks: PlaybookCatalog | None = None,
        id_factory: IdFactory = lambda prefix: f"{prefix}-{uuid4().hex}",
    ) -> None:
        if verification_timeout_hours <= 0:
            raise ValueError("verification timeout must be positive")
        self._clock = clock
        self._timeout = timedelta(hours=verification_timeout_hours)
        self._playbooks = playbooks
        self._id_factory = id_factory

    def request(self, *, case: Case, claim: CompletionClaim) -> VerificationRequested:
        if case.status in TERMINAL_STATUSES:
            raise VerificationIntegrityError("a terminal case cannot request verification")
        if claim.case_id != case.case_id:
            raise VerificationIntegrityError("completion claim belongs to a different case")
        if case.accepted_quote_id != claim.quote_id:
            raise VerificationIntegrityError("completion claim does not match the accepted quote")
        now = self._clock.now()
        if claim.claimed_at > now:
            raise VerificationIntegrityError("completion claim cannot come from the future")

        playbook = self._playbooks.for_category(case.category) if self._playbooks else None
        if playbook is not None:
            try:
                playbook.validate_completion_facts(claim.model_dump(mode="python"))
            except ValueError as exc:
                raise VerificationIntegrityError(str(exc)) from exc
        timeout = (
            timedelta(hours=playbook.verification_timeout_hours)
            if playbook is not None
            else self._timeout
        )
        allowed_actors = (
            playbook.allowed_verifiers
            if playbook is not None
            else (
                ActorType.RESIDENT,
                ActorType.HUMAN,
            )
        )
        request = VerificationRequest(
            request_id=self._id_factory("verification-request"),
            case_id=case.case_id,
            claim_id=claim.claim_id,
            requested_at=now,
            due_at=now + timeout,
            allowed_actors=allowed_actors,
            source_ids=(claim.claim_id, claim.source_id, claim.quote_id),
        )
        updated = case.model_copy(deep=True)
        updated.status = CaseStatus.AWAITING_VERIFICATION
        updated.resolved_at = claim.resolved_at
        updated.next_action_due_at = request.due_at
        updated.updated_at = now
        claim_event = TimelineEvent(
            event_id=self._id_factory("event"),
            case_id=case.case_id,
            at=claim.claimed_at,
            kind=EventKind.COMPLETION_CLAIMED,
            actor=ActorType.VENDOR,
            actor_label=claim.vendor_id,
            summary=f"{claim.vendor_id} reported the work complete; closure is not yet verified.",
            refs=[claim.source_id, claim.quote_id],
            payload={
                "claim_id": claim.claim_id,
                "actual_cost": format(claim.actual_cost, ".2f"),
                "currency": claim.currency,
                "simulated": claim.simulated,
            },
        )
        request_event = TimelineEvent(
            event_id=self._id_factory("event"),
            case_id=case.case_id,
            at=now,
            kind=EventKind.VERIFICATION_REQUESTED,
            actor=ActorType.SYSTEM,
            summary="Requested resident or management confirmation before closing the case.",
            refs=list(request.source_ids),
            payload={
                "request_id": request.request_id,
                "due_at": request.due_at.isoformat(),
                "vendor_claim_is_not_closure": True,
            },
        )
        outbox = OutboxItem(
            outbox_id=self._id_factory("outbox"),
            case_id=case.case_id,
            dedup_key=f"{case.case_id}:verification:{claim.claim_id}",
            kind="request_completion_verification",
            payload={
                "case_id": case.case_id,
                "request_id": request.request_id,
                "claim_id": claim.claim_id,
                "audience": "resident_or_manager",
                "outbound_channel_selected": False,
            },
            created_at=now,
        )
        return VerificationRequested(
            case=updated,
            claim=claim,
            request=request,
            timeline_events=(claim_event, request_event),
            outbox_items=(outbox,),
            artifacts=(
                _artifact(
                    "completion_claim",
                    claim.claim_id,
                    case.case_id,
                    claim.claimed_at,
                    claim,
                ),
                _artifact(
                    "verification_request",
                    request.request_id,
                    case.case_id,
                    request.requested_at,
                    request,
                ),
            ),
        )

    def respond(
        self,
        *,
        case: Case,
        claim: CompletionClaim,
        request: VerificationRequest,
        response: VerificationResponse,
    ) -> VerificationDecision:
        self._validate_response(case=case, claim=claim, request=request, response=response)
        now = self._clock.now()
        if response.responded_at > now:
            raise VerificationIntegrityError("verification response cannot come from the future")

        response_artifact = _artifact(
            "verification_response",
            response.response_id,
            case.case_id,
            response.responded_at,
            response,
        )
        if response.outcome is VerificationOutcome.CONFIRMED:
            updated = case.model_copy(deep=True)
            updated.status = CaseStatus.RESOLVED
            updated.next_action_due_at = None
            updated.resolved_at = claim.resolved_at
            updated.updated_at = now
            if response.notes:
                updated.resolution_notes = response.notes
            event = TimelineEvent(
                event_id=self._id_factory("event"),
                case_id=case.case_id,
                at=response.responded_at,
                kind=EventKind.VERIFICATION_CONFIRMED,
                actor=response.actor,
                actor_label=response.actor_label,
                summary="A resident or manager confirmed that the claimed work resolved the issue.",
                refs=[response.source_id, claim.source_id, claim.quote_id],
                payload={
                    "request_id": request.request_id,
                    "response_id": response.response_id,
                    "outcome": response.outcome.value,
                },
            )
            evidence = ResolutionEvidence(
                source_id=claim.source_id,
                vendor_id=claim.vendor_id,
                quote_id=claim.quote_id,
                onsite_at=claim.onsite_at,
                resolved_at=claim.resolved_at,
                reported_at=response.responded_at,
                actual_cost=claim.actual_cost,
                currency=claim.currency,
                work_performed=claim.work_performed,
                notes=" ".join(value for value in (claim.notes, response.notes) if value),
                verification_source_ids=(response.source_id,),
                simulated=claim.simulated,
            )
            return VerificationDecision(
                case=updated,
                response=response,
                resolution_evidence=evidence,
                warranty_claim=None,
                timeline_events=(event,),
                outbox_items=(),
                artifacts=(response_artifact,),
            )

        updated = case.model_copy(deep=True)
        updated.status = CaseStatus.WARRANTY_REVIEW
        updated.resolved_at = None
        updated.closed_at = None
        updated.next_action_due_at = now
        updated.follow_up_count = 0
        updated.escalated_at = None
        updated.updated_at = now
        updated.resolution_notes = response.notes
        warranty = WarrantyClaim(
            warranty_id=self._id_factory("warranty"),
            case_id=case.case_id,
            completion_claim_id=claim.claim_id,
            vendor_id=claim.vendor_id,
            quote_id=claim.quote_id,
            opened_at=now,
            outcome=response.outcome,
            reason=response.notes or "The claimed resolution was not confirmed.",
            source_ids=(response.source_id, claim.source_id, claim.quote_id),
        )
        rejected_event = TimelineEvent(
            event_id=self._id_factory("event"),
            case_id=case.case_id,
            at=response.responded_at,
            kind=EventKind.VERIFICATION_REJECTED,
            actor=response.actor,
            actor_label=response.actor_label,
            summary=(
                "The claimed resolution was only partial."
                if response.outcome is VerificationOutcome.PARTIAL
                else "The claimed resolution was rejected."
            ),
            refs=list(warranty.source_ids),
            payload={"outcome": response.outcome.value, "response_id": response.response_id},
        )
        warranty_event = TimelineEvent(
            event_id=self._id_factory("event"),
            case_id=case.case_id,
            at=now,
            kind=EventKind.WARRANTY_OPENED,
            actor=ActorType.SYSTEM,
            summary="Opened a warranty/recall review instead of counting the job as successful.",
            refs=[warranty.warranty_id, *warranty.source_ids],
            payload={
                "vendor_id": claim.vendor_id,
                "quote_id": claim.quote_id,
                "vendor_contact_allowed": False,
            },
        )
        outbox = OutboxItem(
            outbox_id=self._id_factory("outbox"),
            case_id=case.case_id,
            dedup_key=f"{case.case_id}:warranty:{warranty.warranty_id}",
            kind="warranty_review_required",
            payload={
                "case_id": case.case_id,
                "warranty_id": warranty.warranty_id,
                "vendor_id": claim.vendor_id,
                "vendor_contact_allowed": False,
            },
            created_at=now,
        )
        return VerificationDecision(
            case=updated,
            response=response,
            resolution_evidence=None,
            warranty_claim=warranty,
            timeline_events=(rejected_event, warranty_event),
            outbox_items=(outbox,),
            artifacts=(
                response_artifact,
                _artifact(
                    "warranty_claim",
                    warranty.warranty_id,
                    case.case_id,
                    warranty.opened_at,
                    warranty,
                ),
            ),
        )

    @staticmethod
    def _validate_response(
        *,
        case: Case,
        claim: CompletionClaim,
        request: VerificationRequest,
        response: VerificationResponse,
    ) -> None:
        if case.status is not CaseStatus.AWAITING_VERIFICATION:
            raise VerificationIntegrityError("case is not awaiting completion verification")
        if claim.case_id != case.case_id or request.case_id != case.case_id:
            raise VerificationIntegrityError("verification records belong to another case")
        if response.case_id != case.case_id or response.request_id != request.request_id:
            raise VerificationIntegrityError("verification response does not match its request")
        if request.claim_id != claim.claim_id:
            raise VerificationIntegrityError("verification request does not match the claim")
        if response.actor not in request.allowed_actors:
            raise VerificationIntegrityError("response actor is not allowed for this request")
        if response.responded_at < request.requested_at:
            raise VerificationIntegrityError("verification response predates its request")


def _artifact(kind: str, artifact_id: str, case_id: str, at, value: BaseModel):
    source_ids = tuple(
        source
        for source in (
            getattr(value, "source_id", None),
            *getattr(value, "source_ids", ()),
        )
        if source
    )
    return WorkflowArtifact(
        artifact_id=artifact_id,
        case_id=case_id,
        kind=kind,
        created_at=at,
        source_ids=source_ids,
        payload=value.model_dump(mode="json"),
    )
