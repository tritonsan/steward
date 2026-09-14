"""Acceptance tests for the English-only semantic robustness benchmark."""

from __future__ import annotations

from steward.agents import TriageAction, TriageResult
from steward.demo.semantic_robustness import PROBES, run_semantic_robustness_benchmark
from steward.domain.enums import Category, Urgency


class ExpectedSemanticClassifier:
    def classify(self, *, message, assets, open_cases):
        del assets
        probe = next(item for item in PROBES if message.message_id.endswith(item.probe_id))
        if probe.expected_disposition.value == "ignored":
            return TriageResult(
                action=TriageAction.IGNORE,
                category=Category.OTHER,
                urgency=Urgency.LOW,
                confidence=0.98,
                rationale="The message contains no operational fault.",
            )
        if probe.expected_disposition.value == "opened":
            return self._open(probe)

        target = next(
            case for case in open_cases if case.asset_id == probe.expected_asset_id
        )
        return TriageResult(
            action=TriageAction.LINK_EXISTING,
            category=Category.ELEVATOR,
            urgency=Urgency.HIGH,
            confidence=0.97,
            asset_id=probe.expected_asset_id,
            duplicate_case_id=target.case_id,
            rationale="The message corroborates the same physical asset and fault.",
        )

    @staticmethod
    def _open(probe):
        return TriageResult(
            action=TriageAction.OPEN_CASE,
            category=Category.ELEVATOR,
            urgency=Urgency.HIGH,
            confidence=0.96,
            title=f"Semantic benchmark fault on {probe.expected_asset_id}",
            asset_id=probe.expected_asset_id,
            rationale="The message describes a new fault on a known elevator asset.",
        )


class StructurallyValidSemanticMistake(ExpectedSemanticClassifier):
    """Make a wrong but guard-valid A/B decision to prove the benchmark detects it."""

    def classify(self, *, message, assets, open_cases):
        if message.message_id.endswith("semantic-003"):
            a_case = next(case for case in open_cases if case.asset_id == "elevator-a")
            return TriageResult(
                action=TriageAction.LINK_EXISTING,
                category=Category.ELEVATOR,
                urgency=Urgency.HIGH,
                confidence=0.95,
                asset_id="elevator-a",
                duplicate_case_id=a_case.case_id,
                rationale="Deliberately wrong semantic choice for harness testing.",
            )
        if message.message_id.endswith("semantic-004"):
            probe = next(item for item in PROBES if item.probe_id == "semantic-004")
            return self._open(probe)
        return super().classify(
            message=message,
            assets=assets,
            open_cases=open_cases,
        )


def test_scripted_semantic_benchmark_accepts_four_clusters_and_two_ignores(tmp_path):
    report = run_semantic_robustness_benchmark(
        ExpectedSemanticClassifier(),
        db_path=tmp_path / "semantic-pass.db",
    )

    assert report["passed"] is True
    assert report["errors"] == []
    assert report["benchmark"] == {
        "language": "English",
        "probe_count": 10,
        "expected_case_count": 4,
        "history_records_loaded": 63,
        "deterministic_alias_matching": False,
    }
    assert report["metrics"] == {
        "message_accuracy": 1.0,
        "messages_correct": 10,
        "routing_accuracy": 1.0,
        "routing_correct": 10,
        "asset_accuracy": 1.0,
        "assets_correct": 8,
        "cluster_accuracy": 1.0,
        "clusters_correct": 8,
        "ignore_accuracy": 1.0,
        "ignores_correct": 2,
        "total_reconciliation_attempts": 0,
    }
    assert set(report["cluster_case_ids"]) == {"a", "b", "c", "d"}
    assert {case["asset_id"] for case in report["cases"]} == {
        "elevator-a",
        "elevator-b",
        "elevator-c",
        "elevator-d",
    }
    assert all(len(case["source_message_ids"]) == 2 for case in report["cases"])
    assert report["safety"] == {
        "outbound_components_constructed": False,
        "network_send_calls": 0,
        "pending_outbox_count": 0,
        "commitment_event_count": 0,
        "commitment_sent": False,
    }
    assert all(step["passed"] is True for step in report["steps"])


def test_benchmark_rejects_semantically_wrong_but_structurally_valid_routing(tmp_path):
    report = run_semantic_robustness_benchmark(
        StructurallyValidSemanticMistake(),
        db_path=tmp_path / "semantic-fail.db",
    )

    assert report["passed"] is False
    by_id = {step["probe_id"]: step for step in report["steps"]}
    assert by_id["semantic-003"]["actual"]["disposition"] == "linked"
    assert by_id["semantic-003"]["actual"]["asset_id"] == "elevator-a"
    assert by_id["semantic-003"]["checks"] == {
        "routing": False,
        "asset": False,
        "cluster": False,
    }
    assert by_id["semantic-004"]["actual"]["disposition"] == "opened"
    assert any("semantic-003 failed" in error for error in report["errors"])
    assert any("semantic-004 failed" in error for error in report["errors"])
    assert report["safety"]["pending_outbox_count"] == 0
    assert report["safety"]["commitment_event_count"] == 0


def test_semantic_probe_corpus_is_english_only_and_avoids_exact_asset_labels():
    expected_senders = {
        "Daniel K.",
        "Priya S.",
        "Marcus R.",
        "Hannah W.",
        "Noah F.",
        "Elena V.",
        "Rafael G.",
        "Ingrid B.",
        "Kwame A.",
        "Mei-Ling C.",
    }
    exact_labels = {
        "a block elevator",
        "b block elevator",
        "c block elevator",
        "d block elevator",
    }

    assert {probe.sender for probe in PROBES} == expected_senders
    for probe in PROBES:
        corpus = " ".join((probe.sender, probe.text, probe.challenge))
        assert all(ord(character) < 128 for character in corpus if character.isalpha())
        if probe.expected_asset_id is not None:
            assert not any(label in probe.text.lower() for label in exact_labels)
