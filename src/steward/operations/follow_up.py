"""Deterministic follow-up tracking for planned vendor attendance.

This module never decides whether work should be purchased.  It accepts the
already source-traced quote decision, rechecks whether vendor contact is still
permitted, and records reminders through ``RecordingMailTransport`` only.
Live delivery is deliberately unavailable in this slice.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Protocol
from uuid import uuid4

from steward.domain.clock import Clock
from steward.domain.enums import TERMINAL_STATUSES, ActorType, CaseStatus, EventKind
from steward.domain.models import AuditEntry, Case, TimelineEvent, Vendor
from steward.mail import (
    AddressScheme,
    OutboundMessage,
    RecipientGuard,
    RecordingMailTransport,
)
from steward.policy import PolicyEngine
from steward.procurement import CommitmentDisposition, QuoteDecisionPackage

__all__ = [
    "ExecutionMode",
    "FollowUpCoordinator",
    "FollowUpDisposition",
    "FollowUpResult",
    "LiveExecutionDisabled",
]

IdFactory = Callable[[str], str]


class AuditSink(Protocol):
    def record(self, entry: AuditEntry) -> None: ...


class ExecutionMode(str, Enum):
    """How an outbound operational action is executed."""

    DRY_RUN = "dry_run"
    LIVE = "live"


class FollowUpDisposition(str, Enum):
    TRACKING_STARTED = "tracking_started"
    ALREADY_TRACKING = "already_tracking"
    NOT_DUE = "not_due"
    FOLLOW_UP_RECORDED = "follow_up_recorded"
    ESCALATED = "escalated"
    INACTIVE = "inactive"


class LiveExecutionDisabled(RuntimeError):
    """Raised when this dry-run slice is asked to deliver real mail."""


@dataclass(frozen=True, slots=True)
class FollowUpResult:
    case: Case
    disposition: FollowUpDisposition
    timeline_events: tuple[TimelineEvent, ...] = ()
    audit_entries: tuple[AuditEntry, ...] = ()
    provider_message_ids: tuple[str, ...] = ()


class FollowUpCoordinator:
    """Track one accepted plan, record due reminders, then escalate.

    The returned ``Case`` is the idempotency state.  Calling ``sweep`` again
    with that returned case at the same clock time cannot record another
    reminder because ``next_action_due_at`` has moved forward.  Persistence
    adapters can store the returned case and events atomically.
    """

    __slots__ = (
        "_audit",
        "_clock",
        "_id_factory",
        "_mode",
        "_policy",
        "_recipient_guard",
        "_scheme",
        "_transport",
    )

    def __init__(
        self,
        *,
        policy: PolicyEngine,
        transport: RecordingMailTransport,
        recipient_guard: RecipientGuard,
        address_scheme: AddressScheme,
        audit_sink: AuditSink,
        clock: Clock,
        execution_mode: ExecutionMode = ExecutionMode.DRY_RUN,
        id_factory: IdFactory = lambda prefix: f"{prefix}-{uuid4().hex}",
    ) -> None:
        if execution_mode is not ExecutionMode.DRY_RUN:
            raise LiveExecutionDisabled(
                "live follow-up delivery is not implemented; use DRY_RUN explicitly"
            )
        if not isinstance(transport, RecordingMailTransport):
            raise TypeError("dry-run follow-up requires RecordingMailTransport")
        self._policy = policy
        self._transport = transport
        self._recipient_guard = recipient_guard
        self._scheme = address_scheme
        self._audit = audit_sink
        self._clock = clock
        self._mode = execution_mode
        self._id_factory = id_factory

    def start_tracking(
        self,
        *,
        case: Case,
        decision: QuoteDecisionPackage,
        vendor: Vendor,
        scheduled_for: datetime | None = None,
        schedule_source_id: str | None = None,
    ) -> FollowUpResult:
        """Put the exact winning quote on Steward's follow-up desk.

        This records a plan only.  It neither constructs nor sends a commitment
        message, and it accepts only a decision in ``AUTHORIZED_NOT_SENT``.
        """
        quote = decision.recommended_quote
        self._validate_exact_target(case=case, decision=decision, vendor=vendor)
        if decision.commitment_disposition is not CommitmentDisposition.AUTHORIZED_NOT_SENT:
            raise ValueError("follow-up tracking requires an authorized dry-run decision")
        if not decision.commitment_decision.may_commit_spend:
            raise ValueError("follow-up tracking cannot widen a non-authorized decision")
        if case.status in TERMINAL_STATUSES:
            raise ValueError("a terminal case cannot start follow-up tracking")

        planned_at = _aware_utc(scheduled_for or quote.earliest_onsite_at)
        if planned_at is None:
            raise ValueError("follow-up tracking requires a source-backed attendance time")
        quote_availability = _aware_utc(quote.earliest_onsite_at)
        if (
            scheduled_for is not None
            and planned_at != quote_availability
            and not schedule_source_id
        ):
            raise ValueError("a schedule differing from quote availability needs a source id")

        if case.accepted_quote_id is not None:
            if case.accepted_quote_id != quote.quote_id:
                raise ValueError("case is already bound to a different quote")
            if case.scheduled_for == planned_at and case.next_action_due_at is not None:
                return FollowUpResult(
                    case=case.model_copy(deep=True),
                    disposition=FollowUpDisposition.ALREADY_TRACKING,
                )

        now = self._clock.now()
        updated = case.model_copy(deep=True)
        updated.status = CaseStatus.SCHEDULED
        updated.accepted_quote_id = quote.quote_id
        updated.autonomy_level = decision.commitment_decision.level
        updated.scheduled_for = planned_at
        updated.next_action_due_at = planned_at + timedelta(
            hours=self._policy.settings.follow_up_grace_hours
        )
        updated.follow_up_count = 0
        updated.escalated_at = None
        updated.updated_at = now
        source_refs = [quote.quote_id]
        if quote.source_email_message_id:
            source_refs.append(quote.source_email_message_id)
        if schedule_source_id:
            source_refs.append(schedule_source_id)
        event = TimelineEvent(
            event_id=self._id_factory("event"),
            case_id=case.case_id,
            at=now,
            kind=EventKind.SCHEDULED,
            actor=ActorType.SYSTEM,
            summary=(
                f"Started dry-run tracking for {vendor.name}'s planned attendance at "
                f"{planned_at.isoformat()}; no commitment was sent."
            ),
            refs=source_refs,
            payload={
                "execution_mode": self._mode.value,
                "vendor_id": vendor.vendor_id,
                "quote_id": quote.quote_id,
                "scheduled_for": planned_at.isoformat(),
                "next_action_due_at": updated.next_action_due_at.isoformat(),
                "commitment_sent": False,
            },
        )
        return FollowUpResult(
            case=updated,
            disposition=FollowUpDisposition.TRACKING_STARTED,
            timeline_events=(event,),
        )

    def sweep(
        self,
        *,
        case: Case,
        decision: QuoteDecisionPackage,
        vendor: Vendor,
        triage_confidence: float,
        month_to_date_spend: Decimal = Decimal("0"),
    ) -> FollowUpResult:
        """Perform at most one due action for a tracked case."""
        self._validate_exact_target(case=case, decision=decision, vendor=vendor)
        now = self._clock.now()
        if (
            case.status in TERMINAL_STATUSES
            or case.status is CaseStatus.RESOLVED
            or case.escalated_at is not None
            or case.next_action_due_at is None
        ):
            return FollowUpResult(
                case=case.model_copy(deep=True),
                disposition=FollowUpDisposition.INACTIVE,
            )
        if now < case.next_action_due_at:
            return FollowUpResult(
                case=case.model_copy(deep=True),
                disposition=FollowUpDisposition.NOT_DUE,
            )

        current_policy = self._policy.evaluate_intake(
            category=case.category,
            triage_confidence=triage_confidence,
            month_to_date_spend=month_to_date_spend,
        )
        vendor_still_valid = (
            vendor.allowlisted
            and case.category in vendor.categories
            and vendor.vendor_id in current_policy.allowed_vendor_ids
        )
        if not current_policy.may_contact_third_parties or not vendor_still_valid:
            reason = (
                current_policy.reason
                if not current_policy.may_contact_third_parties
                else "The tracked vendor is no longer allowed for this category."
            )
            return self._escalate(
                case=case,
                decision=decision,
                vendor=vendor,
                now=now,
                reason=reason,
                policy_rule_id=current_policy.rule_id,
            )

        if case.follow_up_count >= self._policy.settings.max_follow_ups_before_escalation:
            return self._escalate(
                case=case,
                decision=decision,
                vendor=vendor,
                now=now,
                reason=(
                    f"{vendor.name} did not resolve the tracked action after "
                    f"{case.follow_up_count} recorded follow-up(s)."
                ),
                policy_rule_id=current_policy.rule_id,
            )

        # Preflight before writing the audit.  A poisoned vendor address leaves
        # both the transport and the audit sink untouched.
        self._recipient_guard.check((vendor.email,))
        audit = AuditEntry(
            audit_id=self._id_factory("audit"),
            case_id=case.case_id,
            at=now,
            action="send_follow_up_dry_run",
            autonomy_level=current_policy.level,
            policy_rule_id=current_policy.rule_id,
            reason=current_policy.reason,
            amount=decision.recommended_quote.amount,
            vendor_id=vendor.vendor_id,
        )
        message = self._message(case=case, decision=decision, vendor=vendor)
        self._audit.record(audit)
        provider_message_id = self._transport.send(message)

        updated = case.model_copy(deep=True)
        updated.follow_up_count += 1
        updated.next_action_due_at = now + timedelta(
            hours=self._policy.settings.follow_up_grace_hours
        )
        updated.updated_at = now
        event = TimelineEvent(
            event_id=self._id_factory("event"),
            case_id=case.case_id,
            at=now,
            kind=EventKind.FOLLOW_UP_SENT,
            actor=ActorType.AGENT,
            summary=(
                f"Recorded dry-run follow-up {updated.follow_up_count} to {vendor.name}; "
                "nothing was delivered."
            ),
            refs=[
                audit.audit_id,
                provider_message_id,
                decision.recommended_quote.quote_id,
            ],
            payload={
                "execution_mode": self._mode.value,
                "vendor_id": vendor.vendor_id,
                "follow_up_count": updated.follow_up_count,
                "next_action_due_at": updated.next_action_due_at.isoformat(),
                "provider_message_id": provider_message_id,
                "delivered": False,
            },
        )
        return FollowUpResult(
            case=updated,
            disposition=FollowUpDisposition.FOLLOW_UP_RECORDED,
            timeline_events=(event,),
            audit_entries=(audit,),
            provider_message_ids=(provider_message_id,),
        )

    def _escalate(
        self,
        *,
        case: Case,
        decision: QuoteDecisionPackage,
        vendor: Vendor,
        now: datetime,
        reason: str,
        policy_rule_id: str,
    ) -> FollowUpResult:
        updated = case.model_copy(deep=True)
        updated.status = CaseStatus.AWAITING_APPROVAL
        updated.escalated_at = now
        updated.next_action_due_at = None
        updated.updated_at = now
        event = TimelineEvent(
            event_id=self._id_factory("event"),
            case_id=case.case_id,
            at=now,
            kind=EventKind.ESCALATED,
            actor=ActorType.SYSTEM,
            summary=f"Escalated the overdue {vendor.name} action to management. {reason}",
            refs=[decision.recommended_quote.quote_id, policy_rule_id],
            payload={
                "execution_mode": self._mode.value,
                "vendor_id": vendor.vendor_id,
                "follow_up_count": case.follow_up_count,
                "policy_rule_id": policy_rule_id,
                "commitment_sent": False,
            },
        )
        return FollowUpResult(
            case=updated,
            disposition=FollowUpDisposition.ESCALATED,
            timeline_events=(event,),
        )

    def _message(
        self,
        *,
        case: Case,
        decision: QuoteDecisionPackage,
        vendor: Vendor,
    ) -> OutboundMessage:
        quote = decision.recommended_quote
        source_message_id = quote.source_email_message_id
        return OutboundMessage(
            to=(vendor.email,),
            subject=f"[DRY RUN] Follow-up: {case.title}",
            body_text=(
                "DRY-RUN RECORD — this message is captured locally and is not delivered.\n\n"
                f"Hello {vendor.name},\n\n"
                f"We are following up on the planned attendance for case {case.case_id}.\n"
                "Planned attendance: "
                f"{case.scheduled_for.isoformat() if case.scheduled_for else 'not recorded'}.\n"
                f"Quoted scope: {quote.scope}\n"
                f"Quoted amount: {quote.amount} {quote.currency}.\n\n"
                "Please provide a status update. This follow-up does not authorize work "
                "or change the quoted scope or price.\n\n"
                "Regards,\nSteward"
            ),
            from_address=self._scheme.management_address,
            from_display_name=self._scheme.management_display_name,
            reply_to=self._scheme.case_reply_address(case.reply_token),
            in_reply_to=source_message_id,
            references=(source_message_id,) if source_message_id else (),
        )

    @staticmethod
    def _validate_exact_target(
        *,
        case: Case,
        decision: QuoteDecisionPackage,
        vendor: Vendor,
    ) -> None:
        quote = decision.recommended_quote
        if decision.case_id != case.case_id or quote.case_id != case.case_id:
            raise ValueError("case and quote decision do not match")
        if vendor.vendor_id != quote.vendor_id:
            raise ValueError("follow-up vendor is not the recommended quote vendor")
        if not vendor.allowlisted or case.category not in vendor.categories:
            raise ValueError("follow-up vendor is not valid for the case category")
        if case.accepted_quote_id is not None and case.accepted_quote_id != quote.quote_id:
            raise ValueError("case accepted quote does not match the recommendation")


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("follow-up timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
