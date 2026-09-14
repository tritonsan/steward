from types import SimpleNamespace

import pytest

from steward.agents.coordinator import CallBudget, InferenceBudgetExceeded, StrandsCoordinator
from steward.agents.planning import ResolutionPlanRecommendation
from steward.config import StewardSettings
from steward.domain.clock import SystemClock
from steward.domain.enums import ResolutionPath
from steward.store import SqliteOperationalStore


def test_coordinator_is_bounded_and_persists_sources_and_metrics(tmp_path):
    store = SqliteOperationalStore(tmp_path / "trace.db")
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)

        def invoke(*_, **options):
            if "structured_output_model" not in options:
                kwargs["hooks"][0].before_tool(SimpleNamespace(tool_use={"name": "case_evidence"}))
            return SimpleNamespace(
                structured_output=ResolutionPlanRecommendation(
                    path=ResolutionPath.HUMAN_REVIEW,
                    rationale="Location is unknown",
                    source_ids=("m1",),
                ),
                metrics=SimpleNamespace(accumulated_usage={"inputTokens": 100}),
            )

        return invoke

    coordinator = StrandsCoordinator(
        settings=StewardSettings(), store=store, clock=SystemClock(), agent_factory=factory
    )
    result = coordinator.plan(
        context_id="snapshot-1",
        context={
            "case": {"title": "Leak"},
            "messages": [],
            "allowed_source_ids": ["m1"],
            "offered_resolution_paths": ["human_review"],
        },
    )
    assert result.source_ids == ("m1",)
    assert len(calls[0]["tools"]) == 4
    trace = store.artifacts_for(kind="agent.trace.v1")[0].payload
    assert trace["usage"]["inputTokens"] == 200
    assert trace["source_ids"] == ["m1"]
    budget = CallBudget(max_tools=1, max_models=1)
    event = SimpleNamespace(tool_use={"name": "case_evidence"})
    budget.before_tool(event)
    with pytest.raises(InferenceBudgetExceeded):
        budget.before_tool(event)
    budget.before_model(None)
    with pytest.raises(InferenceBudgetExceeded):
        budget.before_model(None)
    store.close()
