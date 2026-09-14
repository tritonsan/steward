"""The cloud probe can be rehearsed without network access or manual worker ticks."""

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_review_seed import prepared_runtime

from steward.api import create_app
from steward.config import StewardSettings
from steward.demo.review_seed import prepare
from steward.runtime import StewardRuntime, build_runtime


def test_cloud_probe_checks_real_api_contract_and_only_enqueues_injection(tmp_path):
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "tools" / "cloud_review_smoke.py"
    spec = importlib.util.spec_from_file_location("cloud_review_smoke", path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    code = "offline-test-review-code-long-enough-for-safe-sessions"
    with prepared_runtime(tmp_path) as review:
        prepare(review)
        with build_runtime(
            StewardSettings(
                database_path=tmp_path / "owner.db", database_url=None, database_secret=None
            )
        ) as owner:
            app = create_app(
                owner, review_runtime=review, review_access_code=code, review_enabled=True
            )
            with TestClient(app) as client:
                report = {"checks": []}
                with patch.object(
                    StewardRuntime, "tick", side_effect=AssertionError("No manual ticks")
                ):
                    probe.run_checks(client, code, report, inject=True)
                assert all(row["passed"] for row in report["checks"])
                assert report["aggregate"]["history_count"] == 63
                assert report["injection"]["newly_accepted"] is True
                assert report["injection"]["worker_tick_requested"] is False
                assert len(review.store.list_cases()) == 2
                assert review.store.inbox_item("simulation", probe.MESSAGE_ID) is not None
                assert code not in json.dumps(report)
                assert probe.MESSAGE["text"] not in json.dumps(report)
                second = {"checks": []}
                probe.run_checks(client, code, second, inject=True)
                assert second["injection"]["newly_accepted"] is False


def test_workflow_evidence_retains_only_sanitized_source_and_plan_metadata():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "tools" / "cloud_review_smoke.py"
    spec = importlib.util.spec_from_file_location("cloud_review_smoke", path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    source = {"source_id": "sim:source", "text": "private raw source", "author": "hidden author"}
    detail = {
        "case_id": "case-1",
        "version": 4,
        "timeline": [
            {
                "kind": "case_opened",
                "event_id": "event-1",
                "payload": {"triage_confidence": 0.97, "category": "plumbing", "secret": "hidden"},
            },
            {
                "kind": "resolution_planned",
                "event_id": "event-2",
                "payload": {
                    "plan_id": "plan-1",
                    "context_id": "context-1",
                    "resolution_path": "warranty_followup",
                    "secret": "hidden",
                },
            },
        ],
        "tasks": [{"task_id": "task-1", "kind": "planning_review", "reason": "private rationale"}],
    }
    evidence = probe.workflow_evidence(detail, source)
    assert evidence["case_version"] == 4
    assert evidence["stage"] == "awaiting_human"
    assert evidence["intake"]["triage_confidence"] == 0.97
    assert evidence["plan"]["context_id"] == "context-1"
    assert not evidence["provider_trace_available_in_api"]
    assert all(
        value not in json.dumps(evidence)
        for value in ("private raw source", "hidden author", "private rationale", "hidden")
    )
