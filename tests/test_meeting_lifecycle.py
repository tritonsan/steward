from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Lock

import pytest

from steward.agents import (
    MeetingActionDraft,
    MeetingAgendaItem,
    MeetingAgendaItemKind,
    MeetingAgendaRecommendation,
    MeetingDecisionDraft,
    MeetingMinutesExtraction,
    ResolutionPlanRecommendation,
    TriageAction,
    TriageResult,
)
from steward.config import StewardSettings
from steward.domain.clock import FrozenClock
from steward.domain.enums import CaseStatus, Category, EventKind, ResolutionPath, Urgency
from steward.mail import RecordingMailTransport
from steward.meetings import (
    MEETING_ACTION_COMPLETION_ARTIFACT_KIND,
    MEETING_ACTION_REMINDER_ARTIFACT_KIND,
    MEETING_DECISION_ARTIFACT_KIND,
    MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
    MEETING_ESCALATION_ARTIFACT_KIND,
    MEETING_MINUTES_ARTIFACT_KIND,
    MEETING_VERIFICATION_REQUEST_ARTIFACT_KIND,
    MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
    MeetingActionCompletionDisposition,
    MeetingAvailabilityResponse,
    MeetingCandidateSlot,
    MeetingDecisionConfirmation,
    MeetingDecisionDisposition,
    MeetingDueDisposition,
    MeetingGovernancePolicy,
    MeetingInferenceInProgressError,
    MeetingLifecycleIntegrityError,
    MeetingLifecycleNotReadyError,
    MeetingLifecycleService,
    MeetingMinutesDisposition,
    MeetingPreparationService,
    MeetingSchedulingPolicy,
    MeetingVerificationDisposition,
    MeetingVerificationOutcome,
)
from steward.memory import CaseRecord, MemoryConflictError, build_scorecards
from steward.orchestration import ResolutionPlanningService
from steward.policy import PolicyEngine
from steward.runtime import build_runtime
from steward.seed import load_seed

UTC = timezone.utc
SECRET = "meeting-lifecycle-webhook-secret-2026"
CHAT_ID = "-1002481179934"
MINUTES = (
    "After discussion, the residents agreed to adopt a 24-hour visitor parking "
    "limit. Amelia T. will publish the written rule by 2026-09-20. Simon O. will "
    "number the visitor spaces by 2026-09-21."
)
DECISION_EVIDENCE = (
    "the residents agreed to adopt a 24-hour visitor parking limit"
)
ACTION_ONE_EVIDENCE = (
    "Amelia T. will publish the written rule by 2026-09-20"
)
ACTION_TWO_EVIDENCE = (
    "Simon O. will number the visitor spaces by 2026-09-21"
)


class _MeetingClassifier:
    def classify(self, *, message, assets, open_cases):
        del message, assets, open_cases
        return TriageResult(
            action=TriageAction.OPEN_CASE,
            category=Category.MEETING_ADMIN,
            urgency=Urgency.NORMAL,
            confidence=0.97,
            title="Visitor parking rule requires a resident decision",
            asset_id=None,
            rationale="The residents need one shared written parking rule.",
        )


class _ResolutionPlanner:
    def plan(self, *, context_id: str, context: dict):
        del context_id
        return ResolutionPlanRecommendation(
            path=ResolutionPath.MEETING_RESOLUTION,
            rationale="A resident meeting must choose the shared rule.",
            source_ids=(context["allowed_source_ids"][0],),
        )


class _AgendaPlanner:
    def prepare(self, *, brief_id: str, context: dict):
        del brief_id
        source_id = context["messages"][0]["message_id"]
        return MeetingAgendaRecommendation(
            summary="Residents need to decide one visitor parking rule.",
            summary_source_ids=(source_id,),
            agenda_items=(
                MeetingAgendaItem(
                    kind=MeetingAgendaItemKind.DECISION,
                    title="Choose the visitor parking rule",
                    detail="Agree the duration and publication owner.",
                    source_ids=(source_id,),
                ),
            ),
        )


class _MinutesExtractor:
    def __init__(self, *, action_count: int = 1) -> None:
        self.action_count = action_count
        self.calls: list[dict] = []

    def extract(self, *, snapshot_id: str, context: dict):
        del snapshot_id
        self.calls.append(context)
        actions = (
            MeetingActionDraft(
                description="Publish the written visitor parking rule.",
                owner_participant_id="member-amelia",
                due_at=datetime(2026, 9, 20, 18, tzinfo=UTC),
                evidence_excerpt=ACTION_ONE_EVIDENCE,
            ),
            MeetingActionDraft(
                description="Number the visitor parking spaces.",
                owner_participant_id="member-simon",
                due_at=datetime(2026, 9, 21, 18, tzinfo=UTC),
                evidence_excerpt=ACTION_TWO_EVIDENCE,
            ),
        )[: self.action_count]
        return MeetingMinutesExtraction(
            summary="Residents adopted a written visitor parking rule.",
            summary_evidence_excerpt=DECISION_EVIDENCE,
            decisions=(
                MeetingDecisionDraft(
                    statement="Adopt a 24-hour visitor parking limit.",
                    evidence_excerpt=DECISION_EVIDENCE,
                ),
            ),
            action_items=actions,
        )


class _FailOnceMinutesExtractor(_MinutesExtractor):
    def extract(self, *, snapshot_id: str, context: dict):
        if not self.calls:
            self.calls.append(context)
            raise RuntimeError("temporary minutes model failure")
        return super().extract(snapshot_id=snapshot_id, context=context)


class _BlockingMinutesExtractor:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = Event()
        self.release = Event()
        self._delegate = _MinutesExtractor()

    def extract(self, *, snapshot_id: str, context: dict):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise RuntimeError("blocking minutes extractor timed out")
        return self._delegate.extract(snapshot_id=snapshot_id, context=context)


class _SimulatedWorkerCrash(BaseException):
    pass


class _CrashingMinutesExtractor:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, *, snapshot_id: str, context: dict):
        del snapshot_id, context
        self.calls += 1
        raise _SimulatedWorkerCrash("worker stopped before releasing its lease")


class _InvalidMinutesExtractor(_MinutesExtractor):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def extract(self, *, snapshot_id: str, context: dict):
        result = super().extract(snapshot_id=snapshot_id, context=context)
        if self.mode == "evidence":
            return result.model_copy(
                update={"summary_evidence_excerpt": "not present in minutes"}
            )
        invalid_action = result.action_items[0].model_copy(
            update={"owner_participant_id": "resident-from-untrusted-prose"}
        )
        return result.model_copy(update={"action_items": (invalid_action,)})


class _FailIfCalled:
    def extract(self, **kwargs):  # pragma: no cover - failure explains itself
        raise AssertionError(f"minutes model called after restart: {kwargs}")



class _BarrierArtifactStore:
    """Synchronize the first two reads for one artifact kind."""

    def __init__(self, inner, *, kind: str) -> None:
        self._inner = inner
        self._kind = kind
        self._barrier = Barrier(2)
        self._lock = Lock()
        self._wait_count = 0

    def artifacts_for(self, *, kind: str, case_id: str | None = None):
        result = self._inner.artifacts_for(kind=kind, case_id=case_id)
        should_wait = False
        if kind == self._kind and case_id is not None:
            with self._lock:
                if self._wait_count < 2:
                    self._wait_count += 1
                    should_wait = True
        if should_wait:
            self._barrier.wait(timeout=5)
        return result

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _NoActionMinutesExtractor(_MinutesExtractor):
    def __init__(self) -> None:
        super().__init__(action_count=0)



class _FailCandidatePersistenceStore:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.failed = False

    def save_transition(self, **kwargs):
        artifacts = kwargs.get("artifacts", ())
        if not self.failed and any(
            artifact.kind == MEETING_DECISION_CANDIDATES_ARTIFACT_KIND
            for artifact in artifacts
        ):
            self.failed = True
            raise RuntimeError("simulated crash after durable inference completion")
        return self._inner.save_transition(**kwargs)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _telegram_update(at: datetime):
    return {
        "update_id": 88201,
        "message": {
            "message_id": 66201,
            "date": int(at.timestamp()),
            "chat": {"id": int(CHAT_ID), "type": "supergroup"},
            "from": {"id": 42001, "is_bot": False, "first_name": "James"},
            "text": (
                "Visitor parking keeps causing arguments. We need one written rule "
                "that everyone can follow."
            ),
        },
    }


def _meeting_inputs(now: datetime):
    policy = MeetingSchedulingPolicy(
        timezone="UTC",
        minimum_notice_hours=24,
        quorum=2,
        eligible_participant_ids=("member-simon", "member-james", "member-amelia"),
        required_participant_ids=("member-simon",),
    )
    slots = (
        MeetingCandidateSlot(
            slot_id="slot-tuesday",
            starts_at=now + timedelta(days=2),
            duration_minutes=60,
        ),
        MeetingCandidateSlot(
            slot_id="slot-wednesday",
            starts_at=now + timedelta(days=3),
            duration_minutes=60,
        ),
    )
    availability = (
        MeetingAvailabilityResponse(
            participant_id="member-simon",
            source_id="availability:simon:1",
            available_slot_ids=("slot-tuesday", "slot-wednesday"),
        ),
        MeetingAvailabilityResponse(
            participant_id="member-james",
            source_id="availability:james:1",
            available_slot_ids=("slot-wednesday",),
        ),
        MeetingAvailabilityResponse(
            participant_id="member-amelia",
            source_id="availability:amelia:1",
            available_slot_ids=("slot-wednesday",),
        ),
    )
    return policy, slots, availability


def _governance(*, max_reminders: int = 2) -> MeetingGovernancePolicy:
    return MeetingGovernancePolicy(
        action_owner_ids=("member-simon", "member-james", "member-amelia"),
        authorized_decider_ids=("member-simon",),
        authorized_verifier_ids=("member-james",),
        reminder_interval_hours=24,
        max_reminders=max_reminders,
        verification_timeout_hours=48,
    )


def _ready_runtime(tmp_path, extractor, *, db_name="meeting-lifecycle.db"):
    opened_at = datetime(2026, 9, 7, 10, tzinfo=UTC)
    clock = FrozenClock(opened_at)
    settings = StewardSettings(
        database_path=tmp_path / db_name,
        telegram_enabled=True,
        telegram_webhook_secret=SECRET,
        telegram_allowed_chat_ids=frozenset({CHAT_ID}),
    )
    runtime = build_runtime(
        settings,
        classifier=_MeetingClassifier(),
        resolution_planner=_ResolutionPlanner(),
        meeting_agenda_planner=_AgendaPlanner(),
        meeting_minutes_extractor=extractor,
        clock=clock,
    )
    runtime.handle_telegram_update(_telegram_update(opened_at), secret_header=SECRET)
    report = runtime.tick()
    case = report.inbound.intake_outcomes[0].case
    assert case is not None
    clock.advance(timedelta(hours=1))
    runtime.plan_resolution(case.case_id)
    policy, slots, availability = _meeting_inputs(clock.now())
    prepared = runtime.prepare_meeting(
        case.case_id,
        policy=policy,
        candidate_slots=slots,
        availability=availability,
    )
    clock.set(prepared.packet.schedule.selected_slot.starts_at + timedelta(hours=1))
    return runtime, clock, settings, case, prepared


def _extract(runtime, clock, case_id, *, governance=None):
    return runtime.extract_meeting_minutes(
        case_id,
        held_at=clock.now() - timedelta(minutes=30),
        body_text=MINUTES,
        source_id="minutes:visitor-parking:2026-09-10",
        participant_count=3,
        governance=governance or _governance(),
        simulated=True,
    )


def _confirmation(extracted, at, *, decisions=None, actions=None, actor_id="member-simon"):
    return MeetingDecisionConfirmation(
        actor_id=actor_id,
        actor_label="Simon O.",
        source_id="confirmation:visitor-parking:1",
        confirmed_at=at,
        confirmed_decision_ids=(
            decisions
            if decisions is not None
            else tuple(item.decision_id for item in extracted.candidates.decisions)
        ),
        confirmed_action_ids=(
            actions
            if actions is not None
            else tuple(item.action_id for item in extracted.candidates.action_items)
        ),
    )


def test_packet_to_verified_closed_memory_is_atomic_and_outbound_free(tmp_path):
    extractor = _MinutesExtractor()
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        extractor,
    )

    extracted = _extract(runtime, clock, case.case_id)
    decided = runtime.confirm_meeting_decisions(
        case.case_id,
        confirmation=_confirmation(extracted, clock.now()),
    )
    action = decided.decision.action_items[0]
    completed = runtime.record_meeting_action_completion(
        case.case_id,
        action_id=action.action_id,
        actor_id="member-amelia",
        actor_label="Amelia T.",
        source_id="completion:parking-rule:1",
        notes="The written rule was posted in the lobby and resident portal.",
        completed_at=clock.now(),
    )
    responded_at = clock.now()
    clock.advance(timedelta(hours=6))
    verified = runtime.verify_meeting_outcome(
        case.case_id,
        outcome=MeetingVerificationOutcome.CONFIRMED,
        actor_id="member-james",
        actor_label="James D.",
        source_id="verification:parking-rule:1",
        notes="The rule is posted and the agreed 24-hour limit is in effect.",
        responded_at=responded_at,
    )

    assert extracted.disposition is MeetingMinutesDisposition.CANDIDATES_RECORDED
    assert len(extractor.calls) == 1
    assert "authorized_decider_ids" not in extractor.calls[0]
    assert "authorized_verifier_ids" not in extractor.calls[0]
    assert decided.disposition is MeetingDecisionDisposition.DECISION_CONFIRMED
    assert decided.case.status is CaseStatus.ACTIONS_TRACKING
    assert completed.disposition is MeetingActionCompletionDisposition.COMPLETED
    assert completed.case.status is CaseStatus.AWAITING_VERIFICATION
    assert completed.verification_request is not None
    assert verified.disposition is MeetingVerificationDisposition.CLOSED
    assert verified.case.status is CaseStatus.CLOSED
    assert verified.case.total_cost == 0
    assert verified.case.resolved_at == responded_at
    assert verified.case.closed_at == clock.now()
    assert verified.case.closed_at > verified.case.resolved_at
    assert verified.transition is not None
    assert verified.transition.applied is True
    memory = runtime.store.get(case.case_id)
    assert memory == verified.memory_record
    assert memory is not None
    assert memory.selected_vendor_id is None
    assert memory.counts_toward_vendor_record is False
    assert memory.meeting_decisions == ["Adopt a 24-hour visitor parking limit."]
    assert memory.meeting_participant_count == 3
    assert memory.meeting_action_items == [
        "Publish the written visitor parking rule. — The written rule was posted "
        "in the lobby and resident portal."
    ]
    assert memory.resolved_at == responded_at
    assert memory.closed_at == clock.now()
    assert memory.selection_source_ids == list(decided.decision.source_ids)
    assert "minutes:visitor-parking:2026-09-10" in memory.resolution_source_ids
    assert decided.decision.meeting_decision_id in memory.resolution_source_ids
    assert completed.completion.completion_id in memory.resolution_source_ids
    assert memory.verification_source_ids == ["verification:parking-rule:1"]
    assert build_scorecards((memory,), computed_at=clock.now()) == {}
    assert runtime.store.outbox_for_case(case.case_id) == ()
    lifecycle_kinds = (
        MEETING_MINUTES_ARTIFACT_KIND,
        MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
        MEETING_DECISION_ARTIFACT_KIND,
        MEETING_ACTION_COMPLETION_ARTIFACT_KIND,
        MEETING_VERIFICATION_REQUEST_ARTIFACT_KIND,
        MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
    )
    assert all(
        artifact.payload["outbound_enabled"] is False
        for kind in lifecycle_kinds
        for artifact in runtime.store.artifacts_for(kind=kind, case_id=case.case_id)
    )
    assert isinstance(runtime.transport, RecordingMailTransport)
    assert runtime.transport.sent == []
    kinds = [event.kind for event in runtime.store.timeline_for(case.case_id)]
    assert EventKind.MEETING_MINUTES_RECORDED in kinds
    assert EventKind.MEETING_DECISION_CONFIRMED in kinds
    assert EventKind.ACTION_ITEM_OPENED in kinds
    assert EventKind.ACTION_ITEM_COMPLETED in kinds
    assert EventKind.VERIFICATION_REQUESTED in kinds
    assert kinds[-4:] == [
        EventKind.VERIFICATION_CONFIRMED,
        EventKind.RESOLVED,
        EventKind.MEMORY_WRITTEN,
        EventKind.CLOSED,
    ]
    runtime.close()


def test_minutes_snapshot_survives_model_failure_and_retry(tmp_path):
    extractor = _FailOnceMinutesExtractor()
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        extractor,
        db_name="minutes-retry.db",
    )

    with pytest.raises(RuntimeError, match="temporary minutes model failure"):
        _extract(runtime, clock, case.case_id)

    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_MINUTES_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    assert runtime.store.artifacts_for(
        kind=MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()

    recovered = _extract(runtime, clock, case.case_id)
    assert recovered.disposition is MeetingMinutesDisposition.CANDIDATES_RECORDED
    assert len(extractor.calls) == 2
    runtime.close()


@pytest.mark.parametrize("mode", ["evidence", "owner"])
def test_minutes_model_cannot_escape_verbatim_evidence_or_owner_set(tmp_path, mode):
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        _InvalidMinutesExtractor(mode),
        db_name=f"invalid-minutes-{mode}.db",
    )

    with pytest.raises(MeetingLifecycleIntegrityError):
        _extract(runtime, clock, case.case_id)

    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_MINUTES_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    assert runtime.store.artifacts_for(
        kind=MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()
    assert runtime.store.outbox_for_case(case.case_id) == ()
    runtime.close()


def test_confirmation_is_policy_authorized_and_closed_set_only(tmp_path):
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        _MinutesExtractor(),
        db_name="confirmation-boundary.db",
    )
    extracted = _extract(runtime, clock, case.case_id)

    with pytest.raises(MeetingLifecycleNotReadyError, match="not configured"):
        runtime.confirm_meeting_decisions(
            case.case_id,
            confirmation=_confirmation(
                extracted,
                clock.now(),
                actor_id="member-from-minutes-prose",
            ),
        )
    with pytest.raises(MeetingLifecycleIntegrityError, match="outside"):
        runtime.confirm_meeting_decisions(
            case.case_id,
            confirmation=_confirmation(
                extracted,
                clock.now(),
                decisions=("decision-not-offered",),
            ),
        )

    assert runtime.store.artifacts_for(
        kind=MEETING_DECISION_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()
    assert runtime.store.outbox_for_case(case.case_id) == ()
    runtime.close()


def test_action_completion_requires_owner_or_configured_verifier(tmp_path):
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        _MinutesExtractor(),
        db_name="completion-authority.db",
    )
    extracted = _extract(runtime, clock, case.case_id)
    decided = runtime.confirm_meeting_decisions(
        case.case_id,
        confirmation=_confirmation(extracted, clock.now()),
    )
    action = decided.decision.action_items[0]

    with pytest.raises(MeetingLifecycleNotReadyError, match="not configured"):
        runtime.record_meeting_action_completion(
            case.case_id,
            action_id=action.action_id,
            actor_id="member-outsider",
            actor_label="Daniel K.",
            source_id="completion:unauthorized",
            notes="An unauthorized completion claim.",
            completed_at=clock.now(),
        )
    with pytest.raises(MeetingLifecycleNotReadyError, match="unconfirmed"):
        runtime.record_meeting_action_completion(
            case.case_id,
            action_id="action-not-confirmed",
            actor_id="member-james",
            actor_label="James D.",
            source_id="completion:unknown",
            notes="A completion claim for an unknown action.",
            completed_at=clock.now(),
        )

    assert runtime.store.artifacts_for(
        kind=MEETING_ACTION_COMPLETION_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()
    runtime.close()


def test_due_tick_records_internal_reminders_then_escalates_without_send(tmp_path):
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        _MinutesExtractor(),
        db_name="action-due.db",
    )
    extracted = _extract(runtime, clock, case.case_id, governance=_governance())
    decided = runtime.confirm_meeting_decisions(
        case.case_id,
        confirmation=_confirmation(extracted, clock.now()),
    )
    action = decided.decision.action_items[0]
    clock.set(action.due_at)

    first = runtime.tick()
    assert first.meeting_due_results[0].disposition is MeetingDueDisposition.REMINDER_RECORDED
    assert first.outbound.deliveries == ()
    assert runtime.store.outbox_for_case(case.case_id) == ()

    clock.advance(timedelta(hours=24))
    second = runtime.tick()
    assert second.meeting_due_results[0].disposition is MeetingDueDisposition.REMINDER_RECORDED

    clock.advance(timedelta(hours=24))
    third = runtime.tick()
    assert third.meeting_due_results[0].disposition is MeetingDueDisposition.ESCALATED
    assert third.meeting_due_results[0].case.status is CaseStatus.ATTENTION_REQUIRED
    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_ACTION_REMINDER_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 2
    escalation = runtime.store.artifacts_for(
        kind=MEETING_ESCALATION_ARTIFACT_KIND,
        case_id=case.case_id,
    )
    assert len(escalation) == 1
    assert escalation[0].payload["outbound_enabled"] is False
    assert runtime.transport.sent == []
    runtime.close()


def test_no_action_decision_requests_verification_and_timeout_escalates(tmp_path):
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        _NoActionMinutesExtractor(),
        db_name="verification-timeout.db",
    )
    extracted = _extract(runtime, clock, case.case_id)
    decided = runtime.confirm_meeting_decisions(
        case.case_id,
        confirmation=_confirmation(extracted, clock.now()),
    )

    assert decided.case.status is CaseStatus.AWAITING_VERIFICATION
    assert decided.verification_request is not None
    clock.set(decided.verification_request.due_at)
    report = runtime.tick()
    assert report.meeting_due_results[0].disposition is MeetingDueDisposition.ESCALATED
    assert report.meeting_due_results[0].case.status is CaseStatus.ATTENTION_REQUIRED
    assert runtime.store.artifacts_for(
        kind=MEETING_ACTION_COMPLETION_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()
    assert runtime.store.outbox_for_case(case.case_id) == ()
    assert runtime.transport.sent == []
    runtime.close()


def test_rejected_verification_can_be_corrected_and_then_closed(tmp_path):
    runtime, clock, settings, case, _prepared = _ready_runtime(
        tmp_path,
        _NoActionMinutesExtractor(),
        db_name="verification-rejected.db",
    )
    extracted = _extract(runtime, clock, case.case_id)
    runtime.confirm_meeting_decisions(
        case.case_id,
        confirmation=_confirmation(extracted, clock.now()),
    )

    with pytest.raises(MeetingLifecycleNotReadyError, match="not configured"):
        runtime.verify_meeting_outcome(
            case.case_id,
            outcome=MeetingVerificationOutcome.REJECTED,
            actor_id="member-amelia",
            actor_label="Amelia T.",
            source_id="verification:unauthorized:1",
            notes="An unauthorized verification claim.",
            responded_at=clock.now(),
        )
    assert runtime.store.artifacts_for(
        kind=MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()

    rejected = runtime.verify_meeting_outcome(
        case.case_id,
        outcome=MeetingVerificationOutcome.REJECTED,
        actor_id="member-james",
        actor_label="James D.",
        source_id="verification:rejected:1",
        notes="The written rule has not been published yet.",
        responded_at=clock.now(),
    )

    assert rejected.disposition is MeetingVerificationDisposition.REJECTED
    assert rejected.case.status is CaseStatus.ATTENTION_REQUIRED
    assert runtime.store.get(case.case_id) is None
    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    assert EventKind.CLOSED not in {
        event.kind for event in runtime.store.timeline_for(case.case_id)
    }
    runtime.close()

    runtime = build_runtime(
        settings,
        classifier=_MeetingClassifier(),
        resolution_planner=_ResolutionPlanner(),
        meeting_agenda_planner=_AgendaPlanner(),
        meeting_minutes_extractor=_FailIfCalled(),
        clock=clock,
    )
    clock.advance(timedelta(hours=2))
    corrected = runtime.verify_meeting_outcome(
        case.case_id,
        outcome=MeetingVerificationOutcome.CONFIRMED,
        actor_id="member-james",
        actor_label="James D.",
        source_id="verification:corrected:2",
        notes="The written rule is now published and the decision is in effect.",
        responded_at=clock.now(),
    )

    assert rejected.response.attempt == 1
    assert corrected.response.attempt == 2
    assert corrected.disposition is MeetingVerificationDisposition.CLOSED
    assert corrected.case.status is CaseStatus.CLOSED
    responses = runtime.store.artifacts_for(
        kind=MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
        case_id=case.case_id,
    )
    assert [artifact.payload["attempt"] for artifact in responses] == [1, 2]
    memory = runtime.store.get(case.case_id)
    assert memory is not None
    assert memory.verification_source_ids == [
        "verification:rejected:1",
        "verification:corrected:2",
    ]
    assert runtime.store.outbox_for_case(case.case_id) == ()
    runtime.close()


def test_restart_loads_candidates_without_model_invocation(tmp_path):
    extractor = _MinutesExtractor()
    runtime, clock, settings, case, _prepared = _ready_runtime(
        tmp_path,
        extractor,
        db_name="minutes-restart.db",
    )
    first = _extract(runtime, clock, case.case_id)
    runtime.close()

    restarted = build_runtime(
        settings,
        classifier=_MeetingClassifier(),
        resolution_planner=_ResolutionPlanner(),
        meeting_agenda_planner=_AgendaPlanner(),
        meeting_minutes_extractor=_FailIfCalled(),
        clock=clock,
    )
    duplicate = _extract(restarted, clock, case.case_id)

    assert duplicate.disposition is MeetingMinutesDisposition.DUPLICATE
    assert duplicate.snapshot == first.snapshot
    assert duplicate.candidates == first.candidates
    assert restarted.store.outbox_for_case(case.case_id) == ()
    restarted.close()


def test_concurrent_distinct_action_completions_merge_and_request_verification(tmp_path):
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        _MinutesExtractor(action_count=2),
        db_name="completion-concurrency.db",
    )
    extracted = _extract(runtime, clock, case.case_id)
    decided = runtime.confirm_meeting_decisions(
        case.case_id,
        confirmation=_confirmation(extracted, clock.now()),
    )
    action_one, action_two = decided.decision.action_items

    def complete(action, actor_id, actor_label, suffix):
        return runtime.record_meeting_action_completion(
            case.case_id,
            action_id=action.action_id,
            actor_id=actor_id,
            actor_label=actor_label,
            source_id=f"completion:concurrent:{suffix}",
            notes=f"Completed action {suffix} with source-backed evidence.",
            completed_at=clock.now(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(
                complete,
                action_one,
                "member-amelia",
                "Amelia T.",
                "one",
            ),
            executor.submit(
                complete,
                action_two,
                "member-simon",
                "Simon O.",
                "two",
            ),
        )
        results = tuple(future.result(timeout=10) for future in futures)

    assert all(
        result.disposition is MeetingActionCompletionDisposition.COMPLETED
        for result in results
    )
    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_ACTION_COMPLETION_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 2
    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_VERIFICATION_REQUEST_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    assert runtime.store.get_case(case.case_id).status is CaseStatus.AWAITING_VERIFICATION
    assert runtime.store.outbox_for_case(case.case_id) == ()
    runtime.close()



def test_completing_one_of_two_actions_recomputes_next_due(tmp_path):
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        _MinutesExtractor(action_count=2),
        db_name="completion-next-due.db",
    )
    extracted = _extract(runtime, clock, case.case_id)
    decided = runtime.confirm_meeting_decisions(
        case.case_id,
        confirmation=_confirmation(extracted, clock.now()),
    )
    action_one, action_two = decided.decision.action_items

    completed = runtime.record_meeting_action_completion(
        case.case_id,
        action_id=action_one.action_id,
        actor_id="member-amelia",
        actor_label="Amelia T.",
        source_id="completion:recompute:one",
        notes="The written rule was published.",
        completed_at=clock.now(),
    )

    assert completed.case.status is CaseStatus.ACTIONS_TRACKING
    assert completed.case.next_action_due_at == action_two.due_at
    assert completed.verification_request is None
    assert runtime.store.artifacts_for(
        kind=MEETING_VERIFICATION_REQUEST_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()
    runtime.close()


def test_two_sqlite_connections_converge_on_confirmation_and_verification(tmp_path):
    runtime, clock, settings, case, _prepared = _ready_runtime(
        tmp_path,
        _NoActionMinutesExtractor(),
        db_name="decision-verification-concurrency.db",
    )
    extracted = _extract(runtime, clock, case.case_id)
    peer = build_runtime(
        settings,
        classifier=_MeetingClassifier(),
        resolution_planner=_ResolutionPlanner(),
        meeting_agenda_planner=_AgendaPlanner(),
        meeting_minutes_extractor=_FailIfCalled(),
        clock=clock,
    )
    confirmation = _confirmation(extracted, clock.now())
    runtimes = (runtime, peer)

    with ThreadPoolExecutor(max_workers=2) as executor:
        confirmation_results = tuple(
            executor.map(
                lambda item: item.confirm_meeting_decisions(
                    case.case_id,
                    confirmation=confirmation,
                ),
                runtimes,
            )
        )

    assert {item.disposition for item in confirmation_results} == {
        MeetingDecisionDisposition.DECISION_CONFIRMED,
        MeetingDecisionDisposition.DUPLICATE,
    }
    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_DECISION_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1

    def verify(item):
        return item.verify_meeting_outcome(
            case.case_id,
            outcome=MeetingVerificationOutcome.CONFIRMED,
            actor_id="member-james",
            actor_label="James D.",
            source_id="verification:concurrent:1",
            notes="The meeting decision is in effect.",
            responded_at=clock.now(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        verification_results = tuple(executor.map(verify, runtimes))

    assert {item.disposition for item in verification_results} == {
        MeetingVerificationDisposition.CLOSED,
        MeetingVerificationDisposition.DUPLICATE,
    }
    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    assert runtime.store.get_case(case.case_id).status is CaseStatus.CLOSED
    assert peer.store.get_case(case.case_id).status is CaseStatus.CLOSED
    assert runtime.store.get(case.case_id) is not None
    assert runtime.store.outbox_for_case(case.case_id) == ()
    peer.close()
    runtime.close()


def test_restart_resumes_partial_actions_then_verification_without_model(tmp_path):
    runtime, clock, settings, case, _prepared = _ready_runtime(
        tmp_path,
        _MinutesExtractor(action_count=2),
        db_name="partial-action-restart.db",
    )
    extracted = _extract(runtime, clock, case.case_id)
    decided = runtime.confirm_meeting_decisions(
        case.case_id,
        confirmation=_confirmation(extracted, clock.now()),
    )
    action_one, action_two = decided.decision.action_items
    runtime.record_meeting_action_completion(
        case.case_id,
        action_id=action_one.action_id,
        actor_id="member-amelia",
        actor_label="Amelia T.",
        source_id="completion:restart:one",
        notes="The written rule was published.",
        completed_at=clock.now(),
    )
    runtime.close()

    resumed = build_runtime(
        settings,
        classifier=_MeetingClassifier(),
        resolution_planner=_ResolutionPlanner(),
        meeting_agenda_planner=_AgendaPlanner(),
        meeting_minutes_extractor=_FailIfCalled(),
        clock=clock,
    )
    completed = resumed.record_meeting_action_completion(
        case.case_id,
        action_id=action_two.action_id,
        actor_id="member-simon",
        actor_label="Simon O.",
        source_id="completion:restart:two",
        notes="The visitor spaces were numbered.",
        completed_at=clock.now(),
    )
    assert completed.case.status is CaseStatus.AWAITING_VERIFICATION
    assert completed.verification_request is not None
    resumed.close()

    verifier = build_runtime(
        settings,
        classifier=_MeetingClassifier(),
        resolution_planner=_ResolutionPlanner(),
        meeting_agenda_planner=_AgendaPlanner(),
        meeting_minutes_extractor=_FailIfCalled(),
        clock=clock,
    )
    verified = verifier.verify_meeting_outcome(
        case.case_id,
        outcome=MeetingVerificationOutcome.CONFIRMED,
        actor_id="member-james",
        actor_label="James D.",
        source_id="verification:restart:1",
        notes="Both meeting actions are complete and the rule is in effect.",
        responded_at=clock.now(),
    )
    assert verified.disposition is MeetingVerificationDisposition.CLOSED
    assert verifier.store.get_case(case.case_id).status is CaseStatus.CLOSED
    assert verifier.store.get(case.case_id) is not None
    assert verifier.store.outbox_for_case(case.case_id) == ()
    verifier.close()


def test_concurrent_same_case_sweeps_create_one_internal_reminder(tmp_path):
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        _MinutesExtractor(),
        db_name="reminder-concurrency.db",
    )
    extracted = _extract(runtime, clock, case.case_id)
    decided = runtime.confirm_meeting_decisions(
        case.case_id,
        confirmation=_confirmation(extracted, clock.now()),
    )
    clock.set(decided.decision.action_items[0].due_at)

    bundle = load_seed()
    barrier_store = _BarrierArtifactStore(
        runtime.store,
        kind=MEETING_ACTION_REMINDER_ARTIFACT_KIND,
    )
    planning = ResolutionPlanningService(
        store=barrier_store,
        planner=_ResolutionPlanner(),
        vendors=tuple(bundle.vendors),
        clock=clock,
    )
    meeting = MeetingPreparationService(
        store=barrier_store,
        planning=planning,
        agenda_planner=_AgendaPlanner(),
        vendors=tuple(bundle.vendors),
        clock=clock,
    )
    lifecycle = MeetingLifecycleService(
        store=barrier_store,
        meeting=meeting,
        policy=PolicyEngine(bundle.policies, bundle.settings),
        extractor=_FailIfCalled(),
        clock=clock,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: lifecycle.sweep_case(case.case_id), range(2)))

    assert MeetingDueDisposition.REMINDER_RECORDED in {
        item.disposition for item in results
    }
    assert all(
        item.disposition
        in {
            MeetingDueDisposition.REMINDER_RECORDED,
            MeetingDueDisposition.DUPLICATE,
            MeetingDueDisposition.NOT_DUE,
        }
        for item in results
    )
    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_ACTION_REMINDER_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    reminder_events = [
        event
        for event in runtime.store.timeline_for(case.case_id)
        if event.kind is EventKind.ACTION_ITEM_REMINDER
    ]
    assert len(reminder_events) == 1
    assert runtime.store.outbox_for_case(case.case_id) == ()
    assert runtime.transport.sent == []
    runtime.close()


def test_confirmed_closure_rolls_back_when_memory_conflicts(tmp_path):
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        _NoActionMinutesExtractor(),
        db_name="meeting-memory-rollback.db",
    )
    extracted = _extract(runtime, clock, case.case_id)
    runtime.confirm_meeting_decisions(
        case.case_id,
        confirmation=_confirmation(extracted, clock.now()),
    )
    conflicting = CaseRecord(
        case_id=case.case_id,
        title="Conflicting pre-existing institutional memory",
        category=case.category,
        opened_at=case.opened_at,
        outcome_verified=False,
        closed_at=clock.now(),
    )
    assert runtime.store.write(conflicting) is True

    with pytest.raises(MemoryConflictError):
        runtime.verify_meeting_outcome(
            case.case_id,
            outcome=MeetingVerificationOutcome.CONFIRMED,
            actor_id="member-james",
            actor_label="James D.",
            source_id="verification:memory-conflict:1",
            notes="This transition must roll back because memory conflicts.",
            responded_at=clock.now(),
        )

    persisted_case = runtime.store.get_case(case.case_id)
    assert persisted_case is not None
    assert persisted_case.status is CaseStatus.AWAITING_VERIFICATION
    assert runtime.store.get(case.case_id) == conflicting
    assert runtime.store.artifacts_for(
        kind=MEETING_VERIFICATION_RESPONSE_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()
    event_kinds = {
        event.kind for event in runtime.store.timeline_for(case.case_id)
    }
    assert EventKind.VERIFICATION_CONFIRMED not in event_kinds
    assert EventKind.MEMORY_WRITTEN not in event_kinds
    assert EventKind.CLOSED not in event_kinds
    assert runtime.store.outbox_for_case(case.case_id) == ()
    runtime.close()


def test_zero_reminder_escalation_preserves_and_recovers_sibling_due(tmp_path):
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        _MinutesExtractor(action_count=2),
        db_name="sibling-action-escalation.db",
    )
    extracted = _extract(
        runtime,
        clock,
        case.case_id,
        governance=_governance(max_reminders=0),
    )
    decided = runtime.confirm_meeting_decisions(
        case.case_id,
        confirmation=_confirmation(extracted, clock.now()),
    )
    action_one, action_two = decided.decision.action_items
    clock.set(action_one.due_at)

    escalated = runtime.tick().meeting_due_results[0]
    assert escalated.disposition is MeetingDueDisposition.ESCALATED
    assert escalated.case.status is CaseStatus.ATTENTION_REQUIRED
    assert escalated.case.next_action_due_at == action_two.due_at
    assert runtime.store.artifacts_for(
        kind=MEETING_ACTION_REMINDER_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()

    recovered = runtime.record_meeting_action_completion(
        case.case_id,
        action_id=action_one.action_id,
        actor_id="member-amelia",
        actor_label="Amelia T.",
        source_id="completion:escalated-action:1",
        notes="The first escalated action was completed after human attention.",
        completed_at=clock.now(),
    )
    assert recovered.case.status is CaseStatus.ACTIONS_TRACKING
    assert recovered.case.next_action_due_at == action_two.due_at

    clock.set(action_two.due_at)
    sibling = runtime.tick().meeting_due_results[0]
    assert sibling.disposition is MeetingDueDisposition.ESCALATED
    assert sibling.escalation is not None
    assert sibling.escalation.action_id == action_two.action_id
    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_ESCALATION_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 2
    assert runtime.store.outbox_for_case(case.case_id) == ()
    assert runtime.transport.sent == []
    runtime.close()


def test_two_runtime_minutes_calls_invoke_model_once_and_reuse_result(tmp_path):
    blocking = _BlockingMinutesExtractor()
    runtime, clock, settings, case, _prepared = _ready_runtime(
        tmp_path,
        blocking,
        db_name="minutes-inference-concurrency.db",
    )
    peer = build_runtime(
        settings,
        classifier=_MeetingClassifier(),
        resolution_planner=_ResolutionPlanner(),
        meeting_agenda_planner=_AgendaPlanner(),
        meeting_minutes_extractor=_FailIfCalled(),
        clock=clock,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(_extract, runtime, clock, case.case_id)
        assert blocking.entered.wait(timeout=5)
        try:
            with pytest.raises(MeetingInferenceInProgressError, match="another worker"):
                _extract(peer, clock, case.case_id)
        finally:
            blocking.release.set()
        first = first_future.result(timeout=10)

    recovered = _extract(peer, clock, case.case_id)
    assert first.disposition is MeetingMinutesDisposition.CANDIDATES_RECORDED
    assert recovered.disposition is MeetingMinutesDisposition.DUPLICATE
    assert recovered.candidates == first.candidates
    assert blocking.calls == 1
    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    assert runtime.store.outbox_for_case(case.case_id) == ()
    assert peer.store.outbox_for_case(case.case_id) == ()
    peer.close()
    runtime.close()


def test_crashed_minutes_worker_is_recovered_after_lease_expiry(tmp_path):
    crashing = _CrashingMinutesExtractor()
    runtime, clock, settings, case, _prepared = _ready_runtime(
        tmp_path,
        crashing,
        db_name="minutes-inference-crash.db",
    )
    held_at = clock.now() - timedelta(minutes=30)

    def invoke(target):
        return target.extract_meeting_minutes(
            case.case_id,
            held_at=held_at,
            body_text=MINUTES,
            source_id="minutes:visitor-parking:2026-09-10",
            participant_count=3,
            governance=_governance(),
            simulated=True,
        )

    with pytest.raises(_SimulatedWorkerCrash):
        invoke(runtime)
    assert crashing.calls == 1

    recovery_extractor = _MinutesExtractor()
    peer = build_runtime(
        settings,
        classifier=_MeetingClassifier(),
        resolution_planner=_ResolutionPlanner(),
        meeting_agenda_planner=_AgendaPlanner(),
        meeting_minutes_extractor=recovery_extractor,
        clock=clock,
    )
    with pytest.raises(MeetingInferenceInProgressError):
        invoke(peer)
    assert recovery_extractor.calls == []

    clock.advance(timedelta(seconds=settings.inference_lease_seconds + 1))
    recovered = invoke(peer)
    assert recovered.disposition is MeetingMinutesDisposition.CANDIDATES_RECORDED
    assert len(recovery_extractor.calls) == 1
    assert runtime.store.outbox_for_case(case.case_id) == ()
    assert peer.store.outbox_for_case(case.case_id) == ()
    peer.close()
    runtime.close()



def test_completed_inference_recovers_candidate_persistence_without_model_recall(
    tmp_path,
):
    runtime, clock, _settings, case, _prepared = _ready_runtime(
        tmp_path,
        _FailIfCalled(),
        db_name="minutes-inference-result-recovery.db",
    )
    bundle = load_seed()
    failing_store = _FailCandidatePersistenceStore(runtime.store)
    planning = ResolutionPlanningService(
        store=failing_store,
        planner=_ResolutionPlanner(),
        vendors=tuple(bundle.vendors),
        clock=clock,
    )
    meeting = MeetingPreparationService(
        store=failing_store,
        planning=planning,
        agenda_planner=_AgendaPlanner(),
        vendors=tuple(bundle.vendors),
        clock=clock,
    )
    extractor = _MinutesExtractor()
    lifecycle = MeetingLifecycleService(
        store=failing_store,
        meeting=meeting,
        policy=PolicyEngine(bundle.policies, bundle.settings),
        extractor=extractor,
        clock=clock,
    )
    held_at = clock.now() - timedelta(minutes=30)
    request = {
        "case_id": case.case_id,
        "held_at": held_at,
        "body_text": MINUTES,
        "source_id": "minutes:visitor-parking:2026-09-10",
        "participant_count": 3,
        "governance": _governance(),
        "simulated": True,
        "idempotency_key": f"runtime:meeting-minutes:v1:{case.case_id}",
    }

    with pytest.raises(RuntimeError, match="after durable inference completion"):
        lifecycle.extract_minutes(**request)
    assert len(extractor.calls) == 1
    assert runtime.store.artifacts_for(
        kind=MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()

    recovery_planning = ResolutionPlanningService(
        store=runtime.store,
        planner=_ResolutionPlanner(),
        vendors=tuple(bundle.vendors),
        clock=clock,
    )
    recovery_meeting = MeetingPreparationService(
        store=runtime.store,
        planning=recovery_planning,
        agenda_planner=_AgendaPlanner(),
        vendors=tuple(bundle.vendors),
        clock=clock,
    )
    recovery = MeetingLifecycleService(
        store=runtime.store,
        meeting=recovery_meeting,
        policy=PolicyEngine(bundle.policies, bundle.settings),
        extractor=_FailIfCalled(),
        clock=clock,
    ).extract_minutes(**request)

    assert recovery.disposition is MeetingMinutesDisposition.CANDIDATES_RECORDED
    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_DECISION_CANDIDATES_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    assert runtime.store.outbox_for_case(case.case_id) == ()
    runtime.close()
