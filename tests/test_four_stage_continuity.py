from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from test_quote_decisions import (
    MERIDIAN_BODY,
    SECRET,
    _ElevatorClassifier,
    _prepare_runtime,
    _record_reply,
    _SelectingRecommender,
    _update,
)

from steward.api import create_app
from steward.channels.private_telegram import PrivateTelegram
from steward.config import StewardSettings
from steward.domain.clock import FrozenClock, utc_now
from steward.runtime import build_runtime


def client_for(runtime):
    return TestClient(
        create_app(
            runtime,
            simulation=True,
            tokens={
                "m": {"actor_id": "Simon O.", "role": "manager"},
                "r": {"actor_id": "Daniel", "role": "resident"},
                "other": {"actor_id": "James D.", "role": "resident"},
            },
        )
    )


def command(client, rt, cid, action, *, token="m", notes="Source evidence", data=None, key=None):
    return client.post(
        f"/api/cases/{cid}/commands",
        headers={"Authorization": "Bearer " + token, "Idempotency-Key": key or action},
        json={
            "action": action,
            "notes": notes,
            "data": data or {},
            "expected_version": rt.store.case_version(cid),
        },
    )


def test_clarification_is_personal_reassessed_and_restart_safe(tmp_path):
    from datetime import datetime, timezone

    clock = FrozenClock(datetime(2026, 9, 7, 10, tzinfo=timezone.utc))
    cfg = StewardSettings(
        database_path=tmp_path / "clarify.db",
        telegram_enabled=True,
        telegram_webhook_secret=SECRET,
        telegram_allowed_chat_ids={"-1002481179934"},
    )
    rt = build_runtime(cfg, classifier=_ElevatorClassifier(0.3), clock=clock)
    rt.handle_telegram_update(_update(clock.now()), secret_header=SECRET)
    assert not rt.tick().workflow_failures
    case = rt.store.list_open_cases()[0]
    questions = rt.store.artifacts_for(kind="intake.clarification.v1", case_id=case.case_id)
    assert len(questions) == 1 and not rt.transport.sent
    c = client_for(rt)
    assert (
        c.get("/api/resident/overview", headers={"Authorization": "Bearer other"}).json()[
            "requests"
        ]
        == []
    )
    response = command(
        c,
        rt,
        case.case_id,
        "answer_clarification",
        token="other",
        data={"request_id": questions[0].artifact_id},
    )
    assert response.status_code == 403
    response = command(
        c,
        rt,
        case.case_id,
        "answer_clarification",
        notes="Manager checked: A Block elevator at floor four.",
        data={"request_id": questions[0].artifact_id},
    )
    assert response.status_code == 200, response.text
    assert not rt.transport.sent
    rt.close()
    rt = build_runtime(cfg, classifier=_ElevatorClassifier(0.99), clock=clock)
    assert not rt.tick().workflow_failures
    assert len(rt.store.list_cases()) == 1
    assert rt.store.get_case(case.case_id).contacted_vendor_ids
    assert len(rt.transport.sent) == 3
    assert rt.store.get_assessment(case.source_message_ids[0]).result.confidence == 0.3
    assert len(rt.store.artifacts_for(kind="intake.reassessment.v1", case_id=case.case_id)) == 1
    rt.close()


def test_missing_quote_fields_block_order_and_ask_only_for_missing(tmp_path):
    rt, clock, extractor, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    original = extractor.extract

    def incomplete(**kwargs):
        return original(**kwargs).model_copy(
            update={"exclusions_evidence": "", "valid_until": None, "validity_evidence": ""}
        )

    extractor.extract = incomplete
    _record_reply(rt, clock, case, "meridian-lift", MERIDIAN_BODY)
    clock.advance(timedelta(hours=24))
    assert not rt.tick().workflow_failures
    assert not rt.store.get_case(case.case_id).accepted_quote_id
    assert any(
        "exclusions" in m.body_text and "validity" in m.body_text for m in rt.transport.sent[3:]
    )
    assert not rt.store.reservations()
    rt.close()


def test_quote_rejection_is_revision_scoped(tmp_path):
    rt, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    result = _record_reply(rt, clock, case, "meridian-lift", MERIDIAN_BODY)
    c = client_for(rt)
    response = command(
        c, rt, case.case_id, "reject_quote", data={"quote_id": result.quote_artifact_id}
    )
    assert response.status_code == 200, response.text
    clock.advance(timedelta(hours=24))
    assert not rt.tick().workflow_failures
    assert not rt.store.get_case(case.case_id).accepted_quote_id
    assert rt.store.artifact("vendor_quote.v1", result.quote_artifact_id)
    rt.close()


def test_delivered_order_waits_for_sourced_appointment(tmp_path):
    from appointment_support import confirm_appointment

    rt, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    _record_reply(rt, clock, case, "meridian-lift", MERIDIAN_BODY)
    clock.advance(timedelta(hours=24))
    assert not rt.tick().workflow_failures
    assert rt.store.get_case(case.case_id).status.value == "awaiting_appointment"
    assert rt.store.get_case(case.case_id).scheduled_for is None
    confirm_appointment(rt, clock, case.case_id)
    assert len(rt.store.artifacts_for(kind="appointment.confirmed.v1", case_id=case.case_id)) == 1
    assert len(rt.store.reservations()) == 1
    rt.close()


def test_telegram_token_is_single_use_expiring_and_private(tmp_path):
    rt, clock, _, _ = _prepare_runtime(tmp_path, _SelectingRecommender())
    service = PrivateTelegram(rt)
    token = service.issue("James D.")
    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": 777, "type": "private"},
            "from": {"id": 777},
            "text": "/start " + token,
        },
    }
    assert rt.handle_telegram_update(update, secret_header=SECRET)["accepted"]
    assert rt.handle_telegram_update(update, secret_header=SECRET)["accepted"]
    update["update_id"] = 2
    with pytest.raises(ValueError, match="already"):
        rt.handle_telegram_update(update, secret_header=SECRET)
    update["message"]["text"] = "/start " + service.issue("Simon O.")
    with patch(
        "steward.channels.private_telegram.utc_now", return_value=utc_now() + timedelta(minutes=11)
    ), pytest.raises(ValueError, match="expired"):
        rt.handle_telegram_update(update, secret_header=SECRET)
    assert len(service.bindings()) == 1
    rt.close()


def test_personal_task_never_goes_to_group_and_stale_notice_is_suppressed(tmp_path):
    from steward.channels.notifications import TelegramNotices
    from steward.store.workflow import HumanTask

    rt, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    token = PrivateTelegram(rt).issue("Simon O.")
    rt.handle_telegram_update(
        {
            "update_id": 123,
            "message": {
                "chat": {"id": 777, "type": "private"},
                "from": {"id": 777},
                "text": "/start " + token,
            },
        },
        secret_header=SECRET,
    )
    task = HumanTask(
        task_id="private",
        case_id=case.case_id,
        kind="private_review",
        title="Private evidence",
        reason="Not public",
        created_at=clock.now(),
        due_at=clock.now(),
        expected_version=rt.store.case_version(case.case_id),
    )
    rt.store.add_human_task(task)
    notices = TelegramNotices(rt)
    notices.collect()
    out = [
        o for o in rt.store.outbox_for_case(case.case_id) if o.payload.get("task_id") == "private"
    ]
    assert len(out) == 1 and out[0].payload["chat_id"] == "777"
    assert notices.authorized(out[0])
    rt.store.resolve_human_task(
        "private",
        actor_id="Simon O.",
        role="manager",
        expected_version=rt.store.case_version(case.case_id),
        response={"done": True},
    )
    assert not notices.authorized(out[0])
    rt.close()


def test_ses_ack_does_not_wait_for_model(tmp_path):
    from dataclasses import replace

    from steward.mail import InboundMessage
    from steward.mail.receipt_queue import ReceiptQueue

    rt, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    vendor = next(v for v in rt._vendors if v.vendor_id == "meridian-lift")
    inbound = InboundMessage(
        message_id="<raw@vendor>",
        from_address=vendor.email,
        from_display_name=vendor.name,
        to_addresses=(f"case+{case.reply_token}@{rt._settings.management_domain}",),
        cc_addresses=(),
        delivered_to=None,
        subject="Quote",
        body_text=MERIDIAN_BODY,
        body_full_text=MERIDIAN_BODY,
        received_at=clock.now(),
    )
    from steward.mail import AddressScheme

    inbound = replace(
        inbound,
        to_addresses=(
            AddressScheme(
                management_domain=rt._settings.management_domain,
                vendor_domain=rt._settings.vendor_domains[0],
            ).case_reply_address(case.reply_token),
        ),
    )

    class Reader:
        def read(self, *args, **kwargs):
            return inbound

    rt._inbound_reader = Reader()
    event = {
        "notificationType": "Received",
        "mail": {"messageId": "raw", "timestamp": clock.now().isoformat()},
        "receipt": {k: {"status": "PASS"} for k in ("dmarcVerdict", "spamVerdict", "virusVerdict")},
    }
    with patch.object(
        type(rt), "handle_vendor_reply", side_effect=AssertionError("Model must not run")
    ):
        result = ReceiptQueue(rt, queue_url="unused", client=object()).ingest(event)
    assert result["status"] == "queued"
    assert any(j.kind == "mail.process" for j in rt.store.workflow_jobs(case.case_id))
    assert not rt.tick().workflow_failures
    assert len(rt.store.artifacts_for(kind="vendor_quote.v1", case_id=case.case_id)) == 1
    rt.close()


def test_meeting_retry_rounds_do_not_reuse_old_answers_and_stop_at_two(tmp_path):
    from datetime import datetime, timezone

    from test_meeting_lifecycle import _AgendaPlanner, _MeetingClassifier, _ResolutionPlanner

    from steward.operations.meeting_continuity import active_schedule

    clock = FrozenClock(datetime(2026, 9, 14, 10, tzinfo=timezone.utc))
    rt = build_runtime(
        StewardSettings(database_path=tmp_path / "rounds.db"),
        clock=clock,
        classifier=_MeetingClassifier(),
        resolution_planner=_ResolutionPlanner(),
        meeting_agenda_planner=_AgendaPlanner(),
    )
    c = client_for(rt)
    headers = {"Authorization": "Bearer m", "Idempotency-Key": "report"}
    assert (
        c.post(
            "/api/simulation/messages",
            headers=headers,
            json={"sender": "James D.", "text": "We need a shared parking rule."},
        ).status_code
        == 200
    )
    assert not rt.tick().workflow_failures
    cid = rt.store.list_cases()[0].case_id
    data = {
        "policy": {
            "timezone": "UTC",
            "quorum": 2,
            "eligible_participant_ids": ["Simon O.", "James D."],
        },
        "slots": [{"slot_id": "first", "starts_at": (clock.now() + timedelta(days=3)).isoformat()}],
        "retry_slots": [
            {
                "slot_id": f"retry-{i}",
                "starts_at": (clock.now() + timedelta(days=5 + i)).isoformat(),
            }
            for i in range(6)
        ],
    }
    assert command(c, rt, cid, "meeting_schedule", data=data).status_code == 200
    first = active_schedule(rt.store, cid)
    response = c.post(
        f"/api/cases/{cid}/availability",
        headers=headers,
        json={
            "expected_version": rt.store.case_version(cid),
            "schedule_id": first.artifact_id,
            "available_slot_ids": ["first"],
        },
    )
    assert response.status_code == 200, response.text
    for expected_round in (2, 3):
        clock.advance(timedelta(hours=24))
        assert not rt.tick().workflow_failures
        assert active_schedule(rt.store, cid).payload["round"] == expected_round
        offered = c.get(f"/api/cases/{cid}/meeting", headers=headers).json()
        assert offered["schedule_id"] == active_schedule(rt.store, cid).artifact_id
        assert offered["schedule"]["round"] == expected_round
        assert offered["available_slot_ids"] == []
        assert offered["responded"] is False
        selected = [offered["schedule"]["slots"][0]["slot_id"]]
        response = c.post(
            f"/api/cases/{cid}/availability",
            headers={**headers, "Idempotency-Key": f"round-{expected_round}"},
            json={
                "expected_version": offered["version"],
                "schedule_id": offered["schedule_id"],
                "available_slot_ids": selected,
            },
        )
        assert response.status_code == 200, response.text
        refreshed = c.get(f"/api/cases/{cid}/meeting", headers=headers).json()
        assert refreshed["available_slot_ids"] == selected
        assert refreshed["responded"] is True
    response = c.post(
        f"/api/cases/{cid}/availability",
        headers={**headers, "Idempotency-Key": "stale"},
        json={
            "expected_version": rt.store.case_version(cid),
            "schedule_id": first.artifact_id,
            "available_slot_ids": [],
        },
    )
    assert response.status_code == 409
    clock.advance(timedelta(hours=24))
    assert not rt.tick().workflow_failures
    assert active_schedule(rt.store, cid).payload["round"] == 3
    assert not rt.store.artifacts_for(kind="meeting_packet.v1", case_id=cid)
    assert any(t.kind == "meeting_reschedule" for t in rt.store.human_tasks(cid))
    rt.close()


def test_material_decision_revision_preserves_confirmed_decision(tmp_path):
    from test_community_product import community_to_confirmed_actions

    rt, _, cid, _, _, _, clock = community_to_confirmed_actions(tmp_path)
    c = client_for(rt)
    before = rt.store.artifacts_for(kind="meeting_decision.v1", case_id=cid)
    response = command(
        c,
        rt,
        cid,
        "meeting_revise_decision",
        notes="The parking scope needs a new community decision.",
    )
    assert response.status_code == 200, response.text
    link = rt.store.artifacts_for(kind="community.revision.v1", case_id=cid)[0]
    assert link.payload["status"] == "pending_confirmation"
    assert rt.store.artifacts_for(kind="meeting_decision.v1", case_id=cid) == before
    assert not rt.tick().workflow_failures
    child = rt.store.get_case(link.payload["child_case_id"])
    assert cid in child.related_case_ids
    assert any(t.kind == "meeting_schedule" for t in rt.store.human_tasks(child.case_id))
    rt.close()


def test_atomic_clarification_answer_rolls_back_if_job_write_fails(tmp_path):
    from steward.operations.clarifications import Clarifications

    rt, _, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    request = Clarifications(rt).request(case)
    c = client_for(rt)
    with (
        patch.object(type(rt.store), "enqueue_job", side_effect=RuntimeError("power loss")),
        pytest.raises(RuntimeError),
    ):
        command(
            c,
            rt,
            case.case_id,
            "answer_clarification",
            data={"request_id": request.artifact_id},
        )
    assert not rt.store.artifacts_for(kind="intake.answer.v1", case_id=case.case_id)
    assert (
        next(
            t for t in rt.store.human_tasks(case.case_id) if t.task_id == request.artifact_id
        ).status
        == "open"
    )
    rt.close()


def test_appointment_acceptance_delivery_recovers_without_resend(tmp_path):
    from appointment_support import confirm_appointment

    rt, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    _record_reply(rt, clock, case, "meridian-lift", MERIDIAN_BODY)
    clock.advance(timedelta(hours=24))
    rt.tick()
    original = type(rt.store).save_transition

    def fail_confirmation(store, **kwargs):
        if kwargs["idempotency_key"].startswith("confirmed:"):
            raise RuntimeError("crash after provider receipt")
        return original(store, **kwargs)

    with (
        patch.object(type(rt.store), "save_transition", fail_confirmation),
        pytest.raises(RuntimeError, match="provider receipt"),
    ):
        confirm_appointment(rt, clock, case.case_id)
    assert rt.store.get_case(case.case_id).status.value == "awaiting_appointment"
    assert len([m for m in rt.transport.sent if m.subject.startswith("Appointment:")]) == 1
    assert not rt.tick().workflow_failures
    assert rt.store.get_case(case.case_id).status.value == "scheduled"
    assert len([m for m in rt.transport.sent if m.subject.startswith("Appointment:")]) == 1
    assert len(rt.store.reservations()) == 1
    rt.close()


@pytest.mark.parametrize("fault", ["hours", "notice", "access", "price_scope", "ambiguous_date"])
def test_noncompliant_appointment_requires_management(tmp_path, fault):
    from datetime import timedelta

    from appointment_support import OrderExtractor

    rt, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    _record_reply(rt, clock, case, "meridian-lift", MERIDIAN_BODY)
    clock.advance(timedelta(hours=24))
    rt.tick()
    c = client_for(rt)
    headers = {"Authorization": "Bearer m", "Idempotency-Key": "rules"}
    assert (
        c.put(
            "/api/appointment-rules",
            headers=headers,
            json={
                "action": "configure",
                "expected_version": 1,
                "data": {
                    "weekdays": list(range(7)),
                    "start_hour": 9,
                    "end_hour": 17,
                    "minimum_notice_hours": 24,
                    "access_instructions": "Use management entrance.",
                },
            },
        ).status_code
        == 200
    )

    class Extractor(OrderExtractor):
        def extract(self, **kwargs):
            value = super().extract(**kwargs)
            changes = {
                "access": {"access_evidence": ""},
                "price_scope": {"unchanged_terms_evidence": ""},
                "ambiguous_date": {"starts_at": None, "ends_at": None},
            }.get(fault, {})
            return value.model_copy(update=changes)

    rt._order_reply_extractor = Extractor()
    start = (clock.now() + timedelta(days=2)).replace(hour=20 if fault == "hours" else 10)
    if fault == "notice":
        start = clock.now() + timedelta(hours=1)
    text = (
        f"Appointment {start.isoformat()} {(start + timedelta(hours=1)).isoformat()}\n"
        "Same price and scope. Access arrangements agreed."
    )
    result = c.post(
        "/api/simulation/vendor-replies",
        headers=headers,
        json={"case_id": case.case_id, "vendor_id": "meridian-lift", "text": text},
    )
    assert result.status_code == 200, result.text
    assert not rt.tick().workflow_failures
    assert rt.store.get_case(case.case_id).status.value == "awaiting_appointment"
    assert any(t.kind == "appointment_review" for t in rt.store.human_tasks(case.case_id))
    assert not any(m.subject.startswith("Appointment:") for m in rt.transport.sent)
    rt.close()


@pytest.mark.parametrize("body_text", ["See attachment", ""])
def test_trusted_unreadable_attachment_can_request_replacement_but_cannot_bypass(
    tmp_path, body_text
):
    from steward.mail import AddressScheme, InboundMessage
    from steward.mail.receipt_queue import ReceiptQueue
    from steward.store.workflow import stable_id

    rt, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    vendor = next(v for v in rt._vendors if v.vendor_id == "meridian-lift")
    address = AddressScheme(
        management_domain=rt._settings.management_domain,
        vendor_domain=rt._settings.vendor_domains[0],
    ).case_reply_address(case.reply_token)
    mail = InboundMessage(
        message_id="<unreadable>",
        from_address=vendor.email,
        from_display_name=vendor.name,
        to_addresses=(address,),
        cc_addresses=(),
        delivered_to=None,
        subject="Quote",
        body_text=body_text,
        body_full_text=body_text,
        received_at=clock.now(),
        unreadable_attachments=("scan.pdf",),
    )

    class Reader:
        def read(self, *args, **kwargs):
            return mail

    rt._inbound_reader = Reader()
    event = {
        "notificationType": "Received",
        "mail": {"messageId": "unreadable", "timestamp": clock.now().isoformat()},
        "receipt": {k: {"status": "PASS"} for k in ("dmarcVerdict", "spamVerdict", "virusVerdict")},
    }
    result = ReceiptQueue(rt, queue_url="unused", client=object()).ingest(event)
    assert result["reason"] == "unreadable_attachment"
    assert result["case_id"] == case.case_id
    assert not any(j.kind == "mail.process" for j in rt.store.workflow_jobs(case.case_id))
    c = client_for(rt)
    url = f"/api/inbound-reviews/{stable_id('ses-receipt', 'unreadable')}/commands"
    headers = {"Authorization": "Bearer m", "Idempotency-Key": "review"}
    response = c.post(
        url,
        headers=headers,
        json={"action": "reprocess", "expected_version": 1, "notes": "Please process"},
    )
    assert response.status_code == 422
    response = c.post(
        url,
        headers=headers,
        json={
            "action": "request_replacement",
            "expected_version": 1,
            "notes": "Please send a text PDF; scan.pdf was unreadable.",
        },
    )
    assert response.status_code == 200, response.text
    assert not rt.tick().workflow_failures
    assert any("text PDF" in m.body_text for m in rt.transport.sent)
    rt.close()


def test_unknown_asset_is_clarified_instead_of_guessed(tmp_path):
    from datetime import datetime, timezone

    clock = FrozenClock(datetime(2026, 9, 7, 10, tzinfo=timezone.utc))

    class Classifier(_ElevatorClassifier):
        def classify(self, **kwargs):
            return (
                super()
                .classify(**kwargs)
                .model_copy(
                    update={
                        "asset_id": None,
                        "missing_information": ("asset",),
                        "clarification_question": "Which block has the noisy lift?",
                    }
                )
            )

    rt = build_runtime(
        StewardSettings(
            database_path=tmp_path / "unknown.db",
            telegram_enabled=True,
            telegram_webhook_secret=SECRET,
            telegram_allowed_chat_ids={"-1002481179934"},
        ),
        classifier=Classifier(),
        clock=clock,
    )
    rt.handle_telegram_update(_update(clock.now()), secret_header=SECRET)
    report = rt.tick()
    assert not report.workflow_failures and not report.inbound.failures
    case = rt.store.list_cases()[0]
    assert case.asset_id is None and not rt.transport.sent
    assert rt.store.human_tasks(case.case_id)[0].title == "Which block has the noisy lift?"
    rt.close()


def test_older_appointment_receipt_cannot_replace_current_date(tmp_path):
    from appointment_support import confirm_appointment

    from steward.operations.appointments import Appointments
    from steward.store.workflow import stable_id

    rt, clock, _, case = _prepare_runtime(tmp_path, _SelectingRecommender())
    _record_reply(rt, clock, case, "meridian-lift", MERIDIAN_BODY)
    clock.advance(timedelta(hours=24))
    rt.tick()
    confirm_appointment(rt, clock, case.case_id)
    current = rt.store.get_case(case.case_id)
    proposal = rt.store.artifacts_for(kind="appointment.proposal.v1", case_id=case.case_id)[0]
    older = proposal.model_copy(
        update={"artifact_id": "older-proposal", "payload": {**proposal.payload, "revision": 0}}
    )
    item = rt.store.outbox_item(stable_id("appointment-accept", proposal.artifact_id))
    receipt = item.model_copy(
        update={
            "outbox_id": stable_id("appointment-accept", older.artifact_id),
            "dedup_key": stable_id("appointment-accept", older.artifact_id),
        }
    )
    rt.store.save_transition(
        case=current,
        expected_version=rt.store.case_version(case.case_id),
        idempotency_key="late-old-receipt",
        artifacts=(older,),
        outbox_items=(receipt,),
    )
    Appointments(rt).reconcile()
    assert rt.store.get_case(case.case_id).scheduled_for == current.scheduled_for
    assert any("older" in t.title for t in rt.store.human_tasks(case.case_id))
    assert len([m for m in rt.transport.sent if m.subject.startswith("Appointment:")]) == 1
    rt.close()


def test_assignment_revisions_keep_previous_owner_and_follow_new_due(tmp_path):
    from test_community_product import community_to_confirmed_actions

    rt, _, cid, _, _, _, clock = community_to_confirmed_actions(tmp_path)
    c = client_for(rt)
    decision = rt.store.artifacts_for(kind="meeting_decision.v1", case_id=cid)[0]
    action = decision.payload["action_items"][0]
    due = clock.now() + timedelta(days=4)
    response = command(
        c,
        rt,
        cid,
        "meeting_reassign",
        key="assign-1",
        data={
            "action_id": action["action_id"],
            "owner_participant_id": "James D.",
            "due_at": due.isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    response = command(
        c,
        rt,
        cid,
        "meeting_reassign",
        key="assign-2",
        data={
            "action_id": action["action_id"],
            "due_at": (due + timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    revisions = rt.store.artifacts_for(kind="meeting.assignment.v1", case_id=cid)
    assert [a.payload["version"] for a in revisions] == [1, 2]
    assert revisions[-1].payload["owner_participant_id"] == "James D."
    assert revisions[-1].payload["previous_assignment_id"] == revisions[0].artifact_id
    clock.advance(timedelta(days=4))
    assert not rt.tick().workflow_failures
    assert not any(t.kind == "meeting_assignment_overdue" for t in rt.store.human_tasks(cid))
    clock.advance(timedelta(days=1))
    assert not rt.tick().workflow_failures
    assert sum(t.kind == "meeting_assignment_overdue" for t in rt.store.human_tasks(cid)) == 1
    assert rt.store.artifacts_for(kind="meeting_decision.v1", case_id=cid)[0] == decision
    rt.close()
