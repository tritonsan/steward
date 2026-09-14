"""Durable meeting preparation with no external delivery.

This module selects a meeting time deterministically from closed candidate and
availability sets, freezes the model brief before agenda generation, copies
vendor contacts only from the configured directory, and stores an immutable
packet.  It deliberately creates no outbox item and exposes no send method.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta, timezone
from enum import Enum
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from steward.agents import MeetingAgendaPlanner, MeetingAgendaRecommendation
from steward.domain.clock import Clock
from steward.domain.enums import (
    TERMINAL_STATUSES,
    ActorType,
    CaseStatus,
    Category,
    EventKind,
    ResolutionPath,
)
from steward.domain.models import Case, TimelineEvent, UtcDatetime, Vendor
from steward.orchestration import (
    DurableResolutionPlan,
    ResolutionContext,
    ResolutionContextMessage,
    ResolutionPlanningService,
)
from steward.store import (
    ConcurrencyConflict,
    IdempotencyConflict,
    OperationalStore,
    WorkflowArtifact,
)

__all__ = [
    "MEETING_BRIEF_ARTIFACT_KIND",
    "MEETING_PACKET_ARTIFACT_KIND",
    "MeetingAvailabilityResponse",
    "MeetingBrief",
    "MeetingCandidateSlot",
    "MeetingPacket",
    "MeetingPreparationConflictError",
    "MeetingPreparationDisposition",
    "MeetingPreparationError",
    "MeetingPreparationIntegrityError",
    "MeetingPreparationNotReadyError",
    "MeetingPreparationResult",
    "MeetingPreparationService",
    "MeetingRecommendationIntegrityError",
    "MeetingSchedule",
    "MeetingSchedulingPolicy",
    "MeetingVendorContact",
]

MEETING_BRIEF_ARTIFACT_KIND = "meeting_brief.v1"
MEETING_PACKET_ARTIFACT_KIND = "meeting_packet.v1"
_MAX_CANDIDATE_SLOTS = 20
_MAX_PARTICIPANTS = 200


class MeetingPreparationError(ValueError):
    """Base class for deterministic meeting-preparation rejection."""


class MeetingPreparationNotReadyError(MeetingPreparationError):
    """The meeting does not yet have a complete safe input."""


class MeetingPreparationIntegrityError(MeetingPreparationError):
    """Persisted meeting, case, plan, or directory facts are inconsistent."""


class MeetingRecommendationIntegrityError(MeetingPreparationError):
    """The agenda model cited a source outside the frozen brief."""


class MeetingPreparationConflictError(MeetingPreparationError):
    """The case already owns a different immutable meeting snapshot."""


class MeetingPreparationDisposition(str, Enum):
    PACKET_PREPARED = "packet_prepared"
    DUPLICATE = "duplicate"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class MeetingCandidateSlot(_Record):
    slot_id: str = Field(min_length=1, max_length=200)
    starts_at: UtcDatetime
    duration_minutes: int = Field(default=60, ge=15, le=480)

    @field_validator("slot_id")
    @classmethod
    def _strip_slot_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("meeting slot id must not be blank")
        return cleaned


class MeetingAvailabilityResponse(_Record):
    """A normalized response from a future trusted participant adapter."""

    participant_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=2000)
    available_slot_ids: tuple[str, ...] = Field(default=(), max_length=_MAX_CANDIDATE_SLOTS)

    @field_validator("participant_id", "source_id")
    @classmethod
    def _strip_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("meeting availability identifiers must not be blank")
        return cleaned

    @field_validator("available_slot_ids")
    @classmethod
    def _clean_slot_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value or len(value) > 200 for value in cleaned):
            raise ValueError("available slot ids must be non-blank and bounded")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("available slot ids must be unique")
        return cleaned


class MeetingSchedulingPolicy(_Record):
    timezone: str = Field(min_length=1, max_length=100)
    minimum_notice_hours: int = Field(default=24, ge=0, le=720)
    quorum: int = Field(ge=1, le=_MAX_PARTICIPANTS)
    eligible_participant_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_PARTICIPANTS,
    )
    required_participant_ids: tuple[str, ...] = Field(
        default=(),
        max_length=_MAX_PARTICIPANTS,
    )

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        cleaned = value.strip()
        try:
            _timezone(cleaned)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("meeting timezone must be a valid IANA timezone") from exc
        return cleaned

    @field_validator("eligible_participant_ids", "required_participant_ids")
    @classmethod
    def _clean_participants(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value or len(value) > 200 for value in cleaned):
            raise ValueError("participant ids must be non-blank and bounded")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("participant ids must be unique")
        return cleaned

    @model_validator(mode="after")
    def _consistent_participants(self) -> MeetingSchedulingPolicy:
        eligible = set(self.eligible_participant_ids)
        if not set(self.required_participant_ids).issubset(eligible):
            raise ValueError("required participants must be eligible participants")
        if self.quorum > len(eligible):
            raise ValueError("meeting quorum exceeds the eligible participant count")
        return self


class MeetingSchedule(_Record):
    policy: MeetingSchedulingPolicy
    candidate_slots: tuple[MeetingCandidateSlot, ...] = Field(
        min_length=1,
        max_length=_MAX_CANDIDATE_SLOTS,
    )
    selected_slot: MeetingCandidateSlot
    available_participant_count: int = Field(ge=1, le=_MAX_PARTICIPANTS)
    considered_response_source_ids: tuple[str, ...] = Field(min_length=1)
    selection_reason: str = Field(min_length=1, max_length=1000)
    selected_at: UtcDatetime
    selected_by_model: Literal[False] = False

    @model_validator(mode="after")
    def _selected_slot_was_offered(self) -> MeetingSchedule:
        slot_ids = [slot.slot_id for slot in self.candidate_slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("meeting schedule candidate slot ids must be unique")
        offered = {slot.slot_id: slot for slot in self.candidate_slots}
        if offered.get(self.selected_slot.slot_id) != self.selected_slot:
            raise ValueError("selected meeting slot was not offered")
        sources = self.considered_response_source_ids
        if len(sources) != len(set(sources)):
            raise ValueError("meeting schedule response source ids must be unique")
        return self


class MeetingVendorContact(_Record):
    """Directory-sourced contact snapshot never generated by a model."""

    vendor_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    email: str = Field(min_length=3, max_length=320)
    phone: str | None = Field(default=None, max_length=100)
    categories: tuple[Category, ...]
    allowlisted: bool
    simulated: bool


class MeetingBrief(_Record):
    """Immutable agenda-model input persisted before invocation."""

    schema_version: Literal[1] = 1
    brief_id: str = Field(min_length=1)
    request_key_sha256: str = Field(min_length=64, max_length=64)
    input_sha256: str = Field(min_length=64, max_length=64)
    case_id: str = Field(min_length=1)
    case_title: str = Field(min_length=1, max_length=160)
    category: Category
    plan_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    created_at: UtcDatetime
    schedule: MeetingSchedule
    required_fact_codes: tuple[str, ...] = ()
    messages: tuple[ResolutionContextMessage, ...] = Field(min_length=1)
    vendor_contacts: tuple[MeetingVendorContact, ...] = ()
    source_ids: tuple[str, ...] = Field(min_length=1)
    history: tuple[dict[str, Any], ...] = ()
    management_notes: tuple[dict[str, Any], ...] = ()

    def agenda_context(self) -> dict[str, Any]:
        return {
            "brief_id": self.brief_id,
            "case": {
                "title": self.case_title,
                "category": self.category.value,
            },
            "selected_meeting_slot": self.schedule.selected_slot.model_dump(mode="json"),
            "timezone": self.schedule.policy.timezone,
            "required_fact_codes": list(self.required_fact_codes),
            "allowed_source_ids": list(self.source_ids),
            "messages": [item.model_dump(mode="json") for item in self.messages],
            "history": list(self.history),
            "management_notes": list(self.management_notes),
            "vendor_options": [
                {
                    "vendor_id": item.vendor_id,
                    "name": item.name,
                    "categories": [category.value for category in item.categories],
                    "allowlisted": item.allowlisted,
                    "simulated": item.simulated,
                }
                for item in self.vendor_contacts
            ],
        }


class MeetingPacket(_Record):
    """Meeting-ready internal packet; structurally incapable of delivery."""

    schema_version: Literal[1] = 1
    packet_id: str = Field(min_length=1)
    brief_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    created_at: UtcDatetime
    schedule: MeetingSchedule
    agenda: MeetingAgendaRecommendation
    vendor_contacts: tuple[MeetingVendorContact, ...] = ()
    notice_subject: str = Field(min_length=1, max_length=300)
    notice_body_text: str = Field(min_length=1, max_length=12000)
    payload_sha256: str = Field(min_length=64, max_length=64)
    source_ids: tuple[str, ...] = Field(min_length=1)
    model_output_is_authority: Literal[False] = False
    approval_requested: Literal[False] = False
    outbound_enabled: Literal[False] = False
    final_send_blocked: Literal[True] = True
    sent: Literal[False] = False


@dataclass(frozen=True, slots=True)
class MeetingPreparationResult:
    disposition: MeetingPreparationDisposition
    case: Case
    brief: MeetingBrief
    packet: MeetingPacket


class MeetingPreparationService:
    """Prepare and persist one meeting packet without creating an outbound intent."""

    __slots__ = ("_agenda_planner", "_clock", "_planning", "_store", "_vendors")

    def __init__(
        self,
        *,
        store: OperationalStore,
        planning: ResolutionPlanningService,
        agenda_planner: MeetingAgendaPlanner,
        vendors: tuple[Vendor, ...],
        clock: Clock,
    ) -> None:
        by_id: dict[str, Vendor] = {}
        for vendor in vendors:
            if vendor.vendor_id in by_id:
                raise ValueError(f"duplicate vendor id: {vendor.vendor_id}")
            by_id[vendor.vendor_id] = vendor.model_copy(deep=True)
        self._store = store
        self._planning = planning
        self._agenda_planner = agenda_planner
        self._vendors = by_id
        self._clock = clock

    def prepare_draft(self, case_id: str, plan_id: str):
        from steward.meetings.dossier import prepare_draft

        return prepare_draft(self, case_id, plan_id)

    def prepare(
        self,
        *,
        case_id: str,
        policy: MeetingSchedulingPolicy,
        candidate_slots: tuple[MeetingCandidateSlot, ...],
        availability: tuple[MeetingAvailabilityResponse, ...],
        idempotency_key: str,
    ) -> MeetingPreparationResult:
        safe_case_id = case_id.strip()
        safe_key = idempotency_key.strip()
        if not safe_case_id:
            raise MeetingPreparationNotReadyError("meeting case_id is blank")
        if not safe_key or len(safe_key) > 500:
            raise MeetingPreparationNotReadyError("meeting preparation idempotency key is invalid")
        normalized_slots, normalized_responses = _normalize_inputs(
            policy=policy,
            candidate_slots=candidate_slots,
            availability=availability,
        )
        input_hash = _meeting_input_hash(
            policy=policy,
            candidate_slots=normalized_slots,
            availability=normalized_responses,
        )
        brief_id = _stable_id("meeting-brief", safe_case_id)
        packet_id = _stable_id("meeting-packet", brief_id)
        self._reject_competing_artifacts(
            case_id=safe_case_id,
            brief_id=brief_id,
            packet_id=packet_id,
        )

        brief_artifact = self._store.artifact(MEETING_BRIEF_ARTIFACT_KIND, brief_id)
        if brief_artifact is None:
            brief = self._build_brief(
                case_id=safe_case_id,
                brief_id=brief_id,
                request_key=safe_key,
                input_hash=input_hash,
                policy=policy,
                candidate_slots=normalized_slots,
                availability=normalized_responses,
            )
            brief = self._persist_brief(brief)
        else:
            brief = self._load_brief(
                artifact=brief_artifact,
                case_id=safe_case_id,
                brief_id=brief_id,
                request_key_hash=_sha256(safe_key),
                input_hash=input_hash,
            )

        packet_artifact = self._store.artifact(MEETING_PACKET_ARTIFACT_KIND, packet_id)
        if packet_artifact is not None:
            packet = self._load_packet(
                artifact=packet_artifact,
                brief=brief,
                packet_id=packet_id,
            )
            current = self._store.get_case(safe_case_id)
            if current is None:
                raise MeetingPreparationIntegrityError("meeting packet lost its case")
            return MeetingPreparationResult(
                disposition=MeetingPreparationDisposition.DUPLICATE,
                case=current,
                brief=brief,
                packet=packet,
            )

        self._require_meeting_case(safe_case_id)
        from steward.meetings.dossier import draft_for_plan

        draft = draft_for_plan(self._store, brief.case_id, brief.plan_id)
        agenda = (
            MeetingAgendaRecommendation.model_validate(draft.payload["agenda"])
            if draft
            else self._agenda_planner.prepare(
                brief_id=brief.brief_id, context=brief.agenda_context()
            )
        )
        self._validate_agenda(brief, agenda)
        return self._persist_packet(
            brief=brief,
            agenda=agenda,
            packet_id=packet_id,
        )

    def load(self, case_id: str) -> MeetingPreparationResult | None:
        """Load the case's single complete packet without invoking a model."""
        safe_case_id = case_id.strip()
        if not safe_case_id:
            raise MeetingPreparationNotReadyError("meeting case_id is blank")
        briefs = self._store.artifacts_for(
            kind=MEETING_BRIEF_ARTIFACT_KIND,
            case_id=safe_case_id,
        )
        packets = self._store.artifacts_for(
            kind=MEETING_PACKET_ARTIFACT_KIND,
            case_id=safe_case_id,
        )
        if not briefs and not packets:
            return None
        if len(briefs) != 1 or len(packets) != 1:
            raise MeetingPreparationIntegrityError(
                "case does not own exactly one complete meeting packet"
            )
        payload = briefs[0].payload
        brief = self._load_brief(
            artifact=briefs[0],
            case_id=safe_case_id,
            brief_id=briefs[0].artifact_id,
            request_key_hash=str(payload.get("request_key_sha256", "")),
            input_hash=str(payload.get("input_sha256", "")),
        )
        packet = self._load_packet(
            artifact=packets[0],
            brief=brief,
            packet_id=packets[0].artifact_id,
        )
        current = self._store.get_case(safe_case_id)
        if current is None:
            raise MeetingPreparationIntegrityError("meeting packet lost its case")
        return MeetingPreparationResult(
            disposition=MeetingPreparationDisposition.DUPLICATE,
            case=current,
            brief=brief,
            packet=packet,
        )

    def _build_brief(
        self,
        *,
        case_id: str,
        brief_id: str,
        request_key: str,
        input_hash: str,
        policy: MeetingSchedulingPolicy,
        candidate_slots: tuple[MeetingCandidateSlot, ...],
        availability: tuple[MeetingAvailabilityResponse, ...],
    ) -> MeetingBrief:
        planning = self._planning.load(case_id)
        if planning is None:
            raise MeetingPreparationNotReadyError(
                "meeting preparation requires a durable resolution plan"
            )
        context = planning.context
        plan = planning.plan
        if plan.recommendation.path is not ResolutionPath.MEETING_RESOLUTION:
            raise MeetingPreparationNotReadyError(
                "resolution plan did not select meeting_resolution"
            )
        current = self._require_meeting_case(case_id)
        if current.category is not context.category or current.asset_id != context.asset_id:
            raise MeetingPreparationIntegrityError(
                "meeting case differs from its frozen resolution context"
            )
        created_at = self._clock.now()
        if created_at < plan.created_at:
            raise MeetingPreparationIntegrityError(
                "meeting brief cannot predate its resolution plan"
            )
        schedule = _select_schedule(
            policy=policy,
            candidate_slots=candidate_slots,
            availability=availability,
            selected_at=created_at,
        )
        contacts = self._vendor_contacts(context=context, plan=plan)
        source_ids = _ordered_unique(
            (plan.plan_id, context.context_id),
            context.source_ids,
            schedule.considered_response_source_ids,
            tuple(item.vendor_id for item in contacts),
        )
        return MeetingBrief(
            brief_id=brief_id,
            request_key_sha256=_sha256(request_key),
            input_sha256=input_hash,
            case_id=case_id,
            case_title=context.case_title,
            category=context.category,
            plan_id=plan.plan_id,
            context_id=context.context_id,
            created_at=created_at,
            schedule=schedule,
            required_fact_codes=plan.recommendation.required_fact_codes,
            messages=context.messages,
            vendor_contacts=contacts,
            source_ids=source_ids,
            history=context.history,
            management_notes=context.management_notes,
        )

    def _vendor_contacts(
        self,
        *,
        context: ResolutionContext,
        plan: DurableResolutionPlan,
    ) -> tuple[MeetingVendorContact, ...]:
        offered = {item.vendor_id: item for item in context.vendor_options}
        contacts: list[MeetingVendorContact] = []
        for vendor_id in plan.recommendation.relevant_vendor_ids:
            option = offered.get(vendor_id)
            vendor = self._vendors.get(vendor_id)
            if option is None or vendor is None:
                raise MeetingPreparationIntegrityError(
                    "meeting vendor disappeared from the offered directory"
                )
            if (
                option.name != vendor.name
                or option.categories != tuple(vendor.categories)
                or option.allowlisted != vendor.allowlisted
                or option.simulated != vendor.simulated
            ):
                raise MeetingPreparationIntegrityError(
                    "meeting vendor directory facts changed after resolution planning"
                )
            contacts.append(
                MeetingVendorContact(
                    vendor_id=vendor.vendor_id,
                    name=vendor.name,
                    email=vendor.email,
                    phone=vendor.phone,
                    categories=tuple(vendor.categories),
                    allowlisted=vendor.allowlisted,
                    simulated=vendor.simulated,
                )
            )
        return tuple(contacts)

    def _persist_brief(self, brief: MeetingBrief) -> MeetingBrief:
        artifact = WorkflowArtifact(
            artifact_id=brief.brief_id,
            case_id=brief.case_id,
            kind=MEETING_BRIEF_ARTIFACT_KIND,
            created_at=brief.created_at,
            source_ids=brief.source_ids,
            payload=brief.model_dump(mode="json"),
        )
        event = TimelineEvent(
            event_id=_stable_id("event-meeting-schedule", brief.brief_id),
            case_id=brief.case_id,
            at=brief.created_at,
            kind=EventKind.MEETING_SCHEDULE_SELECTED,
            actor=ActorType.SYSTEM,
            summary=(
                "Selected the meeting time deterministically from the offered "
                "availability responses; no invitation was sent."
            ),
            refs=list(brief.source_ids),
            payload={
                "brief_id": brief.brief_id,
                "plan_id": brief.plan_id,
                "selected_slot_id": brief.schedule.selected_slot.slot_id,
                "scheduled_for": brief.schedule.selected_slot.starts_at.isoformat(),
                "timezone": brief.schedule.policy.timezone,
                "available_participant_count": (brief.schedule.available_participant_count),
                "selected_by_model": False,
                "agenda_model_invocation_pending": True,
                "outbound_enabled": False,
            },
        )
        for _attempt in range(3):
            existing = self._store.artifact(MEETING_BRIEF_ARTIFACT_KIND, brief.brief_id)
            if existing is not None:
                return self._load_brief(
                    artifact=existing,
                    case_id=brief.case_id,
                    brief_id=brief.brief_id,
                    request_key_hash=brief.request_key_sha256,
                    input_hash=brief.input_sha256,
                )
            current = self._require_meeting_case(brief.case_id)
            planning = self._planning.load(brief.case_id)
            if (
                planning is None
                or planning.plan.plan_id != brief.plan_id
                or planning.context.context_id != brief.context_id
            ):
                raise MeetingPreparationConflictError(
                    "resolution inputs changed before the meeting brief was committed"
                )
            version = self._store.case_version(brief.case_id)
            if version is None:
                raise MeetingPreparationIntegrityError(
                    "meeting brief case lost its optimistic version"
                )
            updated = current.model_copy(deep=True)
            updated.status = CaseStatus.PLANNING
            updated.scheduled_for = brief.schedule.selected_slot.starts_at
            updated.updated_at = max(updated.updated_at, brief.created_at)
            try:
                self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=f"meeting-brief:v1:{brief.brief_id}",
                    timeline_events=(event,),
                    artifacts=(artifact,),
                )
                return brief
            except (ConcurrencyConflict, IdempotencyConflict):
                persisted = self._store.artifact(
                    MEETING_BRIEF_ARTIFACT_KIND,
                    brief.brief_id,
                )
                if persisted is not None:
                    return self._load_brief(
                        artifact=persisted,
                        case_id=brief.case_id,
                        brief_id=brief.brief_id,
                        request_key_hash=brief.request_key_sha256,
                        input_hash=brief.input_sha256,
                    )
        raise ConcurrencyConflict("meeting brief persistence exceeded retry limit")

    def _persist_packet(
        self,
        *,
        brief: MeetingBrief,
        agenda: MeetingAgendaRecommendation,
        packet_id: str,
    ) -> MeetingPreparationResult:
        created_at = self._clock.now()
        if created_at < brief.created_at:
            raise MeetingPreparationIntegrityError("meeting packet cannot predate its brief")
        subject, body = _notice_draft(brief=brief, agenda=agenda)
        payload_hash = _payload_sha256(
            packet_id=packet_id,
            case_id=brief.case_id,
            scheduled_for=brief.schedule.selected_slot.starts_at.isoformat(),
            subject=subject,
            body_text=body,
        )
        cited_sources = _agenda_sources(agenda)
        source_ids = _ordered_unique((brief.brief_id,), brief.source_ids, cited_sources)
        packet = MeetingPacket(
            packet_id=packet_id,
            brief_id=brief.brief_id,
            plan_id=brief.plan_id,
            case_id=brief.case_id,
            created_at=created_at,
            schedule=brief.schedule,
            agenda=agenda,
            vendor_contacts=brief.vendor_contacts,
            notice_subject=subject,
            notice_body_text=body,
            payload_sha256=payload_hash,
            source_ids=source_ids,
        )
        artifact = WorkflowArtifact(
            artifact_id=packet.packet_id,
            case_id=packet.case_id,
            kind=MEETING_PACKET_ARTIFACT_KIND,
            created_at=packet.created_at,
            source_ids=packet.source_ids,
            payload=packet.model_dump(mode="json"),
        )
        event = TimelineEvent(
            event_id=_stable_id("event-meeting-packet", packet.packet_id),
            case_id=packet.case_id,
            at=packet.created_at,
            kind=EventKind.MEETING_PACKET_PREPARED,
            actor=ActorType.AGENT,
            summary=(
                "Prepared an immutable, source-traced meeting packet and exact "
                "notice draft. External delivery remains blocked."
            ),
            refs=list(packet.source_ids),
            payload={
                "packet_id": packet.packet_id,
                "brief_id": packet.brief_id,
                "payload_sha256": packet.payload_sha256,
                "scheduled_for": packet.schedule.selected_slot.starts_at.isoformat(),
                "vendor_ids": [item.vendor_id for item in packet.vendor_contacts],
                "model_output_is_authority": False,
                "approval_requested": False,
                "outbound_enabled": False,
                "final_send_blocked": True,
                "sent": False,
            },
        )
        for _attempt in range(3):
            existing = self._store.artifact(
                MEETING_PACKET_ARTIFACT_KIND,
                packet.packet_id,
            )
            if existing is not None:
                loaded = self._load_packet(
                    artifact=existing,
                    brief=brief,
                    packet_id=packet.packet_id,
                )
                latest = self._store.get_case(packet.case_id)
                if latest is None:
                    raise MeetingPreparationIntegrityError("meeting packet lost its case")
                return MeetingPreparationResult(
                    disposition=MeetingPreparationDisposition.DUPLICATE,
                    case=latest,
                    brief=brief,
                    packet=loaded,
                )
            current = self._require_meeting_case(packet.case_id)
            planning = self._planning.load(packet.case_id)
            if (
                planning is None
                or planning.plan.plan_id != packet.plan_id
                or planning.context.context_id != brief.context_id
            ):
                raise MeetingPreparationConflictError(
                    "resolution inputs changed before the meeting packet was committed"
                )
            version = self._store.case_version(packet.case_id)
            if version is None:
                raise MeetingPreparationIntegrityError(
                    "meeting packet case lost its optimistic version"
                )
            updated = current.model_copy(deep=True)
            updated.status = CaseStatus.MEETING_READY
            updated.scheduled_for = packet.schedule.selected_slot.starts_at
            updated.next_action_due_at = None
            updated.updated_at = max(updated.updated_at, packet.created_at)
            try:
                self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=f"meeting-packet:v1:{packet.packet_id}",
                    timeline_events=(event,),
                    artifacts=(artifact,),
                )
                return MeetingPreparationResult(
                    disposition=MeetingPreparationDisposition.PACKET_PREPARED,
                    case=updated,
                    brief=brief,
                    packet=packet,
                )
            except (ConcurrencyConflict, IdempotencyConflict):
                persisted = self._store.artifact(
                    MEETING_PACKET_ARTIFACT_KIND,
                    packet.packet_id,
                )
                if persisted is not None:
                    loaded = self._load_packet(
                        artifact=persisted,
                        brief=brief,
                        packet_id=packet.packet_id,
                    )
                    latest = self._store.get_case(packet.case_id)
                    if latest is None:
                        raise MeetingPreparationIntegrityError(
                            "meeting packet lost its case"
                        ) from None
                    return MeetingPreparationResult(
                        disposition=MeetingPreparationDisposition.DUPLICATE,
                        case=latest,
                        brief=brief,
                        packet=loaded,
                    )
        raise ConcurrencyConflict("meeting packet persistence exceeded retry limit")

    def _load_brief(
        self,
        *,
        artifact: WorkflowArtifact,
        case_id: str,
        brief_id: str,
        request_key_hash: str,
        input_hash: str,
    ) -> MeetingBrief:
        try:
            brief = MeetingBrief.model_validate(artifact.payload)
        except ValueError as exc:
            raise MeetingPreparationIntegrityError(
                "persisted meeting brief is not structurally valid"
            ) from exc
        if (
            artifact.kind != MEETING_BRIEF_ARTIFACT_KIND
            or artifact.artifact_id != brief_id
            or artifact.case_id != case_id
            or brief.brief_id != brief_id
            or brief.case_id != case_id
            or brief.request_key_sha256 != request_key_hash
            or brief.input_sha256 != input_hash
            or artifact.source_ids != brief.source_ids
        ):
            raise MeetingPreparationConflictError(
                "persisted meeting brief does not match the request"
            )
        planning = self._planning.load(case_id)
        if planning is None or planning.plan.plan_id != brief.plan_id:
            raise MeetingPreparationIntegrityError("meeting brief lost its durable resolution plan")
        if planning.plan.recommendation.path is not ResolutionPath.MEETING_RESOLUTION:
            raise MeetingPreparationIntegrityError(
                "meeting brief is attached to a non-meeting resolution plan"
            )
        if (
            planning.context.context_id != brief.context_id
            or planning.context.case_title != brief.case_title
            or planning.context.category is not brief.category
        ):
            raise MeetingPreparationIntegrityError(
                "meeting brief differs from its frozen resolution context"
            )
        return brief

    def _load_packet(
        self,
        *,
        artifact: WorkflowArtifact,
        brief: MeetingBrief,
        packet_id: str,
    ) -> MeetingPacket:
        try:
            packet = MeetingPacket.model_validate(artifact.payload)
        except ValueError as exc:
            raise MeetingPreparationIntegrityError(
                "persisted meeting packet is not structurally valid"
            ) from exc
        expected_hash = _payload_sha256(
            packet_id=packet.packet_id,
            case_id=packet.case_id,
            scheduled_for=packet.schedule.selected_slot.starts_at.isoformat(),
            subject=packet.notice_subject,
            body_text=packet.notice_body_text,
        )
        if (
            artifact.kind != MEETING_PACKET_ARTIFACT_KIND
            or artifact.artifact_id != packet_id
            or artifact.case_id != brief.case_id
            or packet.packet_id != packet_id
            or packet.brief_id != brief.brief_id
            or packet.plan_id != brief.plan_id
            or packet.case_id != brief.case_id
            or packet.schedule != brief.schedule
            or packet.vendor_contacts != brief.vendor_contacts
            or packet.payload_sha256 != expected_hash
            or artifact.source_ids != packet.source_ids
        ):
            raise MeetingPreparationIntegrityError(
                "persisted meeting packet violates identity or payload invariants"
            )
        self._validate_agenda(brief, packet.agenda)
        expected_sources = _ordered_unique(
            (brief.brief_id,),
            brief.source_ids,
            _agenda_sources(packet.agenda),
        )
        if packet.source_ids != expected_sources:
            raise MeetingPreparationIntegrityError(
                "persisted meeting packet violates source invariants"
            )
        return packet

    def _reject_competing_artifacts(
        self,
        *,
        case_id: str,
        brief_id: str,
        packet_id: str,
    ) -> None:
        briefs = self._store.artifacts_for(
            kind=MEETING_BRIEF_ARTIFACT_KIND,
            case_id=case_id,
        )
        if any(item.artifact_id != brief_id for item in briefs):
            raise MeetingPreparationConflictError(
                "case already has a different immutable meeting brief"
            )
        packets = self._store.artifacts_for(
            kind=MEETING_PACKET_ARTIFACT_KIND,
            case_id=case_id,
        )
        if any(item.artifact_id != packet_id for item in packets):
            raise MeetingPreparationConflictError(
                "case already has a different immutable meeting packet"
            )

    @staticmethod
    def _validate_agenda(
        brief: MeetingBrief,
        agenda: MeetingAgendaRecommendation,
    ) -> None:
        allowed = set(brief.source_ids)
        cited = _agenda_sources(agenda)
        if any(source_id not in allowed for source_id in cited):
            raise MeetingRecommendationIntegrityError(
                "meeting agenda cited a source outside the frozen brief"
            )
        message_ids = {message.message_id for message in brief.messages}
        if not any(source_id in message_ids for source_id in agenda.summary_source_ids):
            raise MeetingRecommendationIntegrityError(
                "meeting summary must cite at least one resident message"
            )
        allowed_vendors = {vendor.vendor_id for vendor in brief.vendor_contacts}
        if any(v not in allowed_vendors for need in agenda.quote_needs for v in need.vendor_ids):
            raise MeetingRecommendationIntegrityError("meeting quote need used an unknown vendor")

    def _require_meeting_case(self, case_id: str) -> Case:
        case = self._store.get_case(case_id)
        if case is None:
            raise MeetingPreparationNotReadyError("unknown meeting case")
        if case.status in TERMINAL_STATUSES:
            raise MeetingPreparationNotReadyError("terminal case cannot receive a meeting packet")
        if case.status not in (
            CaseStatus.DETECTED,
            CaseStatus.PLANNING,
            CaseStatus.MEETING_READY,
        ):
            raise MeetingPreparationNotReadyError(
                "meeting preparation requires a detected, planning, or meeting-ready case"
            )
        return case


def _normalize_inputs(
    *,
    policy: MeetingSchedulingPolicy,
    candidate_slots: tuple[MeetingCandidateSlot, ...],
    availability: tuple[MeetingAvailabilityResponse, ...],
) -> tuple[tuple[MeetingCandidateSlot, ...], tuple[MeetingAvailabilityResponse, ...]]:
    if not candidate_slots:
        raise MeetingPreparationNotReadyError("meeting needs at least one candidate slot")
    if len(candidate_slots) > _MAX_CANDIDATE_SLOTS:
        raise MeetingPreparationNotReadyError("meeting has too many candidate slots")
    slot_ids = [slot.slot_id for slot in candidate_slots]
    if len(slot_ids) != len(set(slot_ids)):
        raise MeetingPreparationIntegrityError("meeting candidate slot ids must be unique")
    if not availability:
        raise MeetingPreparationNotReadyError("meeting time selection needs availability responses")
    participants = [response.participant_id for response in availability]
    sources = [response.source_id for response in availability]
    if len(participants) != len(set(participants)):
        raise MeetingPreparationIntegrityError(
            "meeting has multiple availability responses for one participant"
        )
    if len(sources) != len(set(sources)):
        raise MeetingPreparationIntegrityError("meeting availability source ids must be unique")
    eligible = set(policy.eligible_participant_ids)
    offered_slots = set(slot_ids)
    for response in availability:
        if response.participant_id not in eligible:
            raise MeetingPreparationIntegrityError(
                "availability response participant was not eligible"
            )
        if any(slot_id not in offered_slots for slot_id in response.available_slot_ids):
            raise MeetingPreparationIntegrityError(
                "availability response named a slot outside the offered set"
            )
    slots = tuple(sorted(candidate_slots, key=lambda item: (item.starts_at, item.slot_id)))
    responses = tuple(sorted(availability, key=lambda item: (item.participant_id, item.source_id)))
    return slots, responses


def _select_schedule(
    *,
    policy: MeetingSchedulingPolicy,
    candidate_slots: tuple[MeetingCandidateSlot, ...],
    availability: tuple[MeetingAvailabilityResponse, ...],
    selected_at: UtcDatetime,
) -> MeetingSchedule:
    earliest = selected_at + timedelta(hours=policy.minimum_notice_hours)
    if any(slot.starts_at < earliest for slot in candidate_slots):
        raise MeetingPreparationNotReadyError(
            "candidate meeting slot violates the minimum notice period"
        )
    required = set(policy.required_participant_ids)
    ranked: list[tuple[int, MeetingCandidateSlot]] = []
    for slot in candidate_slots:
        available = {
            response.participant_id
            for response in availability
            if slot.slot_id in response.available_slot_ids
        }
        if len(available) < policy.quorum or not required.issubset(available):
            continue
        ranked.append((len(available), slot))
    if not ranked:
        raise MeetingPreparationNotReadyError(
            "no candidate meeting slot satisfies quorum and required participants"
        )
    count, selected = min(
        ranked,
        key=lambda item: (-item[0], item[1].starts_at, item[1].slot_id),
    )
    return MeetingSchedule(
        policy=policy,
        candidate_slots=candidate_slots,
        selected_slot=selected,
        available_participant_count=count,
        considered_response_source_ids=tuple(response.source_id for response in availability),
        selection_reason=(
            f"Selected {selected.slot_id} with {count} available eligible participant(s); "
            "it satisfied quorum and every required participant, using earliest-time then "
            "slot-id as deterministic tie-breakers."
        ),
        selected_at=selected_at,
    )


def _timezone(value: str):
    if value.upper() in {"UTC", "ETC/UTC", "GMT"}:
        return timezone.utc
    return ZoneInfo(value)


def _meeting_input_hash(
    *,
    policy: MeetingSchedulingPolicy,
    candidate_slots: tuple[MeetingCandidateSlot, ...],
    availability: tuple[MeetingAvailabilityResponse, ...],
) -> str:
    payload = {
        "policy": policy.model_dump(mode="json"),
        "candidate_slots": [item.model_dump(mode="json") for item in candidate_slots],
        "availability": [item.model_dump(mode="json") for item in availability],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _notice_draft(
    *,
    brief: MeetingBrief,
    agenda: MeetingAgendaRecommendation,
) -> tuple[str, str]:
    local_start = brief.schedule.selected_slot.starts_at.astimezone(
        _timezone(brief.schedule.policy.timezone)
    )
    agenda_lines = "\n".join(
        f"{index}. {item.title} — {item.detail}"
        for index, item in enumerate(agenda.agenda_items, start=1)
    )
    question_lines = "\n".join(f"- {item.question}" for item in agenda.open_questions)
    subject = f"Community meeting: {brief.case_title}"
    body = (
        f"A community meeting has been prepared for {local_start.isoformat()} "
        f"({brief.schedule.policy.timezone}).\n\n"
        f"Topic\n{agenda.summary}\n\n"
        f"Agenda\n{agenda_lines}"
    )
    if question_lines:
        body += f"\n\nQuestions to resolve\n{question_lines}"
    body += "\n\nRegards,\nSteward Property Management"
    return subject, body


def _agenda_sources(agenda: MeetingAgendaRecommendation) -> tuple[str, ...]:
    return _ordered_unique(
        agenda.summary_source_ids,
        *(item.source_ids for item in agenda.agenda_items),
        *(item.source_ids for item in agenda.open_questions),
        *(item.source_ids for item in agenda.solution_options),
        *(item.source_ids for item in agenda.quote_needs),
    )


def _payload_sha256(
    *,
    packet_id: str,
    case_id: str,
    scheduled_for: str,
    subject: str,
    body_text: str,
) -> str:
    canonical = json.dumps(
        {
            "packet_id": packet_id,
            "case_id": case_id,
            "scheduled_for": scheduled_for,
            "subject": subject,
            "body_text": body_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ordered_unique(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))
