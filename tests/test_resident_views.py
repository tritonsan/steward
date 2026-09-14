from fastapi.testclient import TestClient
from test_community_product import community_to_confirmed_actions
from test_product_lifecycle import scheduled

from steward.api import create_app


def test_resident_sees_community_summary_without_operational_details(tmp_path):
    runtime, _, case_id = scheduled(tmp_path)
    with TestClient(
        create_app(
            runtime,
            tokens={
                "resident": {"actor_id": "James D.", "role": "resident"},
                "unknown": {"actor_id": "Unregistered", "role": "resident"},
            },
        )
    ) as client:
        headers = {"Authorization": "Bearer resident"}
        response = client.get("/api/resident/overview", headers=headers)
        assert response.status_code == 200
        overview = response.json()
        assert overview["invitations"] == []
        assert overview["cases"][0]["case_id"] == case_id
        assert set(overview["cases"][0]) == {
            "case_id",
            "title",
            "category",
            "status",
            "version",
            "update",
            "updated_at",
        }
        assert client.get(f"/api/cases/{case_id}", headers=headers).json() == overview["cases"][0]
        for path in ("/api/tasks", "/api/settings", "/api/memory", "/api/directory"):
            assert client.get(path, headers=headers).status_code == 403
        assert (
            client.get(
                "/api/resident/overview", headers={"Authorization": "Bearer unknown"}
            ).status_code
            == 403
        )
    runtime.close()


def test_resident_invitation_excludes_drafts_participants_and_procurement(tmp_path):
    runtime, client, cid, _, _, _, _ = community_to_confirmed_actions(tmp_path)
    headers = {"Authorization": "Bearer resident"}
    overview = client.get("/api/resident/overview", headers=headers).json()
    invite = overview["invitations"][0]
    assert invite["case_id"] == cid and invite["starts_at"] and invite["agenda"]
    assert set(invite) == {
        "case_id",
        "title",
        "version",
        "starts_at",
        "slots",
        "agenda",
        "available_slot_ids",
        "responded",
        "schedule_id",
        "round",
    }
    assert client.get(f"/api/cases/{cid}/meeting", headers=headers).json() == invite
    assert (
        "meeting_decision_candidates.v1"
        in client.get(
            f"/api/cases/{cid}/meeting", headers={"Authorization": "Bearer manager"}
        ).json()["records"]
    )
    outsider = TestClient(
        create_app(runtime, tokens={"other": {"actor_id": "Daniel K.", "role": "resident"}})
    )
    other = {"Authorization": "Bearer other"}
    assert outsider.get("/api/resident/overview", headers=other).json()["invitations"] == []
    assert outsider.get(f"/api/cases/{cid}/meeting", headers=other).status_code == 403
    runtime.close()
