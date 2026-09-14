"""Enumerations shared across the whole system.

Two ideas in here carry real weight and are worth reading before the rest:

`AutonomyLevel` draws the line between asking and committing. Requesting a
quote creates no obligation for the community, so a partially trusted category
is still allowed to do it. Accepting a quote creates a real obligation to a
real company, so that step is gated separately.

`Category` is deliberately a closed set. Triage classifies a free-text message
into one of these values and nothing else, which means an unknown or hostile
message can only ever land on `Category.OTHER`, never invent a new category
with its own permissions.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "ActorType",
    "ApprovalMode",
    "AutonomyLevel",
    "CATEGORY_GROUPS",
    "Category",
    "CategoryGroup",
    "CaseStatus",
    "EventKind",
    "OPEN_STATUSES",
    "QuoteStatus",
    "ResolutionPath",
    "TERMINAL_STATUSES",
    "Urgency",
    "group_of",
]


class CategoryGroup(str, Enum):
    """Top level grouping shown in the management console's Approvals screen."""

    REPAIR_MAINTENANCE = "repair_maintenance"
    GROUNDS = "grounds"
    CLEANING_SANITATION = "cleaning_sanitation"
    SAFETY_SECURITY = "safety_security"
    COMMUNITY_ADMIN = "community_admin"


class Category(str, Enum):
    """Leaf category. Every case has exactly one, and memory is scoped by it."""

    ELEVATOR = "elevator"
    PLUMBING = "plumbing"
    ELECTRICAL = "electrical"
    HVAC = "hvac"
    POOL = "pool"
    LANDSCAPING = "landscaping"
    COMMON_AREA_CLEANING = "common_area_cleaning"
    WASTE = "waste"
    LIGHTING = "lighting"
    ACCESS_CONTROL = "access_control"
    FIRE_SAFETY = "fire_safety"
    MEETING_ADMIN = "meeting_admin"
    OTHER = "other"


CATEGORY_GROUPS: dict[Category, CategoryGroup] = {
    Category.ELEVATOR: CategoryGroup.REPAIR_MAINTENANCE,
    Category.PLUMBING: CategoryGroup.REPAIR_MAINTENANCE,
    Category.ELECTRICAL: CategoryGroup.REPAIR_MAINTENANCE,
    Category.HVAC: CategoryGroup.REPAIR_MAINTENANCE,
    Category.POOL: CategoryGroup.GROUNDS,
    Category.LANDSCAPING: CategoryGroup.GROUNDS,
    Category.COMMON_AREA_CLEANING: CategoryGroup.CLEANING_SANITATION,
    Category.WASTE: CategoryGroup.CLEANING_SANITATION,
    Category.LIGHTING: CategoryGroup.SAFETY_SECURITY,
    Category.ACCESS_CONTROL: CategoryGroup.SAFETY_SECURITY,
    Category.FIRE_SAFETY: CategoryGroup.SAFETY_SECURITY,
    Category.MEETING_ADMIN: CategoryGroup.COMMUNITY_ADMIN,
    Category.OTHER: CategoryGroup.COMMUNITY_ADMIN,
}


def group_of(category: Category) -> CategoryGroup:
    """Return the group a category belongs to.

    Falls back to ``COMMUNITY_ADMIN`` rather than raising, because an
    unmapped category must never be able to crash intake. A case that lands
    in ``COMMUNITY_ADMIN`` gets the most conservative default policy.
    """
    return CATEGORY_GROUPS.get(category, CategoryGroup.COMMUNITY_ADMIN)


class Urgency(str, Enum):
    """How badly the community needs this fixed."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Numeric rank for comparisons. Higher means more urgent."""
        return _URGENCY_RANKS[self]

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Urgency):
            return self.rank >= other.rank
        return NotImplemented

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Urgency):
            return self.rank > other.rank
        return NotImplemented

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Urgency):
            return self.rank <= other.rank
        return NotImplemented

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Urgency):
            return self.rank < other.rank
        return NotImplemented


_URGENCY_RANKS: dict[Urgency, int] = {
    Urgency.LOW: 0,
    Urgency.NORMAL: 1,
    Urgency.HIGH: 2,
    Urgency.CRITICAL: 3,
}


class ApprovalMode(str, Enum):
    """What management pre-authorized for a category, set in the console.

    ALWAYS_APPROVE
        Steward may run the category end to end, still bounded by the spend
        caps and vendor allowlist attached to the same policy.
    PREPARE_ONLY
        Steward may gather information, including asking vendors for quotes,
        but a human accepts the quote.
    ESCALATE_ONLY
        Steward may not contact any third party at all. It records the case
        and notifies management. Intended for anything involving disputes
        between residents, legal exposure, or personal safety.
    """

    ALWAYS_APPROVE = "always_approve"
    PREPARE_ONLY = "prepare_only"
    ESCALATE_ONLY = "escalate_only"


class AutonomyLevel(str, Enum):
    """What Steward is actually allowed to do on one specific case, right now.

    This is the *output* of the policy engine, not a configuration value. It
    can be stricter than the category's `ApprovalMode` when a cap is exceeded,
    a vendor is unknown, triage confidence is low, or the kill switch is on.
    It is never more permissive.
    """

    AUTONOMOUS = "autonomous"
    PREPARE_ONLY = "prepare_only"
    ESCALATE = "escalate"

    @property
    def may_contact_third_parties(self) -> bool:
        """True when Steward may email vendors, e.g. to request a quote."""
        return self in (AutonomyLevel.AUTONOMOUS, AutonomyLevel.PREPARE_ONLY)

    @property
    def may_commit_spend(self) -> bool:
        """True when Steward may accept a quote and book the work itself."""
        return self is AutonomyLevel.AUTONOMOUS


class ResolutionPath(str, Enum):
    """How Steward should prepare one case; never a grant of authority."""

    PROCUREMENT = "procurement"
    MEETING_RESOLUTION = "meeting_resolution"
    HUMAN_REVIEW = "human_review"


class CaseStatus(str, Enum):
    """Lifecycle of a single problem."""

    DETECTED = "detected"
    PLANNING = "planning"
    MEETING_READY = "meeting_ready"
    ACTIONS_TRACKING = "actions_tracking"
    ATTENTION_REQUIRED = "attention_required"
    AWAITING_APPROVAL = "awaiting_approval"
    VENDOR_CONTACTED = "vendor_contacted"
    QUOTES_RECEIVED = "quotes_received"
    COMMITTED = "committed"
    AWAITING_APPOINTMENT = "awaiting_appointment"
    SCHEDULED = "scheduled"
    AWAITING_VERIFICATION = "awaiting_verification"
    WARRANTY_REVIEW = "warranty_review"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES: frozenset[CaseStatus] = frozenset({CaseStatus.CLOSED, CaseStatus.CANCELLED})
"""Statuses the follow-up sweeper ignores."""

OPEN_STATUSES: frozenset[CaseStatus] = frozenset(set(CaseStatus) - set(TERMINAL_STATUSES))
"""Everything the sweeper keeps on Steward's desk.

`RESOLVED` is deliberately still open: the work is done but the outcome has
not been written back to memory yet, and that write-back is the whole point.
"""


class QuoteStatus(str, Enum):
    RECEIVED = "received"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class ActorType(str, Enum):
    """Who caused a timeline event. Used for attribution in the audit trail."""

    AGENT = "agent"
    HUMAN = "human"
    VENDOR = "vendor"
    RESIDENT = "resident"
    SYSTEM = "system"


class EventKind(str, Enum):
    """Timeline event types.

    The timeline is what the management console renders and what the demo
    video walks through, so these names are user-facing.
    """

    CASE_OPENED = "case_opened"
    MESSAGES_LINKED = "messages_linked"
    DUPLICATE_MERGED = "duplicate_merged"
    MEMORY_CONSULTED = "memory_consulted"
    POLICY_EVALUATED = "policy_evaluated"
    PLAN_DRAFTED = "plan_drafted"
    RESOLUTION_PLANNED = "resolution_planned"
    MEETING_SCHEDULE_SELECTED = "meeting_schedule_selected"
    MEETING_PACKET_PREPARED = "meeting_packet_prepared"
    MEETING_MINUTES_RECORDED = "meeting_minutes_recorded"
    MEETING_DECISION_CONFIRMED = "meeting_decision_confirmed"
    ACTION_ITEM_OPENED = "action_item_opened"
    ACTION_ITEM_COMPLETED = "action_item_completed"
    ACTION_ITEM_REMINDER = "action_item_reminder"
    RFQ_SENT = "rfq_sent"
    VENDOR_REPLIED = "vendor_replied"
    QUOTE_RECORDED = "quote_recorded"
    QUOTE_RECOMMENDED = "quote_recommended"
    COMMITMENT_AUTHORIZED = "commitment_authorized"
    COMMITMENT_SENT = "commitment_sent"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    FOLLOW_UP_SENT = "follow_up_sent"
    ESCALATED = "escalated"
    SCHEDULED = "scheduled"
    COMPLETION_CLAIMED = "completion_claimed"
    VERIFICATION_REQUESTED = "verification_requested"
    VERIFICATION_CONFIRMED = "verification_confirmed"
    VERIFICATION_REJECTED = "verification_rejected"
    WARRANTY_OPENED = "warranty_opened"
    PROACTIVE_SUGGESTED = "proactive_suggested"
    RESOLVED = "resolved"
    MEMORY_WRITTEN = "memory_written"
    CLOSED = "closed"
    NOTE = "note"
