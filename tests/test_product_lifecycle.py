from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_quote_decisions import (
    COASTLINE_BODY,
    MERIDIAN_BODY,
    _prepare_runtime,
    _record_reply,
    _SelectingRecommender,
)

from steward.api import create_app
from steward.domain.enums import CaseStatus


def scheduled(tmp_path):
    runtime, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    _record_reply(runtime, clock, case, "meridian-lift", MERIDIAN_BODY)
    _record_reply(runtime, clock, case, "coastline-elevator", COASTLINE_BODY)
    clock.advance(timedelta(hours=24))
    report = runtime.tick()
    assert report.workflow_failures == ()
    assert runtime.store.get_case(case.case_id).status == CaseStatus.AWAITING_APPOINTMENT
    from appointment_support import confirm_appointment

    confirm_appointment(runtime, clock, case.case_id)
    assert len(runtime.store.reservations()) == 1
    assert runtime.store.reservations()[0].status == "committed"
    assert len([m for m in runtime.transport.sent if m.subject.startswith("Service order:")]) == 1
    return runtime, clock, case.case_id


def test_worker_to_human_verification_writes_memory_atomically(tmp_path):
    runtime, clock, case_id = scheduled(tmp_path)
    app = create_app(
        runtime,
        tokens={
            "manager-test": {"actor_id": "Simon O.", "role": "manager"},
            "resident-test": {"actor_id": "Daniel K.", "role": "resident"},
        },
    )
    with TestClient(app) as client:
        assert client.get("/api/cases").status_code == 401
        resident_response = client.get(
            "/api/cases", headers={"Authorization": "Bearer resident-test"}
        )
        assert resident_response.status_code == 200
        assert all(
            "outbox" not in c and "decisions" not in c and "source_message_ids" not in c
            for c in resident_response.json()
        )
        assert (
            client.get("/api/tasks", headers={"Authorization": "Bearer resident-test"}).status_code
            == 403
        )
        headers = {"Authorization": "Bearer manager-test", "Idempotency-Key": "completion-1"}
        completion = {
            "action": "record_completion",
            "notes": "Replaced the guide shoes and tested the lift.",
            "expected_version": runtime.store.case_version(case_id),
        }
        url = f"/api/cases/{case_id}/commands"
        assert client.post(url, headers=headers, json=completion).status_code == 200
        assert runtime.store.get_case(case_id).status == CaseStatus.AWAITING_VERIFICATION
        assert runtime.store.get(case_id) is None
        assert client.post(url, headers=headers, json=completion).json() == {"applied": False}
        headers["Idempotency-Key"] = "verification-1"
        verification = {
            "action": "verify",
            "notes": "Tested every stop; the vibration is gone.",
            "accepted": True,
            "expected_version": runtime.store.case_version(case_id),
        }
        response = client.post(url, headers=headers, json=verification)
        assert response.status_code == 200, response.text
        assert runtime.store.get_case(case_id).status == CaseStatus.CLOSED
        assert runtime.store.get(case_id).outcome_verified
        assert runtime.store.get(case_id).is_simulated
        assert client.post(url, headers=headers, json=verification).json() == {"applied": False}
        assert not [
            t
            for t in runtime.store.human_tasks(case_id)
            if t.kind == "verify_completion" and t.status == "open"
        ]
    runtime.close()


def test_rejected_completion_does_not_teach_success(tmp_path):
    runtime, clock, case_id = scheduled(tmp_path)
    runtime._maintenance.claim_completion(
        case_id,
        actor_id="Simon O.",
        notes="Vendor reports repair",
        expected_version=runtime.store.case_version(case_id),
        source_id="claim-1",
    )
    with pytest.raises(PermissionError):
        runtime._maintenance.verify(
            case_id,
            actor_id="Vendor",
            accepted=True,
            notes="done",
            expected_version=runtime.store.case_version(case_id),
            source_id="bad",
        )
    runtime._maintenance.verify(
        case_id,
        actor_id="Simon O.",
        accepted=False,
        notes="Still shaking",
        expected_version=runtime.store.case_version(case_id),
        source_id="verification-1",
    )
    assert runtime.store.get_case(case_id).status == CaseStatus.WARRANTY_REVIEW
    assert runtime.store.get(case_id) is None
    assert runtime.store.human_tasks(case_id)[-1].kind == "warranty_review"
    runtime.close()


def test_api_rejects_stale_command_and_simulation_when_disabled(tmp_path):
    runtime, _, case_id = scheduled(tmp_path)
    with TestClient(
        create_app(runtime, tokens={"test": {"actor_id": "Simon O.", "role": "manager"}})
    ) as client:
        headers = {"Authorization": "Bearer test", "Idempotency-Key": "stale"}
        assert (
            client.post(
                f"/api/cases/{case_id}/commands",
                headers=headers,
                json={"expected_version": 1, "action": "record_completion", "notes": "done"},
            ).status_code
            == 409
        )
        assert client.post("/api/simulation/tick", headers=headers).status_code == 403
    runtime.close()
