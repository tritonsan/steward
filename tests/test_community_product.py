from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from test_meeting_lifecycle import (
    MINUTES,
    _AgendaPlanner,
    _MeetingClassifier,
    _MinutesExtractor,
    _ResolutionPlanner,
)

from steward.api import create_app
from steward.config import StewardSettings
from steward.domain.clock import FrozenClock
from steward.domain.enums import CaseStatus
from steward.runtime import build_runtime


class Minutes(_MinutesExtractor):
    def extract(self, **kwargs):
        result = super().extract(**kwargs)
        return result.model_copy(
            update={
                "action_items": tuple(
                    a.model_copy(update={"owner_participant_id": "Simon O."})
                    for a in result.action_items
                )
            }
        )


def community_to_confirmed_actions(tmp_path, classifier=None):
    clock = FrozenClock(datetime(2026, 9, 14, 10, tzinfo=timezone.utc))
    runtime = build_runtime(
        StewardSettings(database_path=tmp_path / "community.db"),
        clock=clock,
        classifier=classifier or _MeetingClassifier(),
        quote_extractor=__import__("test_quote_decisions")._QuoteExtractor(),
        decision_recommender=__import__("test_quote_decisions")._SelectingRecommender(),
        resolution_planner=_ResolutionPlanner(),
        meeting_agenda_planner=_AgendaPlanner(),
        meeting_minutes_extractor=Minutes(),
    )
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
    manager = {"Authorization": "Bearer manager"}

    def post(path, body=None, *, key="", token="manager"):
        response = client.post(
            "/api" + path,
            json=body,
            headers={"Authorization": "Bearer " + token, "Idempotency-Key": key or path},
        )
        assert response.status_code == 200, response.text
        return response.json()

    post("/simulation/messages", {"sender": "James D.", "text": "We need a shared parking rule."})
    post("/simulation/tick")
    case = client.get("/api/cases", headers=manager).json()[0]
    cid = case["case_id"]
    assert any(
        t["kind"] == "meeting_schedule" for t in client.get("/api/tasks", headers=manager).json()
    )

    def command(action, data=None, notes="Recorded evidence"):
        version = client.get("/api/cases/" + cid, headers=manager).json()["version"]
        return post(
            f"/cases/{cid}/commands",
            {"action": action, "notes": notes, "data": data or {}, "expected_version": version},
            key=action,
        )

    command(
        "meeting_schedule",
        {
            "policy": {
                "timezone": "UTC",
                "quorum": 2,
                "eligible_participant_ids": ["Simon O.", "James D."],
                "required_participant_ids": ["Simon O."],
            },
            "slots": [
                {"slot_id": "tuesday", "starts_at": (clock.now() + timedelta(days=2)).isoformat()}
            ],
        },
    )
    for token in ("manager", "resident"):
        version = client.get(
            "/api/cases/" + cid + "/meeting", headers={"Authorization": "Bearer " + token}
        ).json()["version"]
        post(
            f"/cases/{cid}/availability",
            {"expected_version": version, "available_slot_ids": ["tuesday"]},
            token=token,
        )
        post("/simulation/tick")
    assert runtime.store.get_case(cid).status == CaseStatus.MEETING_READY
    post("/simulation/advance?hours=49")
    command(
        "meeting_minutes",
        {"held_at": (clock.now() - timedelta(hours=1)).isoformat(), "participant_count": 2},
        notes=MINUTES,
    )
    post("/simulation/tick")
    candidates = runtime.store.artifacts_for(kind="meeting_decision_candidates.v1", case_id=cid)[
        0
    ].payload
    command(
        "meeting_confirm",
        {
            "decision_ids": [d["decision_id"] for d in candidates["decisions"]],
            "action_ids": [a["action_id"] for a in candidates["action_items"]],
        },
    )
    return runtime, client, cid, candidates, post, command, clock


def test_community_events_reach_verified_memory_without_manual_service_calls(tmp_path):
    runtime, client, cid, candidates, post, command, clock = community_to_confirmed_actions(
        tmp_path
    )
    command(
        "meeting_action",
        {"action_id": candidates["action_items"][0]["action_id"]},
        notes="Published the signed visitor rule on the community noticeboard.",
    )
    command(
        "meeting_verify",
        {"outcome": "confirmed"},
        notes="Residents can find and follow the published rule.",
    )
    assert runtime.store.get_case(cid).status == CaseStatus.CLOSED
    assert next(r for r in runtime.store.records() if r.case_id == cid).outcome_verified
    assert next(r for r in runtime.store.records() if r.case_id == cid).is_simulated
    assert not [t for t in runtime.store.human_tasks(cid) if t.status == "open"]
    runtime.close()


def test_confirmed_community_action_reaches_linked_verified_maintenance(tmp_path):
    from test_quote_decisions import COASTLINE_BODY, MERIDIAN_BODY, _ElevatorClassifier

    class Routing(_MeetingClassifier):
        def classify(self, **kwargs):
            if kwargs["message"].source == "community_procurement":
                return _ElevatorClassifier().classify(**kwargs)
            return super().classify(**kwargs)

    runtime, client, cid, candidates, post, command, clock = community_to_confirmed_actions(
        tmp_path, Routing()
    )
    action = candidates["action_items"][0]["action_id"]
    command(
        "meeting_procurement",
        {"action_id": action, "asset_id": "elevator-a"},
        notes="Inspect and repair the guide shoes as part of the confirmed action.",
    )
    parent_version = runtime.store.case_version(cid)
    rejected = client.post(
        f"/api/cases/{cid}/commands",
        headers={"Authorization": "Bearer manager", "Idempotency-Key": "early-complete"},
        json={
            "action": "meeting_action",
            "expected_version": parent_version,
            "notes": "Not yet verified",
            "data": {"action_id": action},
        },
    )
    assert rejected.status_code == 422
    post("/simulation/tick")
    child = next(c for c in runtime.store.list_cases() if c.case_id != cid)
    assert cid in runtime.store.get_case(child.case_id).related_case_ids
    for vendor, text in [("meridian-lift", MERIDIAN_BODY), ("coastline-elevator", COASTLINE_BODY)]:
        post(
            "/simulation/vendor-replies",
            {"case_id": child.case_id, "vendor_id": vendor, "text": text},
            key=vendor,
        )
    post("/simulation/advance?hours=24")
    post("/simulation/tick")
    from appointment_support import confirm_appointment
    confirm_appointment(runtime, clock, child.case_id)
    assert runtime.store.get_case(child.case_id).status == CaseStatus.SCHEDULED
    for action_name, notes in [
        ("record_completion", "Guide shoes repaired and tested."),
        ("verify", "Resident inspected the repair and confirmed success."),
    ]:
        post(
            f"/cases/{child.case_id}/commands",
            {
                "action": action_name,
                "expected_version": runtime.store.case_version(child.case_id),
                "notes": notes,
            },
            key=action_name,
        )
    command("meeting_action", {"action_id": action}, notes="Linked repair has a verified outcome.")
    command(
        "meeting_verify",
        {"outcome": "confirmed"},
        notes="The agreed work was delivered and verified.",
    )
    assert (
        runtime.store.get(cid).outcome_verified
        and runtime.store.get(child.case_id).outcome_verified
    )
    runtime.close()
