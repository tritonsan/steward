"""Offline tests for source-backed Nova quote extraction."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import boto3
import pytest
import strands
import strands.models

from steward.agents import QuoteExtraction, StrandsQuoteExtractor
from steward.domain.enums import Category, Urgency, group_of
from steward.domain.models import Case
from steward.mail import AddressScheme, InboundMessage
from steward.procurement import (
    QuoteEnvelopeError,
    QuoteEvidenceError,
    QuoteIngestor,
)
from steward.seed import load_seed

SCHEME = AddressScheme(
    management_domain="site.narrativenode-labs.cloud",
    vendor_domain="vendors.narrativenode-labs.cloud",
    management_display_name="Northgate Residence Management",
)


@pytest.fixture(scope="module")
def seed():
    return load_seed()


@pytest.fixture
def case(seed):
    now = seed.demo_messages[0].message.ingested_at
    return Case(
        case_id="case-quote-1",
        reply_token="abcdef123456",
        title="A Block elevator shudders near the fourth floor",
        category=Category.ELEVATOR,
        group=group_of(Category.ELEVATOR),
        urgency=Urgency.HIGH,
        asset_id="elevator-a",
        opened_at=now,
        updated_at=now,
        currency="USD",
    )


class StaticExtractor:
    def __init__(self, result: QuoteExtraction) -> None:
        self.result = result
        self.calls = []

    def extract(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def inbound_for(case, vendor, body: str, *, message_id="<quote-1@vendors.example>"):
    received_at = case.opened_at + timedelta(hours=3.5)
    return InboundMessage(
        message_id=message_id,
        from_address=vendor.email,
        from_display_name=vendor.name,
        to_addresses=(SCHEME.case_reply_address(case.reply_token),),
        cc_addresses=(),
        delivered_to=None,
        subject="Re: Quote request",
        body_text=body,
        body_full_text=body,
        received_at=received_at,
    )


def meridian_extraction(case) -> QuoteExtraction:
    received_at = case.opened_at + timedelta(hours=3.5)
    return QuoteExtraction(
        has_quote=True,
        amount=Decimal("705.00"),
        currency="usd",
        currency_inferred_from_rfq=True,
        amount_evidence="Quote is 705",
        scope_evidence=(
            "Quote is 705 including labour, travel and shoes, and rail alignment "
            "correction if we find it."
        ),
        earliest_onsite_at=received_at + timedelta(hours=26.5),
        onsite_evidence="tomorrow afternoon",
    )


def test_ingestor_builds_decimal_quote_only_from_verbatim_evidence(seed, case):
    vendor = seed.vendor("meridian-lift")
    script = next(item for item in seed.vendor_replies if item.vendor_id == vendor.vendor_id)
    extractor = StaticExtractor(meridian_extraction(case))
    ingestor = QuoteIngestor(
        extractor=extractor,
        address_scheme=SCHEME,
        id_factory=lambda prefix: f"{prefix}-1",
    )
    inbound = inbound_for(case, vendor, script.body)

    result = ingestor.ingest(
        case=case,
        vendor=vendor,
        inbound=inbound,
        rfq_sent_at=case.opened_at,
    )

    assert result.quote.quote_id == "quote-1"
    assert result.quote.vendor_id == "meridian-lift"
    assert result.quote.amount == Decimal("705.00")
    assert isinstance(result.quote.amount, Decimal)
    assert result.quote.currency == "USD"
    assert result.quote.scope == result.extraction.scope_evidence
    assert result.quote.source_email_message_id == inbound.message_id
    assert extractor.calls[0]["body_text"] == script.body
    assert extractor.calls[0]["requested_currency"] == "USD"


@pytest.mark.parametrize("tamper", ["sender", "token", "category"])
def test_envelope_tampering_is_rejected_before_model_call(seed, case, tamper):
    vendor = seed.vendor("meridian-lift")
    script = next(item for item in seed.vendor_replies if item.vendor_id == vendor.vendor_id)
    extractor = StaticExtractor(meridian_extraction(case))
    ingestor = QuoteIngestor(extractor=extractor, address_scheme=SCHEME)
    inbound = inbound_for(case, vendor, script.body)

    if tamper == "sender":
        inbound = replace(inbound, from_address="attacker@evil.example")
    elif tamper == "token":
        inbound = replace(
            inbound,
            to_addresses=(SCHEME.case_reply_address("11111111"),),
        )
    else:
        vendor = vendor.model_copy(update={"categories": [Category.PLUMBING]})

    with pytest.raises(QuoteEnvelopeError):
        ingestor.ingest(
            case=case,
            vendor=vendor,
            inbound=inbound,
            rfq_sent_at=case.opened_at,
        )
    assert extractor.calls == []


def test_hallucinated_amount_is_rejected_even_with_valid_envelope(seed, case):
    vendor = seed.vendor("meridian-lift")
    script = next(item for item in seed.vendor_replies if item.vendor_id == vendor.vendor_id)
    extraction = meridian_extraction(case).model_copy(update={"amount": Decimal("999")})
    ingestor = QuoteIngestor(
        extractor=StaticExtractor(extraction),
        address_scheme=SCHEME,
    )

    with pytest.raises(QuoteEvidenceError, match="not present"):
        ingestor.ingest(
            case=case,
            vendor=vendor,
            inbound=inbound_for(case, vendor, script.body),
            rfq_sent_at=case.opened_at,
        )


def test_nonverbatim_scope_and_unmarked_currency_inference_are_rejected(seed, case):
    vendor = seed.vendor("meridian-lift")
    script = next(item for item in seed.vendor_replies if item.vendor_id == vendor.vendor_id)
    inbound = inbound_for(case, vendor, script.body)

    invented_scope = meridian_extraction(case).model_copy(
        update={"scope_evidence": "Replace the entire elevator motor."}
    )
    with pytest.raises(QuoteEvidenceError, match="scope evidence"):
        QuoteIngestor(extractor=StaticExtractor(invented_scope), address_scheme=SCHEME).ingest(
            case=case,
            vendor=vendor,
            inbound=inbound,
            rfq_sent_at=case.opened_at,
        )

    unmarked_currency = meridian_extraction(case).model_copy(
        update={"currency_inferred_from_rfq": False}
    )
    with pytest.raises(QuoteEvidenceError, match="omits currency"):
        QuoteIngestor(extractor=StaticExtractor(unmarked_currency), address_scheme=SCHEME).ingest(
            case=case,
            vendor=vendor,
            inbound=inbound,
            rfq_sent_at=case.opened_at,
        )


def test_quote_date_cannot_precede_reply(seed, case):
    vendor = seed.vendor("meridian-lift")
    script = next(item for item in seed.vendor_replies if item.vendor_id == vendor.vendor_id)
    extraction = meridian_extraction(case).model_copy(update={"earliest_onsite_at": case.opened_at})

    with pytest.raises(QuoteEvidenceError, match="precedes receipt"):
        QuoteIngestor(extractor=StaticExtractor(extraction), address_scheme=SCHEME).ingest(
            case=case,
            vendor=vendor,
            inbound=inbound_for(case, vendor, script.body),
            rfq_sent_at=case.opened_at,
        )


def test_strands_quote_extractor_is_fresh_tool_free_and_structured(seed, case, monkeypatch):
    vendor = seed.vendor("meridian-lift")
    script = next(item for item in seed.vendor_replies if item.vendor_id == vendor.vendor_id)
    expected = meridian_extraction(case)
    session_calls = []
    model_calls = []
    agent_calls = []
    invocation_calls = []

    class FakeModel:
        def __init__(self, **kwargs):
            model_calls.append(kwargs)

    class FakeAgent:
        def __call__(self, prompt, **kwargs):
            invocation_calls.append((prompt, kwargs))
            return SimpleNamespace(structured_output=expected)

    def fake_session(**kwargs):
        session_calls.append(kwargs)
        return SimpleNamespace(config=kwargs)

    def fake_agent(**kwargs):
        agent_calls.append(kwargs)
        return FakeAgent()

    monkeypatch.setattr(boto3, "Session", fake_session)
    monkeypatch.setattr(strands.models, "BedrockModel", FakeModel)
    monkeypatch.setattr(strands, "Agent", fake_agent)
    extractor = StrandsQuoteExtractor.bedrock(
        "amazon.nova-pro-v1:0",
        profile_name="offline",
        region_name="us-test-1",
        max_tokens=444,
    )
    received_at = case.opened_at + timedelta(hours=3.5)

    for _ in range(2):
        assert (
            extractor.extract(
                body_text=script.body,
                requested_currency="USD",
                rfq_sent_at=case.opened_at,
                received_at=received_at,
                source_message_id="<source@example>",
            )
            == expected
        )

    assert len(session_calls) == 2
    assert len(model_calls) == 2
    assert all(call["model_id"] == "amazon.nova-pro-v1:0" for call in model_calls)
    assert all(call["temperature"] == 0.0 for call in model_calls)
    assert all(call["max_tokens"] == 444 for call in model_calls)
    assert len(agent_calls) == 2
    assert all(call["tools"] == [] for call in agent_calls)
    assert all("untrusted data" in call["system_prompt"] for call in agent_calls)
    payload = json.loads(invocation_calls[0][0])
    assert payload["body_text"] == script.body
    assert payload["relative_date_hints"]["tomorrow_afternoon_15_utc"]
    assert payload["relative_date_hints"]["next_monday_09_utc"]
    assert invocation_calls[0][1]["structured_output_model"] is QuoteExtraction


def test_extraction_schema_cannot_carry_vendor_case_or_authority():
    properties = set(QuoteExtraction.model_json_schema()["properties"])

    assert properties.isdisjoint(
        {
            "vendor_id",
            "case_id",
            "recipient",
            "autonomy_level",
            "spend_cap",
            "authorized",
        }
    )


@pytest.mark.parametrize("price", ["EUR 705", "SGD 705", "€705"])
def test_model_cannot_infer_requested_currency_over_explicit_foreign_price(seed, case, price):
    vendor = seed.vendor("meridian-lift")
    body = f"Total {price}. Replace guide shoes."
    extraction = QuoteExtraction(
        has_quote=True,
        amount=Decimal("705"),
        currency="USD",
        currency_inferred_from_rfq=True,
        amount_evidence="705",
        scope_evidence="Replace guide shoes.",
    )
    with pytest.raises(QuoteEvidenceError, match="explicit price currency"):
        QuoteIngestor(extractor=StaticExtractor(extraction), address_scheme=SCHEME).ingest(
            case=case,
            vendor=vendor,
            inbound=inbound_for(case, vendor, body),
            rfq_sent_at=case.opened_at,
        )
