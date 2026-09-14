"""The archive: what a closed case becomes once it is remembered.

A `CaseRecord` is richer than the live `Case` it came from, because closing a
case is when the parts worth keeping finally exist. What was actually done, why
that vendor was chosen over the others, whether the repair held. The live model
carries state; this one carries experience.

The derived properties on `CaseRecord` are the whole reason the archive is
structured rather than a pile of notes. `first_response_hours` is a subtraction,
not an impression, and `counts_toward_vendor_record` encodes three exclusions
that would otherwise quietly distort every vendor's numbers.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from steward.domain.enums import Category, Urgency
from steward.domain.models import UtcDatetime

__all__ = ["CaseRecord", "QuoteRecord"]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class QuoteRecord(_Record):
    """One company's answer to one request, kept whether or not it won."""

    quote_id: str | None = None
    source_email_message_id: str | None = None
    vendor_id: str
    amount: Decimal
    first_response_at: UtcDatetime | None = Field(
        default=None,
        description="When they answered. None means they never did.",
    )
    earliest_onsite_at: UtcDatetime | None = None
    scope: str = Field(
        default="",
        description=(
            "What they said the price covers. Kept verbatim because the archive's "
            "central lesson is about quotes that were compared on price while "
            "proposing different work."
        ),
    )


class CaseRecord(_Record):
    """A closed case, as remembered."""

    case_id: str
    title: str
    category: Category
    asset_id: str | None = None
    urgency: Urgency = Urgency.NORMAL

    is_planned_maintenance: bool = Field(
        default=False,
        description="Scheduled work. Excluded from failure and response statistics.",
    )
    is_simulated: bool = Field(
        default=False,
        description="True when the outcome was produced by a labelled demo run.",
    )
    outcome_verified: bool = Field(
        default=True,
        description="Whether a resident or manager verified the claimed outcome.",
    )
    is_recall: bool = Field(
        default=False,
        description=(
            "A return visit to a vendor's own failed work. Excluded from job counts "
            "and cost averages, because counting it would credit a company with an "
            "extra cheap job for having got it wrong the first time."
        ),
    )

    opened_at: UtcDatetime
    raised_by: str = ""
    raised_as: str = Field(
        default="",
        description="The message that raised it, kept as written.",
    )
    problem: str = ""

    rfq_sent_at: UtcDatetime | None = None
    vendors_contacted: list[str] = Field(default_factory=list)
    quotes: list[QuoteRecord] = Field(default_factory=list)

    selected_vendor_id: str | None = None
    selection_rationale: str | None = None
    selection_source_ids: list[str] = Field(default_factory=list)

    onsite_at: UtcDatetime | None = None
    resolved_at: UtcDatetime | None = None
    cost: Decimal = Decimal("0")
    currency: str = "USD"

    work_performed: str = ""
    resolution_notes: str = ""
    resolution_source_ids: list[str] = Field(default_factory=list)
    verification_source_ids: list[str] = Field(default_factory=list)

    meeting_held_at: UtcDatetime | None = None
    meeting_decisions: list[str] = Field(default_factory=list)
    meeting_action_items: list[str] = Field(default_factory=list)
    meeting_participant_count: int | None = Field(default=None, ge=0)

    recurrence_of_case_id: str | None = None
    recurred_as_case_id: str | None = Field(
        default=None,
        description="Set when this repair failed and the problem came back.",
    )

    closed_at: UtcDatetime

    # -- derived ---------------------------------------------------------

    def quote_from(self, vendor_id: str) -> QuoteRecord | None:
        for quote in self.quotes:
            if quote.vendor_id == vendor_id:
                return quote
        return None

    def first_response_hours(self, vendor_id: str) -> float | None:
        """Hours between the request going out and this vendor answering."""
        quote = self.quote_from(vendor_id)
        if quote is None or quote.first_response_at is None or self.rfq_sent_at is None:
            return None
        return (quote.first_response_at - self.rfq_sent_at).total_seconds() / 3600.0

    @property
    def hours_to_onsite(self) -> float | None:
        if self.onsite_at is None or self.rfq_sent_at is None:
            return None
        return (self.onsite_at - self.rfq_sent_at).total_seconds() / 3600.0

    @property
    def hours_to_resolution(self) -> float | None:
        if self.resolved_at is None or self.rfq_sent_at is None:
            return None
        return (self.resolved_at - self.rfq_sent_at).total_seconds() / 3600.0

    @property
    def had_vendor(self) -> bool:
        """False for cases resolved administratively, with nothing to procure."""
        return self.selected_vendor_id is not None

    @property
    def counts_toward_vendor_record(self) -> bool:
        """Whether this case should shape the selected vendor's track record.

        Planned maintenance is excluded because a scheduled inspection says
        nothing about how a company handles a fault. Recalls are excluded
        because they are the consequence of a job already counted. Cases with
        no vendor are excluded because there is nobody to attribute them to.
        """
        return self.had_vendor and not self.is_planned_maintenance and not self.is_recall

    @property
    def counts_toward_response_record(self) -> bool:
        """Whether the quotes on this case say anything about response speed.

        Response time is learned from every request a company answered, not only
        from the ones it won, which is how the archive knows Coastline is slow
        even on the jobs it never got.
        """
        return (
            self.rfq_sent_at is not None and not self.is_planned_maintenance and not self.is_recall
        )
