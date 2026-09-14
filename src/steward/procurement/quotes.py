"""Deterministic validation and recording of extracted vendor quotes."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from email.utils import parseaddr
from uuid import uuid4

from steward.agents.quote import QuoteExtraction, QuoteExtractor
from steward.domain.models import Case, Quote, UtcDatetime, Vendor
from steward.mail import AddressScheme, InboundMessage

__all__ = [
    "NoQuoteFound",
    "QuoteEnvelopeError",
    "QuoteEvidenceError",
    "QuoteIngestion",
    "QuoteIngestor",
]


class QuoteEnvelopeError(ValueError):
    """The reply sender or case route does not match deterministic records."""


class QuoteEvidenceError(ValueError):
    """Extracted values are not supported by the supplied reply body."""


class NoQuoteFound(ValueError):
    """The validated model output says the reply contains no quote."""


@dataclass(frozen=True, slots=True)
class QuoteIngestion:
    quote: Quote
    extraction: QuoteExtraction


IdFactory = Callable[[str], str]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


class QuoteIngestor:
    """Route first, extract second, then validate every material field."""

    __slots__ = ("_extractor", "_id_factory", "_scheme")

    def __init__(
        self,
        *,
        extractor: QuoteExtractor,
        address_scheme: AddressScheme,
        id_factory: IdFactory = _new_id,
    ) -> None:
        self._extractor = extractor
        self._scheme = address_scheme
        self._id_factory = id_factory

    def ingest(
        self,
        *,
        case: Case,
        vendor: Vendor,
        inbound: InboundMessage,
        rfq_sent_at: UtcDatetime,
    ) -> QuoteIngestion:
        if rfq_sent_at.tzinfo is None or inbound.received_at.tzinfo is None:
            raise QuoteEnvelopeError("RFQ and reply timestamps must be timezone-aware")
        if inbound.received_at < rfq_sent_at:
            raise QuoteEnvelopeError("quote reply predates its RFQ")
        self._validate_envelope(case=case, vendor=vendor, inbound=inbound)
        if inbound.message_id is None:
            raise QuoteEnvelopeError("quote reply requires a source Message-ID")
        if not inbound.body_text.strip():
            raise NoQuoteFound("vendor reply body is empty")

        extraction = self._extractor.extract(
            body_text=inbound.body_text,
            requested_currency=case.currency,
            rfq_sent_at=rfq_sent_at,
            received_at=inbound.received_at,
            source_message_id=inbound.message_id,
        )
        if not extraction.has_quote:
            raise NoQuoteFound(extraction.no_quote_reason or "reply contains no quote")
        self._validate_evidence(
            extraction=extraction,
            body_text=inbound.body_text,
            requested_currency=case.currency,
            received_at=inbound.received_at,
        )

        assert extraction.amount is not None  # noqa: S101 - guaranteed by model validation
        assert extraction.currency is not None  # noqa: S101 - guaranteed by model validation
        quote = Quote(
            quote_id=self._id_factory("quote"),
            case_id=case.case_id,
            vendor_id=vendor.vendor_id,
            amount=extraction.amount,
            currency=extraction.currency,
            scope=extraction.scope_evidence,
            earliest_onsite_at=extraction.earliest_onsite_at,
            valid_until=extraction.valid_until,
            received_at=inbound.received_at,
            source_email_message_id=inbound.message_id,
        )
        return QuoteIngestion(quote=quote, extraction=extraction)

    def _validate_envelope(
        self,
        *,
        case: Case,
        vendor: Vendor,
        inbound: InboundMessage,
    ) -> None:
        _, sender = parseaddr(inbound.from_address)
        if sender.casefold() != vendor.email.casefold():
            raise QuoteEnvelopeError(
                f"reply sender does not match vendor record for {vendor.vendor_id}"
            )
        if case.category not in vendor.categories:
            raise QuoteEnvelopeError("reply vendor does not serve the case category")

        recipients = [*inbound.to_addresses, *inbound.cc_addresses]
        if inbound.delivered_to:
            recipients.append(inbound.delivered_to)
        tokens = {
            token
            for address in recipients
            if (token := self._scheme.parse_case_token(address)) is not None
        }
        if case.reply_token not in tokens:
            raise QuoteEnvelopeError("reply was not routed through this case's reply token")

    @staticmethod
    def _validate_evidence(
        *,
        extraction: QuoteExtraction,
        body_text: str,
        requested_currency: str,
        received_at,
    ) -> None:
        assert extraction.amount is not None  # noqa: S101
        assert extraction.currency is not None  # noqa: S101
        _require_excerpt(body_text, extraction.amount_evidence, "amount")
        _require_excerpt(body_text, extraction.scope_evidence, "scope")

        for field in ("inclusions_evidence", "exclusions_evidence"):
            excerpt = getattr(extraction, field)
            if excerpt:
                _require_excerpt(body_text, excerpt, field)
        amount = extraction.amount
        evidence_amounts = _decimal_candidates(extraction.amount_evidence)
        if amount not in evidence_amounts:
            raise QuoteEvidenceError(
                f"amount {amount} is not present in the verbatim amount evidence"
            )
        if extraction.currency != requested_currency:
            raise QuoteEvidenceError(
                f"quote currency {extraction.currency} does not match requested "
                f"currency {requested_currency}"
            )

        # Check explicit ISO currency beside the price independently of model inference.
        # Reject conflicting evidence; never repair a monetary quote by converting it.
        currency_codes = {
            "USD",
            "SGD",
            "EUR",
            "GBP",
            "AUD",
            "CAD",
            "NZD",
            "JPY",
            "CHF",
            "CNY",
            "HKD",
            "INR",
            "TRY",
            "MYR",
            "THB",
            "IDR",
            "PHP",
            "VND",
            "KRW",
            "AED",
            "SAR",
            "ZAR",
            "BRL",
            "MXN",
            "SEK",
            "NOK",
            "DKK",
            "PLN",
            "CZK",
            requested_currency,
        }
        stated = set(re.findall(r"\b[A-Z]{3}\b", body_text.upper())) & currency_codes
        for marker, code in (("€", "EUR"), ("£", "GBP"), ("US$", "USD"), ("S$", "SGD")):
            if marker in body_text:
                stated.add(code)
        if stated and (stated != {extraction.currency} or extraction.currency_inferred_from_rfq):
            raise QuoteEvidenceError("explicit price currency conflicts with extracted currency")

        explicit_currency = bool(
            re.search(rf"\b{re.escape(requested_currency)}\b", body_text, re.I)
        )
        if not explicit_currency and not extraction.currency_inferred_from_rfq:
            raise QuoteEvidenceError(
                "reply omits currency but extraction did not mark RFQ currency inference"
            )

        if extraction.earliest_onsite_at is not None:
            _require_excerpt(body_text, extraction.onsite_evidence, "onsite")
            if extraction.earliest_onsite_at < received_at:
                raise QuoteEvidenceError("earliest onsite time precedes receipt of the quote")
        if extraction.valid_until is not None:
            _require_excerpt(body_text, extraction.validity_evidence, "validity")
            if extraction.valid_until < received_at:
                raise QuoteEvidenceError("quote validity ends before the quote was received")


def _require_excerpt(body: str, excerpt: str, field_name: str) -> None:
    if not excerpt or excerpt.casefold() not in body.casefold():
        raise QuoteEvidenceError(f"{field_name} evidence is not a verbatim body excerpt")


_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?")


def _decimal_candidates(text: str) -> set[Decimal]:
    values: set[Decimal] = set()
    for match in _NUMBER_RE.findall(text):
        try:
            values.add(Decimal(match.replace(",", "")))
        except InvalidOperation:
            continue
    return values
