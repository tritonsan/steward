"""SQLite-backed restart-safe operational store.

The adapter uses only Python's standard-library ``sqlite3`` module.  Every
workflow transition is committed with its timeline, audit, outbox, artifacts,
memory records, and spend entries in one ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from steward.agents.triage import TriageAssessment
from steward.domain.enums import TERMINAL_STATUSES, Category
from steward.domain.models import AuditEntry, Case, ResidentMessage, TimelineEvent
from steward.memory.archive import MemoryConflictError
from steward.memory.records import CaseRecord
from steward.memory.retrieval import MemoryRecall, StructuredMemoryRetriever
from steward.proactive import MaintenanceSuggestion, SuggestionStatus
from steward.store.contracts import (
    ClaimLease,
    ConcurrencyConflict,
    FailureDisposition,
    IdempotencyConflict,
    InboxItem,
    InboxStatus,
    InferenceClaimDisposition,
    InferenceClaimResult,
    InferenceJob,
    InferenceJobStatus,
    OutboxItem,
    OutboxStatus,
    SpendEntry,
    StaleLeaseToken,
    TransitionResult,
    WorkflowArtifact,
)
from steward.store.memory import InMemoryCaseStore, StoreInvariantError
from steward.store.workflow import WORKFLOW_SCHEMA, WorkflowJob, WorkflowStoreMixin, stable_id

__all__ = ["SqliteOperationalStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    reply_token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    next_action_due_at TEXT,
    version INTEGER NOT NULL,
    doc_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_due
    ON cases(status, next_action_due_at);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    case_id TEXT,
    doc_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assessments (
    message_id TEXT PRIMARY KEY,
    doc_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timeline (
    event_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    at TEXT NOT NULL,
    doc_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timeline_case_at
    ON timeline(case_id, at, event_id);
CREATE TABLE IF NOT EXISTS audits (
    audit_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    at TEXT NOT NULL,
    doc_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audits_case_at
    ON audits(case_id, at, audit_id);

CREATE TABLE IF NOT EXISTS idempotency (
    idempotency_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    case_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id TEXT PRIMARY KEY,
    case_id TEXT,
    dedup_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    failed_at TEXT,
    delivery_started_at TEXT,
    delivered_at TEXT,
    provider_message_id TEXT,
    doc_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox(delivered_at, created_at, outbox_id);
CREATE TABLE IF NOT EXISTS artifacts (
    kind TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    case_id TEXT,
    created_at TEXT NOT NULL,
    doc_json TEXT NOT NULL,
    PRIMARY KEY(kind, artifact_id)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_case_kind
    ON artifacts(case_id, kind, created_at, artifact_id);
CREATE TABLE IF NOT EXISTS memory_records (
    case_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    asset_id TEXT,
    closed_at TEXT NOT NULL,
    doc_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_category_asset_closed
    ON memory_records(category, asset_id, closed_at);
CREATE TABLE IF NOT EXISTS spend_ledger (
    entry_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    category TEXT NOT NULL,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    source_audit_id TEXT NOT NULL,
    doc_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spend_category_currency_at
    ON spend_ledger(category, currency, committed_at);
CREATE TABLE IF NOT EXISTS inbound_inbox (
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    failed_at TEXT,
    doc_json TEXT NOT NULL,
    PRIMARY KEY(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_inbound_pending
    ON inbound_inbox(status, received_at, source, external_id);
CREATE TABLE IF NOT EXISTS claim_inbound (
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    token TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    PRIMARY KEY(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_claim_inbound_lease
    ON claim_inbound(lease_expires_at, source, external_id);
CREATE TABLE IF NOT EXISTS claim_outbox (
    outbox_id TEXT NOT NULL,
    token TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    PRIMARY KEY(outbox_id)
);
CREATE INDEX IF NOT EXISTS idx_claim_outbox_lease
    ON claim_outbox(lease_expires_at, outbox_id);
CREATE TABLE IF NOT EXISTS inference_jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    case_id TEXT,
    input_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    failed_at TEXT,
    last_error_code TEXT,
    result_json TEXT,
    lease_token TEXT,
    claimed_at TEXT,
    lease_expires_at TEXT,
    doc_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inference_jobs_status_lease
    ON inference_jobs(status, lease_expires_at, created_at, job_id);
CREATE TABLE IF NOT EXISTS proactive_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    due_at TEXT NOT NULL,
    suggested_at TEXT NOT NULL,
    doc_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suggestions_status_due
    ON proactive_suggestions(status, due_at, suggestion_id);
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '4');
"""


class SqliteOperationalStore(WorkflowStoreMixin):
    """One-file durable store implementing CaseStore and MemoryArchive contracts."""

    __slots__ = (
        "_conn",
        "_lock",
        "_max_case_hits",
        "_path",
        "_recurrence_window_days",
        "_same_fault_threshold",
    )

    def __init__(
        self,
        path: str | Path,
        *,
        recurrence_window_days: int = 90,
        same_fault_threshold: float = 0.35,
        max_case_hits: int = 10,
    ) -> None:
        if recurrence_window_days <= 0:
            raise ValueError("recurrence_window_days must be positive")
        if not 0.0 <= same_fault_threshold <= 1.0:
            raise ValueError("same_fault_threshold must be between 0 and 1")
        if max_case_hits <= 0:
            raise ValueError("max_case_hits must be positive")
        self._path = str(path)
        self._recurrence_window_days = recurrence_window_days
        self._same_fault_threshold = same_fault_threshold
        self._max_case_hits = max_case_hits
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self._path,
            isolation_level=None,
            check_same_thread=False,
            timeout=10.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 10000")
        if self._path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.executescript(_SCHEMA)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Apply ordered additive migrations and reject unknown future schemas."""
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            raise StoreInvariantError("database has no schema version")
        version = int(row["value"])
        if version > 6:
            raise StoreInvariantError(
                f"database schema version {version} is newer than supported version 6"
            )
        if version == 1:
            with self._transaction() as conn:
                for statement in (
                    "ALTER TABLE inbound_inbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE inbound_inbox ADD COLUMN last_error_code TEXT",
                    "ALTER TABLE inbound_inbox ADD COLUMN failed_at TEXT",
                    "ALTER TABLE outbox ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'",
                    "ALTER TABLE outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE outbox ADD COLUMN last_error_code TEXT",
                    "ALTER TABLE outbox ADD COLUMN failed_at TEXT",
                    "ALTER TABLE outbox ADD COLUMN provider_message_id TEXT",
                ):
                    conn.execute(statement)
                rows = conn.execute(
                    "SELECT outbox_id, delivered_at, doc_json FROM outbox"
                ).fetchall()
                for outbox_row in rows:
                    item = OutboxItem.model_validate_json(outbox_row["doc_json"])
                    status = (
                        OutboxStatus.DELIVERED
                        if outbox_row["delivered_at"] is not None
                        else OutboxStatus.PENDING
                    )
                    updated = item.model_copy(update={"status": status})
                    conn.execute(
                        "UPDATE outbox SET status = ?, doc_json = ? WHERE outbox_id = ?",
                        (status.value, updated.model_dump_json(), outbox_row["outbox_id"]),
                    )
                conn.execute("UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'")
            version = 2
        if version == 2:
            with self._transaction() as conn:
                conn.execute("ALTER TABLE outbox ADD COLUMN delivery_started_at TEXT")
                conn.execute("UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'")
            version = 3
        if version == 3:
            with self._transaction() as conn:
                conn.execute("UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'")
            version = 4
        if version == 4:
            with self._transaction() as conn:
                for statement in WORKFLOW_SCHEMA:
                    conn.execute(statement)
                # Existing open cases get a recoverable intake continuation too.
                for row in conn.execute("SELECT doc_json, version FROM cases").fetchall():
                    case = Case.model_validate_json(row["doc_json"])
                    if case.status not in TERMINAL_STATUSES and case.source_message_ids:
                        self._insert_job(conn, self._intake_job(case, int(row["version"])))
                conn.execute("UPDATE schema_meta SET value='5' WHERE key='schema_version'")
        if version <= 5:
            from steward.domain.enums import CaseStatus
            from steward.store.workflow import HumanTask

            with self._transaction() as conn:
                # JSON contracts remain additive; recover old optimistic scheduling explicitly.
                for row in conn.execute(
                    "SELECT doc_json, version FROM cases WHERE status='scheduled'"
                ).fetchall():
                    case = Case.model_validate_json(row["doc_json"])
                    if not self.artifacts_for(
                        kind="appointment.confirmed.v1", case_id=case.case_id
                    ):
                        updated = case.model_copy(
                            update={
                                "status": CaseStatus.AWAITING_APPOINTMENT,
                                "scheduled_for": None,
                            }
                        )
                        self._upsert_case(
                            conn, updated, version=int(row["version"]) + 1, insert=False
                        )
                        self._insert_task(
                            conn,
                            HumanTask(
                                task_id=stable_id("legacy-appointment", case.case_id),
                                case_id=case.case_id,
                                kind="appointment_review",
                                title="Confirm the recovered appointment",
                                reason="Legacy scheduling has no vendor appointment evidence.",
                                created_at=case.updated_at,
                                due_at=case.updated_at,
                                expected_version=int(row["version"]) + 1,
                            ),
                        )
                conn.execute("UPDATE schema_meta SET value='6' WHERE key='schema_version'")
        with self._lock:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_status_created "
                "ON outbox(status, created_at, outbox_id)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._conn.in_transaction:
                # Reentrant application transactions compose budget + case + outbox.
                from uuid import uuid4

                name = "nested_" + uuid4().hex
                self._conn.execute(f"SAVEPOINT {name}")
                try:
                    yield self._conn
                except Exception:
                    self._conn.execute(f"ROLLBACK TO {name}")
                    self._conn.execute(f"RELEASE {name}")
                    raise
                else:
                    self._conn.execute(f"RELEASE {name}")
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def atomic(self):
        """Compose pure validation and database writes; never enclose network I/O."""
        return self._transaction()

    # -- CaseStore reads -------------------------------------------------

    def get_message(self, message_id: str) -> ResidentMessage | None:
        row = self._one("SELECT doc_json FROM messages WHERE message_id = ?", (message_id,))
        return ResidentMessage.model_validate_json(row["doc_json"]) if row else None

    def get_assessment(self, message_id: str) -> TriageAssessment | None:
        row = self._one(
            "SELECT doc_json FROM assessments WHERE message_id = ?",
            (message_id,),
        )
        return TriageAssessment.model_validate_json(row["doc_json"]) if row else None

    def get_case(self, case_id: str) -> Case | None:
        row = self._one("SELECT doc_json FROM cases WHERE case_id = ?", (case_id,))
        return Case.model_validate_json(row["doc_json"]) if row else None

    def case_for_reply_token(self, reply_token: str) -> Case | None:
        row = self._one("SELECT doc_json FROM cases WHERE reply_token = ?", (reply_token,))
        return Case.model_validate_json(row["doc_json"]) if row else None

    def list_cases(self) -> tuple[Case, ...]:
        rows = self._all("SELECT doc_json FROM cases ORDER BY opened_at, case_id")
        return tuple(Case.model_validate_json(row["doc_json"]) for row in rows)

    def list_open_cases(self) -> tuple[Case, ...]:
        terminal = tuple(status.value for status in TERMINAL_STATUSES)
        rows = self._all(
            "SELECT doc_json FROM cases WHERE status NOT IN (?, ?) ORDER BY opened_at, case_id",
            terminal,
        )
        return tuple(Case.model_validate_json(row["doc_json"]) for row in rows)

    def timeline_for(self, case_id: str) -> tuple[TimelineEvent, ...]:
        rows = self._all(
            "SELECT doc_json FROM timeline WHERE case_id = ? ORDER BY at, rowid",
            (case_id,),
        )
        return tuple(TimelineEvent.model_validate_json(row["doc_json"]) for row in rows)

    def audit_for(self, case_id: str) -> tuple[AuditEntry, ...]:
        rows = self._all(
            "SELECT doc_json FROM audits WHERE case_id = ? ORDER BY at, audit_id",
            (case_id,),
        )
        return tuple(AuditEntry.model_validate_json(row["doc_json"]) for row in rows)

    def case_version(self, case_id: str) -> int | None:
        row = self._one("SELECT version FROM cases WHERE case_id = ?", (case_id,))
        return int(row["version"]) if row else None

    def due_cases(self, *, now: datetime, limit: int = 100) -> tuple[Case, ...]:
        if limit <= 0:
            raise ValueError("due-case limit must be positive")
        now_text = _aware_iso(now)
        terminal = tuple(status.value for status in TERMINAL_STATUSES)
        rows = self._all(
            "SELECT doc_json FROM cases "
            "WHERE status NOT IN (?, ?) AND next_action_due_at IS NOT NULL "
            "AND next_action_due_at <= ? "
            "ORDER BY next_action_due_at, case_id LIMIT ?",
            (*terminal, now_text, limit),
        )
        return tuple(Case.model_validate_json(row["doc_json"]) for row in rows)

    # -- Atomic writes ---------------------------------------------------

    @staticmethod
    def _intake_job(case: Case, version: int) -> WorkflowJob:
        return WorkflowJob(
            job_id=stable_id("case.opened", case.case_id),
            case_id=case.case_id,
            kind="case.opened",
            source_id=case.source_message_ids[0],
            due_at=case.opened_at,
            created_at=case.opened_at,
            expected_version=version,
        )

    def record_intake(
        self,
        *,
        message: ResidentMessage,
        assessment: TriageAssessment,
        case: Case | None = None,
        new_case: bool = False,
        timeline_events: Sequence[TimelineEvent] = (),
        audit_entries: Sequence[AuditEntry] = (),
    ) -> bool:
        InMemoryCaseStore._validate_write(
            message=message,
            assessment=assessment,
            case=case,
            new_case=new_case,
            timeline_events=timeline_events,
            audit_entries=audit_entries,
        )
        try:
            with self._transaction() as conn:
                if conn.execute(
                    "SELECT 1 FROM messages WHERE message_id = ?",
                    (message.message_id,),
                ).fetchone():
                    return False
                if case is not None:
                    existing = conn.execute(
                        "SELECT version FROM cases WHERE case_id = ?",
                        (case.case_id,),
                    ).fetchone()
                    if new_case and existing is not None:
                        raise StoreInvariantError(f"case already exists: {case.case_id}")
                    if not new_case and existing is None:
                        raise StoreInvariantError(f"cannot update missing case: {case.case_id}")
                    self._ensure_reply_token(conn, case)

                conn.execute(
                    "INSERT INTO messages(message_id, case_id, doc_json) VALUES (?, ?, ?)",
                    (message.message_id, message.case_id, message.model_dump_json()),
                )
                conn.execute(
                    "INSERT INTO assessments(message_id, doc_json) VALUES (?, ?)",
                    (assessment.message_id, assessment.model_dump_json()),
                )
                if case is not None:
                    version = 1 if new_case else int(existing["version"]) + 1
                    self._upsert_case(conn, case, version=version, insert=new_case)
                    if new_case:
                        self._insert_job(conn, self._intake_job(case, version))
                self._insert_events(conn, timeline_events)
                self._insert_audits(conn, audit_entries)
                return True
        except sqlite3.IntegrityError as exc:
            raise StoreInvariantError(
                f"intake transaction violated a store invariant: {exc}"
            ) from exc

    def save_transition(
        self,
        *,
        case: Case,
        expected_version: int,
        idempotency_key: str,
        new_case: bool = False,
        timeline_events: Sequence[TimelineEvent] = (),
        audit_entries: Sequence[AuditEntry] = (),
        outbox_items: Sequence[OutboxItem] = (),
        artifacts: Sequence[WorkflowArtifact] = (),
        memory_records: Sequence[CaseRecord] = (),
        spend_entries: Sequence[SpendEntry] = (),
        workflow_jobs: Sequence[WorkflowJob] = (),
    ) -> TransitionResult:
        if not idempotency_key.strip():
            raise ValueError("idempotency key must not be blank")
        self._validate_transition(
            case=case,
            timeline_events=timeline_events,
            audit_entries=audit_entries,
            outbox_items=outbox_items,
            artifacts=artifacts,
            memory_records=memory_records,
            spend_entries=spend_entries,
        )
        fingerprint = _transition_fingerprint(
            case=case,
            new_case=new_case,
            timeline_events=timeline_events,
            audit_entries=audit_entries,
            outbox_items=outbox_items,
            artifacts=artifacts,
            memory_records=memory_records,
            spend_entries=spend_entries,
        )
        if workflow_jobs:
            fingerprint = hashlib.sha256(
                (
                    fingerprint
                    + json.dumps(
                        [job.model_dump(mode="json") for job in workflow_jobs], sort_keys=True
                    )
                ).encode()
            ).hexdigest()
        try:
            with self._transaction() as conn:
                prior = conn.execute(
                    "SELECT fingerprint, case_id FROM idempotency WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if prior is not None:
                    if prior["fingerprint"] != fingerprint or prior["case_id"] != case.case_id:
                        raise IdempotencyConflict(
                            f"idempotency key {idempotency_key!r} was reused for different facts"
                        )
                    current = conn.execute(
                        "SELECT version FROM cases WHERE case_id = ?",
                        (case.case_id,),
                    ).fetchone()
                    if current is None:
                        raise StoreInvariantError("idempotent transition lost its case")
                    return TransitionResult(applied=False, version=int(current["version"]))

                existing = conn.execute(
                    "SELECT version FROM cases WHERE case_id = ?",
                    (case.case_id,),
                ).fetchone()
                if new_case:
                    if existing is not None:
                        raise ConcurrencyConflict(f"case already exists: {case.case_id}")
                    if expected_version != 0:
                        raise ConcurrencyConflict("new case must start from version zero")
                    next_version = 1
                else:
                    if existing is None:
                        raise StoreInvariantError(f"cannot update missing case: {case.case_id}")
                    actual_version = int(existing["version"])
                    if actual_version != expected_version:
                        raise ConcurrencyConflict(
                            f"case {case.case_id} version is {actual_version}, "
                            f"expected {expected_version}"
                        )
                    next_version = actual_version + 1
                self._ensure_reply_token(conn, case)
                self._upsert_case(conn, case, version=next_version, insert=new_case)
                self._insert_events(conn, timeline_events)
                self._insert_audits(conn, audit_entries)
                self._insert_outbox(conn, outbox_items)
                self._insert_artifacts(conn, artifacts)
                self._insert_memory(conn, memory_records)
                self._insert_spend(conn, spend_entries)
                for job in workflow_jobs:
                    if job.case_id != case.case_id:
                        raise ValueError("workflow job belongs to another case")
                    self._insert_job(conn, job)
                conn.execute(
                    "INSERT INTO idempotency(idempotency_key, fingerprint, case_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (idempotency_key, fingerprint, case.case_id, _aware_iso(case.updated_at)),
                )
                return TransitionResult(applied=True, version=next_version)
        except sqlite3.IntegrityError as exc:
            raise StoreInvariantError(
                f"workflow transaction violated a store invariant: {exc}"
            ) from exc

    # -- Outbox and artifacts -------------------------------------------

    def artifact(self, kind: str, artifact_id: str) -> WorkflowArtifact | None:
        row = self._one(
            "SELECT doc_json FROM artifacts WHERE kind = ? AND artifact_id = ?",
            (kind, artifact_id),
        )
        return WorkflowArtifact.model_validate_json(row["doc_json"]) if row else None

    def artifacts_for(
        self,
        *,
        kind: str,
        case_id: str | None = None,
    ) -> tuple[WorkflowArtifact, ...]:
        if case_id is None:
            rows = self._all(
                "SELECT doc_json FROM artifacts WHERE kind = ? ORDER BY created_at, artifact_id",
                (kind,),
            )
        else:
            rows = self._all(
                "SELECT doc_json FROM artifacts WHERE kind = ? AND case_id = ? "
                "ORDER BY created_at, artifact_id",
                (kind, case_id),
            )
        artifacts = tuple(WorkflowArtifact.model_validate_json(row["doc_json"]) for row in rows)
        revision_field = {
            "appointment.proposal.v1": "revision",
            "appointment.confirmed.v1": "revision",
            "appointment.rules.v1": "version",
            "meeting.assignment.v1": "version",
            "community.schedule.v1": "round",
        }.get(kind)
        return (
            tuple(sorted(artifacts, key=lambda a: a.payload.get(revision_field, 0)))
            if revision_field
            else artifacts
        )

    def outbox_item(self, outbox_id: str) -> OutboxItem | None:
        row = self._one("SELECT doc_json FROM outbox WHERE outbox_id = ?", (outbox_id,))
        return OutboxItem.model_validate_json(row["doc_json"]) if row else None

    def outbox_for_case(self, case_id: str) -> tuple[OutboxItem, ...]:
        rows = self._all(
            "SELECT doc_json FROM outbox WHERE case_id = ? ORDER BY created_at, outbox_id",
            (case_id,),
        )
        return tuple(OutboxItem.model_validate_json(row["doc_json"]) for row in rows)

    def pending_outbox(self, *, limit: int = 100) -> tuple[OutboxItem, ...]:
        if limit <= 0:
            raise ValueError("outbox limit must be positive")
        rows = self._all(
            "SELECT doc_json FROM outbox WHERE status = ? AND delivered_at IS NULL "
            "ORDER BY created_at, outbox_id LIMIT ?",
            (OutboxStatus.PENDING.value, limit),
        )
        return tuple(OutboxItem.model_validate_json(row["doc_json"]) for row in rows)

    def dead_letter_outbox(self, *, limit: int = 100) -> tuple[OutboxItem, ...]:
        if limit <= 0:
            raise ValueError("outbox limit must be positive")
        rows = self._all(
            "SELECT doc_json FROM outbox WHERE status = ? ORDER BY failed_at, outbox_id LIMIT ?",
            (OutboxStatus.DEAD_LETTER.value, limit),
        )
        return tuple(OutboxItem.model_validate_json(row["doc_json"]) for row in rows)

    def mark_outbox_delivered(self, outbox_id: str, *, delivered_at: datetime) -> bool:
        delivered_text = _aware_iso(delivered_at)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT doc_json, status FROM outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise StoreInvariantError(f"unknown outbox item: {outbox_id}")
            if row["status"] == OutboxStatus.DELIVERED.value:
                return False
            if row["status"] != OutboxStatus.PENDING.value:
                raise StoreInvariantError(
                    "legacy delivery marker accepts only an unclaimed pending item"
                )
            if (
                conn.execute(
                    "SELECT 1 FROM claim_outbox WHERE outbox_id = ?",
                    (outbox_id,),
                ).fetchone()
                is not None
            ):
                raise StoreInvariantError(
                    "claimed outbox delivery requires token-guarded completion"
                )
            item = OutboxItem.model_validate_json(row["doc_json"])
            updated = item.model_copy(
                update={
                    "status": OutboxStatus.DELIVERED,
                    "delivered_at": delivered_at,
                    "last_error_code": None,
                    "failed_at": None,
                }
            )
            conn.execute(
                "UPDATE outbox SET status = ?, delivered_at = ?, last_error_code = NULL, "
                "failed_at = NULL, doc_json = ? WHERE outbox_id = ?",
                (
                    OutboxStatus.DELIVERED.value,
                    delivered_text,
                    updated.model_dump_json(),
                    outbox_id,
                ),
            )
            return True

    # -- Spend ledger ----------------------------------------------------

    def month_to_date_spend(
        self,
        category: Category,
        *,
        as_of: datetime,
        currency: str,
    ) -> Decimal:
        if as_of.tzinfo is None:
            raise ValueError("spend as_of must be timezone-aware")
        at = as_of.astimezone(timezone.utc)
        month_start = datetime(at.year, at.month, 1, tzinfo=timezone.utc)
        rows = self._all(
            "SELECT amount FROM spend_ledger WHERE category = ? AND currency = ? "
            "AND committed_at >= ? AND committed_at <= ? ORDER BY committed_at, entry_id",
            (
                category.value,
                currency.strip().upper(),
                month_start.isoformat(),
                at.isoformat(),
            ),
        )
        return sum((Decimal(row["amount"]) for row in rows), Decimal("0"))

    # -- Durable normalized inbox ---------------------------------------

    def record_inbound(self, item: InboxItem) -> bool:
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT payload_hash, doc_json FROM inbound_inbox "
                "WHERE source = ? AND external_id = ?",
                (item.source, item.external_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] == item.payload_hash:
                    return False
                raise IdempotencyConflict(
                    f"inbound id {item.source}:{item.external_id} changed after recording"
                )
            conn.execute(
                "INSERT INTO inbound_inbox(source, external_id, payload_hash, received_at, "
                "status, attempts, last_error_code, failed_at, doc_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.source,
                    item.external_id,
                    item.payload_hash,
                    _aware_iso(item.received_at),
                    item.status.value,
                    item.attempts,
                    item.last_error_code,
                    _optional_iso(item.failed_at),
                    item.model_dump_json(),
                ),
            )
            return True

    def inbox_item(self, source: str, external_id: str) -> InboxItem | None:
        row = self._one(
            "SELECT doc_json FROM inbound_inbox WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        return InboxItem.model_validate_json(row["doc_json"]) if row else None

    def pending_inbound(self, *, limit: int = 100) -> tuple[InboxItem, ...]:
        if limit <= 0:
            raise ValueError("inbox limit must be positive")
        rows = self._all(
            "SELECT doc_json FROM inbound_inbox WHERE status = ? "
            "ORDER BY received_at, source, external_id LIMIT ?",
            (InboxStatus.PENDING.value, limit),
        )
        return tuple(InboxItem.model_validate_json(row["doc_json"]) for row in rows)

    def dead_letter_inbound(self, *, limit: int = 100) -> tuple[InboxItem, ...]:
        if limit <= 0:
            raise ValueError("inbox limit must be positive")
        rows = self._all(
            "SELECT doc_json FROM inbound_inbox WHERE status = ? "
            "ORDER BY failed_at, source, external_id LIMIT ?",
            (InboxStatus.DEAD_LETTER.value, limit),
        )
        return tuple(InboxItem.model_validate_json(row["doc_json"]) for row in rows)

    def mark_inbound_processed(self, source: str, external_id: str) -> bool:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT doc_json, status FROM inbound_inbox WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
            if row is None:
                raise StoreInvariantError(f"unknown inbound item: {source}:{external_id}")
            if row["status"] in (
                InboxStatus.PROCESSED.value,
                InboxStatus.IGNORED.value,
                InboxStatus.DEAD_LETTER.value,
            ):
                return False
            item = InboxItem.model_validate_json(row["doc_json"])
            updated = item.model_copy(update={"status": InboxStatus.PROCESSED})
            conn.execute(
                "UPDATE inbound_inbox SET status = ?, doc_json = ? "
                "WHERE source = ? AND external_id = ?",
                (
                    InboxStatus.PROCESSED.value,
                    updated.model_dump_json(),
                    source,
                    external_id,
                ),
            )
            return True

    # -- Lease claims ----------------------------------------------------

    def inference_job(self, job_id: str) -> InferenceJob | None:
        safe_job_id = job_id.strip()
        if not safe_job_id:
            raise ValueError("inference job_id must not be blank")
        row = self._one(
            "SELECT doc_json FROM inference_jobs WHERE job_id = ?",
            (safe_job_id,),
        )
        return InferenceJob.model_validate_json(row["doc_json"]) if row else None

    def claim_inference_job(
        self,
        *,
        job_id: str,
        kind: str,
        case_id: str | None,
        input_sha256: str,
        token: str,
        now: datetime,
        lease_seconds: int = 300,
        max_attempts: int = 3,
    ) -> InferenceClaimResult:
        safe_job_id = job_id.strip()
        safe_kind = kind.strip()
        safe_case_id = case_id.strip() if case_id is not None else None
        safe_token = token.strip()
        safe_hash = _safe_sha256(input_sha256)
        if not safe_job_id or not safe_kind or not safe_token:
            raise ValueError("inference claim identity and token must not be blank")
        if case_id is not None and not safe_case_id:
            raise ValueError("inference case_id must not be blank when provided")
        if lease_seconds <= 0:
            raise ValueError("inference lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("inference max_attempts must be positive")
        now_utc = _aware_utc_ts(now)
        expires_at = now_utc + _seconds(lease_seconds)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT doc_json, lease_token, claimed_at, lease_expires_at "
                "FROM inference_jobs WHERE job_id = ?",
                (safe_job_id,),
            ).fetchone()
            if row is None:
                job = InferenceJob(
                    job_id=safe_job_id,
                    kind=safe_kind,
                    case_id=safe_case_id,
                    input_sha256=safe_hash,
                    created_at=now_utc,
                    updated_at=now_utc,
                )
                conn.execute(
                    "INSERT INTO inference_jobs(job_id, kind, case_id, input_sha256, "
                    "status, attempts, created_at, updated_at, completed_at, failed_at, "
                    "last_error_code, result_json, lease_token, claimed_at, "
                    "lease_expires_at, doc_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, "
                    "NULL, NULL, NULL, NULL, NULL, NULL, ?)",
                    (
                        job.job_id,
                        job.kind,
                        job.case_id,
                        job.input_sha256,
                        job.status.value,
                        job.attempts,
                        _aware_iso(job.created_at),
                        _aware_iso(job.updated_at),
                        job.model_dump_json(),
                    ),
                )
                row = conn.execute(
                    "SELECT doc_json, lease_token, claimed_at, lease_expires_at "
                    "FROM inference_jobs WHERE job_id = ?",
                    (safe_job_id,),
                ).fetchone()
                assert row is not None
            job = InferenceJob.model_validate_json(row["doc_json"])
            if (
                job.kind != safe_kind
                or job.case_id != safe_case_id
                or job.input_sha256 != safe_hash
            ):
                raise IdempotencyConflict(
                    "inference job_id was reused for different immutable input"
                )
            if job.status is InferenceJobStatus.COMPLETED:
                return InferenceClaimResult(
                    disposition=InferenceClaimDisposition.COMPLETED,
                    job=job,
                )
            if job.status is InferenceJobStatus.FAILED:
                return InferenceClaimResult(
                    disposition=InferenceClaimDisposition.FAILED,
                    job=job,
                )
            if now_utc < job.updated_at:
                raise StoreInvariantError("inference clock moved backwards")
            lease_token = row["lease_token"]
            lease_expires = (
                datetime.fromisoformat(row["lease_expires_at"])
                if row["lease_expires_at"] is not None
                else None
            )
            if lease_token is not None and lease_expires is not None:
                if lease_expires > now_utc and lease_token == safe_token:
                    claimed_at = datetime.fromisoformat(row["claimed_at"])
                    return InferenceClaimResult(
                        disposition=InferenceClaimDisposition.CLAIMED,
                        job=job,
                        lease=ClaimLease(
                            scope="inference",
                            item_key=job.job_id,
                            token=safe_token,
                            claimed_at=claimed_at,
                            lease_expires_at=lease_expires,
                            attempts=job.attempts,
                        ),
                    )
                if lease_expires > now_utc:
                    return InferenceClaimResult(
                        disposition=InferenceClaimDisposition.BUSY,
                        job=job,
                    )
            # A lease prevents concurrent work while live. Once expired, a replacement
            # worker may repeat a still-running slow invocation, but token fencing
            # ensures only the current owner can persist one durable result.
            if job.attempts >= max_attempts:
                failed = _validated_inference_update(
                    job,
                    {
                        "status": InferenceJobStatus.FAILED,
                        "updated_at": now_utc,
                        "failed_at": now_utc,
                        "last_error_code": "lease_attempts_exhausted",
                    },
                )
                conn.execute(
                    "UPDATE inference_jobs SET status = ?, updated_at = ?, failed_at = ?, "
                    "last_error_code = ?, lease_token = NULL, claimed_at = NULL, "
                    "lease_expires_at = NULL, doc_json = ? WHERE job_id = ?",
                    (
                        failed.status.value,
                        _aware_iso(failed.updated_at),
                        _aware_iso(failed.failed_at),
                        failed.last_error_code,
                        failed.model_dump_json(),
                        failed.job_id,
                    ),
                )
                return InferenceClaimResult(
                    disposition=InferenceClaimDisposition.FAILED,
                    job=failed,
                )
            claimed_job = _validated_inference_update(
                job,
                {
                    "attempts": job.attempts + 1,
                    "updated_at": now_utc,
                    "last_error_code": None,
                },
            )
            conn.execute(
                "UPDATE inference_jobs SET attempts = ?, updated_at = ?, "
                "last_error_code = NULL, lease_token = ?, claimed_at = ?, "
                "lease_expires_at = ?, doc_json = ? WHERE job_id = ?",
                (
                    claimed_job.attempts,
                    _aware_iso(claimed_job.updated_at),
                    safe_token,
                    _aware_iso(now_utc),
                    _aware_iso(expires_at),
                    claimed_job.model_dump_json(),
                    claimed_job.job_id,
                ),
            )
            lease = ClaimLease(
                scope="inference",
                item_key=claimed_job.job_id,
                token=safe_token,
                claimed_at=now_utc,
                lease_expires_at=expires_at,
                attempts=claimed_job.attempts,
            )
            return InferenceClaimResult(
                disposition=InferenceClaimDisposition.CLAIMED,
                job=claimed_job,
                lease=lease,
            )

    def renew_inference_claim(
        self,
        *,
        job_id: str,
        token: str,
        now: datetime,
        lease_seconds: int = 300,
    ) -> ClaimLease:
        if lease_seconds <= 0:
            raise ValueError("inference lease_seconds must be positive")
        now_utc = _aware_utc_ts(now)
        expires_at = now_utc + _seconds(lease_seconds)
        with self._transaction() as conn:
            row = self._require_inference_token(
                conn,
                job_id=job_id,
                token=token,
                at=now_utc,
            )
            job = InferenceJob.model_validate_json(row["doc_json"])
            if now_utc < job.updated_at:
                raise StoreInvariantError("inference clock moved backwards")
            if job.status is not InferenceJobStatus.PENDING:
                raise StoreInvariantError("only a pending inference job can be renewed")
            updated = _validated_inference_update(
                job,
                {"updated_at": now_utc},
            )
            conn.execute(
                "UPDATE inference_jobs SET updated_at = ?, lease_expires_at = ?, "
                "doc_json = ? WHERE job_id = ?",
                (
                    _aware_iso(updated.updated_at),
                    _aware_iso(expires_at),
                    updated.model_dump_json(),
                    updated.job_id,
                ),
            )
            return ClaimLease(
                scope="inference",
                item_key=updated.job_id,
                token=token,
                claimed_at=datetime.fromisoformat(row["claimed_at"]),
                lease_expires_at=expires_at,
                attempts=updated.attempts,
            )

    def complete_inference_claim(
        self,
        *,
        job_id: str,
        token: str,
        completed_at: datetime,
        result_payload: dict[str, Any],
    ) -> InferenceJob:
        completed_utc = _aware_utc_ts(completed_at)
        result_json = json.dumps(
            result_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(result_json.encode("utf-8")) > 1_000_000:
            raise ValueError("inference result payload exceeds one megabyte")
        with self._transaction() as conn:
            row = self._require_inference_token(
                conn,
                job_id=job_id,
                token=token,
                at=completed_utc,
            )
            job = InferenceJob.model_validate_json(row["doc_json"])
            if completed_utc < job.updated_at:
                raise StoreInvariantError("inference clock moved backwards")
            if job.status is not InferenceJobStatus.PENDING:
                raise StoreInvariantError("only a pending inference job can complete")
            completed = _validated_inference_update(
                job,
                {
                    "status": InferenceJobStatus.COMPLETED,
                    "updated_at": completed_utc,
                    "completed_at": completed_utc,
                    "last_error_code": None,
                    "result_payload": result_payload,
                },
            )
            conn.execute(
                "UPDATE inference_jobs SET status = ?, updated_at = ?, completed_at = ?, "
                "failed_at = NULL, last_error_code = NULL, result_json = ?, "
                "lease_token = NULL, claimed_at = NULL, lease_expires_at = NULL, "
                "doc_json = ? WHERE job_id = ?",
                (
                    completed.status.value,
                    _aware_iso(completed.updated_at),
                    _aware_iso(completed.completed_at),
                    result_json,
                    completed.model_dump_json(),
                    completed.job_id,
                ),
            )
            return completed

    def fail_inference_claim(
        self,
        *,
        job_id: str,
        token: str,
        failed_at: datetime,
        error_code: str,
        retryable: bool,
        max_attempts: int,
    ) -> FailureDisposition:
        if max_attempts <= 0:
            raise ValueError("inference max_attempts must be positive")
        failed_utc = _aware_utc_ts(failed_at)
        safe_code = _safe_error_code(error_code)
        with self._transaction() as conn:
            row = self._require_inference_token(
                conn,
                job_id=job_id,
                token=token,
                at=failed_utc,
            )
            job = InferenceJob.model_validate_json(row["doc_json"])
            if failed_utc < job.updated_at:
                raise StoreInvariantError("inference clock moved backwards")
            if job.status is not InferenceJobStatus.PENDING:
                raise StoreInvariantError("only a pending inference job can fail")
            should_retry = retryable and job.attempts < max_attempts
            if should_retry:
                updated = _validated_inference_update(
                    job,
                    {
                        "updated_at": failed_utc,
                        "last_error_code": safe_code,
                    },
                )
                disposition = FailureDisposition.RETRY_PENDING
            else:
                updated = _validated_inference_update(
                    job,
                    {
                        "status": InferenceJobStatus.FAILED,
                        "updated_at": failed_utc,
                        "failed_at": failed_utc,
                        "last_error_code": safe_code,
                    },
                )
                disposition = FailureDisposition.DEAD_LETTERED
            conn.execute(
                "UPDATE inference_jobs SET status = ?, updated_at = ?, failed_at = ?, "
                "last_error_code = ?, result_json = NULL, lease_token = NULL, "
                "claimed_at = NULL, lease_expires_at = NULL, doc_json = ? "
                "WHERE job_id = ?",
                (
                    updated.status.value,
                    _aware_iso(updated.updated_at),
                    _optional_iso(updated.failed_at),
                    updated.last_error_code,
                    updated.model_dump_json(),
                    updated.job_id,
                ),
            )
            return disposition

    def release_inference_claim(self, *, job_id: str, token: str) -> bool:
        with self._transaction() as conn:
            self._require_inference_token(
                conn,
                job_id=job_id,
                token=token,
                at=None,
            )
            cursor = conn.execute(
                "UPDATE inference_jobs SET lease_token = NULL, claimed_at = NULL, "
                "lease_expires_at = NULL WHERE job_id = ? AND lease_token = ?",
                (job_id, token),
            )
            return cursor.rowcount == 1

    def claim_inbound(
        self,
        *,
        token: str,
        now: datetime,
        lease_seconds: int = 300,
        limit: int = 1,
        max_attempts: int = 3,
    ) -> tuple[InboxItem, ...]:
        if not token.strip():
            raise ValueError("lease token must not be blank")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if limit <= 0:
            raise ValueError("claim limit must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        now_utc = _aware_utc_ts(now)
        now_text = now_utc.isoformat()
        expires_text = (now_utc + _seconds(lease_seconds)).isoformat()
        claimed: list[InboxItem] = []
        with self._transaction() as conn:
            self._dead_letter_exhausted_inbound(
                conn,
                now=now_utc,
                max_attempts=max_attempts,
            )
            rows = conn.execute(
                "SELECT i.source AS source, i.external_id AS external_id, "
                "i.attempts AS attempts, i.doc_json AS doc_json "
                "FROM inbound_inbox AS i "
                "LEFT JOIN claim_inbound AS c "
                "ON c.source = i.source AND c.external_id = i.external_id "
                "WHERE i.status = ? AND i.attempts < ? "
                "AND (c.token IS NULL OR c.lease_expires_at <= ?) "
                "ORDER BY i.received_at, i.source, i.external_id LIMIT ?",
                (InboxStatus.PENDING.value, max_attempts, now_text, limit),
            ).fetchall()
            for row in rows:
                attempts = int(row["attempts"]) + 1
                item = InboxItem.model_validate_json(row["doc_json"]).model_copy(
                    update={"attempts": attempts}
                )
                conn.execute(
                    "UPDATE inbound_inbox SET attempts = ?, doc_json = ? "
                    "WHERE source = ? AND external_id = ?",
                    (
                        attempts,
                        item.model_dump_json(),
                        row["source"],
                        row["external_id"],
                    ),
                )
                conn.execute(
                    "INSERT INTO claim_inbound(source, external_id, token, claimed_at, "
                    "lease_expires_at, attempts) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(source, external_id) DO UPDATE SET "
                    "token = excluded.token, claimed_at = excluded.claimed_at, "
                    "lease_expires_at = excluded.lease_expires_at, attempts = excluded.attempts",
                    (
                        row["source"],
                        row["external_id"],
                        token,
                        now_text,
                        expires_text,
                        attempts,
                    ),
                )
                claimed.append(item)
        return tuple(claimed)

    def complete_inbound_claim(
        self,
        *,
        source: str,
        external_id: str,
        token: str,
        now: datetime,
    ) -> bool:
        with self._transaction() as conn:
            self._require_live_inbound_token(conn, source, external_id, token)
            row = conn.execute(
                "SELECT doc_json, status FROM inbound_inbox WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
            if row is None:
                raise StoreInvariantError(f"unknown inbound item: {source}:{external_id}")
            conn.execute(
                "DELETE FROM claim_inbound WHERE source = ? AND external_id = ?",
                (source, external_id),
            )
            if row["status"] in (
                InboxStatus.PROCESSED.value,
                InboxStatus.IGNORED.value,
                InboxStatus.DEAD_LETTER.value,
            ):
                return False
            item = InboxItem.model_validate_json(row["doc_json"])
            updated = item.model_copy(
                update={
                    "status": InboxStatus.PROCESSED,
                    "last_error_code": None,
                    "failed_at": None,
                }
            )
            conn.execute(
                "UPDATE inbound_inbox SET status = ?, last_error_code = NULL, "
                "failed_at = NULL, doc_json = ? WHERE source = ? AND external_id = ?",
                (
                    InboxStatus.PROCESSED.value,
                    updated.model_dump_json(),
                    source,
                    external_id,
                ),
            )
            return True

    def fail_inbound_claim(
        self,
        *,
        source: str,
        external_id: str,
        token: str,
        failed_at: datetime,
        error_code: str,
        retryable: bool,
        max_attempts: int,
    ) -> FailureDisposition:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        safe_code = _safe_error_code(error_code)
        failed_utc = _aware_utc_ts(failed_at)
        with self._transaction() as conn:
            self._require_live_inbound_token(conn, source, external_id, token)
            row = conn.execute(
                "SELECT doc_json, status, attempts FROM inbound_inbox "
                "WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
            if row is None:
                raise StoreInvariantError(f"unknown inbound item: {source}:{external_id}")
            if row["status"] != InboxStatus.PENDING.value:
                raise StoreInvariantError("only a pending inbound item can fail")
            attempts = int(row["attempts"])
            should_retry = retryable and attempts < max_attempts
            status = InboxStatus.PENDING if should_retry else InboxStatus.DEAD_LETTER
            terminal_at = None if should_retry else failed_utc
            item = InboxItem.model_validate_json(row["doc_json"])
            updated = item.model_copy(
                update={
                    "status": status,
                    "last_error_code": safe_code,
                    "failed_at": terminal_at,
                }
            )
            conn.execute(
                "UPDATE inbound_inbox SET status = ?, last_error_code = ?, failed_at = ?, "
                "doc_json = ? WHERE source = ? AND external_id = ?",
                (
                    status.value,
                    safe_code,
                    _optional_iso(terminal_at),
                    updated.model_dump_json(),
                    source,
                    external_id,
                ),
            )
            conn.execute(
                "DELETE FROM claim_inbound WHERE source = ? AND external_id = ?",
                (source, external_id),
            )
            return (
                FailureDisposition.RETRY_PENDING
                if should_retry
                else FailureDisposition.DEAD_LETTERED
            )

    def release_inbound_claim(
        self,
        *,
        source: str,
        external_id: str,
        token: str,
    ) -> bool:
        with self._transaction() as conn:
            self._require_live_inbound_token(conn, source, external_id, token)
            cursor = conn.execute(
                "DELETE FROM claim_inbound WHERE source = ? AND external_id = ? AND token = ?",
                (source, external_id, token),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _dead_letter_exhausted_inbound(
        conn: sqlite3.Connection,
        *,
        now: datetime,
        max_attempts: int,
    ) -> None:
        rows = conn.execute(
            "SELECT i.source, i.external_id, i.doc_json FROM inbound_inbox AS i "
            "LEFT JOIN claim_inbound AS c "
            "ON c.source = i.source AND c.external_id = i.external_id "
            "WHERE i.status = ? AND i.attempts >= ? "
            "AND (c.token IS NULL OR c.lease_expires_at <= ?)",
            (InboxStatus.PENDING.value, max_attempts, now.isoformat()),
        ).fetchall()
        for row in rows:
            item = InboxItem.model_validate_json(row["doc_json"])
            updated = item.model_copy(
                update={
                    "status": InboxStatus.DEAD_LETTER,
                    "last_error_code": "lease_attempts_exhausted",
                    "failed_at": now,
                }
            )
            conn.execute(
                "UPDATE inbound_inbox SET status = ?, last_error_code = ?, failed_at = ?, "
                "doc_json = ? WHERE source = ? AND external_id = ?",
                (
                    InboxStatus.DEAD_LETTER.value,
                    "lease_attempts_exhausted",
                    now.isoformat(),
                    updated.model_dump_json(),
                    row["source"],
                    row["external_id"],
                ),
            )
            conn.execute(
                "DELETE FROM claim_inbound WHERE source = ? AND external_id = ?",
                (row["source"], row["external_id"]),
            )

    def claim_outbox(
        self,
        *,
        token: str,
        now: datetime,
        lease_seconds: int = 300,
        limit: int = 1,
        max_attempts: int = 3,
        kind: str | None = None,
    ) -> tuple[OutboxItem, ...]:
        if not token.strip():
            raise ValueError("lease token must not be blank")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if limit <= 0:
            raise ValueError("claim limit must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if kind is not None and not kind.strip():
            raise ValueError("outbox kind filter must not be blank")
        now_utc = _aware_utc_ts(now)
        now_text = now_utc.isoformat()
        expires_text = (now_utc + _seconds(lease_seconds)).isoformat()
        claimed: list[OutboxItem] = []
        with self._transaction() as conn:
            self._mark_expired_dispatching_ambiguous(conn, now=now_utc)
            self._dead_letter_exhausted_outbox(
                conn,
                now=now_utc,
                max_attempts=max_attempts,
            )
            rows = conn.execute(
                "SELECT o.outbox_id AS outbox_id, o.attempts AS attempts, "
                "o.doc_json AS doc_json FROM outbox AS o "
                "LEFT JOIN claim_outbox AS c ON c.outbox_id = o.outbox_id "
                "WHERE o.status = ? AND o.delivered_at IS NULL AND o.attempts < ? "
                "AND (? IS NULL OR o.kind = ?) "
                "AND (c.token IS NULL OR c.lease_expires_at <= ?) "
                "ORDER BY o.created_at, o.outbox_id LIMIT ?",
                (
                    OutboxStatus.PENDING.value,
                    max_attempts,
                    kind,
                    kind,
                    now_text,
                    limit,
                ),
            ).fetchall()
            for row in rows:
                attempts = int(row["attempts"]) + 1
                item = OutboxItem.model_validate_json(row["doc_json"]).model_copy(
                    update={"attempts": attempts}
                )
                conn.execute(
                    "UPDATE outbox SET attempts = ?, doc_json = ? WHERE outbox_id = ?",
                    (attempts, item.model_dump_json(), row["outbox_id"]),
                )
                conn.execute(
                    "INSERT INTO claim_outbox(outbox_id, token, claimed_at, lease_expires_at, "
                    "attempts) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(outbox_id) DO UPDATE SET "
                    "token = excluded.token, claimed_at = excluded.claimed_at, "
                    "lease_expires_at = excluded.lease_expires_at, attempts = excluded.attempts",
                    (row["outbox_id"], token, now_text, expires_text, attempts),
                )
                claimed.append(item)
        return tuple(claimed)

    @staticmethod
    def _mark_expired_dispatching_ambiguous(
        conn: sqlite3.Connection,
        *,
        now: datetime,
    ) -> None:
        rows = conn.execute(
            "SELECT o.outbox_id, o.doc_json FROM outbox AS o "
            "JOIN claim_outbox AS c ON c.outbox_id = o.outbox_id "
            "WHERE o.status = ? AND c.lease_expires_at <= ?",
            (OutboxStatus.DISPATCHING.value, now.isoformat()),
        ).fetchall()
        for row in rows:
            item = OutboxItem.model_validate_json(row["doc_json"])
            updated = item.model_copy(
                update={
                    "status": OutboxStatus.AMBIGUOUS,
                    "last_error_code": "delivery_outcome_unknown",
                    "failed_at": now,
                }
            )
            conn.execute(
                "UPDATE outbox SET status = ?, last_error_code = ?, failed_at = ?, "
                "doc_json = ? WHERE outbox_id = ?",
                (
                    OutboxStatus.AMBIGUOUS.value,
                    "delivery_outcome_unknown",
                    now.isoformat(),
                    updated.model_dump_json(),
                    row["outbox_id"],
                ),
            )
            conn.execute("DELETE FROM claim_outbox WHERE outbox_id = ?", (row["outbox_id"],))

    @staticmethod
    def _dead_letter_exhausted_outbox(
        conn: sqlite3.Connection,
        *,
        now: datetime,
        max_attempts: int,
    ) -> None:
        rows = conn.execute(
            "SELECT o.outbox_id, o.doc_json FROM outbox AS o "
            "LEFT JOIN claim_outbox AS c ON c.outbox_id = o.outbox_id "
            "WHERE o.status = ? AND o.attempts >= ? "
            "AND (c.token IS NULL OR c.lease_expires_at <= ?)",
            (OutboxStatus.PENDING.value, max_attempts, now.isoformat()),
        ).fetchall()
        for row in rows:
            item = OutboxItem.model_validate_json(row["doc_json"])
            updated = item.model_copy(
                update={
                    "status": OutboxStatus.DEAD_LETTER,
                    "last_error_code": "lease_attempts_exhausted",
                    "failed_at": now,
                }
            )
            conn.execute(
                "UPDATE outbox SET status = ?, last_error_code = ?, failed_at = ?, "
                "doc_json = ? WHERE outbox_id = ?",
                (
                    OutboxStatus.DEAD_LETTER.value,
                    "lease_attempts_exhausted",
                    now.isoformat(),
                    updated.model_dump_json(),
                    row["outbox_id"],
                ),
            )
            conn.execute("DELETE FROM claim_outbox WHERE outbox_id = ?", (row["outbox_id"],))

    def begin_outbox_delivery(
        self,
        *,
        outbox_id: str,
        token: str,
        started_at: datetime,
    ) -> bool:
        started_utc = _aware_utc_ts(started_at)
        with self._transaction() as conn:
            self._require_live_outbox_token(conn, outbox_id, token)
            row = conn.execute(
                "SELECT doc_json, status FROM outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise StoreInvariantError(f"unknown outbox item: {outbox_id}")
            if row["status"] != OutboxStatus.PENDING.value:
                return False
            item = OutboxItem.model_validate_json(row["doc_json"])
            updated = item.model_copy(
                update={
                    "status": OutboxStatus.DISPATCHING,
                    "delivery_started_at": started_utc,
                    "last_error_code": None,
                    "failed_at": None,
                }
            )
            conn.execute(
                "UPDATE outbox SET status = ?, delivery_started_at = ?, "
                "last_error_code = NULL, failed_at = NULL, doc_json = ? "
                "WHERE outbox_id = ?",
                (
                    OutboxStatus.DISPATCHING.value,
                    started_utc.isoformat(),
                    updated.model_dump_json(),
                    outbox_id,
                ),
            )
            return True

    def complete_outbox_claim(
        self,
        *,
        outbox_id: str,
        token: str,
        delivered_at: datetime,
        provider_message_id: str | None = None,
    ) -> bool:
        delivered_text = _aware_iso(delivered_at)
        provider_id = provider_message_id.strip() if provider_message_id else None
        with self._transaction() as conn:
            self._require_live_outbox_token(conn, outbox_id, token)
            row = conn.execute(
                "SELECT doc_json, status FROM outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise StoreInvariantError(f"unknown outbox item: {outbox_id}")
            conn.execute("DELETE FROM claim_outbox WHERE outbox_id = ?", (outbox_id,))
            if row["status"] == OutboxStatus.DELIVERED.value:
                return False
            if row["status"] not in (
                OutboxStatus.PENDING.value,
                OutboxStatus.DISPATCHING.value,
            ):
                raise StoreInvariantError(
                    "only a pending or dispatching outbox item can be completed"
                )
            item = OutboxItem.model_validate_json(row["doc_json"])
            updated = item.model_copy(
                update={
                    "status": OutboxStatus.DELIVERED,
                    "delivered_at": delivered_at,
                    "provider_message_id": provider_id,
                    "last_error_code": None,
                    "failed_at": None,
                }
            )
            conn.execute(
                "UPDATE outbox SET status = ?, delivered_at = ?, provider_message_id = ?, "
                "last_error_code = NULL, failed_at = NULL, doc_json = ? "
                "WHERE outbox_id = ?",
                (
                    OutboxStatus.DELIVERED.value,
                    delivered_text,
                    provider_id,
                    updated.model_dump_json(),
                    outbox_id,
                ),
            )
            return True

    def fail_outbox_claim(
        self,
        *,
        outbox_id: str,
        token: str,
        failed_at: datetime,
        error_code: str,
        retryable: bool,
        max_attempts: int,
        ambiguous: bool = False,
    ) -> FailureDisposition:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        safe_code = _safe_error_code(error_code)
        failed_utc = _aware_utc_ts(failed_at)
        with self._transaction() as conn:
            self._require_live_outbox_token(conn, outbox_id, token)
            row = conn.execute(
                "SELECT doc_json, status, attempts FROM outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise StoreInvariantError(f"unknown outbox item: {outbox_id}")
            if row["status"] not in (
                OutboxStatus.PENDING.value,
                OutboxStatus.DISPATCHING.value,
            ):
                raise StoreInvariantError("only a pending or dispatching outbox item can fail")
            attempts = int(row["attempts"])
            should_retry = not ambiguous and retryable and attempts < max_attempts
            if ambiguous:
                status = OutboxStatus.AMBIGUOUS
            elif should_retry:
                status = OutboxStatus.PENDING
            else:
                status = OutboxStatus.DEAD_LETTER
            terminal_at = None if should_retry else failed_utc
            item = OutboxItem.model_validate_json(row["doc_json"])
            updated = item.model_copy(
                update={
                    "status": status,
                    "last_error_code": safe_code,
                    "failed_at": terminal_at,
                    "delivery_started_at": (None if should_retry else item.delivery_started_at),
                }
            )
            conn.execute(
                "UPDATE outbox SET status = ?, last_error_code = ?, failed_at = ?, "
                "delivery_started_at = ?, doc_json = ? WHERE outbox_id = ?",
                (
                    status.value,
                    safe_code,
                    _optional_iso(terminal_at),
                    _optional_iso(updated.delivery_started_at),
                    updated.model_dump_json(),
                    outbox_id,
                ),
            )
            conn.execute("DELETE FROM claim_outbox WHERE outbox_id = ?", (outbox_id,))
            if ambiguous:
                return FailureDisposition.AMBIGUOUS
            return (
                FailureDisposition.RETRY_PENDING
                if should_retry
                else FailureDisposition.DEAD_LETTERED
            )

    def release_outbox_claim(self, *, outbox_id: str, token: str) -> bool:
        with self._transaction() as conn:
            self._require_live_outbox_token(conn, outbox_id, token)
            cursor = conn.execute(
                "DELETE FROM claim_outbox WHERE outbox_id = ? AND token = ?",
                (outbox_id, token),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _require_inference_token(
        conn: sqlite3.Connection,
        *,
        job_id: str,
        token: str,
        at: datetime | None,
    ) -> sqlite3.Row:
        safe_job_id = job_id.strip()
        safe_token = token.strip()
        if not safe_job_id or not safe_token:
            raise ValueError("inference job_id and token must not be blank")
        row = conn.execute(
            "SELECT doc_json, lease_token, claimed_at, lease_expires_at "
            "FROM inference_jobs WHERE job_id = ?",
            (safe_job_id,),
        ).fetchone()
        if row is None or row["lease_token"] != safe_token:
            raise StaleLeaseToken(f"token no longer owns inference lease {safe_job_id}")
        if row["claimed_at"] is None or row["lease_expires_at"] is None:
            raise StaleLeaseToken(f"inference lease is incomplete for {safe_job_id}")
        if at is not None and datetime.fromisoformat(row["lease_expires_at"]) <= at:
            raise StaleLeaseToken(f"inference lease expired for {safe_job_id}")
        return row

    @staticmethod
    def _require_live_inbound_token(
        conn: sqlite3.Connection,
        source: str,
        external_id: str,
        token: str,
    ) -> None:
        row = conn.execute(
            "SELECT token FROM claim_inbound WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        if row is None or row["token"] != token:
            raise StaleLeaseToken(f"token no longer owns inbound lease {source}:{external_id}")

    @staticmethod
    def _require_live_outbox_token(
        conn: sqlite3.Connection,
        outbox_id: str,
        token: str,
    ) -> None:
        row = conn.execute(
            "SELECT token FROM claim_outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        if row is None or row["token"] != token:
            raise StaleLeaseToken(f"token no longer owns outbox lease {outbox_id}")

    # -- Proactive suggestions ------------------------------------------

    def upsert_suggestion(self, suggestion: MaintenanceSuggestion) -> bool:
        with self._transaction() as conn:
            # Evidence enrichment is the same review opportunity, not another
            # task. Older releases included source IDs in the identifier. Retain
            # those rows as expired history, and never reopen an acted-on review.
            related = conn.execute(
                "SELECT doc_json FROM proactive_suggestions WHERE rule_id = ? AND category = ?",
                (suggestion.rule_id, suggestion.category.value),
            ).fetchall()
            legacy = [
                item
                for row in related
                if (item := MaintenanceSuggestion.model_validate_json(row["doc_json"]))
                and item.suggestion_id != suggestion.suggestion_id
                and item.kind is suggestion.kind
                and item.asset_id == suggestion.asset_id
                and item.due_at.date() == suggestion.due_at.date()
            ]
            for item in legacy:
                if item.status is SuggestionStatus.SUGGESTED:
                    archived = item.model_copy(update={"status": SuggestionStatus.EXPIRED})
                    conn.execute(
                        "UPDATE proactive_suggestions SET status = ?, doc_json = ? "
                        "WHERE suggestion_id = ?",
                        (archived.status.value, archived.model_dump_json(), item.suggestion_id),
                    )
            if any(
                item.status in (SuggestionStatus.ACCEPTED, SuggestionStatus.DISMISSED)
                for item in legacy
            ):
                pending = conn.execute(
                    "SELECT doc_json FROM proactive_suggestions "
                    "WHERE suggestion_id = ? AND status = ?",
                    (suggestion.suggestion_id, SuggestionStatus.SUGGESTED.value),
                ).fetchone()
                if pending:
                    archived = MaintenanceSuggestion.model_validate_json(
                        pending["doc_json"]
                    ).model_copy(update={"status": SuggestionStatus.EXPIRED})
                    conn.execute(
                        "UPDATE proactive_suggestions SET status = ?, doc_json = ? "
                        "WHERE suggestion_id = ?",
                        (archived.status.value, archived.model_dump_json(), archived.suggestion_id),
                    )
                return False
            existing = conn.execute(
                "SELECT doc_json FROM proactive_suggestions WHERE suggestion_id = ?",
                (suggestion.suggestion_id,),
            ).fetchone()
            if existing is not None:
                stored = MaintenanceSuggestion.model_validate_json(existing["doc_json"])
                # A live decision or expiry is never silently overwritten by a
                # freshly recomputed SUGGESTED row for the same deterministic id.
                if stored.status is not SuggestionStatus.SUGGESTED:
                    return False
                candidate = suggestion.model_copy(update={"suggested_at": stored.suggested_at})
                if stored == candidate:
                    return False
                conn.execute(
                    "UPDATE proactive_suggestions SET rule_id = ?, category = ?, status = ?, "
                    "due_at = ?, suggested_at = ?, doc_json = ? WHERE suggestion_id = ?",
                    (
                        candidate.rule_id,
                        candidate.category.value,
                        candidate.status.value,
                        _aware_iso(candidate.due_at),
                        _aware_iso(candidate.suggested_at),
                        candidate.model_dump_json(),
                        candidate.suggestion_id,
                    ),
                )
                return True
            conn.execute(
                "INSERT INTO proactive_suggestions(suggestion_id, rule_id, category, status, "
                "due_at, suggested_at, doc_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    suggestion.suggestion_id,
                    suggestion.rule_id,
                    suggestion.category.value,
                    suggestion.status.value,
                    _aware_iso(suggestion.due_at),
                    _aware_iso(suggestion.suggested_at),
                    suggestion.model_dump_json(),
                ),
            )
            return True

    def suggestion(self, suggestion_id: str) -> MaintenanceSuggestion | None:
        row = self._one(
            "SELECT doc_json FROM proactive_suggestions WHERE suggestion_id = ?",
            (suggestion_id,),
        )
        return MaintenanceSuggestion.model_validate_json(row["doc_json"]) if row else None

    def list_suggestions(
        self,
        *,
        status: SuggestionStatus | None = None,
        limit: int = 100,
    ) -> tuple[MaintenanceSuggestion, ...]:
        if limit <= 0:
            raise ValueError("suggestion limit must be positive")
        if status is None:
            rows = self._all(
                "SELECT doc_json FROM proactive_suggestions ORDER BY due_at, suggestion_id LIMIT ?",
                (limit,),
            )
        else:
            rows = self._all(
                "SELECT doc_json FROM proactive_suggestions WHERE status = ? "
                "ORDER BY due_at, suggestion_id LIMIT ?",
                (status.value, limit),
            )
        return tuple(MaintenanceSuggestion.model_validate_json(row["doc_json"]) for row in rows)

    def decide_suggestion(
        self,
        suggestion_id: str,
        *,
        status: SuggestionStatus,
        decided_by: str,
        decided_at: datetime,
    ) -> MaintenanceSuggestion:
        if status not in (SuggestionStatus.ACCEPTED, SuggestionStatus.DISMISSED):
            raise ValueError("decisions must accept or dismiss a suggestion")
        decided_utc = _aware_utc_ts(decided_at)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT doc_json FROM proactive_suggestions WHERE suggestion_id = ?",
                (suggestion_id,),
            ).fetchone()
            if row is None:
                raise StoreInvariantError(f"unknown suggestion: {suggestion_id}")
            stored = MaintenanceSuggestion.model_validate_json(row["doc_json"])
            if stored.status is status and stored.decided_by == decided_by.strip():
                return stored
            if stored.status is not SuggestionStatus.SUGGESTED:
                raise StoreInvariantError(
                    f"suggestion {suggestion_id} is already {stored.status.value}"
                )
            decided = stored.model_copy(
                update={
                    "status": status,
                    "decided_by": decided_by.strip(),
                    "decided_at": decided_utc,
                }
            )
            conn.execute(
                "UPDATE proactive_suggestions SET status = ?, doc_json = ? WHERE suggestion_id = ?",
                (status.value, decided.model_dump_json(), suggestion_id),
            )
            return decided

    def expire_suggestions(self, *, now: datetime) -> tuple[str, ...]:
        now_text = _aware_utc_ts(now).isoformat()
        expired: list[str] = []
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT suggestion_id, doc_json FROM proactive_suggestions "
                "WHERE status = ? AND due_at < ? ORDER BY due_at, suggestion_id",
                (SuggestionStatus.SUGGESTED.value, now_text),
            ).fetchall()
            for row in rows:
                stored = MaintenanceSuggestion.model_validate_json(row["doc_json"])
                updated = stored.model_copy(update={"status": SuggestionStatus.EXPIRED})
                conn.execute(
                    "UPDATE proactive_suggestions SET status = ?, doc_json = ? "
                    "WHERE suggestion_id = ?",
                    (
                        SuggestionStatus.EXPIRED.value,
                        updated.model_dump_json(),
                        row["suggestion_id"],
                    ),
                )
                expired.append(row["suggestion_id"])
        return tuple(expired)

    # -- MemoryArchive ---------------------------------------------------

    def get(self, case_id: str) -> CaseRecord | None:
        row = self._one("SELECT doc_json FROM memory_records WHERE case_id = ?", (case_id,))
        return CaseRecord.model_validate_json(row["doc_json"]) if row else None

    def records(self) -> tuple[CaseRecord, ...]:
        rows = self._all("SELECT doc_json FROM memory_records ORDER BY closed_at, case_id")
        return tuple(CaseRecord.model_validate_json(row["doc_json"]) for row in rows)

    def write(self, record: CaseRecord) -> bool:
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT doc_json FROM memory_records WHERE case_id = ?",
                (record.case_id,),
            ).fetchone()
            if existing is not None:
                stored = CaseRecord.model_validate_json(existing["doc_json"])
                if stored == record:
                    return False
                raise MemoryConflictError(
                    f"memory already contains different facts for case {record.case_id}"
                )
            self._insert_memory(conn, (record,))
            return True

    def recall(
        self,
        *,
        category: Category,
        asset_id: str | None,
        query_text: str,
        as_of: datetime,
    ) -> MemoryRecall:
        return StructuredMemoryRetriever(
            self.records(),
            recurrence_window_days=self._recurrence_window_days,
            same_fault_threshold=self._same_fault_threshold,
            max_case_hits=self._max_case_hits,
        ).recall(
            category=category,
            asset_id=asset_id,
            query_text=query_text,
            as_of=as_of,
        )

    # -- Internal SQL helpers -------------------------------------------

    def _one(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    @staticmethod
    def _ensure_reply_token(conn: sqlite3.Connection, case: Case) -> None:
        owner = conn.execute(
            "SELECT case_id FROM cases WHERE reply_token = ?",
            (case.reply_token,),
        ).fetchone()
        if owner is not None and owner["case_id"] != case.case_id:
            raise StoreInvariantError("reply token already belongs to another case")

    @staticmethod
    def _upsert_case(
        conn: sqlite3.Connection,
        case: Case,
        *,
        version: int,
        insert: bool,
    ) -> None:
        values = (
            case.case_id,
            case.reply_token,
            case.status.value,
            _aware_iso(case.opened_at),
            _aware_iso(case.updated_at),
            _optional_iso(case.next_action_due_at),
            version,
            case.model_dump_json(),
        )
        if insert:
            conn.execute(
                "INSERT INTO cases(case_id, reply_token, status, opened_at, updated_at, "
                "next_action_due_at, version, doc_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        else:
            cursor = conn.execute(
                "UPDATE cases SET reply_token = ?, status = ?, opened_at = ?, updated_at = ?, "
                "next_action_due_at = ?, version = ?, doc_json = ? WHERE case_id = ?",
                (
                    case.reply_token,
                    case.status.value,
                    _aware_iso(case.opened_at),
                    _aware_iso(case.updated_at),
                    _optional_iso(case.next_action_due_at),
                    version,
                    case.model_dump_json(),
                    case.case_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreInvariantError(f"cannot update missing case: {case.case_id}")

    @staticmethod
    def _insert_events(
        conn: sqlite3.Connection,
        events: Sequence[TimelineEvent],
    ) -> None:
        conn.executemany(
            "INSERT INTO timeline(event_id, case_id, at, doc_json) VALUES (?, ?, ?, ?)",
            [
                (event.event_id, event.case_id, _aware_iso(event.at), event.model_dump_json())
                for event in events
            ],
        )

    @staticmethod
    def _insert_audits(
        conn: sqlite3.Connection,
        entries: Sequence[AuditEntry],
    ) -> None:
        conn.executemany(
            "INSERT INTO audits(audit_id, case_id, at, doc_json) VALUES (?, ?, ?, ?)",
            [
                (entry.audit_id, entry.case_id, _aware_iso(entry.at), entry.model_dump_json())
                for entry in entries
            ],
        )

    @staticmethod
    def _insert_outbox(
        conn: sqlite3.Connection,
        items: Sequence[OutboxItem],
    ) -> None:
        conn.executemany(
            "INSERT INTO outbox(outbox_id, case_id, dedup_key, kind, created_at, status, "
            "attempts, last_error_code, failed_at, delivery_started_at, delivered_at, "
            "provider_message_id, doc_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.outbox_id,
                    item.case_id,
                    item.dedup_key,
                    item.kind,
                    _aware_iso(item.created_at),
                    item.status.value,
                    item.attempts,
                    item.last_error_code,
                    _optional_iso(item.failed_at),
                    _optional_iso(item.delivery_started_at),
                    _optional_iso(item.delivered_at),
                    item.provider_message_id,
                    item.model_dump_json(),
                )
                for item in items
            ],
        )

    @staticmethod
    def _insert_artifacts(
        conn: sqlite3.Connection,
        artifacts: Sequence[WorkflowArtifact],
    ) -> None:
        conn.executemany(
            "INSERT INTO artifacts(kind, artifact_id, case_id, created_at, doc_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    artifact.kind,
                    artifact.artifact_id,
                    artifact.case_id,
                    _aware_iso(artifact.created_at),
                    artifact.model_dump_json(),
                )
                for artifact in artifacts
            ],
        )

    @staticmethod
    def _insert_memory(
        conn: sqlite3.Connection,
        records: Sequence[CaseRecord],
    ) -> None:
        for record in records:
            existing = conn.execute(
                "SELECT doc_json FROM memory_records WHERE case_id = ?",
                (record.case_id,),
            ).fetchone()
            if existing is not None:
                stored = CaseRecord.model_validate_json(existing["doc_json"])
                if stored == record:
                    continue
                raise MemoryConflictError(
                    f"memory already contains different facts for case {record.case_id}"
                )
            conn.execute(
                "INSERT INTO memory_records(case_id, category, asset_id, closed_at, doc_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.case_id,
                    record.category.value,
                    record.asset_id,
                    _aware_iso(record.closed_at),
                    record.model_dump_json(),
                ),
            )

    @staticmethod
    def _insert_spend(
        conn: sqlite3.Connection,
        entries: Sequence[SpendEntry],
    ) -> None:
        conn.executemany(
            "INSERT INTO spend_ledger(entry_id, case_id, category, amount, currency, "
            "committed_at, source_audit_id, doc_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    entry.entry_id,
                    entry.case_id,
                    entry.category.value,
                    str(entry.amount),
                    entry.currency,
                    _aware_iso(entry.committed_at),
                    entry.source_audit_id,
                    entry.model_dump_json(),
                )
                for entry in entries
            ],
        )

    @staticmethod
    def _validate_transition(
        *,
        case: Case,
        timeline_events: Sequence[TimelineEvent],
        audit_entries: Sequence[AuditEntry],
        outbox_items: Sequence[OutboxItem],
        artifacts: Sequence[WorkflowArtifact],
        memory_records: Sequence[CaseRecord],
        spend_entries: Sequence[SpendEntry],
    ) -> None:
        if any(event.case_id != case.case_id for event in timeline_events):
            raise StoreInvariantError("timeline event belongs to a different case")
        if any(entry.case_id != case.case_id for entry in audit_entries):
            raise StoreInvariantError("audit entry belongs to a different case")
        if any(item.case_id not in (None, case.case_id) for item in outbox_items):
            raise StoreInvariantError("outbox item belongs to a different case")
        if any(item.case_id not in (None, case.case_id) for item in artifacts):
            raise StoreInvariantError("artifact belongs to a different case")
        if any(record.case_id != case.case_id for record in memory_records):
            raise StoreInvariantError("memory record belongs to a different case")
        if any(entry.case_id != case.case_id for entry in spend_entries):
            raise StoreInvariantError("spend entry belongs to a different case")


def _transition_fingerprint(
    *,
    case: Case,
    new_case: bool,
    timeline_events: Sequence[TimelineEvent],
    audit_entries: Sequence[AuditEntry],
    outbox_items: Sequence[OutboxItem],
    artifacts: Sequence[WorkflowArtifact],
    memory_records: Sequence[CaseRecord],
    spend_entries: Sequence[SpendEntry],
) -> str:
    payload = {
        "case": case.model_dump(mode="json"),
        "new_case": new_case,
        "timeline": [item.model_dump(mode="json") for item in timeline_events],
        "audits": [item.model_dump(mode="json") for item in audit_entries],
        "outbox": [item.model_dump(mode="json") for item in outbox_items],
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "memory": [item.model_dump(mode="json") for item in memory_records],
        "spend": [item.model_dump(mode="json") for item in spend_entries],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("operational-store timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _optional_iso(value: datetime | None) -> str | None:
    return _aware_iso(value) if value is not None else None


def _aware_utc_ts(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("operational-store timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _seconds(value: int):
    from datetime import timedelta

    return timedelta(seconds=value)


def _validated_inference_update(
    job: InferenceJob,
    updates: dict[str, Any],
) -> InferenceJob:
    payload = job.model_dump(mode="python")
    payload.update(updates)
    return InferenceJob.model_validate(payload)


def _safe_sha256(value: str) -> str:
    cleaned = value.strip().lower()
    if len(cleaned) != 64 or any(character not in "0123456789abcdef" for character in cleaned):
        raise ValueError("inference input_sha256 must be a 64-character hex digest")
    return cleaned


def _safe_error_code(value: str) -> str:
    cleaned = value.strip().lower()
    if not cleaned or len(cleaned) > 80:
        raise ValueError("error_code must contain 1 to 80 safe identifier characters")
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.-")
    if any(character not in allowed for character in cleaned):
        raise ValueError("error_code may contain only lowercase letters, digits, '.', '_' or '-'")
    return cleaned
