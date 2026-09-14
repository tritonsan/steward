"""Source-traceable quote comparison and dry-run commitment authorization."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from math import inf
from uuid import uuid4

from steward.domain.clock import Clock
from steward.domain.enums import AutonomyLevel
from steward.domain.models import AuditEntry, Case, Quote, Vendor
from steward.memory import MemoryRecall, MemoryRelation, VendorScorecardEvidence
from steward.policy import PolicyDecision, PolicyEngine

__all__ = [
    "CommitmentDisposition",
    "DecisionReason",
    "DecisionReasonCode",
    "QuoteComparison",
    "QuoteDecisionBuilder",
    "QuoteDecisionPackage",
]


class CommitmentDisposition(str, Enum):
    AUTHORIZED_NOT_SENT = "authorized_not_sent"
    AWAITING_HUMAN_APPROVAL = "awaiting_human_approval"
    NOT_AUTHORIZED = "not_authorized"


class DecisionReasonCode(str, Enum):
    KNOWN_CAUSE_COVERED = "known_cause_covered"
    FAILED_REMEDY_AVOIDED = "failed_remedy_avoided"
    RELIABILITY_RECORD = "reliability_record"
    RESPONSE_RECORD = "response_record"
    PRICE_TRADEOFF = "price_tradeoff"
    POLICY_AUTHORIZATION = "policy_authorization"


@dataclass(frozen=True, slots=True)
class DecisionReason:
    code: DecisionReasonCode
    summary: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuoteComparison:
    quote: Quote
    vendor_name: str
    authorization: PolicyDecision
    addresses_known_cause: bool
    repeats_failed_scope_only: bool
    known_cause_terms: tuple[str, ...]
    failed_scope_terms: tuple[str, ...]
    jobs_completed: int | None
    repeat_failure_rate: float | None
    avg_first_response_hours: float | None
    scorecard_source_ids: tuple[str, ...]
    rank_factors: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class QuoteDecisionPackage:
    case_id: str
    recommended_quote: Quote
    comparisons: tuple[QuoteComparison, ...]
    nonresponding_vendor_ids: tuple[str, ...]
    price_premium: Decimal
    reasons: tuple[DecisionReason, ...]
    commitment_decision: PolicyDecision
    commitment_disposition: CommitmentDisposition
    commitment_audit: AuditEntry
    created_at: datetime

    @property
    def source_ids(self) -> tuple[str, ...]:
        return _ordered_unique(*(reason.source_ids for reason in self.reasons))

    @property
    def commitment_sent(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class _HistoricalScope:
    failed_scope_tokens: frozenset[str]
    known_cause_tokens: frozenset[str]
    source_case_ids: tuple[str, ...]


IdFactory = Callable[[str], str]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


class QuoteDecisionBuilder:
    """Recommend from verified facts, then re-authorize the exact commitment."""

    __slots__ = ("_clock", "_id_factory", "_policy")

    def __init__(
        self,
        *,
        policy: PolicyEngine,
        clock: Clock,
        id_factory: IdFactory = _new_id,
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._id_factory = id_factory

    def build(
        self,
        *,
        case: Case,
        triage_confidence: float,
        quotes: Iterable[Quote],
        vendors: Iterable[Vendor],
        memory: MemoryRecall,
        nonresponding_vendor_ids: Iterable[str] = (),
        month_to_date_spend: Decimal = Decimal("0"),
    ) -> QuoteDecisionPackage:
        if memory.category is not case.category or memory.asset_id != case.asset_id:
            raise ValueError("memory recall does not belong to the decision case")
        vendor_map = _index_vendors(vendors)
        quote_list = tuple(quote.model_copy(deep=True) for quote in quotes)
        if not quote_list:
            raise ValueError("cannot build a decision package without quotes")
        if len({quote.quote_id for quote in quote_list}) != len(quote_list):
            raise ValueError("decision package contains duplicate quote ids")
        if len({quote.vendor_id for quote in quote_list}) != len(quote_list):
            raise ValueError("decision package contains multiple quotes from one vendor")

        historical_scope = _historical_scope(memory)
        card_map = {evidence.scorecard.vendor_id: evidence for evidence in memory.vendor_scorecards}
        comparisons = tuple(
            self._comparison(
                case=case,
                triage_confidence=triage_confidence,
                quote=quote,
                vendor_map=vendor_map,
                evidence=card_map.get(quote.vendor_id),
                historical_scope=historical_scope,
                month_to_date_spend=month_to_date_spend,
            )
            for quote in quote_list
        )
        ranked = tuple(sorted(comparisons, key=lambda comparison: comparison.rank_factors))
        recommended = ranked[0]
        vendor = vendor_map[recommended.quote.vendor_id]

        # This is deliberately recomputed for the exact winning quote. A ranking
        # result is never accepted as authority, even when every comparison was
        # individually checked while building the package.
        commitment = self._policy.authorize_commitment(
            category=case.category,
            triage_confidence=triage_confidence,
            vendor_id=vendor.vendor_id,
            vendor_allowlisted=vendor.allowlisted,
            amount=recommended.quote.amount,
            currency=recommended.quote.currency,
            month_to_date_spend=month_to_date_spend,
        )
        disposition = _commitment_disposition(commitment)
        cheapest = min(quote_list, key=lambda quote: (quote.amount, quote.vendor_id))
        premium = recommended.quote.amount - cheapest.amount
        reasons = _reasons(
            recommended=recommended,
            cheapest=next(item for item in comparisons if item.quote.quote_id == cheapest.quote_id),
            comparisons=comparisons,
            historical_scope=historical_scope,
            commitment=commitment,
            premium=premium,
        )
        created_at = self._clock.now()
        audit = AuditEntry(
            audit_id=self._id_factory("audit"),
            case_id=case.case_id,
            at=created_at,
            action="commit_quote_dry_run",
            autonomy_level=commitment.level,
            policy_rule_id=commitment.rule_id,
            reason=commitment.reason,
            amount=recommended.quote.amount,
            vendor_id=recommended.quote.vendor_id,
        )
        return QuoteDecisionPackage(
            case_id=case.case_id,
            recommended_quote=recommended.quote.model_copy(deep=True),
            comparisons=ranked,
            nonresponding_vendor_ids=tuple(nonresponding_vendor_ids),
            price_premium=premium,
            reasons=reasons,
            commitment_decision=commitment,
            commitment_disposition=disposition,
            commitment_audit=audit,
            created_at=created_at,
        )

    def _comparison(
        self,
        *,
        case: Case,
        triage_confidence: float,
        quote: Quote,
        vendor_map: dict[str, Vendor],
        evidence: VendorScorecardEvidence | None,
        historical_scope: _HistoricalScope,
        month_to_date_spend: Decimal,
    ) -> QuoteComparison:
        if quote.case_id != case.case_id:
            raise ValueError(f"quote {quote.quote_id} belongs to a different case")
        vendor = vendor_map.get(quote.vendor_id)
        if vendor is None:
            raise ValueError(f"quote {quote.quote_id} references an unknown vendor")
        if case.category not in vendor.categories:
            raise ValueError(f"vendor {vendor.vendor_id} does not serve the case category")

        scope_tokens = _scope_tokens(quote.scope)
        cause_overlap = scope_tokens & historical_scope.known_cause_tokens
        failed_overlap = scope_tokens & historical_scope.failed_scope_tokens
        addresses_cause = len(cause_overlap) >= 2
        failed_threshold = max(2, len(historical_scope.failed_scope_tokens) // 2)
        repeats_failed = (
            bool(historical_scope.failed_scope_tokens)
            and len(failed_overlap) >= failed_threshold
            and not addresses_cause
        )
        authorization = self._policy.authorize_commitment(
            category=case.category,
            triage_confidence=triage_confidence,
            vendor_id=vendor.vendor_id,
            vendor_allowlisted=vendor.allowlisted,
            amount=quote.amount,
            currency=quote.currency,
            month_to_date_spend=month_to_date_spend,
        )

        card = evidence.scorecard if evidence is not None else None
        jobs = card.jobs_completed if card is not None else None
        failure_rate = card.repeat_failure_rate if card is not None else None
        response_hours = card.avg_first_response_hours if card is not None else None
        cause_rank = (
            0
            if addresses_cause
            else 2
            if repeats_failed
            else 1
            if historical_scope.known_cause_tokens
            else 0
        )
        rank = (
            0 if authorization.may_commit_spend else 1,
            cause_rank,
            0 if jobs is not None and jobs >= 2 else 1,
            failure_rate if failure_rate is not None else 1.0,
            response_hours if response_hours is not None else inf,
            quote.earliest_onsite_at.timestamp() if quote.earliest_onsite_at else inf,
            quote.amount,
            quote.vendor_id,
        )
        return QuoteComparison(
            quote=quote.model_copy(deep=True),
            vendor_name=vendor.name,
            authorization=authorization,
            addresses_known_cause=addresses_cause,
            repeats_failed_scope_only=repeats_failed,
            known_cause_terms=tuple(sorted(cause_overlap)),
            failed_scope_terms=tuple(sorted(failed_overlap)),
            jobs_completed=jobs,
            repeat_failure_rate=failure_rate,
            avg_first_response_hours=response_hours,
            scorecard_source_ids=(evidence.source_case_ids if evidence else ()),
            rank_factors=rank,
        )


def _commitment_disposition(decision: PolicyDecision) -> CommitmentDisposition:
    if decision.may_commit_spend:
        return CommitmentDisposition.AUTHORIZED_NOT_SENT
    if decision.level is AutonomyLevel.PREPARE_ONLY:
        return CommitmentDisposition.AWAITING_HUMAN_APPROVAL
    return CommitmentDisposition.NOT_AUTHORIZED


def _historical_scope(memory: MemoryRecall) -> _HistoricalScope:
    same_fault = {
        hit.case_id: hit.record
        for hit in memory.case_hits
        if hit.relation is MemoryRelation.SAME_FAULT
    }
    failed_tokens: set[str] = set()
    cause_tokens: set[str] = set()
    source_ids: list[str] = []
    for record in same_fault.values():
        successor_id = record.recurred_as_case_id
        if successor_id is None or successor_id not in same_fault:
            continue
        if record.selected_vendor_id is None:
            continue
        failed_quote = record.quote_from(record.selected_vendor_id)
        if failed_quote is None:
            continue
        successor = same_fault[successor_id]
        current_failed = _scope_tokens(failed_quote.scope)
        failed_tokens.update(current_failed)
        cause_tokens.update(_scope_tokens(successor.work_performed) - current_failed)
        source_ids.extend((record.case_id, successor.case_id))
    return _HistoricalScope(
        failed_scope_tokens=frozenset(failed_tokens),
        known_cause_tokens=frozenset(cause_tokens),
        source_case_ids=_ordered_unique(source_ids),
    )


def _reasons(
    *,
    recommended: QuoteComparison,
    cheapest: QuoteComparison,
    comparisons: tuple[QuoteComparison, ...],
    historical_scope: _HistoricalScope,
    commitment: PolicyDecision,
    premium: Decimal,
) -> tuple[DecisionReason, ...]:
    reasons: list[DecisionReason] = []
    if recommended.addresses_known_cause:
        reasons.append(
            DecisionReason(
                code=DecisionReasonCode.KNOWN_CAUSE_COVERED,
                summary=(
                    f"{recommended.vendor_name}'s scope covers known cause terms: "
                    + ", ".join(recommended.known_cause_terms)
                    + "."
                ),
                source_ids=_ordered_unique(
                    historical_scope.source_case_ids,
                    _quote_sources(recommended.quote),
                ),
            )
        )
    failed_alternatives = [
        comparison
        for comparison in comparisons
        if comparison.repeats_failed_scope_only
        and comparison.quote.quote_id != recommended.quote.quote_id
    ]
    if failed_alternatives:
        alternative = min(failed_alternatives, key=lambda item: item.quote.amount)
        reasons.append(
            DecisionReason(
                code=DecisionReasonCode.FAILED_REMEDY_AVOIDED,
                summary=(
                    f"{alternative.vendor_name}'s lower quote repeats only the prior failed "
                    "remedy and does not cover the recorded cause."
                ),
                source_ids=_ordered_unique(
                    historical_scope.source_case_ids,
                    _quote_sources(alternative.quote),
                ),
            )
        )

    if recommended.jobs_completed is not None:
        rate = recommended.repeat_failure_rate
        rate_text = "unknown" if rate is None else f"{rate:.1%}"
        if (
            cheapest.quote.quote_id != recommended.quote.quote_id
            and cheapest.jobs_completed is not None
        ):
            cheapest_rate = cheapest.repeat_failure_rate
            cheapest_rate_text = "unknown" if cheapest_rate is None else f"{cheapest_rate:.1%}"
            reliability_summary = (
                f"{recommended.vendor_name} has {recommended.jobs_completed} completed "
                f"job(s) and a {rate_text} repeat-failure rate; "
                f"{cheapest.vendor_name} has {cheapest.jobs_completed} job(s) and a "
                f"{cheapest_rate_text} repeat-failure rate."
            )
            reliability_sources = _ordered_unique(
                recommended.scorecard_source_ids,
                cheapest.scorecard_source_ids,
            )
        else:
            reliability_summary = (
                f"{recommended.vendor_name} has {recommended.jobs_completed} completed "
                f"job(s) and a {rate_text} repeat-failure rate in this category."
            )
            reliability_sources = recommended.scorecard_source_ids
        reasons.append(
            DecisionReason(
                code=DecisionReasonCode.RELIABILITY_RECORD,
                summary=reliability_summary,
                source_ids=reliability_sources,
            )
        )
    if recommended.avg_first_response_hours is not None:
        if (
            cheapest.quote.quote_id != recommended.quote.quote_id
            and cheapest.avg_first_response_hours is not None
        ):
            response_summary = (
                f"{recommended.vendor_name}'s historical first response averages "
                f"{recommended.avg_first_response_hours:.2f} hours versus "
                f"{cheapest.avg_first_response_hours:.2f} hours for "
                f"{cheapest.vendor_name}."
            )
            response_sources = _ordered_unique(
                recommended.scorecard_source_ids,
                cheapest.scorecard_source_ids,
            )
        else:
            response_summary = (
                f"{recommended.vendor_name}'s historical first response averages "
                f"{recommended.avg_first_response_hours:.2f} hours."
            )
            response_sources = recommended.scorecard_source_ids
        reasons.append(
            DecisionReason(
                code=DecisionReasonCode.RESPONSE_RECORD,
                summary=response_summary,
                source_ids=response_sources,
            )
        )
    if premium > 0:
        reasons.append(
            DecisionReason(
                code=DecisionReasonCode.PRICE_TRADEOFF,
                summary=(
                    f"The recommendation costs {premium} {recommended.quote.currency} more "
                    f"than {cheapest.vendor_name}'s {cheapest.quote.amount} "
                    f"{cheapest.quote.currency} quote."
                ),
                source_ids=_ordered_unique(
                    _quote_sources(recommended.quote),
                    _quote_sources(cheapest.quote),
                ),
            )
        )
    reasons.append(
        DecisionReason(
            code=DecisionReasonCode.POLICY_AUTHORIZATION,
            summary=commitment.reason,
            source_ids=(commitment.rule_id,),
        )
    )
    return tuple(reasons)


def _quote_sources(quote: Quote) -> tuple[str, ...]:
    values = [quote.quote_id]
    if quote.source_email_message_id:
        values.append(quote.source_email_message_id)
    return tuple(values)


_SCOPE_PATTERN = re.compile(r"[a-z0-9]+")
_SCOPE_STOP = {
    "a",
    "again",
    "all",
    "and",
    "at",
    "be",
    "between",
    "for",
    "from",
    "full",
    "if",
    "in",
    "including",
    "it",
    "of",
    "on",
    "over",
    "rather",
    "the",
    "to",
    "we",
}
_SCOPE_ALIASES = {
    "alignment": "align",
    "aligned": "align",
    "misaligned": "align",
    "brackets": "bracket",
    "corrected": "correct",
    "correction": "correct",
    "rails": "rail",
    "replaced": "replace",
    "replacing": "replace",
    "shoes": "shoe",
}


def _scope_tokens(text: str) -> frozenset[str]:
    values: set[str] = set()
    for raw in _SCOPE_PATTERN.findall(text.casefold()):
        token = _SCOPE_ALIASES.get(raw, raw)
        if token not in _SCOPE_STOP and not token.isdigit():
            values.add(token)
    return frozenset(values)


def _index_vendors(vendors: Iterable[Vendor]) -> dict[str, Vendor]:
    indexed: dict[str, Vendor] = {}
    for vendor in vendors:
        if vendor.vendor_id in indexed:
            raise ValueError(f"duplicate vendor id: {vendor.vendor_id}")
        indexed[vendor.vendor_id] = vendor.model_copy(deep=True)
    return indexed


def _ordered_unique(*groups: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for value in group:
            if value not in seen:
                seen.add(value)
                result.append(value)
    return tuple(result)
