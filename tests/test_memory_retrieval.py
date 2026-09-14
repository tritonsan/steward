"""Institutional-memory retrieval and intake integration tests."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

import pytest

from steward.agents import IntakeDisposition, IntakeService, TriageAction, TriageResult
from steward.domain.clock import FrozenClock
from steward.domain.enums import Category, EventKind, Urgency
from steward.memory import (
    MemoryRelation,
    StructuredMemoryRetriever,
)
from steward.policy import PolicyEngine, Rule
from steward.seed import load_seed
from steward.store import InMemoryCaseStore

EXPECTED_ELEVATOR_HISTORY = (
    "hist-2025-017",
    "hist-2026-001",
    "hist-2025-002",
    "hist-2025-008",
    "hist-2026-007",
)


@pytest.fixture(scope="module")
def seed():
    return load_seed()


@pytest.fixture(scope="module")
def retriever(seed):
    return StructuredMemoryRetriever(
        seed.history,
        recurrence_window_days=seed.settings.recurrence_window_days,
    )


def elevator_query(seed) -> str:
    message = seed.demo_messages[0].message
    return "\n".join(
        (
            message.text,
            "A Block elevator shudders near the fourth floor",
            "Recurring shuddering and grinding on ascent near floor four.",
        )
    )


def test_elevator_recall_prioritizes_same_fault_then_wider_asset_history(seed, retriever):
    recall = retriever.recall(
        category=Category.ELEVATOR,
        asset_id="elevator-a",
        query_text=elevator_query(seed),
        as_of=seed.demo_messages[0].message.ingested_at,
    )

    assert recall.related_case_ids == EXPECTED_ELEVATOR_HISTORY
    assert [hit.relation for hit in recall.case_hits] == [
        MemoryRelation.SAME_FAULT,
        MemoryRelation.SAME_FAULT,
        MemoryRelation.ASSET_HISTORY,
        MemoryRelation.ASSET_HISTORY,
        MemoryRelation.ASSET_HISTORY,
    ]
    root, recurrence = recall.case_hits[:2]
    assert root.relevance_score > 0.7
    assert {"shudder", "grind", "fourth", "up", "down"} <= set(root.matched_terms)
    assert "raised_as" in root.matched_fields
    assert recurrence.record.recurrence_of_case_id == root.case_id
    assert "recurrence_link" in recurrence.matched_fields


def test_vendor_scorecard_evidence_traces_every_metric_source(seed, retriever):
    recall = retriever.recall(
        category=Category.ELEVATOR,
        asset_id="elevator-a",
        query_text=elevator_query(seed),
        as_of=seed.demo_messages[0].message.ingested_at,
    )

    assert [item.scorecard.vendor_id for item in recall.vendor_scorecards] == [
        "meridian-lift",
        "pinnacle-vertical",
        "coastline-elevator",
    ]
    for evidence in recall.vendor_scorecards:
        expected_union = set(evidence.job_case_ids)
        expected_union.update(evidence.response_case_ids)
        expected_union.update(evidence.engagement_case_ids)
        expected_union.update(evidence.recurrence_case_ids)
        assert set(evidence.source_case_ids) == expected_union
        assert set(evidence.scorecard.source_case_ids) == set(evidence.job_case_ids)
        assert evidence.scorecard.computed_at == recall.as_of

    coastline = next(
        item
        for item in recall.vendor_scorecards
        if item.scorecard.vendor_id == "coastline-elevator"
    )
    assert "hist-2025-017" in coastline.job_case_ids
    assert "hist-2026-001" in coastline.recurrence_case_ids
    assert "hist-2025-017" in coastline.response_case_ids
    assert set(EXPECTED_ELEVATOR_HISTORY) < set(recall.source_case_ids)


def test_as_of_filter_excludes_future_cases_and_future_recurrence_links(seed):
    as_of = datetime(2025, 12, 1, 0, 0, tzinfo=timezone.utc)
    recall = StructuredMemoryRetriever(seed.history).recall(
        category=Category.ELEVATOR,
        asset_id="elevator-a",
        query_text=elevator_query(seed),
        as_of=as_of,
    )

    assert "hist-2025-017" in recall.related_case_ids
    assert "hist-2026-001" not in recall.source_case_ids
    root = next(hit for hit in recall.case_hits if hit.case_id == "hist-2025-017")
    assert root.record.recurred_as_case_id is None

    by_id = {record.case_id: record for record in seed.history}
    for source_case_id in recall.source_case_ids:
        assert by_id[source_case_id].closed_at <= as_of


def test_category_only_recall_is_recent_bounded_and_not_marked_same_fault(seed):
    recall = StructuredMemoryRetriever(seed.history, max_case_hits=3).recall(
        category=Category.LANDSCAPING,
        asset_id=None,
        query_text="",
        as_of=seed.demo_messages[0].message.ingested_at,
    )

    assert len(recall.case_hits) == 3
    assert all(hit.relation is MemoryRelation.CATEGORY_HISTORY for hit in recall.case_hits)
    opened = [hit.record.opened_at for hit in recall.case_hits]
    assert opened == sorted(opened, reverse=True)


class IdSequence:
    def __init__(self) -> None:
        self.counts: defaultdict[str, int] = defaultdict(int)

    def __call__(self, prefix: str) -> str:
        self.counts[prefix] += 1
        return f"{prefix}-memory-{self.counts[prefix]}"


class ElevatorOpeningClassifier:
    def classify(self, *, message, assets, open_cases):
        del message, assets, open_cases
        return TriageResult(
            action=TriageAction.OPEN_CASE,
            category=Category.ELEVATOR,
            urgency=Urgency.HIGH,
            confidence=0.94,
            title="A Block elevator shudders near the fourth floor",
            asset_id="elevator-a",
            rationale="Recurring shuddering and grinding on ascent near floor four.",
        )


class IgnoringClassifier:
    def classify(self, *, message, assets, open_cases):
        del message, assets, open_cases
        return TriageResult(
            action=TriageAction.IGNORE,
            category=Category.OTHER,
            urgency=Urgency.LOW,
            confidence=0.99,
            rationale="No operational problem.",
        )


def memory_service(seed, memory, classifier=None):
    store = InMemoryCaseStore()
    service = IntakeService(
        classifier=classifier or ElevatorOpeningClassifier(),
        store=store,
        policy=PolicyEngine(seed.policies, seed.settings),
        assets=seed.assets,
        clock=FrozenClock(seed.demo_messages[0].message.ingested_at),
        memory=memory,
        id_factory=IdSequence(),
        reply_token_factory=lambda: "abcdef123456",
    )
    return service, store


def test_intake_links_recall_to_case_and_writes_source_traced_timeline(seed, retriever):
    service, store = memory_service(seed, retriever)
    message = seed.demo_messages[0].message

    outcome = service.process(message)

    assert outcome.disposition is IntakeDisposition.OPENED
    assert outcome.memory_error_type is None
    assert outcome.memory_recall is not None
    assert tuple(outcome.case.related_case_ids) == EXPECTED_ELEVATOR_HISTORY
    assert outcome.memory_recall.related_case_ids == EXPECTED_ELEVATOR_HISTORY

    timeline = store.timeline_for(outcome.case.case_id)
    assert [event.kind for event in timeline] == [
        EventKind.CASE_OPENED,
        EventKind.MEMORY_CONSULTED,
        EventKind.POLICY_EVALUATED,
    ]
    memory_event = timeline[1]
    assert memory_event.payload["status"] == "ok"
    assert [item["case_id"] for item in memory_event.payload["case_hits"]] == list(
        EXPECTED_ELEVATOR_HISTORY
    )
    assert [item["relation"] for item in memory_event.payload["case_hits"][:2]] == [
        "same_fault",
        "same_fault",
    ]
    assert [item["vendor_id"] for item in memory_event.payload["vendor_scorecards"]] == [
        "meridian-lift",
        "pinnacle-vertical",
        "coastline-elevator",
    ]
    assert set(memory_event.refs) == set(outcome.memory_recall.source_case_ids)
    for item in memory_event.payload["vendor_scorecards"]:
        metric_sources = set(item["job_case_ids"])
        metric_sources.update(item["response_case_ids"])
        metric_sources.update(item["engagement_case_ids"])
        metric_sources.update(item["recurrence_case_ids"])
        assert set(item["source_case_ids"]) == metric_sources
        assert metric_sources <= set(memory_event.refs)

    assert store.audit_for(outcome.case.case_id)[0].policy_rule_id == Rule.INTAKE_WITHIN_POLICY


def test_memory_failure_isolated_case_and_policy_still_persist(seed):
    class BrokenMemory:
        def recall(self, **_kwargs):
            raise RuntimeError("secret backend detail must not be persisted")

    service, store = memory_service(seed, BrokenMemory())

    outcome = service.process(seed.demo_messages[0].message)

    assert outcome.disposition is IntakeDisposition.OPENED
    assert outcome.memory_recall is None
    assert outcome.memory_error_type == "RuntimeError"
    assert outcome.case.related_case_ids == []
    assert outcome.policy_decision.rule_id == Rule.INTAKE_WITHIN_POLICY

    timeline = store.timeline_for(outcome.case.case_id)
    assert [event.kind for event in timeline] == [
        EventKind.CASE_OPENED,
        EventKind.MEMORY_CONSULTED,
        EventKind.POLICY_EVALUATED,
    ]
    memory_event = timeline[1]
    assert memory_event.payload == {
        "status": "unavailable",
        "error_type": "RuntimeError",
    }
    assert memory_event.refs == []
    assert "secret backend detail" not in memory_event.model_dump_json()
    assert len(store.audit_for(outcome.case.case_id)) == 1


def test_ignored_message_does_not_consult_case_memory(seed):
    class MustNotRunMemory:
        def recall(self, **_kwargs):
            raise AssertionError("ignored message reached case memory")

    service, store = memory_service(
        seed,
        MustNotRunMemory(),
        classifier=IgnoringClassifier(),
    )
    message = seed.demo_messages[3].message

    outcome = service.process(message)

    assert outcome.disposition is IntakeDisposition.IGNORED
    assert outcome.memory_recall is None
    assert outcome.memory_error_type is None
    assert store.list_cases() == ()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"same_fault_threshold": -0.1}, "same_fault_threshold"),
        ({"same_fault_threshold": 1.1}, "same_fault_threshold"),
        ({"recurrence_window_days": 0}, "recurrence_window_days"),
        ({"max_case_hits": 0}, "max_case_hits"),
    ],
)
def test_retriever_rejects_invalid_configuration(seed, kwargs, message):
    with pytest.raises(ValueError, match=message):
        StructuredMemoryRetriever(seed.history, **kwargs)


def test_retriever_rejects_naive_as_of(seed, retriever):
    with pytest.raises(ValueError, match="timezone-aware"):
        retriever.recall(
            category=Category.ELEVATOR,
            asset_id="elevator-a",
            query_text=elevator_query(seed),
            as_of=datetime(2026, 9, 10, 9, 0),
        )
