"""Durable, source-traced quote portfolios and authority-bounded decisions.

A portfolio is persisted before any recommendation model is called. The model
can select only an offered quote id and offered source ids. Deterministic code
then reloads the exact quote evidence, re-runs PolicyEngine for that quote, and
atomically records the decision, policy audit, and timeline event. This module
never sends or commits anything and never sets ``Case.accepted_quote_id``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from steward.agents import QuoteExtraction, QuoteRecommendation, QuoteRecommender
from steward.domain.clock import Clock
from steward.domain.enums import (
    TERMINAL_STATUSES,
    ActorType,
    AutonomyLevel,
    CaseStatus,
    Category,
    EventKind,
    QuoteStatus,
)
from steward.domain.models import AuditEntry, Case, Quote, TimelineEvent, UtcDatetime, Vendor
from steward.memory import MemoryRecall, MemoryRelation
from steward.policy import PolicyDecision, PolicyEngine
from steward.procurement.decision import CommitmentDisposition
from steward.procurement.quotes import QuoteEvidenceError, QuoteIngestor
from steward.procurement.replies import (
    QUOTE_ARTIFACT_KIND,
    VENDOR_EMAIL_ARTIFACT_KIND,
    StoredVendorEmail,
)
from steward.store import (
    ConcurrencyConflict,
    IdempotencyConflict,
    OperationalStore,
    WorkflowArtifact,
)

__all__ = [
    "QUOTE_DECISION_ARTIFACT_KIND",
    "QUOTE_PORTFOLIO_ARTIFACT_KIND",
    "DurableQuoteDecision",
    "PortfolioHistoryCase",
    "PortfolioQuote",
    "PortfolioVendorHistory",
    "QuoteDecisionConflictError",
    "QuoteDecisionDisposition",
    "QuoteDecisionError",
    "QuoteDecisionIntegrityError",
    "QuoteDecisionNotReadyError",
    "QuoteDecisionResult",
    "QuoteDecisionService",
    "QuotePortfolio",
    "QuoteRecommendationIntegrityError",
    "StoredPolicyDecision",
]

QUOTE_PORTFOLIO_ARTIFACT_KIND = "quote_portfolio.v1"
QUOTE_DECISION_ARTIFACT_KIND = "quote_decision.v1"
_MAX_PORTFOLIO_QUOTES = 20
_MAX_HISTORY_CASES = 10


class QuoteDecisionError(ValueError):
    """Base class for deterministic durable-decision rejection."""


class QuoteDecisionNotReadyError(QuoteDecisionError):
    """The case does not yet have a safe, complete decision input."""


class QuoteDecisionIntegrityError(QuoteDecisionError):
    """Persisted quote, case, memory, or policy inputs are inconsistent."""


class QuoteRecommendationIntegrityError(QuoteDecisionError):
    """The model named a quote or source outside its closed input set."""


class QuoteDecisionConflictError(QuoteDecisionError):
    """A case already owns a different immutable portfolio or decision."""


class QuoteDecisionDisposition(str, Enum):
    DECISION_RECORDED = "decision_recorded"
    DUPLICATE = "duplicate"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class StoredPolicyDecision(_Record):
    """JSON-safe snapshot of one deterministic PolicyEngine verdict."""

    level: AutonomyLevel
    rule_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    spend_cap: Decimal | None = None
    currency: str = Field(min_length=3, max_length=3)
    allowed_vendor_ids: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()

    @classmethod
    def from_policy(cls, decision: PolicyDecision) -> StoredPolicyDecision:
        return cls(
            level=decision.level,
            rule_id=decision.rule_id,
            reason=decision.reason,
            spend_cap=decision.spend_cap,
            currency=decision.currency,
            allowed_vendor_ids=decision.allowed_vendor_ids,
            constraints=decision.constraints,
        )

    @property
    def may_commit_spend(self) -> bool:
        return self.level.may_commit_spend


class PortfolioQuote(_Record):
    quote_artifact_id: str = Field(min_length=1)
    email_artifact_id: str = Field(min_length=1)
    vendor_name: str = Field(min_length=1, max_length=300)
    vendor_allowlisted: bool
    quote: Quote
    extraction: QuoteExtraction
    policy: StoredPolicyDecision
    source_ids: tuple[str, ...] = Field(min_length=1)


class PortfolioHistoryCase(_Record):
    case_id: str = Field(min_length=1)
    relation: MemoryRelation
    problem: str = Field(default="", max_length=2000)
    work_performed: str = Field(default="", max_length=2000)
    resolution_notes: str = Field(default="", max_length=2000)
    selected_vendor_id: str | None = None
    outcome_verified: bool
    cost: Decimal = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    recurred_as_case_id: str | None = None
    source_ids: tuple[str, ...] = Field(min_length=1)


class PortfolioVendorHistory(_Record):
    vendor_id: str = Field(min_length=1)
    jobs_completed: int = Field(ge=0)
    repeat_failure_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    avg_first_response_hours: float | None = Field(default=None, ge=0.0)
    avg_hours_to_onsite: float | None = Field(default=None, ge=0.0)
    avg_hours_to_resolution: float | None = Field(default=None, ge=0.0)
    avg_cost: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(min_length=3, max_length=3)
    source_case_ids: tuple[str, ...] = ()


class QuotePortfolio(_Record):
    """Immutable model input fixed before the model invocation begins."""

    schema_version: Literal[1] = 1
    previous_portfolio_id: str | None = None
    revision: int = Field(default=1, ge=1)
    portfolio_id: str = Field(min_length=1)
    request_key_sha256: str = Field(min_length=64, max_length=64)
    case_id: str = Field(min_length=1)
    case_title: str = Field(min_length=1, max_length=160)
    category: Category
    asset_id: str | None = None
    currency: str = Field(min_length=3, max_length=3)
    created_at: UtcDatetime
    triage_source_id: str = Field(min_length=1)
    triage_confidence: float = Field(ge=0.0, le=1.0)
    month_to_date_spend: Decimal = Field(ge=0)
    contacted_vendor_ids: tuple[str, ...] = ()
    nonresponding_vendor_ids: tuple[str, ...] = ()
    quotes: tuple[PortfolioQuote, ...] = Field(
        min_length=1,
        max_length=_MAX_PORTFOLIO_QUOTES,
    )
    history_cases: tuple[PortfolioHistoryCase, ...] = Field(
        default=(),
        max_length=_MAX_HISTORY_CASES,
    )
    vendor_history: tuple[PortfolioVendorHistory, ...] = ()
    source_ids: tuple[str, ...] = Field(min_length=1)

    def recommendation_context(self) -> dict[str, Any]:
        """Return bounded JSON data; every prose field remains untrusted data."""
        return {
            "portfolio_id": self.portfolio_id,
            "case": {
                "title": self.case_title,
                "category": self.category.value,
                "asset_id": self.asset_id,
                "currency": self.currency,
            },
            "offered_quote_ids": [item.quote.quote_id for item in self.quotes],
            "allowed_source_ids": list(self.source_ids),
            "quotes": [item.model_dump(mode="json") for item in self.quotes],
            "history_cases": [item.model_dump(mode="json") for item in self.history_cases],
            "vendor_history": [item.model_dump(mode="json") for item in self.vendor_history],
            "nonresponding_vendor_ids": list(self.nonresponding_vendor_ids),
        }


class DurableQuoteDecision(_Record):
    """A recommendation plus the exact deterministic policy result; never a send."""

    schema_version: Literal[1] = 1
    decision_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    selected_quote_artifact_id: str = Field(min_length=1)
    selected_quote: Quote
    recommendation: QuoteRecommendation
    portfolio_policy: StoredPolicyDecision
    decision_policy: StoredPolicyDecision
    commitment_disposition: CommitmentDisposition
    price_premium: Decimal = Field(ge=0)
    triage_confidence: float = Field(ge=0.0, le=1.0)
    month_to_date_spend: Decimal = Field(ge=0)
    created_at: UtcDatetime
    source_ids: tuple[str, ...] = Field(min_length=1)
    model_rationale_is_authority: Literal[False] = False
    commitment_sent: Literal[False] = False


@dataclass(frozen=True, slots=True)
class QuoteDecisionResult:
    disposition: QuoteDecisionDisposition
    case: Case
    portfolio: QuotePortfolio
    decision: DurableQuoteDecision


class QuoteDecisionService:
    """Freeze quote inputs, ask a narrow model, then persist a policy verdict."""

    __slots__ = (
        "_clock",
        "_policy",
        "_recommender",
        "_store",
        "_vendors_by_id",
    )

    def __init__(
        self,
        *,
        store: OperationalStore,
        policy: PolicyEngine,
        recommender: QuoteRecommender,
        vendors: tuple[Vendor, ...],
        clock: Clock,
    ) -> None:
        by_id: dict[str, Vendor] = {}
        for vendor in vendors:
            if vendor.vendor_id in by_id:
                raise ValueError(f"duplicate vendor id: {vendor.vendor_id}")
            by_id[vendor.vendor_id] = vendor.model_copy(deep=True)
        self._store = store
        self._policy = policy
        self._recommender = recommender
        self._vendors_by_id = by_id
        self._clock = clock

    def decide(
        self,
        *,
        case_id: str,
        idempotency_key: str,
    ) -> QuoteDecisionResult:
        safe_case_id = case_id.strip()
        safe_key = idempotency_key.strip()
        if not safe_case_id:
            raise QuoteDecisionNotReadyError("quote decision case_id is blank")
        if not safe_key or len(safe_key) > 500:
            raise QuoteDecisionNotReadyError("quote decision idempotency key is invalid")

        portfolio_id = _stable_id("quote-portfolio", safe_case_id, safe_key)
        decision_id = _stable_id("quote-decision", portfolio_id)
        portfolio_artifact = self._store.artifact(
            QUOTE_PORTFOLIO_ARTIFACT_KIND,
            portfolio_id,
        )
        if portfolio_artifact is None:
            portfolio = self._build_portfolio(
                case_id=safe_case_id,
                portfolio_id=portfolio_id,
                request_key=safe_key,
            )
            previous = self._store.artifacts_for(
                kind=QUOTE_PORTFOLIO_ARTIFACT_KIND, case_id=safe_case_id
            )
            if previous:
                last = max(
                    (QuotePortfolio.model_validate(a.payload) for a in previous),
                    key=lambda p: (p.revision, p.created_at, p.portfolio_id),
                )
                portfolio = portfolio.model_copy(
                    update={
                        "previous_portfolio_id": last.portfolio_id,
                        "revision": last.revision + 1,
                    }
                )
            portfolio = self._persist_portfolio(portfolio)
        else:
            portfolio = self._load_portfolio(
                artifact=portfolio_artifact,
                case_id=safe_case_id,
                portfolio_id=portfolio_id,
                request_key=safe_key,
            )

        existing_decision = self._store.artifact(
            QUOTE_DECISION_ARTIFACT_KIND,
            decision_id,
        )
        if existing_decision is not None:
            decision = self._load_decision(
                artifact=existing_decision,
                portfolio=portfolio,
                decision_id=decision_id,
            )
            current = self._store.get_case(safe_case_id)
            if current is None:
                raise QuoteDecisionIntegrityError("durable decision lost its case")
            return QuoteDecisionResult(
                disposition=QuoteDecisionDisposition.DUPLICATE,
                case=current,
                portfolio=portfolio,
                decision=decision,
            )

        self._require_decidable_case(safe_case_id)
        recommendation = self._recommender.recommend(
            portfolio_id=portfolio.portfolio_id,
            context=portfolio.recommendation_context(),
        )
        self._validate_recommendation(portfolio, recommendation)
        return self._persist_decision(
            portfolio=portfolio,
            recommendation=recommendation,
            decision_id=decision_id,
        )

    def _reject_competing_artifacts(
        self,
        *,
        case_id: str,
        portfolio_id: str,
        decision_id: str,
    ) -> None:
        portfolios = self._store.artifacts_for(
            kind=QUOTE_PORTFOLIO_ARTIFACT_KIND,
            case_id=case_id,
        )
        if any(item.artifact_id != portfolio_id for item in portfolios):
            raise QuoteDecisionConflictError(
                "case already has a different immutable quote portfolio"
            )
        decisions = self._store.artifacts_for(
            kind=QUOTE_DECISION_ARTIFACT_KIND,
            case_id=case_id,
        )
        if any(item.artifact_id != decision_id for item in decisions):
            raise QuoteDecisionConflictError("case already has a different durable quote decision")

    def _build_portfolio(
        self,
        *,
        case_id: str,
        portfolio_id: str,
        request_key: str,
    ) -> QuotePortfolio:
        case = self._require_decidable_case(case_id)
        if not case.source_message_ids:
            raise QuoteDecisionIntegrityError(
                "decision case has no persisted triage source message"
            )
        triage_source_id = case.source_message_ids[0]
        message = self._store.get_message(triage_source_id)
        from steward.operations.clarifications import effective_assessment

        assessment = effective_assessment(self._store, case)
        if message is None or assessment is None or message.case_id != case.case_id:
            raise QuoteDecisionIntegrityError(
                "decision case lost its original message or triage assessment"
            )
        triage = assessment.result
        if triage.category is not case.category or triage.asset_id != case.asset_id:
            raise QuoteDecisionIntegrityError(
                "persisted triage facts do not match the decision case"
            )

        created_at = self._clock.now()
        if created_at < case.opened_at:
            raise QuoteDecisionIntegrityError("quote portfolio cannot predate its case")
        month_spend = self._store.month_to_date_spend(
            case.category,
            as_of=created_at,
            currency=case.currency,
        )
        candidates: list[PortfolioQuote] = []
        superseded = {
            a.payload["previous_quote_id"]
            for a in self._store.artifacts_for(kind="quote.supersession.v1", case_id=case.case_id)
        }
        superseded.update(
            a.payload["quote_id"]
            for a in self._store.artifacts_for(kind="quote.rejection.v1", case_id=case.case_id)
        )
        for artifact in self._store.artifacts_for(
            kind=QUOTE_ARTIFACT_KIND,
            case_id=case.case_id,
        ):
            from steward.operations.quote_completeness import missing_fields

            if artifact.artifact_id in superseded:
                continue
            candidate = self._candidate_from_artifact(
                case=case,
                artifact=artifact,
                triage_confidence=triage.confidence,
                month_to_date_spend=month_spend,
            )
            if missing_fields(artifact.payload):
                continue
            if candidate.quote.received_at > created_at:
                raise QuoteDecisionIntegrityError(
                    "quote portfolio contains a reply from the future"
                )
            if candidate.quote.valid_until is not None and candidate.quote.valid_until < created_at:
                continue
            candidates.append(candidate)

        if not candidates:
            raise QuoteDecisionNotReadyError("case has no active source-validated quote artifacts")
        if len(candidates) > _MAX_PORTFOLIO_QUOTES:
            raise QuoteDecisionNotReadyError("case has too many active quotes to compare safely")
        quote_ids = [item.quote.quote_id for item in candidates]
        if len(quote_ids) != len(set(quote_ids)):
            raise QuoteDecisionIntegrityError("portfolio contains duplicate quote ids")
        vendor_ids = [item.quote.vendor_id for item in candidates]
        if len(vendor_ids) != len(set(vendor_ids)):
            raise QuoteDecisionNotReadyError(
                "multiple active quotes from one vendor require explicit supersession"
            )
        candidates.sort(key=lambda item: (item.quote.received_at, item.quote.quote_id))

        answers = self._store.artifacts_for(kind="intake.answer.v1", case_id=case.case_id)
        query_text = "\n".join(
            value
            for value in (
                message.text,
                case.title,
                triage.rationale,
                *[a.payload["notes"] for a in answers],
            )
            if value.strip()
        )
        recall = self._store.recall(
            category=case.category,
            asset_id=case.asset_id,
            query_text=query_text,
            as_of=created_at,
        )
        if recall.category is not case.category or recall.asset_id != case.asset_id:
            raise QuoteDecisionIntegrityError("memory recall does not belong to the decision case")
        history_cases, vendor_history = self._memory_context(
            recall=recall,
            offered_vendor_ids=frozenset(vendor_ids),
        )
        contacted = tuple(dict.fromkeys(case.contacted_vendor_ids))
        nonresponding = tuple(
            vendor_id for vendor_id in contacted if vendor_id not in set(vendor_ids)
        )
        source_ids = _ordered_unique(
            (triage_source_id, *[a.artifact_id for a in answers]),
            *(candidate.source_ids for candidate in candidates),
            *(item.source_ids for item in history_cases),
            *(item.source_case_ids for item in vendor_history),
            *((candidate.policy.rule_id,) for candidate in candidates),
        )
        return QuotePortfolio(
            portfolio_id=portfolio_id,
            request_key_sha256=_sha256(request_key),
            case_id=case.case_id,
            case_title=case.title,
            category=case.category,
            asset_id=case.asset_id,
            currency=case.currency,
            created_at=created_at,
            triage_source_id=triage_source_id,
            triage_confidence=triage.confidence,
            month_to_date_spend=month_spend,
            contacted_vendor_ids=contacted,
            nonresponding_vendor_ids=nonresponding,
            quotes=tuple(candidates),
            history_cases=history_cases,
            vendor_history=vendor_history,
            source_ids=source_ids,
        )

    def _candidate_from_artifact(
        self,
        *,
        case: Case,
        artifact: WorkflowArtifact,
        triage_confidence: float,
        month_to_date_spend: Decimal,
    ) -> PortfolioQuote:
        if artifact.kind != QUOTE_ARTIFACT_KIND or artifact.case_id != case.case_id:
            raise QuoteDecisionIntegrityError("quote artifact is attached to another case")
        try:
            quote = Quote.model_validate(artifact.payload["quote"])
            extraction = QuoteExtraction.model_validate(artifact.payload["extraction"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QuoteDecisionIntegrityError(
                "quote artifact payload is not structurally valid"
            ) from exc
        if not extraction.has_quote:
            raise QuoteDecisionIntegrityError("quote artifact contains a no-quote extraction")
        if quote.quote_id != artifact.artifact_id or quote.case_id != case.case_id:
            raise QuoteDecisionIntegrityError(
                "quote identity does not match its immutable artifact"
            )
        if quote.status is not QuoteStatus.RECEIVED:
            raise QuoteDecisionNotReadyError("only received quotes may enter a portfolio")
        vendor = self._vendors_by_id.get(quote.vendor_id)
        if vendor is None:
            raise QuoteDecisionIntegrityError("quote references an unknown vendor")
        if case.category not in vendor.categories:
            raise QuoteDecisionIntegrityError("quote vendor does not serve the case category")

        email_artifacts = tuple(
            email
            for source_id in artifact.source_ids
            if (
                email := self._store.artifact(
                    VENDOR_EMAIL_ARTIFACT_KIND,
                    source_id,
                )
            )
            is not None
        )
        if len(email_artifacts) != 1:
            raise QuoteDecisionIntegrityError(
                "quote artifact must reference exactly one persisted vendor email"
            )
        email_artifact = email_artifacts[0]
        try:
            stored_email = StoredVendorEmail.model_validate(email_artifact.payload)
        except ValueError as exc:
            raise QuoteDecisionIntegrityError(
                "quote source email artifact is not structurally valid"
            ) from exc
        if (
            email_artifact.case_id != case.case_id
            or stored_email.case_id != case.case_id
            or stored_email.vendor_id != quote.vendor_id
            or stored_email.source_message_id != quote.source_email_message_id
            or stored_email.received_at != quote.received_at
            or artifact.created_at != quote.received_at
            or email_artifact.artifact_id not in artifact.source_ids
            or quote.source_email_message_id not in artifact.source_ids
        ):
            raise QuoteDecisionIntegrityError(
                "quote artifact is not traceable to its exact routed vendor email"
            )
        body_hash = hashlib.sha256(stored_email.body_text.encode("utf-8")).hexdigest()
        if body_hash != stored_email.body_sha256:
            raise QuoteDecisionIntegrityError("stored vendor email body hash changed")
        if (
            quote.amount != extraction.amount
            or quote.currency != extraction.currency
            or quote.scope != extraction.scope_evidence
            or quote.earliest_onsite_at != extraction.earliest_onsite_at
            or quote.valid_until != extraction.valid_until
        ):
            raise QuoteDecisionIntegrityError("quote fields differ from the persisted extraction")
        try:
            QuoteIngestor._validate_evidence(  # noqa: SLF001 - shared trust boundary
                extraction=extraction,
                body_text=stored_email.body_text,
                requested_currency=case.currency,
                received_at=stored_email.received_at,
            )
        except QuoteEvidenceError as exc:
            raise QuoteDecisionIntegrityError(
                "quote extraction no longer passes verbatim evidence validation"
            ) from exc

        policy = self._policy.authorize_commitment(
            category=case.category,
            triage_confidence=triage_confidence,
            vendor_id=vendor.vendor_id,
            vendor_allowlisted=vendor.allowlisted,
            amount=quote.amount,
            currency=quote.currency,
            month_to_date_spend=month_to_date_spend,
        )
        source_ids = _ordered_unique(
            (
                artifact.artifact_id,
                email_artifact.artifact_id,
                stored_email.source_message_id,
                stored_email.rfq_outbox_id,
                policy.rule_id,
            )
        )
        return PortfolioQuote(
            quote_artifact_id=artifact.artifact_id,
            email_artifact_id=email_artifact.artifact_id,
            vendor_name=vendor.name,
            vendor_allowlisted=vendor.allowlisted,
            quote=quote,
            extraction=extraction,
            policy=StoredPolicyDecision.from_policy(policy),
            source_ids=source_ids,
        )

    @staticmethod
    def _memory_context(
        *,
        recall: MemoryRecall,
        offered_vendor_ids: frozenset[str],
    ) -> tuple[tuple[PortfolioHistoryCase, ...], tuple[PortfolioVendorHistory, ...]]:
        history: list[PortfolioHistoryCase] = []
        for hit in recall.case_hits[:_MAX_HISTORY_CASES]:
            record = hit.record
            source_ids = _ordered_unique(
                (record.case_id,),
                record.selection_source_ids,
                record.resolution_source_ids,
                record.verification_source_ids,
            )
            history.append(
                PortfolioHistoryCase(
                    case_id=record.case_id,
                    relation=hit.relation,
                    problem=_bounded_text(record.problem, 2000),
                    work_performed=_bounded_text(record.work_performed, 2000),
                    resolution_notes=_bounded_text(record.resolution_notes, 2000),
                    selected_vendor_id=record.selected_vendor_id,
                    outcome_verified=record.outcome_verified,
                    cost=record.cost,
                    currency=record.currency,
                    recurred_as_case_id=record.recurred_as_case_id,
                    source_ids=source_ids,
                )
            )

        vendor_history: list[PortfolioVendorHistory] = []
        for evidence in recall.vendor_scorecards:
            card = evidence.scorecard
            if card.vendor_id not in offered_vendor_ids:
                continue
            if card.category is not recall.category:
                raise QuoteDecisionIntegrityError(
                    "vendor scorecard category differs from the decision category"
                )
            vendor_history.append(
                PortfolioVendorHistory(
                    vendor_id=card.vendor_id,
                    jobs_completed=card.jobs_completed,
                    repeat_failure_rate=card.repeat_failure_rate,
                    avg_first_response_hours=card.avg_first_response_hours,
                    avg_hours_to_onsite=card.avg_hours_to_onsite,
                    avg_hours_to_resolution=card.avg_hours_to_resolution,
                    avg_cost=card.avg_cost,
                    currency=card.currency,
                    source_case_ids=evidence.source_case_ids,
                )
            )
        vendor_history.sort(key=lambda item: item.vendor_id)
        return tuple(history), tuple(vendor_history)

    def _persist_portfolio(self, portfolio: QuotePortfolio) -> QuotePortfolio:
        artifact = WorkflowArtifact(
            artifact_id=portfolio.portfolio_id,
            case_id=portfolio.case_id,
            kind=QUOTE_PORTFOLIO_ARTIFACT_KIND,
            created_at=portfolio.created_at,
            source_ids=portfolio.source_ids,
            payload=portfolio.model_dump(mode="json"),
        )
        event = TimelineEvent(
            event_id=_stable_id("event-quote-portfolio", portfolio.portfolio_id),
            case_id=portfolio.case_id,
            at=portfolio.created_at,
            kind=EventKind.PLAN_DRAFTED,
            actor=ActorType.AGENT,
            summary=(
                f"Frozen a source-traced portfolio of {len(portfolio.quotes)} "
                "validated quote(s) before recommendation."
            ),
            refs=[item.quote_artifact_id for item in portfolio.quotes],
            payload={
                "portfolio_id": portfolio.portfolio_id,
                "quote_ids": [item.quote.quote_id for item in portfolio.quotes],
                "recommendation_pending": True,
            },
        )
        for _attempt in range(3):
            existing = self._store.artifact(
                QUOTE_PORTFOLIO_ARTIFACT_KIND,
                portfolio.portfolio_id,
            )
            if existing is not None:
                return self._load_portfolio(
                    artifact=existing,
                    case_id=portfolio.case_id,
                    portfolio_id=portfolio.portfolio_id,
                    request_key_hash=portfolio.request_key_sha256,
                )
            current = self._require_decidable_case(portfolio.case_id)
            version = self._store.case_version(portfolio.case_id)
            if version is None:
                raise QuoteDecisionIntegrityError(
                    "quote portfolio case lost its optimistic version"
                )
            updated = current.model_copy(deep=True)
            updated.updated_at = max(updated.updated_at, portfolio.created_at)
            try:
                self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=(f"quote-portfolio:v1:{portfolio.portfolio_id}"),
                    timeline_events=(event,),
                    artifacts=(artifact,),
                )
                return portfolio
            except (ConcurrencyConflict, IdempotencyConflict):
                persisted = self._store.artifact(
                    QUOTE_PORTFOLIO_ARTIFACT_KIND,
                    portfolio.portfolio_id,
                )
                if persisted is not None:
                    return self._load_portfolio(
                        artifact=persisted,
                        case_id=portfolio.case_id,
                        portfolio_id=portfolio.portfolio_id,
                        request_key_hash=portfolio.request_key_sha256,
                    )
        raise ConcurrencyConflict("quote portfolio persistence exceeded retry limit")

    def _persist_decision(
        self,
        *,
        portfolio: QuotePortfolio,
        recommendation: QuoteRecommendation,
        decision_id: str,
    ) -> QuoteDecisionResult:
        selected = next(
            item
            for item in portfolio.quotes
            if item.quote.quote_id == recommendation.recommended_quote_id
        )
        quote_artifact = self._store.artifact(
            QUOTE_ARTIFACT_KIND,
            selected.quote_artifact_id,
        )
        if quote_artifact is None:
            raise QuoteDecisionIntegrityError("selected source quote artifact disappeared")
        current = self._require_decidable_case(portfolio.case_id)
        current_candidate = self._candidate_from_artifact(
            case=current,
            artifact=quote_artifact,
            triage_confidence=portfolio.triage_confidence,
            month_to_date_spend=portfolio.month_to_date_spend,
        )
        if current_candidate != selected:
            raise QuoteDecisionIntegrityError(
                "selected quote or its source evidence changed after portfolio creation"
            )

        created_at = self._clock.now()
        if created_at < portfolio.created_at:
            raise QuoteDecisionIntegrityError("quote decision cannot predate its portfolio")
        if selected.quote.valid_until is not None and selected.quote.valid_until < created_at:
            raise QuoteDecisionNotReadyError(
                "selected quote expired before the decision could be recorded"
            )
        month_spend = self._store.month_to_date_spend(
            current.category,
            as_of=created_at,
            currency=current.currency,
        )
        vendor = self._vendors_by_id[selected.quote.vendor_id]
        policy = self._policy.authorize_commitment(
            category=current.category,
            triage_confidence=portfolio.triage_confidence,
            vendor_id=vendor.vendor_id,
            vendor_allowlisted=vendor.allowlisted,
            amount=selected.quote.amount,
            currency=selected.quote.currency,
            month_to_date_spend=month_spend,
        )
        disposition = _commitment_disposition(policy)
        cheapest = min(
            (item.quote for item in portfolio.quotes),
            key=lambda quote: (quote.amount, quote.quote_id),
        )
        source_ids = _ordered_unique(
            (portfolio.portfolio_id, selected.quote_artifact_id),
            recommendation.source_ids,
            selected.source_ids,
            (policy.rule_id,),
        )
        decision = DurableQuoteDecision(
            decision_id=decision_id,
            portfolio_id=portfolio.portfolio_id,
            case_id=current.case_id,
            selected_quote_artifact_id=selected.quote_artifact_id,
            selected_quote=selected.quote.model_copy(deep=True),
            recommendation=recommendation,
            portfolio_policy=selected.policy,
            decision_policy=StoredPolicyDecision.from_policy(policy),
            commitment_disposition=disposition,
            price_premium=selected.quote.amount - cheapest.amount,
            triage_confidence=portfolio.triage_confidence,
            month_to_date_spend=month_spend,
            created_at=created_at,
            source_ids=source_ids,
        )
        audit_id = _stable_id("audit-quote-decision", decision_id)
        audit = AuditEntry(
            audit_id=audit_id,
            case_id=current.case_id,
            at=created_at,
            action="evaluate_quote_recommendation",
            autonomy_level=policy.level,
            policy_rule_id=policy.rule_id,
            reason=policy.reason,
            amount=selected.quote.amount,
            vendor_id=selected.quote.vendor_id,
        )
        event = TimelineEvent(
            event_id=_stable_id("event-quote-decision", decision_id),
            case_id=current.case_id,
            at=created_at,
            kind=EventKind.QUOTE_RECOMMENDED,
            actor=ActorType.AGENT,
            summary=(
                f"Recorded quote recommendation {selected.quote.quote_id} from "
                f"{vendor.name}; final-send policy will be checked later. "
                "No approval was requested and no commitment was sent."
            ),
            refs=list(_ordered_unique((decision_id, audit_id), source_ids)),
            payload={
                "decision_id": decision_id,
                "portfolio_id": portfolio.portfolio_id,
                "quote_id": selected.quote.quote_id,
                "vendor_id": selected.quote.vendor_id,
                "amount": format(selected.quote.amount, ".2f"),
                "currency": selected.quote.currency,
                "policy_preview_rule_id": policy.rule_id,
                "commitment_disposition": disposition.value,
                "approval_required_at_send": (
                    disposition is CommitmentDisposition.AWAITING_HUMAN_APPROVAL
                ),
                "final_send_blocked": (disposition is CommitmentDisposition.NOT_AUTHORIZED),
                "approval_requested": False,
                "commitment_authorized": False,
                "model_rationale_is_authority": False,
                "commitment_sent": False,
            },
        )
        artifact = WorkflowArtifact(
            artifact_id=decision_id,
            case_id=current.case_id,
            kind=QUOTE_DECISION_ARTIFACT_KIND,
            created_at=created_at,
            source_ids=decision.source_ids,
            payload=decision.model_dump(mode="json"),
        )

        for _attempt in range(3):
            existing = self._store.artifact(QUOTE_DECISION_ARTIFACT_KIND, decision_id)
            if existing is not None:
                persisted = self._load_decision(
                    artifact=existing,
                    portfolio=portfolio,
                    decision_id=decision_id,
                )
                latest = self._store.get_case(current.case_id)
                if latest is None:
                    raise QuoteDecisionIntegrityError("durable decision lost its case")
                return QuoteDecisionResult(
                    disposition=QuoteDecisionDisposition.DUPLICATE,
                    case=latest,
                    portfolio=portfolio,
                    decision=persisted,
                )
            current = self._require_decidable_case(portfolio.case_id)
            version = self._store.case_version(portfolio.case_id)
            if version is None:
                raise QuoteDecisionIntegrityError("quote decision case lost its optimistic version")
            updated = current.model_copy(deep=True)
            updated.updated_at = max(updated.updated_at, created_at)
            try:
                self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=f"quote-decision:v1:{decision_id}",
                    timeline_events=(event,),
                    audit_entries=(audit,),
                    artifacts=(artifact,),
                )
                return QuoteDecisionResult(
                    disposition=QuoteDecisionDisposition.DECISION_RECORDED,
                    case=updated,
                    portfolio=portfolio,
                    decision=decision,
                )
            except (ConcurrencyConflict, IdempotencyConflict):
                persisted = self._store.artifact(
                    QUOTE_DECISION_ARTIFACT_KIND,
                    decision_id,
                )
                if persisted is not None:
                    loaded = self._load_decision(
                        artifact=persisted,
                        portfolio=portfolio,
                        decision_id=decision_id,
                    )
                    latest = self._store.get_case(portfolio.case_id)
                    if latest is None:
                        raise QuoteDecisionIntegrityError(
                            "durable decision lost its case"
                        ) from None
                    return QuoteDecisionResult(
                        disposition=QuoteDecisionDisposition.DUPLICATE,
                        case=latest,
                        portfolio=portfolio,
                        decision=loaded,
                    )
        raise ConcurrencyConflict("quote decision persistence exceeded retry limit")

    def _load_portfolio(
        self,
        *,
        artifact: WorkflowArtifact,
        case_id: str,
        portfolio_id: str,
        request_key: str | None = None,
        request_key_hash: str | None = None,
    ) -> QuotePortfolio:
        try:
            portfolio = QuotePortfolio.model_validate(artifact.payload)
        except ValueError as exc:
            raise QuoteDecisionIntegrityError(
                "persisted quote portfolio is not structurally valid"
            ) from exc
        expected_hash = request_key_hash or _sha256(request_key or "")
        if (
            artifact.kind != QUOTE_PORTFOLIO_ARTIFACT_KIND
            or artifact.artifact_id != portfolio_id
            or artifact.case_id != case_id
            or portfolio.portfolio_id != portfolio_id
            or portfolio.case_id != case_id
            or portfolio.request_key_sha256 != expected_hash
            or artifact.source_ids != portfolio.source_ids
        ):
            raise QuoteDecisionConflictError(
                "persisted portfolio does not match the idempotent request"
            )
        quote_ids = [item.quote.quote_id for item in portfolio.quotes]
        vendor_ids = [item.quote.vendor_id for item in portfolio.quotes]
        if (
            len(quote_ids) != len(set(quote_ids))
            or len(vendor_ids) != len(set(vendor_ids))
            or any(quote_id not in portfolio.source_ids for quote_id in quote_ids)
        ):
            raise QuoteDecisionIntegrityError(
                "persisted quote portfolio violates closed-set invariants"
            )
        current = self._store.get_case(case_id)
        if current is None:
            raise QuoteDecisionIntegrityError("persisted quote portfolio lost its case")
        if current.category is not portfolio.category or current.asset_id != portfolio.asset_id:
            raise QuoteDecisionIntegrityError(
                "case routing facts changed after portfolio persistence"
            )
        return portfolio

    def _load_decision(
        self,
        *,
        artifact: WorkflowArtifact,
        portfolio: QuotePortfolio,
        decision_id: str,
    ) -> DurableQuoteDecision:
        try:
            decision = DurableQuoteDecision.model_validate(artifact.payload)
        except ValueError as exc:
            raise QuoteDecisionIntegrityError(
                "persisted quote decision is not structurally valid"
            ) from exc
        candidates = {item.quote.quote_id: item for item in portfolio.quotes}
        selected = candidates.get(decision.selected_quote.quote_id)
        if (
            artifact.kind != QUOTE_DECISION_ARTIFACT_KIND
            or artifact.artifact_id != decision_id
            or artifact.case_id != portfolio.case_id
            or decision.decision_id != decision_id
            or decision.portfolio_id != portfolio.portfolio_id
            or decision.case_id != portfolio.case_id
            or decision.recommendation.recommended_quote_id != decision.selected_quote.quote_id
            or selected is None
            or selected.quote_artifact_id != decision.selected_quote_artifact_id
            or selected.quote != decision.selected_quote
            or artifact.source_ids != decision.source_ids
            or decision.selected_quote.quote_id not in decision.recommendation.source_ids
        ):
            raise QuoteDecisionIntegrityError(
                "persisted quote decision violates source or identity invariants"
            )
        return decision

    @staticmethod
    def _validate_recommendation(
        portfolio: QuotePortfolio,
        recommendation: QuoteRecommendation,
    ) -> None:
        offered = {item.quote.quote_id for item in portfolio.quotes}
        if any(
            a.quote_id not in offered or a.quote_id == recommendation.recommended_quote_id
            for a in recommendation.alternatives
        ):
            raise QuoteRecommendationIntegrityError(
                "alternative does not identify an offered non-selected quote"
            )
        if recommendation.recommended_quote_id not in offered:
            raise QuoteRecommendationIntegrityError(
                "model recommended a quote outside the offered portfolio"
            )
        allowed_sources = set(portfolio.source_ids)
        if any(source_id not in allowed_sources for source_id in recommendation.source_ids):
            raise QuoteRecommendationIntegrityError(
                "model cited a source outside the offered portfolio"
            )
        if recommendation.recommended_quote_id not in recommendation.source_ids:
            raise QuoteRecommendationIntegrityError(
                "model recommendation must cite the selected quote id"
            )

    def _require_decidable_case(self, case_id: str) -> Case:
        case = self._store.get_case(case_id)
        if case is None:
            raise QuoteDecisionNotReadyError("unknown quote decision case")
        if case.status in TERMINAL_STATUSES:
            raise QuoteDecisionNotReadyError("terminal case cannot receive a quote decision")
        if case.status is not CaseStatus.QUOTES_RECEIVED:
            raise QuoteDecisionNotReadyError(
                "quote decision requires a case in quotes_received state"
            )
        if case.accepted_quote_id is not None:
            raise QuoteDecisionConflictError("case is already bound to an accepted quote")
        return case


def _commitment_disposition(decision: PolicyDecision) -> CommitmentDisposition:
    if decision.may_commit_spend:
        return CommitmentDisposition.AUTHORIZED_NOT_SENT
    if decision.level is AutonomyLevel.PREPARE_ONLY:
        return CommitmentDisposition.AWAITING_HUMAN_APPROVAL
    return CommitmentDisposition.NOT_AUTHORIZED


def _stable_id(prefix: str, *parts: str) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ordered_unique(*groups) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for value in group:
            if value and value not in seen:
                seen.add(value)
                result.append(value)
    return tuple(result)


def _bounded_text(value: str, limit: int) -> str:
    return value.strip()[:limit]
