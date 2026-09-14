"""Domain entities.

Every timestamp in this module is timezone-aware UTC, enforced by the
`UtcDatetime` annotation rather than by convention. The follow-up sweeper
compares stored deadlines against the clock on every pass, and a single naive
datetime slipping into the store turns that comparison into a `TypeError` at
runtime in the one code path that must never fail quietly.

Money is `Decimal`, never `float`. Spend caps are a safety control, and a
control that is off by a rounding error is not a control.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from steward.domain.enums import (
    ActorType,
    ApprovalMode,
    AutonomyLevel,
    CaseStatus,
    Category,
    CategoryGroup,
    EventKind,
    QuoteStatus,
    Urgency,
)

__all__ = [
    "ApprovalPolicy",
    "Asset",
    "AuditEntry",
    "Block",
    "Case",
    "GlobalSettings",
    "PropertyProfile",
    "Quote",
    "Resident",
    "ResidentMessage",
    "TimelineEvent",
    "UtcDatetime",
    "Vendor",
    "VendorScorecard",
]


def _ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes and normalize everything else to UTC."""
    if value.tzinfo is None:
        raise ValueError(
            "naive datetime rejected; construct it with tzinfo=timezone.utc or take it from a Clock"
        )
    return value.astimezone(timezone.utc)


UtcDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]


class _Base(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=False,
    )


class ResidentMessage(_Base):
    """A single message observed in the community's group chat.

    Only the sender's display name is stored. Phone numbers, user ids from the
    source platform, and any other directly identifying handle are dropped at
    ingestion, because this record is the one that gets fed to a language model
    and quoted back in the console.
    """

    message_id: str = Field(description="Stable id, source prefixed, e.g. 'tg:-100123:456'")
    source: str = Field(description="Ingestion adapter that produced this, e.g. 'telegram'")
    chat_id: str
    sender_display: str
    text: str
    sent_at: UtcDatetime
    ingested_at: UtcDatetime
    case_id: str | None = Field(
        default=None, description="Set once triage links this message to a case"
    )


class Asset(_Base):
    """A specific physical thing that can break.

    History is keyed on the asset, not only the category. "A Block elevator"
    and "B Block elevator" are the same category and different track records,
    and telling them apart is what makes the memory lookup useful rather than
    merely plausible.
    """

    asset_id: str
    label: str = Field(description="Human readable, e.g. 'A Block Elevator'")
    category: Category
    block: str | None = None
    installed_on: date | None = None
    notes: str = ""


class Vendor(_Base):
    """A service company Steward can contact."""

    vendor_id: str
    name: str
    email: str
    categories: list[Category] = Field(default_factory=list)
    phone: str | None = None
    allowlisted: bool = Field(
        default=False,
        description=(
            "Whether Steward may contact this vendor autonomously. Absence of "
            "an entry is a denial, never a permission."
        ),
    )
    simulated: bool = Field(
        default=True,
        description=(
            "True for the demo counterparties. Surfaced as a badge in the "
            "console so a reviewer is never misled about who is on the other "
            "end of a real email."
        ),
    )
    notes: str = ""


class VendorScorecard(_Base):
    """Aggregated performance of one vendor in one category.

    Computed from closed cases by deterministic code. No part of this record is
    produced by a language model, and `source_case_ids` exists so that every
    number on screen can be drilled back to the cases it came from.
    """

    vendor_id: str
    category: Category
    jobs_completed: int = 0
    avg_first_response_hours: float | None = None
    avg_hours_to_onsite: float | None = None
    avg_hours_to_resolution: float | None = None
    avg_cost: Decimal | None = None
    currency: str = "USD"
    repeat_failure_rate: float | None = Field(
        default=None,
        description="Share of jobs where the same asset failed again within the recurrence window",
    )
    last_engaged_at: UtcDatetime | None = None
    computed_at: UtcDatetime
    source_case_ids: list[str] = Field(default_factory=list)


class Quote(_Base):
    """A price and a date, extracted from a vendor's actual reply."""

    quote_id: str
    case_id: str
    vendor_id: str
    amount: Decimal
    currency: str = "USD"
    scope: str = Field(description="What the vendor says the price covers")
    earliest_onsite_at: UtcDatetime | None = None
    valid_until: UtcDatetime | None = None
    received_at: UtcDatetime
    status: QuoteStatus = QuoteStatus.RECEIVED
    source_email_message_id: str | None = Field(
        default=None, description="RFC Message-ID of the email this was read from"
    )


class TimelineEvent(_Base):
    """One entry in the story of a case, as rendered in the console."""

    event_id: str
    case_id: str
    at: UtcDatetime
    kind: EventKind
    actor: ActorType
    actor_label: str | None = None
    summary: str
    refs: list[str] = Field(
        default_factory=list,
        description="Ids of records backing any factual claim in `summary`",
    )
    payload: dict[str, Any] = Field(default_factory=dict)


class AuditEntry(_Base):
    """Why Steward was permitted to take one specific action.

    Written *before* the action it authorizes, so that an action which fails
    halfway still leaves evidence that it was attempted and under what
    authority. `policy_rule_id` names the rule that decided, which makes the
    trail reviewable without rerunning the engine.
    """

    audit_id: str
    case_id: str
    at: UtcDatetime
    action: str = Field(description="e.g. 'send_rfq', 'commit_quote', 'send_follow_up'")
    autonomy_level: AutonomyLevel
    policy_rule_id: str
    reason: str
    amount: Decimal | None = None
    vendor_id: str | None = None
    actor: ActorType = ActorType.AGENT


class Case(_Base):
    """One problem, from the message that raised it to the memory it becomes."""

    case_id: str
    reply_token: str = Field(description="Opaque token embedded in this case's reply address")
    title: str
    category: Category
    group: CategoryGroup
    urgency: Urgency
    status: CaseStatus = CaseStatus.DETECTED
    asset_id: str | None = None

    opened_at: UtcDatetime
    updated_at: UtcDatetime

    source_message_ids: list[str] = Field(default_factory=list)
    related_case_ids: list[str] = Field(
        default_factory=list,
        description="Historical cases that memory retrieval linked to this one",
    )

    autonomy_level: AutonomyLevel = AutonomyLevel.ESCALATE
    spend_cap: Decimal | None = None
    currency: str = "USD"

    contacted_vendor_ids: list[str] = Field(default_factory=list)
    accepted_quote_id: str | None = None

    next_action_due_at: UtcDatetime | None = Field(
        default=None,
        description=(
            "When the sweeper should look at this case again. This single "
            "field is what stops a case from being forgotten."
        ),
    )
    follow_up_count: int = 0
    escalated_at: UtcDatetime | None = None

    scheduled_for: UtcDatetime | None = None
    resolved_at: UtcDatetime | None = None
    closed_at: UtcDatetime | None = None
    total_cost: Decimal | None = None
    resolution_notes: str = ""

    @property
    def is_open(self) -> bool:
        return self.status not in (CaseStatus.CLOSED, CaseStatus.CANCELLED)


class ApprovalPolicy(_Base):
    """One row of the Approvals screen in the management console.

    This is the only place a human grants Steward authority, and it is data,
    not prose. Nothing a resident writes in the group chat can alter it.
    """

    category: Category
    mode: ApprovalMode = ApprovalMode.ESCALATE_ONLY
    per_incident_cap: Decimal | None = None
    monthly_cap: Decimal | None = None
    currency: str = "USD"
    allowed_vendor_ids: list[str] = Field(default_factory=list)
    quote_wait_hours: int = Field(default=24, ge=1, le=336)
    reminder_interval_hours: int = Field(default=24, ge=1, le=336)
    max_reminders: int = Field(default=2, ge=0, le=10)
    updated_at: UtcDatetime
    updated_by: str
    rationale: str = Field(
        default="",
        description=(
            "Why a human set it this way. Shown in the console beside the rule that "
            "fired, so 'why was Steward allowed to do that' is answerable without "
            "asking whoever configured it."
        ),
    )


class GlobalSettings(_Base):
    """System-wide controls that sit above any individual category policy."""

    kill_switch: bool = Field(
        default=False,
        description="When true, every case escalates and no outbound mail is sent",
    )
    min_triage_confidence: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Below this, Steward may prepare but never commit",
    )
    follow_up_grace_hours: int = Field(
        default=24,
        gt=0,
        description="How long a promised action may be late before Steward chases",
    )
    max_follow_ups_before_escalation: int = Field(
        default=2,
        ge=0,
        description="After this many unanswered nudges, a human is pulled in",
    )
    recurrence_window_days: int = Field(
        default=90,
        gt=0,
        description="Window used to decide whether a repaired asset failed 'again'",
    )
    default_currency: str = "USD"


class Block(_Base):
    """One residential block of the property."""

    block: str
    floors: int = Field(gt=0)
    units: int = Field(gt=0)


class Resident(_Base):
    """A person in the community, stored by display name only.

    There is no field here for a phone number, an email address, or a platform
    user id, and that is the point. This record is quoted into prompts and
    rendered in the console, so the safest design is for the identifying data
    never to arrive rather than to be redacted later.
    """

    display_name: str
    block: str | None = None
    note: str = ""


class PropertyProfile(_Base):
    """The community Steward works for."""

    property_id: str
    name: str
    description: str = ""
    units: int = Field(gt=0)
    approximate_residents: int | None = None
    completed_year: int | None = None
    currency: str = "USD"
    timezone: str = "UTC"
    chat_source: str = "telegram"
    chat_id: str | None = None
    chat_title: str | None = None
    management_committee: list[str] = Field(default_factory=list)
