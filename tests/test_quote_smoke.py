"""Offline tests for the live quote-to-decision smoke command."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from steward.agents import QuoteExtraction
from steward.demo.quote_decision import main, run_quote_decision_smoke


class ScriptedExtractor:
    def __init__(self) -> None:
        self.calls = []

    def extract(self, **kwargs):
        self.calls.append(kwargs)
        body = kwargs["body_text"]
        received_at = kwargs["received_at"]
        if "Quote is 705" in body:
            return QuoteExtraction(
                has_quote=True,
                amount=Decimal("705.00"),
                currency="USD",
                currency_inferred_from_rfq=True,
                amount_evidence="Quote is 705",
                scope_evidence=(
                    "Quote is 705 including labour, travel and shoes, and rail alignment "
                    "correction if we find it."
                ),
                earliest_onsite_at=received_at + timedelta(hours=15.5),
                onsite_evidence="tomorrow afternoon",
            )
        if "guide shoes for 540" in body:
            return QuoteExtraction(
                has_quote=True,
                amount=Decimal("540.00"),
                currency="USD",
                currency_inferred_from_rfq=True,
                amount_evidence="540 all in",
                scope_evidence="We can replace the guide shoes for 540 all in.",
                earliest_onsite_at=received_at + timedelta(days=3),
                onsite_evidence="Monday",
            )
        raise AssertionError("unexpected vendor reply")


def test_offline_smoke_records_rfqs_extracts_quotes_and_authorizes_without_sending():
    extractor = ScriptedExtractor()

    report = run_quote_decision_smoke(extractor)

    assert report.passed
    assert report.errors == ()
    assert report.rfq_vendor_ids == (
        "meridian-lift",
        "coastline-elevator",
        "pinnacle-vertical",
    )
    assert report.rfq_provider_message_ids == (
        "recorded-0001",
        "recorded-0002",
        "recorded-0003",
    )
    assert report.rfq_audit_count == 3
    assert {item.vendor_id: item.amount for item in report.quotes} == {
        "meridian-lift": "705.00",
        "coastline-elevator": "540.00",
    }
    assert report.nonresponding_vendor_ids == ("pinnacle-vertical",)
    assert report.recommended_vendor_id == "meridian-lift"
    assert report.recommended_amount == "705.00"
    assert report.price_premium == "165.00"
    assert report.commitment_rule_id == "COMMIT.WITHIN_POLICY"
    assert report.commitment_disposition == "authorized_not_sent"
    assert report.commitment_sent is False
    assert len(extractor.calls) == 2


def test_cli_forwards_bedrock_configuration_and_emits_safe_json(capsys):
    factory_calls = []
    extractor = ScriptedExtractor()

    def factory(model_id, **kwargs):
        factory_calls.append((model_id, kwargs))
        return extractor

    exit_code = main(
        [
            "--model-id",
            "amazon.nova-pro-v1:0",
            "--profile",
            "offline",
            "--region",
            "us-test-1",
            "--max-tokens",
            "555",
            "--json",
        ],
        extractor_factory=factory,
    )

    assert exit_code == 0
    assert factory_calls == [
        (
            "amazon.nova-pro-v1:0",
            {
                "profile_name": "offline",
                "region_name": "us-test-1",
                "temperature": 0.0,
                "max_tokens": 555,
            },
        )
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert payload["safe_mode"] == {
        "rfq_transport": "RecordingMailTransport",
        "commitment_sent": False,
    }
    assert payload["decision"]["recommended_vendor_id"] == "meridian-lift"
    assert payload["decision"]["commitment_disposition"] == "authorized_not_sent"


def test_cli_model_failure_is_distinct_and_still_reports_safe_mode(capsys):
    class BrokenExtractor:
        def extract(self, **_kwargs):
            raise RuntimeError("synthetic Bedrock outage")

    exit_code = main(
        ["--json"],
        extractor_factory=lambda *_args, **_kwargs: BrokenExtractor(),
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert payload["safe_mode"]["rfq_transport"] == "RecordingMailTransport"
    assert payload["safe_mode"]["commitment_sent"] is False
    assert payload["error"] == "RuntimeError: synthetic Bedrock outage"
