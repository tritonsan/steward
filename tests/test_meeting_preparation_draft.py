from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from test_meeting_lifecycle import _AgendaPlanner, _MeetingClassifier, _ResolutionPlanner

from steward.agents.planning import MeetingQuoteNeed, MeetingSolutionOption
from steward.api import create_app
from steward.config import StewardSettings
from steward.domain.clock import FrozenClock
from steward.domain.enums import CaseStatus
from steward.meetings.dossier import DRAFT_KIND, INPUT_KIND
from steward.runtime import build_runtime


class Agenda(_AgendaPlanner):
    def __init__(self, fail_once=False, invalid_source=False, invalid_vendor=False):
        self.calls = []
        self.fail_once, self.invalid_source, self.invalid_vendor = (
            fail_once,
            invalid_source,
            invalid_vendor,
        )

    def prepare(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_once and len(self.calls) == 1:
            raise RuntimeError("temporary inference failure")
        result = super().prepare(**kwargs)
        source = "made-up-source" if self.invalid_source else result.summary_source_ids[0]
        return result.model_copy(
            update={
                "solution_options": (
                    MeetingSolutionOption(
                        kind="discussion",
                        title="Publish a time limit",
                        detail="Discuss a written rule.",
                        tradeoffs="Low cost; relies on voluntary compliance.",
                        source_ids=(source,),
                    ),
                ),
                "quote_needs": (
                    MeetingQuoteNeed(
                        scope="Parking signage",
                        reason="Only if signs are part of the agreed scope.",
                        question="Which locations would need signs?",
                        source_ids=(source,),
                        vendor_ids=("made-up-vendor",) if self.invalid_vendor else (),
                    ),
                ),
            }
        )


def setup_case(tmp_path, planner=None, *, auto=True, message=None):
    clock = FrozenClock(datetime(2026, 9, 14, 10, tzinfo=timezone.utc))
    agenda = planner or Agenda()
    settings = StewardSettings(database_path=tmp_path / "draft.db")
    runtime = build_runtime(
        settings,
        clock=clock,
        classifier=_MeetingClassifier(),
        resolution_planner=_ResolutionPlanner(),
        meeting_agenda_planner=agenda,
    )
    for record in runtime._bundle.history:
        runtime.store.write(record)
    client = TestClient(
        create_app(
            runtime,
            simulation=True,
            tokens={
                "manager": {"actor_id": "Simon O.", "role": "manager"},
                "resident": {"actor_id": "James D.", "role": "resident"},
            },
        )
    )
    headers = {"Authorization": "Bearer manager", "Idempotency-Key": "initial-message"}
    assert (
        client.post(
            "/api/simulation/messages",
            headers=headers,
            json={
                "sender": "James D.",
                "text": message or "We need to agree one visitor parking rule.",
            },
        ).status_code
        == 200
    )
    if auto:
        assert client.post("/api/simulation/tick", headers=headers).status_code == 200
    else:
        runtime._runner.tick()  # An existing installation with no preparation continuation.
        runtime.plan_resolution(runtime.store.list_open_cases()[0].case_id)
    cid = runtime.store.list_open_cases()[0].case_id
    return runtime, client, clock, agenda, headers, cid, settings


def test_draft_arrives_via_worker_before_scheduling_and_does_not_authorize_work(tmp_path):
    runtime, client, _, agenda, headers, cid, _ = setup_case(tmp_path)
    result = client.get(f"/api/cases/{cid}/meeting", headers=headers)
    assert result.status_code == 200
    data = result.json()
    assert data["schedule"] is None and data["schedule_id"] is None
    draft = data["preparation"]["payload"]
    assert draft["agenda"]["solution_options"] and draft["agenda"]["quote_needs"]
    assert draft["scheduled"] is False and draft["outbound_enabled"] is False
    assert data["records"]["meeting_packet.v1"] == []
    assert runtime.store.get_case(cid).scheduled_for is None
    assert not runtime.store.get_case(cid).accepted_quote_id
    assert runtime.transport.sent == []
    assert agenda.calls[0]["context"]["selected_meeting_slot"] is None
    assert agenda.calls[0]["context"]["history"]
    # Manager research is not a resident invitation or a leak of committee notes.
    assert (
        client.get(
            f"/api/cases/{cid}/meeting", headers={"Authorization": "Bearer resident"}
        ).status_code
        == 403
    )
    runtime.close()


def test_quorum_reuses_the_prepared_agenda_without_reinvoking_the_model(tmp_path):
    runtime, client, clock, agenda, headers, cid, _ = setup_case(tmp_path)
    draft = runtime.store.artifacts_for(kind=DRAFT_KIND, case_id=cid)[0]
    version = runtime.store.case_version(cid)
    assert (
        client.post(
            f"/api/cases/{cid}/commands",
            headers=headers,
            json={
                "action": "meeting_schedule",
                "notes": "Offer a time",
                "expected_version": version,
                "data": {
                    "policy": {
                        "timezone": "Europe/Istanbul",
                        "quorum": 2,
                        "eligible_participant_ids": ["Simon O.", "James D."],
                    },
                    "slots": [
                        {
                            "slot_id": "one",
                            "starts_at": (clock.now() + timedelta(days=2)).isoformat(),
                        }
                    ],
                },
            },
        ).status_code
        == 200
    )
    for token in ("manager", "resident"):
        h = {"Authorization": "Bearer " + token, "Idempotency-Key": token}
        data = client.get(f"/api/cases/{cid}/meeting", headers=h).json()
        assert (
            client.post(
                f"/api/cases/{cid}/availability",
                headers=h,
                json={
                    "expected_version": data["version"],
                    "schedule_id": data["schedule_id"],
                    "available_slot_ids": ["one"],
                },
            ).status_code
            == 200
        )
        client.post("/api/simulation/tick", headers=headers)
    assert runtime.store.get_case(cid).status == CaseStatus.MEETING_READY
    packet = runtime.store.artifacts_for(kind="meeting_packet.v1", case_id=cid)[0]
    assert packet.payload["agenda"] == draft.payload["agenda"]
    assert len(agenda.calls) == 1
    runtime.close()


@pytest.mark.parametrize("invalid", ["source", "vendor"])
def test_unsupported_sources_or_vendors_never_become_a_draft(tmp_path, invalid):
    runtime, _, _, _, _, cid, _ = setup_case(
        tmp_path, Agenda(invalid_source=invalid == "source", invalid_vendor=invalid == "vendor")
    )
    assert not runtime.store.artifacts_for(kind=DRAFT_KIND, case_id=cid)
    assert runtime.store.artifacts_for(kind=INPUT_KIND, case_id=cid)
    assert runtime.store.get_case(cid).scheduled_for is None
    runtime.close()


def test_restart_recovers_frozen_input_and_deduplicates_draft(tmp_path):
    runtime, _, clock, failed, _, cid, settings = setup_case(tmp_path, Agenda(fail_once=True))
    frozen = runtime.store.artifacts_for(kind=INPUT_KIND, case_id=cid)[0]
    assert not runtime.store.artifacts_for(kind=DRAFT_KIND, case_id=cid)
    runtime.close()
    clock.advance(timedelta(minutes=10))
    model = Agenda()
    restarted = build_runtime(
        settings,
        clock=clock,
        classifier=_MeetingClassifier(),
        resolution_planner=_ResolutionPlanner(),
        meeting_agenda_planner=model,
    )
    restarted.tick()
    restarted.tick()
    assert model.calls[0] == failed.calls[0]
    assert len(model.calls) == 1
    assert len(restarted.store.artifacts_for(kind=DRAFT_KIND, case_id=cid)) == 1
    assert restarted.store.artifacts_for(kind=INPUT_KIND, case_id=cid)[0] == frozen
    restarted.close()


def test_worker_backfills_older_plan_without_requiring_a_new_user_command(tmp_path):
    runtime, _, _, agenda, _, cid, _ = setup_case(tmp_path, auto=False)
    assert not runtime.store.artifacts_for(kind=DRAFT_KIND, case_id=cid)
    runtime.tick()
    runtime.tick()
    assert len(agenda.calls) == 1
    assert len(runtime.store.artifacts_for(kind=DRAFT_KIND, case_id=cid)) == 1
    runtime.close()


def test_old_plan_job_cannot_overwrite_a_new_preparation_revision(tmp_path):
    runtime, _, _, agenda, _, cid, _ = setup_case(tmp_path)
    first = runtime.store.artifacts_for(kind=DRAFT_KIND, case_id=cid)[0]
    newer = runtime.plan_resolution(cid, idempotency_key="reconsider-scope", revise=True)
    # An expired worker resumes with the superseded source plan.
    assert runtime._meeting_preparation.prepare_draft(cid, first.payload["plan_id"]) == first
    runtime.tick()
    drafts = runtime.store.artifacts_for(kind=DRAFT_KIND, case_id=cid)
    assert len(drafts) == 2
    assert {d.payload["plan_id"] for d in drafts} == {first.payload["plan_id"], newer.plan.plan_id}
    assert len(agenda.calls) == 2
    assert runtime.store.artifact(DRAFT_KIND, first.artifact_id) == first
    runtime.close()
