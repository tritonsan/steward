"""Mutable institutional-memory port for local execution and future adapters."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Protocol, runtime_checkable

from steward.domain.enums import Category
from steward.memory.records import CaseRecord
from steward.memory.retrieval import MemoryRecall, MemoryRetriever, StructuredMemoryRetriever

__all__ = [
    "InMemoryMemoryArchive",
    "MemoryArchive",
    "MemoryConflictError",
]


class MemoryConflictError(ValueError):
    """A case id already exists with different remembered facts."""


@runtime_checkable
class MemoryArchive(MemoryRetriever, Protocol):
    """Read/write boundary for validated institutional memory records."""

    def get(self, case_id: str) -> CaseRecord | None: ...

    def records(self) -> tuple[CaseRecord, ...]: ...

    def write(self, record: CaseRecord) -> bool:
        """Append a new record; return false for an identical prior write."""
        ...


class InMemoryMemoryArchive:
    """Thread-safe archive used by tests and the hackathon demo.

    It is intentionally a real read/write implementation rather than a demo
    fixture: callers use the same ``MemoryArchive`` contract that a durable
    database adapter can implement later.
    """

    __slots__ = (
        "_lock",
        "_max_case_hits",
        "_records",
        "_recurrence_window_days",
        "_same_fault_threshold",
    )

    def __init__(
        self,
        records: tuple[CaseRecord, ...] | list[CaseRecord] = (),
        *,
        recurrence_window_days: int = 90,
        same_fault_threshold: float = 0.35,
        max_case_hits: int = 10,
    ) -> None:
        copied = [record.model_copy(deep=True) for record in records]
        # Delegate configuration and duplicate-id validation to the canonical
        # retriever so read and write adapters cannot disagree on the rules.
        StructuredMemoryRetriever(
            copied,
            recurrence_window_days=recurrence_window_days,
            same_fault_threshold=same_fault_threshold,
            max_case_hits=max_case_hits,
        )
        self._records = {record.case_id: record for record in copied}
        self._recurrence_window_days = recurrence_window_days
        self._same_fault_threshold = same_fault_threshold
        self._max_case_hits = max_case_hits
        self._lock = threading.RLock()

    def get(self, case_id: str) -> CaseRecord | None:
        with self._lock:
            record = self._records.get(case_id)
            return record.model_copy(deep=True) if record is not None else None

    def records(self) -> tuple[CaseRecord, ...]:
        with self._lock:
            return tuple(record.model_copy(deep=True) for record in self._records.values())

    def write(self, record: CaseRecord) -> bool:
        candidate = record.model_copy(deep=True)
        with self._lock:
            existing = self._records.get(candidate.case_id)
            if existing is not None:
                if existing == candidate:
                    return False
                raise MemoryConflictError(
                    f"memory already contains different facts for case {candidate.case_id}"
                )
            self._records[candidate.case_id] = candidate
            return True

    def recall(
        self,
        *,
        category: Category,
        asset_id: str | None,
        query_text: str,
        as_of: datetime,
    ) -> MemoryRecall:
        with self._lock:
            snapshot = tuple(record.model_copy(deep=True) for record in self._records.values())
        return StructuredMemoryRetriever(
            snapshot,
            recurrence_window_days=self._recurrence_window_days,
            same_fault_threshold=self._same_fault_threshold,
            max_case_hits=self._max_case_hits,
        ).recall(
            category=category,
            asset_id=asset_id,
            query_text=query_text,
            as_of=as_of,
        )
