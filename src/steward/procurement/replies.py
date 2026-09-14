"""Durable, source-traced vendor reply and quote round-trip orchestration.

Routing and sender authorization are deterministic and happen before a model is
called. The cleaned email body and raw S3 pointer are persisted first, so a
transient extraction failure can be retried without losing the vendor reply.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from steward.agents import QuoteExtraction, QuoteExtractor
from steward.domain.enums import (
    TERMINAL_STATUSES,
    ActorType,
    CaseStatus,
    EventKind,
)
from steward.domain.models import Case, Quote, TimelineEvent, UtcDatetime, Vendor
from steward.mail import AddressScheme, InboundMessage
from steward.procurement.quotes import NoQuoteFound, QuoteEvidenceError, QuoteIngestor
from steward.store import (
    ConcurrencyConflict,
    OperationalStore,
    OutboxItem,
    OutboxStatus,
    WorkflowArtifact,
)

__all__ = [
    "NO_QUOTE_ARTIFACT_KIND",
    "QUOTE_ARTIFACT_KIND",
    "VENDOR_EMAIL_ARTIFACT_KIND",
    "StoredVendorEmail",
    "VendorReplyAuthorizationError",
    "VendorReplyConflictError",
    "VendorReplyDisposition",
    "VendorReplyError",
    "VendorReplyResult",
    "VendorReplyRouteError",
    "VendorReplyService",
]

VENDOR_EMAIL_ARTIFACT_KIND = "vendor_email.v1"
QUOTE_ARTIFACT_KIND = "vendor_quote.v1"
NO_QUOTE_ARTIFACT_KIND = "vendor_no_quote.v1"


class VendorReplyError(ValueError):
    """Base class for deterministic inbound vendor-reply rejection."""


class VendorReplyRouteError(VendorReplyError):
    """The recipient envelope does not identify exactly one Steward case."""


class VendorReplyAuthorizationError(VendorReplyError):
    """The sender cannot prove it is replying to a delivered RFQ."""


class VendorReplyConflictError(VendorReplyError):
    """A stable source identity was reused with different immutable content."""


class VendorReplyDisposition(str, Enum):
    QUOTE_RECORDED = "quote_recorded"
    NO_QUOTE_RECORDED = "no_quote_recorded"
    DUPLICATE = "duplicate"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class StoredVendorEmail(_Record):
    """Sanitized email evidence retained before any model invocation."""

    schema_version: Literal[1] = 1
    case_id: str = Field(min_length=1)
    vendor_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1, max_length=998)
    from_address: str = Field(min_length=3, max_length=320)
    to_addresses: tuple[str, ...] = ()
    cc_addresses: tuple[str, ...] = ()
    delivered_to: str | None = None
    subject: str = Field(default="", max_length=1000)
    body_text: str = Field(min_length=1, max_length=100_000)
    body_sha256: str = Field(min_length=64, max_length=64)
    received_at: UtcDatetime
    in_reply_to: str | None = Field(default=None, max_length=998)
    references: tuple[str, ...] = ()
    raw_ref: str | None = Field(default=None, max_length=2000)
    rfq_outbox_id: str = Field(min_length=1)
    rfq_sent_at: UtcDatetime

    def to_inbound(self) -> InboundMessage:
        return InboundMessage(
            message_id=self.source_message_id,
            from_address=self.from_address,
            from_display_name="",
            to_addresses=self.to_addresses,
            cc_addresses=self.cc_addresses,
            delivered_to=self.delivered_to,
            subject=self.subject,
            body_text=self.body_text,
            body_full_text=self.body_text,
            received_at=self.received_at,
            in_reply_to=self.in_reply_to,
            references=self.references,
            raw_ref=self.raw_ref,
        )


@dataclass(frozen=True, slots=True)
class VendorReplyResult:
    disposition: VendorReplyDisposition
    case: Case
    vendor_id: str
    email_artifact_id: str
    quote_artifact_id: str | None = None
    quote: Quote | None = None
    extraction: QuoteExtraction | None = None


@dataclass(frozen=True, slots=True)
class _Route:
    case: Case
    vendor: Vendor
    rfq: OutboxItem
    rfq_sent_at: datetime


class VendorReplyService:
    """Persist a routed vendor email, then extract and persist its quote."""

    __slots__ = ("_extractor", "_scheme", "_store", "_vendors_by_email", "_continuations")

    def __init__(
        self,
        *,
        store: OperationalStore,
        extractor: QuoteExtractor,
        address_scheme: AddressScheme,
        vendors: tuple[Vendor, ...],
        continuations=None,
    ) -> None:
        by_email: dict[str, Vendor] = {}
        for vendor in vendors:
            email = _address(vendor.email)
            if not email:
                raise ValueError(f"vendor {vendor.vendor_id} has no valid email address")
            if email in by_email:
                raise ValueError(f"duplicate vendor email address: {email}")
            by_email[email] = vendor.model_copy(deep=True)
        self._store = store
        self._extractor = extractor
        self._scheme = address_scheme
        self._vendors_by_email = by_email
        self._continuations = continuations

    def ingest(
        self,
        inbound: InboundMessage,
        *,
        source_id: str,
    ) -> VendorReplyResult:
        safe_source_id = source_id.strip()
        if not safe_source_id or len(safe_source_id) > 2000:
            raise VendorReplyRouteError("vendor reply source_id is invalid")
        route = self._route(inbound)
        stored = self._stored_email(route=route, inbound=inbound)
        email_artifact_id = _stable_id(
            "vendor-email",
            route.vendor.vendor_id,
            stored.source_message_id,
        )
        quote_artifact_id = _stable_id("quote", email_artifact_id)
        no_quote_artifact_id = _stable_id("no-quote", email_artifact_id)

        existing_email = self._store.artifact(
            VENDOR_EMAIL_ARTIFACT_KIND,
            email_artifact_id,
        )
        if existing_email is not None:
            persisted = StoredVendorEmail.model_validate(existing_email.payload)
            self._validate_replay(stored, persisted)
            stored = persisted
            existing_result = self._existing_result(
                case_id=route.case.case_id,
                vendor_id=route.vendor.vendor_id,
                email_artifact_id=email_artifact_id,
                quote_artifact_id=quote_artifact_id,
                no_quote_artifact_id=no_quote_artifact_id,
            )
            if existing_result is not None:
                return existing_result
        else:
            if route.case.status in TERMINAL_STATUSES:
                raise VendorReplyAuthorizationError(
                    "a new vendor reply cannot mutate a terminal case"
                )
            self._persist_email(
                route=route,
                stored=stored,
                source_id=safe_source_id,
                artifact_id=email_artifact_id,
            )

        current = self._store.get_case(route.case.case_id)
        if current is None:
            raise RuntimeError("vendor reply case disappeared before extraction")
        if current.status in TERMINAL_STATUSES:
            raise VendorReplyAuthorizationError(
                "a pending extraction cannot mutate a terminal case"
            )

        ingestor = QuoteIngestor(
            extractor=self._extractor,
            address_scheme=self._scheme,
            id_factory=lambda _prefix: quote_artifact_id,
        )
        try:
            ingestion = ingestor.ingest(
                case=current,
                vendor=route.vendor,
                inbound=stored.to_inbound(),
                rfq_sent_at=stored.rfq_sent_at,
            )
        except (NoQuoteFound, QuoteEvidenceError) as exc:
            return self._persist_no_quote(
                case_id=current.case_id,
                vendor_id=route.vendor.vendor_id,
                email_artifact_id=email_artifact_id,
                artifact_id=no_quote_artifact_id,
                received_at=stored.received_at,
                reason=str(exc),
            )

        return self._persist_quote(
            case_id=current.case_id,
            vendor_id=route.vendor.vendor_id,
            email_artifact_id=email_artifact_id,
            artifact_id=quote_artifact_id,
            quote=ingestion.quote,
            extraction=ingestion.extraction,
        )

    def _route(
        self, inbound: InboundMessage, *, allow_unreadable_attachment: bool = False
    ) -> _Route:
        tokens = _case_tokens(self._scheme, inbound)
        if not tokens:
            raise VendorReplyRouteError("vendor reply has no Steward case token")
        if len(tokens) != 1:
            raise VendorReplyRouteError("vendor reply names multiple Steward cases")
        case = self._store.case_for_reply_token(next(iter(tokens)))
        if case is None:
            raise VendorReplyRouteError("vendor reply token does not identify an open case")

        sender = _address(inbound.from_address)
        vendor = self._vendors_by_email.get(sender)
        if vendor is None:
            raise VendorReplyAuthorizationError("vendor reply sender is not registered")
        if case.category not in vendor.categories:
            raise VendorReplyAuthorizationError(
                "vendor reply sender does not serve the case category"
            )
        if inbound.message_id is None or not inbound.message_id.strip():
            raise VendorReplyRouteError("vendor reply requires an RFC Message-ID")
        if not inbound.body_text.strip() and not (
            allow_unreadable_attachment and inbound.unreadable_attachments
        ):
            # Preserve QuoteIngestor's no-quote semantics but never persist a
            # blank artifact that cannot be retried or reviewed meaningfully.
            raise VendorReplyRouteError("vendor reply body is empty")
        if len(inbound.body_text) > 100_000:
            raise VendorReplyRouteError("vendor reply body exceeds the safe extraction limit")

        proof = self._delivered_rfq(
            case=case,
            vendor=vendor,
            inbound=inbound,
        )
        if proof.delivered_at is None:
            raise RuntimeError("delivered RFQ proof lost its delivery timestamp")
        if inbound.received_at < proof.delivered_at:
            raise VendorReplyAuthorizationError("vendor reply predates RFQ delivery")
        return _Route(
            case=case,
            vendor=vendor,
            rfq=proof,
            rfq_sent_at=proof.delivered_at,
        )

    def _delivered_rfq(
        self,
        *,
        case: Case,
        vendor: Vendor,
        inbound: InboundMessage,
    ) -> OutboxItem:
        # Local import avoids a procurement <-> operations package cycle.
        from steward.operations.outbox import (
            MAIL_OUTBOX_KIND,
            EmailOutboxPayload,
            OutboundPurpose,
        )

        sender = _address(inbound.from_address)
        threaded: list[OutboxItem] = []
        rfqs: list[OutboxItem] = []
        matched_case_thread = False
        thread_ids = {
            value.strip()
            for value in (inbound.in_reply_to, *inbound.references)
            if value and value.strip()
        }
        for item in self._store.outbox_for_case(case.case_id):
            if (
                item.kind != MAIL_OUTBOX_KIND
                or item.status is not OutboxStatus.DELIVERED
                or item.delivered_at is None
            ):
                continue
            try:
                payload = EmailOutboxPayload.model_validate(item.payload)
            except ValueError:
                continue
            if payload.vendor_id != vendor.vendor_id:
                continue
            recipients = {_address(address) for address in payload.message.to}
            if sender not in recipients:
                continue
            matched = _matches_delivered_thread(item, payload.message.message_id, thread_ids)
            if matched and item.delivered_at <= inbound.received_at:
                matched_case_thread = True
            if payload.purpose is not OutboundPurpose.RFQ:
                continue
            rfqs.append(item)
            if matched:
                threaded.append(item)

        if thread_ids:
            if not matched_case_thread:
                raise VendorReplyAuthorizationError(
                    "vendor reply threading does not match a delivered message for this case"
                )
            # A normal Reply to the service order or appointment acceptance starts
            # a new thread. It still needs the original delivered RFQ as authority.
            candidates = threaded or rfqs
        else:
            candidates = rfqs
        if not candidates:
            raise VendorReplyAuthorizationError("vendor has no delivered RFQ for this case")
        return max(
            candidates,
            key=lambda item: (item.delivered_at, item.outbox_id),
        )

    def _stored_email(
        self,
        *,
        route: _Route,
        inbound: InboundMessage,
    ) -> StoredVendorEmail:
        body = inbound.body_text.strip()
        return StoredVendorEmail(
            case_id=route.case.case_id,
            vendor_id=route.vendor.vendor_id,
            source_message_id=inbound.message_id.strip() if inbound.message_id else "",
            from_address=_address(inbound.from_address),
            to_addresses=tuple(inbound.to_addresses),
            cc_addresses=tuple(inbound.cc_addresses),
            delivered_to=inbound.delivered_to,
            subject=inbound.subject.strip(),
            body_text=body,
            body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            received_at=inbound.received_at,
            in_reply_to=inbound.in_reply_to,
            references=tuple(inbound.references),
            raw_ref=inbound.raw_ref,
            rfq_outbox_id=route.rfq.outbox_id,
            rfq_sent_at=route.rfq_sent_at,
        )

    @staticmethod
    def _validate_replay(
        candidate: StoredVendorEmail,
        persisted: StoredVendorEmail,
    ) -> None:
        immutable = (
            "case_id",
            "vendor_id",
            "source_message_id",
            "from_address",
            "body_sha256",
            "received_at",
            "rfq_outbox_id",
        )
        if any(getattr(candidate, field) != getattr(persisted, field) for field in immutable):
            raise VendorReplyConflictError(
                "vendor Message-ID was replayed with different immutable content"
            )

    def _persist_email(
        self,
        *,
        route: _Route,
        stored: StoredVendorEmail,
        source_id: str,
        artifact_id: str,
    ) -> None:
        artifact = WorkflowArtifact(
            artifact_id=artifact_id,
            case_id=route.case.case_id,
            kind=VENDOR_EMAIL_ARTIFACT_KIND,
            created_at=stored.received_at,
            source_ids=tuple(
                dict.fromkeys(
                    value
                    for value in (
                        source_id,
                        stored.source_message_id,
                        stored.raw_ref,
                        stored.rfq_outbox_id,
                    )
                    if value
                )
            ),
            payload=stored.model_dump(mode="json"),
        )
        event = TimelineEvent(
            event_id=_stable_id("event-vendor-reply", artifact_id),
            case_id=route.case.case_id,
            at=stored.received_at,
            kind=EventKind.VENDOR_REPLIED,
            actor=ActorType.VENDOR,
            actor_label=route.vendor.vendor_id,
            summary=f"Received a source-traced reply from {route.vendor.name}.",
            refs=[artifact_id, stored.source_message_id, stored.rfq_outbox_id],
            payload={
                "vendor_id": route.vendor.vendor_id,
                "raw_ref": stored.raw_ref,
                "quote_extraction_pending": True,
            },
        )
        for _attempt in range(3):
            if self._store.artifact(VENDOR_EMAIL_ARTIFACT_KIND, artifact_id) is not None:
                return
            current = self._store.get_case(route.case.case_id)
            version = self._store.case_version(route.case.case_id)
            if current is None or version is None:
                raise RuntimeError("vendor reply case disappeared during persistence")
            updated = current.model_copy(deep=True)
            if updated.status in (CaseStatus.DETECTED, CaseStatus.PLANNING):
                updated.status = CaseStatus.VENDOR_CONTACTED
            updated.updated_at = max(updated.updated_at, stored.received_at)
            if route.vendor.vendor_id not in updated.contacted_vendor_ids:
                updated.contacted_vendor_ids.append(route.vendor.vendor_id)
            try:
                self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=f"vendor-email:v1:{artifact_id}",
                    timeline_events=(event,),
                    artifacts=(artifact,),
                )
                return
            except ConcurrencyConflict:
                continue
        raise ConcurrencyConflict("vendor email persistence exceeded concurrency retries")

    def _persist_quote(
        self,
        *,
        case_id: str,
        vendor_id: str,
        email_artifact_id: str,
        artifact_id: str,
        quote: Quote,
        extraction: QuoteExtraction,
    ) -> VendorReplyResult:
        artifact = WorkflowArtifact(
            artifact_id=artifact_id,
            case_id=case_id,
            kind=QUOTE_ARTIFACT_KIND,
            created_at=quote.received_at,
            source_ids=tuple(
                value for value in (email_artifact_id, quote.source_email_message_id) if value
            ),
            payload={
                "quote": quote.model_dump(mode="json"),
                "extraction": extraction.model_dump(mode="json"),
            },
        )
        event = TimelineEvent(
            event_id=_stable_id("event-quote", artifact_id),
            case_id=case_id,
            at=quote.received_at,
            kind=EventKind.QUOTE_RECORDED,
            actor=ActorType.AGENT,
            summary=(
                f"Recorded a source-backed quote of {quote.amount:.2f} "
                f"{quote.currency} from {vendor_id}."
            ),
            refs=[artifact_id, email_artifact_id, quote.source_email_message_id or quote.quote_id],
            payload={
                "quote_id": quote.quote_id,
                "vendor_id": vendor_id,
                "amount": format(quote.amount, ".2f"),
                "currency": quote.currency,
            },
        )
        for _attempt in range(3):
            existing = self._store.artifact(QUOTE_ARTIFACT_KIND, artifact_id)
            if existing is not None:
                return self._result_from_quote_artifact(
                    case_id=case_id,
                    vendor_id=vendor_id,
                    email_artifact_id=email_artifact_id,
                    artifact=existing,
                    duplicate=True,
                )
            current = self._store.get_case(case_id)
            version = self._store.case_version(case_id)
            if current is None or version is None:
                raise RuntimeError("vendor reply case disappeared during quote persistence")
            updated = current.model_copy(deep=True)
            if updated.status in (
                CaseStatus.DETECTED,
                CaseStatus.PLANNING,
                CaseStatus.VENDOR_CONTACTED,
            ):
                updated.status = CaseStatus.QUOTES_RECEIVED
            updated.updated_at = max(updated.updated_at, quote.received_at)
            try:
                self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=f"vendor-quote:v1:{artifact_id}",
                    timeline_events=(event,),
                    artifacts=(artifact,),
                    workflow_jobs=self._continuations(updated, artifact)
                    if self._continuations
                    else (),
                )
                return VendorReplyResult(
                    disposition=VendorReplyDisposition.QUOTE_RECORDED,
                    case=updated,
                    vendor_id=vendor_id,
                    email_artifact_id=email_artifact_id,
                    quote_artifact_id=artifact_id,
                    quote=quote,
                    extraction=extraction,
                )
            except ConcurrencyConflict:
                continue
        raise ConcurrencyConflict("quote persistence exceeded concurrency retries")

    def _persist_no_quote(
        self,
        *,
        case_id: str,
        vendor_id: str,
        email_artifact_id: str,
        artifact_id: str,
        received_at: datetime,
        reason: str = "Vendor reply contains no validated quote",
    ) -> VendorReplyResult:
        artifact = WorkflowArtifact(
            artifact_id=artifact_id,
            case_id=case_id,
            kind=NO_QUOTE_ARTIFACT_KIND,
            created_at=received_at,
            source_ids=(email_artifact_id,),
            payload={
                "schema_version": 1,
                "vendor_id": vendor_id,
                "result": "no_validated_quote",
                "reason": reason,
            },
        )
        event = TimelineEvent(
            event_id=_stable_id("event-no-quote", artifact_id),
            case_id=case_id,
            at=received_at,
            kind=EventKind.NOTE,
            actor=ActorType.AGENT,
            summary=f"The reply from {vendor_id} contained no validated quote.",
            refs=[artifact_id, email_artifact_id],
        )
        for _attempt in range(3):
            if self._store.artifact(NO_QUOTE_ARTIFACT_KIND, artifact_id) is not None:
                current = self._store.get_case(case_id)
                if current is None:
                    raise RuntimeError("no-quote artifact lost its case")
                return VendorReplyResult(
                    disposition=VendorReplyDisposition.DUPLICATE,
                    case=current,
                    vendor_id=vendor_id,
                    email_artifact_id=email_artifact_id,
                )
            current = self._store.get_case(case_id)
            version = self._store.case_version(case_id)
            if current is None or version is None:
                raise RuntimeError("vendor reply case disappeared during no-quote persistence")
            updated = current.model_copy(deep=True)
            updated.updated_at = max(updated.updated_at, received_at)
            try:
                self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=f"vendor-no-quote:v1:{artifact_id}",
                    timeline_events=(event,),
                    artifacts=(artifact,),
                    workflow_jobs=self._continuations(updated, artifact)
                    if self._continuations
                    else (),
                )
                return VendorReplyResult(
                    disposition=VendorReplyDisposition.NO_QUOTE_RECORDED,
                    case=updated,
                    vendor_id=vendor_id,
                    email_artifact_id=email_artifact_id,
                )
            except ConcurrencyConflict:
                continue
        raise ConcurrencyConflict("no-quote persistence exceeded concurrency retries")

    def _existing_result(
        self,
        *,
        case_id: str,
        vendor_id: str,
        email_artifact_id: str,
        quote_artifact_id: str,
        no_quote_artifact_id: str,
    ) -> VendorReplyResult | None:
        quote_artifact = self._store.artifact(QUOTE_ARTIFACT_KIND, quote_artifact_id)
        if quote_artifact is not None:
            return self._result_from_quote_artifact(
                case_id=case_id,
                vendor_id=vendor_id,
                email_artifact_id=email_artifact_id,
                artifact=quote_artifact,
                duplicate=True,
            )
        if self._store.artifact(NO_QUOTE_ARTIFACT_KIND, no_quote_artifact_id) is not None:
            current = self._store.get_case(case_id)
            if current is None:
                raise RuntimeError("no-quote artifact lost its case")
            return VendorReplyResult(
                disposition=VendorReplyDisposition.DUPLICATE,
                case=current,
                vendor_id=vendor_id,
                email_artifact_id=email_artifact_id,
            )
        return None

    def _result_from_quote_artifact(
        self,
        *,
        case_id: str,
        vendor_id: str,
        email_artifact_id: str,
        artifact: WorkflowArtifact,
        duplicate: bool,
    ) -> VendorReplyResult:
        current = self._store.get_case(case_id)
        if current is None:
            raise RuntimeError("quote artifact lost its case")
        quote = Quote.model_validate(artifact.payload["quote"])
        extraction = QuoteExtraction.model_validate(artifact.payload["extraction"])
        return VendorReplyResult(
            disposition=(
                VendorReplyDisposition.DUPLICATE
                if duplicate
                else VendorReplyDisposition.QUOTE_RECORDED
            ),
            case=current,
            vendor_id=vendor_id,
            email_artifact_id=email_artifact_id,
            quote_artifact_id=artifact.artifact_id,
            quote=quote,
            extraction=extraction,
        )


def _matches_delivered_thread(
    item: OutboxItem, local_message_id: str | None, thread_ids: set[str]
) -> bool:
    if local_message_id and local_message_id in thread_ids:
        return True
    provider_id = (item.provider_message_id or "").strip()
    if not provider_id:
        return False
    for thread_id in thread_ids:
        value = thread_id.strip().removeprefix("<").removesuffix(">")
        if value == provider_id.removeprefix("<").removesuffix(">"):
            return True
        local, separator, domain = value.rpartition("@")
        # SES replaces the supplied Message-ID. Its send receipt is the local
        # part of the delivered header; arbitrary domains must not be aliases.
        if separator and domain.lower() == "email.amazonses.com" and local == provider_id:
            return True
    return False


def _case_tokens(scheme: AddressScheme, inbound: InboundMessage) -> frozenset[str]:
    recipients = [*inbound.to_addresses, *inbound.cc_addresses]
    if inbound.delivered_to:
        recipients.append(inbound.delivered_to)
    return frozenset(
        token
        for recipient in recipients
        if (token := scheme.parse_case_token(recipient)) is not None
    )


def _address(value: str) -> str:
    return parseaddr(value or "")[1].strip().casefold()


def _stable_id(prefix: str, *parts: str) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"
