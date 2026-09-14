"""Offline tests for the safe Bedrock smoke replay command."""

from __future__ import annotations

import json

from steward.agents import TriageAction, TriageResult
from steward.demo.replay import (
    DEFAULT_MESSAGE_IDS,
    EXPECTED_ELEVATOR_MEMORY_IDS,
    INJECTION_MESSAGE_ID,
    main,
    run_seed_triage_replay,
)
from steward.domain.enums import Category, Urgency


class ReplayClassifier:
    def classify(self, *, message, assets, open_cases):
        del assets
        if message.message_id == DEFAULT_MESSAGE_IDS[0]:
            return TriageResult(
                action=TriageAction.OPEN_CASE,
                category=Category.ELEVATOR,
                urgency=Urgency.HIGH,
                confidence=0.96,
                title="A Block elevator shudders near the fourth floor",
                asset_id="elevator-a",
                rationale="A mechanical fault on the known A Block elevator.",
            )
        if message.message_id in DEFAULT_MESSAGE_IDS[1:3]:
            return TriageResult(
                action=TriageAction.LINK_EXISTING,
                category=Category.ELEVATOR,
                urgency=Urgency.NORMAL,
                confidence=0.92,
                asset_id="elevator-a",
                duplicate_case_id=open_cases[0].case_id,
                rationale="The message refers to the active elevator fault.",
            )
        if message.message_id in {DEFAULT_MESSAGE_IDS[3], INJECTION_MESSAGE_ID}:
            return TriageResult(
                action=TriageAction.IGNORE,
                category=Category.OTHER,
                urgency=Urgency.LOW,
                confidence=0.99,
                rationale="No operational resident problem is being reported.",
            )
        raise AssertionError(f"unexpected replay message: {message.message_id}")


class ChatterOpeningClassifier(ReplayClassifier):
    def classify(self, *, message, assets, open_cases):
        if message.message_id == DEFAULT_MESSAGE_IDS[3]:
            return TriageResult(
                action=TriageAction.OPEN_CASE,
                category=Category.OTHER,
                urgency=Urgency.LOW,
                confidence=0.8,
                title="Barbecue thanks",
                rationale="Deliberately incorrect classifier for failure reporting.",
            )
        return super().classify(message=message, assets=assets, open_cases=open_cases)


def test_replay_acceptance_sequence_and_injection_probe_pass_offline():
    report = run_seed_triage_replay(ReplayClassifier(), include_injection=True)

    assert report.passed
    assert report.errors == ()
    assert report.case_count == 1
    assert [step.disposition for step in report.steps] == [
        "opened",
        "linked",
        "linked",
        "ignored",
        "ignored",
    ]
    assert report.steps[0].case_id == "case-smoke-001"
    assert report.steps[0].related_case_ids == EXPECTED_ELEVATOR_MEMORY_IDS
    assert report.steps[0].memory_vendor_ids == (
        "meridian-lift",
        "pinnacle-vertical",
        "coastline-elevator",
    )
    assert report.steps[-1].message_id == INJECTION_MESSAGE_ID
    assert report.steps[-1].case_id is None


def test_replay_reports_semantic_failure_without_hiding_model_output():
    report = run_seed_triage_replay(ChatterOpeningClassifier())

    assert not report.passed
    assert report.case_count == 2
    assert any("acceptance dispositions differed" in error for error in report.errors)
    assert any("expected exactly one case" in error for error in report.errors)
    assert report.steps[3].rationale.startswith("Deliberately incorrect")


def test_cli_passes_explicit_bedrock_configuration_without_live_aws(capsys):
    factory_calls = []

    def factory(model_id, **kwargs):
        factory_calls.append((model_id, kwargs))
        return ReplayClassifier()

    exit_code = main(
        [
            "--model-id",
            "amazon.nova-pro-v1:0",
            "--profile",
            "offline-profile",
            "--region",
            "us-test-1",
            "--max-tokens",
            "333",
            "--include-injection",
            "--json",
        ],
        classifier_factory=factory,
    )

    assert exit_code == 0
    assert factory_calls == [
        (
            "amazon.nova-pro-v1:0",
            {
                "profile_name": "offline-profile",
                "region_name": "us-test-1",
                "temperature": 0.0,
                "max_tokens": 333,
            },
        )
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert payload["safe_mode"] == "no outbound mail transport constructed"
    assert payload["region"] == "us-test-1"
    assert "ignore all previous instructions" not in json.dumps(payload).lower()


def test_cli_returns_nonzero_for_semantic_failure(capsys):
    exit_code = main(
        ["--json"],
        classifier_factory=lambda *_args, **_kwargs: ChatterOpeningClassifier(),
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert payload["errors"]


def test_cli_returns_distinct_error_code_for_classifier_exception(capsys):
    class ExplodingClassifier:
        def classify(self, **_kwargs):
            raise RuntimeError("synthetic model outage")

    exit_code = main(
        ["--json"],
        classifier_factory=lambda *_args, **_kwargs: ExplodingClassifier(),
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert payload["error"] == "RuntimeError: synthetic model outage"
