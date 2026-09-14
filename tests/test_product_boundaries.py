import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from test_product_lifecycle import scheduled
from test_workflow_jobs import opened

from steward.api import create_app
from steward.backup import rehearsal, snapshot
from steward.mail.receipt_queue import ReceiptQueue
from steward.proactive import SuggestionStatus
from steward.store.workflow import stable_id


def test_manager_review_creates_linked_plan_revision_with_new_evidence(tmp_path):
    from test_meeting_workflow import _open_meeting_case

    runtime, _, _, case, planner, _ = _open_meeting_case(tmp_path)
    assert not runtime.tick().workflow_failures
    original = runtime._resolution_planning.load(case.case_id)
    client = TestClient(
        create_app(runtime, tokens={"t": {"actor_id": "Simon O.", "role": "manager"}})
    )
    body = {
        "action": "review_resolution",
        "expected_version": runtime.store.case_version(case.case_id),
        "notes": "Keep two visitor spaces available; "
        "the resident register confirms this restriction.",
    }
    headers = {"Authorization": "Bearer t", "Idempotency-Key": "new-evidence"}
    response = client.post(f"/api/cases/{case.case_id}/commands", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert not runtime.tick().workflow_failures
    revised = runtime._resolution_planning.load(case.case_id)
    assert revised.context.revision == 2
    assert revised.context.previous_context_id == original.context.context_id
    assert revised.context.management_notes[-1]["notes"] == body["notes"]
    assert revised.context.policy["global"]["kill_switch"] is False
    assert runtime.store.artifact("resolution_plan.v1", original.plan.plan_id)
    assert client.post(
        f"/api/cases/{case.case_id}/commands", json=body, headers=headers
    ).json() == {"applied": False}
    assert not runtime.tick().workflow_failures
    assert len(planner.calls) == 2
    runtime.close()


def test_recent_verified_repair_blocks_new_rfq_until_documented_review(tmp_path):
    runtime, _, clock = opened(tmp_path)
    record = next(r for r in runtime._bundle.history if r.asset_id == "elevator-a")
    runtime.store.write(
        record.model_copy(
            update={
                "closed_at": clock.now() - timedelta(days=4),
                "resolved_at": clock.now() - timedelta(days=4),
                "outcome_verified": True,
            }
        )
    )
    report = runtime.tick()
    assert not report.queued_rfqs and not runtime.transport.sent
    case = runtime.store.list_open_cases()[0]
    assert runtime.store.human_tasks(case.case_id)[0].kind == "warranty_check"
    client = TestClient(
        create_app(runtime, tokens={"t": {"actor_id": "Simon O.", "role": "manager"}})
    )
    response = client.post(
        f"/api/cases/{case.case_id}/commands",
        headers={"Authorization": "Bearer t", "Idempotency-Key": "checked"},
        json={
            "action": "continue_after_warranty_check",
            "expected_version": runtime.store.case_version(case.case_id),
            "notes": "Reviewed original contract: the prior repair covers a different component.",
        },
    )
    assert response.status_code == 200, response.text
    assert len(runtime.tick().queued_rfqs) == 1
    runtime.close()


def test_policy_change_withdraws_unsent_order_and_rechecks_without_double_reservation(tmp_path):
    from test_quote_decisions import (
        MERIDIAN_BODY,
        _prepare_runtime,
        _record_reply,
        _SelectingRecommender,
    )

    runtime, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    _record_reply(runtime, clock, case, "meridian-lift", MERIDIAN_BODY)
    runtime._maintenance.commit(runtime.decide_quotes(case.case_id).decision)
    client = TestClient(
        create_app(runtime, tokens={"t": {"actor_id": "Simon O.", "role": "manager"}})
    )
    headers = {"Authorization": "Bearer t", "Idempotency-Key": "revoke-before-send"}
    config = client.get("/api/settings", headers=headers).json()
    config["policies"]["elevator"]["per_incident_cap"] = "1"
    body = {
        "expected_version": config["version"],
        "settings": config["settings"],
        "policies": config["policies"],
    }
    response = client.put("/api/settings", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert client.put("/api/settings", headers=headers, json=body).status_code == 200
    changed = {**body, "settings": {**body["settings"], "kill_switch": True}}
    assert client.put("/api/settings", headers=headers, json=changed).status_code == 409
    clock.advance(timedelta(hours=24))
    report = runtime.tick()
    assert not report.workflow_failures
    assert not [m for m in runtime.transport.sent if m.subject.startswith("Service order:")]
    assert all(r.status == "released" for r in runtime.store.reservations(case.case_id))
    assert runtime.store.get_case(case.case_id).accepted_quote_id is None
    assert runtime.store.human_tasks(case.case_id)
    config = client.get("/api/settings", headers=headers).json()
    assert config["policies"]["elevator"]["updated_by"] == "Simon O."
    runtime.close()


def test_unrelated_resident_cannot_verify_repair(tmp_path):
    runtime, _, case_id = scheduled(tmp_path)
    runtime._maintenance.claim_completion(
        case_id,
        actor_id="Simon O.",
        notes="Vendor report",
        expected_version=runtime.store.case_version(case_id),
        source_id="completion-proof",
    )
    authors = {
        runtime.store.get_message(mid).sender_display
        for mid in runtime.store.get_case(case_id).source_message_ids
    }
    other = next(r.display_name for r in runtime._bundle.residents if r.display_name not in authors)
    client = TestClient(create_app(runtime, tokens={"r": {"actor_id": other, "role": "resident"}}))
    response = client.post(
        f"/api/cases/{case_id}/commands",
        headers={"Authorization": "Bearer r", "Idempotency-Key": "unrelated"},
        json={
            "action": "verify",
            "expected_version": runtime.store.case_version(case_id),
            "notes": "Works",
        },
    )
    assert response.status_code == 403
    assert runtime.store.get(case_id) is None
    runtime.close()


def test_backup_rehearsal_preserves_data_and_never_overwrites(tmp_path):
    runtime, cfg, _ = opened(tmp_path)
    runtime.tick()
    result = rehearsal(cfg.database_path, tmp_path / "backup.db", tmp_path / "restore.db")
    assert result["passed"] and result["rows"]["workflow_jobs"] >= 2
    with pytest.raises(FileExistsError):
        snapshot(cfg.database_path, tmp_path / "restore.db")
    runtime.close()


def test_ses_queue_ack_requires_durable_quarantine(tmp_path):
    runtime, _, clock = opened(tmp_path)
    event = {
        "notificationType": "Received",
        "mail": {"messageId": "auth-failed", "timestamp": clock.now().isoformat()},
        "receipt": {
            "dmarcVerdict": {"status": "FAIL"},
            "spamVerdict": {"status": "PASS"},
            "virusVerdict": {"status": "PASS"},
        },
    }
    source = stable_id("ses-receipt", "auth-failed")

    class Queue:
        acks = 0

        def receive_message(self, **kwargs):
            return {"Messages": [{"Body": json.dumps(event), "ReceiptHandle": "handle"}]}

        def delete_message(self, **kwargs):
            assert runtime.store.artifact("ses.receipt.completed.v1", source)
            self.acks += 1

    queue = Queue()
    worker = ReceiptQueue(runtime, queue_url="test", client=queue)
    original = runtime.store.put_configuration

    def fail_after_receipt(artifact):
        if artifact.kind == "ses.receipt.completed.v1":
            raise RuntimeError("database unavailable")
        return original(artifact)

    with (
        patch.object(type(runtime.store), "put_configuration", side_effect=fail_after_receipt),
        pytest.raises(RuntimeError),
    ):
        worker.poll()
    assert queue.acks == 0 and runtime.store.artifact("ses.receipt.v1", source)
    assert worker.poll() == 1 and queue.acks == 1
    assert (
        runtime.store.artifact("ses.receipt.completed.v1", source).payload["status"]
        == "quarantined"
    )
    assert worker.poll() == 1 and not runtime.transport.sent
    runtime.close()


def test_proactive_approval_enqueues_once_without_direct_case_mutation(tmp_path):
    runtime, _, clock = opened(tmp_path)
    for record in runtime._bundle.history:
        runtime.store.write(record)
    runtime.tick()
    candidates = [
        s for s in runtime.store.list_suggestions() if s.status == SuggestionStatus.SUGGESTED
    ]
    assert candidates
    suggestion = candidates[0]
    before = len(runtime.store.list_cases())
    client = TestClient(
        create_app(runtime, tokens={"t": {"actor_id": "Simon O.", "role": "manager"}})
    )
    url = f"/api/suggestions/{suggestion.suggestion_id}/decision"
    headers = {"Authorization": "Bearer t", "Idempotency-Key": "proactive-approve"}
    response = client.post(
        url, headers=headers, json={"accepted": True, "notes": "Inspect this asset before failure."}
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"]
    assert len(runtime.store.list_cases()) == before
    assert any(i.source == "proactive_approval" for i in runtime.store.pending_inbound())
    replay = client.post(
        url, headers=headers, json={"accepted": True, "notes": "Inspect this asset before failure."}
    )
    assert replay.json()["applied"] is False
    assert (
        client.post(url, headers=headers, json={"accepted": False, "notes": "Changed"}).status_code
        == 409
    )
    runtime.close()


def test_uncertain_order_requires_provider_evidence_and_is_never_resent(tmp_path):
    from test_quote_decisions import (
        COASTLINE_BODY,
        MERIDIAN_BODY,
        _prepare_runtime,
        _record_reply,
        _SelectingRecommender,
    )

    from steward.domain.enums import CaseStatus
    from steward.mail import MailDeliveryError, RecordingMailTransport

    runtime, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    _record_reply(runtime, clock, case, "meridian-lift", MERIDIAN_BODY)
    _record_reply(runtime, clock, case, "coastline-elevator", COASTLINE_BODY)
    clock.advance(timedelta(hours=24))
    with patch.object(
        RecordingMailTransport,
        "send",
        side_effect=MailDeliveryError("response_lost", retryable=False, ambiguous=True),
    ) as sender:
        runtime.tick()
        assert sender.call_count == 1
        runtime.tick()
        assert sender.call_count == 1
    item = next(
        i
        for i in runtime.store.outbox_for_case(case.case_id)
        if i.payload.get("purpose") == "commitment"
    )
    assert item.status.value == "ambiguous"
    assert runtime.store.reservations()[0].status == "reserved"
    client = TestClient(
        create_app(runtime, tokens={"t": {"actor_id": "Simon O.", "role": "manager"}})
    )
    url = f"/api/cases/{case.case_id}/commands"
    headers = {"Authorization": "Bearer t", "Idempotency-Key": "receipt-confirmed"}
    command = {
        "action": "reconcile_order_delivery",
        "expected_version": runtime.store.case_version(case.case_id),
        "notes": "Provider console shows delivered; supplier confirmed receipt.",
        "data": {"outbox_id": item.outbox_id, "provider_message_id": ""},
    }
    assert client.post(url, headers=headers, json=command).status_code == 422
    command["data"]["provider_message_id"] = "provider-receipt-1"
    response = client.post(url, headers=headers, json=command)
    assert response.status_code == 200, response.text
    assert runtime.store.get_case(case.case_id).status == CaseStatus.AWAITING_APPOINTMENT
    assert runtime.store.reservations()[0].status == "committed"
    runtime.tick()
    assert not [m for m in runtime.transport.sent if m.subject.startswith("Service order:")]
    assert client.post(url, headers=headers, json=command).json() == {"applied": False}
    runtime.close()


@pytest.mark.parametrize("already_sent", [False, True])
def test_late_quote_revises_only_an_order_that_never_started_dispatch(tmp_path, already_sent):
    from test_quote_decisions import (
        MERIDIAN_BODY,
        PINNACLE_BODY,
        _prepare_runtime,
        _record_reply,
        _SelectingRecommender,
    )

    recommender = _SelectingRecommender()
    runtime, clock, _, case = _prepare_runtime(tmp_path, recommender)
    _record_reply(runtime, clock, case, "meridian-lift", MERIDIAN_BODY)
    decision = runtime.decide_quotes(case.case_id).decision
    runtime._maintenance.commit(decision)
    if already_sent:
        runtime.tick()
    before = runtime.store.get_case(case.case_id).accepted_quote_id
    clock.advance(timedelta(hours=24))
    _record_reply(runtime, clock, case, "pinnacle-vertical", PINNACLE_BODY)
    recommender.vendor_id = "pinnacle-vertical"
    report = runtime.tick()
    assert not report.workflow_failures
    orders = [m for m in runtime.transport.sent if m.subject.startswith("Service order:")]
    assert len(orders) == 1
    if already_sent:
        assert runtime.store.get_case(case.case_id).accepted_quote_id == before
        assert any(t.kind == "late_quote" for t in runtime.store.human_tasks(case.case_id))
        assert len(recommender.calls) == 1
    else:
        assert runtime.store.get_case(case.case_id).accepted_quote_id != before
        assert sorted(r.status for r in runtime.store.reservations(case.case_id)) == [
            "committed",
            "released",
        ]
        assert len(recommender.calls) == 2
        assert (
            len(runtime.store.artifacts_for(kind="quote_portfolio.v1", case_id=case.case_id)) == 2
        )
        assert not [
            t for t in runtime.store.human_tasks(case.case_id) if t.kind == "delivery_review"
        ]
    runtime.close()
