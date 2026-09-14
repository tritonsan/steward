"""Atomic verified closure over the durable operational-store boundary."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from uuid import uuid4

from steward.domain.clock import Clock
from steward.domain.enums import CaseStatus
from steward.domain.models import Case, Quote
from steward.memory import InMemoryMemoryArchive
from steward.operations.resolution import (
    ResolutionDisposition,
    ResolutionEvidence,
    ResolutionIntegrityError,
    ResolutionRecorder,
    ResolutionResult,
)
from steward.procurement import QuoteDecisionPackage, RfqBatch
from steward.store import OperationalStore, TransitionResult

__all__ = ["AtomicClosureResult", "VerifiedClosureCoordinator"]

IdFactory = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class AtomicClosureResult:
    """The prepared resolution plus its durable transition receipt."""

    resolution: ResolutionResult
    transition: TransitionResult


class VerifiedClosureCoordinator:
    """Commit CLOSED case state, timeline, and verified memory atomically.

    ``ResolutionRecorder`` remains the canonical integrity validator and record
    builder.  It writes first to an isolated in-memory staging archive; only the
    operational store transaction can make the case, events, and memory durable.
    """

    __slots__ = ("_clock", "_id_factory", "_store")

    def __init__(
        self,
        *,
        store: OperationalStore,
        clock: Clock,
        id_factory: IdFactory = lambda prefix: f"{prefix}-{uuid4().hex}",
    ) -> None:
        self._store = store
        self._clock = clock
        self._id_factory = id_factory

    def close(
        self,
        *,
        case: Case,
        expected_version: int,
        idempotency_key: str,
        decision: QuoteDecisionPackage,
        rfq_batch: RfqBatch,
        quotes: Iterable[Quote],
        evidence: ResolutionEvidence,
        raised_by: str,
        raised_as: str,
        problem: str | None = None,
    ) -> AtomicClosureResult:
        """Validate and atomically persist one human-verified closure."""
        if case.status is not CaseStatus.RESOLVED:
            raise ResolutionIntegrityError(
                "atomic closure requires a case with confirmed verification"
            )

        existing = self._store.get(case.case_id)
        archive = self._store if existing is not None else InMemoryMemoryArchive()
        resolution = ResolutionRecorder(
            archive=archive,
            clock=self._clock,
            id_factory=self._id_factory,
        ).record(
            case=case,
            decision=decision,
            rfq_batch=rfq_batch,
            quotes=quotes,
            evidence=evidence,
            raised_by=raised_by,
            raised_as=raised_as,
            problem=problem,
        )

        if resolution.disposition is ResolutionDisposition.ALREADY_RECORDED:
            persisted_case = self._store.get_case(case.case_id)
            version = self._store.case_version(case.case_id)
            if persisted_case != resolution.case or version is None:
                raise ResolutionIntegrityError(
                    "verified memory exists without its matching atomic case closure"
                )
            return AtomicClosureResult(
                resolution=resolution,
                transition=TransitionResult(applied=False, version=version),
            )

        transition = self._store.save_transition(
            case=resolution.case,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            timeline_events=resolution.timeline_events,
            memory_records=(resolution.record,),
        )
        return AtomicClosureResult(resolution=resolution, transition=transition)
