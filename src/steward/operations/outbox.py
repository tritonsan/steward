"""Transactional outbox delivery with policy-audit and recipient guards.

Only the versioned ``mail.send.v1`` intent is physically deliverable. Other
workflow outbox rows intentionally remain non-deliverable until a channel has
been selected and a typed intent has been written. The dispatcher marks an
intent DISPATCHING before the transport call; an expired lease in that state is
AMBIGUOUS rather than blindly retried.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from steward.config import RuntimeExecutionMode
from steward.domain.clock import Clock
from steward.mail import (
    MailDeliveryError,
    MailTransport,
    OutboundMessage,
    RecipientGuard,
    RecipientNotAllowed,
    RecordingMailTransport,
)
from steward.store import (
    FailureDisposition,
    OperationalStore,
    OutboxItem,
    StaleLeaseToken,
)

__all__ = [
    "MAIL_OUTBOX_KIND",
    "EmailMessagePayload",
    "EmailOutboxPayload",
    "OutboundExecutionMode",
    "OutboundPurpose",
    "OutboxDispatchFailure",
    "OutboxDispatchReport",
    "OutboxDispatcher",
    "OutboxDelivery",
    "mail_outbox_item",
]

MAIL_OUTBOX_KIND = "mail.send.v1"


OutboundExecutionMode = RuntimeExecutionMode


class OutboundPurpose(str, Enum):
    RFQ = "rfq"
    FOLLOW_UP = "follow_up"
    VERIFICATION = "verification"
    MANAGEMENT_ALERT = "management_alert"
    COMMITMENT = "commitment"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class EmailMessagePayload(_Payload):
    to: tuple[str, ...] = Field(min_length=1)
    subject: str = Field(min_length=1)
    body_text: str
    from_address: str = Field(min_length=1)
    from_display_name: str | None = None
    reply_to: str | None = None
    cc: tuple[str, ...] = ()
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    message_id: str = Field(min_length=3)

    @classmethod
    def from_message(cls, message: OutboundMessage) -> EmailMessagePayload:
        if message.message_id is None:
            raise ValueError("durable outbound mail requires a stable message_id")
        return cls(
            to=message.to,
            subject=message.subject,
            body_text=message.body_text,
            from_address=message.from_address,
            from_display_name=message.from_display_name,
            reply_to=message.reply_to,
            cc=message.cc,
            in_reply_to=message.in_reply_to,
            references=message.references,
            message_id=message.message_id,
        )

    def to_message(self) -> OutboundMessage:
        return OutboundMessage(**self.model_dump())


class EmailOutboxPayload(_Payload):
    schema_version: Literal[1] = 1
    purpose: OutboundPurpose
    authority_audit_id: str = Field(min_length=1)
    vendor_id: str | None = None
    message: EmailMessagePayload

    @model_validator(mode="after")
    def _purpose_has_target(self) -> EmailOutboxPayload:
        if self.purpose in (OutboundPurpose.RFQ, OutboundPurpose.COMMITMENT) and not self.vendor_id:
            raise ValueError("vendor-bound mail requires vendor_id")
        return self


@dataclass(frozen=True, slots=True)
class OutboxDelivery:
    outbox_id: str
    purpose: OutboundPurpose
    provider_message_id: str
    attempts: int


@dataclass(frozen=True, slots=True)
class OutboxDispatchFailure:
    outbox_id: str
    error_code: str
    attempts: int
    disposition: FailureDisposition


@dataclass(frozen=True, slots=True)
class OutboxDispatchReport:
    dispatched_at: datetime
    deliveries: tuple[OutboxDelivery, ...] = ()
    failures: tuple[OutboxDispatchFailure, ...] = ()
    skipped_lost_leases: tuple[str, ...] = field(default_factory=tuple)
    pending_remaining: int = 0
    real_delivery_enabled: bool = False


class _DispatchRejected(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


TokenFactory = Callable[[], str]


def _default_token_factory() -> str:
    return f"outbox-lease-{uuid4().hex}"


def _stable_message_id(dedup_key: str, from_address: str) -> str:
    domain = from_address.rpartition("@")[2].strip().lower() or "steward.invalid"
    digest = sha256(dedup_key.encode("utf-8")).hexdigest()[:32]
    return f"<steward-{digest}@{domain}>"


def mail_outbox_item(
    *,
    outbox_id: str,
    case_id: str,
    dedup_key: str,
    purpose: OutboundPurpose,
    authority_audit_id: str,
    message: OutboundMessage,
    created_at: datetime,
    vendor_id: str | None = None,
) -> OutboxItem:
    """Build the only outbox shape the physical mail dispatcher accepts."""
    durable_message = message
    if durable_message.message_id is None:
        durable_message = replace(
            durable_message,
            message_id=_stable_message_id(dedup_key, message.from_address),
        )
    payload = EmailOutboxPayload(
        purpose=purpose,
        authority_audit_id=authority_audit_id,
        vendor_id=vendor_id,
        message=EmailMessagePayload.from_message(durable_message),
    )
    return OutboxItem(
        outbox_id=outbox_id,
        case_id=case_id,
        dedup_key=dedup_key,
        kind=MAIL_OUTBOX_KIND,
        payload=payload.model_dump(mode="json"),
        created_at=created_at,
    )


class OutboxDispatcher:
    """Claim and deliver typed mail intents without widening their authority."""

    __slots__ = (
        "_batch_limit",
        "_clock",
        "_guard",
        "_lease_seconds",
        "_max_attempts",
        "_mode",
        "_store",
        "_token_factory",
        "_transport",
        "_authority_validator",
    )

    def __init__(
        self,
        *,
        store: OperationalStore,
        transport: MailTransport,
        recipient_guard: RecipientGuard,
        clock: Clock,
        execution_mode: OutboundExecutionMode = OutboundExecutionMode.DRY_RUN,
        lease_seconds: int = 300,
        batch_limit: int = 100,
        max_attempts: int = 3,
        token_factory: TokenFactory = _default_token_factory,
        authority_validator: Callable | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if batch_limit <= 0:
            raise ValueError("batch_limit must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if execution_mode is OutboundExecutionMode.DRY_RUN and not isinstance(
            transport, RecordingMailTransport
        ):
            raise TypeError("DRY_RUN requires RecordingMailTransport")
        self._store = store
        self._transport = transport
        self._guard = recipient_guard
        self._clock = clock
        self._mode = execution_mode
        self._lease_seconds = lease_seconds
        self._batch_limit = batch_limit
        self._max_attempts = max_attempts
        self._token_factory = token_factory
        self._authority_validator = authority_validator

    @property
    def real_delivery_enabled(self) -> bool:
        return self._mode is not OutboundExecutionMode.DRY_RUN

    def dispatch(self) -> OutboxDispatchReport:
        started_at = self._clock.now()
        token = self._token_factory()
        claimed = self._store.claim_outbox(
            token=token,
            now=started_at,
            lease_seconds=self._lease_seconds,
            limit=self._batch_limit,
            max_attempts=self._max_attempts,
            kind=MAIL_OUTBOX_KIND,
        )
        deliveries: list[OutboxDelivery] = []
        failures: list[OutboxDispatchFailure] = []
        skipped: list[str] = []

        for item in claimed:
            try:
                payload = self._prepare(item)
            except Exception as exc:  # noqa: BLE001 - item-level poison isolation
                failure = self._record_failure(
                    item=item,
                    token=token,
                    exc=exc,
                    delivery_started=False,
                )
                if failure is None:
                    skipped.append(item.outbox_id)
                else:
                    failures.append(failure)
                continue

            try:
                began = self._store.begin_outbox_delivery(
                    outbox_id=item.outbox_id,
                    token=token,
                    started_at=self._clock.now(),
                )
            except StaleLeaseToken:
                skipped.append(item.outbox_id)
                continue
            if not began:
                skipped.append(item.outbox_id)
                continue

            try:
                provider_message_id = self._transport.send(payload.message.to_message())
                if not provider_message_id or not provider_message_id.strip():
                    raise MailDeliveryError(
                        "transport_missing_message_id",
                        retryable=False,
                        ambiguous=True,
                    )
            except Exception as exc:  # noqa: BLE001 - transport errors are classified below
                failure = self._record_failure(
                    item=item,
                    token=token,
                    exc=exc,
                    delivery_started=True,
                )
                if failure is None:
                    skipped.append(item.outbox_id)
                else:
                    failures.append(failure)
                continue

            try:
                completed = self._store.complete_outbox_claim(
                    outbox_id=item.outbox_id,
                    token=token,
                    delivered_at=self._clock.now(),
                    provider_message_id=provider_message_id,
                )
            except Exception as exc:  # delivery may have happened; never retry blindly
                failure = self._record_failure(
                    item=item,
                    token=token,
                    exc=exc,
                    delivery_started=True,
                    force_ambiguous=True,
                    forced_code="delivery_completion_unknown",
                )
                if failure is None:
                    failures.append(
                        OutboxDispatchFailure(
                            outbox_id=item.outbox_id,
                            error_code="delivery_completion_unknown",
                            attempts=item.attempts,
                            disposition=FailureDisposition.AMBIGUOUS,
                        )
                    )
                else:
                    failures.append(failure)
                continue
            if not completed:
                skipped.append(item.outbox_id)
                continue
            deliveries.append(
                OutboxDelivery(
                    outbox_id=item.outbox_id,
                    purpose=payload.purpose,
                    provider_message_id=provider_message_id.strip(),
                    attempts=item.attempts,
                )
            )

        return OutboxDispatchReport(
            dispatched_at=started_at,
            deliveries=tuple(deliveries),
            failures=tuple(failures),
            skipped_lost_leases=tuple(skipped),
            pending_remaining=len(self._store.pending_outbox(limit=self._batch_limit)),
            real_delivery_enabled=self.real_delivery_enabled,
        )

    def _prepare(self, item: OutboxItem) -> EmailOutboxPayload:
        if item.kind != MAIL_OUTBOX_KIND:
            raise _DispatchRejected("unsupported_outbox_kind")
        try:
            payload = EmailOutboxPayload.model_validate(item.payload)
        except ValidationError as exc:
            raise _DispatchRejected("invalid_mail_payload") from exc
        self._validate_execution_mode(payload)
        self._validate_authority(item, payload)
        self._guard.check(payload.message.to_message().all_recipients)
        if self._authority_validator is not None:
            self._authority_validator(item, payload)
        return payload

    def _validate_execution_mode(self, payload: EmailOutboxPayload) -> None:
        if self._mode is OutboundExecutionMode.DRY_RUN:
            return
        if self._mode is OutboundExecutionMode.LIVE_RFQ:
            if payload.purpose is not OutboundPurpose.RFQ:
                raise _DispatchRejected("execution_mode_blocked")
            return
        if self._mode is OutboundExecutionMode.LIVE_COMMITMENT:
            return
        raise _DispatchRejected("execution_mode_blocked")

    def _validate_authority(
        self,
        item: OutboxItem,
        payload: EmailOutboxPayload,
    ) -> None:
        if item.case_id is None:
            raise _DispatchRejected("missing_case_authority")
        audit = next(
            (
                entry
                for entry in self._store.audit_for(item.case_id)
                if entry.audit_id == payload.authority_audit_id
            ),
            None,
        )
        if audit is None:
            raise _DispatchRejected("authority_audit_not_found")
        if payload.purpose is OutboundPurpose.RFQ:
            if audit.action != "send_rfq" or audit.vendor_id != payload.vendor_id:
                raise _DispatchRejected("authority_audit_mismatch")
        elif payload.purpose is OutboundPurpose.COMMITMENT and (
            audit.action != "commit_quote"
            or audit.vendor_id != payload.vendor_id
            or audit.amount is None
        ):
            raise _DispatchRejected("authority_audit_mismatch")

    def _record_failure(
        self,
        *,
        item: OutboxItem,
        token: str,
        exc: Exception,
        delivery_started: bool,
        force_ambiguous: bool = False,
        forced_code: str | None = None,
    ) -> OutboxDispatchFailure | None:
        code, retryable, ambiguous = _classify_failure(
            exc,
            delivery_started=delivery_started,
        )
        if forced_code is not None:
            code = forced_code
        if force_ambiguous:
            retryable = False
            ambiguous = True
        try:
            disposition = self._store.fail_outbox_claim(
                outbox_id=item.outbox_id,
                token=token,
                failed_at=self._clock.now(),
                error_code=code,
                retryable=retryable,
                max_attempts=self._max_attempts,
                ambiguous=ambiguous,
            )
        except StaleLeaseToken:
            return None
        return OutboxDispatchFailure(
            outbox_id=item.outbox_id,
            error_code=code,
            attempts=item.attempts,
            disposition=disposition,
        )


def _classify_failure(
    exc: Exception,
    *,
    delivery_started: bool,
) -> tuple[str, bool, bool]:
    if isinstance(exc, _DispatchRejected):
        return exc.code, False, False
    if isinstance(exc, RecipientNotAllowed):
        return "recipient_not_allowed", False, False
    if isinstance(exc, MailDeliveryError):
        return exc.code, exc.retryable, exc.ambiguous
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_outbound_message", False, False
    if delivery_started:
        return "delivery_outcome_unknown", False, True
    return "outbox_dispatch_error", False, False
