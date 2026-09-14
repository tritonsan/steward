"""Durable operational-store contracts shared by local and cloud adapters."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from steward.domain.enums import Category
from steward.domain.models import (
    AuditEntry,
    Case,
    ResidentMessage,
    TimelineEvent,
    UtcDatetime,
)
from steward.memory.archive import MemoryArchive
from steward.memory.records import CaseRecord
from steward.proactive import MaintenanceSuggestion, SuggestionStatus
from steward.store.memory import CaseStore, StoreInvariantError

if TYPE_CHECKING:
    from steward.store.workflow import BudgetReservation, HumanTask, WorkflowJob

__all__ = [
    "ClaimLease",
    "ConcurrencyConflict",
    "FailureDisposition",
    "IdempotencyConflict",
    "InboxItem",
    "InboxStatus",
    "InferenceClaimDisposition",
    "InferenceClaimResult",
    "InferenceJob",
    "InferenceJobStatus",
    "LeaseConflict",
    "OperationalStore",
    "OutboxItem",
    "OutboxStatus",
    "SpendEntry",
    "StaleLeaseToken",
    "TransitionResult",
    "WorkflowArtifact",
]


class ConcurrencyConflict(StoreInvariantError):
    """The case changed after a caller read its optimistic version."""


class IdempotencyConflict(StoreInvariantError):
    """An idempotency key was reused for different facts."""


class LeaseConflict(StoreInvariantError):
    """A lease claim was attempted while another live lease already holds it."""


class StaleLeaseToken(StoreInvariantError):
    """A completion or release presented a token that no longer owns the lease."""


class InboxStatus(str, Enum):
    PENDING = "pending"
    IGNORED = "ignored"
    PROCESSED = "processed"
    DEAD_LETTER = "dead_letter"


class OutboxStatus(str, Enum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    DELIVERED = "delivered"
    DEAD_LETTER = "dead_letter"
    AMBIGUOUS = "ambiguous"


class FailureDisposition(str, Enum):
    """Result of atomically failing a claimed item."""

    RETRY_PENDING = "retry_pending"
    DEAD_LETTERED = "dead_lettered"
    AMBIGUOUS = "ambiguous"


class InferenceJobStatus(str, Enum):
    """Durable state of one fingerprint-bound model invocation."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class InferenceClaimDisposition(str, Enum):
    """Atomic result of trying to own one inference job."""

    CLAIMED = "claimed"
    BUSY = "busy"
    COMPLETED = "completed"
    FAILED = "failed"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class OutboxItem(_Record):
    """An intended side effect, atomically recorded but not necessarily delivered."""

    outbox_id: str = Field(min_length=1)
    case_id: str | None = None
    dedup_key: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: UtcDatetime
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    last_error_code: str | None = None
    failed_at: UtcDatetime | None = None
    delivery_started_at: UtcDatetime | None = None
    delivered_at: UtcDatetime | None = None
    provider_message_id: str | None = None


class WorkflowArtifact(_Record):
    """Immutable source-traced workflow fact such as a verification request."""

    artifact_id: str = Field(min_length=1)
    case_id: str | None = None
    kind: str = Field(min_length=1)
    created_at: UtcDatetime
    source_ids: tuple[str, ...] = ()
    payload: dict[str, Any] = Field(default_factory=dict)


class SpendEntry(_Record):
    """One exact commitment included in deterministic monthly-spend checks."""

    entry_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    category: Category
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    committed_at: UtcDatetime
    source_audit_id: str = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return value.strip().upper()


class InboxItem(_Record):
    """A normalized inbound item; raw provider payload and user ids are omitted."""

    source: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    payload_hash: str = Field(min_length=64, max_length=64)
    received_at: UtcDatetime
    status: InboxStatus
    message: ResidentMessage | None = None
    attempts: int = Field(default=0, ge=0)
    last_error_code: str | None = None
    failed_at: UtcDatetime | None = None


class InferenceJob(_Record):
    """Durable model work whose result can be reused after a worker restart."""

    job_id: str = Field(min_length=1, max_length=300)
    kind: str = Field(min_length=1, max_length=200)
    case_id: str | None = Field(default=None, max_length=300)
    input_sha256: str = Field(min_length=64, max_length=64)
    status: InferenceJobStatus = InferenceJobStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime
    completed_at: UtcDatetime | None = None
    failed_at: UtcDatetime | None = None
    last_error_code: str | None = Field(default=None, max_length=80)
    result_payload: dict[str, Any] | None = None

    @field_validator("job_id", "kind")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("inference job identity must not be blank")
        return cleaned

    @field_validator("case_id")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def _consistent_state(self) -> InferenceJob:
        if self.updated_at < self.created_at:
            raise ValueError("inference job update cannot predate creation")
        if self.status is InferenceJobStatus.COMPLETED:
            if (
                self.completed_at is None
                or self.completed_at < self.created_at
                or self.result_payload is None
                or self.failed_at is not None
                or self.last_error_code is not None
            ):
                raise ValueError("completed inference job has inconsistent facts")
        elif self.status is InferenceJobStatus.FAILED:
            if (
                self.failed_at is None
                or self.failed_at < self.created_at
                or self.last_error_code is None
                or self.completed_at is not None
                or self.result_payload is not None
            ):
                raise ValueError("failed inference job has inconsistent facts")
        elif (
            self.completed_at is not None
            or self.failed_at is not None
            or self.result_payload is not None
        ):
            raise ValueError("pending inference job cannot contain terminal facts")
        return self


class ClaimLease(_Record):
    """A live lease over one inbound, outbox, or inference item."""

    scope: str = Field(min_length=1)
    item_key: str = Field(min_length=1)
    token: str = Field(min_length=1)
    claimed_at: UtcDatetime
    lease_expires_at: UtcDatetime
    attempts: int = Field(ge=1)

    @field_validator("scope")
    @classmethod
    def _known_scope(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned not in ("inbound", "outbox", "inference"):
            raise ValueError("lease scope must be 'inbound', 'outbox', or 'inference'")
        return cleaned


@dataclass(frozen=True, slots=True)
class InferenceClaimResult:
    disposition: InferenceClaimDisposition
    job: InferenceJob
    lease: ClaimLease | None = None

    def __post_init__(self) -> None:
        claimed = self.disposition is InferenceClaimDisposition.CLAIMED
        if claimed != (self.lease is not None):
            raise ValueError("only a claimed inference result may expose a lease")


@dataclass(frozen=True, slots=True)
class TransitionResult:
    applied: bool
    version: int


@runtime_checkable
class OperationalStore(CaseStore, MemoryArchive, Protocol):
    """Atomic persistence boundary for every non-trivial Steward transition."""

    def close(self) -> None: ...

    def atomic(self) -> AbstractContextManager[Any]: ...

    def enqueue_job(self, job: WorkflowJob) -> bool: ...

    def workflow_jobs(self, case_id: str | None = None) -> tuple[WorkflowJob, ...]: ...

    def claim_jobs(
        self,
        *,
        now: datetime,
        token: str,
        lease_seconds: int = 300,
        limit: int = 100,
        max_attempts: int = 3,
    ) -> tuple[WorkflowJob, ...]: ...

    def renew_job(
        self, job_id: str, *, token: str, now: datetime, lease_seconds: int = 300
    ) -> None: ...

    def complete_job(
        self, job_id: str, *, token: str, now: datetime, successors: tuple[WorkflowJob, ...] = ()
    ) -> None: ...

    def fail_job(
        self, job_id: str, *, token: str, now: datetime, error_code: str, max_attempts: int = 3
    ) -> None: ...

    def add_human_task(self, task: HumanTask) -> None: ...

    def human_tasks(self, case_id: str | None = None) -> tuple[HumanTask, ...]: ...

    def resolve_human_task(
        self,
        task_id: str,
        *,
        actor_id: str,
        role: str,
        expected_version: int,
        response: dict[str, Any],
    ) -> HumanTask: ...

    def reserve_budget(
        self,
        reservation: BudgetReservation,
        *,
        expected_version: int,
        monthly_cap: Decimal,
        incident_cap: Decimal,
    ) -> bool: ...

    def reservations(self, case_id: str | None = None) -> tuple[BudgetReservation, ...]: ...

    def settle_reservation(self, reservation_id: str, *, now: datetime, audit_id: str) -> None: ...

    def put_configuration(self, artifact: WorkflowArtifact) -> None: ...

    def case_version(self, case_id: str) -> int | None: ...

    def due_cases(self, *, now: datetime, limit: int = 100) -> tuple[Case, ...]: ...

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
    ) -> TransitionResult: ...

    def artifact(self, kind: str, artifact_id: str) -> WorkflowArtifact | None: ...

    def artifacts_for(
        self,
        *,
        kind: str,
        case_id: str | None = None,
    ) -> tuple[WorkflowArtifact, ...]: ...

    def pending_outbox(self, *, limit: int = 100) -> tuple[OutboxItem, ...]: ...

    def dead_letter_outbox(self, *, limit: int = 100) -> tuple[OutboxItem, ...]: ...

    def outbox_item(self, outbox_id: str) -> OutboxItem | None: ...

    def outbox_for_case(self, case_id: str) -> tuple[OutboxItem, ...]: ...

    def mark_outbox_delivered(self, outbox_id: str, *, delivered_at: datetime) -> bool: ...

    def month_to_date_spend(
        self,
        category: Category,
        *,
        as_of: datetime,
        currency: str,
    ) -> Decimal: ...

    def record_inbound(self, item: InboxItem) -> bool: ...

    def inbox_item(self, source: str, external_id: str) -> InboxItem | None: ...

    def pending_inbound(self, *, limit: int = 100) -> tuple[InboxItem, ...]: ...

    def dead_letter_inbound(self, *, limit: int = 100) -> tuple[InboxItem, ...]: ...

    def mark_inbound_processed(self, source: str, external_id: str) -> bool: ...

    # -- Lease claims ----------------------------------------------------

    def inference_job(self, job_id: str) -> InferenceJob | None: ...

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
    ) -> InferenceClaimResult: ...

    def renew_inference_claim(
        self,
        *,
        job_id: str,
        token: str,
        now: datetime,
        lease_seconds: int = 300,
    ) -> ClaimLease: ...

    def complete_inference_claim(
        self,
        *,
        job_id: str,
        token: str,
        completed_at: datetime,
        result_payload: dict[str, Any],
    ) -> InferenceJob: ...

    def fail_inference_claim(
        self,
        *,
        job_id: str,
        token: str,
        failed_at: datetime,
        error_code: str,
        retryable: bool,
        max_attempts: int,
    ) -> FailureDisposition: ...

    def release_inference_claim(self, *, job_id: str, token: str) -> bool: ...

    def claim_inbound(
        self,
        *,
        token: str,
        now: datetime,
        lease_seconds: int = 300,
        limit: int = 1,
        max_attempts: int = 3,
    ) -> tuple[InboxItem, ...]: ...

    def complete_inbound_claim(
        self,
        *,
        source: str,
        external_id: str,
        token: str,
        now: datetime,
    ) -> bool: ...

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
    ) -> FailureDisposition: ...

    def release_inbound_claim(
        self,
        *,
        source: str,
        external_id: str,
        token: str,
    ) -> bool: ...

    def claim_outbox(
        self,
        *,
        token: str,
        now: datetime,
        lease_seconds: int = 300,
        limit: int = 1,
        max_attempts: int = 3,
        kind: str | None = None,
    ) -> tuple[OutboxItem, ...]: ...

    def begin_outbox_delivery(
        self,
        *,
        outbox_id: str,
        token: str,
        started_at: datetime,
    ) -> bool: ...

    def complete_outbox_claim(
        self,
        *,
        outbox_id: str,
        token: str,
        delivered_at: datetime,
        provider_message_id: str | None = None,
    ) -> bool: ...

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
    ) -> FailureDisposition: ...

    def release_outbox_claim(self, *, outbox_id: str, token: str) -> bool: ...

    # -- Proactive suggestions ------------------------------------------

    def upsert_suggestion(self, suggestion: MaintenanceSuggestion) -> bool: ...

    def list_suggestions(
        self,
        *,
        status: SuggestionStatus | None = None,
        limit: int = 100,
    ) -> tuple[MaintenanceSuggestion, ...]: ...

    def suggestion(self, suggestion_id: str) -> MaintenanceSuggestion | None: ...

    def decide_suggestion(
        self,
        suggestion_id: str,
        *,
        status: SuggestionStatus,
        decided_by: str,
        decided_at: datetime,
    ) -> MaintenanceSuggestion: ...

    def expire_suggestions(self, *, now: datetime) -> tuple[str, ...]: ...
