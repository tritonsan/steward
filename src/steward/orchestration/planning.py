"""Durable, authority-free resolution planning for open cases.

A source snapshot is committed before the model is called.  The model may then
select only an offered resolution path, source id, and vendor id.  No action is
performed and no policy authority is inferred from the recommendation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from steward.agents import ResolutionPlanner, ResolutionPlanRecommendation
from steward.domain.clock import Clock
from steward.domain.enums import (
    TERMINAL_STATUSES,
    ActorType,
    CaseStatus,
    Category,
    EventKind,
    ResolutionPath,
    Urgency,
)
from steward.domain.models import Case, TimelineEvent, UtcDatetime, Vendor
from steward.store import (
    ConcurrencyConflict,
    IdempotencyConflict,
    OperationalStore,
    WorkflowArtifact,
)

__all__ = [
    "RESOLUTION_CONTEXT_ARTIFACT_KIND",
    "RESOLUTION_PLAN_ARTIFACT_KIND",
    "DurableResolutionPlan",
    "ResolutionContext",
    "ResolutionContextMessage",
    "ResolutionPlanningConflictError",
    "ResolutionPlanningDisposition",
    "ResolutionPlanningError",
    "ResolutionPlanningIntegrityError",
    "ResolutionPlanningNotReadyError",
    "ResolutionPlanningResult",
    "ResolutionPlanningService",
    "ResolutionRecommendationIntegrityError",
    "ResolutionVendorOption",
]

RESOLUTION_CONTEXT_ARTIFACT_KIND = "resolution_context.v1"
RESOLUTION_PLAN_ARTIFACT_KIND = "resolution_plan.v1"
_MAX_CONTEXT_MESSAGES = 100
_MAX_VENDOR_OPTIONS = 100


class ResolutionPlanningError(ValueError):
    """Base class for deterministic resolution-planning rejection."""


class ResolutionPlanningNotReadyError(ResolutionPlanningError):
    """The case does not have a complete safe planning input."""


class ResolutionPlanningIntegrityError(ResolutionPlanningError):
    """Persisted case, message, or planning facts are inconsistent."""


class ResolutionRecommendationIntegrityError(ResolutionPlanningError):
    """The model escaped the offered path, source, or vendor set."""


class ResolutionPlanningConflictError(ResolutionPlanningError):
    """A case already owns a different immutable planning snapshot."""


class ResolutionPlanningDisposition(str, Enum):
    PLAN_RECORDED = "plan_recorded"
    DUPLICATE = "duplicate"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class ResolutionContextMessage(_Record):
    message_id: str = Field(min_length=1, max_length=2000)
    sender_display: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=4096)
    sent_at: UtcDatetime


class ResolutionVendorOption(_Record):
    vendor_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    categories: tuple[Category, ...]
    allowlisted: bool
    simulated: bool


class ResolutionContext(_Record):
    """Immutable model input committed before semantic planning."""

    schema_version: Literal[1] = 1
    context_id: str = Field(min_length=1)
    request_key_sha256: str = Field(min_length=64, max_length=64)
    case_id: str = Field(min_length=1)
    case_title: str = Field(min_length=1, max_length=160)
    category: Category
    urgency: Urgency
    asset_id: str | None = None
    created_at: UtcDatetime
    allowed_paths: tuple[ResolutionPath, ...] = Field(min_length=1)
    messages: tuple[ResolutionContextMessage, ...] = Field(
        min_length=1,
        max_length=_MAX_CONTEXT_MESSAGES,
    )
    vendor_options: tuple[ResolutionVendorOption, ...] = Field(
        default=(),
        max_length=_MAX_VENDOR_OPTIONS,
    )
    source_ids: tuple[str, ...] = Field(min_length=1)
    expected_version: int | None = None
    history: tuple[dict[str, Any], ...] = ()
    management_notes: tuple[dict[str, Any], ...] = ()
    policy: dict[str, Any] = Field(default_factory=dict)
    revision: int = Field(default=1, ge=1)
    previous_context_id: str | None = None

    def recommendation_context(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "expected_version": self.expected_version,
            "history": list(self.history),
            "management_notes": list(self.management_notes),
            "policy": self.policy,
            "case": {
                "title": self.case_title,
                "category": self.category.value,
                "urgency": self.urgency.value,
                "asset_id": self.asset_id,
            },
            "offered_resolution_paths": [path.value for path in self.allowed_paths],
            "allowed_source_ids": list(self.source_ids),
            "offered_vendor_ids": [item.vendor_id for item in self.vendor_options],
            "messages": [item.model_dump(mode="json") for item in self.messages],
            "vendor_options": [item.model_dump(mode="json") for item in self.vendor_options],
        }


class DurableResolutionPlan(_Record):
    schema_version: Literal[1] = 1
    plan_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    recommendation: ResolutionPlanRecommendation
    created_at: UtcDatetime
    source_ids: tuple[str, ...] = Field(min_length=1)
    model_output_is_authority: Literal[False] = False
    outbound_enabled: Literal[False] = False


@dataclass(frozen=True, slots=True)
class ResolutionPlanningResult:
    disposition: ResolutionPlanningDisposition
    case: Case
    context: ResolutionContext
    plan: DurableResolutionPlan


class ResolutionPlanningService:
    """Freeze a case discussion, ask a narrow model, and persist its plan."""

    __slots__ = ("_clock", "_planner", "_policy", "_store", "_vendors")

    def __init__(
        self,
        *,
        store: OperationalStore,
        planner: ResolutionPlanner,
        vendors: tuple[Vendor, ...],
        clock: Clock,
        policy=None,
    ) -> None:
        by_id: dict[str, ResolutionVendorOption] = {}
        for vendor in vendors:
            if vendor.vendor_id in by_id:
                raise ValueError(f"duplicate vendor id: {vendor.vendor_id}")
            by_id[vendor.vendor_id] = ResolutionVendorOption(
                vendor_id=vendor.vendor_id,
                name=vendor.name,
                categories=tuple(vendor.categories),
                allowlisted=vendor.allowlisted,
                simulated=vendor.simulated,
            )
        self._store = store
        self._planner = planner
        self._vendors = tuple(by_id[key] for key in sorted(by_id))
        self._clock = clock
        self._policy = policy

    def plan(
        self, *, case_id: str, idempotency_key: str, revise: bool = False
    ) -> ResolutionPlanningResult:
        safe_case_id = case_id.strip()
        safe_key = idempotency_key.strip()
        if not safe_case_id:
            raise ResolutionPlanningNotReadyError("resolution planning case_id is blank")
        if not safe_key or len(safe_key) > 500:
            raise ResolutionPlanningNotReadyError("resolution planning idempotency key is invalid")

        context_id = (
            _stable_id("resolution-context", safe_case_id, safe_key)
            if revise
            else _stable_id("resolution-context", safe_case_id)
        )
        plan_id = _stable_id("resolution-plan", context_id)
        if not revise:
            self._reject_competing_artifacts(
                case_id=safe_case_id,
                context_id=context_id,
                plan_id=plan_id,
            )

        context_artifact = self._store.artifact(
            RESOLUTION_CONTEXT_ARTIFACT_KIND,
            context_id,
        )
        if context_artifact is None:
            context = self._build_context(
                case_id=safe_case_id,
                context_id=context_id,
                request_key=safe_key,
            )
            context = self._persist_context(context)
        else:
            context = self._load_context(
                artifact=context_artifact,
                case_id=safe_case_id,
                context_id=context_id,
                request_key_hash=_sha256(safe_key),
            )

        plan_artifact = self._store.artifact(RESOLUTION_PLAN_ARTIFACT_KIND, plan_id)
        if plan_artifact is not None:
            plan = self._load_plan(
                artifact=plan_artifact,
                context=context,
                plan_id=plan_id,
            )
            current = self._store.get_case(safe_case_id)
            if current is None:
                raise ResolutionPlanningIntegrityError("durable plan lost its case")
            return ResolutionPlanningResult(
                disposition=ResolutionPlanningDisposition.DUPLICATE,
                case=current,
                context=context,
                plan=plan,
            )

        self._require_plannable_case(safe_case_id)
        recommendation = self._planner.plan(
            context_id=context.context_id,
            context=context.recommendation_context(),
        )
        self._validate_recommendation(context, recommendation)
        return self._persist_plan(
            context=context,
            recommendation=recommendation,
            plan_id=plan_id,
        )

    def load(self, case_id: str) -> ResolutionPlanningResult | None:
        """Load the latest complete, explicitly linked planning revision."""
        safe_case_id = case_id.strip()
        if not safe_case_id:
            raise ResolutionPlanningNotReadyError("resolution planning case_id is blank")
        contexts = self._store.artifacts_for(
            kind=RESOLUTION_CONTEXT_ARTIFACT_KIND,
            case_id=safe_case_id,
        )
        plans = self._store.artifacts_for(
            kind=RESOLUTION_PLAN_ARTIFACT_KIND,
            case_id=safe_case_id,
        )
        if not contexts and not plans:
            return None
        revisions = sorted(contexts, key=lambda a: a.payload.get("revision", 1))
        if any(
            a.payload.get("revision", 1) != i + 1
            or (i and a.payload.get("previous_context_id") != revisions[i - 1].artifact_id)
            for i, a in enumerate(revisions)
        ):
            raise ResolutionPlanningIntegrityError("resolution revision chain is invalid")
        latest = revisions[-1] if revisions else None
        plans = [p for p in plans if latest and p.payload["context_id"] == latest.artifact_id]
        if latest is None or len(plans) != 1:
            raise ResolutionPlanningIntegrityError(
                "case does not own exactly one complete resolution plan"
            )
        context_payload = latest.payload
        request_hash = str(context_payload.get("request_key_sha256", ""))
        context = self._load_context(
            artifact=latest,
            case_id=safe_case_id,
            context_id=latest.artifact_id,
            request_key_hash=request_hash,
        )
        plan = self._load_plan(
            artifact=plans[0],
            context=context,
            plan_id=plans[0].artifact_id,
        )
        current = self._store.get_case(safe_case_id)
        if current is None:
            raise ResolutionPlanningIntegrityError("durable plan lost its case")
        return ResolutionPlanningResult(
            disposition=ResolutionPlanningDisposition.DUPLICATE,
            case=current,
            context=context,
            plan=plan,
        )

    def _build_context(
        self,
        *,
        case_id: str,
        context_id: str,
        request_key: str,
    ) -> ResolutionContext:
        case = self._require_plannable_case(case_id)
        if not case.source_message_ids:
            raise ResolutionPlanningIntegrityError("planning case has no persisted source message")
        source_ids = tuple(dict.fromkeys(case.source_message_ids))
        if len(source_ids) > _MAX_CONTEXT_MESSAGES:
            raise ResolutionPlanningNotReadyError(
                "case has too many source messages for one planning snapshot"
            )
        messages: list[ResolutionContextMessage] = []
        for source_id in source_ids:
            message = self._store.get_message(source_id)
            if message is None or message.case_id != case.case_id:
                raise ResolutionPlanningIntegrityError("planning case lost a linked source message")
            messages.append(
                ResolutionContextMessage(
                    message_id=message.message_id,
                    sender_display=message.sender_display,
                    text=message.text,
                    sent_at=message.sent_at,
                )
            )
        created_at = self._clock.now()
        if created_at < case.opened_at:
            raise ResolutionPlanningIntegrityError("resolution context cannot predate its case")
        history = tuple(
            r.model_dump(mode="json")
            for r in self._store.records()
            if r.category == case.category
            and r.closed_at <= created_at
            and (case.asset_id is None or r.asset_id == case.asset_id)
        )[-12:]
        notes = tuple(
            {
                "source_id": a.artifact_id,
                "actor_id": a.payload["actor_id"],
                "notes": a.payload["notes"],
            }
            for a in self._store.artifacts_for(kind="api.command.v1", case_id=case_id)
            if a.payload.get("action") == "review_resolution" and a.payload.get("notes")
        )[-8:]
        earlier = self._store.artifacts_for(kind=RESOLUTION_CONTEXT_ARTIFACT_KIND, case_id=case_id)
        previous = max(earlier, key=lambda a: a.payload.get("revision", 1)) if earlier else None
        return ResolutionContext(
            context_id=context_id,
            request_key_sha256=_sha256(request_key),
            case_id=case.case_id,
            case_title=case.title,
            category=case.category,
            urgency=case.urgency,
            asset_id=case.asset_id,
            created_at=created_at,
            allowed_paths=_allowed_paths(case.category),
            messages=tuple(messages),
            vendor_options=self._vendors,
            source_ids=tuple(
                dict.fromkeys(
                    (
                        *source_ids,
                        *(r["case_id"] for r in history),
                        *(n["source_id"] for n in notes),
                    )
                )
            ),
            expected_version=self._store.case_version(case_id),
            history=history,
            management_notes=notes,
            revision=previous.payload.get("revision", 1) + 1 if previous else 1,
            previous_context_id=previous.artifact_id if previous else None,
            policy={
                "category": self._policy.policy_for(case.category).model_dump(mode="json")
                if self._policy and self._policy.policy_for(case.category)
                else None,
                "global": self._policy.settings.model_dump(mode="json") if self._policy else {},
                "snapshot_is_authority": False,
            },
        )

    def _persist_context(self, context: ResolutionContext) -> ResolutionContext:
        artifact = WorkflowArtifact(
            artifact_id=context.context_id,
            case_id=context.case_id,
            kind=RESOLUTION_CONTEXT_ARTIFACT_KIND,
            created_at=context.created_at,
            source_ids=context.source_ids,
            payload=context.model_dump(mode="json"),
        )
        event = TimelineEvent(
            event_id=_stable_id("event-resolution-context", context.context_id),
            case_id=context.case_id,
            at=context.created_at,
            kind=EventKind.PLAN_DRAFTED,
            actor=ActorType.SYSTEM,
            summary=(
                f"Frozen {len(context.messages)} source message(s) before resolution-path planning."
            ),
            refs=list(context.source_ids),
            payload={
                "context_id": context.context_id,
                "offered_resolution_paths": [path.value for path in context.allowed_paths],
                "model_invocation_pending": True,
                "outbound_enabled": False,
            },
        )
        for _attempt in range(3):
            existing = self._store.artifact(
                RESOLUTION_CONTEXT_ARTIFACT_KIND,
                context.context_id,
            )
            if existing is not None:
                return self._load_context(
                    artifact=existing,
                    case_id=context.case_id,
                    context_id=context.context_id,
                    request_key_hash=context.request_key_sha256,
                )
            current = self._require_plannable_case(context.case_id)
            if (
                current.category is not context.category
                or current.asset_id != context.asset_id
                or current.title != context.case_title
                or tuple(dict.fromkeys(current.source_message_ids))
                != tuple(m.message_id for m in context.messages)
            ):
                raise ResolutionPlanningConflictError(
                    "case discussion changed before the resolution context was committed"
                )
            version = self._store.case_version(context.case_id)
            if version is None:
                raise ResolutionPlanningIntegrityError(
                    "resolution context case lost its optimistic version"
                )
            if context.expected_version is not None and version != context.expected_version:
                persisted = self._store.artifact(
                    RESOLUTION_CONTEXT_ARTIFACT_KIND, context.context_id
                )
                if persisted is not None:
                    return self._load_context(
                        artifact=persisted,
                        case_id=context.case_id,
                        context_id=context.context_id,
                        request_key_hash=context.request_key_sha256,
                    )
                raise ResolutionPlanningConflictError(
                    "Case changed before planning; request a fresh review"
                )
            updated = current.model_copy(deep=True)
            updated.updated_at = max(updated.updated_at, context.created_at)
            try:
                self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=f"resolution-context:v1:{context.context_id}",
                    timeline_events=(event,),
                    artifacts=(artifact,),
                )
                return context
            except (ConcurrencyConflict, IdempotencyConflict):
                persisted = self._store.artifact(
                    RESOLUTION_CONTEXT_ARTIFACT_KIND,
                    context.context_id,
                )
                if persisted is not None:
                    return self._load_context(
                        artifact=persisted,
                        case_id=context.case_id,
                        context_id=context.context_id,
                        request_key_hash=context.request_key_sha256,
                    )
        raise ConcurrencyConflict("resolution context persistence exceeded retry limit")

    def _persist_plan(
        self,
        *,
        context: ResolutionContext,
        recommendation: ResolutionPlanRecommendation,
        plan_id: str,
    ) -> ResolutionPlanningResult:
        created_at = self._clock.now()
        if created_at < context.created_at:
            raise ResolutionPlanningIntegrityError("resolution plan cannot predate its context")
        source_ids = _ordered_unique(
            (context.context_id,),
            recommendation.source_ids,
            recommendation.relevant_vendor_ids,
        )
        plan = DurableResolutionPlan(
            plan_id=plan_id,
            context_id=context.context_id,
            case_id=context.case_id,
            recommendation=recommendation,
            created_at=created_at,
            source_ids=source_ids,
        )
        artifact = WorkflowArtifact(
            artifact_id=plan.plan_id,
            case_id=plan.case_id,
            kind=RESOLUTION_PLAN_ARTIFACT_KIND,
            created_at=plan.created_at,
            source_ids=plan.source_ids,
            payload=plan.model_dump(mode="json"),
        )
        event = TimelineEvent(
            event_id=_stable_id("event-resolution-plan", plan.plan_id),
            case_id=plan.case_id,
            at=plan.created_at,
            kind=EventKind.RESOLUTION_PLANNED,
            actor=ActorType.AGENT,
            summary=(
                f"Prepared the source-traced {recommendation.path.value} path; "
                "the model granted no authority and no outbound action occurred."
            ),
            refs=list(plan.source_ids),
            payload={
                "plan_id": plan.plan_id,
                "context_id": plan.context_id,
                "resolution_path": recommendation.path.value,
                "required_fact_codes": list(recommendation.required_fact_codes),
                "relevant_vendor_ids": list(recommendation.relevant_vendor_ids),
                "model_output_is_authority": False,
                "outbound_enabled": False,
            },
        )
        for _attempt in range(3):
            existing = self._store.artifact(RESOLUTION_PLAN_ARTIFACT_KIND, plan.plan_id)
            if existing is not None:
                persisted = self._load_plan(
                    artifact=existing,
                    context=context,
                    plan_id=plan.plan_id,
                )
                latest = self._store.get_case(plan.case_id)
                if latest is None:
                    raise ResolutionPlanningIntegrityError("durable plan lost its case")
                return ResolutionPlanningResult(
                    disposition=ResolutionPlanningDisposition.DUPLICATE,
                    case=latest,
                    context=context,
                    plan=persisted,
                )
            current = self._require_plannable_case(plan.case_id)
            version = self._store.case_version(plan.case_id)
            if version is None:
                raise ResolutionPlanningIntegrityError(
                    "resolution plan case lost its optimistic version"
                )
            if context.expected_version is not None and version != context.expected_version + 1:
                raise ConcurrencyConflict("Case changed during planning; request a fresh review")
            updated = current.model_copy(deep=True)
            updated.status = CaseStatus.PLANNING
            updated.updated_at = max(updated.updated_at, plan.created_at)
            try:
                self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=f"resolution-plan:v1:{plan.plan_id}",
                    timeline_events=(event,),
                    artifacts=(artifact,),
                )
                return ResolutionPlanningResult(
                    disposition=ResolutionPlanningDisposition.PLAN_RECORDED,
                    case=updated,
                    context=context,
                    plan=plan,
                )
            except (ConcurrencyConflict, IdempotencyConflict):
                persisted = self._store.artifact(
                    RESOLUTION_PLAN_ARTIFACT_KIND,
                    plan.plan_id,
                )
                if persisted is not None:
                    loaded = self._load_plan(
                        artifact=persisted,
                        context=context,
                        plan_id=plan.plan_id,
                    )
                    latest = self._store.get_case(plan.case_id)
                    if latest is None:
                        raise ResolutionPlanningIntegrityError(
                            "durable plan lost its case"
                        ) from None
                    return ResolutionPlanningResult(
                        disposition=ResolutionPlanningDisposition.DUPLICATE,
                        case=latest,
                        context=context,
                        plan=loaded,
                    )
        raise ConcurrencyConflict("resolution plan persistence exceeded retry limit")

    def _load_context(
        self,
        *,
        artifact: WorkflowArtifact,
        case_id: str,
        context_id: str,
        request_key_hash: str,
    ) -> ResolutionContext:
        try:
            context = ResolutionContext.model_validate(artifact.payload)
        except ValueError as exc:
            raise ResolutionPlanningIntegrityError(
                "persisted resolution context is not structurally valid"
            ) from exc
        if (
            artifact.kind != RESOLUTION_CONTEXT_ARTIFACT_KIND
            or artifact.artifact_id != context_id
            or artifact.case_id != case_id
            or context.context_id != context_id
            or context.case_id != case_id
            or context.request_key_sha256 != request_key_hash
            or artifact.source_ids != context.source_ids
        ):
            raise ResolutionPlanningConflictError(
                "persisted resolution context does not match the request"
            )
        if len(context.source_ids) != len(set(context.source_ids)):
            raise ResolutionPlanningIntegrityError(
                "resolution context contains duplicate source ids"
            )
        current = self._store.get_case(case_id)
        if current is None:
            raise ResolutionPlanningIntegrityError("resolution context lost its case")
        if (
            current.category is not context.category
            or current.asset_id != context.asset_id
            or current.title != context.case_title
        ):
            raise ResolutionPlanningIntegrityError(
                "case routing facts changed after resolution context persistence"
            )
        if tuple(dict.fromkeys(current.source_message_ids)) != tuple(
            m.message_id for m in context.messages
        ):
            raise ResolutionPlanningConflictError(
                "case discussion changed after the immutable context was frozen"
            )
        offered_vendor_ids = [item.vendor_id for item in context.vendor_options]
        if len(offered_vendor_ids) != len(set(offered_vendor_ids)):
            raise ResolutionPlanningIntegrityError(
                "resolution context contains duplicate vendor options"
            )
        return context

    def _load_plan(
        self,
        *,
        artifact: WorkflowArtifact,
        context: ResolutionContext,
        plan_id: str,
    ) -> DurableResolutionPlan:
        try:
            plan = DurableResolutionPlan.model_validate(artifact.payload)
        except ValueError as exc:
            raise ResolutionPlanningIntegrityError(
                "persisted resolution plan is not structurally valid"
            ) from exc
        if (
            artifact.kind != RESOLUTION_PLAN_ARTIFACT_KIND
            or artifact.artifact_id != plan_id
            or artifact.case_id != context.case_id
            or plan.plan_id != plan_id
            or plan.context_id != context.context_id
            or plan.case_id != context.case_id
            or artifact.source_ids != plan.source_ids
        ):
            raise ResolutionPlanningIntegrityError(
                "persisted resolution plan violates identity invariants"
            )
        self._validate_recommendation(context, plan.recommendation)
        expected_sources = _ordered_unique(
            (context.context_id,),
            plan.recommendation.source_ids,
            plan.recommendation.relevant_vendor_ids,
        )
        if plan.source_ids != expected_sources:
            raise ResolutionPlanningIntegrityError(
                "persisted resolution plan violates source invariants"
            )
        return plan

    def _reject_competing_artifacts(
        self,
        *,
        case_id: str,
        context_id: str,
        plan_id: str,
    ) -> None:
        contexts = self._store.artifacts_for(
            kind=RESOLUTION_CONTEXT_ARTIFACT_KIND,
            case_id=case_id,
        )
        if any(item.artifact_id != context_id for item in contexts):
            raise ResolutionPlanningConflictError(
                "case already has a different immutable resolution context"
            )
        plans = self._store.artifacts_for(
            kind=RESOLUTION_PLAN_ARTIFACT_KIND,
            case_id=case_id,
        )
        if any(item.artifact_id != plan_id for item in plans):
            raise ResolutionPlanningConflictError(
                "case already has a different durable resolution plan"
            )

    @staticmethod
    def _validate_recommendation(
        context: ResolutionContext,
        recommendation: ResolutionPlanRecommendation,
    ) -> None:
        if recommendation.path not in context.allowed_paths:
            raise ResolutionRecommendationIntegrityError(
                "model selected a resolution path outside the offered set"
            )
        allowed_sources = set(context.source_ids)
        if any(source_id not in allowed_sources for source_id in recommendation.source_ids):
            raise ResolutionRecommendationIntegrityError(
                "model cited a source outside the frozen discussion"
            )
        offered_vendors = {item.vendor_id for item in context.vendor_options}
        if any(
            vendor_id not in offered_vendors for vendor_id in recommendation.relevant_vendor_ids
        ):
            raise ResolutionRecommendationIntegrityError(
                "model selected a vendor outside the offered directory"
            )
        if (
            recommendation.path is ResolutionPath.HUMAN_REVIEW
            and recommendation.relevant_vendor_ids
        ):
            raise ResolutionRecommendationIntegrityError(
                "human-review fallback cannot carry a vendor shortlist"
            )

    def _require_plannable_case(self, case_id: str) -> Case:
        case = self._store.get_case(case_id)
        if case is None:
            raise ResolutionPlanningNotReadyError("unknown resolution-planning case")
        if case.status in TERMINAL_STATUSES:
            raise ResolutionPlanningNotReadyError("terminal case cannot be planned")
        if case.status not in (CaseStatus.DETECTED, CaseStatus.PLANNING):
            raise ResolutionPlanningNotReadyError(
                "resolution planning requires a detected or planning case"
            )
        return case


def _allowed_paths(category: Category) -> tuple[ResolutionPath, ...]:
    if category in (Category.MEETING_ADMIN, Category.OTHER):
        return (ResolutionPath.MEETING_RESOLUTION, ResolutionPath.HUMAN_REVIEW)
    return (
        ResolutionPath.PROCUREMENT,
        ResolutionPath.MEETING_RESOLUTION,
        ResolutionPath.HUMAN_REVIEW,
    )


def _stable_id(prefix: str, *parts: str) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ordered_unique(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))
