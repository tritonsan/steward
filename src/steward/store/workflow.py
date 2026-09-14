"""Durable work and budget primitives, independent of model invocation.

The SQLite adapter supplies the transaction boundary. Jobs can be inserted
inside the same transaction as the fact that creates them. Completion uses a
fencing token; a worker whose lease expired cannot acknowledge another's work.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from steward.domain.models import UtcDatetime
from steward.store.contracts import ConcurrencyConflict, IdempotencyConflict, StaleLeaseToken


def stable_id(*parts: str) -> str:
    return sha256("\x00".join(parts).encode()).hexdigest()


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    job_id: str
    case_id: str
    kind: str
    source_id: str
    due_at: UtcDatetime
    created_at: UtcDatetime
    expected_version: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    lease_token: str | None = None
    lease_until: UtcDatetime | None = None
    last_error: str | None = None


class HumanTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str
    case_id: str
    kind: str
    title: str
    reason: str
    created_at: UtcDatetime
    due_at: UtcDatetime
    expected_version: int
    allowed_roles: tuple[str, ...] = ("manager",)
    assigned_actor_ids: tuple[str, ...] = ()
    status: str = "open"
    response: dict[str, Any] = Field(default_factory=dict)


class BudgetReservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reservation_id: str
    case_id: str
    quote_id: str
    category: str
    currency: str
    amount: Decimal = Field(gt=0)
    created_at: UtcDatetime
    policy_version: str
    status: str = "reserved"


WORKFLOW_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS workflow_jobs (job_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, "
    "kind TEXT NOT NULL, status TEXT NOT NULL, due_at TEXT NOT NULL, lease_until TEXT, "
    "doc_json TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_due ON workflow_jobs(status, due_at, lease_until)",
    "CREATE TABLE IF NOT EXISTS human_tasks (task_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, "
    "status TEXT NOT NULL, doc_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS budget_reservations (reservation_id TEXT PRIMARY KEY, "
    "case_id TEXT NOT NULL, category TEXT NOT NULL, currency TEXT NOT NULL, "
    "created_at TEXT NOT NULL, status TEXT NOT NULL, doc_json TEXT NOT NULL)",
)


def _iso(value: datetime) -> str:
    from steward.store.sqlite import _aware_iso

    return _aware_iso(value)


class WorkflowStoreMixin:
    """Methods use the owning operational store's lock and SQL transaction."""

    @staticmethod
    def _insert_job(conn, job: WorkflowJob) -> bool:
        previous = conn.execute(
            "SELECT doc_json FROM workflow_jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()
        if previous:
            old = WorkflowJob.model_validate_json(previous["doc_json"])
            if (old.case_id, old.kind, old.source_id, old.payload) != (
                job.case_id,
                job.kind,
                job.source_id,
                job.payload,
            ):
                raise IdempotencyConflict("workflow job key reused for different input")
            return False
        conn.execute(
            "INSERT INTO workflow_jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job.job_id,
                job.case_id,
                job.kind,
                job.status.value,
                _iso(job.due_at),
                _iso(job.lease_until) if job.lease_until else None,
                job.model_dump_json(),
            ),
        )
        return True

    @staticmethod
    def _write_job(conn, job: WorkflowJob) -> None:
        conn.execute(
            "UPDATE workflow_jobs SET status=?, due_at=?, lease_until=?, doc_json=? WHERE job_id=?",
            (
                job.status.value,
                _iso(job.due_at),
                _iso(job.lease_until) if job.lease_until else None,
                job.model_dump_json(),
                job.job_id,
            ),
        )

    def enqueue_job(self, job: WorkflowJob) -> bool:
        with self._transaction() as conn:
            if not conn.execute("SELECT 1 FROM cases WHERE case_id=?", (job.case_id,)).fetchone():
                raise ValueError("workflow job requires an existing case")
            return self._insert_job(conn, job)

    def workflow_jobs(self, case_id: str | None = None) -> tuple[WorkflowJob, ...]:
        sql = "SELECT doc_json FROM workflow_jobs"
        rows = self._all(
            sql + (" WHERE case_id=?" if case_id else "") + " ORDER BY due_at, job_id",
            (case_id,) if case_id else (),
        )
        return tuple(WorkflowJob.model_validate_json(row["doc_json"]) for row in rows)

    def claim_jobs(
        self,
        *,
        now: datetime,
        token: str,
        lease_seconds: int = 300,
        limit: int = 100,
        max_attempts: int = 3,
    ) -> tuple[WorkflowJob, ...]:
        if not token or min(lease_seconds, limit, max_attempts) <= 0:
            raise ValueError("positive claim limits and a token are required")
        claimed = []
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT doc_json FROM workflow_jobs WHERE due_at<=? AND "
                "(status='pending' OR (status='running' AND lease_until<=?)) "
                "ORDER BY due_at, job_id LIMIT ?",
                (_iso(now), _iso(now), limit),
            ).fetchall()
            for row in rows:
                job = WorkflowJob.model_validate_json(row["doc_json"])
                if job.attempts >= max_attempts:
                    failed = job.model_copy(
                        update={
                            "status": JobStatus.FAILED,
                            "lease_token": None,
                            "lease_until": None,
                            "last_error": "attempts_exhausted",
                        }
                    )
                    self._write_job(conn, failed)
                    self._insert_failure_task(conn, failed, now)
                    continue
                owned = job.model_copy(
                    update={
                        "status": JobStatus.RUNNING,
                        "lease_token": token,
                        "lease_until": now + timedelta(seconds=lease_seconds),
                        "attempts": job.attempts + 1,
                    }
                )
                self._write_job(conn, owned)
                claimed.append(owned)
        return tuple(claimed)

    @staticmethod
    def _owned_job(conn, job_id: str, token: str, now: datetime) -> WorkflowJob:
        row = conn.execute(
            "SELECT doc_json FROM workflow_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise StaleLeaseToken("unknown workflow job")
        job = WorkflowJob.model_validate_json(row["doc_json"])
        if (
            job.status != JobStatus.RUNNING
            or job.lease_token != token
            or job.lease_until is None
            or job.lease_until <= now
        ):
            raise StaleLeaseToken("workflow job lease is no longer owned")
        return job

    def renew_job(self, job_id: str, *, token: str, now: datetime, lease_seconds: int = 300):
        if lease_seconds <= 0:
            raise ValueError("positive lease required")
        with self._transaction() as conn:
            job = self._owned_job(conn, job_id, token, now)
            self._write_job(
                conn, job.model_copy(update={"lease_until": now + timedelta(seconds=lease_seconds)})
            )

    def complete_job(
        self, job_id: str, *, token: str, now: datetime, successors: tuple[WorkflowJob, ...] = ()
    ) -> None:
        with self._transaction() as conn:
            job = self._owned_job(conn, job_id, token, now)
            for successor in successors:
                if successor.case_id != job.case_id:
                    raise ValueError("successor must belong to the same case")
                self._insert_job(conn, successor)
            self._write_job(
                conn,
                job.model_copy(
                    update={"status": JobStatus.COMPLETED, "lease_token": None, "lease_until": None}
                ),
            )

    def fail_job(
        self, job_id: str, *, token: str, now: datetime, error_code: str, max_attempts: int = 3
    ) -> None:
        with self._transaction() as conn:
            job = self._owned_job(conn, job_id, token, now)
            failed = job.attempts >= max_attempts
            updated = job.model_copy(
                update={
                    "status": JobStatus.FAILED if failed else JobStatus.PENDING,
                    "lease_token": None,
                    "lease_until": None,
                    "last_error": error_code[:80],
                    "due_at": now + timedelta(seconds=min(300, 10 * 2**job.attempts)),
                }
            )
            self._write_job(conn, updated)
            if failed:
                self._insert_failure_task(conn, updated, now)

    def _insert_failure_task(self, conn, job: WorkflowJob, now: datetime) -> None:
        row = conn.execute("SELECT version FROM cases WHERE case_id=?", (job.case_id,)).fetchone()
        task = HumanTask(
            task_id=stable_id("job-failed", job.job_id),
            case_id=job.case_id,
            kind="workflow_failure",
            title="An action needs your attention",
            reason=f"{job.kind} could not finish after {job.attempts} attempts.",
            created_at=now,
            due_at=now,
            expected_version=int(row["version"]),
        )
        self._insert_task(conn, task)

    @staticmethod
    def _insert_task(conn, task: HumanTask) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO human_tasks VALUES (?, ?, ?, ?)",
            (task.task_id, task.case_id, task.status, task.model_dump_json()),
        )

    def add_human_task(self, task: HumanTask) -> None:
        with self._transaction() as conn:
            self._insert_task(conn, task)

    def human_tasks(self, case_id: str | None = None) -> tuple[HumanTask, ...]:
        rows = self._all(
            "SELECT doc_json FROM human_tasks" + (" WHERE case_id=?" if case_id else ""),
            (case_id,) if case_id else (),
        )
        return tuple(HumanTask.model_validate_json(row["doc_json"]) for row in rows)

    def reserve_budget(
        self,
        reservation: BudgetReservation,
        *,
        expected_version: int,
        monthly_cap: Decimal,
        incident_cap: Decimal,
    ) -> bool:
        """Serialize exact-money budget checks with creation, including pending reservations."""
        with self._transaction() as conn:
            prior = conn.execute(
                "SELECT doc_json FROM budget_reservations WHERE reservation_id=?",
                (reservation.reservation_id,),
            ).fetchone()
            if prior:
                if BudgetReservation.model_validate_json(prior["doc_json"]) != reservation:
                    raise IdempotencyConflict("reservation key reused")
                return False
            row = conn.execute(
                "SELECT version FROM cases WHERE case_id=?", (reservation.case_id,)
            ).fetchone()
            if row is None or int(row["version"]) != expected_version:
                raise ConcurrencyConflict("reservation case version changed")
            if conn.execute(
                "SELECT 1 FROM budget_reservations WHERE case_id=? AND status IN "
                "('reserved','committed')",
                (reservation.case_id,),
            ).fetchone():
                raise ConcurrencyConflict("case already has an active reservation")
            month = _iso(reservation.created_at)[:7]
            rows = conn.execute(
                "SELECT doc_json FROM budget_reservations WHERE category=? "
                "AND currency=? AND status='reserved'",
                (reservation.category, reservation.currency),
            ).fetchall()
            pending = sum(
                (BudgetReservation.model_validate_json(r["doc_json"]).amount for r in rows),
                Decimal(0),
            )
            spent = sum(
                (
                    Decimal(r["amount"])
                    for r in conn.execute(
                        "SELECT amount FROM spend_ledger WHERE category=? AND currency=? "
                        "AND substr(committed_at,1,7)=?",
                        (reservation.category, reservation.currency, month),
                    )
                ),
                Decimal(0),
            )
            if (
                reservation.amount > incident_cap
                or spent + pending + reservation.amount > monthly_cap
            ):
                raise ValueError("budget limit exceeded")
            conn.execute(
                "INSERT INTO budget_reservations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    reservation.reservation_id,
                    reservation.case_id,
                    reservation.category,
                    reservation.currency,
                    _iso(reservation.created_at),
                    reservation.status,
                    reservation.model_dump_json(),
                ),
            )
            return True

    def reservations(self, case_id: str | None = None) -> tuple[BudgetReservation, ...]:
        rows = self._all(
            "SELECT doc_json FROM budget_reservations" + (" WHERE case_id=?" if case_id else ""),
            (case_id,) if case_id else (),
        )
        return tuple(BudgetReservation.model_validate_json(row["doc_json"]) for row in rows)

    def settle_reservation(self, reservation_id: str, *, now: datetime, audit_id: str):
        from steward.domain.enums import Category
        from steward.store.contracts import SpendEntry

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT doc_json FROM budget_reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if row is None:
                raise ValueError("missing reservation")
            reservation = BudgetReservation.model_validate_json(row["doc_json"])
            if reservation.status == "committed":
                return
            if reservation.status != "reserved":
                raise ValueError("reservation is not active")
            entry = SpendEntry(
                entry_id=stable_id("spend", reservation_id),
                case_id=reservation.case_id,
                category=Category(reservation.category),
                currency=reservation.currency,
                amount=reservation.amount,
                committed_at=now,
                source_audit_id=audit_id,
            )
            self._insert_spend(conn, (entry,))
            updated = reservation.model_copy(update={"status": "committed"})
            conn.execute(
                "UPDATE budget_reservations SET status='committed', doc_json=? "
                "WHERE reservation_id=?",
                (updated.model_dump_json(), reservation_id),
            )

    def resolve_human_task(
        self,
        task_id: str,
        *,
        actor_id: str,
        role: str,
        expected_version: int,
        response: dict[str, Any],
    ):
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT doc_json FROM human_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row:
                raise ValueError("unknown human task")
            task = HumanTask.model_validate_json(row["doc_json"])
            if role not in task.allowed_roles or (
                role != "manager"
                and task.assigned_actor_ids
                and actor_id not in task.assigned_actor_ids
            ):
                raise PermissionError("actor cannot resolve this task")
            reply = {**response, "actor_id": actor_id}
            if task.status != "open":
                if task.response != reply:
                    raise IdempotencyConflict("task was already answered differently")
                return task
            version = conn.execute(
                "SELECT version FROM cases WHERE case_id=?", (task.case_id,)
            ).fetchone()
            if version is None or int(version["version"]) != expected_version:
                raise ConcurrencyConflict("case changed; review the current evidence")
            updated = task.model_copy(update={"status": "resolved", "response": reply})
            conn.execute(
                "UPDATE human_tasks SET status='resolved',doc_json=? WHERE task_id=?",
                (updated.model_dump_json(), task_id),
            )
            return updated

    def put_configuration(self, artifact):
        with self._transaction() as conn:
            previous = self.artifact(artifact.kind, artifact.artifact_id)
            if previous is not None:
                if previous != artifact:
                    raise IdempotencyConflict("artifact identity reused with different evidence")
                return
            self._insert_artifacts(conn, (artifact,))

    def reconcile_delivered_order(
        self,
        outbox_id,
        *,
        case_id,
        expected_version,
        actor_id,
        provider_message_id,
        evidence,
        source_id,
        now,
        appointment_id=None,
    ):
        """Record an operator's provider receipt; never retry an uncertain send."""
        from steward.store.contracts import OutboxStatus, WorkflowArtifact

        if not provider_message_id.strip() or not evidence.strip():
            raise ValueError("Provider receipt identifier and reconciliation evidence are required")
        with self._transaction() as conn:
            if self.case_version(case_id) != expected_version:
                raise ConcurrencyConflict("Case changed during reconciliation")
            item = self.outbox_item(outbox_id)
            if (
                item is None
                or item.case_id != case_id
                or (
                    item.payload.get("purpose") != "commitment"
                    if appointment_id is None
                    else (
                        item.outbox_id != stable_id("appointment-accept", appointment_id)
                        or not self.artifact("appointment.proposal.v1", appointment_id)
                        or self.artifact("appointment.proposal.v1", appointment_id).case_id
                        != case_id
                    )
                )
            ):
                raise ValueError("Select the service order for this case")
            if item.status not in (OutboxStatus.AMBIGUOUS, OutboxStatus.DEAD_LETTER):
                raise ConcurrencyConflict("Only an uncertain or failed order can be reconciled")
            artifact = WorkflowArtifact(
                artifact_id=source_id,
                case_id=case_id,
                kind="appointment.reconciliation.v1"
                if appointment_id
                else "order.reconciliation.v1",
                created_at=now,
                source_ids=(outbox_id,),
                payload={
                    "actor_id": actor_id,
                    "provider_message_id": provider_message_id,
                    "evidence": evidence,
                    "outcome": "provider_delivery_confirmed",
                },
            )
            self.put_configuration(artifact)
            updated = item.model_copy(
                update={
                    "status": OutboxStatus.DELIVERED,
                    "delivered_at": now,
                    "provider_message_id": provider_message_id,
                    "last_error_code": None,
                    "failed_at": None,
                }
            )
            conn.execute("DELETE FROM claim_outbox WHERE outbox_id=?", (outbox_id,))
            conn.execute(
                "UPDATE outbox SET status=?,delivered_at=?,provider_message_id=?,"
                "last_error_code=NULL,failed_at=NULL,doc_json=? WHERE outbox_id=?",
                (
                    OutboxStatus.DELIVERED.value,
                    _iso(now),
                    provider_message_id,
                    updated.model_dump_json(),
                    outbox_id,
                ),
            )
