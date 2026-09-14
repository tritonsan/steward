from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock

import pytest
from pydantic import ValidationError

from steward.agents import (
    MeetingAgendaItem,
    MeetingAgendaItemKind,
    MeetingAgendaRecommendation,
    MeetingOpenQuestion,
    ResolutionPlanRecommendation,
    TriageAction,
    TriageResult,
)
from steward.config import StewardSettings
from steward.domain.clock import FrozenClock
from steward.domain.enums import CaseStatus, Category, EventKind, ResolutionPath, Urgency
from steward.mail import RecordingMailTransport
from steward.meetings import (
    MEETING_BRIEF_ARTIFACT_KIND,
    MEETING_PACKET_ARTIFACT_KIND,
    MeetingAvailabilityResponse,
    MeetingCandidateSlot,
    MeetingPreparationConflictError,
    MeetingPreparationDisposition,
    MeetingPreparationNotReadyError,
    MeetingPreparationResult,
    MeetingPreparationService,
    MeetingRecommendationIntegrityError,
    MeetingSchedulingPolicy,
)
from steward.orchestration import (
    RESOLUTION_CONTEXT_ARTIFACT_KIND,
    RESOLUTION_PLAN_ARTIFACT_KIND,
    ResolutionPlanningConflictError,
    ResolutionPlanningDisposition,
    ResolutionPlanningService,
    ResolutionRecommendationIntegrityError,
)
from steward.runtime import build_runtime
from steward.seed import load_seed

UTC = timezone.utc
SECRET = "meeting-workflow-webhook-secret-2026"
CHAT_ID = "-1002481179934"


class _MeetingClassifier:
    def classify(self, *, message, assets, open_cases):
        del message, assets, open_cases
        return TriageResult(
            action=TriageAction.OPEN_CASE,
            category=Category.MEETING_ADMIN,
            urgency=Urgency.NORMAL,
            confidence=0.96,
            title="Repeated disagreement about visitor parking",
            asset_id=None,
            rationale=(
                "Residents are asking for a shared rule rather than reporting a "
                "repair that can be sent directly to a vendor."
            ),
        )


class _MeetingResolutionPlanner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def plan(self, *, context_id: str, context: dict):
        del context_id
        self.calls.append(context)
        source_id = context["allowed_source_ids"][0]
        assert "sentinel-access" in context["offered_vendor_ids"]
        return ResolutionPlanRecommendation(
            path=ResolutionPath.MEETING_RESOLUTION,
            rationale=(
                "The community must agree a visitor-parking rule before any physical "
                "access-control or signage option is considered."
            ),
            source_ids=(source_id,),
            required_fact_codes=("current_parking_rule", "visitor_space_count"),
            relevant_vendor_ids=("sentinel-access",),
        )


class _MeetingAgendaPlanner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def prepare(self, *, brief_id: str, context: dict):
        del brief_id
        self.calls.append(context)
        source_id = context["messages"][0]["message_id"]
        vendor_id = context["vendor_options"][0]["vendor_id"]
        assert "email" not in context["vendor_options"][0]
        assert "phone" not in context["vendor_options"][0]
        return MeetingAgendaRecommendation(
            summary=(
                "Residents have repeatedly disagreed about visitor parking and need "
                "one written rule before operational changes are considered."
            ),
            summary_source_ids=(source_id,),
            agenda_items=(
                MeetingAgendaItem(
                    kind=MeetingAgendaItemKind.CONTEXT,
                    title="Current visitor-parking problem",
                    detail="Review the recurring disagreement and the rule currently in use.",
                    source_ids=(source_id,),
                ),
                MeetingAgendaItem(
                    kind=MeetingAgendaItemKind.DISCUSSION,
                    title="Operational options",
                    detail=(
                        "Review whether access-control changes could support the agreed rule; "
                        "no vendor has been contacted or selected."
                    ),
                    source_ids=(source_id, vendor_id),
                ),
                MeetingAgendaItem(
                    kind=MeetingAgendaItemKind.DECISION,
                    title="Decision required",
                    detail=(
                        "Agree the visitor-space numbering, permitted duration, and owner "
                        "for publishing the written rule."
                    ),
                    source_ids=(source_id,),
                ),
            ),
            open_questions=(
                MeetingOpenQuestion(
                    question="How many spaces must remain available for visitors?",
                    source_ids=(source_id,),
                ),
            ),
        )


class _FailOnceResolutionPlanner(_MeetingResolutionPlanner):
    def plan(self, *, context_id: str, context: dict):
        if not self.calls:
            self.calls.append(context)
            raise RuntimeError("temporary model failure; detail must not be persisted")
        return super().plan(context_id=context_id, context=context)


class _InvalidResolutionPlanner:
    def __init__(self, *, mode: str) -> None:
        self.mode = mode

    def plan(self, *, context_id: str, context: dict):
        del context_id
        source_id = context["allowed_source_ids"][0]
        if self.mode == "path":
            return ResolutionPlanRecommendation(
                path=ResolutionPath.PROCUREMENT,
                rationale="Attempted path escape.",
                source_ids=(source_id,),
            )
        if self.mode == "source":
            return ResolutionPlanRecommendation(
                path=ResolutionPath.MEETING_RESOLUTION,
                rationale="Attempted source escape.",
                source_ids=("source-not-offered",),
            )
        return ResolutionPlanRecommendation(
            path=ResolutionPath.MEETING_RESOLUTION,
            rationale="Attempted vendor escape.",
            source_ids=(source_id,),
            relevant_vendor_ids=("vendor-not-offered",),
        )


class _InvalidAgendaPlanner:
    def prepare(self, *, brief_id: str, context: dict):
        del brief_id
        source_id = context["messages"][0]["message_id"]
        return MeetingAgendaRecommendation(
            summary="A source escape was attempted.",
            summary_source_ids=(source_id,),
            agenda_items=(
                MeetingAgendaItem(
                    kind=MeetingAgendaItemKind.DECISION,
                    title="Invalid source",
                    detail="This item cites a source outside the frozen brief.",
                    source_ids=("attacker-controlled-source",),
                ),
            ),
        )


class _FailIfCalled:
    def plan(self, **kwargs):  # pragma: no cover - failure explains itself
        raise AssertionError(f"resolution model was called after restart: {kwargs}")

    def prepare(self, **kwargs):  # pragma: no cover - failure explains itself
        raise AssertionError(f"agenda model was called after restart: {kwargs}")


class _BarrierArtifactStore:
    """Synchronize the first two singleton preflight reads across two workers."""

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


def _update(at: datetime):
    return {
        "update_id": 88101,
        "message": {
            "message_id": 66101,
            "date": int(at.timestamp()),
            "chat": {"id": int(CHAT_ID), "type": "supergroup"},
            "from": {"id": 12345, "is_bot": False, "first_name": "James"},
            "text": (
                "Visitor parking has caused another argument. We need one written rule "
                "that everyone can follow, not another temporary chat agreement."
            ),
        },
    }


def _scheduling_inputs(now: datetime):
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
    policy = MeetingSchedulingPolicy(
        timezone="UTC",
        minimum_notice_hours=24,
        quorum=2,
        eligible_participant_ids=("member-simon", "member-james", "member-amelia"),
        required_participant_ids=("member-simon",),
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


def _open_meeting_case(tmp_path, *, planner=None, agenda_planner=None, db_name="meeting.db"):
    at = datetime(2026, 9, 7, 10, tzinfo=UTC)
    clock = FrozenClock(at)
    resolution_planner = planner or _MeetingResolutionPlanner()
    meeting_planner = agenda_planner or _MeetingAgendaPlanner()
    settings = StewardSettings(
        database_path=tmp_path / db_name,
        telegram_enabled=True,
        telegram_webhook_secret=SECRET,
        telegram_allowed_chat_ids=frozenset({CHAT_ID}),
    )
    runtime = build_runtime(
        settings,
        classifier=_MeetingClassifier(),
        resolution_planner=resolution_planner,
        meeting_agenda_planner=meeting_planner,
        clock=clock,
    )
    runtime.handle_telegram_update(_update(at), secret_header=SECRET)
    # These service tests start before automatic planning; the product runtime
    # continuation is covered separately through authenticated API events.
    report = runtime._runner.tick()
    assert len(report.intake_outcomes) == 1
    case = report.intake_outcomes[0].case
    assert case is not None
    return runtime, clock, settings, case, resolution_planner, meeting_planner


def test_meeting_vertical_slice_is_durable_source_traced_and_outbound_disabled(tmp_path):
    runtime, clock, _settings, case, planner, agenda_planner = _open_meeting_case(
        tmp_path
    )
    clock.advance(timedelta(hours=1))

    planning = runtime.plan_resolution(case.case_id)
    policy, slots, availability = _scheduling_inputs(clock.now())
    prepared = runtime.prepare_meeting(
        case.case_id,
        policy=policy,
        candidate_slots=slots,
        availability=availability,
    )

    assert planning.disposition is ResolutionPlanningDisposition.PLAN_RECORDED
    assert planning.plan.recommendation.path is ResolutionPath.MEETING_RESOLUTION
    assert planning.plan.model_output_is_authority is False
    assert planning.plan.outbound_enabled is False
    assert len(planner.calls) == 1
    assert prepared.disposition is MeetingPreparationDisposition.PACKET_PREPARED
    assert prepared.case.status is CaseStatus.MEETING_READY
    assert prepared.packet.schedule.selected_slot.slot_id == "slot-wednesday"
    assert prepared.packet.schedule.available_participant_count == 3
    assert prepared.packet.schedule.selected_by_model is False
    assert prepared.packet.vendor_contacts[0].vendor_id == "sentinel-access"
    assert prepared.packet.vendor_contacts[0].email == (
        "sentinel@vendors.narrativenode-labs.cloud"
    )
    assert prepared.packet.vendor_contacts[0].simulated is True
    assert prepared.packet.model_output_is_authority is False
    assert prepared.packet.approval_requested is False
    assert prepared.packet.outbound_enabled is False
    assert prepared.packet.final_send_blocked is True
    assert prepared.packet.sent is False
    assert len(prepared.packet.payload_sha256) == 64
    assert len(agenda_planner.calls) == 1

    assert runtime.store.artifact(
        RESOLUTION_CONTEXT_ARTIFACT_KIND,
        planning.context.context_id,
    ) is not None
    assert runtime.store.artifact(
        RESOLUTION_PLAN_ARTIFACT_KIND,
        planning.plan.plan_id,
    ) is not None
    assert runtime.store.artifact(
        MEETING_BRIEF_ARTIFACT_KIND,
        prepared.brief.brief_id,
    ) is not None
    assert runtime.store.artifact(
        MEETING_PACKET_ARTIFACT_KIND,
        prepared.packet.packet_id,
    ) is not None
    assert runtime.store.outbox_for_case(case.case_id) == ()
    assert isinstance(runtime.transport, RecordingMailTransport)
    assert runtime.transport.sent == []
    event_kinds = {event.kind for event in runtime.store.timeline_for(case.case_id)}
    assert EventKind.RESOLUTION_PLANNED in event_kinds
    assert EventKind.MEETING_SCHEDULE_SELECTED in event_kinds
    assert EventKind.MEETING_PACKET_PREPARED in event_kinds
    runtime.close()


def test_resolution_context_survives_model_failure_and_retry(tmp_path):
    planner = _FailOnceResolutionPlanner()
    runtime, _clock, _settings, case, _planner, _agenda = _open_meeting_case(
        tmp_path,
        planner=planner,
        db_name="planning-retry.db",
    )

    with pytest.raises(RuntimeError, match="temporary model failure"):
        runtime.plan_resolution(case.case_id)

    contexts = runtime.store.artifacts_for(
        kind=RESOLUTION_CONTEXT_ARTIFACT_KIND,
        case_id=case.case_id,
    )
    assert len(contexts) == 1
    assert runtime.store.artifacts_for(
        kind=RESOLUTION_PLAN_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()

    recovered = runtime.plan_resolution(case.case_id)
    assert recovered.disposition is ResolutionPlanningDisposition.PLAN_RECORDED
    assert len(planner.calls) == 2
    runtime.close()


@pytest.mark.parametrize("mode", ["path", "source", "vendor"])
def test_resolution_planner_cannot_escape_closed_sets(tmp_path, mode):
    runtime, _clock, _settings, case, _planner, _agenda = _open_meeting_case(
        tmp_path,
        planner=_InvalidResolutionPlanner(mode=mode),
        db_name=f"invalid-resolution-{mode}.db",
    )

    with pytest.raises(ResolutionRecommendationIntegrityError):
        runtime.plan_resolution(case.case_id)

    assert runtime.store.artifacts_for(
        kind=RESOLUTION_PLAN_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()
    assert runtime.store.outbox_for_case(case.case_id) == ()
    runtime.close()


def test_meeting_agenda_cannot_escape_frozen_sources(tmp_path):
    runtime, clock, _settings, case, _planner, _agenda = _open_meeting_case(
        tmp_path,
        agenda_planner=_InvalidAgendaPlanner(),
        db_name="invalid-agenda.db",
    )
    clock.advance(timedelta(hours=1))
    runtime.plan_resolution(case.case_id)
    policy, slots, availability = _scheduling_inputs(clock.now())

    with pytest.raises(MeetingRecommendationIntegrityError):
        runtime.prepare_meeting(
            case.case_id,
            policy=policy,
            candidate_slots=slots,
            availability=availability,
        )

    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_BRIEF_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    assert runtime.store.artifacts_for(
        kind=MEETING_PACKET_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()
    assert runtime.store.outbox_for_case(case.case_id) == ()
    runtime.close()


def test_schedule_fails_closed_without_quorum_or_required_participant(tmp_path):
    runtime, clock, _settings, case, _planner, agenda = _open_meeting_case(
        tmp_path,
        db_name="quorum.db",
    )
    clock.advance(timedelta(hours=1))
    runtime.plan_resolution(case.case_id)
    policy, slots, _availability = _scheduling_inputs(clock.now())
    unavailable_required = (
        MeetingAvailabilityResponse(
            participant_id="member-simon",
            source_id="availability:simon:none",
            available_slot_ids=(),
        ),
        MeetingAvailabilityResponse(
            participant_id="member-james",
            source_id="availability:james:1",
            available_slot_ids=("slot-tuesday", "slot-wednesday"),
        ),
        MeetingAvailabilityResponse(
            participant_id="member-amelia",
            source_id="availability:amelia:1",
            available_slot_ids=("slot-tuesday", "slot-wednesday"),
        ),
    )

    with pytest.raises(MeetingPreparationNotReadyError, match="no candidate"):
        runtime.prepare_meeting(
            case.case_id,
            policy=policy,
            candidate_slots=slots,
            availability=unavailable_required,
        )

    assert agenda.calls == []
    assert runtime.store.artifacts_for(
        kind=MEETING_BRIEF_ARTIFACT_KIND,
        case_id=case.case_id,
    ) == ()
    runtime.close()


def test_restart_loads_same_plan_and_packet_without_model_calls(tmp_path):
    runtime, clock, settings, case, _planner, _agenda = _open_meeting_case(
        tmp_path,
        db_name="restart.db",
    )
    clock.advance(timedelta(hours=1))
    first_plan = runtime.plan_resolution(case.case_id)
    policy, slots, availability = _scheduling_inputs(clock.now())
    first_packet = runtime.prepare_meeting(
        case.case_id,
        policy=policy,
        candidate_slots=slots,
        availability=availability,
    )
    runtime.close()

    restarted = build_runtime(
        settings,
        classifier=_MeetingClassifier(),
        resolution_planner=_FailIfCalled(),
        meeting_agenda_planner=_FailIfCalled(),
        clock=clock,
    )
    duplicate_plan = restarted.plan_resolution(case.case_id)
    duplicate_packet = restarted.prepare_meeting(
        case.case_id,
        policy=policy,
        candidate_slots=slots,
        availability=availability,
    )

    assert duplicate_plan.disposition is ResolutionPlanningDisposition.DUPLICATE
    assert duplicate_plan.plan == first_plan.plan
    assert duplicate_packet.disposition is MeetingPreparationDisposition.DUPLICATE
    assert duplicate_packet.packet == first_packet.packet
    assert restarted.store.outbox_for_case(case.case_id) == ()
    restarted.close()


def test_immutable_snapshots_reject_new_idempotency_keys(tmp_path):
    runtime, clock, _settings, case, _planner, _agenda = _open_meeting_case(
        tmp_path,
        db_name="conflict.db",
    )
    clock.advance(timedelta(hours=1))
    runtime.plan_resolution(case.case_id)

    with pytest.raises(ResolutionPlanningConflictError):
        runtime.plan_resolution(case.case_id, idempotency_key="different-plan-key")

    policy, slots, availability = _scheduling_inputs(clock.now())
    runtime.prepare_meeting(
        case.case_id,
        policy=policy,
        candidate_slots=slots,
        availability=availability,
    )
    with pytest.raises(MeetingPreparationConflictError):
        runtime.prepare_meeting(
            case.case_id,
            policy=policy,
            candidate_slots=slots,
            availability=availability,
            idempotency_key="different-meeting-key",
        )
    runtime.close()


def test_concurrent_different_plan_keys_leave_one_recoverable_snapshot(tmp_path):
    runtime, clock, _settings, case, _unused, _agenda = _open_meeting_case(
        tmp_path,
        db_name="concurrent-plan.db",
    )
    clock.advance(timedelta(hours=1))
    planner = _MeetingResolutionPlanner()
    barrier_store = _BarrierArtifactStore(
        runtime.store,
        kind=RESOLUTION_CONTEXT_ARTIFACT_KIND,
    )
    service = ResolutionPlanningService(
        store=barrier_store,
        planner=planner,
        vendors=load_seed().vendors,
        clock=clock,
    )

    def invoke(key: str):
        try:
            return service.plan(case_id=case.case_id, idempotency_key=key)
        except Exception as exc:  # noqa: BLE001 - outcome is asserted below
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(invoke, ("plan-key-a", "plan-key-b")))

    successes = [item for item in outcomes if not isinstance(item, Exception)]
    conflicts = [
        item for item in outcomes if isinstance(item, ResolutionPlanningConflictError)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert len(planner.calls) == 1
    assert len(
        runtime.store.artifacts_for(
            kind=RESOLUTION_CONTEXT_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    assert len(
        runtime.store.artifacts_for(
            kind=RESOLUTION_PLAN_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    loaded = service.load(case.case_id)
    assert loaded is not None
    assert loaded.plan == successes[0].plan
    runtime.close()


def test_concurrent_different_meeting_keys_leave_one_recoverable_packet(tmp_path):
    runtime, clock, _settings, case, _unused, _agenda = _open_meeting_case(
        tmp_path,
        db_name="concurrent-meeting.db",
    )
    clock.advance(timedelta(hours=1))
    runtime.plan_resolution(case.case_id)
    bundle = load_seed()
    planning = ResolutionPlanningService(
        store=runtime.store,
        planner=_FailIfCalled(),
        vendors=bundle.vendors,
        clock=clock,
    )
    barrier_store = _BarrierArtifactStore(
        runtime.store,
        kind=MEETING_BRIEF_ARTIFACT_KIND,
    )
    agenda = _MeetingAgendaPlanner()
    service = MeetingPreparationService(
        store=barrier_store,
        planning=planning,
        agenda_planner=agenda,
        vendors=bundle.vendors,
        clock=clock,
    )
    policy, slots, availability = _scheduling_inputs(clock.now())

    def invoke(key: str):
        try:
            return service.prepare(
                case_id=case.case_id,
                policy=policy,
                candidate_slots=slots,
                availability=availability,
                idempotency_key=key,
            )
        except Exception as exc:  # noqa: BLE001 - outcome is asserted below
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(invoke, ("meeting-key-a", "meeting-key-b")))

    successes = [
        item for item in outcomes if isinstance(item, MeetingPreparationResult)
    ]
    conflicts = [
        item for item in outcomes if isinstance(item, MeetingPreparationConflictError)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert len(agenda.calls) == 1
    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_BRIEF_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    assert len(
        runtime.store.artifacts_for(
            kind=MEETING_PACKET_ARTIFACT_KIND,
            case_id=case.case_id,
        )
    ) == 1
    assert runtime.store.outbox_for_case(case.case_id) == ()
    runtime.close()


def test_model_schemas_structurally_forbid_authority_and_contact_fields():
    with pytest.raises(ValidationError):
        ResolutionPlanRecommendation.model_validate(
            {
                "path": "meeting_resolution",
                "rationale": "A meeting is needed.",
                "source_ids": ["message-1"],
                "required_fact_codes": [],
                "relevant_vendor_ids": [],
                "recipient": "resident@example.com",
            }
        )
    with pytest.raises(ValidationError):
        MeetingAgendaRecommendation.model_validate(
            {
                "summary": "A meeting is needed.",
                "summary_source_ids": ["message-1"],
                "agenda_items": [
                    {
                        "kind": "decision",
                        "title": "Agree a rule",
                        "detail": "Choose one written rule.",
                        "source_ids": ["message-1"],
                    }
                ],
                "open_questions": [],
                "vendor_email": "invented@example.com",
            }
        )
