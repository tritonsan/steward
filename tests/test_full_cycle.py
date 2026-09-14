from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from steward.agents import QuoteExtraction, TriageAction, TriageResult
from steward.demo.full_cycle import main, run_full_cycle_demo
from steward.domain.enums import Category, Urgency


class ScriptedTriageClassifier:
    def __init__(self) -> None:
        self.calls = []

    def classify(self, *, message, assets, open_cases):
        self.calls.append((message.message_id, len(assets), len(open_cases)))
        if message.message_id.endswith("88401"):
            return TriageResult(
                action=TriageAction.OPEN_CASE,
                category=Category.ELEVATOR,
                urgency=Urgency.HIGH,
                confidence=0.95,
                title="A Block elevator shudders and grinds near the fourth floor",
                asset_id="elevator-a",
                rationale="Recurring shuddering and grinding on ascent near floor four.",
            )
        if message.message_id.endswith(("88402", "88403")):
            return TriageResult(
                action=TriageAction.LINK_EXISTING,
                category=Category.ELEVATOR,
                urgency=Urgency.HIGH,
                confidence=0.94,
                asset_id="elevator-a",
                duplicate_case_id=open_cases[0].case_id,
                rationale="Same elevator, same location, and same open fault.",
            )
        return TriageResult(
            action=TriageAction.IGNORE,
            category=Category.OTHER,
            urgency=Urgency.LOW,
            confidence=0.99,
            rationale="No new actionable property problem with trusted authority.",
        )


class ScriptedQuoteExtractor:
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
                    "check the bracket alignment over the full fourth to fifth floor run "
                    "rather than just replace the shoes again"
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
                scope_evidence="replace the guide shoes",
                earliest_onsite_at=received_at + timedelta(days=3),
                onsite_evidence="Monday",
            )
        raise AssertionError("unexpected quote body")


def test_full_cycle_closes_the_case_and_teaches_fresh_memory_without_real_sends():
    triage = ScriptedTriageClassifier()
    quotes = ScriptedQuoteExtractor()

    report = run_full_cycle_demo(triage, quotes, include_injection=True)

    assert report.passed, report.errors
    assert [step.disposition for step in report.intake_steps] == [
        "opened",
        "linked",
        "linked",
        "ignored",
        "ignored",
    ]
    assert report.historical_case_ids[:2] == ("hist-2025-017", "hist-2026-001")
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
    assert {quote.vendor_id: quote.amount for quote in report.quotes} == {
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
    assert report.follow_up_count == 2
    assert report.follow_up_provider_message_ids == ("recorded-0001", "recorded-0002")
    assert report.follow_up_audit_count == 2
    assert report.escalated_at is not None
    assert report.final_status == "closed"
    assert report.resolution_cost == "705.00"
    assert report.resolution_source_ids == ("demo-completion-report-001",)
    assert report.memory_record_count_after == report.memory_record_count_before + 1
    assert report.case_id in report.memory_recall_case_ids
    assert report.meridian_jobs_after == report.meridian_jobs_before + 1
    assert len(triage.calls) == 5
    assert len(quotes.calls) == 2

    kinds = [beat.kind for beat in report.timeline]
    assert kinds[0] == "case_opened"
    assert "memory_consulted" in kinds
    assert "rfq_sent" in kinds
    assert kinds.count("quote_recorded") == 2
    assert "commitment_authorized" in kinds
    assert kinds.count("follow_up_sent") == 2
    assert "escalated" in kinds
    assert "completion_claimed" in kinds
    assert "verification_requested" in kinds
    assert "verification_confirmed" in kinds
    assert "resolved" in kinds
    assert "memory_written" in kinds
    assert "closed" in kinds
    assert kinds[-1] == "memory_consulted"


def test_full_demo_cli_forwards_bedrock_settings_and_emits_safe_json(capsys):
    triage_calls = []
    quote_calls = []

    def triage_factory(model_id, **kwargs):
        triage_calls.append((model_id, kwargs))
        return ScriptedTriageClassifier()

    def quote_factory(model_id, **kwargs):
        quote_calls.append((model_id, kwargs))
        return ScriptedQuoteExtractor()

    exit_code = main(
        [
            "--model-id",
            "amazon.nova-pro-v1:0",
            "--profile",
            "offline",
            "--region",
            "us-test-1",
            "--triage-max-tokens",
            "444",
            "--quote-max-tokens",
            "777",
            "--json",
        ],
        triage_factory=triage_factory,
        quote_factory=quote_factory,
    )

    assert exit_code == 0
    assert triage_calls == [
        (
            "amazon.nova-pro-v1:0",
            {
                "profile_name": "offline",
                "region_name": "us-test-1",
                "temperature": 0.0,
                "max_tokens": 444,
            },
        )
    ]
    assert quote_calls == [
        (
            "amazon.nova-pro-v1:0",
            {
                "profile_name": "offline",
                "region_name": "us-test-1",
                "temperature": 0.0,
                "max_tokens": 777,
            },
        )
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert payload["safe_mode"] == {
        "execution_mode": "dry_run",
        "rfq_transport": "RecordingMailTransport",
        "follow_up_transport": "RecordingMailTransport",
        "commitment_sent": False,
        "synthetic_data": True,
    }
    assert payload["procurement"]["recommended_vendor_id"] == "meridian-lift"
    assert payload["follow_up"]["count"] == 2
    assert payload["follow_up"]["delivered"] is False
    assert payload["case"]["final_status"] == "closed"
    assert payload["memory"]["meridian_jobs_after"] == 7


def test_full_demo_model_failure_returns_safe_distinct_error(capsys):
    class BrokenTriage:
        def classify(self, **_kwargs):
            raise RuntimeError("synthetic Bedrock outage")

    exit_code = main(
        ["--json"],
        triage_factory=lambda *_args, **_kwargs: BrokenTriage(),
        quote_factory=lambda *_args, **_kwargs: ScriptedQuoteExtractor(),
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert payload["safe_mode"]["execution_mode"] == "dry_run"
    assert payload["safe_mode"]["commitment_sent"] is False
    assert payload["safe_mode"]["synthetic_data"] is True
    assert payload["error"] == "RuntimeError: synthetic Bedrock outage"
