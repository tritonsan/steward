"""Restart-safe operational tick: claim inbound, intake, suggest, report due work.

The runner is the recurring heartbeat a scheduler (or the demo clock) invokes.
It is deliberately inbound-only: it claims normalized inbox items under a lease,
feeds them to :class:`IntakeService`, token-completes each claim, refreshes
proactive suggestions from verified memory, and reports the ids of cases whose
next action is due.  It never dispatches the outbox and holds no network client,
so a crash between any two steps is safe to re-run.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from steward.agents.intake import IntakeOutcome, IntakeService
from steward.domain.clock import Clock
from steward.proactive import MaintenanceSuggestion, ProactiveMaintenanceEngine
from steward.store.contracts import (
    FailureDisposition,
    InboxItem,
    OperationalStore,
    StaleLeaseToken,
)

__all__ = ["InboundProcessingFailure", "OperationalRunner", "OperationalTickReport"]


@dataclass(frozen=True, slots=True)
class InboundProcessingFailure:
    """Safe failure metadata; exception text is deliberately never retained."""

    external_id: str
    error_code: str
    attempts: int
    disposition: FailureDisposition


@dataclass(frozen=True, slots=True)
class OperationalTickReport:
    """Everything one tick did, with no side effect left implicit."""

    tick_at: datetime
    processed_external_ids: tuple[str, ...] = ()
    intake_outcomes: tuple[IntakeOutcome, ...] = ()
    due_case_ids: tuple[str, ...] = ()
    suggestions: tuple[MaintenanceSuggestion, ...] = ()
    persisted_suggestion_ids: tuple[str, ...] = ()
    expired_suggestion_ids: tuple[str, ...] = ()
    outbound_enabled: Literal[False] = False
    pending_inbound_remaining: int = 0
    skipped_lost_leases: tuple[str, ...] = field(default_factory=tuple)
    failures: tuple[InboundProcessingFailure, ...] = field(default_factory=tuple)


def _default_token_factory() -> str:
    return f"lease-{uuid4().hex}"


def _default_retryable(exc: Exception) -> bool:
    return not isinstance(exc, (ValueError, TypeError))


def _error_code(exc: Exception) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    cleaned = re.sub(r"[^a-z0-9_.-]", "_", name).strip("_")
    return cleaned[:80] or "inbound_processing_error"


class MissingNormalizedMessage(ValueError):
    """A pending inbox row has no normalized message to process."""


class OperationalRunner:
    """Drive one durable, idempotent, inbound-only operational tick."""

    __slots__ = (
        "_batch_limit",
        "_clock",
        "_intake",
        "_lease_seconds",
        "_max_attempts",
        "_proactive",
        "_retryable",
        "_store",
        "_token_factory",
    )

    def __init__(
        self,
        *,
        store: OperationalStore,
        intake: IntakeService,
        clock: Clock,
        proactive: ProactiveMaintenanceEngine | None = None,
        lease_seconds: int = 300,
        batch_limit: int = 100,
        max_attempts: int = 3,
        retryable: Callable[[Exception], bool] = _default_retryable,
        token_factory: Callable[[], str] = _default_token_factory,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if batch_limit <= 0:
            raise ValueError("batch_limit must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._store = store
        self._intake = intake
        self._clock = clock
        self._proactive = proactive
        self._lease_seconds = lease_seconds
        self._batch_limit = batch_limit
        self._max_attempts = max_attempts
        self._retryable = retryable
        self._token_factory = token_factory

    @property
    def outbound_enabled(self) -> Literal[False]:
        """Structurally guarantees the runner never sends anything outward."""
        return False

    def tick(self, *, due_limit: int = 100) -> OperationalTickReport:
        if due_limit <= 0:
            raise ValueError("due_limit must be positive")
        now = _aware_utc(self._clock.now())

        processed: list[str] = []
        outcomes: list[IntakeOutcome] = []
        skipped: list[str] = []
        failures: list[InboundProcessingFailure] = []

        token = self._token_factory()
        claimed = self._store.claim_inbound(
            token=token,
            now=now,
            lease_seconds=self._lease_seconds,
            limit=self._batch_limit,
            max_attempts=self._max_attempts,
        )
        for item in claimed:
            try:
                outcome = self._process_claim(item=item, token=token, now=now)
            except Exception as exc:  # noqa: BLE001 - isolate one poison item from the batch
                failure = self._fail_claim(
                    item=item,
                    token=token,
                    failed_at=_aware_utc(self._clock.now()),
                    exc=exc,
                )
                if failure is None:
                    skipped.append(item.external_id)
                else:
                    failures.append(failure)
                continue
            if outcome is None:
                skipped.append(item.external_id)
                continue
            outcomes.append(outcome)
            processed.append(item.external_id)

        suggestions, persisted = self._refresh_suggestions()
        expired = self._store.expire_suggestions(now=now)

        due_case_ids = tuple(
            case.case_id for case in self._store.due_cases(now=now, limit=due_limit)
        )
        remaining = len(self._store.pending_inbound(limit=self._batch_limit))

        return OperationalTickReport(
            tick_at=now,
            processed_external_ids=tuple(processed),
            intake_outcomes=tuple(outcomes),
            due_case_ids=due_case_ids,
            suggestions=suggestions,
            persisted_suggestion_ids=persisted,
            expired_suggestion_ids=expired,
            pending_inbound_remaining=remaining,
            skipped_lost_leases=tuple(skipped),
            failures=tuple(failures),
        )

    def _process_claim(
        self,
        *,
        item: InboxItem,
        token: str,
        now: datetime,
    ) -> IntakeOutcome | None:
        if item.message is None:
            raise MissingNormalizedMessage("pending inbox item has no normalized message")
        outcome = self._intake.process(item.message)
        completed = self._safe_complete(item=item, token=token, now=now)
        if not completed:
            return None
        return outcome

    def _fail_claim(
        self,
        *,
        item: InboxItem,
        token: str,
        failed_at: datetime,
        exc: Exception,
    ) -> InboundProcessingFailure | None:
        code = _error_code(exc)
        try:
            disposition = self._store.fail_inbound_claim(
                source=item.source,
                external_id=item.external_id,
                token=token,
                failed_at=failed_at,
                error_code=code,
                retryable=self._retryable(exc),
                max_attempts=self._max_attempts,
            )
        except StaleLeaseToken:
            return None
        return InboundProcessingFailure(
            external_id=item.external_id,
            error_code=code,
            attempts=item.attempts,
            disposition=disposition,
        )

    def _safe_complete(
        self,
        *,
        item: InboxItem,
        token: str,
        now: datetime,
    ) -> bool:
        try:
            self._store.complete_inbound_claim(
                source=item.source,
                external_id=item.external_id,
                token=token,
                now=now,
            )
        except StaleLeaseToken:
            # Another worker reclaimed the lease after it expired; fail closed
            # by dropping this outcome rather than double-completing.
            return False
        return True

    def _refresh_suggestions(
        self,
    ) -> tuple[tuple[MaintenanceSuggestion, ...], tuple[str, ...]]:
        if self._proactive is None:
            return (), ()
        now = _aware_utc(self._clock.now())
        suggestions = self._proactive.suggest(self._store.records(), as_of=now)
        persisted: list[str] = []
        for suggestion in suggestions:
            if self._store.upsert_suggestion(suggestion):
                persisted.append(suggestion.suggestion_id)
        return suggestions, tuple(persisted)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("operational-runner timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
