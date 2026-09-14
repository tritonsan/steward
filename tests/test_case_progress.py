"""Management summaries follow delivery evidence, not an older monitoring job."""

from datetime import timedelta
from unittest.mock import patch

from appointment_support import confirm_appointment
from test_community_product import community_to_confirmed_actions
from test_four_stage_continuity import client_for, command
from test_product_lifecycle import scheduled
from test_quote_decisions import (
    MERIDIAN_BODY,
    PINNACLE_BODY,
    _prepare_runtime,
    _record_reply,
    _SelectingRecommender,
)

from steward.domain.clock import parse_datetime


def detail(client, case_id):
    response = client.get(f"/api/cases/{case_id}", headers={"Authorization": "Bearer m"})
    assert response.status_code == 200, response.text
    return response.json()


def test_progress_distinguishes_order_delivery_and_confirmed_visit(tmp_path):
    rt, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    try:
        _record_reply(rt, clock, case, "meridian-lift", MERIDIAN_BODY)
        decision = rt.decide_quotes(case.case_id).decision
        with client_for(rt) as client:
            response = command(
                client,
                rt,
                case.case_id,
                "approve_quote",
                data={"decision_id": decision.decision_id},
            )
            assert response.status_code == 200, response.text
            before = detail(client, case.case_id)
            assert before["status"] == "committed"
            assert before["next_step"] == "Send the approved service order"
            assert before["waiting_for"] == "Steward"
            assert before["due_at"] is None
            assert not rt.tick().workflow_failures
            delivered = detail(client, case.case_id)
            assert delivered["status"] == "awaiting_appointment"
            assert delivered["next_step"] == "Await the vendor’s appointment proposal"
            assert delivered["scheduled_for"] is None
            assert delivered["waiting_for"] == "Selected vendor"

            # Pause only the outbound boundary: the API must not call this a
            # confirmed visit until the queued acceptance has a delivery receipt.
            pending = []
            dispatch = rt._outbox.dispatch

            def observe_before_delivery(_dispatcher):
                pending.append(detail(client, case.case_id))
                return dispatch()

            with patch.object(type(rt._outbox), "dispatch", observe_before_delivery):
                confirm_appointment(rt, clock, case.case_id)
            assert pending[-1]["status"] == "awaiting_appointment"
            assert pending[-1]["next_step"] == "Send appointment confirmation"
            assert pending[-1]["scheduled_for"] is None

            confirmed = detail(client, case.case_id)
            assert confirmed["status"] == "scheduled"
            assert confirmed["next_step"] == "Attend the confirmed repair visit"
            assert parse_datetime(confirmed["due_at"]) == parse_datetime(confirmed["scheduled_for"])
            appointment = confirmed["appointments"][-1]["payload"]["facts"]
            end = parse_datetime(appointment["ends_at"])
            clock.advance(end - clock.now() + timedelta(minutes=5))
            after_visit = detail(client, case.case_id)
            assert after_visit["next_step"] == "Check the visit outcome"
            assert parse_datetime(after_visit["due_at"]) == end + timedelta(hours=1)
            assert after_visit["version"] == confirmed["version"]
            assert len(rt.store.reservations(case.case_id)) == 1

            rt._maintenance.task(
                rt.store.get_case(case.case_id),
                "appointment_delivery",
                "Check appointment acceptance delivery",
                "Provider receipt needs review.",
            )
            exception = detail(client, case.case_id)
            assert exception["next_step"] == "Check appointment acceptance delivery"
            assert exception["waiting_for"] == "Management"
    finally:
        rt.close()


def test_closed_repair_keeps_verified_summary_despite_old_monitoring_jobs(tmp_path):
    rt, _, cid = scheduled(tmp_path)
    try:
        with client_for(rt) as client:
            for action in ("record_completion", "verify"):
                response = command(client, rt, cid, action, notes="All lift stops checked.")
                assert response.status_code == 200, response.text
            assert any(j.status.value == "pending" for j in rt.store.workflow_jobs(cid))
            closed = detail(client, cid)
            assert closed["status"] == "closed"
            assert closed["next_step"] == "Verified outcome saved to community memory"
            assert closed["waiting_for"] == "No one"
            assert closed["due_at"] is None
    finally:
        rt.close()


def test_closed_community_case_preserves_verified_outcome_summary(tmp_path):
    rt, client, cid, candidates, _, community_command, _ = community_to_confirmed_actions(tmp_path)
    try:
        community_command(
            "meeting_action",
            {"action_id": candidates["action_items"][0]["action_id"]},
            notes="Published the signed visitor rule on the noticeboard.",
        )
        community_command(
            "meeting_verify",
            {"outcome": "confirmed"},
            notes="Residents can find and follow the published rule.",
        )
        response = client.get(f"/api/cases/{cid}", headers={"Authorization": "Bearer manager"})
        assert response.status_code == 200
        assert response.json()["next_step"] == "Verified outcome saved to community memory"
        assert response.json()["waiting_for"] == "No one"
        assert response.json()["due_at"] is None
    finally:
        client.close()
        rt.close()


def test_quote_revision_history_is_persisted_and_management_only(tmp_path):
    rt, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    try:
        old = _record_reply(rt, clock, case, "meridian-lift", MERIDIAN_BODY)
        with client_for(rt) as client:
            response = command(
                client, rt, case.case_id, "reject_quote", data={"quote_id": old.quote_artifact_id}
            )
            assert response.status_code == 200, response.text
            new = _record_reply(rt, clock, case, "meridian-lift", PINNACLE_BODY)
            response = command(
                client,
                rt,
                case.case_id,
                "supersede_quote",
                data={
                    "previous_quote_id": old.quote_artifact_id,
                    "replacement_quote_id": new.quote_artifact_id,
                },
            )
            assert response.status_code == 200, response.text
            view = detail(client, case.case_id)
            assert view["quote_rejections"][0]["payload"]["quote_id"] == old.quote_artifact_id
            supersession = view["quote_supersessions"][0]["payload"]
            assert supersession["previous_quote_id"] == old.quote_artifact_id
            assert supersession["replacement_quote_id"] == new.quote_artifact_id
            resident = client.get(
                f"/api/cases/{case.case_id}", headers={"Authorization": "Bearer other"}
            )
            assert resident.status_code == 200
            assert "quote_rejections" not in resident.json()
            assert "quote_supersessions" not in resident.json()
    finally:
        rt.close()


def test_meeting_shows_procurement_request_before_linked_case_exists(tmp_path):
    rt, client, cid, candidates, _, community_command, _ = community_to_confirmed_actions(tmp_path)
    try:
        action_id = candidates["action_items"][0]["action_id"]
        community_command(
            "meeting_procurement",
            {"action_id": action_id, "asset_id": "elevator-a"},
            notes="Inspect guide shoes under the approved scope.",
        )
        response = client.get(
            f"/api/cases/{cid}/meeting", headers={"Authorization": "Bearer manager"}
        )
        assert response.status_code == 200, response.text
        assert not response.json()["maintenance"]
        requests = response.json()["procurement_requests"]
        assert len(requests) == 1
        assert requests[0]["payload"]["action_id"] == action_id
        resident = client.get(
            f"/api/cases/{cid}/meeting", headers={"Authorization": "Bearer resident"}
        )
        assert "procurement_requests" not in resident.json()
    finally:
        client.close()
        rt.close()
