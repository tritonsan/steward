"""A failed model measurement must fail automation, even when execution succeeded."""

import json

import pytest

from steward import evaluation, memory_evaluation, quote_evaluation
from steward.agents import QuoteExtraction, QuoteRecommendation


def test_triage_cli_returns_nonzero_for_failed_measurement(monkeypatch, tmp_path):
    monkeypatch.setattr(
        evaluation,
        "evaluate",
        lambda *a, **k: {
            "sample_count": 200,
            "accuracy": 0.9,
            "passed": False,
        },
    )
    assert evaluation.main(["--output", str(tmp_path / "triage.json")]) == 1


def test_empty_triage_corpus_fails_before_model_creation(monkeypatch, tmp_path):
    corpus = tmp_path / "empty.jsonl"
    corpus.write_text("")
    monkeypatch.setattr(
        evaluation.StrandsTriageClassifier,
        "bedrock",
        lambda *a, **k: pytest.fail("empty corpus must not construct a model"),
    )
    with pytest.raises(ValueError, match="at least one probe"):
        evaluation.evaluate(corpus, tmp_path / "result.json")


def test_quote_cli_returns_nonzero_for_extraction_failures(monkeypatch, tmp_path):
    class EmptyExtractor:
        def extract(self, **kwargs):
            return QuoteExtraction(has_quote=False, no_quote_reason="No total price extracted")

    monkeypatch.setattr(
        quote_evaluation.StrandsQuoteExtractor, "bedrock", lambda *a, **k: EmptyExtractor()
    )
    output = tmp_path / "new" / "quotes.json"
    assert quote_evaluation.main(["--output", str(output)]) == 1
    report = json.loads(output.read_text())
    assert report["quote_count"] == 90 and report["passing_quotes"] == 0


def test_memory_cli_fails_when_choices_ignore_recurrence(monkeypatch, tmp_path):
    class MemorylessRecommender:
        def recommend(self, **kwargs):
            return QuoteRecommendation(
                recommended_quote_id="quote-a",
                rationale="Always select cheapest",
                source_ids=("quote-a",),
            )

    monkeypatch.setattr(
        memory_evaluation.StrandsQuoteRecommender,
        "bedrock",
        lambda *a, **k: MemorylessRecommender(),
    )
    output = tmp_path / "new" / "memory.json"
    assert memory_evaluation.main(["--output", str(output)]) == 1
    report = json.loads(output.read_text())
    assert len(report["results"]) == 9 and report["passed"] is False
