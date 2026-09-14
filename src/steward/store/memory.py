"""Case persistence port and a process-local implementation.

The in-memory store is intended for tests and the local demo. ``record_intake``
keeps the message, its triage assessment, the resulting case mutation, and its
audit records in one critical section so a half-recorded intake is not visible.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from steward.domain.models import AuditEntry, Case, ResidentMessage, TimelineEvent

if TYPE_CHECKING:
    from steward.agents.triage import TriageAssessment

__all__ = ["CaseStore", "InMemoryCaseStore", "StoreInvariantError"]


class StoreInvariantError(ValueError):
    """A write would make the intake records internally inconsistent."""


@runtime_checkable
class CaseStore(Protocol):
    """Persistence operations needed by the first intake vertical slice."""

    def get_message(self, message_id: str) -> ResidentMessage | None: ...

    def get_assessment(self, message_id: str) -> TriageAssessment | None: ...

    def get_case(self, case_id: str) -> Case | None: ...

    def case_for_reply_token(self, reply_token: str) -> Case | None: ...

    def list_cases(self) -> tuple[Case, ...]: ...

    def list_open_cases(self) -> tuple[Case, ...]: ...

    def timeline_for(self, case_id: str) -> tuple[TimelineEvent, ...]: ...

    def audit_for(self, case_id: str) -> tuple[AuditEntry, ...]: ...

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
        """Atomically record one processed message; return false when already present."""
        ...


class InMemoryCaseStore:
    """Thread-safe, defensive-copying storage for tests and local execution."""

    __slots__ = (
        "_assessments",
        "_audits",
        "_cases",
        "_lock",
        "_messages",
        "_reply_tokens",
        "_timeline",
    )

    def __init__(self) -> None:
        self._messages: dict[str, ResidentMessage] = {}
        self._assessments: dict[str, TriageAssessment] = {}
        self._cases: dict[str, Case] = {}
        self._reply_tokens: dict[str, str] = {}
        self._timeline: list[TimelineEvent] = []
        self._audits: list[AuditEntry] = []
        self._lock = threading.RLock()

    def get_message(self, message_id: str) -> ResidentMessage | None:
        with self._lock:
            return self._copy(self._messages.get(message_id))

    def get_assessment(self, message_id: str) -> TriageAssessment | None:
        with self._lock:
            return self._copy(self._assessments.get(message_id))

    def get_case(self, case_id: str) -> Case | None:
        with self._lock:
            return self._copy(self._cases.get(case_id))

    def case_for_reply_token(self, reply_token: str) -> Case | None:
        with self._lock:
            case_id = self._reply_tokens.get(reply_token)
            return self._copy(self._cases.get(case_id)) if case_id else None

    def list_cases(self) -> tuple[Case, ...]:
        with self._lock:
            return tuple(self._copy(case) for case in self._cases.values())

    def list_open_cases(self) -> tuple[Case, ...]:
        with self._lock:
            return tuple(self._copy(case) for case in self._cases.values() if case.is_open)

    def timeline_for(self, case_id: str) -> tuple[TimelineEvent, ...]:
        with self._lock:
            return tuple(self._copy(event) for event in self._timeline if event.case_id == case_id)

    def audit_for(self, case_id: str) -> tuple[AuditEntry, ...]:
        with self._lock:
            return tuple(self._copy(entry) for entry in self._audits if entry.case_id == case_id)

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
        self._validate_write(
            message=message,
            assessment=assessment,
            case=case,
            new_case=new_case,
            timeline_events=timeline_events,
            audit_entries=audit_entries,
        )

        with self._lock:
            if message.message_id in self._messages:
                return False

            if case is not None:
                existing = self._cases.get(case.case_id)
                if new_case and existing is not None:
                    raise StoreInvariantError(f"case already exists: {case.case_id}")
                if not new_case and existing is None:
                    raise StoreInvariantError(f"cannot update missing case: {case.case_id}")

                token_owner = self._reply_tokens.get(case.reply_token)
                if token_owner is not None and token_owner != case.case_id:
                    raise StoreInvariantError("reply token already belongs to another case")

            self._messages[message.message_id] = message.model_copy(deep=True)
            self._assessments[message.message_id] = assessment.model_copy(deep=True)
            if case is not None:
                self._cases[case.case_id] = case.model_copy(deep=True)
                self._reply_tokens[case.reply_token] = case.case_id
            self._timeline.extend(event.model_copy(deep=True) for event in timeline_events)
            self._audits.extend(entry.model_copy(deep=True) for entry in audit_entries)
            return True

    @staticmethod
    def _validate_write(
        *,
        message: ResidentMessage,
        assessment: TriageAssessment,
        case: Case | None,
        new_case: bool,
        timeline_events: Sequence[TimelineEvent],
        audit_entries: Sequence[AuditEntry],
    ) -> None:
        if assessment.message_id != message.message_id:
            raise StoreInvariantError("assessment and message ids do not match")
        if case is None:
            if new_case:
                raise StoreInvariantError("new_case requires a case record")
            if message.case_id is not None:
                raise StoreInvariantError("a message cannot name a case that was not stored")
            if timeline_events or audit_entries:
                raise StoreInvariantError("case events require a case record")
            return

        if message.case_id != case.case_id:
            raise StoreInvariantError("message and case ids do not match")
        if message.message_id not in case.source_message_ids:
            raise StoreInvariantError("case does not reference its source message")
        if any(event.case_id != case.case_id for event in timeline_events):
            raise StoreInvariantError("timeline event belongs to a different case")
        if any(entry.case_id != case.case_id for entry in audit_entries):
            raise StoreInvariantError("audit entry belongs to a different case")

    @staticmethod
    def _copy(value):
        return value.model_copy(deep=True) if value is not None else None
