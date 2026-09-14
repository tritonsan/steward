"""The prepared jury workspace is usable, isolated and never reset on restart."""

import pytest
from fastapi.testclient import TestClient

from steward.api import create_app
from steward.config import StewardSettings
from steward.demo.review_seed import (
    MARKER_KIND,
    SEED_ID,
    PreparedAgenda,
    PreparedIntake,
    PreparedQuotes,
    PreparedRecommendation,
    PreparedResolution,
    prepare,
)
from steward.review import build_review_runtime
from steward.runtime import build_runtime


def prepared_runtime(tmp_path):
    return build_review_runtime(
        StewardSettings(
            database_path=tmp_path / "owner.db", database_url=None, database_secret=None
        ),
        classifier=PreparedIntake(),
        quote_extractor=PreparedQuotes(),
        decision_recommender=PreparedRecommendation(),
        resolution_planner=PreparedResolution(),
        meeting_agenda_planner=PreparedAgenda(),
    )


def test_prepared_scenarios_have_sources_quote_approval_and_resident_invitation(tmp_path):
    with prepared_runtime(tmp_path) as runtime:
        report = prepare(runtime)
        assert report["live_model_calls"] is False
        assert report["external_delivery"] is False
        assert len(runtime.store.records()) == 63
        assert len(runtime.store.list_cases()) == 2
        assert not (tmp_path / "owner.db").exists()
        parking = report["case_ids"]["parking"]
        elevator = report["case_ids"]["elevator"]
        assert any(
            t.kind == "quote_approval" and t.status == "open"
            for t in runtime.store.human_tasks(elevator)
        )
        assert runtime.store.get_case(elevator).accepted_quote_id is None
        assert len(runtime.store.artifacts_for(kind="vendor_quote.v1", case_id=elevator)) == 2
        assert not runtime.store.artifacts_for(kind="meeting_packet.v1", case_id=parking)
        assert (
            len(runtime.store.artifacts_for(kind="community.availability.v1", case_id=parking)) == 1
        )
        app = create_app(
            runtime,
            simulation=True,
            tokens={"resident": {"actor_id": "Daniel K.", "role": "resident"}},
        )
        headers = {"Authorization": "Bearer resident"}
        with TestClient(app) as client:
            response = client.get("/api/resident/overview", headers=headers)
            assert response.status_code == 200
            assert parking in response.text
            assert "first-evening" in response.text
            assert client.get("/api/settings", headers=headers).status_code == 403
        clock = runtime._clock.now()
        versions = {
            c.case_id: runtime.store.case_version(c.case_id) for c in runtime.store.list_cases()
        }
        assert prepare(runtime)["already_prepared"] is True
        assert runtime._clock.now() == clock
        assert versions == {
            c.case_id: runtime.store.case_version(c.case_id) for c in runtime.store.list_cases()
        }


def test_seed_rejects_owner_runtime_before_any_write(tmp_path):
    with build_runtime(
        StewardSettings(
            database_path=tmp_path / "owner.db", database_url=None, database_secret=None
        )
    ) as owner:
        with pytest.raises(ValueError, match="build_review_runtime"):
            prepare(owner)
        assert not owner.store.list_cases()
        assert not owner.store.records()
        assert not owner.store.artifacts_for(kind=MARKER_KIND)


def test_completed_seed_preserves_later_reviewer_actions(tmp_path):
    with prepared_runtime(tmp_path) as runtime:
        report = prepare(runtime)
        parking = report["case_ids"]["parking"]
        with TestClient(
            create_app(
                runtime,
                simulation=True,
                tokens={"resident": {"actor_id": "Daniel K.", "role": "resident"}},
            )
        ) as client:
            response = client.post(
                f"/api/cases/{parking}/availability",
                headers={"Authorization": "Bearer resident", "Idempotency-Key": "reviewer-reply"},
                json={
                    "expected_version": runtime.store.case_version(parking),
                    "available_slot_ids": ["first-evening"],
                },
            )
            assert response.status_code == 200, response.text
        runtime.tick()
        assert runtime.store.get_case(parking).status.value == "meeting_ready"
        version = runtime.store.case_version(parking)
        assert prepare(runtime)["already_prepared"] is True
        assert runtime.store.case_version(parking) == version
        assert runtime.store.get_case(parking).status.value == "meeting_ready"


def test_seed_resumes_when_process_stops_before_completion_marker(tmp_path, monkeypatch):
    with prepared_runtime(tmp_path) as runtime:
        put = runtime.store.put_configuration

        def crash_at_final_marker(artifact):
            if artifact.artifact_id == SEED_ID + ":complete":
                raise RuntimeError("injected process stop")
            return put(artifact)

        monkeypatch.setattr(runtime.store, "put_configuration", crash_at_final_marker)
        with pytest.raises(RuntimeError, match="injected process stop"):
            prepare(runtime)
        first_ids = {case.case_id for case in runtime.store.list_cases()}
        first_clock = runtime._clock.now()
    with prepared_runtime(tmp_path) as restarted:
        report = prepare(restarted)
        assert report["already_prepared"] is False
        assert {case.case_id for case in restarted.store.list_cases()} == first_ids
        assert restarted._clock.now() == first_clock
        assert len(restarted.store.records()) == 63
        assert len(restarted.store.artifacts_for(kind="vendor_quote.v1")) == 2
