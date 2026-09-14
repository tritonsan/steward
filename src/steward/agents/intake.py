"""The first executable Steward vertical slice: message to governed case."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol
from uuid import uuid4

from steward.agents.triage import (
    TriageAction,
    TriageAssessment,
    TriageClassifier,
    TriageIssueCode,
    TriageReconciler,
    TriageReconciliationIssue,
    TriageResult,
)
from steward.domain.clock import Clock
from steward.domain.enums import ActorType, Category, EventKind, group_of
from steward.domain.models import (
    Asset,
    AuditEntry,
    Case,
    ResidentMessage,
    TimelineEvent,
)
from steward.mail.addressing import AddressScheme, new_reply_token
from steward.memory import MemoryRecall, MemoryRelation, MemoryRetriever
from steward.policy import PolicyDecision
from steward.store.memory import CaseStore

__all__ = [
    "IntakeDisposition",
    "IntakeOutcome",
    "IntakeService",
    "TriageIntegrityError",
]


class PolicyEvaluator(Protocol):
    """The authority-free facts intake is allowed to present to policy."""

    def evaluate_intake(
        self,
        *,
        category: Category,
        triage_confidence: float,
        month_to_date_spend: Decimal = Decimal("0"),
    ) -> PolicyDecision: ...


class IntakeDisposition(str, Enum):
    IGNORED = "ignored"
    OPENED = "opened"
    LINKED = "linked"
    ALREADY_PROCESSED = "already_processed"


@dataclass(frozen=True, slots=True)
class IntakeOutcome:
    disposition: IntakeDisposition
    message: ResidentMessage
    triage: TriageResult
    case: Case | None = None
    policy_decision: PolicyDecision | None = None
    memory_recall: MemoryRecall | None = None
    memory_error_type: str | None = None
    reconciliation_attempts: int = 0
    reconciliation_issue_codes: tuple[TriageIssueCode, ...] = ()


class TriageIntegrityError(ValueError):
    """Structured output referred to data outside the supplied triage context."""


IdFactory = Callable[[str], str]
SpendLookup = Callable[[Category, datetime], Decimal]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _zero_spend(_category: Category, _at: datetime) -> Decimal:
    return Decimal("0")


def _memory_event(
    *,
    event_id: str,
    case_id: str,
    at: datetime,
    recall: MemoryRecall | None,
    error_type: str | None,
) -> TimelineEvent:
    if recall is None:
        return TimelineEvent(
            event_id=event_id,
            case_id=case_id,
            at=at,
            kind=EventKind.MEMORY_CONSULTED,
            actor=ActorType.SYSTEM,
            summary="Institutional memory was unavailable; case opening continued safely.",
            payload={"status": "unavailable", "error_type": error_type or "UnknownError"},
        )

    same_fault_count = sum(hit.relation is MemoryRelation.SAME_FAULT for hit in recall.case_hits)
    broader_count = len(recall.case_hits) - same_fault_count
    return TimelineEvent(
        event_id=event_id,
        case_id=case_id,
        at=at,
        kind=EventKind.MEMORY_CONSULTED,
        actor=ActorType.SYSTEM,
        summary=(
            f"Institutional memory linked {same_fault_count} same-fault and "
            f"{broader_count} broader historical case(s), with "
            f"{len(recall.vendor_scorecards)} deterministic vendor scorecard(s)."
        ),
        refs=list(recall.source_case_ids),
        payload={
            "status": "ok",
            "category": recall.category.value,
            "asset_id": recall.asset_id,
            "as_of": recall.as_of.isoformat(),
            "case_hits": [
                {
                    "case_id": hit.case_id,
                    "title": hit.record.title,
                    "relation": hit.relation.value,
                    "relevance_score": hit.relevance_score,
                    "matched_fields": list(hit.matched_fields),
                    "matched_terms": list(hit.matched_terms),
                    "selected_vendor_id": hit.record.selected_vendor_id,
                    "cost": str(hit.record.cost),
                    "currency": hit.record.currency,
                    "recurrence_of_case_id": hit.record.recurrence_of_case_id,
                    "recurred_as_case_id": hit.record.recurred_as_case_id,
                }
                for hit in recall.case_hits
            ],
            "vendor_scorecards": [
                {
                    "vendor_id": evidence.scorecard.vendor_id,
                    "jobs_completed": evidence.scorecard.jobs_completed,
                    "avg_first_response_hours": (evidence.scorecard.avg_first_response_hours),
                    "avg_hours_to_onsite": evidence.scorecard.avg_hours_to_onsite,
                    "avg_hours_to_resolution": (evidence.scorecard.avg_hours_to_resolution),
                    "avg_cost": (
                        str(evidence.scorecard.avg_cost)
                        if evidence.scorecard.avg_cost is not None
                        else None
                    ),
                    "currency": evidence.scorecard.currency,
                    "repeat_failure_rate": evidence.scorecard.repeat_failure_rate,
                    "source_case_ids": list(evidence.source_case_ids),
                    "job_case_ids": list(evidence.job_case_ids),
                    "response_case_ids": list(evidence.response_case_ids),
                    "engagement_case_ids": list(evidence.engagement_case_ids),
                    "recurrence_case_ids": list(evidence.recurrence_case_ids),
                }
                for evidence in recall.vendor_scorecards
            ],
        },
    )


class IntakeService:
    """Classify a message, deduplicate it, and bind a new case to policy."""

    __slots__ = (
        "_assets",
        "_classifier",
        "_clock",
        "_id_factory",
        "_max_reconciliation_attempts",
        "_memory",
        "_policy",
        "_reconciler",
        "_reply_token_factory",
        "_spend_lookup",
        "_store",
    )

    def __init__(
        self,
        *,
        classifier: TriageClassifier,
        store: CaseStore,
        policy: PolicyEvaluator,
        assets: Sequence[Asset],
        clock: Clock,
        memory: MemoryRetriever | None = None,
        reconciler: TriageReconciler | None = None,
        max_reconciliation_attempts: int = 2,
        id_factory: IdFactory = _new_id,
        reply_token_factory: Callable[[], str] = new_reply_token,
        spend_lookup: SpendLookup = _zero_spend,
    ) -> None:
        if max_reconciliation_attempts < 0:
            raise ValueError("max reconciliation attempts must not be negative")
        self._classifier = classifier
        self._reconciler = (
            classifier
            if reconciler is None and isinstance(classifier, TriageReconciler)
            else reconciler
        )
        self._max_reconciliation_attempts = max_reconciliation_attempts
        self._store = store
        self._policy = policy
        self._clock = clock
        self._memory = memory
        self._id_factory = id_factory
        self._reply_token_factory = reply_token_factory
        self._spend_lookup = spend_lookup
        self._assets = self._index_assets(assets)

    def process(self, message: ResidentMessage) -> IntakeOutcome:
        """Process one message idempotently.

        Model or contextual validation failures are raised before anything is
        persisted, allowing the caller to retry or route the message for human
        review without exposing a partially opened case.
        """
        existing = self._store.get_message(message.message_id)
        if existing is not None:
            return self._already_processed(existing)
        if message.case_id is not None:
            raise ValueError("new intake messages must not already name a case")

        candidates = self._store.list_open_cases()
        candidate_map = {case.case_id: case for case in candidates}
        triage, issue_codes = self._classify_with_reconciliation(
            message=message,
            candidates=candidates,
            candidate_map=candidate_map,
        )

        now = self._clock.now()
        assessment = TriageAssessment(
            message_id=message.message_id,
            assessed_at=now,
            result=triage,
            reconciliation_attempts=len(issue_codes),
            reconciliation_issue_codes=issue_codes,
        )

        if triage.action is TriageAction.IGNORE:
            recorded = self._store.record_intake(message=message, assessment=assessment)
            if not recorded:
                return self._already_processed_id(message.message_id)
            return IntakeOutcome(
                disposition=IntakeDisposition.IGNORED,
                message=message.model_copy(deep=True),
                triage=triage,
                reconciliation_attempts=assessment.reconciliation_attempts,
                reconciliation_issue_codes=assessment.reconciliation_issue_codes,
            )

        if triage.action is TriageAction.LINK_EXISTING:
            return self._link_existing(
                message=message,
                assessment=assessment,
                triage=triage,
                target=candidate_map[triage.duplicate_case_id or ""],
                now=now,
            )

        return self._open_case(
            message=message,
            assessment=assessment,
            triage=triage,
            now=now,
        )

    def _open_case(
        self,
        *,
        message: ResidentMessage,
        assessment: TriageAssessment,
        triage: TriageResult,
        now: datetime,
    ) -> IntakeOutcome:
        memory_recall, memory_error_type = self._consult_memory(
            message=message,
            triage=triage,
            now=now,
        )
        month_spend = self._spend_lookup(triage.category, now)
        decision = self._policy.evaluate_intake(
            category=triage.category,
            triage_confidence=triage.confidence,
            month_to_date_spend=month_spend,
        )
        case_id = self._available_case_id()
        reply_token = self._reply_token_factory()
        AddressScheme.require_valid_token(reply_token)
        linked_message = message.model_copy(update={"case_id": case_id}, deep=True)
        case = Case(
            case_id=case_id,
            reply_token=reply_token,
            title=triage.title,
            category=triage.category,
            group=group_of(triage.category),
            urgency=triage.urgency,
            asset_id=triage.asset_id,
            opened_at=now,
            updated_at=now,
            source_message_ids=[message.message_id],
            related_case_ids=(
                list(memory_recall.related_case_ids) if memory_recall is not None else []
            ),
            autonomy_level=decision.level,
            spend_cap=decision.spend_cap,
            currency=decision.currency,
        )
        events = [
            TimelineEvent(
                event_id=self._id_factory("event"),
                case_id=case_id,
                at=now,
                kind=EventKind.CASE_OPENED,
                actor=ActorType.AGENT,
                summary=f"Opened case from resident message {message.message_id}.",
                refs=[message.message_id],
                payload={
                    "category": triage.category.value,
                    "urgency": triage.urgency.value,
                    "asset_id": triage.asset_id,
                    "triage_confidence": triage.confidence,
                    "triage_reconciliation_attempts": (assessment.reconciliation_attempts),
                    "triage_reconciliation_issue_codes": [
                        code.value for code in assessment.reconciliation_issue_codes
                    ],
                },
            )
        ]
        if self._memory is not None:
            events.append(
                _memory_event(
                    event_id=self._id_factory("event"),
                    case_id=case_id,
                    at=now,
                    recall=memory_recall,
                    error_type=memory_error_type,
                )
            )
        events.append(
            TimelineEvent(
                event_id=self._id_factory("event"),
                case_id=case_id,
                at=now,
                kind=EventKind.POLICY_EVALUATED,
                actor=ActorType.SYSTEM,
                summary=decision.reason,
                payload={
                    "policy_rule_id": decision.rule_id,
                    "autonomy_level": decision.level.value,
                    "spend_cap": str(decision.spend_cap) if decision.spend_cap else None,
                    "currency": decision.currency,
                    "allowed_vendor_ids": list(decision.allowed_vendor_ids),
                    "month_to_date_spend": str(month_spend),
                },
            )
        )
        audit = AuditEntry(
            audit_id=self._id_factory("audit"),
            case_id=case_id,
            at=now,
            action="evaluate_intake",
            autonomy_level=decision.level,
            policy_rule_id=decision.rule_id,
            reason=decision.reason,
        )
        recorded = self._store.record_intake(
            message=linked_message,
            assessment=assessment,
            case=case,
            new_case=True,
            timeline_events=tuple(events),
            audit_entries=(audit,),
        )
        if not recorded:
            return self._already_processed_id(message.message_id)
        return IntakeOutcome(
            disposition=IntakeDisposition.OPENED,
            message=linked_message,
            triage=triage,
            case=case,
            policy_decision=decision,
            memory_recall=memory_recall,
            memory_error_type=memory_error_type,
            reconciliation_attempts=assessment.reconciliation_attempts,
            reconciliation_issue_codes=assessment.reconciliation_issue_codes,
        )

    def _consult_memory(
        self,
        *,
        message: ResidentMessage,
        triage: TriageResult,
        now: datetime,
    ) -> tuple[MemoryRecall | None, str | None]:
        if self._memory is None:
            return None, None
        query_text = "\n".join((message.text, triage.title, triage.rationale))
        try:
            recall = self._memory.recall(
                category=triage.category,
                asset_id=triage.asset_id,
                query_text=query_text,
                as_of=now,
            )
            if recall.category is not triage.category or recall.asset_id != triage.asset_id:
                raise ValueError("memory retriever returned a result for a different query")
            return recall, None
        except Exception as exc:
            # Memory improves a case but is never allowed to prevent one opening.
            # Only the exception type is persisted; messages may contain backend
            # details or secrets and do not belong in the case timeline.
            return None, type(exc).__name__

    def _link_existing(
        self,
        *,
        message: ResidentMessage,
        assessment: TriageAssessment,
        triage: TriageResult,
        target: Case,
        now: datetime,
    ) -> IntakeOutcome:
        updated = target.model_copy(deep=True)
        updated.source_message_ids.append(message.message_id)
        updated.updated_at = now
        if triage.urgency > updated.urgency:
            updated.urgency = triage.urgency
        linked_message = message.model_copy(update={"case_id": target.case_id}, deep=True)
        event = TimelineEvent(
            event_id=self._id_factory("event"),
            case_id=target.case_id,
            at=now,
            kind=EventKind.MESSAGES_LINKED,
            actor=ActorType.AGENT,
            summary=f"Linked resident message {message.message_id} to the open case.",
            refs=[message.message_id],
            payload={
                "triage_confidence": triage.confidence,
                "urgency": triage.urgency.value,
                "triage_reconciliation_attempts": assessment.reconciliation_attempts,
                "triage_reconciliation_issue_codes": [
                    code.value for code in assessment.reconciliation_issue_codes
                ],
            },
        )
        recorded = self._store.record_intake(
            message=linked_message,
            assessment=assessment,
            case=updated,
            timeline_events=(event,),
        )
        if not recorded:
            return self._already_processed_id(message.message_id)
        return IntakeOutcome(
            disposition=IntakeDisposition.LINKED,
            message=linked_message,
            triage=triage,
            case=updated,
            reconciliation_attempts=assessment.reconciliation_attempts,
            reconciliation_issue_codes=assessment.reconciliation_issue_codes,
        )

    def _classify_with_reconciliation(
        self,
        *,
        message: ResidentMessage,
        candidates: Sequence[Case],
        candidate_map: Mapping[str, Case],
    ) -> tuple[TriageResult, tuple[TriageIssueCode, ...]]:
        assets = tuple(self._assets.values())
        model_message = message.model_copy(deep=True)
        triage = self._classifier.classify(
            message=model_message,
            assets=assets,
            open_cases=candidates,
        )
        issue_codes: list[TriageIssueCode] = []
        attempt = 0
        while issue := self._context_issue(triage, candidate_map):
            if self._reconciler is None or attempt >= self._max_reconciliation_attempts:
                raise TriageIntegrityError(issue.message)
            issue_codes.append(issue.code)
            attempt += 1
            triage = self._reconciler.reconcile(
                message=model_message.model_copy(deep=True),
                assets=assets,
                open_cases=candidates,
                previous_result=triage,
                issue=issue,
                attempt=attempt,
            )
        return triage, tuple(issue_codes)

    def _context_issue(
        self,
        triage: TriageResult,
        candidates: Mapping[str, Case],
    ) -> TriageReconciliationIssue | None:
        if triage.action is TriageAction.OPEN_CASE and triage.asset_id is None:
            category_assets = tuple(
                asset.asset_id
                for asset in self._assets.values()
                if asset.category is triage.category
            )
            if category_assets and not ({"asset", "location"} & set(triage.missing_information)):
                return TriageReconciliationIssue(
                    code=TriageIssueCode.ASSET_REQUIRED,
                    message=(
                        f"opening category '{triage.category.value}' requires the model "
                        "to select a known asset"
                    ),
                )

        if triage.asset_id is not None:
            asset = self._assets.get(triage.asset_id)
            if asset is None:
                return TriageReconciliationIssue(
                    code=TriageIssueCode.ASSET_NOT_OFFERED,
                    message=f"triage named an asset it was not offered: {triage.asset_id}",
                )
            if asset.category is not triage.category:
                return TriageReconciliationIssue(
                    code=TriageIssueCode.ASSET_CATEGORY_MISMATCH,
                    message="triage category does not match the selected asset",
                )

        if triage.action is not TriageAction.LINK_EXISTING:
            return None
        target = candidates.get(triage.duplicate_case_id or "")
        if target is None:
            return TriageReconciliationIssue(
                code=TriageIssueCode.DUPLICATE_NOT_OFFERED,
                message="triage named a case outside the open candidate set",
            )
        if target.category is not triage.category:
            return TriageReconciliationIssue(
                code=TriageIssueCode.DUPLICATE_CATEGORY_MISMATCH,
                message="triage category does not match the duplicate case",
            )
        if triage.asset_id is not None and target.asset_id != triage.asset_id:
            return TriageReconciliationIssue(
                code=TriageIssueCode.DUPLICATE_ASSET_MISMATCH,
                message="triage asset does not match the duplicate case",
            )
        return None

    def _available_case_id(self) -> str:
        for _ in range(10):
            candidate = self._id_factory("case")
            if self._store.get_case(candidate) is None:
                return candidate
        raise RuntimeError("could not generate a unique case id")

    def _already_processed_id(self, message_id: str) -> IntakeOutcome:
        existing = self._store.get_message(message_id)
        if existing is None:
            raise RuntimeError("message became unavailable during idempotent intake")
        return self._already_processed(existing)

    def _already_processed(self, message: ResidentMessage) -> IntakeOutcome:
        assessment = self._store.get_assessment(message.message_id)
        if assessment is None:
            raise RuntimeError("stored message has no triage assessment")
        case = self._store.get_case(message.case_id) if message.case_id else None
        return IntakeOutcome(
            disposition=IntakeDisposition.ALREADY_PROCESSED,
            message=message,
            triage=assessment.result,
            case=case,
            reconciliation_attempts=assessment.reconciliation_attempts,
            reconciliation_issue_codes=assessment.reconciliation_issue_codes,
        )

    @staticmethod
    def _index_assets(assets: Sequence[Asset]) -> dict[str, Asset]:
        indexed: dict[str, Asset] = {}
        for asset in assets:
            if asset.asset_id in indexed:
                raise ValueError(f"duplicate asset id: {asset.asset_id}")
            indexed[asset.asset_id] = asset.model_copy(deep=True)
        return indexed
