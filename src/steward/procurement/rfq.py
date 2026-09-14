"""Policy-governed request-for-quote dispatch.

The dispatcher is transport-agnostic, but the demo wires it only to
``RecordingMailTransport``. Every recipient is preflighted before the first
send, and every per-vendor audit entry is recorded before its transport call.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Protocol, runtime_checkable
from uuid import uuid4

from steward.domain.clock import Clock
from steward.domain.enums import ActorType, CaseStatus, EventKind
from steward.domain.models import Asset, AuditEntry, Case, TimelineEvent, Vendor
from steward.mail import (
    AddressScheme,
    MailTransport,
    OutboundMessage,
    RecipientGuard,
)
from steward.policy import PolicyDecision, PolicyEngine
from steward.store import OperationalStore, OutboxItem, TransitionResult

__all__ = [
    "AuditSink",
    "QueuedRfqBatch",
    "RecordingAuditSink",
    "RfqBatch",
    "RfqDispatch",
    "RfqDispatcher",
    "RfqNotAuthorized",
    "RfqOutboxCoordinator",
    "VendorExclusion",
]


class RfqNotAuthorized(RuntimeError):
    """Policy or valid-vendor configuration does not permit an RFQ batch."""


@runtime_checkable
class AuditSink(Protocol):
    def record(self, entry: AuditEntry) -> None: ...


class RecordingAuditSink:
    """Process-local audit sink used by the dry-run demo and tests."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry.model_copy(deep=True))


@dataclass(frozen=True, slots=True)
class VendorExclusion:
    vendor_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RfqDispatch:
    vendor_id: str
    provider_message_id: str
    audit_id: str
    message: OutboundMessage


@dataclass(frozen=True, slots=True)
class RfqBatch:
    case_id: str
    sent_at: datetime
    policy_decision: PolicyDecision
    dispatches: tuple[RfqDispatch, ...]
    exclusions: tuple[VendorExclusion, ...]

    @property
    def contacted_vendor_ids(self) -> tuple[str, ...]:
        return tuple(dispatch.vendor_id for dispatch in self.dispatches)


@dataclass(frozen=True, slots=True)
class _RfqCandidate:
    vendor: Vendor
    message: OutboundMessage


@dataclass(frozen=True, slots=True)
class _PreparedRfq:
    sent_at: datetime
    policy_decision: PolicyDecision
    candidates: tuple[_RfqCandidate, ...]
    exclusions: tuple[VendorExclusion, ...]


@dataclass(frozen=True, slots=True)
class QueuedRfqBatch:
    case: Case
    policy_decision: PolicyDecision
    outbox_items: tuple[OutboxItem, ...]
    audit_entries: tuple[AuditEntry, ...]
    exclusions: tuple[VendorExclusion, ...]
    transition: TransitionResult

    @property
    def queued_vendor_ids(self) -> tuple[str, ...]:
        return tuple(entry.vendor_id for entry in self.audit_entries if entry.vendor_id)


IdFactory = Callable[[str], str]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


class RfqDispatcher:
    """Prepare and dispatch one guarded RFQ per valid policy vendor."""

    __slots__ = (
        "_audit",
        "_clock",
        "_id_factory",
        "_policy",
        "_recipient_guard",
        "_scheme",
        "_transport",
    )

    def __init__(
        self,
        *,
        policy: PolicyEngine,
        transport: MailTransport,
        recipient_guard: RecipientGuard,
        address_scheme: AddressScheme,
        audit_sink: AuditSink,
        clock: Clock,
        id_factory: IdFactory = _new_id,
    ) -> None:
        self._policy = policy
        self._transport = transport
        self._recipient_guard = recipient_guard
        self._scheme = address_scheme
        self._audit = audit_sink
        self._clock = clock
        self._id_factory = id_factory

    def dispatch(
        self,
        *,
        case: Case,
        triage_confidence: float,
        asset: Asset,
        vendors: Iterable[Vendor],
        month_to_date_spend: Decimal = Decimal("0"),
    ) -> RfqBatch:
        prepared = _prepare_rfq(
            policy=self._policy,
            recipient_guard=self._recipient_guard,
            address_scheme=self._scheme,
            clock=self._clock,
            case=case,
            triage_confidence=triage_confidence,
            asset=asset,
            vendors=vendors,
            month_to_date_spend=month_to_date_spend,
        )

        dispatches: list[RfqDispatch] = []
        for candidate in prepared.candidates:
            vendor = candidate.vendor
            audit = AuditEntry(
                audit_id=self._id_factory("audit"),
                case_id=case.case_id,
                at=prepared.sent_at,
                action="send_rfq",
                autonomy_level=prepared.policy_decision.level,
                policy_rule_id=prepared.policy_decision.rule_id,
                reason=prepared.policy_decision.reason,
                vendor_id=vendor.vendor_id,
            )
            self._audit.record(audit)
            provider_message_id = self._transport.send(candidate.message)
            dispatches.append(
                RfqDispatch(
                    vendor_id=vendor.vendor_id,
                    provider_message_id=provider_message_id,
                    audit_id=audit.audit_id,
                    message=candidate.message,
                )
            )

        return RfqBatch(
            case_id=case.case_id,
            sent_at=prepared.sent_at,
            policy_decision=prepared.policy_decision,
            dispatches=tuple(dispatches),
            exclusions=prepared.exclusions,
        )

    def _message(self, *, case: Case, asset: Asset, vendor: Vendor) -> OutboundMessage:
        return _rfq_message(
            address_scheme=self._scheme,
            case=case,
            asset=asset,
            vendor=vendor,
        )


class RfqOutboxCoordinator:
    """Persist RFQ authority and typed mail intents before any transport call."""

    __slots__ = ("_clock", "_policy", "_recipient_guard", "_scheme", "_store")

    def __init__(
        self,
        *,
        store: OperationalStore,
        policy: PolicyEngine,
        recipient_guard: RecipientGuard,
        address_scheme: AddressScheme,
        clock: Clock,
    ) -> None:
        self._store = store
        self._policy = policy
        self._recipient_guard = recipient_guard
        self._scheme = address_scheme
        self._clock = clock

    def _existing_batch(
        self,
        *,
        case_id: str,
        idempotency_key: str,
    ) -> QueuedRfqBatch | None:
        # Local import avoids making procurement package initialization depend
        # on the operations package, whose follow-up module imports procurement.
        from steward.operations.outbox import (
            MAIL_OUTBOX_KIND,
            EmailOutboxPayload,
            OutboundPurpose,
        )

        prefix = f"rfq:v1:{case_id}:"
        suffix = f":{idempotency_key}"
        matches = tuple(
            item
            for item in self._store.outbox_for_case(case_id)
            if item.kind == MAIL_OUTBOX_KIND
            and item.dedup_key.startswith(prefix)
            and item.dedup_key.endswith(suffix)
        )
        if not matches:
            return None
        payload_by_vendor: dict[str, tuple[OutboxItem, EmailOutboxPayload]] = {}
        for item in matches:
            payload = EmailOutboxPayload.model_validate(item.payload)
            if payload.purpose is not OutboundPurpose.RFQ or not payload.vendor_id:
                raise RuntimeError("persisted RFQ batch contains an invalid mail intent")
            if payload.vendor_id in payload_by_vendor:
                raise RuntimeError("persisted RFQ batch contains a duplicate vendor")
            payload_by_vendor[payload.vendor_id] = (item, payload)

        event_id = _stable_id("event-rfq", idempotency_key, case_id)
        event = next(
            (
                candidate
                for candidate in self._store.timeline_for(case_id)
                if candidate.event_id == event_id
            ),
            None,
        )
        vendor_ids = (
            tuple(event.payload.get("vendor_ids", ()))
            if event is not None
            else tuple(sorted(payload_by_vendor))
        )
        if set(vendor_ids) != set(payload_by_vendor):
            raise RuntimeError("persisted RFQ batch metadata is inconsistent")

        audit_by_id = {entry.audit_id: entry for entry in self._store.audit_for(case_id)}
        ordered_items: list[OutboxItem] = []
        ordered_audits: list[AuditEntry] = []
        for vendor_id in vendor_ids:
            item, payload = payload_by_vendor[vendor_id]
            audit = audit_by_id.get(payload.authority_audit_id)
            if audit is None or audit.action != "send_rfq" or audit.vendor_id != vendor_id:
                raise RuntimeError("persisted RFQ authority audit is inconsistent")
            ordered_items.append(item)
            ordered_audits.append(audit)

        stored_case = self._store.get_case(case_id)
        version = self._store.case_version(case_id)
        if stored_case is None or version is None:
            raise RuntimeError("persisted RFQ batch lost its case")
        first_audit = ordered_audits[0]
        decision = PolicyDecision(
            level=first_audit.autonomy_level,
            rule_id=first_audit.policy_rule_id,
            reason=first_audit.reason,
            spend_cap=stored_case.spend_cap,
            currency=stored_case.currency,
            allowed_vendor_ids=vendor_ids,
        )
        return QueuedRfqBatch(
            case=stored_case,
            policy_decision=decision,
            outbox_items=tuple(ordered_items),
            audit_entries=tuple(ordered_audits),
            exclusions=(),
            transition=TransitionResult(applied=False, version=version),
        )

    def queue(
        self,
        *,
        case: Case,
        expected_version: int,
        idempotency_key: str,
        triage_confidence: float,
        asset: Asset,
        vendors: Iterable[Vendor],
        month_to_date_spend: Decimal = Decimal("0"),
    ) -> QueuedRfqBatch:
        if not idempotency_key.strip():
            raise ValueError("idempotency key must not be blank")
        existing = self._existing_batch(
            case_id=case.case_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing
        prepared = _prepare_rfq(
            policy=self._policy,
            recipient_guard=self._recipient_guard,
            address_scheme=self._scheme,
            clock=self._clock,
            case=case,
            triage_confidence=triage_confidence,
            asset=asset,
            vendors=vendors,
            month_to_date_spend=month_to_date_spend,
        )

        # Local import avoids making procurement package initialization depend
        # on the operations package, whose follow-up module imports procurement.
        from steward.operations.outbox import OutboundPurpose, mail_outbox_item

        audits: list[AuditEntry] = []
        outbox: list[OutboxItem] = []
        for candidate in prepared.candidates:
            vendor_id = candidate.vendor.vendor_id
            audit = AuditEntry(
                audit_id=_stable_id("audit-rfq", idempotency_key, vendor_id),
                case_id=case.case_id,
                at=prepared.sent_at,
                action="send_rfq",
                autonomy_level=prepared.policy_decision.level,
                policy_rule_id=prepared.policy_decision.rule_id,
                reason=prepared.policy_decision.reason,
                vendor_id=vendor_id,
            )
            dedup_key = f"rfq:v1:{case.case_id}:{vendor_id}:{idempotency_key}"
            audits.append(audit)
            outbox.append(
                mail_outbox_item(
                    outbox_id=_stable_id("outbox-rfq", idempotency_key, vendor_id),
                    case_id=case.case_id,
                    dedup_key=dedup_key,
                    purpose=OutboundPurpose.RFQ,
                    authority_audit_id=audit.audit_id,
                    message=candidate.message,
                    created_at=prepared.sent_at,
                    vendor_id=vendor_id,
                )
            )

        updated = case.model_copy(deep=True)
        updated.status = CaseStatus.PLANNING
        updated.updated_at = prepared.sent_at
        event = TimelineEvent(
            event_id=_stable_id("event-rfq", idempotency_key, case.case_id),
            case_id=case.case_id,
            at=prepared.sent_at,
            kind=EventKind.PLAN_DRAFTED,
            actor=ActorType.AGENT,
            summary=(f"Queued {len(outbox)} policy-authorized RFQ emails for guarded delivery."),
            refs=[entry.audit_id for entry in audits],
            payload={
                "outbox_ids": [item.outbox_id for item in outbox],
                "vendor_ids": [candidate.vendor.vendor_id for candidate in prepared.candidates],
                "policy_rule_id": prepared.policy_decision.rule_id,
                "delivery_pending": True,
            },
        )
        transition = self._store.save_transition(
            case=updated,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            timeline_events=(event,),
            audit_entries=tuple(audits),
            outbox_items=tuple(outbox),
        )
        return QueuedRfqBatch(
            case=updated,
            policy_decision=prepared.policy_decision,
            outbox_items=tuple(outbox),
            audit_entries=tuple(audits),
            exclusions=prepared.exclusions,
            transition=transition,
        )


def _prepare_rfq(
    *,
    policy: PolicyEngine,
    recipient_guard: RecipientGuard,
    address_scheme: AddressScheme,
    clock: Clock,
    case: Case,
    triage_confidence: float,
    asset: Asset,
    vendors: Iterable[Vendor],
    month_to_date_spend: Decimal,
) -> _PreparedRfq:
    if case.asset_id != asset.asset_id or case.category is not asset.category:
        raise ValueError("case and RFQ asset do not match")

    decision = policy.evaluate_intake(
        category=case.category,
        triage_confidence=triage_confidence,
        month_to_date_spend=month_to_date_spend,
    )
    if not decision.may_contact_third_parties:
        raise RfqNotAuthorized(
            f"policy rule {decision.rule_id} does not permit third-party contact"
        )

    by_id = _index_vendors(vendors)
    eligible: list[Vendor] = []
    exclusions: list[VendorExclusion] = []
    for vendor_id in decision.allowed_vendor_ids:
        vendor = by_id.get(vendor_id)
        if vendor is None:
            exclusions.append(VendorExclusion(vendor_id, "vendor record is missing"))
            continue
        if not vendor.allowlisted:
            exclusions.append(VendorExclusion(vendor_id, "vendor is not globally allowlisted"))
            continue
        if case.category not in vendor.categories:
            exclusions.append(VendorExclusion(vendor_id, "vendor does not serve the case category"))
            continue
        eligible.append(vendor)

    if not eligible:
        raise RfqNotAuthorized("policy permits RFQs but no valid allowed vendor remains")

    # Preflight the complete batch before writing an audit or outbox row.
    recipient_guard.check(vendor.email for vendor in eligible)
    prepared_at = clock.now()
    return _PreparedRfq(
        sent_at=prepared_at,
        policy_decision=decision,
        candidates=tuple(
            _RfqCandidate(
                vendor=vendor,
                message=_rfq_message(
                    address_scheme=address_scheme,
                    case=case,
                    asset=asset,
                    vendor=vendor,
                ),
            )
            for vendor in eligible
        ),
        exclusions=tuple(exclusions),
    )


def _rfq_message(
    *,
    address_scheme: AddressScheme,
    case: Case,
    asset: Asset,
    vendor: Vendor,
) -> OutboundMessage:
    return OutboundMessage(
        to=(vendor.email,),
        subject=f"Quote request: {asset.label} — {case.title}",
        body_text=(
            f"Hello {vendor.name},\n\n"
            f"Please quote for inspection and repair of {asset.label}.\n\n"
            f"Observed issue: {case.title}.\n"
            f"Case reference: {case.case_id}.\n\n"
            "Please include:\n"
            f"- an all-in amount in {case.currency};\n"
            "- the work and parts included;\n"
            "- your earliest attendance time; and\n"
            "- any validity period or exclusions.\n\n"
            "This is a request for quotation only. No work is authorized by this email.\n\n"
            "Regards,\nSteward"
        ),
        from_address=address_scheme.management_address,
        from_display_name=address_scheme.management_display_name,
        reply_to=address_scheme.case_reply_address(case.reply_token),
    )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _index_vendors(vendors: Iterable[Vendor]) -> dict[str, Vendor]:
    indexed: dict[str, Vendor] = {}
    for vendor in vendors:
        if vendor.vendor_id in indexed:
            raise ValueError(f"duplicate vendor id: {vendor.vendor_id}")
        indexed[vendor.vendor_id] = vendor.model_copy(deep=True)
    return indexed
