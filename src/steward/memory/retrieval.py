"""Source-traceable retrieval over Steward's structured case archive.

This is the local deterministic retrieval backend. It narrows by category and
asset first, uses lexical symptom overlap only to identify the most likely prior
fault, expands explicit recurrence links, and computes vendor figures through
the existing arithmetic scorecard builder. A future vector backend can implement
``MemoryRetriever`` without changing intake or the evidence contract.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

from steward.domain.enums import Category
from steward.domain.models import VendorScorecard
from steward.memory.records import CaseRecord
from steward.memory.scorecard import build_scorecards, scorecard_table

__all__ = [
    "CaseMemoryHit",
    "MemoryRecall",
    "MemoryRelation",
    "MemoryRetriever",
    "StructuredMemoryRetriever",
    "VendorScorecardEvidence",
]


class MemoryRelation(str, Enum):
    """Why a historical case was returned."""

    SAME_FAULT = "same_fault"
    ASSET_HISTORY = "asset_history"
    CATEGORY_HISTORY = "category_history"


@dataclass(frozen=True, slots=True)
class CaseMemoryHit:
    """One historical record plus deterministic retrieval evidence."""

    record: CaseRecord
    relation: MemoryRelation
    relevance_score: float
    matched_fields: tuple[str, ...] = ()
    matched_terms: tuple[str, ...] = ()

    @property
    def case_id(self) -> str:
        return self.record.case_id


@dataclass(frozen=True, slots=True)
class VendorScorecardEvidence:
    """A deterministic scorecard with metric-specific source case ids."""

    scorecard: VendorScorecard
    job_case_ids: tuple[str, ...]
    response_case_ids: tuple[str, ...]
    engagement_case_ids: tuple[str, ...]
    recurrence_case_ids: tuple[str, ...]

    @property
    def source_case_ids(self) -> tuple[str, ...]:
        return _ordered_unique(
            self.job_case_ids,
            self.response_case_ids,
            self.engagement_case_ids,
            self.recurrence_case_ids,
        )


@dataclass(frozen=True, slots=True)
class MemoryRecall:
    """Everything institutional memory contributes to one new case."""

    category: Category
    asset_id: str | None
    as_of: datetime
    case_hits: tuple[CaseMemoryHit, ...]
    vendor_scorecards: tuple[VendorScorecardEvidence, ...]

    @property
    def related_case_ids(self) -> tuple[str, ...]:
        return tuple(hit.case_id for hit in self.case_hits)

    @property
    def source_case_ids(self) -> tuple[str, ...]:
        return _ordered_unique(
            self.related_case_ids,
            *(evidence.source_case_ids for evidence in self.vendor_scorecards),
        )


@runtime_checkable
class MemoryRetriever(Protocol):
    """Institutional-memory port consumed by intake."""

    def recall(
        self,
        *,
        category: Category,
        asset_id: str | None,
        query_text: str,
        as_of: datetime,
    ) -> MemoryRecall: ...


@dataclass(frozen=True, slots=True)
class _Match:
    score: float
    fields: tuple[str, ...]
    terms: tuple[str, ...]


class StructuredMemoryRetriever:
    """Retrieve from validated closed cases without model-generated facts."""

    __slots__ = (
        "_max_case_hits",
        "_records",
        "_recurrence_window_days",
        "_same_fault_threshold",
    )

    def __init__(
        self,
        records: Iterable[CaseRecord],
        *,
        recurrence_window_days: int = 90,
        same_fault_threshold: float = 0.35,
        max_case_hits: int = 10,
    ) -> None:
        copied = tuple(record.model_copy(deep=True) for record in records)
        ids = [record.case_id for record in copied]
        if len(ids) != len(set(ids)):
            raise ValueError("memory archive contains duplicate case ids")
        if recurrence_window_days <= 0:
            raise ValueError("recurrence_window_days must be positive")
        if not 0.0 <= same_fault_threshold <= 1.0:
            raise ValueError("same_fault_threshold must be between 0 and 1")
        if max_case_hits <= 0:
            raise ValueError("max_case_hits must be positive")

        self._records = copied
        self._recurrence_window_days = recurrence_window_days
        self._same_fault_threshold = same_fault_threshold
        self._max_case_hits = max_case_hits

    def recall(
        self,
        *,
        category: Category,
        asset_id: str | None,
        query_text: str,
        as_of: datetime,
    ) -> MemoryRecall:
        if as_of.tzinfo is None:
            raise ValueError("memory as_of must be timezone-aware")
        as_of = as_of.astimezone(timezone.utc)
        visible_records = tuple(
            record
            for record in self._records
            if record.category is category and record.closed_at <= as_of
        )
        visible_ids = {record.case_id for record in visible_records}
        category_records = tuple(
            _without_hidden_recurrence_links(record, visible_ids) for record in visible_records
        )
        candidates = tuple(
            record for record in category_records if asset_id is None or record.asset_id == asset_id
        )
        matches = {record.case_id: _match_record(query_text, record) for record in candidates}
        hits = self._case_hits(
            candidates,
            matches,
            asset_scoped=asset_id is not None,
        )
        scorecards = build_scorecards(
            category_records,
            computed_at=as_of,
            recurrence_window_days=self._recurrence_window_days,
        )
        evidence = tuple(
            _scorecard_evidence(card, category_records)
            for card in scorecard_table(scorecards, category)
        )
        return MemoryRecall(
            category=category,
            asset_id=asset_id,
            as_of=as_of,
            case_hits=hits,
            vendor_scorecards=evidence,
        )

    def _case_hits(
        self,
        candidates: tuple[CaseRecord, ...],
        matches: dict[str, _Match],
        *,
        asset_scoped: bool,
    ) -> tuple[CaseMemoryHit, ...]:
        if not candidates:
            return ()
        if not asset_scoped:
            selected = sorted(candidates, key=lambda record: record.opened_at, reverse=True)[
                : self._max_case_hits
            ]
            return tuple(
                _hit(record, MemoryRelation.CATEGORY_HISTORY, matches[record.case_id])
                for record in selected
            )

        root = max(
            candidates,
            key=lambda record: (
                matches[record.case_id].score,
                record.opened_at,
                record.case_id,
            ),
        )
        same_fault_ids: set[str] = set()
        if matches[root.case_id].score >= self._same_fault_threshold:
            same_fault_ids = _recurrence_component(root.case_id, candidates)

        same_fault = sorted(
            (record for record in candidates if record.case_id in same_fault_ids),
            key=lambda record: record.opened_at,
        )
        remaining = sorted(
            (record for record in candidates if record.case_id not in same_fault_ids),
            key=lambda record: record.opened_at,
            reverse=True,
        )
        available = max(self._max_case_hits - len(same_fault), 0)
        wider = sorted(remaining[:available], key=lambda record: record.opened_at)

        hits: list[CaseMemoryHit] = []
        for record in same_fault:
            match = matches[record.case_id]
            if record.case_id != root.case_id:
                match = _Match(
                    score=match.score,
                    fields=_ordered_unique(match.fields, ("recurrence_link",)),
                    terms=match.terms,
                )
            hits.append(_hit(record, MemoryRelation.SAME_FAULT, match))
        hits.extend(
            _hit(record, MemoryRelation.ASSET_HISTORY, matches[record.case_id]) for record in wider
        )
        return tuple(hits)


_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "around",
    "as",
    "at",
    "before",
    "block",
    "by",
    "for",
    "from",
    "going",
    "has",
    "have",
    "in",
    "is",
    "it",
    "its",
    "lift",
    "elevator",
    "near",
    "of",
    "on",
    "somewhere",
    "started",
    "that",
    "the",
    "this",
    "to",
    "was",
    "way",
    "with",
}
_TOKEN_ALIASES = {
    "ascent": "up",
    "descend": "down",
    "descent": "down",
    "floors": "floor",
    "grinding": "grind",
    "lifts": "lift",
    "noisy": "noise",
    "returned": "return",
    "returning": "return",
    "shudders": "shudder",
    "shuddering": "shudder",
    "vibrating": "shudder",
    "vibration": "shudder",
    "vibrations": "shudder",
}
_MATCH_FIELDS = ("title", "raised_as", "problem")


def _tokens(text: str) -> frozenset[str]:
    tokens: set[str] = set()
    for raw in _TOKEN_PATTERN.findall(text.casefold()):
        token = _TOKEN_ALIASES.get(raw, raw)
        if token not in _STOP_WORDS:
            tokens.add(token)
    return frozenset(tokens)


def _match_record(query_text: str, record: CaseRecord) -> _Match:
    query = _tokens(query_text)
    if not query:
        return _Match(0.0, (), ())

    best = 0.0
    matched_fields: list[str] = []
    matched_terms: set[str] = set()
    for field_name in _MATCH_FIELDS:
        field_tokens = _tokens(getattr(record, field_name))
        overlap = query & field_tokens
        if not overlap:
            continue
        matched_fields.append(field_name)
        matched_terms.update(overlap)
        score = (2 * len(overlap)) / (len(query) + len(field_tokens))
        best = max(best, score)
    return _Match(
        score=round(best, 4),
        fields=tuple(matched_fields),
        terms=tuple(sorted(matched_terms)),
    )


def _without_hidden_recurrence_links(
    record: CaseRecord,
    visible_ids: set[str],
) -> CaseRecord:
    visible = record.model_copy(deep=True)
    if visible.recurrence_of_case_id not in visible_ids:
        visible.recurrence_of_case_id = None
    if visible.recurred_as_case_id not in visible_ids:
        visible.recurred_as_case_id = None
    return visible


def _recurrence_component(
    root_case_id: str,
    candidates: tuple[CaseRecord, ...],
) -> set[str]:
    by_id = {record.case_id: record for record in candidates}
    found: set[str] = set()
    pending = [root_case_id]
    while pending:
        case_id = pending.pop()
        if case_id in found or case_id not in by_id:
            continue
        found.add(case_id)
        record = by_id[case_id]
        for linked_id in (record.recurrence_of_case_id, record.recurred_as_case_id):
            if linked_id is not None and linked_id not in found:
                pending.append(linked_id)
    return found


def _hit(
    record: CaseRecord,
    relation: MemoryRelation,
    match: _Match,
) -> CaseMemoryHit:
    return CaseMemoryHit(
        record=record.model_copy(deep=True),
        relation=relation,
        relevance_score=match.score,
        matched_fields=match.fields,
        matched_terms=match.terms,
    )


def _scorecard_evidence(
    scorecard: VendorScorecard,
    records: tuple[CaseRecord, ...],
) -> VendorScorecardEvidence:
    vendor_id = scorecard.vendor_id
    job_case_ids = tuple(sorted(scorecard.source_case_ids))
    response_case_ids = tuple(
        sorted(
            record.case_id
            for record in records
            if record.counts_toward_response_record
            and record.first_response_hours(vendor_id) is not None
        )
    )
    engagement_case_ids = tuple(
        sorted(record.case_id for record in records if record.selected_vendor_id == vendor_id)
    )
    recurrence_case_ids = tuple(
        sorted(
            record.recurred_as_case_id
            for record in records
            if record.case_id in job_case_ids and record.recurred_as_case_id is not None
        )
    )
    return VendorScorecardEvidence(
        scorecard=scorecard.model_copy(deep=True),
        job_case_ids=job_case_ids,
        response_case_ids=response_case_ids,
        engagement_case_ids=engagement_case_ids,
        recurrence_case_ids=recurrence_case_ids,
    )


def _ordered_unique(*groups: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for group in groups:
        for value in group:
            if value not in seen:
                seen.add(value)
                ordered.append(value)
    return tuple(ordered)
