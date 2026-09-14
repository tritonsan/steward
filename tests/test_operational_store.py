from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from steward.agents import TriageAction, TriageAssessment, TriageResult
from steward.domain.enums import (
    ActorType,
    AutonomyLevel,
    CaseStatus,
    Category,
    EventKind,
    Urgency,
    group_of,
)
from steward.domain.models import AuditEntry, Case, ResidentMessage, TimelineEvent
from steward.memory import CaseRecord
from steward.store import (
    ConcurrencyConflict,
    IdempotencyConflict,
    InboxItem,
    InboxStatus,
    OutboxItem,
    SpendEntry,
    SqliteOperationalStore,
    StoreInvariantError,
    WorkflowArtifact,
)

UTC = timezone.utc


def initial_records():
    at = datetime(2026, 9, 10, 9, tzinfo=UTC)
    message = ResidentMessage(
        message_id="tg:test:1",
        source="telegram_shadow",
        chat_id="test",
        sender_display="Resident A.",
        text="A Block elevator is shuddering.",
        sent_at=at,
        ingested_at=at,
        case_id="case-durable-001",
    )
    triage = TriageResult(
        action=TriageAction.OPEN_CASE,
        category=Category.ELEVATOR,
        urgency=Urgency.HIGH,
        confidence=0.95,
        title="A Block elevator shudders",
        asset_id="elevator-a",
        rationale="Shuddering elevator report.",
    )
    assessment = TriageAssessment(message_id=message.message_id, assessed_at=at, result=triage)
    case = Case(
        case_id="case-durable-001",
        reply_token="abcdef123456",
        title=triage.title,
        category=triage.category,
        group=group_of(triage.category),
        urgency=triage.urgency,
        asset_id=triage.asset_id,
        opened_at=at,
        updated_at=at,
        source_message_ids=[message.message_id],
        autonomy_level=AutonomyLevel.AUTONOMOUS,
        spend_cap=Decimal("1500.00"),
    )
    event = TimelineEvent(
        event_id="event-open-001",
        case_id=case.case_id,
        at=at,
        kind=EventKind.CASE_OPENED,
        actor=ActorType.AGENT,
        summary="Opened durable test case.",
        refs=[message.message_id],
    )
    audit = AuditEntry(
        audit_id="audit-open-001",
        case_id=case.case_id,
        at=at,
        action="evaluate_intake",
        autonomy_level=AutonomyLevel.AUTONOMOUS,
        policy_rule_id="INTAKE.WITHIN_POLICY",
        reason="Durable test policy.",
    )
    return at, message, assessment, case, event, audit


def open_case(store: SqliteOperationalStore):
    at, message, assessment, case, event, audit = initial_records()
    assert store.record_intake(
        message=message,
        assessment=assessment,
        case=case,
        new_case=True,
        timeline_events=(event,),
        audit_entries=(audit,),
    )
    return at, case


def test_sqlite_store_survives_restart_and_round_trips_intake(tmp_path):
    path = tmp_path / "steward.db"
    store = SqliteOperationalStore(path)
    at, message, assessment, case, _event, _audit = initial_records()

    assert store.record_intake(
        message=message,
        assessment=assessment,
        case=case,
        new_case=True,
    )
    assert (
        store.record_intake(
            message=message,
            assessment=assessment,
            case=case,
            new_case=True,
        )
        is False
    )
    assert store.case_version(case.case_id) == 1
    store.close()

    reopened = SqliteOperationalStore(path)
    assert reopened.get_message(message.message_id) == message
    assert reopened.get_assessment(message.message_id) == assessment
    assert reopened.get_case(case.case_id) == case
    assert reopened.case_for_reply_token(case.reply_token) == case
    assert reopened.list_open_cases() == (case,)
    assert reopened.case_version(case.case_id) == 1
    assert reopened.due_cases(now=at) == ()
    reopened.close()


def test_atomic_transition_is_restart_safe_idempotent_and_due_ordered(tmp_path):
    path = tmp_path / "workflow.db"
    store = SqliteOperationalStore(path)
    at, case = open_case(store)
    due_at = at + timedelta(days=1)
    updated = case.model_copy(
        update={
            "status": CaseStatus.SCHEDULED,
            "updated_at": at + timedelta(hours=1),
            "scheduled_for": due_at - timedelta(hours=2),
            "next_action_due_at": due_at,
            "accepted_quote_id": "quote-001",
        },
        deep=True,
    )
    event = TimelineEvent(
        event_id="event-scheduled-001",
        case_id=case.case_id,
        at=updated.updated_at,
        kind=EventKind.SCHEDULED,
        actor=ActorType.SYSTEM,
        summary="Scheduled from a source-backed confirmation.",
        refs=["schedule-source-001"],
    )
    outbox = OutboxItem(
        outbox_id="outbox-001",
        case_id=case.case_id,
        dedup_key=f"{case.case_id}:verification:1",
        kind="request_completion_verification",
        payload={"case_id": case.case_id},
        created_at=updated.updated_at,
    )
    artifact = WorkflowArtifact(
        artifact_id="schedule-source-001",
        case_id=case.case_id,
        kind="schedule_confirmation",
        created_at=updated.updated_at,
        source_ids=("vendor-email-001",),
        payload={"scheduled_for": updated.scheduled_for.isoformat()},
    )
    spend = SpendEntry(
        entry_id="spend-001",
        case_id=case.case_id,
        category=case.category,
        amount=Decimal("705.00"),
        currency="usd",
        committed_at=updated.updated_at,
        source_audit_id="audit-commit-001",
    )

    first = store.save_transition(
        case=updated,
        expected_version=1,
        idempotency_key="transition:schedule:001",
        timeline_events=(event,),
        outbox_items=(outbox,),
        artifacts=(artifact,),
        spend_entries=(spend,),
    )
    assert first.applied is True
    assert first.version == 2
    repeated = store.save_transition(
        case=updated,
        expected_version=1,
        idempotency_key="transition:schedule:001",
        timeline_events=(event,),
        outbox_items=(outbox,),
        artifacts=(artifact,),
        spend_entries=(spend,),
    )
    assert repeated.applied is False
    assert repeated.version == 2
    assert store.due_cases(now=due_at - timedelta(seconds=1)) == ()
    assert store.due_cases(now=due_at) == (updated,)
    assert store.pending_outbox() == (outbox,)
    assert store.artifact("schedule_confirmation", artifact.artifact_id) == artifact
    assert store.month_to_date_spend(
        Category.ELEVATOR,
        as_of=updated.updated_at + timedelta(days=1),
        currency="USD",
    ) == Decimal("705.00")
    store.close()

    reopened = SqliteOperationalStore(path)
    assert reopened.get_case(case.case_id) == updated
    assert reopened.case_version(case.case_id) == 2
    assert reopened.timeline_for(case.case_id)[-1] == event
    assert reopened.pending_outbox() == (outbox,)
    assert reopened.mark_outbox_delivered(
        outbox.outbox_id,
        delivered_at=due_at,
    )
    assert reopened.mark_outbox_delivered(outbox.outbox_id, delivered_at=due_at) is False
    assert reopened.pending_outbox() == ()
    reopened.close()


def test_stale_version_and_changed_idempotency_key_facts_fail_closed(tmp_path):
    store = SqliteOperationalStore(tmp_path / "conflict.db")
    at, case = open_case(store)
    updated = case.model_copy(update={"updated_at": at + timedelta(minutes=1)}, deep=True)
    store.save_transition(
        case=updated,
        expected_version=1,
        idempotency_key="transition:one",
    )

    with pytest.raises(ConcurrencyConflict):
        store.save_transition(
            case=updated.model_copy(update={"updated_at": at + timedelta(minutes=2)}),
            expected_version=1,
            idempotency_key="transition:stale",
        )
    with pytest.raises(IdempotencyConflict):
        store.save_transition(
            case=updated.model_copy(update={"resolution_notes": "different"}),
            expected_version=2,
            idempotency_key="transition:one",
        )
    assert store.case_version(case.case_id) == 2
    store.close()


def test_failed_side_effect_insert_rolls_back_case_and_timeline(tmp_path):
    store = SqliteOperationalStore(tmp_path / "rollback.db")
    at, case = open_case(store)
    first = case.model_copy(update={"updated_at": at + timedelta(minutes=1)}, deep=True)
    existing_outbox = OutboxItem(
        outbox_id="outbox-original",
        case_id=case.case_id,
        dedup_key="same-effect",
        kind="notification",
        created_at=first.updated_at,
    )
    store.save_transition(
        case=first,
        expected_version=1,
        idempotency_key="transition:first",
        outbox_items=(existing_outbox,),
    )
    second = first.model_copy(update={"updated_at": at + timedelta(minutes=2)}, deep=True)
    event = TimelineEvent(
        event_id="event-must-rollback",
        case_id=case.case_id,
        at=second.updated_at,
        kind=EventKind.NOTE,
        actor=ActorType.SYSTEM,
        summary="Must roll back with duplicate side effect.",
    )
    duplicate_effect = OutboxItem(
        outbox_id="outbox-other",
        case_id=case.case_id,
        dedup_key="same-effect",
        kind="notification",
        created_at=second.updated_at,
    )

    with pytest.raises(StoreInvariantError):
        store.save_transition(
            case=second,
            expected_version=2,
            idempotency_key="transition:rollback",
            timeline_events=(event,),
            outbox_items=(duplicate_effect,),
        )

    assert store.get_case(case.case_id) == first
    assert store.case_version(case.case_id) == 2
    assert event not in store.timeline_for(case.case_id)
    assert store.pending_outbox() == (existing_outbox,)
    store.close()


def test_memory_and_normalized_inbox_are_durable_and_conflict_safe(tmp_path):
    path = tmp_path / "memory-inbox.db"
    store = SqliteOperationalStore(path)
    at, _message, _assessment, case, _event, _audit = initial_records()
    record = CaseRecord(
        case_id="memory-case-001",
        title="Verified elevator repair",
        category=Category.ELEVATOR,
        asset_id="elevator-a",
        opened_at=at,
        resolved_at=at + timedelta(hours=3),
        closed_at=at + timedelta(hours=4),
        selected_vendor_id="meridian-lift",
        cost=Decimal("705.00"),
        work_performed="Corrected rail bracket alignment.",
        resolution_source_ids=["resident-confirmation-001"],
    )
    assert store.write(record)
    assert store.write(record) is False

    payload_hash = hashlib.sha256(b"normalized telegram update").hexdigest()
    inbox = InboxItem(
        source="telegram_shadow",
        external_id="9001",
        payload_hash=payload_hash,
        received_at=at,
        status=InboxStatus.PENDING,
        message=case.source_message_ids
        and ResidentMessage(
            message_id="tg:-100:99",
            source="telegram_shadow",
            chat_id="-100",
            sender_display="Resident A.",
            text="Lift is shuddering.",
            sent_at=at,
            ingested_at=at,
        ),
    )
    assert store.record_inbound(inbox)
    assert store.record_inbound(inbox) is False
    store.close()

    reopened = SqliteOperationalStore(path)
    assert reopened.get(record.case_id) == record
    assert reopened.records() == (record,)
    assert reopened.pending_inbound() == (inbox,)
    assert reopened.mark_inbound_processed(inbox.source, inbox.external_id)
    assert reopened.mark_inbound_processed(inbox.source, inbox.external_id) is False
    assert reopened.pending_inbound() == ()
    changed = inbox.model_copy(update={"payload_hash": "f" * 64})
    with pytest.raises(IdempotencyConflict):
        reopened.record_inbound(changed)
    reopened.close()
