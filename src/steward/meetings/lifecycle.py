"""Durable meeting decisions, action tracking, due work, and verified closure.

No operation in this module constructs an outbox item or owns a transport.
Models extract candidates only; configured humans confirm, complete, and verify
closed identifiers through deterministic PolicyEngine checks.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from steward.agents import MeetingMinutesExtraction, MeetingMinutesExtractor
from steward.domain.clock import Clock, parse_datetime
from steward.domain.enums import ActorType, AutonomyLevel, CaseStatus, EventKind
from steward.domain.models import AuditEntry, Case, TimelineEvent, UtcDatetime
from steward.meetings.records import (
    MEETING_ACTION_COMPLETION_ARTIFACT_KIND,
    MEETING_ACTION_REMINDER_ARTIFACT_KIND,
    MEETING_DECISION_ARTIFACT_KIND,
    MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
    MEETING_ESCALATION_ARTIFACT_KIND,
    MEETING_MINUTES_ARTIFACT_KIND,
    MEETING_VERIFICATION_REQUEST_ARTIFACT_KIND,
    MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
    DurableMeetingDecision,
    MeetingActionCandidate,
    MeetingActionCompletion,
    MeetingActionReminder,
    MeetingDecisionCandidate,
    MeetingDecisionCandidateSet,
    MeetingDecisionConfirmation,
    MeetingEscalation,
    MeetingGovernancePolicy,
    MeetingMinutesSnapshot,
    MeetingVerificationOutcome,
    MeetingVerificationRequest,
    MeetingVerificationResponse,
)
from steward.meetings.workflow import MeetingPreparationService
from steward.memory import CaseRecord
from steward.policy import MeetingAuthorityAction, PolicyEngine
from steward.store import (
    ConcurrencyConflict,
    IdempotencyConflict,
    InferenceClaimDisposition,
    InferenceJob,
    InferenceJobStatus,
    OperationalStore,
    StaleLeaseToken,
    TransitionResult,
    WorkflowArtifact,
)

__all__ = [
    "MeetingActionCompletionDisposition",
    "MeetingActionCompletionResult",
    "MeetingDecisionDisposition",
    "MeetingDecisionResult",
    "MeetingDueDisposition",
    "MeetingDueResult",
    "MeetingInferenceFailedError",
    "MeetingInferenceInProgressError",
    "MeetingLifecycleConflictError",
    "MeetingLifecycleError",
    "MeetingLifecycleIntegrityError",
    "MeetingLifecycleNotReadyError",
    "MeetingLifecycleService",
    "MeetingMinutesDisposition",
    "MeetingMinutesResult",
    "MeetingVerificationDisposition",
    "MeetingVerificationResult",
]

_MEETING_MINUTES_INFERENCE_KIND = "meeting_minutes_extraction.v1"


def _default_inference_token() -> str:
    return f"inference-{uuid4().hex}"


class MeetingLifecycleError(ValueError):
    """Base class for meeting decision-lifecycle rejection."""


class MeetingLifecycleNotReadyError(MeetingLifecycleError):
    """The case has not reached the required durable phase."""


class MeetingInferenceInProgressError(MeetingLifecycleNotReadyError):
    """Another worker currently owns the durable minutes inference lease."""


class MeetingInferenceFailedError(MeetingLifecycleNotReadyError):
    """Minutes inference exhausted its configured durable attempts."""


class MeetingLifecycleIntegrityError(MeetingLifecycleError):
    """Persisted meeting evidence or identity facts are inconsistent."""


class MeetingLifecycleConflictError(MeetingLifecycleError):
    """A singleton lifecycle fact already exists with different content."""


class MeetingMinutesDisposition(str, Enum):
    CANDIDATES_RECORDED = "candidates_recorded"
    DUPLICATE = "duplicate"


class MeetingDecisionDisposition(str, Enum):
    DECISION_CONFIRMED = "decision_confirmed"
    DUPLICATE = "duplicate"


class MeetingActionCompletionDisposition(str, Enum):
    COMPLETED = "completed"
    DUPLICATE = "duplicate"


class MeetingDueDisposition(str, Enum):
    INACTIVE = "inactive"
    NOT_DUE = "not_due"
    REMINDER_RECORDED = "reminder_recorded"
    ESCALATED = "escalated"
    DUPLICATE = "duplicate"


class MeetingVerificationDisposition(str, Enum):
    CLOSED = "closed"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class MeetingMinutesResult:
    disposition: MeetingMinutesDisposition
    case: Case
    snapshot: MeetingMinutesSnapshot
    candidates: MeetingDecisionCandidateSet


@dataclass(frozen=True, slots=True)
class MeetingDecisionResult:
    disposition: MeetingDecisionDisposition
    case: Case
    decision: DurableMeetingDecision
    verification_request: MeetingVerificationRequest | None = None


@dataclass(frozen=True, slots=True)
class MeetingActionCompletionResult:
    disposition: MeetingActionCompletionDisposition
    case: Case
    completion: MeetingActionCompletion
    verification_request: MeetingVerificationRequest | None = None


@dataclass(frozen=True, slots=True)
class MeetingDueResult:
    disposition: MeetingDueDisposition
    case: Case
    reminder: MeetingActionReminder | None = None
    escalation: MeetingEscalation | None = None


@dataclass(frozen=True, slots=True)
class MeetingVerificationResult:
    disposition: MeetingVerificationDisposition
    case: Case
    response: MeetingVerificationResponse
    memory_record: CaseRecord | None = None
    transition: TransitionResult | None = None


class MeetingLifecycleService:
    """Drive one meeting packet through decisions, actions, and verified memory."""

    __slots__ = (
        "_clock",
        "_extractor",
        "_inference_lease_seconds",
        "_inference_max_attempts",
        "_inference_token_factory",
        "_meeting",
        "_policy",
        "_store",
    )

    def __init__(
        self,
        *,
        store: OperationalStore,
        meeting: MeetingPreparationService,
        policy: PolicyEngine,
        extractor: MeetingMinutesExtractor,
        clock: Clock,
        inference_lease_seconds: int = 300,
        inference_max_attempts: int = 3,
        inference_token_factory: Callable[[], str] = _default_inference_token,
    ) -> None:
        if inference_lease_seconds <= 0:
            raise ValueError("inference_lease_seconds must be positive")
        if inference_max_attempts <= 0:
            raise ValueError("inference_max_attempts must be positive")
        self._store = store
        self._meeting = meeting
        self._policy = policy
        self._extractor = extractor
        self._clock = clock
        self._inference_lease_seconds = inference_lease_seconds
        self._inference_max_attempts = inference_max_attempts
        self._inference_token_factory = inference_token_factory

    # -- Minutes and candidates ---------------------------------------

    def extract_minutes(
        self,
        *,
        case_id: str,
        held_at,
        body_text: str,
        source_id: str,
        participant_count: int,
        governance: MeetingGovernancePolicy,
        simulated: bool,
        idempotency_key: str,
    ) -> MeetingMinutesResult:
        safe_case_id = case_id.strip()
        safe_key = idempotency_key.strip()
        safe_body = body_text.strip()
        safe_source = source_id.strip()
        if not safe_case_id or not safe_key or len(safe_key) > 500:
            raise MeetingLifecycleNotReadyError("meeting minutes request is invalid")
        if not safe_body or len(safe_body) > 30000 or not safe_source:
            raise MeetingLifecycleNotReadyError("meeting minutes source is invalid")
        snapshot_id = _stable_id("meeting-minutes", safe_case_id)
        candidate_set_id = _stable_id("meeting-candidates", snapshot_id)
        self._reject_competing_singleton(
            kind=MEETING_MINUTES_ARTIFACT_KIND,
            case_id=safe_case_id,
            artifact_id=snapshot_id,
        )
        self._reject_competing_singleton(
            kind=MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
            case_id=safe_case_id,
            artifact_id=candidate_set_id,
        )

        snapshot_artifact = self._store.artifact(
            MEETING_MINUTES_ARTIFACT_KIND,
            snapshot_id,
        )
        if snapshot_artifact is None:
            snapshot = self._build_snapshot(
                case_id=safe_case_id,
                snapshot_id=snapshot_id,
                request_key=safe_key,
                held_at=held_at,
                body_text=safe_body,
                source_id=safe_source,
                participant_count=participant_count,
                governance=governance,
                simulated=simulated,
            )
            snapshot = self._persist_snapshot(snapshot)
        else:
            snapshot = self._load_snapshot(
                artifact=snapshot_artifact,
                request_key_hash=_sha256(safe_key),
                expected_body=safe_body,
                expected_source=safe_source,
                expected_held_at=held_at,
                expected_participant_count=participant_count,
                expected_governance=governance,
                expected_simulated=simulated,
            )

        candidates_artifact = self._store.artifact(
            MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
            candidate_set_id,
        )
        if candidates_artifact is not None:
            candidates = self._load_candidates(
                artifact=candidates_artifact,
                snapshot=snapshot,
                candidate_set_id=candidate_set_id,
            )
            current = self._require_case(safe_case_id)
            return MeetingMinutesResult(
                disposition=MeetingMinutesDisposition.DUPLICATE,
                case=current,
                snapshot=snapshot,
                candidates=candidates,
            )

        extraction, completed_at = self._durable_minutes_extraction(snapshot)
        candidates = self._candidate_set(
            snapshot=snapshot,
            extraction=extraction,
            candidate_set_id=candidate_set_id,
            created_at=completed_at,
        )
        return self._persist_candidates(snapshot=snapshot, candidates=candidates)

    def _durable_minutes_extraction(
        self,
        snapshot: MeetingMinutesSnapshot,
    ) -> tuple[MeetingMinutesExtraction, UtcDatetime]:
        context = snapshot.extraction_context()
        input_payload = {
            "kind": _MEETING_MINUTES_INFERENCE_KIND,
            "snapshot_id": snapshot.snapshot_id,
            "context": context,
        }
        input_sha256 = _sha256(
            json.dumps(
                input_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        job_id = _stable_id("meeting-minutes-inference", snapshot.snapshot_id)
        token = self._inference_token_factory()
        claim = self._store.claim_inference_job(
            job_id=job_id,
            kind=_MEETING_MINUTES_INFERENCE_KIND,
            case_id=snapshot.case_id,
            input_sha256=input_sha256,
            token=token,
            now=self._clock.now(),
            lease_seconds=self._inference_lease_seconds,
            max_attempts=self._inference_max_attempts,
        )
        if claim.disposition is InferenceClaimDisposition.BUSY:
            latest = self._store.inference_job(job_id)
            if latest is not None and latest.status is InferenceJobStatus.COMPLETED:
                return self._load_inference_result(snapshot, latest)
            raise MeetingInferenceInProgressError(
                "meeting minutes extraction is already running on another worker"
            )
        if claim.disposition is InferenceClaimDisposition.FAILED:
            raise MeetingInferenceFailedError(
                "meeting minutes extraction exhausted its durable attempts"
            )
        if claim.disposition is InferenceClaimDisposition.COMPLETED:
            return self._load_inference_result(snapshot, claim.job)
        lease = claim.lease
        if lease is None:
            raise MeetingLifecycleIntegrityError(
                "claimed meeting inference did not return its lease"
            )
        try:
            extraction = self._extractor.extract(
                snapshot_id=snapshot.snapshot_id,
                context=context,
            )
            self._validate_extraction(snapshot, extraction)
        except MeetingLifecycleIntegrityError:
            self._fail_minutes_inference(
                job_id=job_id,
                token=lease.token,
                error_code="meeting_minutes_invalid_output",
                retryable=False,
            )
            raise
        except Exception:
            self._fail_minutes_inference(
                job_id=job_id,
                token=lease.token,
                error_code="meeting_minutes_model_error",
                retryable=True,
            )
            raise
        completed_at = self._clock.now()
        try:
            job = self._store.complete_inference_claim(
                job_id=job_id,
                token=lease.token,
                completed_at=completed_at,
                result_payload=extraction.model_dump(mode="json"),
            )
        except StaleLeaseToken as exc:
            raise MeetingInferenceInProgressError(
                "meeting minutes inference lease expired before completion"
            ) from exc
        except ValueError as exc:
            raise MeetingLifecycleIntegrityError(
                "meeting minutes inference durable completion was rejected"
            ) from exc
        return self._load_inference_result(snapshot, job)

    def _fail_minutes_inference(
        self,
        *,
        job_id: str,
        token: str,
        error_code: str,
        retryable: bool,
    ) -> None:
        try:
            self._store.fail_inference_claim(
                job_id=job_id,
                token=token,
                failed_at=self._clock.now(),
                error_code=error_code,
                retryable=retryable,
                max_attempts=self._inference_max_attempts,
            )
        except ValueError:
            return

    def _load_inference_result(
        self,
        snapshot: MeetingMinutesSnapshot,
        job: InferenceJob,
    ) -> tuple[MeetingMinutesExtraction, UtcDatetime]:
        if job.completed_at is None or job.result_payload is None:
            raise MeetingLifecycleIntegrityError(
                "completed meeting inference has no durable result"
            )
        try:
            extraction = MeetingMinutesExtraction.model_validate(job.result_payload)
        except ValueError as exc:
            raise MeetingLifecycleIntegrityError(
                "durable meeting inference result is invalid"
            ) from exc
        self._validate_extraction(snapshot, extraction)
        return extraction, job.completed_at

    def _build_snapshot(
        self,
        *,
        case_id: str,
        snapshot_id: str,
        request_key: str,
        held_at,
        body_text: str,
        source_id: str,
        participant_count: int,
        governance: MeetingGovernancePolicy,
        simulated: bool,
    ) -> MeetingMinutesSnapshot:
        prepared = self._meeting.load(case_id)
        if prepared is None:
            raise MeetingLifecycleNotReadyError(
                "meeting minutes require an immutable meeting packet"
            )
        case = self._require_case(case_id)
        if case.status is not CaseStatus.MEETING_READY:
            raise MeetingLifecycleNotReadyError("meeting minutes require a meeting-ready case")
        now = self._clock.now()
        if held_at < prepared.packet.schedule.selected_slot.starts_at:
            raise MeetingLifecycleIntegrityError("meeting cannot be held before its selected slot")
        if held_at > now:
            raise MeetingLifecycleIntegrityError("meeting cannot be held in the future")
        if participant_count < prepared.packet.schedule.policy.quorum:
            raise MeetingLifecycleNotReadyError(
                "meeting participant count did not satisfy configured quorum"
            )
        eligible = set(prepared.packet.schedule.policy.eligible_participant_ids)
        if not set(governance.action_owner_ids).issubset(eligible):
            raise MeetingLifecycleIntegrityError(
                "meeting action owners must be eligible packet participants"
            )
        source_ids = (prepared.packet.packet_id, source_id)
        return MeetingMinutesSnapshot(
            snapshot_id=snapshot_id,
            request_key_sha256=_sha256(request_key),
            case_id=case_id,
            packet_id=prepared.packet.packet_id,
            held_at=held_at,
            recorded_at=now,
            source_id=source_id,
            body_text=body_text,
            participant_count=participant_count,
            governance=governance,
            simulated=simulated,
            source_ids=source_ids,
        )

    def _persist_snapshot(self, snapshot: MeetingMinutesSnapshot) -> MeetingMinutesSnapshot:
        artifact = _artifact(
            kind=MEETING_MINUTES_ARTIFACT_KIND,
            artifact_id=snapshot.snapshot_id,
            case_id=snapshot.case_id,
            created_at=snapshot.recorded_at,
            source_ids=snapshot.source_ids,
            payload=snapshot,
        )
        event = TimelineEvent(
            event_id=_stable_id("event-meeting-minutes", snapshot.snapshot_id),
            case_id=snapshot.case_id,
            at=snapshot.recorded_at,
            kind=EventKind.MEETING_MINUTES_RECORDED,
            actor=ActorType.HUMAN,
            summary=(
                "Recorded a source-traced meeting-minutes snapshot before extracting "
                "decision candidates."
            ),
            refs=list(snapshot.source_ids),
            payload={
                "snapshot_id": snapshot.snapshot_id,
                "held_at": snapshot.held_at.isoformat(),
                "participant_count": snapshot.participant_count,
                "model_invocation_pending": True,
                "outbound_enabled": False,
            },
        )
        for _attempt in range(3):
            existing = self._store.artifact(
                MEETING_MINUTES_ARTIFACT_KIND,
                snapshot.snapshot_id,
            )
            if existing is not None:
                return self._load_snapshot(
                    artifact=existing,
                    request_key_hash=snapshot.request_key_sha256,
                    expected_body=snapshot.body_text,
                    expected_source=snapshot.source_id,
                    expected_held_at=snapshot.held_at,
                    expected_participant_count=snapshot.participant_count,
                    expected_governance=snapshot.governance,
                    expected_simulated=snapshot.simulated,
                )
            prepared = self._meeting.load(snapshot.case_id)
            if prepared is None or prepared.packet.packet_id != snapshot.packet_id:
                raise MeetingLifecycleConflictError(
                    "meeting packet changed before minutes were committed"
                )
            current = self._require_case(snapshot.case_id)
            if current.status is not CaseStatus.MEETING_READY:
                raise MeetingLifecycleConflictError(
                    "meeting case changed before minutes were committed"
                )
            version = self._require_version(snapshot.case_id)
            updated = current.model_copy(deep=True)
            updated.updated_at = max(updated.updated_at, snapshot.recorded_at)
            try:
                self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=f"meeting-minutes:v1:{snapshot.snapshot_id}",
                    timeline_events=(event,),
                    artifacts=(artifact,),
                )
                return snapshot
            except (ConcurrencyConflict, IdempotencyConflict):
                continue
        raise ConcurrencyConflict("meeting minutes persistence exceeded retry limit")

    def _candidate_set(
        self,
        *,
        snapshot: MeetingMinutesSnapshot,
        extraction: MeetingMinutesExtraction,
        candidate_set_id: str,
        created_at,
    ) -> MeetingDecisionCandidateSet:
        if created_at < snapshot.recorded_at:
            raise MeetingLifecycleIntegrityError(
                "meeting candidates cannot predate their minutes snapshot"
            )
        decisions = tuple(
            MeetingDecisionCandidate(
                decision_id=_stable_id(
                    "meeting-decision-candidate",
                    snapshot.snapshot_id,
                    str(index),
                    item.statement,
                    item.evidence_excerpt,
                ),
                statement=item.statement,
                evidence_excerpt=item.evidence_excerpt,
                source_id=snapshot.source_id,
            )
            for index, item in enumerate(extraction.decisions)
        )
        action_items = tuple(
            MeetingActionCandidate(
                action_id=_stable_id(
                    "meeting-action-candidate",
                    snapshot.snapshot_id,
                    str(index),
                    item.description,
                    item.owner_participant_id,
                    item.due_at.isoformat(),
                ),
                description=item.description,
                owner_participant_id=item.owner_participant_id,
                due_at=item.due_at,
                evidence_excerpt=item.evidence_excerpt,
                source_id=snapshot.source_id,
            )
            for index, item in enumerate(extraction.action_items)
        )
        return MeetingDecisionCandidateSet(
            candidate_set_id=candidate_set_id,
            snapshot_id=snapshot.snapshot_id,
            case_id=snapshot.case_id,
            extraction=extraction,
            decisions=decisions,
            action_items=action_items,
            created_at=created_at,
            source_ids=(snapshot.snapshot_id, snapshot.source_id),
        )

    def _persist_candidates(
        self,
        *,
        snapshot: MeetingMinutesSnapshot,
        candidates: MeetingDecisionCandidateSet,
    ) -> MeetingMinutesResult:
        artifact = _artifact(
            kind=MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
            artifact_id=candidates.candidate_set_id,
            case_id=candidates.case_id,
            created_at=candidates.created_at,
            source_ids=candidates.source_ids,
            payload=candidates,
        )
        event = TimelineEvent(
            event_id=_stable_id("event-meeting-candidates", candidates.candidate_set_id),
            case_id=candidates.case_id,
            at=candidates.created_at,
            kind=EventKind.PLAN_DRAFTED,
            actor=ActorType.AGENT,
            summary=(
                f"Extracted {len(candidates.decisions)} decision candidate(s) and "
                f"{len(candidates.action_items)} action candidate(s); none is confirmed."
            ),
            refs=list(candidates.source_ids),
            payload={
                "candidate_set_id": candidates.candidate_set_id,
                "decision_candidate_ids": [item.decision_id for item in candidates.decisions],
                "action_candidate_ids": [item.action_id for item in candidates.action_items],
                "model_output_is_authority": False,
                "outbound_enabled": False,
            },
        )
        for _attempt in range(3):
            existing = self._store.artifact(
                MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
                candidates.candidate_set_id,
            )
            if existing is not None:
                loaded = self._load_candidates(
                    artifact=existing,
                    snapshot=snapshot,
                    candidate_set_id=candidates.candidate_set_id,
                )
                return MeetingMinutesResult(
                    disposition=MeetingMinutesDisposition.DUPLICATE,
                    case=self._require_case(candidates.case_id),
                    snapshot=snapshot,
                    candidates=loaded,
                )
            current = self._require_case(candidates.case_id)
            if current.status is not CaseStatus.MEETING_READY:
                raise MeetingLifecycleConflictError(
                    "meeting case changed before candidates were committed"
                )
            version = self._require_version(candidates.case_id)
            updated = current.model_copy(deep=True)
            updated.updated_at = max(updated.updated_at, candidates.created_at)
            try:
                transition = self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=(f"meeting-candidates:v1:{candidates.candidate_set_id}"),
                    timeline_events=(event,),
                    artifacts=(artifact,),
                )
                if not transition.applied:
                    loaded = self._store.artifact(
                        MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
                        candidates.candidate_set_id,
                    )
                    if loaded is None:
                        raise MeetingLifecycleIntegrityError(
                            "idempotent candidate transition lost its artifact"
                        )
                    return MeetingMinutesResult(
                        disposition=MeetingMinutesDisposition.DUPLICATE,
                        case=self._require_case(candidates.case_id),
                        snapshot=snapshot,
                        candidates=self._load_candidates(
                            artifact=loaded,
                            snapshot=snapshot,
                            candidate_set_id=candidates.candidate_set_id,
                        ),
                    )
                return MeetingMinutesResult(
                    disposition=MeetingMinutesDisposition.CANDIDATES_RECORDED,
                    case=updated,
                    snapshot=snapshot,
                    candidates=candidates,
                )
            except (ConcurrencyConflict, IdempotencyConflict):
                continue
        raise ConcurrencyConflict("meeting candidates persistence exceeded retry limit")

    # -- Human decision confirmation ----------------------------------

    def confirm_decision(
        self,
        *,
        case_id: str,
        confirmation: MeetingDecisionConfirmation,
    ) -> MeetingDecisionResult:
        snapshot, candidates = self._require_candidates(case_id)
        decision_id = _stable_id("meeting-decision", case_id)
        self._reject_competing_singleton(
            kind=MEETING_DECISION_ARTIFACT_KIND,
            case_id=case_id,
            artifact_id=decision_id,
        )
        existing = self._store.artifact(MEETING_DECISION_ARTIFACT_KIND, decision_id)
        if existing is not None:
            decision = self._load_decision(
                artifact=existing,
                snapshot=snapshot,
                candidates=candidates,
                decision_id=decision_id,
            )
            if decision.confirmation != confirmation:
                raise MeetingLifecycleConflictError(
                    "meeting decision already has a different confirmation"
                )
            return MeetingDecisionResult(
                disposition=MeetingDecisionDisposition.DUPLICATE,
                case=self._require_case(case_id),
                decision=decision,
                verification_request=self._load_verification_request(
                    decision,
                    required=False,
                ),
            )
        now = self._clock.now()
        if confirmation.confirmed_at < candidates.created_at:
            raise MeetingLifecycleIntegrityError("meeting confirmation predates its candidates")
        if confirmation.confirmed_at > now:
            raise MeetingLifecycleIntegrityError("meeting confirmation cannot come from the future")
        authority = self._policy.authorize_meeting_actor(
            action=MeetingAuthorityAction.CONFIRM_DECISION,
            actor_id=confirmation.actor_id,
            authorized_actor_ids=snapshot.governance.authorized_decider_ids,
        )
        if not authority.allowed:
            raise MeetingLifecycleNotReadyError(authority.reason)
        decision_by_id = {item.decision_id: item for item in candidates.decisions}
        action_by_id = {item.action_id: item for item in candidates.action_items}
        if any(item_id not in decision_by_id for item_id in confirmation.confirmed_decision_ids):
            raise MeetingLifecycleIntegrityError(
                "confirmation named a decision outside the candidate set"
            )
        if any(item_id not in action_by_id for item_id in confirmation.confirmed_action_ids):
            raise MeetingLifecycleIntegrityError(
                "confirmation named an action outside the candidate set"
            )
        decisions = tuple(
            decision_by_id[item_id] for item_id in confirmation.confirmed_decision_ids
        )
        actions = tuple(action_by_id[item_id] for item_id in confirmation.confirmed_action_ids)
        source_ids = _ordered_unique(
            (candidates.candidate_set_id, snapshot.snapshot_id),
            confirmation.confirmed_decision_ids,
            confirmation.confirmed_action_ids,
            (confirmation.source_id, authority.rule_id),
        )
        decision = DurableMeetingDecision(
            meeting_decision_id=decision_id,
            candidate_set_id=candidates.candidate_set_id,
            snapshot_id=snapshot.snapshot_id,
            packet_id=snapshot.packet_id,
            case_id=case_id,
            confirmation=confirmation,
            decisions=decisions,
            action_items=actions,
            governance=snapshot.governance,
            authority_rule_id=authority.rule_id,
            created_at=now,
            source_ids=source_ids,
        )
        return self._persist_decision(decision=decision, snapshot=snapshot)

    def _persist_decision(
        self,
        *,
        decision: DurableMeetingDecision,
        snapshot: MeetingMinutesSnapshot,
    ) -> MeetingDecisionResult:
        decision_artifact = _artifact(
            kind=MEETING_DECISION_ARTIFACT_KIND,
            artifact_id=decision.meeting_decision_id,
            case_id=decision.case_id,
            created_at=decision.created_at,
            source_ids=decision.source_ids,
            payload=decision,
        )
        request = (
            None
            if decision.action_items
            else self._verification_request(decision, requested_at=decision.created_at)
        )
        artifacts = [decision_artifact]
        if request is not None:
            artifacts.append(
                _artifact(
                    kind=MEETING_VERIFICATION_REQUEST_ARTIFACT_KIND,
                    artifact_id=request.request_id,
                    case_id=request.case_id,
                    created_at=request.requested_at,
                    source_ids=request.source_ids,
                    payload=request,
                )
            )
        events = [
            TimelineEvent(
                event_id=_stable_id(
                    "event-meeting-decision",
                    decision.meeting_decision_id,
                ),
                case_id=decision.case_id,
                at=decision.created_at,
                kind=EventKind.MEETING_DECISION_CONFIRMED,
                actor=ActorType.HUMAN,
                actor_label=decision.confirmation.actor_label,
                summary=(
                    f"Confirmed {len(decision.decisions)} meeting decision(s) and "
                    f"{len(decision.action_items)} action item(s) from the closed "
                    "candidate set."
                ),
                refs=list(decision.source_ids),
                payload={
                    "meeting_decision_id": decision.meeting_decision_id,
                    "decision_ids": [item.decision_id for item in decision.decisions],
                    "action_ids": [item.action_id for item in decision.action_items],
                    "authority_rule_id": decision.authority_rule_id,
                    "model_output_is_authority": False,
                    "outbound_enabled": False,
                },
            )
        ]
        events.extend(
            TimelineEvent(
                event_id=_stable_id("event-action-opened", item.action_id),
                case_id=decision.case_id,
                at=decision.created_at,
                kind=EventKind.ACTION_ITEM_OPENED,
                actor=ActorType.HUMAN,
                actor_label=decision.confirmation.actor_label,
                summary=f"Opened meeting action: {item.description}",
                refs=[item.action_id, item.source_id, decision.meeting_decision_id],
                payload={
                    "action_id": item.action_id,
                    "owner_participant_id": item.owner_participant_id,
                    "due_at": item.due_at.isoformat(),
                },
            )
            for item in decision.action_items
        )
        if request is not None:
            events.append(_verification_requested_event(request))
        audit = AuditEntry(
            audit_id=_stable_id("audit-meeting-decision", decision.meeting_decision_id),
            case_id=decision.case_id,
            at=decision.created_at,
            action="confirm_meeting_decision",
            autonomy_level=AutonomyLevel.PREPARE_ONLY,
            policy_rule_id=decision.authority_rule_id,
            reason=(
                f"Configured actor {decision.confirmation.actor_id} confirmed exact "
                "closed-set meeting candidates."
            ),
            actor=ActorType.HUMAN,
        )
        for _attempt in range(3):
            existing = self._store.artifact(
                MEETING_DECISION_ARTIFACT_KIND,
                decision.meeting_decision_id,
            )
            if existing is not None:
                snapshot_now, candidates_now = self._require_candidates(decision.case_id)
                loaded = self._load_decision(
                    artifact=existing,
                    snapshot=snapshot_now,
                    candidates=candidates_now,
                    decision_id=decision.meeting_decision_id,
                )
                if loaded != decision:
                    raise MeetingLifecycleConflictError(
                        "meeting decision was concurrently confirmed differently"
                    )
                return MeetingDecisionResult(
                    disposition=MeetingDecisionDisposition.DUPLICATE,
                    case=self._require_case(decision.case_id),
                    decision=loaded,
                    verification_request=self._load_verification_request(
                        loaded,
                        required=False,
                    ),
                )
            current = self._require_case(decision.case_id)
            if current.status is not CaseStatus.MEETING_READY:
                raise MeetingLifecycleConflictError(
                    "meeting case changed before decision confirmation"
                )
            version = self._require_version(decision.case_id)
            updated = current.model_copy(deep=True)
            if decision.action_items:
                updated.status = CaseStatus.ACTIONS_TRACKING
                updated.next_action_due_at = min(item.due_at for item in decision.action_items)
            else:
                assert request is not None  # guaranteed by construction
                updated.status = CaseStatus.AWAITING_VERIFICATION
                updated.next_action_due_at = request.due_at
            updated.updated_at = max(updated.updated_at, decision.created_at)
            try:
                transition = self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=(f"meeting-decision:v1:{decision.meeting_decision_id}"),
                    timeline_events=tuple(events),
                    audit_entries=(audit,),
                    artifacts=tuple(artifacts),
                )
                if not transition.applied:
                    return MeetingDecisionResult(
                        disposition=MeetingDecisionDisposition.DUPLICATE,
                        case=self._require_case(decision.case_id),
                        decision=decision,
                        verification_request=self._load_verification_request(
                            decision,
                            required=False,
                        ),
                    )
                return MeetingDecisionResult(
                    disposition=MeetingDecisionDisposition.DECISION_CONFIRMED,
                    case=updated,
                    decision=decision,
                    verification_request=request,
                )
            except (ConcurrencyConflict, IdempotencyConflict):
                continue
        raise ConcurrencyConflict("meeting decision persistence exceeded retry limit")

    # -- Action completion --------------------------------------------

    def complete_action(
        self,
        *,
        case_id: str,
        action_id: str,
        actor_id: str,
        actor_label: str,
        source_id: str,
        notes: str,
        completed_at,
    ) -> MeetingActionCompletionResult:
        decision = self._require_decision(case_id)
        actions = {item.action_id: item for item in decision.action_items}
        action = actions.get(action_id)
        if action is None:
            raise MeetingLifecycleNotReadyError("action completion named an unconfirmed action")
        now = self._clock.now()
        if completed_at < decision.created_at or completed_at > now:
            raise MeetingLifecycleIntegrityError("action completion has invalid chronology")
        allowed_actors = _ordered_unique(
            (action.owner_participant_id,),
            decision.governance.authorized_verifier_ids,
        )
        authority = self._policy.authorize_meeting_actor(
            action=MeetingAuthorityAction.COMPLETE_ACTION,
            actor_id=actor_id,
            authorized_actor_ids=allowed_actors,
        )
        if not authority.allowed:
            raise MeetingLifecycleNotReadyError(authority.reason)
        completion_id = _stable_id("meeting-action-completion", case_id, action_id)
        completion = MeetingActionCompletion(
            completion_id=completion_id,
            case_id=case_id,
            meeting_decision_id=decision.meeting_decision_id,
            action_id=action_id,
            actor_id=actor_id,
            actor_label=actor_label,
            source_id=source_id,
            notes=notes,
            completed_at=completed_at,
            authority_rule_id=authority.rule_id,
            source_ids=(decision.meeting_decision_id, action_id, source_id),
        )
        existing = self._store.artifact(
            MEETING_ACTION_COMPLETION_ARTIFACT_KIND,
            completion_id,
        )
        if existing is not None:
            loaded = self._load_completion(existing, decision)
            if loaded != completion:
                raise MeetingLifecycleConflictError(
                    "meeting action already has different completion evidence"
                )
            return MeetingActionCompletionResult(
                disposition=MeetingActionCompletionDisposition.DUPLICATE,
                case=self._require_case(case_id),
                completion=loaded,
                verification_request=self._load_verification_request(
                    decision,
                    required=False,
                ),
            )
        return self._persist_completion(decision=decision, completion=completion)

    def _persist_completion(
        self,
        *,
        decision: DurableMeetingDecision,
        completion: MeetingActionCompletion,
    ) -> MeetingActionCompletionResult:
        completion_artifact = _artifact(
            kind=MEETING_ACTION_COMPLETION_ARTIFACT_KIND,
            artifact_id=completion.completion_id,
            case_id=completion.case_id,
            created_at=completion.completed_at,
            source_ids=completion.source_ids,
            payload=completion,
        )
        for _attempt in range(3):
            existing = self._store.artifact(
                MEETING_ACTION_COMPLETION_ARTIFACT_KIND,
                completion.completion_id,
            )
            if existing is not None:
                loaded = self._load_completion(existing, decision)
                if loaded != completion:
                    raise MeetingLifecycleConflictError(
                        "meeting action was concurrently completed differently"
                    )
                return MeetingActionCompletionResult(
                    disposition=MeetingActionCompletionDisposition.DUPLICATE,
                    case=self._require_case(completion.case_id),
                    completion=loaded,
                    verification_request=self._load_verification_request(
                        decision,
                        required=False,
                    ),
                )
            current = self._require_case(completion.case_id)
            if current.status not in (
                CaseStatus.ACTIONS_TRACKING,
                CaseStatus.ATTENTION_REQUIRED,
            ):
                raise MeetingLifecycleNotReadyError("case is not tracking meeting actions")
            decision = self._require_decision(completion.case_id)
            persisted = self._completion_map(decision)
            if completion.action_id in persisted:
                continue
            after = {**persisted, completion.action_id: completion}
            incomplete = tuple(
                item for item in decision.action_items if item.action_id not in after
            )
            request = None
            if not incomplete:
                request = self._verification_request(
                    decision,
                    requested_at=self._clock.now(),
                    completion_ids=tuple(
                        after[item.action_id].completion_id for item in decision.action_items
                    ),
                )
            artifacts = [completion_artifact]
            events = [
                TimelineEvent(
                    event_id=_stable_id(
                        "event-action-completed",
                        completion.completion_id,
                    ),
                    case_id=completion.case_id,
                    at=completion.completed_at,
                    kind=EventKind.ACTION_ITEM_COMPLETED,
                    actor=ActorType.HUMAN,
                    actor_label=completion.actor_label,
                    summary=f"Completed meeting action {completion.action_id}: {completion.notes}",
                    refs=list(completion.source_ids),
                    payload={
                        "action_id": completion.action_id,
                        "completion_id": completion.completion_id,
                        "authority_rule_id": completion.authority_rule_id,
                        "outbound_enabled": False,
                    },
                )
            ]
            if request is not None:
                prior_request = self._load_verification_request(decision, required=False)
                if prior_request is not None and prior_request != request:
                    raise MeetingLifecycleConflictError(
                        "meeting verification request already contains different evidence"
                    )
                if prior_request is None:
                    artifacts.append(
                        _artifact(
                            kind=MEETING_VERIFICATION_REQUEST_ARTIFACT_KIND,
                            artifact_id=request.request_id,
                            case_id=request.case_id,
                            created_at=request.requested_at,
                            source_ids=request.source_ids,
                            payload=request,
                        )
                    )
                    events.append(_verification_requested_event(request))
            version = self._require_version(completion.case_id)
            updated = current.model_copy(deep=True)
            if request is not None:
                updated.status = CaseStatus.AWAITING_VERIFICATION
                updated.next_action_due_at = request.due_at
            else:
                trackable = self._trackable_actions(decision, incomplete)
                has_escalated = len(trackable) != len(incomplete)
                updated.status = (
                    CaseStatus.ATTENTION_REQUIRED if has_escalated else CaseStatus.ACTIONS_TRACKING
                )
                updated.next_action_due_at = (
                    min(self._effective_due(item, decision) for item in trackable)
                    if trackable
                    else None
                )
            updated.updated_at = max(updated.updated_at, completion.completed_at)
            try:
                transition = self._store.save_transition(
                    case=updated,
                    expected_version=version,
                    idempotency_key=(f"meeting-action-completion:v1:{completion.completion_id}"),
                    timeline_events=tuple(events),
                    artifacts=tuple(artifacts),
                )
                if not transition.applied:
                    return MeetingActionCompletionResult(
                        disposition=MeetingActionCompletionDisposition.DUPLICATE,
                        case=self._require_case(completion.case_id),
                        completion=completion,
                        verification_request=self._load_verification_request(
                            decision,
                            required=False,
                        ),
                    )
                return MeetingActionCompletionResult(
                    disposition=MeetingActionCompletionDisposition.COMPLETED,
                    case=updated,
                    completion=completion,
                    verification_request=request,
                )
            except (ConcurrencyConflict, IdempotencyConflict):
                continue
        raise ConcurrencyConflict("meeting action persistence exceeded retry limit")

    # -- Internal due orchestration -----------------------------------

    def sweep_due(self, *, case_ids: tuple[str, ...]) -> tuple[MeetingDueResult, ...]:
        results: list[MeetingDueResult] = []
        for case_id in case_ids:
            result = self.sweep_case(case_id)
            if result.disposition is not MeetingDueDisposition.INACTIVE:
                results.append(result)
        return tuple(results)

    def sweep_case(self, case_id: str) -> MeetingDueResult:
        current = self._store.get_case(case_id)
        if current is None:
            raise MeetingLifecycleNotReadyError("unknown meeting due case")
        if current.status in (CaseStatus.CLOSED, CaseStatus.CANCELLED):
            return MeetingDueResult(MeetingDueDisposition.INACTIVE, current)
        decision_artifacts = self._store.artifacts_for(
            kind=MEETING_DECISION_ARTIFACT_KIND,
            case_id=case_id,
        )
        if not decision_artifacts:
            return MeetingDueResult(MeetingDueDisposition.INACTIVE, current)
        decision = self._require_decision(case_id)
        if current.status is CaseStatus.AWAITING_VERIFICATION:
            return self._sweep_verification(current=current, decision=decision)
        if current.status not in (
            CaseStatus.ACTIONS_TRACKING,
            CaseStatus.ATTENTION_REQUIRED,
        ):
            return MeetingDueResult(MeetingDueDisposition.INACTIVE, current)
        return self._sweep_actions(current=current, decision=decision)

    def _sweep_actions(
        self,
        *,
        current: Case,
        decision: DurableMeetingDecision,
    ) -> MeetingDueResult:
        now = self._clock.now()
        for _attempt in range(3):
            current = self._require_case(current.case_id)
            completions = self._completion_map(decision)
            incomplete = tuple(
                item for item in decision.action_items if item.action_id not in completions
            )
            if not incomplete:
                return MeetingDueResult(MeetingDueDisposition.INACTIVE, current)
            trackable = self._trackable_actions(decision, incomplete)
            if not trackable:
                return MeetingDueResult(MeetingDueDisposition.INACTIVE, current)
            ranked = sorted(
                ((self._effective_due(item, decision), item) for item in trackable),
                key=lambda pair: (pair[0], pair[1].action_id),
            )
            due_at, action = ranked[0]
            if due_at > now:
                return MeetingDueResult(MeetingDueDisposition.NOT_DUE, current)
            reminders = self._action_reminders(action.action_id, decision)
            if len(reminders) < decision.governance.max_reminders:
                attempt = len(reminders) + 1
                reminder_id = _stable_id(
                    "meeting-action-reminder",
                    action.action_id,
                    str(attempt),
                )
                existing = self._store.artifact(
                    MEETING_ACTION_REMINDER_ARTIFACT_KIND,
                    reminder_id,
                )
                if existing is not None:
                    reminder = self._load_reminder(existing, decision, action)
                    return MeetingDueResult(
                        MeetingDueDisposition.DUPLICATE,
                        current,
                        reminder=reminder,
                    )
                next_due = now + timedelta(hours=decision.governance.reminder_interval_hours)
                reminder = MeetingActionReminder(
                    reminder_id=reminder_id,
                    case_id=current.case_id,
                    meeting_decision_id=decision.meeting_decision_id,
                    action_id=action.action_id,
                    attempt=attempt,
                    created_at=now,
                    next_due_at=next_due,
                    source_ids=(decision.meeting_decision_id, action.action_id),
                )
                artifact = _artifact(
                    kind=MEETING_ACTION_REMINDER_ARTIFACT_KIND,
                    artifact_id=reminder.reminder_id,
                    case_id=current.case_id,
                    created_at=reminder.created_at,
                    source_ids=reminder.source_ids,
                    payload=reminder,
                )
                event = TimelineEvent(
                    event_id=_stable_id("event-action-reminder", reminder.reminder_id),
                    case_id=current.case_id,
                    at=now,
                    kind=EventKind.ACTION_ITEM_REMINDER,
                    actor=ActorType.SYSTEM,
                    summary=(
                        f"Recorded internal reminder {attempt} for overdue meeting "
                        f"action {action.action_id}; no message was sent."
                    ),
                    refs=list(reminder.source_ids),
                    payload={
                        "action_id": action.action_id,
                        "attempt": attempt,
                        "next_due_at": next_due.isoformat(),
                        "internal_only": True,
                        "outbound_enabled": False,
                    },
                )
                effective = [
                    next_due
                    if item.action_id == action.action_id
                    else self._effective_due(item, decision)
                    for item in trackable
                ]
                updated = current.model_copy(deep=True)
                updated.status = (
                    CaseStatus.ATTENTION_REQUIRED
                    if len(trackable) != len(incomplete)
                    else CaseStatus.ACTIONS_TRACKING
                )
                updated.next_action_due_at = min(effective)
                updated.follow_up_count += 1
                updated.updated_at = now
                try:
                    self._store.save_transition(
                        case=updated,
                        expected_version=self._require_version(current.case_id),
                        idempotency_key=f"meeting-reminder:v1:{reminder.reminder_id}",
                        timeline_events=(event,),
                        artifacts=(artifact,),
                    )
                    return MeetingDueResult(
                        MeetingDueDisposition.REMINDER_RECORDED,
                        updated,
                        reminder=reminder,
                    )
                except (ConcurrencyConflict, IdempotencyConflict):
                    continue
            escalation_id = _stable_id("meeting-action-escalation", action.action_id)
            existing = self._store.artifact(
                MEETING_ESCALATION_ARTIFACT_KIND,
                escalation_id,
            )
            if existing is not None:
                escalation = self._load_escalation(existing, decision)
                return MeetingDueResult(
                    MeetingDueDisposition.DUPLICATE,
                    current,
                    escalation=escalation,
                )
            escalation = MeetingEscalation(
                escalation_id=escalation_id,
                case_id=current.case_id,
                meeting_decision_id=decision.meeting_decision_id,
                action_id=action.action_id,
                reason=(f"Action remained incomplete after {len(reminders)} internal reminder(s)."),
                created_at=now,
                source_ids=(decision.meeting_decision_id, action.action_id),
            )
            event = TimelineEvent(
                event_id=_stable_id("event-meeting-escalation", escalation_id),
                case_id=current.case_id,
                at=now,
                kind=EventKind.ESCALATED,
                actor=ActorType.SYSTEM,
                summary=(
                    f"Escalated overdue meeting action {action.action_id} for internal "
                    "attention; no message was sent."
                ),
                refs=list(escalation.source_ids),
                payload={
                    "escalation_id": escalation_id,
                    "action_id": action.action_id,
                    "internal_only": True,
                    "outbound_enabled": False,
                },
            )
            remaining_trackable = tuple(
                item for item in trackable if item.action_id != action.action_id
            )
            updated = current.model_copy(deep=True)
            updated.status = CaseStatus.ATTENTION_REQUIRED
            updated.escalated_at = now
            updated.next_action_due_at = (
                min(self._effective_due(item, decision) for item in remaining_trackable)
                if remaining_trackable
                else None
            )
            updated.updated_at = now
            try:
                self._store.save_transition(
                    case=updated,
                    expected_version=self._require_version(current.case_id),
                    idempotency_key=f"meeting-escalation:v1:{escalation_id}",
                    timeline_events=(event,),
                    artifacts=(
                        _artifact(
                            kind=MEETING_ESCALATION_ARTIFACT_KIND,
                            artifact_id=escalation.escalation_id,
                            case_id=escalation.case_id,
                            created_at=escalation.created_at,
                            source_ids=escalation.source_ids,
                            payload=escalation,
                        ),
                    ),
                )
                return MeetingDueResult(
                    MeetingDueDisposition.ESCALATED,
                    updated,
                    escalation=escalation,
                )
            except (ConcurrencyConflict, IdempotencyConflict):
                continue
        raise ConcurrencyConflict("meeting due-action sweep exceeded retry limit")

    def _sweep_verification(
        self,
        *,
        current: Case,
        decision: DurableMeetingDecision,
    ) -> MeetingDueResult:
        request = self._load_verification_request(decision, required=True)
        assert request is not None
        now = self._clock.now()
        if request.due_at > now:
            return MeetingDueResult(MeetingDueDisposition.NOT_DUE, current)
        response = self._store.artifacts_for(
            kind=MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
            case_id=current.case_id,
        )
        if response:
            return MeetingDueResult(MeetingDueDisposition.INACTIVE, current)
        escalation_id = _stable_id("meeting-verification-escalation", request.request_id)
        existing = self._store.artifact(MEETING_ESCALATION_ARTIFACT_KIND, escalation_id)
        if existing is not None:
            return MeetingDueResult(
                MeetingDueDisposition.DUPLICATE,
                current,
                escalation=self._load_escalation(existing, decision),
            )
        escalation = MeetingEscalation(
            escalation_id=escalation_id,
            case_id=current.case_id,
            meeting_decision_id=decision.meeting_decision_id,
            verification_request_id=request.request_id,
            reason="Meeting outcome verification deadline expired without a response.",
            created_at=now,
            source_ids=(decision.meeting_decision_id, request.request_id),
        )
        event = TimelineEvent(
            event_id=_stable_id("event-meeting-escalation", escalation_id),
            case_id=current.case_id,
            at=now,
            kind=EventKind.ESCALATED,
            actor=ActorType.SYSTEM,
            summary=(
                "Escalated overdue meeting outcome verification for internal attention; "
                "no message was sent."
            ),
            refs=list(escalation.source_ids),
            payload={
                "escalation_id": escalation_id,
                "verification_request_id": request.request_id,
                "internal_only": True,
                "outbound_enabled": False,
            },
        )
        updated = current.model_copy(deep=True)
        updated.status = CaseStatus.ATTENTION_REQUIRED
        updated.escalated_at = now
        updated.next_action_due_at = None
        updated.updated_at = now
        try:
            self._store.save_transition(
                case=updated,
                expected_version=self._require_version(current.case_id),
                idempotency_key=f"meeting-escalation:v1:{escalation_id}",
                timeline_events=(event,),
                artifacts=(
                    _artifact(
                        kind=MEETING_ESCALATION_ARTIFACT_KIND,
                        artifact_id=escalation.escalation_id,
                        case_id=escalation.case_id,
                        created_at=escalation.created_at,
                        source_ids=escalation.source_ids,
                        payload=escalation,
                    ),
                ),
            )
        except (ConcurrencyConflict, IdempotencyConflict):
            persisted = self._store.artifact(
                MEETING_ESCALATION_ARTIFACT_KIND,
                escalation_id,
            )
            if persisted is None:
                raise
            return MeetingDueResult(
                MeetingDueDisposition.DUPLICATE,
                self._require_case(current.case_id),
                escalation=self._load_escalation(persisted, decision),
            )
        return MeetingDueResult(
            MeetingDueDisposition.ESCALATED,
            updated,
            escalation=escalation,
        )

    # -- Final verification and closure -------------------------------

    def verify_outcome(
        self,
        *,
        case_id: str,
        outcome: MeetingVerificationOutcome,
        actor_id: str,
        actor_label: str,
        source_id: str,
        notes: str,
        responded_at,
    ) -> MeetingVerificationResult:
        decision = self._require_decision(case_id)
        request = self._load_verification_request(decision, required=True)
        assert request is not None
        authority = self._policy.authorize_meeting_actor(
            action=MeetingAuthorityAction.VERIFY_OUTCOME,
            actor_id=actor_id,
            authorized_actor_ids=request.authorized_verifier_ids,
        )
        if not authority.allowed:
            raise MeetingLifecycleNotReadyError(authority.reason)
        now = self._clock.now()
        if responded_at < request.requested_at or responded_at > now:
            raise MeetingLifecycleIntegrityError(
                "meeting verification response has invalid chronology"
            )
        prior_responses = self._verification_responses(request, decision)
        if prior_responses and responded_at < prior_responses[-1].responded_at:
            raise MeetingLifecycleIntegrityError(
                "meeting verification response predates the prior attempt"
            )
        if prior_responses:
            last = prior_responses[-1]
            repeated = MeetingVerificationResponse(
                response_id=last.response_id,
                request_id=request.request_id,
                case_id=case_id,
                attempt=last.attempt,
                outcome=outcome,
                actor_id=actor_id,
                actor_label=actor_label,
                source_id=source_id,
                notes=notes,
                responded_at=responded_at,
                authority_rule_id=authority.rule_id,
                source_ids=(
                    request.request_id,
                    decision.meeting_decision_id,
                    source_id,
                ),
            )
            if repeated == last:
                return MeetingVerificationResult(
                    disposition=MeetingVerificationDisposition.DUPLICATE,
                    case=self._require_case(case_id),
                    response=last,
                    memory_record=self._store.get(case_id),
                )
            if last.outcome is MeetingVerificationOutcome.CONFIRMED:
                raise MeetingLifecycleConflictError("confirmed meeting outcome cannot be replaced")
        attempt = len(prior_responses) + 1
        if attempt > 20:
            raise MeetingLifecycleNotReadyError("meeting verification attempt limit was reached")
        response_id = _verification_response_id(case_id, attempt)
        response = MeetingVerificationResponse(
            response_id=response_id,
            request_id=request.request_id,
            case_id=case_id,
            attempt=attempt,
            outcome=outcome,
            actor_id=actor_id,
            actor_label=actor_label,
            source_id=source_id,
            notes=notes,
            responded_at=responded_at,
            authority_rule_id=authority.rule_id,
            source_ids=(request.request_id, decision.meeting_decision_id, source_id),
        )
        for _retry in range(3):
            existing = self._store.artifact(
                MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
                response_id,
            )
            if existing is not None:
                loaded = self._load_verification_response(existing, request, decision)
                if loaded != response:
                    raise MeetingLifecycleConflictError(
                        "meeting verification attempt already has a different response"
                    )
                current = self._require_case(case_id)
                memory = self._store.get(case_id)
                return MeetingVerificationResult(
                    disposition=MeetingVerificationDisposition.DUPLICATE,
                    case=current,
                    response=loaded,
                    memory_record=memory,
                )
            completions = self._completion_map(decision)
            if any(item.action_id not in completions for item in decision.action_items):
                raise MeetingLifecycleNotReadyError(
                    "meeting outcome cannot be verified before every action is complete"
                )
            try:
                return self._persist_verification(
                    decision=decision,
                    request=request,
                    response=response,
                    prior_responses=prior_responses,
                )
            except (ConcurrencyConflict, IdempotencyConflict):
                continue
            except MeetingLifecycleNotReadyError:
                if (
                    self._store.artifact(
                        MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
                        response_id,
                    )
                    is None
                ):
                    raise
                continue
        raise ConcurrencyConflict("meeting verification exceeded retry limit")

    def _persist_verification(
        self,
        *,
        decision: DurableMeetingDecision,
        request: MeetingVerificationRequest,
        response: MeetingVerificationResponse,
        prior_responses: tuple[MeetingVerificationResponse, ...],
    ) -> MeetingVerificationResult:
        if self._verification_responses(request, decision) != prior_responses:
            raise ConcurrencyConflict("meeting verification history changed before persistence")
        if response.attempt != len(prior_responses) + 1:
            raise MeetingLifecycleIntegrityError("meeting verification attempt is not contiguous")
        current = self._require_case(decision.case_id)
        if current.status not in (
            CaseStatus.AWAITING_VERIFICATION,
            CaseStatus.ATTENTION_REQUIRED,
        ):
            raise MeetingLifecycleNotReadyError("case is not awaiting meeting outcome verification")
        response_artifact = _artifact(
            kind=MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
            artifact_id=response.response_id,
            case_id=response.case_id,
            created_at=response.responded_at,
            source_ids=response.source_ids,
            payload=response,
        )
        if response.outcome is MeetingVerificationOutcome.REJECTED:
            event = TimelineEvent(
                event_id=_stable_id("event-meeting-verification", response.response_id),
                case_id=response.case_id,
                at=response.responded_at,
                kind=EventKind.VERIFICATION_REJECTED,
                actor=ActorType.HUMAN,
                actor_label=response.actor_label,
                summary="A configured verifier rejected the claimed meeting outcome.",
                refs=list(response.source_ids),
                payload={
                    "response_id": response.response_id,
                    "attempt": response.attempt,
                    "authority_rule_id": response.authority_rule_id,
                    "outbound_enabled": False,
                },
            )
            updated = current.model_copy(deep=True)
            updated.status = CaseStatus.ATTENTION_REQUIRED
            updated.next_action_due_at = None
            updated.escalated_at = response.responded_at
            updated.resolution_notes = response.notes
            updated.updated_at = max(updated.updated_at, response.responded_at)
            transition = self._store.save_transition(
                case=updated,
                expected_version=self._require_version(response.case_id),
                idempotency_key=f"meeting-verification:v1:{response.response_id}",
                timeline_events=(event,),
                artifacts=(response_artifact,),
            )
            if not transition.applied:
                return MeetingVerificationResult(
                    disposition=MeetingVerificationDisposition.DUPLICATE,
                    case=self._require_case(response.case_id),
                    response=response,
                    memory_record=self._store.get(response.case_id),
                    transition=transition,
                )
            return MeetingVerificationResult(
                disposition=MeetingVerificationDisposition.REJECTED,
                case=updated,
                response=response,
                transition=transition,
            )

        snapshot, _candidates = self._require_candidates(decision.case_id)
        completions = self._completion_map(decision)
        closed_at = self._clock.now()
        if closed_at < response.responded_at:
            raise MeetingLifecycleIntegrityError("meeting closure cannot predate verification")
        memory = self._meeting_memory(
            case=current,
            snapshot=snapshot,
            decision=decision,
            completions=completions,
            response=response,
            responses=(*prior_responses, response),
            closed_at=closed_at,
        )
        updated = current.model_copy(deep=True)
        updated.status = CaseStatus.CLOSED
        updated.resolved_at = response.responded_at
        updated.closed_at = closed_at
        updated.total_cost = Decimal("0")
        updated.resolution_notes = response.notes
        updated.next_action_due_at = None
        updated.updated_at = closed_at
        events = (
            TimelineEvent(
                event_id=_stable_id("event-meeting-verification", response.response_id),
                case_id=response.case_id,
                at=response.responded_at,
                kind=EventKind.VERIFICATION_CONFIRMED,
                actor=ActorType.HUMAN,
                actor_label=response.actor_label,
                summary="A configured verifier confirmed the meeting outcome and actions.",
                refs=list(response.source_ids),
                payload={
                    "response_id": response.response_id,
                    "attempt": response.attempt,
                    "authority_rule_id": response.authority_rule_id,
                },
            ),
            TimelineEvent(
                event_id=_stable_id("event-meeting-resolved", response.response_id),
                case_id=response.case_id,
                at=response.responded_at,
                kind=EventKind.RESOLVED,
                actor=ActorType.HUMAN,
                actor_label=response.actor_label,
                summary="Verified that the confirmed meeting decisions were carried out.",
                refs=list(memory.resolution_source_ids),
                payload={"cost": "0.00", "currency": current.currency},
            ),
            TimelineEvent(
                event_id=_stable_id("event-meeting-memory", response.response_id),
                case_id=response.case_id,
                at=closed_at,
                kind=EventKind.MEMORY_WRITTEN,
                actor=ActorType.SYSTEM,
                summary="Wrote the verified meeting decisions and actions to memory.",
                refs=[decision.meeting_decision_id, response.source_id],
                payload={
                    "decision_count": len(decision.decisions),
                    "action_count": len(decision.action_items),
                    "had_vendor": False,
                },
            ),
            TimelineEvent(
                event_id=_stable_id("event-meeting-closed", response.response_id),
                case_id=response.case_id,
                at=closed_at,
                kind=EventKind.CLOSED,
                actor=ActorType.SYSTEM,
                summary="Closed the meeting case after verified memory write-back.",
                refs=[decision.meeting_decision_id, response.response_id],
                payload={"memory_written": True, "outbound_enabled": False},
            ),
        )
        transition = self._store.save_transition(
            case=updated,
            expected_version=self._require_version(response.case_id),
            idempotency_key=f"meeting-verification:v1:{response.response_id}",
            timeline_events=events,
            artifacts=(response_artifact,),
            memory_records=(memory,),
        )
        if not transition.applied:
            return MeetingVerificationResult(
                disposition=MeetingVerificationDisposition.DUPLICATE,
                case=self._require_case(response.case_id),
                response=response,
                memory_record=self._store.get(response.case_id),
                transition=transition,
            )
        return MeetingVerificationResult(
            disposition=MeetingVerificationDisposition.CLOSED,
            case=updated,
            response=response,
            memory_record=memory,
            transition=transition,
        )

    def _meeting_memory(
        self,
        *,
        case: Case,
        snapshot: MeetingMinutesSnapshot,
        decision: DurableMeetingDecision,
        completions: dict[str, MeetingActionCompletion],
        response: MeetingVerificationResponse,
        responses: tuple[MeetingVerificationResponse, ...],
        closed_at,
    ) -> CaseRecord:
        first_message = (
            self._store.get_message(case.source_message_ids[0]) if case.source_message_ids else None
        )
        decision_texts = [item.statement for item in decision.decisions]
        action_texts = [
            f"{item.description} — {completions[item.action_id].notes}"
            for item in decision.action_items
        ]
        performed_parts = [
            "Decisions: " + " ".join(decision_texts),
        ]
        if action_texts:
            performed_parts.append("Completed actions: " + " ".join(action_texts))
        completion_ids = tuple(
            completions[item.action_id].completion_id for item in decision.action_items
        )
        return CaseRecord(
            case_id=case.case_id,
            title=case.title,
            category=case.category,
            asset_id=case.asset_id,
            urgency=case.urgency,
            is_simulated=snapshot.simulated,
            outcome_verified=True,
            opened_at=case.opened_at,
            raised_by=first_message.sender_display if first_message else "",
            raised_as=first_message.text if first_message else "",
            problem=case.title,
            selected_vendor_id=None,
            selection_rationale=" ".join(decision_texts),
            selection_source_ids=list(decision.source_ids),
            resolved_at=response.responded_at,
            cost=Decimal("0"),
            currency=case.currency,
            work_performed=" ".join(performed_parts),
            resolution_notes=response.notes,
            resolution_source_ids=list(
                _ordered_unique(
                    (snapshot.source_id, decision.meeting_decision_id),
                    completion_ids,
                )
            ),
            verification_source_ids=[item.source_id for item in responses],
            meeting_held_at=snapshot.held_at,
            meeting_decisions=decision_texts,
            meeting_action_items=action_texts,
            meeting_participant_count=snapshot.participant_count,
            closed_at=closed_at,
        )

    # -- Load and invariant helpers -----------------------------------

    def _require_candidates(
        self,
        case_id: str,
    ) -> tuple[MeetingMinutesSnapshot, MeetingDecisionCandidateSet]:
        snapshots = self._store.artifacts_for(
            kind=MEETING_MINUTES_ARTIFACT_KIND,
            case_id=case_id,
        )
        candidates = self._store.artifacts_for(
            kind=MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
            case_id=case_id,
        )
        if len(snapshots) != 1 or len(candidates) != 1:
            raise MeetingLifecycleNotReadyError(
                "meeting decision requires one complete candidate set"
            )
        try:
            raw_snapshot = MeetingMinutesSnapshot.model_validate(snapshots[0].payload)
        except ValueError as exc:
            raise MeetingLifecycleIntegrityError(
                "persisted meeting minutes snapshot is invalid"
            ) from exc
        snapshot = self._load_snapshot(
            artifact=snapshots[0],
            request_key_hash=raw_snapshot.request_key_sha256,
            expected_body=raw_snapshot.body_text,
            expected_source=raw_snapshot.source_id,
            expected_held_at=raw_snapshot.held_at,
            expected_participant_count=raw_snapshot.participant_count,
            expected_governance=raw_snapshot.governance,
            expected_simulated=raw_snapshot.simulated,
        )
        candidate_set = self._load_candidates(
            artifact=candidates[0],
            snapshot=snapshot,
            candidate_set_id=candidates[0].artifact_id,
        )
        return snapshot, candidate_set

    def _load_snapshot(
        self,
        *,
        artifact: WorkflowArtifact,
        request_key_hash: str,
        expected_body: str,
        expected_source: str,
        expected_held_at,
        expected_participant_count: int,
        expected_governance: MeetingGovernancePolicy,
        expected_simulated: bool,
    ) -> MeetingMinutesSnapshot:
        try:
            snapshot = MeetingMinutesSnapshot.model_validate(artifact.payload)
        except ValueError as exc:
            raise MeetingLifecycleIntegrityError(
                "persisted meeting minutes snapshot is invalid"
            ) from exc
        prepared = self._meeting.load(snapshot.case_id)
        expected_sources = (snapshot.packet_id, snapshot.source_id)
        expected_snapshot_id = _stable_id("meeting-minutes", snapshot.case_id)
        if (
            artifact.kind != MEETING_MINUTES_ARTIFACT_KIND
            or artifact.artifact_id != snapshot.snapshot_id
            or snapshot.snapshot_id != expected_snapshot_id
            or artifact.case_id != snapshot.case_id
            or artifact.source_ids != snapshot.source_ids
            or snapshot.request_key_sha256 != request_key_hash
            or snapshot.body_text != expected_body
            or snapshot.source_id != expected_source
            or snapshot.held_at != expected_held_at
            or snapshot.participant_count != expected_participant_count
            or snapshot.governance != expected_governance
            or snapshot.simulated != expected_simulated
            or snapshot.source_ids != expected_sources
            or prepared is None
            or prepared.packet.packet_id != snapshot.packet_id
        ):
            raise MeetingLifecycleConflictError(
                "persisted meeting minutes do not match their frozen request"
            )
        if (
            snapshot.held_at < prepared.packet.schedule.selected_slot.starts_at
            or snapshot.held_at > snapshot.recorded_at
            or snapshot.participant_count < prepared.packet.schedule.policy.quorum
            or not set(snapshot.governance.action_owner_ids).issubset(
                set(prepared.packet.schedule.policy.eligible_participant_ids)
            )
        ):
            raise MeetingLifecycleIntegrityError(
                "persisted meeting minutes violate schedule or governance invariants"
            )
        return snapshot

    def _load_candidates(
        self,
        *,
        artifact: WorkflowArtifact,
        snapshot: MeetingMinutesSnapshot,
        candidate_set_id: str,
    ) -> MeetingDecisionCandidateSet:
        try:
            candidates = MeetingDecisionCandidateSet.model_validate(artifact.payload)
        except ValueError as exc:
            raise MeetingLifecycleIntegrityError(
                "persisted meeting candidates are invalid"
            ) from exc
        self._validate_extraction(snapshot, candidates.extraction)
        expected = self._candidate_set(
            snapshot=snapshot,
            extraction=candidates.extraction,
            candidate_set_id=candidate_set_id,
            created_at=candidates.created_at,
        )
        expected_candidate_set_id = _stable_id(
            "meeting-candidates",
            snapshot.snapshot_id,
        )
        if (
            artifact.kind != MEETING_DECISION_CANDIDATES_ARTIFACT_KIND
            or artifact.artifact_id != candidate_set_id
            or candidate_set_id != expected_candidate_set_id
            or artifact.case_id != snapshot.case_id
            or candidates != expected
            or artifact.source_ids != candidates.source_ids
        ):
            raise MeetingLifecycleIntegrityError(
                "persisted meeting candidates violate identity or evidence invariants"
            )
        return candidates

    def _require_decision(self, case_id: str) -> DurableMeetingDecision:
        artifacts = self._store.artifacts_for(
            kind=MEETING_DECISION_ARTIFACT_KIND,
            case_id=case_id,
        )
        if len(artifacts) != 1:
            raise MeetingLifecycleNotReadyError("meeting action requires one confirmed decision")
        snapshot, candidates = self._require_candidates(case_id)
        return self._load_decision(
            artifact=artifacts[0],
            snapshot=snapshot,
            candidates=candidates,
            decision_id=artifacts[0].artifact_id,
        )

    def _load_decision(
        self,
        *,
        artifact: WorkflowArtifact,
        snapshot: MeetingMinutesSnapshot,
        candidates: MeetingDecisionCandidateSet,
        decision_id: str,
    ) -> DurableMeetingDecision:
        try:
            decision = DurableMeetingDecision.model_validate(artifact.payload)
        except ValueError as exc:
            raise MeetingLifecycleIntegrityError("persisted meeting decision is invalid") from exc
        by_decision = {item.decision_id: item for item in candidates.decisions}
        by_action = {item.action_id: item for item in candidates.action_items}
        expected_decisions = tuple(
            by_decision[item_id]
            for item_id in decision.confirmation.confirmed_decision_ids
            if item_id in by_decision
        )
        expected_actions = tuple(
            by_action[item_id]
            for item_id in decision.confirmation.confirmed_action_ids
            if item_id in by_action
        )
        expected_sources = _ordered_unique(
            (candidates.candidate_set_id, snapshot.snapshot_id),
            decision.confirmation.confirmed_decision_ids,
            decision.confirmation.confirmed_action_ids,
            (
                decision.confirmation.source_id,
                decision.authority_rule_id,
            ),
        )
        authority = self._policy.authorize_meeting_actor(
            action=MeetingAuthorityAction.CONFIRM_DECISION,
            actor_id=decision.confirmation.actor_id,
            authorized_actor_ids=snapshot.governance.authorized_decider_ids,
        )
        if (
            not authority.allowed
            or authority.rule_id != decision.authority_rule_id
            or decision.confirmation.confirmed_at < candidates.created_at
            or decision.created_at < decision.confirmation.confirmed_at
        ):
            raise MeetingLifecycleIntegrityError(
                "persisted meeting decision violates actor authority or chronology"
            )
        expected_decision_id = _stable_id("meeting-decision", snapshot.case_id)
        if (
            artifact.kind != MEETING_DECISION_ARTIFACT_KIND
            or artifact.artifact_id != decision_id
            or decision_id != expected_decision_id
            or artifact.case_id != snapshot.case_id
            or decision.meeting_decision_id != decision_id
            or decision.candidate_set_id != candidates.candidate_set_id
            or decision.snapshot_id != snapshot.snapshot_id
            or decision.packet_id != snapshot.packet_id
            or decision.case_id != snapshot.case_id
            or decision.governance != snapshot.governance
            or decision.decisions != expected_decisions
            or len(expected_decisions) != len(decision.confirmation.confirmed_decision_ids)
            or decision.action_items != expected_actions
            or len(expected_actions) != len(decision.confirmation.confirmed_action_ids)
            or decision.source_ids != expected_sources
            or artifact.source_ids != decision.source_ids
        ):
            raise MeetingLifecycleIntegrityError(
                "persisted meeting decision violates closed-set invariants"
            )
        return decision

    def _completion_map(
        self,
        decision: DurableMeetingDecision,
    ) -> dict[str, MeetingActionCompletion]:
        result: dict[str, MeetingActionCompletion] = {}
        for artifact in self._store.artifacts_for(
            kind=MEETING_ACTION_COMPLETION_ARTIFACT_KIND,
            case_id=decision.case_id,
        ):
            completion = self._load_completion(artifact, decision)
            if completion.action_id in result:
                raise MeetingLifecycleIntegrityError(
                    "meeting action has duplicate completion artifacts"
                )
            result[completion.action_id] = completion
        return result

    def _load_completion(
        self,
        artifact: WorkflowArtifact,
        decision: DurableMeetingDecision,
    ) -> MeetingActionCompletion:
        try:
            completion = MeetingActionCompletion.model_validate(artifact.payload)
        except ValueError as exc:
            raise MeetingLifecycleIntegrityError(
                "persisted meeting action completion is invalid"
            ) from exc
        actions = {item.action_id: item for item in decision.action_items}
        action = actions.get(completion.action_id)
        allowed_actors = (
            _ordered_unique(
                (action.owner_participant_id,),
                decision.governance.authorized_verifier_ids,
            )
            if action is not None
            else ()
        )
        authority = self._policy.authorize_meeting_actor(
            action=MeetingAuthorityAction.COMPLETE_ACTION,
            actor_id=completion.actor_id,
            authorized_actor_ids=allowed_actors,
        )
        expected_id = _stable_id(
            "meeting-action-completion",
            decision.case_id,
            completion.action_id,
        )
        if (
            artifact.kind != MEETING_ACTION_COMPLETION_ARTIFACT_KIND
            or artifact.artifact_id != completion.completion_id
            or completion.completion_id != expected_id
            or artifact.case_id != decision.case_id
            or completion.case_id != decision.case_id
            or completion.meeting_decision_id != decision.meeting_decision_id
            or action is None
            or not authority.allowed
            or completion.authority_rule_id != authority.rule_id
            or completion.completed_at < decision.created_at
            or artifact.source_ids != completion.source_ids
            or completion.source_ids
            != (
                decision.meeting_decision_id,
                completion.action_id,
                completion.source_id,
            )
        ):
            raise MeetingLifecycleIntegrityError(
                "persisted meeting action completion violates identity invariants"
            )
        return completion

    def _verification_request(
        self,
        decision: DurableMeetingDecision,
        *,
        requested_at,
        completion_ids: tuple[str, ...] = (),
    ) -> MeetingVerificationRequest:
        request_id = _stable_id("meeting-verification-request", decision.case_id)
        return MeetingVerificationRequest(
            request_id=request_id,
            case_id=decision.case_id,
            meeting_decision_id=decision.meeting_decision_id,
            requested_at=requested_at,
            due_at=requested_at + timedelta(hours=decision.governance.verification_timeout_hours),
            authorized_verifier_ids=decision.governance.authorized_verifier_ids,
            source_ids=_ordered_unique(
                (decision.meeting_decision_id,),
                completion_ids,
            ),
        )

    def _load_verification_request(
        self,
        decision: DurableMeetingDecision,
        *,
        required: bool,
    ) -> MeetingVerificationRequest | None:
        artifacts = self._store.artifacts_for(
            kind=MEETING_VERIFICATION_REQUEST_ARTIFACT_KIND,
            case_id=decision.case_id,
        )
        if not artifacts:
            if required:
                raise MeetingLifecycleNotReadyError("meeting outcome has no verification request")
            return None
        if len(artifacts) != 1:
            raise MeetingLifecycleIntegrityError(
                "meeting outcome has duplicate verification requests"
            )
        try:
            request = MeetingVerificationRequest.model_validate(artifacts[0].payload)
        except ValueError as exc:
            raise MeetingLifecycleIntegrityError(
                "persisted meeting verification request is invalid"
            ) from exc
        completions = self._completion_map(decision)
        if any(item.action_id not in completions for item in decision.action_items):
            raise MeetingLifecycleIntegrityError(
                "verification request exists before all meeting actions completed"
            )
        completion_ids = tuple(
            completions[item.action_id].completion_id for item in decision.action_items
        )
        expected_sources = _ordered_unique(
            (decision.meeting_decision_id,),
            completion_ids,
        )
        expected_id = _stable_id("meeting-verification-request", decision.case_id)
        expected_due = request.requested_at + timedelta(
            hours=decision.governance.verification_timeout_hours
        )
        if (
            artifacts[0].artifact_id != request.request_id
            or request.request_id != expected_id
            or artifacts[0].case_id != decision.case_id
            or artifacts[0].source_ids != request.source_ids
            or request.case_id != decision.case_id
            or request.meeting_decision_id != decision.meeting_decision_id
            or request.requested_at < decision.created_at
            or request.due_at != expected_due
            or request.authorized_verifier_ids != decision.governance.authorized_verifier_ids
            or request.source_ids != expected_sources
        ):
            raise MeetingLifecycleIntegrityError(
                "persisted meeting verification request violates source invariants"
            )
        return request

    def _verification_responses(
        self,
        request: MeetingVerificationRequest,
        decision: DurableMeetingDecision,
    ) -> tuple[MeetingVerificationResponse, ...]:
        responses = tuple(
            self._load_verification_response(artifact, request, decision)
            for artifact in self._store.artifacts_for(
                kind=MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
                case_id=decision.case_id,
            )
        )
        ordered = tuple(sorted(responses, key=lambda item: item.attempt))
        if [item.attempt for item in ordered] != list(range(1, len(ordered) + 1)):
            raise MeetingLifecycleIntegrityError(
                "meeting verification response attempts are not contiguous"
            )
        if any(
            current.responded_at < previous.responded_at
            for previous, current in zip(ordered, ordered[1:], strict=False)
        ):
            raise MeetingLifecycleIntegrityError(
                "meeting verification response chronology is inconsistent"
            )
        confirmed = [
            index
            for index, item in enumerate(ordered)
            if item.outcome is MeetingVerificationOutcome.CONFIRMED
        ]
        if len(confirmed) > 1 or (confirmed and confirmed[0] != len(ordered) - 1):
            raise MeetingLifecycleIntegrityError(
                "confirmed meeting verification must terminate response history"
            )
        return ordered

    def _load_verification_response(
        self,
        artifact: WorkflowArtifact,
        request: MeetingVerificationRequest,
        decision: DurableMeetingDecision,
    ) -> MeetingVerificationResponse:
        try:
            response = MeetingVerificationResponse.model_validate(artifact.payload)
        except ValueError as exc:
            raise MeetingLifecycleIntegrityError(
                "persisted meeting verification response is invalid"
            ) from exc
        authority = self._policy.authorize_meeting_actor(
            action=MeetingAuthorityAction.VERIFY_OUTCOME,
            actor_id=response.actor_id,
            authorized_actor_ids=request.authorized_verifier_ids,
        )
        expected_id = _verification_response_id(
            decision.case_id,
            response.attempt,
        )
        if (
            artifact.kind != MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND
            or artifact.artifact_id != response.response_id
            or response.response_id != expected_id
            or artifact.case_id != decision.case_id
            or artifact.created_at != response.responded_at
            or artifact.source_ids != response.source_ids
            or response.request_id != request.request_id
            or response.case_id != decision.case_id
            or not authority.allowed
            or response.authority_rule_id != authority.rule_id
            or response.responded_at < request.requested_at
            or response.source_ids
            != (request.request_id, decision.meeting_decision_id, response.source_id)
        ):
            raise MeetingLifecycleIntegrityError(
                "persisted meeting verification response violates identity invariants"
            )
        return response

    def _action_reminders(
        self,
        action_id: str,
        decision: DurableMeetingDecision,
    ) -> tuple[MeetingActionReminder, ...]:
        reminders = []
        for artifact in self._store.artifacts_for(
            kind=MEETING_ACTION_REMINDER_ARTIFACT_KIND,
            case_id=decision.case_id,
        ):
            try:
                reminder = MeetingActionReminder.model_validate(artifact.payload)
            except ValueError as exc:
                raise MeetingLifecycleIntegrityError(
                    "persisted meeting reminder is invalid"
                ) from exc
            if reminder.action_id != action_id:
                continue
            reminders.append(self._load_reminder(artifact, decision, None))
        reminders.sort(key=lambda item: item.attempt)
        if [item.attempt for item in reminders] != list(range(1, len(reminders) + 1)):
            raise MeetingLifecycleIntegrityError("meeting reminder attempts are not contiguous")
        return tuple(reminders)

    def _load_reminder(
        self,
        artifact: WorkflowArtifact,
        decision: DurableMeetingDecision,
        action: MeetingActionCandidate | None,
    ) -> MeetingActionReminder:
        try:
            reminder = MeetingActionReminder.model_validate(artifact.payload)
        except ValueError as exc:
            raise MeetingLifecycleIntegrityError("persisted meeting reminder is invalid") from exc
        actions = {item.action_id: item for item in decision.action_items}
        persisted_action = actions.get(reminder.action_id)
        expected_id = _stable_id(
            "meeting-action-reminder",
            reminder.action_id,
            str(reminder.attempt),
        )
        expected_next_due = reminder.created_at + timedelta(
            hours=decision.governance.reminder_interval_hours
        )
        if (
            artifact.kind != MEETING_ACTION_REMINDER_ARTIFACT_KIND
            or artifact.artifact_id != reminder.reminder_id
            or reminder.reminder_id != expected_id
            or artifact.case_id != decision.case_id
            or artifact.source_ids != reminder.source_ids
            or reminder.case_id != decision.case_id
            or reminder.meeting_decision_id != decision.meeting_decision_id
            or persisted_action is None
            or reminder.next_due_at != expected_next_due
            or reminder.source_ids != (decision.meeting_decision_id, reminder.action_id)
            or (action is not None and reminder.action_id != action.action_id)
        ):
            raise MeetingLifecycleIntegrityError(
                "persisted meeting reminder violates identity invariants"
            )
        return reminder

    def _effective_due(
        self,
        action: MeetingActionCandidate,
        decision: DurableMeetingDecision,
    ):
        reminders = self._action_reminders(action.action_id, decision)
        revisions = [
            a
            for a in self._store.artifacts_for(
                kind="meeting.assignment.v1", case_id=decision.case_id
            )
            if a.payload["action_id"] == action.action_id
        ]
        if revisions:
            revision = revisions[-1]
            due = parse_datetime(revision.payload["due_at"])
            later = [r for r in reminders if r.created_at >= revision.created_at]
            return max(due, later[-1].next_due_at) if later else due
        return reminders[-1].next_due_at if reminders else action.due_at

    def _action_escalation(
        self,
        action_id: str,
        decision: DurableMeetingDecision,
    ) -> MeetingEscalation | None:
        escalation_id = _stable_id("meeting-action-escalation", action_id)
        artifact = self._store.artifact(MEETING_ESCALATION_ARTIFACT_KIND, escalation_id)
        if artifact is None:
            return None
        if artifact.case_id != decision.case_id:
            raise MeetingLifecycleIntegrityError(
                "meeting action escalation references a different case"
            )
        return self._load_escalation(artifact, decision)

    def _trackable_actions(
        self,
        decision: DurableMeetingDecision,
        actions: tuple[MeetingActionCandidate, ...],
    ) -> tuple[MeetingActionCandidate, ...]:
        return tuple(
            item for item in actions if self._action_escalation(item.action_id, decision) is None
        )

    def _load_escalation(
        self,
        artifact: WorkflowArtifact,
        decision: DurableMeetingDecision,
    ) -> MeetingEscalation:
        try:
            escalation = MeetingEscalation.model_validate(artifact.payload)
        except ValueError as exc:
            raise MeetingLifecycleIntegrityError("persisted meeting escalation is invalid") from exc
        if escalation.action_id is not None:
            expected_id = _stable_id(
                "meeting-action-escalation",
                escalation.action_id,
            )
            expected_sources = (
                decision.meeting_decision_id,
                escalation.action_id,
            )
            known_target = any(
                item.action_id == escalation.action_id for item in decision.action_items
            )
        else:
            assert escalation.verification_request_id is not None
            expected_id = _stable_id(
                "meeting-verification-escalation",
                escalation.verification_request_id,
            )
            expected_sources = (
                decision.meeting_decision_id,
                escalation.verification_request_id,
            )
            request = self._load_verification_request(decision, required=True)
            known_target = (
                request is not None and request.request_id == escalation.verification_request_id
            )
        if (
            artifact.kind != MEETING_ESCALATION_ARTIFACT_KIND
            or artifact.artifact_id != escalation.escalation_id
            or escalation.escalation_id != expected_id
            or artifact.case_id != decision.case_id
            or artifact.source_ids != escalation.source_ids
            or escalation.source_ids != expected_sources
            or escalation.case_id != decision.case_id
            or escalation.meeting_decision_id != decision.meeting_decision_id
            or not known_target
        ):
            raise MeetingLifecycleIntegrityError(
                "persisted meeting escalation violates identity invariants"
            )
        return escalation

    def _validate_extraction(
        self,
        snapshot: MeetingMinutesSnapshot,
        extraction: MeetingMinutesExtraction,
    ) -> None:
        body = snapshot.body_text
        if extraction.summary_evidence_excerpt not in body:
            raise MeetingLifecycleIntegrityError(
                "meeting summary evidence is not verbatim minutes text"
            )
        statements = [item.statement for item in extraction.decisions]
        if len(statements) != len(set(statements)):
            raise MeetingLifecycleIntegrityError("meeting extraction contains duplicate decisions")
        for item in extraction.decisions:
            if item.evidence_excerpt not in body:
                raise MeetingLifecycleIntegrityError(
                    "meeting decision evidence is not verbatim minutes text"
                )
        descriptions = [item.description for item in extraction.action_items]
        if len(descriptions) != len(set(descriptions)):
            raise MeetingLifecycleIntegrityError("meeting extraction contains duplicate actions")
        allowed_owners = set(snapshot.governance.action_owner_ids)
        latest_due = snapshot.held_at + timedelta(days=366)
        for item in extraction.action_items:
            if item.evidence_excerpt not in body:
                raise MeetingLifecycleIntegrityError(
                    "meeting action evidence is not verbatim minutes text"
                )
            if item.owner_participant_id not in allowed_owners:
                raise MeetingLifecycleIntegrityError(
                    "meeting action owner was outside the offered participant set"
                )
            if item.due_at < snapshot.held_at or item.due_at > latest_due:
                raise MeetingLifecycleIntegrityError(
                    "meeting action due date is outside the accepted chronology"
                )

    def _reject_competing_singleton(
        self,
        *,
        kind: str,
        case_id: str,
        artifact_id: str,
    ) -> None:
        artifacts = self._store.artifacts_for(kind=kind, case_id=case_id)
        if any(item.artifact_id != artifact_id for item in artifacts):
            raise MeetingLifecycleConflictError(
                f"case already owns a different singleton artifact for {kind}"
            )

    def _require_case(self, case_id: str) -> Case:
        case = self._store.get_case(case_id)
        if case is None:
            raise MeetingLifecycleNotReadyError("unknown meeting lifecycle case")
        return case

    def _require_version(self, case_id: str) -> int:
        version = self._store.case_version(case_id)
        if version is None:
            raise MeetingLifecycleIntegrityError("meeting case lost its version")
        return version


def _verification_requested_event(
    request: MeetingVerificationRequest,
) -> TimelineEvent:
    return TimelineEvent(
        event_id=_stable_id("event-meeting-verification-request", request.request_id),
        case_id=request.case_id,
        at=request.requested_at,
        kind=EventKind.VERIFICATION_REQUESTED,
        actor=ActorType.SYSTEM,
        summary=(
            "Prepared internal meeting-outcome verification after all confirmed actions "
            "were complete; no message was sent."
        ),
        refs=list(request.source_ids),
        payload={
            "request_id": request.request_id,
            "due_at": request.due_at.isoformat(),
            "outbound_enabled": False,
        },
    )


def _artifact(
    *,
    kind: str,
    artifact_id: str,
    case_id: str,
    created_at,
    source_ids: tuple[str, ...],
    payload,
) -> WorkflowArtifact:
    return WorkflowArtifact(
        artifact_id=artifact_id,
        case_id=case_id,
        kind=kind,
        created_at=created_at,
        source_ids=source_ids,
        payload=payload.model_dump(mode="json"),
    )


def _verification_response_id(case_id: str, attempt: int) -> str:
    if attempt == 1:
        return _stable_id("meeting-verification-response", case_id)
    return _stable_id("meeting-verification-response", case_id, str(attempt))


def _stable_id(prefix: str, *parts: str) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ordered_unique(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))
