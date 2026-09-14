from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from steward.agents import IntakeDisposition, IntakeOutcome, TriageAction, TriageResult
from steward.domain.clock import FrozenClock
from steward.domain.enums import Category, Urgency
from steward.domain.models import ResidentMessage
from steward.operations import OperationalRunner
from steward.store import (
    FailureDisposition,
    InboxItem,
    InboxStatus,
    InferenceClaimDisposition,
    OutboxItem,
    OutboxStatus,
    SqliteOperationalStore,
    StoreInvariantError,
)

UTC = timezone.utc


def _message(external_id: str, text: str, at: datetime) -> ResidentMessage:
    return ResidentMessage(
        message_id=f"tg:test:{external_id}",
        source="telegram_shadow",
        chat_id="test",
        sender_display="Resident A.",
        text=text,
        sent_at=at,
        ingested_at=at,
    )


def _item(external_id: str, text: str, at: datetime) -> InboxItem:
    message = _message(external_id, text, at)
    return InboxItem(
        source="telegram_shadow",
        external_id=external_id,
        payload_hash=hashlib.sha256(message.model_dump_json().encode()).hexdigest(),
        received_at=at,
        status=InboxStatus.PENDING,
        message=message,
    )


class _SelectiveIntake:
    def process(self, message: ResidentMessage) -> IntakeOutcome:
        if message.text == "permanent poison":
            raise ValueError("sensitive resident prose must never be persisted as an error")
        if message.text == "transient poison":
            raise RuntimeError("temporary model failure with sensitive details")
        triage = TriageResult(
            action=TriageAction.IGNORE,
            category=Category.OTHER,
            urgency=Urgency.LOW,
            confidence=0.99,
            rationale="No operational issue is present.",
        )
        return IntakeOutcome(
            disposition=IntakeDisposition.IGNORED,
            message=message,
            triage=triage,
        )


def test_poison_inbound_is_isolated_and_does_not_stop_remaining_batch(tmp_path):
    at = datetime(2026, 9, 7, 10, tzinfo=UTC)
    clock = FrozenClock(at)
    store = SqliteOperationalStore(tmp_path / "poison-isolation.db")
    assert store.record_inbound(_item("1", "permanent poison", at))
    assert store.record_inbound(_item("2", "ordinary chatter", at + timedelta(seconds=1)))
    runner = OperationalRunner(
        store=store,
        intake=_SelectiveIntake(),  # type: ignore[arg-type]
        clock=clock,
        max_attempts=3,
    )

    report = runner.tick()

    assert report.processed_external_ids == ("2",)
    assert len(report.failures) == 1
    assert report.failures[0].external_id == "1"
    assert report.failures[0].error_code == "value_error"
    assert report.failures[0].disposition is FailureDisposition.DEAD_LETTERED
    dead = store.dead_letter_inbound()
    assert len(dead) == 1
    assert dead[0].status is InboxStatus.DEAD_LETTER
    assert dead[0].last_error_code == "value_error"
    assert "sensitive" not in json.dumps(dead[0].model_dump(mode="json"))
    assert store.inbox_item("telegram_shadow", "2").status is InboxStatus.PROCESSED
    store.close()


def test_retryable_inbound_reaches_dead_letter_at_configured_attempt_limit(tmp_path):
    at = datetime(2026, 9, 7, 10, tzinfo=UTC)
    clock = FrozenClock(at)
    store = SqliteOperationalStore(tmp_path / "bounded-retry.db")
    assert store.record_inbound(_item("1", "transient poison", at))
    runner = OperationalRunner(
        store=store,
        intake=_SelectiveIntake(),  # type: ignore[arg-type]
        clock=clock,
        max_attempts=2,
    )

    first = runner.tick()
    assert first.failures[0].disposition is FailureDisposition.RETRY_PENDING
    pending = store.inbox_item("telegram_shadow", "1")
    assert pending is not None
    assert pending.status is InboxStatus.PENDING
    assert pending.attempts == 1

    second = runner.tick()
    assert second.failures[0].disposition is FailureDisposition.DEAD_LETTERED
    dead = store.inbox_item("telegram_shadow", "1")
    assert dead is not None
    assert dead.status is InboxStatus.DEAD_LETTER
    assert dead.attempts == 2
    assert runner.tick().failures == ()
    store.close()


def test_expired_dispatching_lease_becomes_ambiguous_instead_of_resending(tmp_path):
    at = datetime(2026, 9, 7, 10, tzinfo=UTC)
    store = SqliteOperationalStore(tmp_path / "expired-dispatch.db")
    # Use a minimal case-less row because this test exercises only store leases.
    item = OutboxItem(
        outbox_id="outbox-expired-dispatch",
        dedup_key="expired-dispatch:1",
        kind="mail.send.v1",
        created_at=at,
    )
    # Outbox rows normally enter through save_transition; seed this focused
    # lifecycle test through a tiny v1-compatible database transition helper.
    with store._transaction() as conn:  # noqa: SLF001 - store invariant test
        store._insert_outbox(conn, (item,))  # noqa: SLF001

    claimed = store.claim_outbox(
        token="worker-a",
        now=at,
        lease_seconds=60,
        max_attempts=3,
        kind="mail.send.v1",
    )
    assert claimed[0].attempts == 1
    with pytest.raises(StoreInvariantError, match="token-guarded completion"):
        store.mark_outbox_delivered(item.outbox_id, delivered_at=at)
    assert store.begin_outbox_delivery(
        outbox_id=item.outbox_id,
        token="worker-a",
        started_at=at,
    )

    assert store.claim_outbox(
        token="worker-b",
        now=at + timedelta(seconds=61),
        lease_seconds=60,
        max_attempts=3,
        kind="mail.send.v1",
    ) == ()
    ambiguous = store.outbox_item(item.outbox_id)
    assert ambiguous is not None
    assert ambiguous.status is OutboxStatus.AMBIGUOUS
    assert ambiguous.last_error_code == "delivery_outcome_unknown"
    assert ambiguous.attempts == 1
    with pytest.raises(StoreInvariantError, match="only an unclaimed pending item"):
        store.mark_outbox_delivered(
            item.outbox_id,
            delivered_at=at + timedelta(seconds=62),
        )
    store.close()


def test_schema_v1_is_additively_migrated_with_existing_rows(tmp_path):
    path = tmp_path / "schema-v1.db"
    at = datetime(2026, 9, 7, 10, tzinfo=UTC)
    old_item = OutboxItem(
        outbox_id="outbox-old",
        dedup_key="old:1",
        kind="notification",
        created_at=at,
    )
    old_doc = old_item.model_dump(mode="json")
    for field in (
        "status",
        "attempts",
        "last_error_code",
        "failed_at",
        "delivery_started_at",
        "provider_message_id",
    ):
        old_doc.pop(field, None)

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1');
        CREATE TABLE outbox (
            outbox_id TEXT PRIMARY KEY, case_id TEXT, dedup_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT, doc_json TEXT NOT NULL
        );
        CREATE TABLE inbound_inbox (
            source TEXT NOT NULL, external_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
            received_at TEXT NOT NULL, status TEXT NOT NULL, doc_json TEXT NOT NULL,
            PRIMARY KEY(source, external_id)
        );
        """
    )
    connection.execute(
        "INSERT INTO outbox(outbox_id, case_id, dedup_key, kind, created_at, delivered_at, "
        "doc_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            old_item.outbox_id,
            None,
            old_item.dedup_key,
            old_item.kind,
            at.isoformat(),
            None,
            json.dumps(old_doc),
        ),
    )
    connection.commit()
    connection.close()

    store = SqliteOperationalStore(path)
    migrated = store.outbox_item(old_item.outbox_id)
    assert migrated is not None
    assert migrated.status is OutboxStatus.PENDING
    assert migrated.attempts == 0
    store.close()

    verification = sqlite3.connect(path)
    version = verification.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    columns = {
        row[1] for row in verification.execute("PRAGMA table_info(outbox)").fetchall()
    }
    tables = {
        row[0]
        for row in verification.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    verification.close()
    assert version == "6"
    assert {"workflow_jobs", "human_tasks", "budget_reservations"} <= tables
    assert "inference_jobs" in tables
    assert {"status", "attempts", "delivery_started_at", "provider_message_id"} <= columns



def test_schema_v3_migration_supports_inference_claims(tmp_path):
    path = tmp_path / "schema-v3.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta(key, value) VALUES ('schema_version', '3');
        """
    )
    connection.commit()
    connection.close()

    store = SqliteOperationalStore(path)
    claimed = store.claim_inference_job(
        job_id="inference-job-after-v3-migration",
        kind="meeting_minutes_extraction.v1",
        case_id="case-after-v3-migration",
        input_sha256="f" * 64,
        token="migration-test-worker",
        now=datetime(2026, 9, 8, 12, tzinfo=UTC),
    )
    assert claimed.disposition is InferenceClaimDisposition.CLAIMED
    assert claimed.lease is not None
    store.close()

    verification = sqlite3.connect(path)
    version = verification.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    has_inference_table = verification.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inference_jobs'"
    ).fetchone()
    verification.close()
    assert version == "6"
    assert has_inference_table == (1,)
