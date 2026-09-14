"""Immutable records for the internal meeting-decision lifecycle."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from steward.agents import MeetingMinutesExtraction
from steward.domain.models import UtcDatetime

__all__ = [
    "MEETING_ACTION_COMPLETION_ARTIFACT_KIND",
    "MEETING_ACTION_REMINDER_ARTIFACT_KIND",
    "MEETING_DECISION_ARTIFACT_KIND",
    "MEETING_DECISION_CANDIDATES_ARTIFACT_KIND",
    "MEETING_ESCALATION_ARTIFACT_KIND",
    "MEETING_MINUTES_ARTIFACT_KIND",
    "MEETING_VERIFICATION_REQUEST_ARTIFACT_KIND",
    "MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND",
    "DurableMeetingDecision",
    "MeetingActionCandidate",
    "MeetingActionCompletion",
    "MeetingActionReminder",
    "MeetingDecisionCandidate",
    "MeetingDecisionCandidateSet",
    "MeetingDecisionConfirmation",
    "MeetingEscalation",
    "MeetingGovernancePolicy",
    "MeetingMinutesSnapshot",
    "MeetingVerificationOutcome",
    "MeetingVerificationRequest",
    "MeetingVerificationResponse",
]

MEETING_MINUTES_ARTIFACT_KIND = "meeting_minutes.v1"
MEETING_DECISION_CANDIDATES_ARTIFACT_KIND = "meeting_decision_candidates.v1"
MEETING_DECISION_ARTIFACT_KIND = "meeting_decision.v1"
MEETING_ACTION_COMPLETION_ARTIFACT_KIND = "meeting_action_completion.v1"
MEETING_ACTION_REMINDER_ARTIFACT_KIND = "meeting_action_reminder.v1"
MEETING_ESCALATION_ARTIFACT_KIND = "meeting_escalation.v1"
MEETING_VERIFICATION_REQUEST_ARTIFACT_KIND = "meeting_verification_request.v1"
MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND = "meeting_verification_response.v1"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


def _clean_unique(
    values: tuple[str, ...],
    *,
    field: str,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if not allow_empty and not cleaned:
        raise ValueError(f"{field} must not be empty")
    if any(not value or len(value) > 2000 for value in cleaned):
        raise ValueError(f"{field} must be non-blank and bounded")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError(f"{field} must be unique")
    return cleaned


class MeetingGovernancePolicy(_Record):
    """Trusted configured actor sets; never populated from resident prose.

    Action owners must be eligible packet participants. Deciders and verifiers may be
    separately configured managers and therefore need not be meeting attendees.
    """

    action_owner_ids: tuple[str, ...] = Field(min_length=1, max_length=200)
    authorized_decider_ids: tuple[str, ...] = Field(min_length=1, max_length=50)
    authorized_verifier_ids: tuple[str, ...] = Field(min_length=1, max_length=50)
    reminder_interval_hours: int = Field(default=24, ge=1, le=720)
    max_reminders: int = Field(default=2, ge=0, le=20)
    verification_timeout_hours: int = Field(default=48, ge=1, le=720)

    @field_validator(
        "action_owner_ids",
        "authorized_decider_ids",
        "authorized_verifier_ids",
    )
    @classmethod
    def _clean_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, field="meeting governance participant ids", allow_empty=False)


class MeetingMinutesSnapshot(_Record):
    schema_version: Literal[1] = 1
    snapshot_id: str = Field(min_length=1)
    request_key_sha256: str = Field(min_length=64, max_length=64)
    case_id: str = Field(min_length=1)
    packet_id: str = Field(min_length=1)
    held_at: UtcDatetime
    recorded_at: UtcDatetime
    source_id: str = Field(min_length=1, max_length=2000)
    body_text: str = Field(min_length=1, max_length=30000)
    participant_count: int = Field(ge=1, le=10000)
    governance: MeetingGovernancePolicy
    simulated: bool = False
    outbound_enabled: Literal[False] = False
    source_ids: tuple[str, ...] = Field(min_length=1)

    def extraction_context(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "meeting_held_at": self.held_at.isoformat(),
            "minutes_body": self.body_text,
            "offered_action_owner_ids": list(self.governance.action_owner_ids),
        }


class MeetingDecisionCandidate(_Record):
    decision_id: str = Field(min_length=1)
    statement: str = Field(min_length=1, max_length=1500)
    evidence_excerpt: str = Field(min_length=1, max_length=2000)
    source_id: str = Field(min_length=1, max_length=2000)


class MeetingActionCandidate(_Record):
    action_id: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=1500)
    owner_participant_id: str = Field(min_length=1, max_length=200)
    due_at: UtcDatetime
    evidence_excerpt: str = Field(min_length=1, max_length=2000)
    source_id: str = Field(min_length=1, max_length=2000)


class MeetingDecisionCandidateSet(_Record):
    schema_version: Literal[1] = 1
    candidate_set_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    extraction: MeetingMinutesExtraction
    decisions: tuple[MeetingDecisionCandidate, ...] = Field(min_length=1, max_length=20)
    action_items: tuple[MeetingActionCandidate, ...] = Field(default=(), max_length=30)
    created_at: UtcDatetime
    source_ids: tuple[str, ...] = Field(min_length=1)
    model_output_is_authority: Literal[False] = False
    outbound_enabled: Literal[False] = False


class MeetingDecisionConfirmation(_Record):
    actor_id: str = Field(min_length=1, max_length=200)
    actor_label: str = Field(min_length=1, max_length=120)
    source_id: str = Field(min_length=1, max_length=2000)
    confirmed_at: UtcDatetime
    confirmed_decision_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    confirmed_action_ids: tuple[str, ...] = Field(default=(), max_length=30)

    @field_validator("actor_id", "actor_label", "source_id")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("meeting confirmation text must not be blank")
        return cleaned

    @field_validator("confirmed_decision_ids", "confirmed_action_ids")
    @classmethod
    def _clean_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_unique(values, field="confirmed meeting ids")


class DurableMeetingDecision(_Record):
    schema_version: Literal[1] = 1
    meeting_decision_id: str = Field(min_length=1)
    candidate_set_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    packet_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    confirmation: MeetingDecisionConfirmation
    decisions: tuple[MeetingDecisionCandidate, ...] = Field(min_length=1)
    action_items: tuple[MeetingActionCandidate, ...] = ()
    governance: MeetingGovernancePolicy
    authority_rule_id: str = Field(min_length=1, max_length=200)
    created_at: UtcDatetime
    source_ids: tuple[str, ...] = Field(min_length=1)
    model_output_is_authority: Literal[False] = False
    outbound_enabled: Literal[False] = False


class MeetingActionCompletion(_Record):
    schema_version: Literal[1] = 1
    completion_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    meeting_decision_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1, max_length=200)
    actor_label: str = Field(min_length=1, max_length=120)
    source_id: str = Field(min_length=1, max_length=2000)
    notes: str = Field(min_length=1, max_length=2500)
    completed_at: UtcDatetime
    authority_rule_id: str = Field(min_length=1, max_length=200)
    source_ids: tuple[str, ...] = Field(min_length=1)
    outbound_enabled: Literal[False] = False

    @field_validator("actor_id", "actor_label", "source_id", "notes")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("meeting action completion text must not be blank")
        return cleaned


class MeetingActionReminder(_Record):
    schema_version: Literal[1] = 1
    reminder_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    meeting_decision_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    attempt: int = Field(ge=1, le=20)
    created_at: UtcDatetime
    next_due_at: UtcDatetime
    source_ids: tuple[str, ...] = Field(min_length=1)
    internal_only: Literal[True] = True
    outbound_enabled: Literal[False] = False

    @model_validator(mode="after")
    def _future_due(self) -> MeetingActionReminder:
        if self.next_due_at <= self.created_at:
            raise ValueError("meeting reminder next due time must be in the future")
        return self


class MeetingEscalation(_Record):
    schema_version: Literal[1] = 1
    escalation_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    meeting_decision_id: str = Field(min_length=1)
    action_id: str | None = None
    verification_request_id: str | None = None
    reason: str = Field(min_length=1, max_length=2000)
    created_at: UtcDatetime
    source_ids: tuple[str, ...] = Field(min_length=1)
    internal_only: Literal[True] = True
    outbound_enabled: Literal[False] = False

    @model_validator(mode="after")
    def _one_target(self) -> MeetingEscalation:
        if (self.action_id is None) == (self.verification_request_id is None):
            raise ValueError("meeting escalation must name exactly one target")
        return self


class MeetingVerificationRequest(_Record):
    schema_version: Literal[1] = 1
    request_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    meeting_decision_id: str = Field(min_length=1)
    requested_at: UtcDatetime
    due_at: UtcDatetime
    authorized_verifier_ids: tuple[str, ...] = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)
    outbound_enabled: Literal[False] = False

    @model_validator(mode="after")
    def _chronology(self) -> MeetingVerificationRequest:
        if self.due_at <= self.requested_at:
            raise ValueError("meeting verification deadline must follow its request")
        return self


class MeetingVerificationOutcome(str, Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class MeetingVerificationResponse(_Record):
    schema_version: Literal[1] = 1
    response_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1, le=20)
    outcome: MeetingVerificationOutcome
    actor_id: str = Field(min_length=1, max_length=200)
    actor_label: str = Field(min_length=1, max_length=120)
    source_id: str = Field(min_length=1, max_length=2000)
    notes: str = Field(min_length=1, max_length=2500)
    responded_at: UtcDatetime
    authority_rule_id: str = Field(min_length=1, max_length=200)
    source_ids: tuple[str, ...] = Field(min_length=1)
    outbound_enabled: Literal[False] = False

    @field_validator("actor_id", "actor_label", "source_id", "notes")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("meeting verification text must not be blank")
        return cleaned
