"""Vendor performance, computed.

Nothing in this module asks a language model anything. Every figure it produces
is arithmetic over closed cases, and every scorecard carries the case ids it was
built from, so a number on screen in the console can be opened and checked.

That constraint is not stylistic. The archive's central lesson is that the
cheapest quote was repeatedly the most expensive choice, and a system arguing
that to a management committee has to be able to show the working. "The model
felt this vendor was unreliable" is not an argument anyone should spend money
on. "Two of this vendor's three repairs failed again within ninety days, here
are the four case records" is.

Three exclusions do most of the work and are worth stating plainly.

Planned maintenance never counts toward failure statistics, because a scheduled
inspection reveals nothing about how a company handles a fault.

Recalls never count as jobs. A company returning to fix its own failed work at
no charge would otherwise be rewarded twice: once with an extra job in the
denominator, and once with a zero dragging its average invoice down.

Response times are learned from every request a vendor answered, including the
ones it lost. This is how the archive knows a company is slow even on work it
never won, which is exactly the information a committee comparing two quotes
does not otherwise have.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from steward.domain.enums import Category
from steward.domain.models import VendorScorecard
from steward.memory.records import CaseRecord

__all__ = ["ScorecardKey", "build_scorecards", "scorecard_table"]

ScorecardKey = tuple[str, Category]
"""A scorecard is per vendor *and* per category. A company can be excellent at
one kind of work and unproven at another, and averaging across categories hides
exactly the distinction a decision needs."""

_CENTS = Decimal("0.01")


@dataclass
class _Accumulator:
    response_hours: list[float] = field(default_factory=list)
    onsite_hours: list[float] = field(default_factory=list)
    resolution_hours: list[float] = field(default_factory=list)
    costs: list[Decimal] = field(default_factory=list)
    job_case_ids: list[str] = field(default_factory=list)
    failed_case_ids: list[str] = field(default_factory=list)
    last_engaged_at: datetime | None = None

    def note_engagement(self, at: datetime | None) -> None:
        if at is None:
            return
        if self.last_engaged_at is None or at > self.last_engaged_at:
            self.last_engaged_at = at


def build_scorecards(
    records: Iterable[CaseRecord],
    *,
    computed_at: datetime,
    recurrence_window_days: int = 90,
) -> dict[ScorecardKey, VendorScorecard]:
    """Aggregate closed cases into one scorecard per vendor and category.

    `recurrence_window_days` decides how long after a repair a fresh report of
    the same problem is still that repair's fault. Without a window, a vendor
    would carry a failure forever for work that held for two years, and with too
    generous a window every asset that ever breaks twice condemns whoever
    touched it last.
    """
    by_id: dict[str, CaseRecord] = {record.case_id: record for record in records}
    accumulators: dict[ScorecardKey, _Accumulator] = defaultdict(_Accumulator)
    window = timedelta(days=recurrence_window_days)

    for record in by_id.values():
        # Response speed, learned from every answered request.
        if record.counts_toward_response_record:
            for quote in record.quotes:
                hours = record.first_response_hours(quote.vendor_id)
                if hours is not None:
                    accumulators[(quote.vendor_id, record.category)].response_hours.append(hours)

        # Every engagement, planned or not, updates recency.
        if record.selected_vendor_id is not None:
            accumulators[(record.selected_vendor_id, record.category)].note_engagement(
                record.onsite_at or record.resolved_at or record.closed_at
            )

        if not record.counts_toward_vendor_record:
            continue

        assert record.selected_vendor_id is not None  # noqa: S101 - guaranteed above
        acc = accumulators[(record.selected_vendor_id, record.category)]
        acc.job_case_ids.append(record.case_id)
        acc.costs.append(record.cost)

        if record.hours_to_onsite is not None:
            acc.onsite_hours.append(record.hours_to_onsite)
        if record.hours_to_resolution is not None:
            acc.resolution_hours.append(record.hours_to_resolution)

        if _repair_failed_within_window(record, by_id, window):
            acc.failed_case_ids.append(record.case_id)

    return {
        key: _to_scorecard(key, acc, computed_at=computed_at) for key, acc in accumulators.items()
    }


def _repair_failed_within_window(
    record: CaseRecord,
    by_id: Mapping[str, CaseRecord],
    window: timedelta,
) -> bool:
    """True when the same problem came back soon enough to blame this repair."""
    if record.recurred_as_case_id is None:
        return False

    successor = by_id.get(record.recurred_as_case_id)
    if successor is None:
        # The link points outside the loaded set. Treat it as a failure rather
        # than silently forgiving it; a dangling recurrence is a data problem,
        # and resolving it in the vendor's favour is the wrong default.
        return True

    baseline = record.resolved_at or record.closed_at
    return successor.opened_at - baseline <= window


def _to_scorecard(
    key: ScorecardKey,
    acc: _Accumulator,
    *,
    computed_at: datetime,
) -> VendorScorecard:
    vendor_id, category = key
    jobs = len(acc.job_case_ids)

    return VendorScorecard(
        vendor_id=vendor_id,
        category=category,
        jobs_completed=jobs,
        avg_first_response_hours=_mean(acc.response_hours),
        avg_hours_to_onsite=_mean(acc.onsite_hours),
        avg_hours_to_resolution=_mean(acc.resolution_hours),
        avg_cost=_mean_money(acc.costs),
        repeat_failure_rate=(len(acc.failed_case_ids) / jobs) if jobs else None,
        last_engaged_at=acc.last_engaged_at,
        computed_at=computed_at,
        source_case_ids=sorted(acc.job_case_ids),
    )


def _mean(values: Sequence[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _mean_money(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    total = sum(values, Decimal("0"))
    return (total / Decimal(len(values))).quantize(_CENTS, rounding=ROUND_HALF_UP)


def scorecard_table(
    scorecards: Mapping[ScorecardKey, VendorScorecard],
    category: Category,
) -> list[VendorScorecard]:
    """Scorecards for one category in a stable display order.

    Ordered by repeat failure rate, then response speed, then average cost. This
    is a presentation order and not a recommendation: choosing a vendor is a
    judgement about a specific problem, and a system that collapsed that
    judgement into a single sort key would be making exactly the mistake the
    archive is a record of. A vendor with one clean job sorts well and still has
    a track record of one.
    """
    rows = [card for key, card in scorecards.items() if key[1] is category]
    return sorted(
        rows,
        key=lambda c: (
            c.repeat_failure_rate if c.repeat_failure_rate is not None else 1.0,
            c.avg_first_response_hours if c.avg_first_response_hours is not None else 1e9,
            c.avg_cost if c.avg_cost is not None else Decimal("999999"),
            c.vendor_id,
        ),
    )
